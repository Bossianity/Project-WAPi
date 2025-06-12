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
from media_handler import download_and_decrypt_media
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
    process_google_document_text # Added for Google Drive document processing
)
from google_drive_handler import ( # Added for Google Drive document processing
    get_google_drive_file_mime_type,
    get_google_doc_content,
    get_google_sheet_content
)
from outreach_handler import process_outreach_campaign # For outreach feature
from whatsapp_utils import send_whatsapp_message, send_whatsapp_image_message # For sending WhatsApp messages

# ─── Data Ingestion Configuration ──────────────────────────────────────────────
COMPANY_DATA_FOLDER = 'company_data'

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
OPENAI_API_KEY      = os.getenv('OPENAI_API_KEY')
WASENDER_API_TOKEN  = os.getenv('WASENDER_API_TOKEN')
WASENDER_API_URL    = "https://www.wasenderapi.com/api/send-message"
PROPERTY_SHEET_ID   = os.getenv('PROPERTY_SHEET_ID')
PROPERTY_SHEET_NAME = os.getenv('PROPERTY_SHEET_NAME', 'Properties')

# --- Global Pause Feature ---
# These variables are in-memory and will be reset if the Flask app restarts or is redeployed.
is_globally_paused = False
paused_conversations = set()

# ─── Persona loading ───────────────────────────────────────────────────────────
PERSONA_FILE    = 'persona.json'

PERSONA_NAME = "Emran"

BASE_PROMPT = (
    "You are Emran, a seasoned real estate sales agent. Your tone is professional, convincing, and direct. You are a businessman focused on closing deals and providing accurate information. Your responses are clear, concise, and to the point. You do not use emojis or other informalities."
    "CRITICAL LANGUAGE RULE: Your response MUST ALWAYS be in the SAME language as the user's last message. If the user messages in English, you reply in English. If they message in Arabic, you MUST reply in Emirati dialect."
    "Failure to adhere to this language rule is a critical failure."
    "YOUR RESPONSES MUST BE GROUNDED ON THE CONTEXT PROVIDED. Do not invent details about properties (prices, features, availability) that are not present in the 'Relevant Information Found'. Your goal is to answer the user's query using the provided context. If the context does not sufficiently answer the query about a factual aspect of a property, state that you will check for that specific detail and get back to them. In this situation, append the exact string `[ACTION_NOTIFY_UNANSWERED_QUERY]` to the end of your response."
    "You should be persuasive and look for opportunities to upsell or highlight the value of a property, but without sounding needy. You are an expert, not just a salesperson."
    "You understand context from recent messages. If a user has provided information, do not ask for it again."
    "When starting a new conversation, begin with a direct greeting: 'This is Emran from [Your Business Name]. How may I assist you with your property inquiry?' Adapt this to the user's language."
    "If a user wants to schedule a viewing, gather the necessary details: their full name, preferred date and time, and the property of interest (if not already clear). Once you have these details, confirm by stating: 'Thank you. I have your details. A member of our team will contact you shortly to confirm your viewing appointment.' Then, append the exact string `[ACTION_SEND_EMAIL_CONFIRMATION]` to the very end of your response."
    "TEXT STYLING (CRUCIAL): Absolutely NO emojis, asterisks (*), or any other markdown syntax should be used. Your responses must be plain text. Perform a final check to ensure no such characters are present before sending."
    "LANGUAGE & DIALECT: Assist the client in any language. If the client uses Arabic, you MUST switch to a professional Emirati dialect (e.g., 'مرحباً، وياك عمران بخصوص العقارات', 'البيانات المطلوبة هي', 'سيتم التواصل معاك قريباً')."
    "HANDLING RETRIEVED INFORMATION (RAG CONTEXT): When 'Relevant Information Found' is provided, you MUST carefully review all snippets. Synthesize the information to formulate a comprehensive and direct answer. Do not reproduce the original formatting of the source data. Your final response must be clean, natural language."
    "END OF CONVERSATION: You do not always need to end with a question or an offer for more help. Sometimes, simply providing the answer is sufficient. Be efficient."
    "IMAGE SENDING TASK: If an 'IMAGE_ENTRY' in the 'Relevant Information Found' is relevant to the user's query, you MUST respond with the strict 3-line format: \n[ACTION_SEND_IMAGE_VIA_URL]\nThe_ImageURL_from_the_IMAGE_ENTRY\nThe_Caption_from_the_IMAGE_ENTRY\nWhen you use this format, no other text should precede or follow it."
)

try:
    with open(PERSONA_FILE) as f:
        p = json.load(f)
        # PERSONA_NAME = p.get('name', PERSONA_NAME) # Commented out to ensure "Layla" is used from script
        pass # persona.json is not used to override Layla's name defined in BASE_PROMPT
    logging.info(f"Persona name is '{PERSONA_NAME}'. System prompt is now controlled by script.py's BASE_PROMPT.")
except Exception as e:
    logging.warning(f"Could not load {PERSONA_FILE} or parse 'name': {e}. Using default name '{PERSONA_NAME}'. System prompt is controlled by script.py's BASE_PROMPT.")

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

    # --- Step 2: Execute Logic Based on Intent ---
    context_str = ""
    if intent == "property_search" and PROPERTY_SHEET_ID:
        # --- Structured Property Search Logic ---
        all_properties_df = property_handler.get_sheet_data()
        if not all_properties_df.empty:
            filtered_df = property_handler.filter_properties(all_properties_df, filters)

            if not filtered_df.empty:
                context_str = "Relevant Information Found:\n"
                for _, prop in filtered_df.head(5).iterrows():
                    prop_details = (
                        f"Title: {prop['Title']}\n"
                        f"Location: {prop['area']}, {prop['city']}, {prop['emirate']}\n"
                        f"Price: {prop['Price_AED']} AED\n"
                        f"Bedrooms: {prop['Bedrooms']}\n"
                        f"Description: {prop['Description']}\n"
                    )
                    context_str += prop_details
                    for img_col in ['img1', 'img2', 'img3']:
                        if prop[img_col] and isinstance(prop[img_col], str) and prop[img_col].startswith('http'):
                            context_str += f"[ACTION_SEND_IMAGE_VIA_URL]\n{prop[img_col]}\n{prop['Title']}\n"
                    context_str += "---\n"
            else:
                context_str = "Relevant Information Found:\nNo properties found matching your specific criteria. I can search again if you adjust your filters."
        else:
             context_str = "Relevant Information Found:\nI was unable to access the property listings. Please try again shortly."

    else:
        # --- Fallback to General RAG (Vector Search) Logic ---
        logging.info("Performing general RAG query using vector store.")
    if 'current_app' in globals() and current_app: 
        vector_store = current_app.config.get('VECTOR_STORE')
    else: 
        vector_store = vector_store_rag

        if vector_store:
            retrieved_docs = query_vector_store(text, vector_store, k=5)
            if retrieved_docs:
                processed_docs_content = [re.sub(r'\*+\s*(.*?)\s*\*+', r'\1', doc.page_content) for doc in retrieved_docs]
                context_str = "\n\nRelevant Information Found:\n" + "\n".join(processed_docs_content)
            else:
                logging.info("No relevant context found in vector store for general query.")
        else:
            logging.warning("Vector store not available for general query.")

    # --- Step 3: Generate Final Response Based on Context ---
    final_prompt_to_llm = context_str + f"\n\nUser Question: {text}" if context_str else text

    messages = [SystemMessage(content=BASE_PROMPT)]
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

            final_response_data = {'type': 'text', 'content': raw_llm_output} # Default to text

            # Check for image action and try to parse
            if "[ACTION_SEND_IMAGE_VIA_URL]" in raw_llm_output:
                image_parts = []
                for line in raw_llm_output.splitlines():
                    # Collect image related lines only if they are formatted as expected
                    if line.strip() == "[ACTION_SEND_IMAGE_VIA_URL]" or (image_parts and len(image_parts) < 3):
                        image_parts.append(line)
                
                if len(image_parts) >= 3:
                    image_url = image_parts[1].strip()
                    image_caption = image_parts[2].strip()
                    if image_url.startswith('http'): # Basic validation for URL
                        final_response_data = {'type': 'image', 'url': image_url, 'caption': image_caption}
                        # If an image is successfully parsed, we can return it immediately
                        return final_response_data
            
            # If we reach here, it means either no image action was detected or it was malformed.
            # Now process the raw_llm_output for text, stripping all action tokens.
            response_text_for_display = raw_llm_output.replace("[ACTION_NOTIFY_UNANSWERED_QUERY]", "").replace("[ACTION_SEND_EMAIL_CONFIRMATION]", "").strip()
            response_text_for_display = response_text_for_display.replace("[ACTION_SEND_IMAGE_VIA_URL]", "").strip() # Ensure image token is also removed from text output
            
            # If after stripping, there's still meaningful text, return it as a text message
            if response_text_for_display: 
                return {'type': 'text', 'content': response_text_for_display}

            # If nothing was returned (neither image nor text), log a warning
            logging.warning(f"LLM returned an empty or token-only response on attempt {attempt+1}")
        
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
def health_check(): return "OK", 200

# Helper function to extract Google Sheet ID from URL or use if already an ID
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


# ─── Webhook endpoint ─────────────────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json or {} 

        now_utc = datetime.now(pytz.utc)
        now_dubai = now_utc.astimezone(DUBAI_TIMEZONE)
        current_hour_dubai = now_dubai.hour

        is_operational = False
        if OPERATIONAL_START_HOUR_DUBAI > OPERATIONAL_END_HOUR_DUBAI: 
            if current_hour_dubai >= OPERATIONAL_START_HOUR_DUBAI or current_hour_dubai < OPERATIONAL_END_HOUR_DUBAI:
                is_operational = True
        else: 
            if OPERATIONAL_START_HOUR_DUBAI <= current_hour_dubai < OPERATIONAL_END_HOUR_DUBAI:
                is_operational = True
        
        # Operational hours check remains commented as per user's original script state
        # if not is_operational:
        #     # ... logging and return ...

        if not isinstance(data, dict) or 'event' not in data or 'data' not in data:
            logging.warning(f"Webhook received malformed data: {data}")
            return jsonify(status='ignored: malformed data'), 400
        if data.get('event') != 'messages.upsert':
            logging.info(f"Webhook ignored event: {data.get('event')}")
            return jsonify(status='ignored: not a message upsert event'), 200
        
        msg_data_outer = data.get('data', {})
        messages_payload = msg_data_outer.get('messages') 
        if not messages_payload: 
            messages_payload = msg_data_outer

        if not messages_payload or not isinstance(messages_payload, dict):
            logging.warning(f"Webhook: 'messages' field missing, not a dict, or in unexpected location in data: {data.get('data')}")
            return jsonify(status='ignored: no message data or malformed'), 200

        if messages_payload.get('key', {}).get('fromMe'):
            logging.info("Webhook ignored: message is from me.")
            return jsonify(status='ignored: from me'), 200
        
        sender = messages_payload.get('key', {}).get('remoteJid')
        message_content_dict = messages_payload.get('message', {})
        
        # Initialize variables for message processing
        body = None
        is_media = False
        media_info = None
        media_type = None # e.g., "audio", "image", "video"

        if message_content_dict:
            if 'conversation' in message_content_dict:
                body = message_content_dict['conversation']
            elif 'extendedTextMessage' in message_content_dict:
                body = message_content_dict['extendedTextMessage'].get('text')

            # Check for media messages
            if 'audioMessage' in message_content_dict:
                is_media = True
                media_type = "audio"
                media_info = message_content_dict['audioMessage']
                logging.info(f"Received audio message from {sender}")
            elif 'imageMessage' in message_content_dict:
                is_media = True
                media_type = "image"
                media_info = message_content_dict['imageMessage']
                # For images, Layla's RAG is expected to find an image and respond with [ACTION_SEND_IMAGE_VIA_URL]
                # So, we might just set body to a placeholder indicating image received.
                body = "[User sent an image. Analyzing context...]"
                logging.info(f"Received image message from {sender}. Body set for RAG analysis.")
            elif 'videoMessage' in message_content_dict:
                is_media = True
                media_type = "video"
                media_info = message_content_dict['videoMessage']
                # Similar to images, Layla might describe or react based on RAG context.
                body = "[User sent a video. Analyzing context...]"
                logging.info(f"Received video message from {sender}. Body set for RAG analysis.")

            if is_media and media_info:
                media_url = media_info.get('url')
                media_key_b64 = media_info.get('mediaKey')

                if not media_url or not media_key_b64:
                    logging.error(f"Media message from {sender} is missing URL or mediaKey. MediaInfo: {media_info}")
                    body = "[Media processing error: Missing URL or key]"
                else:
                    if media_type in ["audio", "image", "video"]: # Ensure media_type is one of these before decryption
                        try:
                            logging.info(f"Attempting to download and decrypt {media_type} from {sender}. URL: {media_url[:50]}...")
                            decrypted_media_content = download_and_decrypt_media(media_url, media_key_b64, media_type)

                            if decrypted_media_content:
                                logging.info(f"Successfully decrypted {media_type} from {sender}. Size: {len(decrypted_media_content)} bytes.")
                                if media_type == "audio":
                                    if openai_client:
                                        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_audio_file:
                                            tmp_audio_file.write(decrypted_media_content)
                                            tmp_audio_file_path = tmp_audio_file.name

                                        try:
                                            logging.info(f"Transcribing audio file: {tmp_audio_file_path}")
                                            transcript = openai_client.audio.transcriptions.create(
                                                model="whisper-1",
                                                file=open(tmp_audio_file_path, "rb")
                                            )
                                            body = transcript.text
                                            logging.info(f"Transcription result for {sender}: {body}")
                                        except Exception as e_transcribe:
                                            logging.error(f"Whisper API transcription failed for {sender}: {e_transcribe}", exc_info=True)
                                            body = "[Audio transcription failed. Please try again or type your message.]"
                                        finally:
                                            if os.path.exists(tmp_audio_file_path):
                                                os.remove(tmp_audio_file_path)
                                    else:
                                        logging.warning("OpenAI client not initialized. Cannot transcribe audio.")
                                        body = "[Audio received, but transcription service is unavailable.]"
                                # For image/video, body is already set to a placeholder for RAG.
                                # If specific decrypted content handling for image/video (not RAG based) is needed, add here.
                            else:
                                logging.error(f"Failed to decrypt {media_type} from {sender}.")
                                body = f"[{media_type.capitalize()} decryption failed. Please try sending again.]"
                        except ValueError as ve: # Catch errors from get_decryption_keys for unsupported media types
                            logging.error(f"Media handling error for {sender} ({media_type}): {ve}")
                            body = f"[Unsupported media type for decryption: {media_type}]"
                        except Exception as e_decrypt:
                            logging.error(f"General error during media download/decryption for {sender} ({media_type}): {e_decrypt}", exc_info=True)
                            body = f"[{media_type.capitalize()} processing failed. Please try sending again.]"
                    else:
                        # This case should ideally not be reached if media_type is set correctly above.
                        logging.warning(f"Media message from {sender} has an unexpected media_type '{media_type}'. MediaInfo: {media_info}")
                        body = "[Received media of an unexpected type.]"
            # If 'body' is still None here (e.g. it was not a text message and not a recognized media message),
            # the check below `if not (sender and body)` will catch it.

        if not (sender and body):
            # This log now correctly reflects that 'body' might be None due to various reasons,
            # including unrecognized message types or errors in media processing.
            logging.warning(f"Webhook ignored: no sender or body could be processed. Sender: {sender}, Body: {body}, Initial Message Content Dict: {message_content_dict if message_content_dict else 'N/A'}")
            return jsonify(status='ignored: no sender or final body content'), 200

        # --- Bot Control Command Handling ---
        normalized_body = body.lower().strip()
        global is_globally_paused # Needed for reassignment

        if normalized_body == "bot pause all":
            is_globally_paused = True
            send_whatsapp_message(sender, "Bot is now globally paused.")
            logging.info(f"Bot globally paused by {sender}.")
            return jsonify(status='success_paused_all'), 200

        if normalized_body == "bot resume all":
            is_globally_paused = False
            paused_conversations.clear()
            send_whatsapp_message(sender, "Bot is now globally resumed. All specific conversation pauses have been cleared.")
            logging.info(f"Bot globally resumed by {sender}. Specific pauses cleared.")
            return jsonify(status='success_resumed_all'), 200

        if normalized_body.startswith("bot pause "):
            parts = normalized_body.split("bot pause ", 1)
            if len(parts) > 1 and parts[1].strip():
                target_user_id = parts[1].strip()
                # Ensure target_user_id is normalized if it's expected to match sender format (e.g. with @s.whatsapp.net)
                # For now, assuming it's a direct match or an admin will provide the correct format.
                paused_conversations.add(target_user_id)
                send_whatsapp_message(sender, f"Bot interactions will be paused for: {target_user_id}")
                logging.info(f"Bot interactions paused for {target_user_id} by {sender}.")
            else:
                send_whatsapp_message(sender, "Invalid command format. Use: bot pause <target_user_id>")
                logging.info(f"Invalid 'bot pause' command from {sender}: {normalized_body}")
            return jsonify(status='success_paused_specific_or_error'), 200

        if normalized_body.startswith("bot resume "):
            parts = normalized_body.split("bot resume ", 1)
            if len(parts) > 1 and parts[1].strip():
                target_user_id = parts[1].strip()
                paused_conversations.discard(target_user_id) # Use discard to avoid error if ID not in set
                send_whatsapp_message(sender, f"Bot interactions will be resumed for: {target_user_id}")
                logging.info(f"Bot interactions resumed for {target_user_id} by {sender}.")
            else:
                send_whatsapp_message(sender, "Invalid command format. Use: bot resume <target_user_id>")
                logging.info(f"Invalid 'bot resume' command from {sender}: {normalized_body}")
            return jsonify(status='success_resumed_specific_or_error'), 200

        # --- End of Bot Control Command Handling ---

        # --- Outreach Command Handling (NEW) ---
        is_outreach_command = False
        original_sheet_specifier = None # Will store the raw input (URL or ID from command/env)

        if normalized_body == "bot start outreach":
            original_sheet_specifier = os.getenv('DEFAULT_OUTREACH_SHEET_ID')
            is_outreach_command = True
            logging.info(f"Outreach command detected. Attempting to use default Sheet specifier: '{original_sheet_specifier}' (if set).")
        elif normalized_body.startswith("bot start outreach "):
            parts = normalized_body.split("bot start outreach ", 1)
            original_sheet_specifier = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
            is_outreach_command = True
            if not original_sheet_specifier: # Command was "bot start outreach " (with trailing space but no ID)
                original_sheet_specifier = os.getenv('DEFAULT_OUTREACH_SHEET_ID')
                logging.info(f"Outreach command with trailing space detected. Attempting to use default Sheet specifier: '{original_sheet_specifier}' (if set).")
            else:
                logging.info(f"Outreach command detected with specific Sheet specifier: '{original_sheet_specifier}'")

        if is_outreach_command:
            parsed_sheet_id = extract_sheet_id_from_url(original_sheet_specifier)

            if not parsed_sheet_id:
                error_msg = (
                    f"Error: No Google Sheet ID or URL was provided, or the provided one ('{original_sheet_specifier}') is invalid. "
                    "Please specify a valid Google Sheet ID or URL, or ensure DEFAULT_OUTREACH_SHEET_ID is correctly set."
                )
                send_whatsapp_message(sender, error_msg)
                logging.warning(f"Outreach command failed for {sender}: Invalid or missing Sheet specifier ('{original_sheet_specifier}').")
                return jsonify(status='error_invalid_sheet_specifier'), 200

            agent_sender_id = sender
            # Use parsed_sheet_id in user-facing messages and for processing
            send_whatsapp_message(agent_sender_id, f"Outreach campaign started using Sheet ID: {parsed_sheet_id}. You will be notified upon completion.")

            current_app_context = current_app.app_context()
            try:
                executor.submit(process_outreach_campaign, parsed_sheet_id, agent_sender_id, current_app_context)
                logging.info(f"Outreach campaign initiated by {agent_sender_id} for Sheet ID: {parsed_sheet_id} (Original specifier: '{original_sheet_specifier}').")
                return jsonify(status='outreach_campaign_started'), 200
            except Exception as e_executor:
                logging.error(f"Failed to submit outreach campaign to executor for Sheet ID {parsed_sheet_id} (Original specifier: '{original_sheet_specifier}'). Error: {e_executor}", exc_info=True)
                send_whatsapp_message(agent_sender_id, "Error: Could not start the outreach campaign due to an internal issue.")
                return jsonify(status='error_starting_outreach_task'), 500
        # --- End of Outreach Command Handling ---

        # --- Check for Pause States (Global or Specific Conversation) ---
        if is_globally_paused:
            logging.info(f"Bot is globally paused. Ignoring message from {sender}: {body[:100]}...") # Log a snippet of body
            return jsonify(status='ignored_globally_paused'), 200

        # Ensure 'sender' is used for checking against 'paused_conversations'
        # 'sender' typically is in the format 'xxxxxxxxxxx@s.whatsapp.net'
        # 'target_user_id' when added to paused_conversations should match this format or be adapted.
        # For now, assuming 'sender' is the correct key format for the set.
        if sender in paused_conversations:
            logging.info(f"Conversation with {sender} is paused. Ignoring message: {body[:100]}...") # Log a snippet of body
            return jsonify(status='ignored_specifically_paused'), 200

        # --- End of Pause State Checks ---
            
        user_id = ''.join(c for c in sender if c.isalnum()) 
        logging.info(f"Incoming from {sender} (UID: {user_id}): {body}")
        
        history = load_history(user_id)
        llm_response_data = get_llm_response(body, sender, history)
        
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
                    # MODIFICATION: Increased delay to help prevent messages from arriving out of order.
                    time.sleep(random.uniform(2.0, 3.0))
        else:
            logging.error(f"Unknown response type from get_llm_response: {llm_response_data.get('type')}")
            final_model_response_for_history = "[Error: Unknown response type from LLM]"
            error_message = "I'm having a bit of trouble processing that request. Could you try rephrasing?"
            send_whatsapp_message(sender, error_message)
            
        new_history_user = {'role': 'user', 'parts': [body]}
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

# ─── Webhook Endpoint for Google Document/Sheet Synchronization ────────────────
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


if __name__ == '__main__':
    # Ensure RAG components are available in app.config if initialized globally
    # This helps if the app is run directly (python script.py) vs gunicorn
    # For gunicorn, initialization before app creation is usually fine.
    if not app.config.get('VECTOR_STORE') and 'vector_store_rag' in globals() and vector_store_rag:
        app.config['VECTOR_STORE'] = vector_store_rag
    if not app.config.get('EMBEDDINGS') and 'embeddings_rag' in globals() and embeddings_rag:
        app.config['EMBEDDINGS'] = embeddings_rag
        
    port = int(os.environ.get("PORT", 5001)) # Default to 5001 if not set
    # debug=False is appropriate for production/staging with gunicorn
    # For local testing, you might set debug=True, but be mindful of executor behavior with Flask's reloader.
    app.run(host='0.0.0.0', port=port, debug=False)

    # Note on ThreadPoolExecutor shutdown:
    # For a production deployment with Gunicorn, Gunicorn manages worker processes.
    # When Gunicorn stops, it will typically terminate the Python processes, which
    # should lead to the ThreadPoolExecutor being cleaned up.
    # Explicitly calling executor.shutdown(wait=True) here would only run if
    # `app.run()` completes, which it normally doesn't until the server is stopped.
    # If running this script standalone and expecting it to exit cleanly after some
    # condition (not typical for a web server), then `executor.shutdown()` would be
    # more relevant to place, perhaps in a try/finally block around `app.run()`.
    # For now, relying on Gunicorn's process management is sufficient.
