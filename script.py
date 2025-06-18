import os
import json
import time
import random
import logging
import requests
import pandas as pd
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain.schema import SystemMessage, HumanMessage, AIMessage
from flask import Flask, request, jsonify, current_app
from dotenv import load_dotenv
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import re
import tempfile
from openai import OpenAI
from googleapiclient.discovery import build
from google.oauth2 import service_account
import pytz
import smtplib
from email.mime.text import MIMEText
import property_handler

# Ensure rag_handler.py is in the same directory or accessible via PYTHONPATH
from rag_handler import (
    initialize_vector_store,
    process_document,
    query_vector_store,
    get_processed_files_log,
    remove_document_from_store,
    process_google_document_text
)
from google_drive_handler import (
    get_google_drive_file_mime_type,
    get_google_doc_content,
    get_google_sheet_content
)
from outreach_handler import process_outreach_campaign
# send_interactive_list_message is added to this import
from whatsapp_utils import (
    send_whatsapp_message,
    send_whatsapp_image_message,
    set_webhook,
    send_interactive_list_message,
    send_initial_greeting_message,
    send_furnished_query_message,
    send_furnished_apartment_survey_message,
    send_unfurnished_apartment_survey_message,
    send_city_selection_message
)


# ─── Data Ingestion Configuration ──────────────────────────────────────────────
COMPANY_DATA_FOLDER = 'company_data'

# New, simple state tracker for the sell property flow
sell_flow_states = {}

# State tracker for the new interactive flow
interactive_flow_states = {}
user_languages = {} # Stores language per user session

# Hardcoded data for UAE cities and areas for the list messages
UAE_CITIES = ["Dubai", "Abu Dhabi", "Sharjah", "Ajman", "Ras Al Khaimah", "Fujairah", "Umm Al Quwain"]
UAE_AREAS = {
    "Dubai": ["Dubai Marina", "Downtown Dubai", "Palm Jumeirah", "JVC", "Business Bay", "Arabian Ranches", "Other"],
    "Abu Dhabi": ["Al Reem Island", "Saadiyat Island", "Yas Island", "Khalifa City", "Al Raha Beach", "Other"],
    "Sharjah": ["Al Majaz", "Al Nahda", "Muwaileh", "Al Khan", "Other"],
    "Ajman": ["Al Rashidiya", "Ajman Downtown", "Al Jurf", "Other"],
    "Ras Al Khaimah": ["Al Hamra Village", "Mina Al Arab", "Al Marjan Island", "Other"],
    "Fujairah": ["Fujairah City", "Dibba", "Other"],
    "Umm Al Quwain": ["Umm Al Quwain City", "Al Salamah", "Other"]
}

# ─── Google Calendar Configuration ─────────────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/calendar']

# === NEW TIMEZONE STRATEGY ===
# Timezone for user interaction, AI interpretation, and display
TARGET_DISPLAY_TIMEZONE = pytz.timezone('Asia/Dubai')
# Timezone for storing events in Google Calendar (workaround)
EVENT_STORAGE_TIMEZONE = pytz.timezone('America/New_York')
# Global TIMEZONE used by create_calendar_event and check_availability for localizing naive datetimes
TIMEZONE = EVENT_STORAGE_TIMEZONE # This is now New York
# ==============================

# --- Operational Hours Configuration (Asia/Dubai timezone) ---
OPERATIONAL_START_HOUR_DUBAI = 20 # 8 PM
OPERATIONAL_END_HOUR_DUBAI = 8    # 8 AM
DUBAI_TIMEZONE = pytz.timezone('Asia/Dubai') # This is the same as TARGET_DISPLAY_TIMEZONE, defined for clarity here

# ─── Load environment and configure AI ─────────────────────────────────────────
load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PROPERTY_SHEET_ID = os.getenv('PROPERTY_SHEET_ID')
PROPERTY_SHEET_NAME = os.getenv('PROPERTY_SHEET_NAME', 'Properties')

# --- Global Pause Feature ---
# These variables are in-memory and will be reset if the Flask app restarts or is redeployed.
is_globally_paused = False
paused_conversations = set()

# ─── Persona loading ───────────────────────────────────────────────────────────
PERSONA_FILE = 'persona.json'
PERSONA_NAME = "mosaed (مساعد)" # Kept for informational purposes / non-LLM uses

# Renamed to indicate it's a template and name will be added dynamically
BASE_PROMPT_TEMPLATE = (
    "You are a helpful and friendly assistant from Al-Ouja Property Management (شركة عوجا لإدارة الأملاك). "
    "Your primary goal is to guide users through options using interactive messages. "
    "Your tone is polite, professional, and uses a Saudi dialect when the user communicates in Arabic. "
    "CRITICAL LANGUAGE RULE: Your response MUST ALWAYS be in the SAME language as the user's last message. If the user messages in English, you reply in English. If they message in Arabic, you MUST reply in Saudi dialect. "
    "If providing information directly (not via interactive message), keep it concise. "
    "If a user asks a question that can be answered by one of the interactive flow options, try to steer them towards that flow. "
    "If the query is not covered by an interactive flow, use the provided 'Relevant Information Found' to answer. "
    "If the context does not sufficiently answer the query, state that you will check for that specific detail and get back to them, appending `[ACTION_NOTIFY_UNANSWERED_QUERY]`. "
    "TEXT STYLING: No emojis, asterisks, or markdown. Plain text only. "
    # Greeting part is now handled dynamically in get_llm_response by appending a language-specific example.
)

try:
    with open(PERSONA_FILE) as f:
        p = json.load(f)
    logging.info(f"Original persona name from {PERSONA_FILE} was '{p.get('name')}'. Script now uses dynamic naming ('Mosaed'/'مساعد') for LLM prompts based on BASE_PROMPT_TEMPLATE.")
except Exception as e:
    logging.warning(f"Could not load {PERSONA_FILE} or parse it: {e}. Using dynamic naming ('Mosaed'/'مساعد') for LLM prompts based on BASE_PROMPT_TEMPLATE.")

# ─── AI Model and API Client Initialization ────────────────────────────────────
AI_MODEL = None
if OPENAI_API_KEY:
    AI_MODEL = ChatOpenAI(model_name='gpt-4o', openai_api_key=OPENAI_API_KEY, temperature=0)
else:
    logging.error("OPENAI_API_KEY not found; AI responses will fail.")

if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    openai_client = None

# HTTP_SESSION for WaSender is now managed in whatsapp_utils.py
# HTTP_SESSION = requests.Session() # Removed

# ─── Flask setup ───────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize ThreadPoolExecutor
# max_workers can be adjusted based on expected load and server resources.
# For I/O bound tasks like calling external APIs (which RAG processing might do),
# a higher number of workers might be beneficial. Let's start with 2.
executor = ThreadPoolExecutor(max_workers=2)

# ─── Google Calendar Setup ─────────────────────────────────────────────────────
def get_calendar_service():
    """Get Google Calendar service object using service account credentials."""
    try:
        credentials_json = os.getenv('GOOGLE_CALENDAR_CREDENTIALS')
        if not credentials_json:
            logging.error("GOOGLE_CALENDAR_CREDENTIALS environment variable not found")
            return None

        credentials_info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=SCOPES
        )
        service = build('calendar', 'v3', credentials=credentials)
        return service

    except Exception as e:
        logging.error(f"Error initializing calendar service: {e}")
        return None

# ─── Appointment Intent Detection and Extraction ───────────────────────────────
def detect_scheduling_intent(message):
    """Detect if the message contains scheduling intent."""
    scheduling_keywords = [
        'appointment', 'schedule', 'book', 'booking', 'meeting', 'consultation',
        'reserve', 'reservation', 'visit', 'session', 'call', 'meet'
    ]

    time_indicators = [
        'today', 'tomorrow', 'next week', 'monday', 'tuesday', 'wednesday', 'thursday',
        'friday', 'saturday', 'sunday', 'am', 'pm', 'morning', 'afternoon', 'evening',
        'at', 'on', 'o\'clock', ':', 'time'
    ]

    message_lower = message.lower()

    has_scheduling_keyword = any(keyword in message_lower for keyword in scheduling_keywords)
    has_time_indicator = any(indicator in message_lower for indicator in time_indicators)

    date_patterns = [
        r'\d{1,2}[/-]\d{1,2}',
        r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}',
        r'\b\d{1,2}(st|nd|rd|th)\b',
    ]

    has_date_pattern = any(re.search(pattern, message_lower) for pattern in date_patterns)
    return has_scheduling_keyword or (has_time_indicator and has_date_pattern)

# === DATETIME EXTRACTION FUNCTION (extract_datetime_with_ai) ===
def extract_datetime_with_ai(message):
    """Use AI to extract and normalize datetime information from natural language, assuming Dubai time."""
    current_display_time = datetime.now(TARGET_DISPLAY_TIMEZONE)

    extraction_prompt = f"""
    Extract date and time information from this message: "{message}"

    Current date and time in Dubai: {current_display_time.strftime('%Y-%m-%d %H:%M')} ({TARGET_DISPLAY_TIMEZONE.zone} timezone)

    Please respond with ONLY a JSON object in this exact format:
    {{
        "has_datetime": true/false,
        "date": "YYYY-MM-DD" or null,
        "time": "HH:MM" or null,
        "duration_minutes": number or 60,
        "service_type": "extracted service name" or "General Consultation",
        "confidence": 0.0-1.0
    }}

    Rules:
    - If no specific date is mentioned but "today" is implied, use today's date in Dubai timezone ({TARGET_DISPLAY_TIMEZONE.zone})
    - If "tomorrow" is mentioned, use tomorrow's date in Dubai timezone ({TARGET_DISPLAY_TIMEZONE.zone})
    - If a day of the week is mentioned without a date, use the next occurrence of that day
    - If no time is specified, return null for time
    - Default duration is 60 minutes unless specified
    - Extract any service type mentioned (consultation, meeting, checkup, etc.)
    - Confidence should reflect how certain you are about the extraction
    - All dates and times extracted should be interpreted as local time for {TARGET_DISPLAY_TIMEZONE.zone}
    """

    try:
        if not AI_MODEL:
            logging.error("AI_MODEL not initialized in extract_datetime_with_ai")
            return None

        response = AI_MODEL.invoke(extraction_prompt)
        response_text = response.content.strip()

        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0].strip()
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0].strip()

        result = json.loads(response_text)
        logging.info(f"AI extracted datetime (interpreted as Dubai time): {result}")
        return result

    except Exception as e:
        logging.error(f"Error extracting datetime with AI: {e}")
        return None
# === END OF DATETIME EXTRACTION FUNCTION ===

# === CALENDAR EVENT CREATION FUNCTION (create_calendar_event) ===
def create_calendar_event(gcal_service, title, start_datetime_naive_event_tz, end_datetime_naive_event_tz, description="", attendee_email=None):
    """
    Create a Google Calendar event.
    Receives naive datetimes assumed to be in EVENT_STORAGE_TIMEZONE (e.g., New York).
    Localizes them to EVENT_STORAGE_TIMEZONE and creates the event in that timezone.
    """
    logging.info("CALENDAR_DEBUG: create_calendar_event called with title: %s, start_naive_event_tz: %s", title, start_datetime_naive_event_tz)
    try:
        if gcal_service is None:
            logging.error("Google Calendar service is not initialized. Cannot create event.")
            return None

        # Localize naive datetimes to EVENT_STORAGE_TIMEZONE (e.g., New York)
        if start_datetime_naive_event_tz.tzinfo is None:
            start_datetime_event_tz_aware = TIMEZONE.localize(start_datetime_naive_event_tz)
        else:
            logging.warning("create_calendar_event received an already aware start_datetime. Converting to EVENT_STORAGE_TIMEZONE.")
            start_datetime_event_tz_aware = start_datetime_naive_event_tz.astimezone(TIMEZONE)

        if end_datetime_naive_event_tz.tzinfo is None:
            end_datetime_event_tz_aware = TIMEZONE.localize(end_datetime_naive_event_tz)
        else:
            logging.warning("create_calendar_event received an already aware end_datetime. Converting to EVENT_STORAGE_TIMEZONE.")
            end_datetime_event_tz_aware = end_datetime_naive_event_tz.astimezone(TIMEZONE)

        logging.info(f"Creating event with {TIMEZONE.zone} times - Start: {start_datetime_event_tz_aware}, End: {end_datetime_event_tz_aware}")

        event_body = {
            'summary': title,
            'description': description,
            'start': {
                'dateTime': start_datetime_event_tz_aware.isoformat(),
                'timeZone': TIMEZONE.zone,
            },
            'end': {
                'dateTime': end_datetime_event_tz_aware.isoformat(),
                'timeZone': TIMEZONE.zone,
            },
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 24 * 60},
                    {'method': 'popup', 'minutes': 10},
                ],
            },
        }

        if attendee_email:
            event_body['attendees'] = [{'email': attendee_email}]

        logging.info(f"Creating calendar event with payload: {json.dumps(event_body, indent=2)}")
        # Assuming 'mohomer12@gmail.com' is the target calendar ID
        created_event_response = gcal_service.events().insert(calendarId='mohomer12@gmail.com', body=event_body).execute()

        if created_event_response and created_event_response.get('id'):
            logging.info(f"CALENDAR_DEBUG: Event insertion successful. Event ID: {created_event_response.get('id')}, HTML Link: {created_event_response.get('htmlLink')}")
        else:
            logging.error(f"CALENDAR_DEBUG: Event insertion FAILED or returned unexpected response. Response: {created_event_response}")

        logging.info(f"Event supposedly created. Raw Google API response: {json.dumps(created_event_response, indent=2)}")

        if created_event_response and created_event_response.get('id'):
            event_id_to_check = created_event_response.get('id')
            logging.info(f"DIAGNOSTIC: Attempting to retrieve event with ID '{event_id_to_check}' from calendar 'mohomer12@gmail.com' immediately.")
            try:
                retrieved_event_check = gcal_service.events().get(calendarId='mohomer12@gmail.com', eventId=event_id_to_check).execute()
                logging.info(f"DIAGNOSTIC SUCCESS: Successfully retrieved event by ID. Summary: '{retrieved_event_check.get('summary')}', Link: '{retrieved_event_check.get('htmlLink')}'")
                logging.info(f"DIAGNOSTIC SUCCESS: Retrieved event details: {json.dumps(retrieved_event_check, indent=2)}")
            except Exception as e_get:
                logging.error(f"DIAGNOSTIC CRITICAL: FAILED to retrieve event by ID '{event_id_to_check}' from calendar 'mohomer12@gmail.com' immediately after creation. Error: {e_get}")
        else:
            logging.warning("DIAGNOSTIC: Created event object is missing or does not have an ID. Cannot attempt to retrieve by ID.")

        return created_event_response

    except Exception as e:
        logging.error(f"Error creating calendar event: {e}", exc_info=True)
        return None
# === END OF CALENDAR EVENT CREATION FUNCTION ===

# === CALENDAR AVAILABILITY CHECK FUNCTION (check_availability) ===
def check_availability(gcal_service, start_datetime_naive_event_tz, end_datetime_naive_event_tz):
    """
    Check if the requested time slot is available.
    Receives naive datetimes assumed to be in EVENT_STORAGE_TIMEZONE (e.g., New York).
    Localizes them, then converts to UTC for the API call.
    """
    try:
        if gcal_service is None:
            logging.error("Google Calendar service is not initialized. Cannot check availability.")
            return False # Assume not available if service is down

        if start_datetime_naive_event_tz.tzinfo is None:
            start_datetime_event_tz_aware = TIMEZONE.localize(start_datetime_naive_event_tz)
        else:
            start_datetime_event_tz_aware = start_datetime_naive_event_tz.astimezone(TIMEZONE)

        if end_datetime_naive_event_tz.tzinfo is None:
            end_datetime_event_tz_aware = TIMEZONE.localize(end_datetime_naive_event_tz)
        else:
            end_datetime_event_tz_aware = end_datetime_naive_event_tz.astimezone(TIMEZONE)

        start_utc = start_datetime_event_tz_aware.astimezone(pytz.UTC)
        end_utc = end_datetime_event_tz_aware.astimezone(pytz.UTC)

        logging.info(f"Checking availability - {TIMEZONE.zone} times: {start_datetime_event_tz_aware} to {end_datetime_event_tz_aware}")
        logging.info(f"Checking availability - UTC times for API: {start_utc.isoformat()} to {end_utc.isoformat()}")

        events_result = gcal_service.events().list(
            calendarId='mohomer12@gmail.com', # Assuming 'mohomer12@gmail.com'
            timeMin=start_utc.isoformat(),
            timeMax=end_utc.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])
        logging.info(f"Found {len(events)} existing events in the time slot (checked using {TIMEZONE.zone} converted to UTC)")
        return len(events) == 0

    except Exception as e:
        logging.error(f"Error checking availability: {e}", exc_info=True)
        return False
# === END OF CALENDAR AVAILABILITY CHECK FUNCTION ===

# ─── Data Ingestion Function ───────────────────────────────────────────────────
def scan_company_data_folder(vector_store: object, embeddings: object):
    logging.info(f"Starting scan of company data folder: {COMPANY_DATA_FOLDER}")
    if not vector_store or not embeddings:
        logging.error("scan_company_data_folder: Vector store or embeddings not initialized. Aborting scan.")
        return
    if not os.path.exists(COMPANY_DATA_FOLDER) or not os.path.isdir(COMPANY_DATA_FOLDER):
        logging.error(f"scan_company_data_folder: Company data folder '{COMPANY_DATA_FOLDER}' not found or is not a directory. Aborting scan.")
        return
    processed_log = get_processed_files_log()
    current_file_paths_in_folder = []
    try:
        for filename in os.listdir(COMPANY_DATA_FOLDER):
            full_path = os.path.join(COMPANY_DATA_FOLDER, filename)
            if os.path.isfile(full_path):
                if filename.endswith(('.txt', '.pdf')) and not filename.startswith('.'):
                    current_file_paths_in_folder.append(full_path)
                elif not filename.startswith('.'):
                    logging.debug(f"scan_company_data_folder: Skipping unsupported file type or hidden file: {filename}")
    except Exception as e:
        logging.error(f"scan_company_data_folder: Error listing files in {COMPANY_DATA_FOLDER}: {e}", exc_info=True)
        return
    for file_path in current_file_paths_in_folder:
        try:
            file_mtime = os.path.getmtime(file_path)
            file_info = processed_log.get(file_path)
            if file_info and file_info.get('mtime') == file_mtime and file_info.get('status') == 'processed':
                logging.debug(f"scan_company_data_folder: File '{file_path}' is unchanged and already processed. Skipping.")
                continue
            logging.info(f"scan_company_data_folder: Processing new or modified file: {file_path}")
            process_document(file_path, vector_store, embeddings)
        except FileNotFoundError:
            logging.warning(f"scan_company_data_folder: File '{file_path}' found during scan but disappeared before processing. Skipping.")
        except Exception as e:
            logging.error(f"scan_company_data_folder: Error processing file '{file_path}': {e}", exc_info=True)
    logged_file_paths = list(processed_log.keys())
    for file_path in logged_file_paths:
        if file_path.startswith(os.path.abspath(COMPANY_DATA_FOLDER) + os.sep):
            if file_path not in current_file_paths_in_folder and processed_log.get(file_path, {}).get('status') == 'processed':
                logging.info(f"scan_company_data_folder: File '{file_path}' appears to be removed from source folder.")
                remove_document_from_store(file_path, vector_store)
        elif file_path.startswith(COMPANY_DATA_FOLDER + os.sep): # Handle relative paths if stored that way
             if file_path not in current_file_paths_in_folder and processed_log.get(file_path, {}).get('status') == 'processed':
                logging.info(f"scan_company_data_folder: File '{file_path}' (relative) appears to be removed from source folder.")
                remove_document_from_store(file_path, vector_store) # Assumes remove_document_from_store can handle relative path
    logging.info(f"Scan of company data folder: {COMPANY_DATA_FOLDER} complete.")

# ─── RAG Initialization ────────────────────────────────────────────────────────
logging.info("Initializing RAG components...")
OPENAI_API_KEY_RAG = os.getenv('OPENAI_API_KEY_RAG', os.getenv('OPENAI_API_KEY'))
embeddings_rag = None
vector_store_rag = None
if OPENAI_API_KEY_RAG:
    try:
        embeddings_rag = OpenAIEmbeddings(model="text-embedding-ada-002", openai_api_key=OPENAI_API_KEY_RAG)
        vector_store_rag = initialize_vector_store()
        if 'app' in globals() and app: # Check if Flask app context exists
            app.config['EMBEDDINGS'] = embeddings_rag
            app.config['VECTOR_STORE'] = vector_store_rag
        else:
            logging.warning("Flask app context not available during RAG init. Storing embeddings/vector_store globally for now.")
        if vector_store_rag and embeddings_rag:
            logging.info("RAG components initialized successfully.")
            scan_company_data_folder(vector_store_rag, embeddings_rag)
        else:
            logging.error("Failed to initialize RAG components (vector_store or embeddings). RAG functionality might be impaired.")
            if 'app' in globals() and app:
                app.config['EMBEDDINGS'] = None
                app.config['VECTOR_STORE'] = None
    except Exception as e:
        logging.error(f"Error initializing RAG components: {e}", exc_info=True)
        if 'app' in globals() and app:
            app.config['EMBEDDINGS'] = None
            app.config['VECTOR_STORE'] = None
else:
    logging.error("OPENAI_API_KEY_RAG (or OPENAI_API_KEY) not found; RAG functionality will be disabled.")
    if 'app' in globals() and app:
        app.config['EMBEDDINGS'] = None
        app.config['VECTOR_STORE'] = None

# ─── Initialize Google Calendar Service ────────────────────────────────────────
CALENDAR_SERVICE = get_calendar_service()
if CALENDAR_SERVICE:
    logging.info("CALENDAR_CREDENTIAL_VERIFICATION: Google Calendar service initialized successfully using provided credentials.")
else:
    logging.error("CALENDAR_CREDENTIAL_VERIFICATION: FAILED to initialize Google Calendar service. Check credentials and API permissions.")

if CALENDAR_SERVICE:
    logging.info("Google Calendar service initialized successfully.")
else:
    logging.warning("Google Calendar service could not be initialized. Appointment scheduling will be disabled.")

# ─── Email Sending Function for Appointment Requests ──────────────────────────
def send_appointment_request_email(user_name, user_phone, preferred_datetime_str, service_reason_str):
    """Sends an email with the collected appointment request details."""
    sender_email = os.getenv('APPOINTMENT_EMAIL_SENDER')
    sender_password = os.getenv('APPOINTMENT_EMAIL_PASSWORD')
    receiver_email = 'mohomer12@gmail.com'
    smtp_server = os.getenv('APPOINTMENT_SMTP_SERVER', 'smtp.gmail.com')
    smtp_port = int(os.getenv('APPOINTMENT_SMTP_PORT', 587))

    if not all([sender_email, sender_password]):
        logging.error("Sender email or password not configured for APPOINTMENT_EMAIL. Cannot send appointment request email.")
        return False

    subject = f"New Appointment Request via Layla Bot: {user_name}"
    body = (
        f"A new appointment request has been received from Layla (the virtual assistant):\n\n"
        f"Name: {user_name}\n"
        f"Phone (WhatsApp ID): {user_phone}\n"
        f"Preferred Date/Time: {preferred_datetime_str}\n"
        f"Service/Reason: {service_reason_str}\n\n"
        f"Please follow up with the client to confirm their appointment."
    )

    msg = MIMEText(body, _charset='utf-8') # Ensure UTF-8 for non-English characters
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = receiver_email

    try:
        logging.info(f"Attempting to send appointment request email from {sender_email} to {receiver_email} via {smtp_server}:{smtp_port}")
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
        logging.info("Appointment request email sent successfully.")
        return True
    except smtplib.SMTPDataError as e_data:
        logging.error(f"SMTPDataError sending email. This might be related to email content/formatting. Subject: '{subject}'. Body: '{body[:200]}...' Error: {e_data}", exc_info=True)
        return False
    except smtplib.SMTPServerDisconnected as e_disconnect:
        logging.error(f"SMTPServerDisconnected sending email. This could be a network or server issue. Error: {e_disconnect}", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"Generic error sending appointment request email. Subject: '{subject}'. Error: {e}", exc_info=True)
        return False

# Add this function in script.py

def send_property_lead_email(lead_data):
    """Sends an email with the collected property-for-sale lead details."""
    sender_email = os.getenv('LEAD_EMAIL_SENDER')
    sender_password = os.getenv('LEAD_EMAIL_PASSWORD')
    receiver_email = os.getenv('LEAD_EMAIL_RECEIVER')

    if not all([sender_email, sender_password, receiver_email]):
        logging.error("Email credentials/receiver not configured for property leads.")
        return False

    subject = f"New 'For Sale' Property Lead via WhatsApp: {lead_data.get('name')}"
    body_parts = [
        "A new property lead has been captured by the bot from a seller:\n",
        f"Client Name: {lead_data.get('name')}",
        f"WhatsApp Number: {lead_data.get('phone')}",
        "--- Property Details ---",
        f"Type: {lead_data.get('property_type')}",
        f"City: {lead_data.get('city')}",
        f"Area: {lead_data.get('area')}",
    ]
    if 'building' in lead_data:
        body_parts.append(f"Building Name: {lead_data.get('building')}")

    body_parts.append(f"Asking Price: {lead_data.get('price')} AED")

    body = "\n".join(body_parts)

    msg = MIMEText(body, _charset='utf-8')
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = receiver_email

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
        logging.info("Successfully sent property lead email.")
        return True
    except Exception as e:
        logging.error(f"Error sending property lead email: {e}", exc_info=True)
        return False

# ─── Conversation history storage ─────────────────────────────────────────────
CONV_DIR = 'conversations'
os.makedirs(CONV_DIR, exist_ok=True)

# Define the maximum number of conversation turns to load.
# A "turn" consists of one user message and one assistant message.
# So, MAX_HISTORY_TURNS_TO_LOAD * 2 gives the total number of messages.
# Set this to your desired value (e.g., 10 as in your main script's MAX_HISTORY_TURNS).
MAX_HISTORY_TURNS_TO_LOAD = 6

def load_history(uid):
    path = os.path.join(CONV_DIR, f"{uid}.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f) # Specify UTF-8

        history_messages = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and 'role' in item and 'parts' in item:
                    history_messages.append(item)
                else:
                    # Use logging if it's configured in your main script, otherwise print
                    logging.warning(f"Skipping invalid history item for {uid}: {item}")

            # --- Truncate history after loading ---
            if len(history_messages) > MAX_HISTORY_TURNS_TO_LOAD * 2:
                logging.info(f"Loaded {len(history_messages)} messages ({len(history_messages)//2} turns) for {uid}. Truncating to last {MAX_HISTORY_TURNS_TO_LOAD} turns ({MAX_HISTORY_TURNS_TO_LOAD * 2} messages).")
                history_messages = history_messages[-(MAX_HISTORY_TURNS_TO_LOAD * 2):]
            # --- End of truncation ---

            return history_messages
        return []
    except Exception as e:
        logging.error(f"Error loading history for {uid}: {e}")
        return []

def save_history(uid, history):
    # This function expects `history` to be a list of dictionaries.
    # The truncation for what's *saved* to the file should ideally happen
    # in your main script (webhook function) *before* calling save_history,
    # after appending the latest user and assistant messages.
    # Example from your webhook:
    # MAX_HISTORY_TURNS = 10
    # if len(history) > MAX_HISTORY_TURNS * 2:
    # history = history[-(MAX_HISTORY_TURNS * 2):]
    # save_history(user_id, history)

    path = os.path.join(CONV_DIR, f"{uid}.json")
    try:
        # Ensure all items in history are serializable dicts as expected.
        # Your main script already appends new messages as dicts:
        # new_history_user = {'role': 'user', 'parts': [body]}
        # new_history_model = {'role': 'model', 'parts': [final_model_response_for_history]}
        # The check for Langchain types below is for robustness if objects were added directly.
        serializable_history = []
        for msg in history:
            # Check if msg is a Langchain message object (requires imports)
            # For simplicity, this example assumes your main script always prepares dicts.
            # If HumanMessage, AIMessage, etc., could be in `history`, ensure they are imported
            # and uncomment/adapt the isinstance check:
            # if isinstance(msg, (HumanMessage, AIMessage, SystemMessage)):
            #     serializable_history.append({'role': msg.type, 'parts': [msg.content]})
            if isinstance(msg, dict) and 'role' in msg and 'parts' in msg:
                serializable_history.append(msg)
            else:
                logging.warning(f"Skipping non-serializable or malformed history item for {uid} during save: {type(msg)}")
                # If you expect Langchain objects, you might convert them here or ensure they are pre-converted.

        with open(path, 'w', encoding='utf-8') as f:
            json.dump(serializable_history, f, indent=2, ensure_ascii=False) # Specify UTF-8 and ensure_ascii=False
    except Exception as e:
        logging.error(f"Error saving history for {uid}: {e}")

# ─── Function to Extract Appointment Details for Email using LLM ──────────────
def extract_appointment_details_for_email(conversation_history_str):
    """
    Uses the LLM to extract appointment details (name, preferred time, reason)
    from a snippet of conversation history, which might be in any language.
    """
    if not AI_MODEL:
        logging.error("AI_MODEL not initialized in extract_appointment_details_for_email")
        return None

    extraction_prompt = (
        f"Given the following conversation snippet:\n"
        f"--- --- --- --- --- --- --- --- --- --- --- \n"
        f"{conversation_history_str}\n"
        f"--- --- --- --- --- --- --- --- --- --- --- \n"
        f"The last message from the Assistant confirms that details were collected and a human will follow up.\n"
        f"The conversation snippet may be in any language (e.g., English, Arabic). Extract the information into the JSON format below.\n"
        f"The JSON *keys* (i.e., \"name\", \"preferred_datetime\", \"service_reason\") MUST be in English as specified.\n"
        f"The JSON *values* (e.g., the actual name, the service description) should be the extracted text, even if it's in the original language of the conversation (e.g., Arabic).\n"
        f"The extracted text for 'name', 'preferred_datetime', and 'service_reason' values MUST be plain text only. Ensure that these values are clean and do not contain any markdown (e.g., asterisks, underscores), HTML, or other special formatting characters. Preserve all characters of the original language (e.g., Arabic, Farsi characters) correctly as simple text. These values will be used directly in an email, so they must be free of any formatting artifacts.\n"
        f"Extract the user's full name, their stated preferred date and time, and the service/reason for the appointment.\n"
        f"Respond with ONLY a JSON object in this exact format:\n"
        f"{{ \n"
        f"    \"name\": \"extracted full name\" or null,\n"
        f"    \"preferred_datetime\": \"extracted preferred date and time as a string\" or null,\n"
        f"    \"service_reason\": \"extracted service or reason for appointment\" or null\n"
        f"}} \n"
        f"If any detail is missing or unclear from the user's input in the snippet, use null for its value.\n"
        f"Focus on what the user explicitly stated for their appointment request. Look through the entire snippet for the details."
    )
    try:
        response = AI_MODEL.invoke([HumanMessage(content=extraction_prompt)])
        response_text = response.content.strip()

        # Clean potential markdown code block fences
        if response_text.startswith('```json'):
            response_text = response_text[len('```json'):].strip()
        if response_text.endswith('```'):
            response_text = response_text[:-len('```')].strip()

        logging.info(f"LLM extracted for email: {response_text}")
        details = json.loads(response_text)
        return details
    except Exception as e:
        logging.error(f"Error extracting appointment details for email with AI: {e}", exc_info=True)
        return None

# ─── Generate response from LLM with RAG and Scheduling ─────────────────────
def get_llm_response(text, sender_id, history_dicts=None, retries=3):
    if not AI_MODEL:
        return {'type': 'text', 'content': "AI Model not configured."}

    # --- Step 1: Intent and Filter Extraction ---
    analysis_prompt = f"""
    Analyze the user's request: '{text}'
    Determine if this is a query for properties with specific filters (price, location, bedrooms, type, etc.) or a general question.

    Respond with a JSON object with two keys: "intent" and "filters".
    - "intent" can be "property_search" or "general_question".
    - "filters" should be a dictionary of criteria if it's a property search, otherwise null.

    Supported filter keys are: `Price_AED`, `Bedrooms`, `emirate`, `city`, `area`, `developer`, `Title`.
    For numeric keys (`Price_AED`, `Bedrooms`), the operator can be '<', '>', or '='.

    Example 1: "show me properties below 1 million aed in dubai"
    {{
      "intent": "property_search",
      "filters": {{
        "Price_AED": {{ "operator": "<", "value": 1000000 }},
        "city": {{ "operator": "=", "value": "dubai" }}
      }}
    }}

    Example 2: "do you have 3 bedroom villas"
    {{
      "intent": "property_search",
      "filters": {{
        "Bedrooms": {{ "operator": "=", "value": 3 }},
        "Title": {{ "operator": "=", "value": "villa" }}
      }}
    }}

    Example 3: "what is your commission?"
    {{
      "intent": "general_question",
      "filters": null
    }}

    Respond with ONLY the JSON object.
    """

    try:
        analysis_response = AI_MODEL.invoke([HumanMessage(content=analysis_prompt)])
        response_text = analysis_response.content.strip()
        if response_text.startswith('```json'):
            response_text = response_text[len('```json'):].strip()
        if response_text.endswith('```'):
            response_text = response_text[:-len('```')].strip()

        analysis_json = json.loads(response_text)
        intent = analysis_json.get("intent")
        filters = analysis_json.get("filters")
        logging.info(f"Query analysis complete. Intent: '{intent}', Filters: {filters}")
    except Exception as e:
        logging.error(f"Failed to analyze user query with LLM: {e}. Defaulting to general question.")
        intent = "general_question"
        filters = None

    # --- Step 2: REVISED Logic Execution Based on Intent and Keywords ---
    context_str = ""
    # The main condition is now broader to catch all property-related queries.
    if (intent == "property_search" or is_property_related_query(text)) and PROPERTY_SHEET_ID:

        logging.info("Performing property database search (Google Sheets).")
        all_properties_df = property_handler.get_sheet_data()

        if not all_properties_df.empty:
            # If specific filters were found by the AI, use them.
            if filters:
                logging.info(f"Applying specific filters: {filters}")
                filtered_df = property_handler.filter_properties(all_properties_df, filters)
            # Otherwise, use the whole dataframe for a general listing.
            else:
                logging.info("No specific filters found; preparing a general list of properties.")
                filtered_df = all_properties_df

            if not filtered_df.empty:
                context_str = "Relevant Information Found:\n"
                # Show top 5 results, whether from a filtered search or a general list.
                for _, prop in filtered_df.head(5).iterrows():
                    prop_details = (
                        f"Title: {prop.get('Title', 'N/A')}\n"
                        f"Location: {prop.get('area', '')}, {prop.get('city', '')}, {prop.get('emirate', '')}\n"
                        f"Price: {prop.get('Price_AED', 'N/A')} AED\n"
                        f"Bedrooms: {prop.get('Bedrooms', 'N/A')}\n"
                        f"Description: {prop.get('Description', 'No description available.')}\n"
                    )
                    context_str += prop_details
                    # Add images if available
                    for img_col in ['img1', 'img2', 'img3']:
                        img_url = prop.get(img_col)
                        if img_url and isinstance(img_url, str) and img_url.startswith('http'):
                            context_str += f"[ACTION_SEND_IMAGE_VIA_URL]\n{img_url}\n{prop.get('Title', 'Property Image')}\n"
                    context_str += "---\n"
            else:
                context_str = "Relevant Information Found:\nNo properties found matching your criteria. Please try different keywords or filters."
        else:
             context_str = "Relevant Information Found:\nI was unable to access the property listings at this moment. Please try again shortly."

    else:
        # This 'else' block now correctly handles only TRULY general, non-property questions.
        logging.info("Performing general RAG query using vector store for a non-property question.")
        vector_store = current_app.config.get('VECTOR_STORE') or vector_store_rag
        if vector_store:
            retrieved_docs = query_vector_store(text, vector_store, k=5)
            if retrieved_docs:
                processed_docs_content = [re.sub(r'\*+\s*(.*?)\s*\*+', r'\1', doc.page_content) for doc in retrieved_docs]
                context_str = "\n\nRelevant Information Found:\n" + "\n".join(processed_docs_content)
            else:
                logging.info("No relevant context found in vector store for the general query.")
        else:
            logging.warning("Vector store not available for general query.")

    # --- Step 3: Generate Final Response Based on Context ---
    final_prompt_to_llm = context_str + f"\n\nUser Question: {text}" if context_str else text

    # Dynamically construct system_prompt_content based on language
    current_language = user_languages.get(sender_id, 'ar') # Default to 'ar' if not found

    if current_language == 'ar':
        effective_persona_name = "مساعد"
        system_prompt_content = (
            f"أنت {effective_persona_name}، مساعد ودود ومتعاون من شركة عوجا لإدارة الأملاك. " +
            BASE_PROMPT_TEMPLATE +
            f"\nعند بدء محادثة جديدة (إذا لم تستخدم تحية تفاعلية)، يمكنك أن تقول: 'مرحباً، معك {effective_persona_name} من شركة عوجا لإدارة الأملاك. كيف يمكنني توجيه استفسارك اليوم؟'"
        )
    else: # Default to English
        effective_persona_name = "Mosaed"
        system_prompt_content = (
            f"You are {effective_persona_name}, a helpful and friendly assistant from Al-Ouja Property Management. " +
            BASE_PROMPT_TEMPLATE +
            f"\nWhen starting a new conversation (if not using an interactive greeting), you might say: 'Hello, this is {effective_persona_name} from Al-Ouja Property Management. How can I direct your inquiry today?'"
        )

    messages = [SystemMessage(content=system_prompt_content)]
    if history_dicts:
        for item in history_dicts:
            role = item.get('role')
            parts = item.get('parts')
            if role and parts and isinstance(parts, list) and parts:
                content = parts[0]
                if role == 'user': messages.append(HumanMessage(content=content))
                elif role in ['model', 'assistant']: messages.append(AIMessage(content=content))
    messages.append(HumanMessage(content=final_prompt_to_llm))

    for attempt in range(retries):
        try:
            logging.info(f"Sending to LLM for final response generation (Attempt {attempt+1})")
            resp = AI_MODEL.invoke(messages)
            raw_llm_output = resp.content.strip()

            if "[ACTION_SEND_IMAGE_VIA_URL]" in raw_llm_output:
                image_parts = []
                for line in raw_llm_output.splitlines():
                    if line.strip() == "[ACTION_SEND_IMAGE_VIA_URL]" or (image_parts and len(image_parts) < 3):
                        image_parts.append(line)
                if len(image_parts) >= 3:
                    image_url = image_parts[1].strip()
                    image_caption = image_parts[2].strip()
                    if image_url.startswith('http'):
                        return {'type': 'image', 'url': image_url, 'caption': image_caption}

            response_text_for_display = re.sub(r'\[ACTION_SEND_IMAGE_VIA_URL\]\n.*\n.*', '', raw_llm_output).strip()
            response_text_for_display = response_text_for_display.replace("[ACTION_NOTIFY_UNANSWERED_QUERY]", "").replace("[ACTION_SEND_EMAIL_CONFIRMATION]", "").strip()

            if response_text_for_display:
                return {'type': 'text', 'content': response_text_for_display}

            logging.warning(f"LLM returned an empty or token-only response on attempt {attempt+1}. Raw output: {raw_llm_output}")

        except Exception as e:
            logging.warning(f"LLM API error on attempt {attempt+1}/{retries}: {e}")
            if attempt + 1 == retries:
                logging.error("All LLM attempts failed.", exc_info=True)
                return {'type': 'text', 'content': "I am having trouble processing your request at the moment. Please try again shortly."}
            time.sleep((2 ** attempt) + random.uniform(0.1, 0.5))

    return {'type': 'text', 'content': "I could not generate a response after multiple attempts."}

# === APPOINTMENT SCHEDULING HANDLER (handle_appointment_scheduling) ===
# WhatsApp sending functions (send_whatsapp_message, send_whatsapp_image_message)
# have been moved to whatsapp_utils.py
# Ensure that all calls to these functions throughout script.py
# will correctly use the imported versions.

def handle_appointment_scheduling(message):
    """Handle appointment scheduling requests. Interprets user input as Dubai time,
    stores events in New York time, and confirms to user in Dubai time."""

    if not CALENDAR_SERVICE:
        return "Sorry, appointment scheduling is currently unavailable. Please contact us directly to book your appointment."

    datetime_info = extract_datetime_with_ai(message)

    if not datetime_info or not datetime_info.get('has_datetime'):
        return (
            "I'd be happy to help you schedule an appointment! \n\n"
            "Could you please provide more details? For example:\n"
            "• What date would you prefer?\n"
            "• What time works best for you?\n"
            "• What type of service do you need?"
        )

    try:
        date_str = datetime_info.get('date')
        time_str = datetime_info.get('time')
        duration = int(datetime_info.get('duration_minutes', 60))
        service_type = datetime_info.get('service_type', 'General Consultation')

        if not date_str:
            return "I understand you want to schedule an appointment, but I need a specific date. Could you please tell me which date you prefer?"

        appointment_date_dubai_naive = datetime.strptime(date_str, '%Y-%m-%d').date()

        if not time_str:
            return (
                f"Great! I can help you schedule a {service_type} on {appointment_date_dubai_naive.strftime('%A, %B %d, %Y')} ({TARGET_DISPLAY_TIMEZONE.zone} time) \n\n"
                "What time would work best for you? For example:\n"
                "• 9:00 AM\n• 2:00 PM\n• 4:30 PM"
            )

        appointment_time_dubai_naive = datetime.strptime(time_str, '%H:%M').time()

        intended_start_dt_dubai_naive = datetime.combine(appointment_date_dubai_naive, appointment_time_dubai_naive)
        intended_start_dt_dubai_aware = TARGET_DISPLAY_TIMEZONE.localize(intended_start_dt_dubai_naive)
        intended_end_dt_dubai_aware = intended_start_dt_dubai_aware + timedelta(minutes=duration)

        current_dubai_time = datetime.now(TARGET_DISPLAY_TIMEZONE)
        if intended_start_dt_dubai_aware < current_dubai_time:
            return "I can't schedule appointments in the past. Could you please choose a future date and time?"

        event_start_dt_storage_tz_aware = intended_start_dt_dubai_aware.astimezone(EVENT_STORAGE_TIMEZONE)
        event_end_dt_storage_tz_aware = intended_end_dt_dubai_aware.astimezone(EVENT_STORAGE_TIMEZONE)

        event_start_dt_storage_tz_naive = event_start_dt_storage_tz_aware.replace(tzinfo=None)
        event_end_dt_storage_tz_naive = event_end_dt_storage_tz_aware.replace(tzinfo=None)

        logging.info(f"Intended Dubai time: {intended_start_dt_dubai_aware.strftime('%Y-%m-%d %H:%M %Z')}")
        logging.info(f"Equivalent Storage ({EVENT_STORAGE_TIMEZONE.zone}) time for GCal: {event_start_dt_storage_tz_aware.strftime('%Y-%m-%d %H:%M %Z')}")

        if not check_availability(CALENDAR_SERVICE, event_start_dt_storage_tz_naive, event_end_dt_storage_tz_naive):
            return (
                f"Unfortunately, {intended_start_dt_dubai_aware.strftime('%A, %B %d at %I:%M %p %Z')} is not available. \n\n"
                "Could you please suggest another time? I'd be happy to help you find an alternative slot."
            )

        event_title = f"{service_type} - WhatsApp Booking"
        event_description = (
            f"Appointment scheduled via WhatsApp.\n"
            f"Service: {service_type}\n"
            f"Duration: {duration} minutes\n"
            f"Intended time ({TARGET_DISPLAY_TIMEZONE.zone}): {intended_start_dt_dubai_aware.strftime('%Y-%m-%d %H:%M %Z')}"
        )

        test_attendee_email = None

        created_event_api_response = create_calendar_event(
            CALENDAR_SERVICE,
            event_title,
            event_start_dt_storage_tz_naive,
            event_end_dt_storage_tz_naive,
            event_description,
            attendee_email=test_attendee_email
        )

        if created_event_api_response and created_event_api_response.get('htmlLink'):
            return (
                f"Perfect! Your appointment has been scheduled:\n\n"
                f"Date: {intended_start_dt_dubai_aware.strftime('%A, %B %d, %Y')}\n"
                f"Time: {intended_start_dt_dubai_aware.strftime('%I:%M %p %Z')} ({TARGET_DISPLAY_TIMEZONE.zone})\n"
                f"Duration: {duration} minutes\n"
                f"Service: {service_type}\n\n"
                f"You'll receive a reminder. Looking forward to seeing you!"
            )
        else:
            logging.error(f"Failed to create event or event link missing. API Response: {created_event_api_response}")
            return "I encountered an issue while creating your appointment. Please try again or contact us directly."

    except ValueError as ve:
        logging.error(f"ValueError in appointment scheduling: {ve}", exc_info=True)
        return "There was an issue with the date or time format. Please provide it like 'YYYY-MM-DD' for date and 'HH:MM' for time."
    except Exception as e:
        logging.error(f"Error in appointment scheduling: {e}", exc_info=True)
        return "Sorry, I had trouble processing your appointment request. Could you please try again with a specific date and time?"
# === END OF APPOINTMENT SCHEDULING HANDLER ===

# ─── Split long messages ──────────────────────────────────────────────────────
def split_message(text, max_lines=2, max_chars=1000):
    parts = text.split('\n')
    chunks = []
    current_chunk_lines = []
    current_chunk_char_count = 0
    for line in parts:
        line_len_with_newline = len(line) + (1 if current_chunk_lines else 0)
        if (len(current_chunk_lines) >= max_lines or \
            current_chunk_char_count + line_len_with_newline > max_chars) and current_chunk_lines:
            chunks.append('\n'.join(current_chunk_lines))
            current_chunk_lines = []
            current_chunk_char_count = 0
        current_chunk_lines.append(line)
        current_chunk_char_count += line_len_with_newline
    if current_chunk_lines: chunks.append('\n'.join(current_chunk_lines))
    return chunks

# WhatsApp sending functions (send_whatsapp_message, send_whatsapp_image_message)
# were moved to whatsapp_utils.py and are imported at the top of script.py.
# The global WASENDER_API_URL, WASENDER_API_TOKEN, and HTTP_SESSION
# that were specific to these functions in script.py have also been removed,
# as this configuration is now handled within whatsapp_utils.py.

# ─── Health Check Endpoint ─────────────────────────────────────────────────────
@app.route('/')
def home():
    return "WhatsApp Bot is running!"

# --- Language Detection Function ---
def detect_language(text):
    if not text: # Handle empty string case
        return 'en' # Default to English if text is empty
    # Basic check for Arabic characters
    arabic_chars = re.findall(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', text) # Expanded Arabic unicode range
    # If more than a small threshold of characters are Arabic, assume 'ar'
    # Check if more than 30% of non-space chars are Arabic, or if there are at least 3 arabic chars
    non_space_text_len = len(text.replace(" ", ""))
    if non_space_text_len == 0 and len(arabic_chars) > 0: # e.g. "   سلام   "
        return 'ar'
    if non_space_text_len > 0 and (len(arabic_chars) > 2 or (len(arabic_chars) / non_space_text_len > 0.3)):
        return 'ar'
    return 'en'

def extract_sheet_id_from_url(url_or_id: str) -> str:
    if not url_or_id:
        return None

    # Attempt to extract ID from a URL
    url_match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url_or_id)
    if url_match:
        logging.info(f"Extracted Sheet ID '{url_match.group(1)}' from URL '{url_or_id}'.")
        return url_match.group(1)

    # If no URL match, check if the string itself looks like a valid Sheet ID
    # Google Sheet IDs are typically 44 characters long and use base64url characters (A-Z, a-z, 0-9, -, _)
    if re.fullmatch(r'[a-zA-Z0-9-_]{30,}', url_or_id): # Check for typical ID characters and min length (IDs are usually ~44 chars)
        logging.info(f"Input '{url_or_id}' appears to be a direct Sheet ID.")
        return url_or_id

    # If it's not a recognizable URL and doesn't look like a typical ID
    logging.warning(f"Input '{url_or_id}' could not be parsed as a Google Sheet URL nor does it look like a typical Sheet ID. Proceeding with the original input.")
    # Returning the original input and letting the Sheets API call fail later might give more specific errors.
    # However, for outreach, we might want to be stricter. For now, returning it.
    return url_or_id

@app.route('/webhook-google-sync', methods=['POST'])
def webhook_google_sync():
    """
    Webhook endpoint to receive notifications from Google Apps Scripts
    when a Document or Sheet is modified.
    It queues a background task to process the update.
    """
    try:
        data = request.get_json()
        if not isinstance(data, dict) or 'documentId' not in data or 'secretToken' not in data:
            logging.warning(f"/webhook-google-sync: Invalid payload received: {data}")
            return jsonify(error='Invalid payload. Missing documentId or secretToken.'), 400

        document_id = data.get('documentId')
        received_token = data.get('secretToken')

        expected_token = os.getenv('FLASK_SECRET_TOKEN')

        if not expected_token:
            logging.error("/webhook-google-sync: FLASK_SECRET_TOKEN not configured on the server.")
            # For security reasons, avoid giving too much detail to the client here.
            return jsonify(error='Webhook service not configured correctly.'), 500

        if received_token != expected_token:
            logging.warning(f"/webhook-google-sync: Unauthorized attempt. Received token: '{received_token}' for document: {document_id}")
            return jsonify(error='Unauthorized.'), 403

        # Authentication successful
        logging.info(f"/webhook-google-sync: Authentication successful for document_id: {document_id}. Queuing background task.")

        # Get the current Flask app context to pass to the background thread
        # This allows the background thread to use current_app, logging, etc.
        current_app_context = current_app.app_context()

        # Submit the task to the ThreadPoolExecutor
        executor.submit(process_google_document_update, document_id, current_app_context)

        return jsonify(status='success', message='Document update task queued.'), 202

    except Exception as e:
        logging.exception(f"Error in /webhook-google-sync: {e}")
        return jsonify(error='Internal Server Error'), 500

# START OF NEW handle_new_messages
@app.route('/hook', methods=['POST'])
def handle_new_messages():
    try:
        data = request.json or {}
        incoming_messages = data.get('messages', [])
        if not incoming_messages:
            return jsonify(status='success_no_messages'), 200

        for message in incoming_messages:
            body_for_fallback = None # Initialize body_for_fallback for each message

            if message.get('from_me'):
                continue

            sender = message.get('from') # e.g., "1234567890@c.us" or "group_id@g.us"
            msg_type = message.get('type') # e.g., "text", "image", "reply" (for button/list replies)

            # --- Language Detection and Storage ---
            body_text_if_any = ""
            if msg_type == 'text':
                body_text_if_any = message.get('text', {}).get('body', '')
            elif msg_type == 'reply': # For button/list replies, language is usually based on prior interaction
                # We'll use existing user_languages[sender] or detect from title if needed,
                # but primarily rely on established language.
                # For initial detection, if a button reply is the *first* thing we see,
                # its title could be used, but this is an edge case.
                reply_content = message.get('reply', {})
                button_title = reply_content.get('buttons_reply', {}).get('title')
                list_title = reply_content.get('list_reply', {}).get('title')
                body_text_if_any = button_title or list_title or ""

            if sender not in user_languages or (msg_type == 'text' and body_text_if_any): # Update language if new user or new text message
                detected_lang = detect_language(body_text_if_any)
                user_languages[sender] = detected_lang
                logging.info(f"Language for {sender} set/updated to: {detected_lang} based on text: '{body_text_if_any[:50]}'")

            current_language = user_languages.get(sender, 'ar') # Default to Arabic if somehow not set after detection logic

            # --- START: NEW INTERACTIVE FLOW LOGIC ---
            user_in_interactive_flow = sender in interactive_flow_states

            if user_in_interactive_flow:
                current_step = interactive_flow_states[sender].get('step')
                logging.info(f"User {sender} is in interactive flow, step: {current_step}, lang: {current_language}")

                button_id = None
                selected_row_id = None
                selected_title = None # For list or button replies

                if msg_type == 'reply':
                    reply_content = message.get('reply', {})
                    if reply_content.get('type') == 'buttons_reply':
                        button_id = reply_content['buttons_reply'].get('id')
                        selected_title = reply_content['buttons_reply'].get('title')
                        logging.info(f"Interactive flow: Button reply from {sender}. ID: {button_id}, Title: {selected_title}, Step: {current_step}")
                    elif reply_content.get('type') == 'list_reply':
                        selected_row_id = reply_content['list_reply'].get('id')
                        selected_title = reply_content['list_reply'].get('title')
                        logging.info(f"Interactive flow: List reply from {sender}. ID: {selected_row_id}, Title: {selected_title}, Step: {current_step}")

                body_for_fallback = selected_title # Use button/list title for fallback if not handled by new flow

                if current_step == 'awaiting_initial_choice' and button_id:
                    if button_id == 'button_id1': # Owns apartment
                        send_furnished_query_message(sender, language=current_language)
                        interactive_flow_states[sender]['step'] = 'awaiting_furnished_choice'
                        return jsonify(status='success_interactive_handled'), 200
                    elif button_id == 'button_id2': # Wants to rent
                        send_city_selection_message(sender, language=current_language)
                        interactive_flow_states[sender]['step'] = 'awaiting_city_choice'
                        return jsonify(status='success_interactive_handled'), 200
                    elif button_id == 'button_id3': # Other inquiries
                        # Using current_language for the generic message
                        response_text = "Please type your question, and I'll do my best to help."
                        if current_language == 'ar':
                            response_text = "الرجاء كتابة سؤالك، وسأبذل قصارى جهدي لمساعدتك."
                        send_whatsapp_message(sender, response_text)
                        del interactive_flow_states[sender]
                        # Let it fall through to RAG/LLM by not returning explicitly.
                        # body_for_fallback will be the button title "Other inquiries", which might not be ideal for RAG.
                        # Consider setting body_for_fallback to a more neutral "User asked for other inquiries" or None.
                        # For now, let's clear it so RAG doesn't act on the button title.
                        body_for_fallback = "User selected 'Other inquiries' and will type their question."
                        # This will then be processed by the RAG after this interactive block.
                    else:
                        response_text = "Sorry, I didn't understand that selection. Please try again."
                        if current_language == 'ar':
                            response_text = "عذراً، لم أفهم هذا الاختيار. الرجاء المحاولة مرة أخرى."
                        send_whatsapp_message(sender, response_text)
                        # Optionally, resend initial greeting or just wait for next input
                        # send_initial_greeting_message(sender, language=current_language)
                        return jsonify(status='success_interactive_reprompted_unknown_button'), 200

                elif current_step == 'awaiting_furnished_choice':
                    # This block handles the response to the "Is your apartment furnished?" question.
                    if msg_type == 'reply' and message.get('reply', {}).get('type') == 'buttons_reply':
                        # Ensure button_id and selected_title are fresh for this specific step handling
                        button_id = message['reply']['buttons_reply'].get('id')
                        button_title = message['reply']['buttons_reply'].get('title') # For logging or fallback
                        logging.info(f"Interactive flow: User {sender} at step {current_step} pressed button {button_id} ('{button_title}')")

                        if button_id == 'button_id4': # "Yes, furnished"
                            send_furnished_apartment_survey_message(sender, language=current_language)
                            if sender in interactive_flow_states: # Check before deleting
                                del interactive_flow_states[sender]
                            logging.info(f"Interactive flow for {sender} (furnished branch) concluded by sending survey link.")
                            return jsonify(status='success_interactive_handled_survey_sent'), 200
                        elif button_id == 'button_id5': # "No, unfurnished"
                            send_unfurnished_apartment_survey_message(sender, language=current_language)
                            if sender in interactive_flow_states: # Check before deleting
                                del interactive_flow_states[sender]
                            logging.info(f"Interactive flow for {sender} (unfurnished branch) concluded by sending survey link.")
                            return jsonify(status='success_interactive_handled_survey_sent'), 200
                        else:
                            # This 'else' handles an unexpected button_id for the 'awaiting_furnished_choice' step.
                            logging.warning(f"Interactive flow: User {sender} at step {current_step} pressed an unknown button ID: {button_id}")
                            response_text = "Sorry, I didn't understand that selection. Please choose one of the provided options."
                            if current_language == 'ar':
                                response_text = "عذراً، لم أفهم هذا الاختيار. الرجاء اختيار أحد الخيارات المتاحة."
                            send_whatsapp_message(sender, response_text)
                            # Optionally resend the furnished_query_message to show the correct buttons again
                            send_furnished_query_message(sender, language=current_language)
                            return jsonify(status='success_interactive_reprompted_unknown_option'), 200
                    else: # User sent something other than a button reply at this step (e.g., text)
                        logging.info(f"Interactive flow: User {sender} at step {current_step} sent a non-button reply. Reprompting.")
                        response_text = "Please make a selection using the buttons provided for whether the apartment is furnished or not."
                        if current_language == 'ar':
                            response_text = "الرجاء تحديد اختيارك باستخدام الأزرار المتوفرة لتحديد ما إذا كانت الشقة مؤثثة أم لا."
                        send_whatsapp_message(sender, response_text)
                        # Resend the furnished_query_message to show the buttons again
                        send_furnished_query_message(sender, language=current_language)
                        return jsonify(status='success_interactive_reprompted_text_instead_of_button'), 200

                elif current_step == 'awaiting_city_choice' and selected_row_id:
                    response_text = f"You selected {selected_title}. Our team will contact you about rentals in this city."
                    if current_language == 'ar':
                        response_text = f"لقد اخترت {selected_title}. سيقوم فريقنا بالتواصل معك بخصوص الإيجارات في هذه المدينة."
                    send_whatsapp_message(sender, response_text)
                    del interactive_flow_states[sender]
                    return jsonify(status='success_interactive_handled'), 200

                # Handle text messages during an active interactive flow
                elif msg_type == 'text' and body_text_if_any:
                    # User sent text instead of clicking a button/list
                    response_text = "Please make a selection using the buttons or list provided."
                    if current_language == 'ar':
                        response_text = "الرجاء تحديد اختيارك باستخدام الأزرار أو القائمة المتوفرة."
                    send_whatsapp_message(sender, response_text)
                    # Optionally, resend the last interactive message.
                    # This requires storing the type of the last message or the function to call.
                    # For now, a simple reprompt. Or could exit: del interactive_flow_states[sender]
                    # Example resend (needs more robust state):
                    # if interactive_flow_states[sender].get('last_message_type') == 'initial_greeting':
                    #    send_initial_greeting_message(sender, language=current_language)
                    return jsonify(status='success_interactive_reprompted_text_instead_of_button'), 200

                # If the reply type (button/list) or step wasn't handled above within the interactive flow,
                # it might be an old message or an unexpected interaction.
                # We set body_for_fallback earlier, so it can proceed to RAG if needed.
                # However, if it was a button/list reply meant for the interactive flow but wasn't handled,
                # it's better to prompt again or exit the flow.
                elif msg_type == 'reply' and (button_id or selected_row_id): # Unhandled button/list reply in flow
                    logging.warning(f"User {sender} sent unhandled reply in step {current_step}. ButtonID: {button_id}, ListID: {selected_row_id}")
                    response_text = "Sorry, I encountered an issue with that selection. Let's try starting over."
                    if current_language == 'ar':
                       response_text = "عذراً، واجهت مشكلة مع هذا الاختيار. دعنا نحاول البدء من جديد."
                    send_whatsapp_message(sender, response_text)
                    send_initial_greeting_message(sender, language=current_language)
                    interactive_flow_states[sender] = {'step': 'awaiting_initial_choice', 'language': current_language}
                    return jsonify(status='success_interactive_reset'), 200


            # --- START: EXISTING "SELL PROPERTY" FLOW LOGIC ---
            # This existing flow should only trigger if not in the new interactive_flow
            if not user_in_interactive_flow and sender in sell_flow_states:
                state_info = sell_flow_states[sender]
                current_state = state_info.get('state')
                user_data = state_info.get('data', {})

                user_reply_text = ""
                if msg_type == 'text':
                    user_reply_text = message.get('text', {}).get('body', '').strip()
                elif msg_type == 'reply' and message.get('reply', {}).get('type') in ['list_reply', 'buttons_reply']:
                    reply_data = message['reply'].get('list_reply') or message['reply'].get('buttons_reply')
                    user_reply_text = reply_data.get('title')

                if not user_reply_text:
                    logging.info(f"Sell flow: Empty reply from {sender}, state {current_state}. Waiting for valid input.")
                    continue

                if current_state == 'awaiting_seller_name':
                    user_data['name'] = user_reply_text
                    sell_flow_states[sender] = {'state': 'awaiting_seller_property_type', 'data': user_data}
                    type_list_data = {
                        "header": "Choosing property type:",
                        "body": "Please tell us what type of property do you want to sell?",
                        "footer": "Select one from the list below:",
                        "label": "Press here to select property type",
                        "sections": [{"title": "Type of Property", "rows": [
                            {"id": "type_villa", "title": "Villa"}, {"id": "type_apartment", "title": "Apartment"},
                            {"id": "type_penthouse", "title": "Penthouse"}, {"id": "type_townhouse", "title": "Townhouse"},
                            {"id": "type_other", "title": "Other"}
                        ]}]
                    }
                    send_interactive_list_message(sender, type_list_data)

                elif current_state == 'awaiting_seller_property_type':
                    user_data['property_type'] = user_reply_text
                    sell_flow_states[sender] = {'state': 'awaiting_seller_city', 'data': user_data}
                    city_rows = [{"id": f"city_{city.lower().replace(' ', '')}", "title": city} for city in UAE_CITIES]
                    city_list_data = {
                        "header": "Choosing City:",
                        "body": "In which city is your property located?",
                        "footer": "Please select a city from the list.",
                        "label": "Select City",
                        "sections": [{"title": "Cities in UAE", "rows": city_rows}]
                    }
                    send_interactive_list_message(sender, city_list_data)

                elif current_state == 'awaiting_seller_city':
                    user_data['city'] = user_reply_text
                    sell_flow_states[sender] = {'state': 'awaiting_seller_area', 'data': user_data}
                    areas_for_city = UAE_AREAS.get(user_reply_text, [])
                    if not areas_for_city:
                        areas_for_city = ["Other"]

                    area_rows = [{"id": f"area_{area.lower().replace(' ', '_')}", "title": area} for area in areas_for_city]
                    if not any(r['title'] == 'Other' for r in area_rows) and user_reply_text in UAE_AREAS :
                         if "Other" not in areas_for_city:
                            area_rows.append({"id": "area_other", "title": "Other"})

                    area_list_data = {
                        "header": f"Choosing Area in {user_reply_text}:",
                        "body": "Please select the area.",
                        "label": "Select Area",
                        "sections": [{"title": f"Areas in {user_reply_text}", "rows": area_rows}]
                    }
                    send_interactive_list_message(sender, area_list_data)

                elif current_state == 'awaiting_seller_area':
                    user_data['area'] = user_reply_text
                    if user_data.get('property_type', '').lower() == 'apartment':
                        sell_flow_states[sender] = {'state': 'awaiting_seller_building_name', 'data': user_data}
                        send_whatsapp_message(sender, "Understood. What is the name of the building?")
                    else:
                        sell_flow_states[sender] = {'state': 'awaiting_seller_price', 'data': user_data}
                        send_whatsapp_message(sender, "Great! What is your asking price in AED?")

                elif current_state == 'awaiting_seller_building_name':
                    user_data['building'] = user_reply_text
                    sell_flow_states[sender] = {'state': 'awaiting_seller_price', 'data': user_data}
                    send_whatsapp_message(sender, "Great! And what is your asking price in AED?")

                elif current_state == 'awaiting_seller_price':
                    user_data['price'] = user_reply_text
                    user_data['phone'] = sender.split('@')[0]

                    send_whatsapp_message(sender, "Thank you for all the details. Our team will review the information and get in touch with you shortly!")

                    email_sent_successfully = send_property_lead_email(user_data)
                    if email_sent_successfully:
                        logging.info(f"Property lead email for {user_data.get('name')} sent successfully.")
                    else:
                        logging.error(f"Failed to send property lead email for {user_data.get('name')}.")

                    del sell_flow_states[sender]
                continue

            # --- IF NOT IN ANY FLOW, CHECK FOR GREETINGS TO START INTERACTIVE FLOW ---
            # Or, if it fell through the interactive flow (e.g. user selected "Other inquiries")

            # Determine body_for_fallback if not already set by interactive flow logic
            # This is crucial for the RAG/LLM part
            if body_for_fallback is None: # Was not set by the interactive flow logic
                if msg_type == 'text':
                    body_for_fallback = message.get('text', {}).get('body', '').strip()
                elif msg_type == 'reply': # An unhandled reply type or one that fell through
                    reply_content = message.get('reply', {})
                    button_title = reply_content.get('buttons_reply', {}).get('title')
                    list_title = reply_content.get('list_reply', {}).get('title')
                    body_for_fallback = button_title or list_title or "" # Use title if available
                elif msg_type == 'image' or msg_type == 'video':
                    body_for_fallback = f"[User sent a {msg_type}]"
                    if message.get('media', {}).get('caption'):
                        body_for_fallback += f" with caption: {message['media']['caption']}"
                elif msg_type == 'audio':
                    # (Keep existing audio transcription logic here)
                    media_url = message.get('media', {}).get('url')
                    if media_url and openai_client:
                        try:
                            audio_response = requests.get(media_url)
                            audio_response.raise_for_status()
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_audio_file:
                                tmp_audio_file.write(audio_response.content)
                                tmp_audio_file_path = tmp_audio_file.name

                            transcript = openai_client.audio.transcriptions.create(
                                model="whisper-1", file=open(tmp_audio_file_path, "rb")
                            )
                            body_for_fallback = transcript.text
                            os.remove(tmp_audio_file_path)
                        except Exception as e:
                            logging.error(f"Error during audio transcription: {e}")
                            body_for_fallback = "[Audio transcription failed.]"
                    else:
                        body_for_fallback = "[Audio received, but could not be transcribed.]"
                # else: body_for_fallback remains None or its previously set value

            if not user_in_interactive_flow and not (sender in sell_flow_states) and body_for_fallback:
                # Using body_text_if_any for greeting check, as it's cleaner (already extracted text part)
                # If body_text_if_any is empty (e.g. image message), it won't be a greeting.
                current_text_for_greeting_check = body_text_if_any.strip().lower()
                greetings = ["hi", "hello", "hey", "greetings", "good morning", "good afternoon", "good evening", "مرحبا", "السلام عليكم", "هلا", "هاي"]
                # Check for exact match or if the text *starts with* a greeting (for greetings longer than 2 chars)
                is_greeting = any(greet == current_text_for_greeting_check for greet in greetings) or \
                              any(current_text_for_greeting_check.startswith(greet) for greet in greetings if len(greet) > 2)

                if is_greeting:
                    logging.info(f"User {sender} sent a greeting: '{current_text_for_greeting_check}'. Starting interactive flow in {current_language}.")
                    send_initial_greeting_message(sender, language=current_language)
                    interactive_flow_states[sender] = {'step': 'awaiting_initial_choice', 'language': current_language}
                    return jsonify(status='success_interactive_started'), 200

            # --- FALLBACK to existing command/RAG logic ---
            # Ensure body_for_fallback is set for RAG if message wasn't handled by interactive flow and didn't start one.
            # The RAG logic should only run if the message was not fully processed by an interactive flow.

            if not (sender and body_for_fallback): # Check if sender and body_for_fallback are valid
                # This check might be redundant if the message was already handled and returned,
                # but it's a safeguard.
                if msg_type == 'reply' and message.get('reply', {}).get('type') == 'list_reply' and not (sender in sell_flow_states or user_in_interactive_flow) :
                     logging.warning(f"Received list_reply from {sender} outside of any active flow. Ignoring.")
                elif not body_for_fallback: # If body_for_fallback is still empty or None
                     logging.warning(f"Webhook ignored: no sender or body_for_fallback. Message Type: {msg_type}, Message: {message}")
                continue # Skip to next message in loop if this one is not processable

            # The rest of the original /hook logic (global pause, RAG, etc.) follows here.
            # It will use 'body_for_fallback' as the user's message content.
            # Make sure the existing 'if button_id and button_id.endswith('button_1_id'):'
            # for the old sell_flow is correctly placed or adapted if it needs to be outside
            # the new interactive flow logic entirely. The current placement of "SELL PROPERTY" flow
            # check (if not user_in_interactive_flow and sender in sell_flow_states) is correct.

            # The original button check for "sell_flow_states" was:
            # if msg_type == 'reply' and message.get('reply', {}).get('type') == 'buttons_reply':
            #    ...
            #    if button_id and button_id.endswith('button_1_id'): # THIS IS FOR THE OLD FLOW
            #        sell_flow_states[sender] = {'state': 'awaiting_seller_name', 'data': {}}
            #        send_whatsapp_message(sender, "Great! We can certainly help with that. To start, could you please tell me your full name?")
            #        continue
            # This specific button ('button_1_id') needs to be differentiated from the new interactive flow buttons.
            # For now, assuming 'button_1_id' is exclusively for the old sell flow and won't clash.
            # If there's a general button handler, it needs to be careful.
            # The current structure places the new interactive flow first. If it handles a message, it returns.
            # If not, it falls through. Then the old sell_flow is checked. If that handles, it continues or returns.
            # If neither, then the RAG logic gets body_for_fallback.

            # Ensure the old sell flow button logic is handled correctly if it's not part of the new flow.
            # The current structure:
            # 1. New Interactive flow (if active)
            # 2. Old Sell flow (if active and not in new interactive flow)
            # 3. Greeting check (if not in any flow)
            # 4. RAG/LLM (if body_for_fallback is set and not handled above)

            # The original code for getting body_for_fallback from buttons for the RAG part:
            if msg_type == 'reply' and message.get('reply', {}).get('type') == 'buttons_reply' and not user_in_interactive_flow and not (sender in sell_flow_states):
                # This is if a button reply was NOT handled by interactive flow and NOT by sell_flow
                button_reply_data = message['reply']['buttons_reply']
                button_id = button_reply_data.get('id') # Could be from an old message or unhandled
                button_title = button_reply_data.get('title')
                # The original sell_flow button check:
                if button_id and button_id.endswith('button_1_id'): # This is part of the old "sell property" initiation
                    sell_flow_states[sender] = {'state': 'awaiting_seller_name', 'data': {}}
                    send_whatsapp_message(sender, "Great! We can certainly help with that. To start, could you please tell me your full name?")
                    continue # Handled by starting the old sell_flow
                # If it's another button not handled by any flow, its title goes to RAG
                body_for_fallback = button_title
                logging.info(f"User {sender} clicked unhandled button: ID='{button_id}', Title='{button_title}'. Passing title to RAG.")


            # Ensure audio transcription is only done if body_for_fallback is not already set
            # The current logic sets body_for_fallback from various sources. If it's still None, then try audio.
            # This seems fine.

            if msg_type == 'audio' and not body_for_fallback: # Only transcribe if not already processed
                media_url = message.get('media', {}).get('url')
                if media_url and openai_client:
                    try:
                        audio_response = requests.get(media_url)
                        audio_response.raise_for_status()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_audio_file:
                            tmp_audio_file.write(audio_response.content)
                            tmp_audio_file_path = tmp_audio_file.name

                        transcript = openai_client.audio.transcriptions.create(
                            model="whisper-1", file=open(tmp_audio_file_path, "rb")
                        )
                        body_for_fallback = transcript.text
                        os.remove(tmp_audio_file_path)
                    except Exception as e:
                        logging.error(f"Error during audio transcription: {e}")
                        body_for_fallback = "[Audio transcription failed.]"
                else:
                    body_for_fallback = "[Audio received, but could not be transcribed.]"

            if not (sender and body_for_fallback):
                if msg_type == 'reply' and message.get('reply', {}).get('type') == 'list_reply':
                     logging.warning(f"Received list_reply from {sender} outside of active sell flow. Ignoring.")
                elif not body_for_fallback:
                     logging.warning(f"Webhook ignored: no sender or body_for_fallback. Message Type: {msg_type}, Message: {message}")
                continue

            # --- FALLBACK to existing command/RAG logic ---
            normalized_body = body_for_fallback.lower().strip()
            global is_globally_paused

            if normalized_body == "bot pause all":
                is_globally_paused = True
                send_whatsapp_message(sender, "Bot is now globally paused.")
                logging.info(f"Bot globally paused by {sender}.")
                continue

            if normalized_body == "bot resume all":
                is_globally_paused = False
                paused_conversations.clear()
                send_whatsapp_message(sender, "Bot is now globally resumed. All specific conversation pauses have been cleared.")
                logging.info(f"Bot globally resumed by {sender}. Specific pauses cleared.")
                continue

            if normalized_body.startswith("bot pause "):
                parts = normalized_body.split("bot pause ", 1)
                if len(parts) > 1 and parts[1].strip():
                    target_user_id = parts[1].strip()
                    paused_conversations.add(target_user_id)
                    send_whatsapp_message(sender, f"Bot interactions will be paused for: {target_user_id}")
                    logging.info(f"Bot interactions paused for {target_user_id} by {sender}.")
                else:
                    send_whatsapp_message(sender, "Invalid command format. Use: bot pause <target_user_id>")
                    logging.info(f"Invalid 'bot pause' command from {sender}: {normalized_body}")
                continue

            if normalized_body.startswith("bot resume "):
                parts = normalized_body.split("bot resume ", 1)
                if len(parts) > 1 and parts[1].strip():
                    target_user_id = parts[1].strip()
                    paused_conversations.discard(target_user_id)
                    send_whatsapp_message(sender, f"Bot interactions will be resumed for: {target_user_id}")
                    logging.info(f"Bot interactions resumed for {target_user_id} by {sender}.")
                else:
                    send_whatsapp_message(sender, "Invalid command format. Use: bot resume <target_user_id>")
                    logging.info(f"Invalid 'bot resume' command from {sender}: {normalized_body}")
                continue

            if normalized_body.startswith("bot start outreach"):
                parts = normalized_body.split("bot start outreach", 1)
                sheet_specifier = parts[1].strip() if len(parts) > 1 and parts[1].strip() else os.getenv('DEFAULT_OUTREACH_SHEET_ID')

                if not sheet_specifier:
                    send_whatsapp_message(sender, "Error: No Google Sheet ID was provided or found in the default environment variable.")
                    logging.warning(f"Outreach command by {sender} failed: no sheet specifier.")
                    continue

                parsed_sheet_id = extract_sheet_id_from_url(sheet_specifier)
                if not parsed_sheet_id:
                    send_whatsapp_message(sender, f"Error: The provided Google Sheet specifier '{sheet_specifier}' is invalid.")
                    continue

                send_whatsapp_message(sender, f"Outreach campaign started using Sheet ID: {parsed_sheet_id}. You will be notified upon completion.")
                executor.submit(process_outreach_campaign, parsed_sheet_id, sender, current_app.app_context())
                logging.info(f"Outreach campaign submitted to executor for {sender} with Sheet ID: {parsed_sheet_id}.")
                continue

            if is_globally_paused:
                logging.info(f"Bot is globally paused. Ignoring message from {sender}: {body_for_fallback[:100]}...")
                continue

            if sender in paused_conversations:
                logging.info(f"Conversation with {sender} is paused. Ignoring message: {body_for_fallback[:100]}...")
                continue

            user_id = ''.join(c for c in sender if c.isalnum())
            logging.info(f"Incoming from {sender} (UID: {user_id}): {body_for_fallback}")

            history = load_history(user_id)
            llm_response_data = get_llm_response(body_for_fallback, sender, history)

            final_model_response_for_history = ""

            if llm_response_data['type'] == 'image':
                image_url = llm_response_data['url']
                caption = llm_response_data['caption']
                logging.info(f"Attempting to send image to {sender}. URL: {image_url}, Caption: {caption}")
                success = send_whatsapp_image_message(sender, caption, image_url)
                if success:
                    final_model_response_for_history = f"[Sent Image: {image_url} with caption: {caption}]"
                    logging.info(f"Successfully sent image to {sender}.")
                else:
                    final_model_response_for_history = f"[Failed to send Image: {image_url} with caption: {caption}]"
                    logging.error(f"Failed to send image to {sender}. URL: {image_url}")
                    fallback_message = "I tried to send you an image, but it seems there was a problem. Please try again later or ask me something else!"
                    send_whatsapp_message(sender, fallback_message)
            elif llm_response_data['type'] == 'text':
                text_content = llm_response_data['content']
                final_model_response_for_history = text_content
                chunks = split_message(text_content)
                for idx, chunk in enumerate(chunks, start=1):
                    if not send_whatsapp_message(sender, chunk):
                        logging.error(f"Failed to send chunk {idx}/{len(chunks)} to {sender}. Aborting further sends for this message.")
                        break
                    if idx < len(chunks):
                        time.sleep(random.uniform(2.0, 3.0))
            else:
                logging.error(f"Unknown response type from get_llm_response: {llm_response_data.get('type')}")
                final_model_response_for_history = "[Error: Unknown response type from LLM]"
                error_message = "I'm having a bit of trouble processing that request. Could you try rephrasing?"
                send_whatsapp_message(sender, error_message)

            new_history_user = {'role': 'user', 'parts': [body_for_fallback]}
            new_history_model = {'role': 'model', 'parts': [final_model_response_for_history]}
            history.append(new_history_user)
            history.append(new_history_model)

            MAX_HISTORY_TURNS = 10
            if len(history) > MAX_HISTORY_TURNS * 2:
                history = history[-(MAX_HISTORY_TURNS * 2):]

            save_history(user_id, history)

        return jsonify(status='success'), 200

    except json.JSONDecodeError as je:
        logging.error(f"Webhook JSONDecodeError: {je}. Raw data: {request.data}")
        return jsonify(status='error', message='Invalid JSON payload'), 400
    except Exception as e:
        logging.exception(f"FATAL Error in webhook processing: {e}")
        return jsonify(status='error', message='Internal Server Error'), 500
# END OF NEW handle_new_messages

# ─── Background Task Function for Google Document Updates ──────────────────────
def process_google_document_update(document_id, app_context):
    """
    Placeholder function for background processing of Google Document updates.
    This function will be executed in a separate thread by the ThreadPoolExecutor.
    It fetches content from Google Drive based on MIME type and processes it for RAG.
    """
    with app_context:
        try:
            logging.info(f"Background task started for Google Drive document_id: {document_id}")

            # Get RAG components from Flask app config
            vector_store = current_app.config.get('VECTOR_STORE')
            embeddings = current_app.config.get('EMBEDDINGS')

            if not vector_store or not embeddings:
                logging.critical(f"Background task for {document_id}: VECTOR_STORE or EMBEDDINGS not found in app.config. Aborting RAG update.")
                return

            # Get MIME type of the Google Drive file
            mime_type = get_google_drive_file_mime_type(document_id)
            if mime_type is None:
                logging.error(f"Background task for {document_id}: Failed to fetch MIME type, or file not found/accessible. Aborting RAG update.")
                return

            text_content = None
            # Fetch content based on MIME type
            if mime_type == 'application/vnd.google-apps.document':
                logging.info(f"Document ID {document_id} is a Google Doc. Fetching content...")
                text_content = get_google_doc_content(document_id)
            elif mime_type == 'application/vnd.google-apps.spreadsheet':
                logging.info(f"Document ID {document_id} is a Google Sheet. Fetching content...")
                text_content = get_google_sheet_content(document_id)
            else:
                logging.warning(f"Unsupported MIME type '{mime_type}' for document ID {document_id}. Skipping RAG processing.")
                return # Exit if MIME type is not supported

            # Process content if it was successfully fetched
            if text_content is not None:
                logging.info(f"Successfully fetched content for {document_id}. Length: {len(text_content)}. Processing for RAG...")
                success = process_google_document_text(document_id, text_content, vector_store, embeddings)
                if success:
                    logging.info(f"Successfully processed and updated RAG store for document ID {document_id}.")
                else:
                    logging.error(f"Failed to process document ID {document_id} for RAG store.")
            else:
                # This case implies get_google_doc_content or get_google_sheet_content returned None
                logging.error(f"Failed to fetch content for document ID {document_id} (MIME type: {mime_type}). RAG store not updated.")

            logging.info(f"Background task finished for Google Drive document_id: {document_id}")

        except Exception as e:
            logging.error(f"Unexpected error in background task for document_id {document_id}: {e}", exc_info=True)

def is_property_related_query(text):
    """Checks if a query is generally about properties using keywords."""
    keywords = [
        'property', 'properties', 'apartment', 'villa', 'house',
        'buy', 'rent', 'lease', 'listing', 'listings', 'available', 'real estate'
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)

if __name__ == '__main__':
    # Set up webhook on startup
    set_webhook()

    # Start the Flask app
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
