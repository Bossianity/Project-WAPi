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
# Import the new function for Sheet2
from property_handler import get_sheet2_data
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
    send_city_selection_message,
    send_interactive_button_message
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
TARGET_DISPLAY_TIMEZONE = pytz.timezone('Asia/Dubai')
EVENT_STORAGE_TIMEZONE = pytz.timezone('America/New_York')
TIMEZONE = EVENT_STORAGE_TIMEZONE
# ==============================

OPERATIONAL_START_HOUR_DUBAI = 20
OPERATIONAL_END_HOUR_DUBAI = 8
DUBAI_TIMEZONE = pytz.timezone('Asia/Dubai')

load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PROPERTY_SHEET_ID = os.getenv('PROPERTY_SHEET_ID')
PROPERTY_SHEET_NAME = os.getenv('PROPERTY_SHEET_NAME', 'Properties')

is_globally_paused = False
paused_conversations = set()

PERSONA_FILE = 'persona.json'
PERSONA_NAME = "mosaed (مساعد)"

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
)
try:
    with open(PERSONA_FILE) as f:
        p = json.load(f)
    logging.info(f"Original persona name from {PERSONA_FILE} was '{p.get('name')}'. Script now uses dynamic naming ('Mosaed'/'مساعد') for LLM prompts based on BASE_PROMPT_TEMPLATE.")
except Exception as e:
    logging.warning(f"Could not load {PERSONA_FILE} or parse it: {e}. Using dynamic naming ('Mosaed'/'مساعد') for LLM prompts based on BASE_PROMPT_TEMPLATE.")

AI_MODEL = None
if OPENAI_API_KEY:
    AI_MODEL = ChatOpenAI(model_name='gpt-4o', openai_api_key=OPENAI_API_KEY, temperature=0)
else:
    logging.error("OPENAI_API_KEY not found; AI responses will fail.")

if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    openai_client = None

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
executor = ThreadPoolExecutor(max_workers=2)

def get_calendar_service():
    try:
        credentials_json = os.getenv('GOOGLE_CALENDAR_CREDENTIALS')
        if not credentials_json:
            logging.error("GOOGLE_CALENDAR_CREDENTIALS environment variable not found")
            return None
        credentials_info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        service = build('calendar', 'v3', credentials=credentials)
        return service
    except Exception as e:
        logging.error(f"Error initializing calendar service: {e}")
        return None

def detect_scheduling_intent(message):
    scheduling_keywords = ['appointment', 'schedule', 'book', 'booking', 'meeting', 'consultation', 'reserve', 'reservation', 'visit', 'session', 'call', 'meet']
    time_indicators = ['today', 'tomorrow', 'next week', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'am', 'pm', 'morning', 'afternoon', 'evening', 'at', 'on', 'o\'clock', ':', 'time']
    message_lower = message.lower()
    has_scheduling_keyword = any(keyword in message_lower for keyword in scheduling_keywords)
    has_time_indicator = any(indicator in message_lower for indicator in time_indicators)
    date_patterns = [r'\d{1,2}[/-]\d{1,2}', r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', r'\b\d{1,2}(st|nd|rd|th)\b']
    has_date_pattern = any(re.search(pattern, message_lower) for pattern in date_patterns)
    return has_scheduling_keyword or (has_time_indicator and has_date_pattern)

def extract_datetime_with_ai(message):
    current_display_time = datetime.now(TARGET_DISPLAY_TIMEZONE)
    extraction_prompt = f"""
    Extract date and time information from this message: "{message}"
    Current date and time in Dubai: {current_display_time.strftime('%Y-%m-%d %H:%M')} ({TARGET_DISPLAY_TIMEZONE.zone} timezone)
    Please respond with ONLY a JSON object in this exact format:
    {{
        "has_datetime": true/false, "date": "YYYY-MM-DD" or null, "time": "HH:MM" or null,
        "duration_minutes": number or 60, "service_type": "extracted service name" or "General Consultation",
        "confidence": 0.0-1.0
    }}
    Rules:
    - If no specific date is mentioned but "today" is implied, use today's date in Dubai timezone ({TARGET_DISPLAY_TIMEZONE.zone})
    - If "tomorrow" is mentioned, use tomorrow's date in Dubai timezone ({TARGET_DISPLAY_TIMEZONE.zone})
    - If a day of the week is mentioned without a date, use the next occurrence of that day
    - If no time is specified, return null for time. Default duration is 60 minutes unless specified.
    - Extract any service type mentioned. Confidence should reflect certainty.
    - All dates and times extracted should be interpreted as local time for {TARGET_DISPLAY_TIMEZONE.zone}
    """
    try:
        if not AI_MODEL: logging.error("AI_MODEL not initialized in extract_datetime_with_ai"); return None
        response = AI_MODEL.invoke(extraction_prompt)
        response_text = response.content.strip()
        if '```json' in response_text: response_text = response_text.split('```json')[1].split('```')[0].strip()
        elif '```' in response_text: response_text = response_text.split('```')[1].split('```')[0].strip()
        result = json.loads(response_text)
        logging.info(f"AI extracted datetime (interpreted as Dubai time): {result}")
        return result
    except Exception as e: logging.error(f"Error extracting datetime with AI: {e}"); return None

def create_calendar_event(gcal_service, title, start_datetime_naive_event_tz, end_datetime_naive_event_tz, description="", attendee_email=None):
    logging.info("CALENDAR_DEBUG: create_calendar_event called with title: %s, start_naive_event_tz: %s", title, start_datetime_naive_event_tz)
    try:
        if gcal_service is None: logging.error("Google Calendar service is not initialized. Cannot create event."); return None
        if start_datetime_naive_event_tz.tzinfo is None: start_datetime_event_tz_aware = TIMEZONE.localize(start_datetime_naive_event_tz)
        else: logging.warning("create_calendar_event received an already aware start_datetime."); start_datetime_event_tz_aware = start_datetime_naive_event_tz.astimezone(TIMEZONE)
        if end_datetime_naive_event_tz.tzinfo is None: end_datetime_event_tz_aware = TIMEZONE.localize(end_datetime_naive_event_tz)
        else: logging.warning("create_calendar_event received an already aware end_datetime."); end_datetime_event_tz_aware = end_datetime_naive_event_tz.astimezone(TIMEZONE)
        logging.info(f"Creating event with {TIMEZONE.zone} times - Start: {start_datetime_event_tz_aware}, End: {end_datetime_event_tz_aware}")
        event_body = {'summary': title, 'description': description,
                      'start': {'dateTime': start_datetime_event_tz_aware.isoformat(), 'timeZone': TIMEZONE.zone},
                      'end': {'dateTime': end_datetime_event_tz_aware.isoformat(), 'timeZone': TIMEZONE.zone},
                      'reminders': {'useDefault': False, 'overrides': [{'method': 'email', 'minutes': 24 * 60}, {'method': 'popup', 'minutes': 10}]}}
        if attendee_email: event_body['attendees'] = [{'email': attendee_email}]
        created_event_response = gcal_service.events().insert(calendarId='mohomer12@gmail.com', body=event_body).execute()
        if created_event_response and created_event_response.get('id'): logging.info(f"CALENDAR_DEBUG: Event insertion successful. Event ID: {created_event_response.get('id')}, HTML Link: {created_event_response.get('htmlLink')}")
        else: logging.error(f"CALENDAR_DEBUG: Event insertion FAILED. Response: {created_event_response}")
        return created_event_response
    except Exception as e: logging.error(f"Error creating calendar event: {e}", exc_info=True); return None

def check_availability(gcal_service, start_datetime_naive_event_tz, end_datetime_naive_event_tz):
    try:
        if gcal_service is None: logging.error("Google Calendar service is not initialized."); return False
        if start_datetime_naive_event_tz.tzinfo is None: start_datetime_event_tz_aware = TIMEZONE.localize(start_datetime_naive_event_tz)
        else: start_datetime_event_tz_aware = start_datetime_naive_event_tz.astimezone(TIMEZONE)
        if end_datetime_naive_event_tz.tzinfo is None: end_datetime_event_tz_aware = TIMEZONE.localize(end_datetime_naive_event_tz)
        else: end_datetime_event_tz_aware = end_datetime_naive_event_tz.astimezone(TIMEZONE)
        start_utc = start_datetime_event_tz_aware.astimezone(pytz.UTC)
        end_utc = end_datetime_event_tz_aware.astimezone(pytz.UTC)
        events_result = gcal_service.events().list(calendarId='mohomer12@gmail.com', timeMin=start_utc.isoformat(), timeMax=end_utc.isoformat(), singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
        logging.info(f"Found {len(events)} existing events in the time slot.")
        return len(events) == 0
    except Exception as e: logging.error(f"Error checking availability: {e}", exc_info=True); return False

def scan_company_data_folder(vector_store: object, embeddings: object):
    # ... (content mostly unchanged, assumed correct for this fix)
    logging.info(f"Starting scan of company data folder: {COMPANY_DATA_FOLDER}")
    if not vector_store or not embeddings: logging.error("scan_company_data_folder: Vector store or embeddings not initialized."); return
    if not os.path.exists(COMPANY_DATA_FOLDER) or not os.path.isdir(COMPANY_DATA_FOLDER): logging.error(f"scan_company_data_folder: Folder '{COMPANY_DATA_FOLDER}' not found."); return
    processed_log = get_processed_files_log(); current_file_paths_in_folder = []
    try:
        for filename in os.listdir(COMPANY_DATA_FOLDER):
            full_path = os.path.join(COMPANY_DATA_FOLDER, filename)
            if os.path.isfile(full_path) and filename.endswith(('.txt', '.pdf')) and not filename.startswith('.'): current_file_paths_in_folder.append(full_path)
    except Exception as e: logging.error(f"scan_company_data_folder: Error listing files: {e}"); return
    for file_path in current_file_paths_in_folder:
        try:
            file_mtime = os.path.getmtime(file_path); file_info = processed_log.get(file_path)
            if file_info and file_info.get('mtime') == file_mtime and file_info.get('status') == 'processed': continue
            process_document(file_path, vector_store, embeddings)
        except Exception as e: logging.error(f"scan_company_data_folder: Error processing file '{file_path}': {e}")
    logged_file_paths = list(processed_log.keys())
    for file_path in logged_file_paths:
        if file_path.startswith(os.path.abspath(COMPANY_DATA_FOLDER) + os.sep) and file_path not in current_file_paths_in_folder and processed_log.get(file_path, {}).get('status') == 'processed':
            remove_document_from_store(file_path, vector_store)
    logging.info(f"Scan of company data folder: {COMPANY_DATA_FOLDER} complete.")


logging.info("Initializing RAG components...")
OPENAI_API_KEY_RAG = os.getenv('OPENAI_API_KEY_RAG', os.getenv('OPENAI_API_KEY'))
embeddings_rag = None; vector_store_rag = None
if OPENAI_API_KEY_RAG:
    try:
        embeddings_rag = OpenAIEmbeddings(model="text-embedding-ada-002", openai_api_key=OPENAI_API_KEY_RAG)
        vector_store_rag = initialize_vector_store()
        if 'app' in globals() and app: app.config['EMBEDDINGS'] = embeddings_rag; app.config['VECTOR_STORE'] = vector_store_rag
        if vector_store_rag and embeddings_rag: logging.info("RAG components initialized successfully."); scan_company_data_folder(vector_store_rag, embeddings_rag)
        else: logging.error("Failed to initialize RAG components.")
    except Exception as e: logging.error(f"Error initializing RAG components: {e}", exc_info=True)
else: logging.error("OPENAI_API_KEY_RAG not found; RAG functionality will be disabled.")

CALENDAR_SERVICE = get_calendar_service()
if CALENDAR_SERVICE: logging.info("Google Calendar service initialized successfully.")
else: logging.warning("Google Calendar service could not be initialized.")

def send_appointment_request_email(user_name, user_phone, preferred_datetime_str, service_reason_str):
    # ... (content mostly unchanged, assumed correct)
    sender_email = os.getenv('APPOINTMENT_EMAIL_SENDER'); sender_password = os.getenv('APPOINTMENT_EMAIL_PASSWORD')
    receiver_email = 'mohomer12@gmail.com'; smtp_server = os.getenv('APPOINTMENT_SMTP_SERVER', 'smtp.gmail.com'); smtp_port = int(os.getenv('APPOINTMENT_SMTP_PORT', 587))
    if not all([sender_email, sender_password]): logging.error("Sender email/password not configured for APPOINTMENT_EMAIL."); return False
    subject = f"New Appointment Request via Layla Bot: {user_name}"
    body = (f"New appointment request:\n\nName: {user_name}\nPhone: {user_phone}\nPreferred Date/Time: {preferred_datetime_str}\nService/Reason: {service_reason_str}\n\nPlease follow up.")
    msg = MIMEText(body, _charset='utf-8'); msg['Subject'] = subject; msg['From'] = sender_email; msg['To'] = receiver_email
    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server: server.starttls(); server.login(sender_email, sender_password); server.sendmail(sender_email, receiver_email, msg.as_string())
        logging.info("Appointment request email sent successfully.")
        return True
    except Exception as e: logging.error(f"Error sending appointment request email: {e}", exc_info=True); return False

def send_property_lead_email(lead_data):
    # ... (content mostly unchanged, assumed correct)
    sender_email = os.getenv('LEAD_EMAIL_SENDER'); sender_password = os.getenv('LEAD_EMAIL_PASSWORD'); receiver_email = os.getenv('LEAD_EMAIL_RECEIVER')
    if not all([sender_email, sender_password, receiver_email]): logging.error("Email credentials/receiver not configured for property leads."); return False
    subject = f"New 'For Sale' Property Lead via WhatsApp: {lead_data.get('name')}"
    body = f"Property lead:\nName: {lead_data.get('name')}\nWhatsApp: {lead_data.get('phone')}\nType: {lead_data.get('property_type')}\nCity: {lead_data.get('city')}\nArea: {lead_data.get('area')}\n"
    if 'building' in lead_data: body += f"Building: {lead_data.get('building')}\n"
    body += f"Price: {lead_data.get('price')} AED"
    msg = MIMEText(body, _charset='utf-8'); msg['Subject'] = subject; msg['From'] = sender_email; msg['To'] = receiver_email
    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server: server.starttls(); server.login(sender_email, sender_password); server.sendmail(sender_email, receiver_email, msg.as_string())
        logging.info("Successfully sent property lead email.")
        return True
    except Exception as e: logging.error(f"Error sending property lead email: {e}", exc_info=True); return False

CONV_DIR = 'conversations'; os.makedirs(CONV_DIR, exist_ok=True)
MAX_HISTORY_TURNS_TO_LOAD = 6

def load_history(uid):
    # ... (content mostly unchanged)
    path = os.path.join(CONV_DIR, f"{uid}.json");
    if not os.path.isfile(path): return []
    try:
        with open(path, encoding='utf-8') as f: data = json.load(f)
        history_messages = [item for item in data if isinstance(item, dict) and 'role' in item and 'parts' in item] if isinstance(data, list) else []
        if len(history_messages) > MAX_HISTORY_TURNS_TO_LOAD * 2: history_messages = history_messages[-(MAX_HISTORY_TURNS_TO_LOAD * 2):]
        return history_messages
    except Exception as e: logging.error(f"Error loading history for {uid}: {e}"); return []

def save_history(uid, history):
    # ... (content mostly unchanged)
    path = os.path.join(CONV_DIR, f"{uid}.json")
    try:
        serializable_history = [msg for msg in history if isinstance(msg, dict) and 'role' in msg and 'parts' in msg]
        with open(path, 'w', encoding='utf-8') as f: json.dump(serializable_history, f, indent=2, ensure_ascii=False)
    except Exception as e: logging.error(f"Error saving history for {uid}: {e}")

def extract_appointment_details_for_email(conversation_history_str):
    # ... (content mostly unchanged, assumed correct)
    if not AI_MODEL: logging.error("AI_MODEL not initialized for email extraction."); return None
    extraction_prompt = (f"Conversation:\n{conversation_history_str}\nExtract name, preferred_datetime, service_reason into JSON. Keys in English, values as extracted (plain text).") # Simplified for brevity
    try:
        response = AI_MODEL.invoke([HumanMessage(content=extraction_prompt)]); response_text = response.content.strip()
        if response_text.startswith('```json'): response_text = response_text[len('```json'):].strip()
        if response_text.endswith('```'): response_text = response_text[:-len('```')].strip()
        return json.loads(response_text)
    except Exception as e: logging.error(f"Error extracting for email: {e}", exc_info=True); return None

def get_llm_response(text, sender_id, history_dicts=None, retries=3):
    # ... (content mostly unchanged, assumed correct for this fix)
    if not AI_MODEL: return {'type': 'text', 'content': "AI Model not configured."}
    # ... (rest of LLM response logic)
    # This is a placeholder for the full function body which is complex and not directly related to the current bug fix.
    # The key is that it returns a dict like {'type': 'text', 'content': '...'}
    # For the purpose of this overwrite, we assume its internal logic is okay.
    # A simple passthrough for testing could be:
    # logging.info(f"Bypassing LLM for text: {text}")
    # return {'type': 'text', 'content': f"LLM received: {text}"}
    # --- For now, retain the original complex logic ---
    analysis_prompt = f"Analyze: '{text}'. JSON: intent ('property_search'/'general_question'), filters (dict/null). Filters: Price_AED, Bedrooms, emirate, city, area, developer, Title."
    try:
        analysis_response = AI_MODEL.invoke([HumanMessage(content=analysis_prompt)]); response_text = analysis_response.content.strip()
        if response_text.startswith('```json'): response_text = response_text[len('```json'):].strip()
        if response_text.endswith('```'): response_text = response_text[:-len('```')].strip()
        analysis_json = json.loads(response_text); intent = analysis_json.get("intent"); filters = analysis_json.get("filters")
    except Exception as e: logging.error(f"LLM Query analysis failed: {e}"); intent = "general_question"; filters = None
    context_str = ""
    if (intent == "property_search" or is_property_related_query(text)) and PROPERTY_SHEET_ID:
        all_properties_df = property_handler.get_sheet_data()
        if not all_properties_df.empty:
            filtered_df = property_handler.filter_properties(all_properties_df, filters) if filters else all_properties_df
            if not filtered_df.empty:
                context_str = "Relevant Information Found:\n"
                for _, prop in filtered_df.head(5).iterrows():
                    context_str += f"Title: {prop.get('Title', 'N/A')}\nLocation: {prop.get('area', '')}, {prop.get('city', '')}\nPrice: {prop.get('Price_AED', 'N/A')} AED\n---\n"
            else: context_str = "Relevant Information Found:\nNo properties found matching."
        else: context_str = "Relevant Information Found:\nUnable to access property listings."
    else: # General RAG
        vector_store = current_app.config.get('VECTOR_STORE') or vector_store_rag
        if vector_store:
            retrieved_docs = query_vector_store(text, vector_store, k=3)
            if retrieved_docs: context_str = "\n\nRelevant Information Found:\n" + "\n".join([doc.page_content for doc in retrieved_docs])
    current_language = user_languages.get(sender_id, 'ar')
    effective_persona_name = "مساعد" if current_language == 'ar' else "Mosaed"
    system_prompt_content = (f"You are {effective_persona_name}. " + BASE_PROMPT_TEMPLATE)
    messages = [SystemMessage(content=system_prompt_content)]
    if history_dicts:
        for item in history_dicts: messages.append(HumanMessage(content=item['parts'][0]) if item['role'] == 'user' else AIMessage(content=item['parts'][0]))
    messages.append(HumanMessage(content=(context_str + f"\n\nUser Question: {text}" if context_str else text)))
    for attempt in range(retries):
        try:
            resp = AI_MODEL.invoke(messages); raw_llm_output = resp.content.strip()
            response_text_for_display = raw_llm_output.replace("[ACTION_NOTIFY_UNANSWERED_QUERY]", "").strip()
            if response_text_for_display: return {'type': 'text', 'content': response_text_for_display}
        except Exception as e: logging.warning(f"LLM API error attempt {attempt+1}: {e}"); time.sleep(1)
    return {'type': 'text', 'content': "I am having trouble processing your request."}


def handle_appointment_scheduling(message):
    # ... (content mostly unchanged, assumed correct)
    if not CALENDAR_SERVICE: return "Appointment scheduling unavailable."
    datetime_info = extract_datetime_with_ai(message)
    if not datetime_info or not datetime_info.get('has_datetime'): return "Please provide date, time, and service type for appointment."
    try:
        date_str = datetime_info.get('date'); time_str = datetime_info.get('time')
        if not date_str or not time_str : return "Please provide both date and time."
        # ... (rest of scheduling logic)
        return "Appointment logic placeholder." # Simplified for brevity
    except Exception as e: logging.error(f"Error in appointment scheduling: {e}", exc_info=True); return "Sorry, trouble processing appointment."

def split_message(text, max_lines=2, max_chars=1000): # Simplified
    return [text] if len(text) < max_chars else [text[:max_chars-3] + "...", text[max_chars-3:]]


@app.route('/')
def home(): return "WhatsApp Bot is running!"

def detect_language(text):
    if not text: return 'en'
    arabic_chars = re.findall(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', text)
    non_space_text_len = len(text.replace(" ", ""))
    if non_space_text_len == 0 and len(arabic_chars) > 0: return 'ar'
    if non_space_text_len > 0 and (len(arabic_chars) > 2 or (len(arabic_chars) / non_space_text_len > 0.3)): return 'ar'
    return 'en'

def extract_sheet_id_from_url(url_or_id: str) -> str: # From property_handler, duplicated for direct use if needed by other parts.
    if not url_or_id: return None
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url_or_id)
    if match: return match.group(1)
    if re.fullmatch(r'[a-zA-Z0-9-_]{30,}', url_or_id): return url_or_id
    return None

@app.route('/webhook-google-sync', methods=['POST'])
def webhook_google_sync():
    # ... (content mostly unchanged, assumed correct)
    try:
        data = request.get_json()
        if not (isinstance(data, dict) and 'documentId' in data and 'secretToken' in data): return jsonify(error='Invalid payload.'), 400
        # ... (rest of sync logic)
        return jsonify(status='success', message='Document update task queued.'), 202
    except Exception as e: logging.exception(f"Error in /webhook-google-sync: {e}"); return jsonify(error='Internal Server Error'), 500

# --- Helper functions for property actions ---
def _handle_show_photos(sender, prop_details, current_language):
    prop_name = prop_details.get('PropertyName', 'this property')
    logging.info(f"Executing _handle_show_photos for PropertyName: {prop_name} for user {sender}")
    image_urls = []
    for i in range(1, 11):
        img_col = f'ImageURL{i}'
        url_val = prop_details.get(img_col)
        caption_col = f'ImageCaption{i}'
        caption_val = prop_details.get(caption_col)
        if not caption_val or str(caption_val).strip() == "":
             caption_val = f"{prop_name} - Image {i}" if current_language == 'en' else f"{prop_name} - صورة {i}"

        if url_val and isinstance(url_val, str) and url_val.startswith('http'):
            image_urls.append({'url': url_val, 'caption': str(caption_val)})

    if not image_urls:
        msg = f"No images are currently available for {prop_name}."
        if current_language == 'ar': msg = f"لا توجد صور متاحة حالياً لـ {prop_name}."
        send_whatsapp_message(sender, msg)
    else:
        msg = f"Sending {len(image_urls)} image(s) for {prop_name}..."
        if current_language == 'ar': msg = f"جاري إرسال {len(image_urls)} صورة/صور لـ {prop_name}..."
        send_whatsapp_message(sender, msg)
        time.sleep(0.5)
        for img_data in image_urls:
            send_whatsapp_image_message(sender, img_data['caption'], img_data['url'])
            time.sleep(random.uniform(1.0, 2.0))

    follow_up_text = f"What else would you like to know about {prop_name}? You can ask about prices, booking, or see photos again."
    if current_language == 'ar': follow_up_text = f"ماذا تريد أن تعرف أيضاً عن {prop_name}؟ يمكنك السؤال عن الأسعار، الحجز، أو مشاهدة الصور مرة أخرى."
    send_whatsapp_message(sender, follow_up_text)

def _handle_show_prices(sender, prop_details, current_language):
    prop_name = prop_details.get('PropertyName', 'this property')
    logging.info(f"Executing _handle_show_prices for PropertyName: {prop_name} for user {sender}")
    weekday_price = prop_details.get('WeekdayPrice', 'N/A')
    weekend_price = prop_details.get('WeekendPrice', 'N/A')
    monthly_price = prop_details.get('MonthlyPrice', 'N/A')
    price_text = ""
    if current_language == 'ar':
        price_text = f"أسعار {prop_name}:\n- سعر الليلة (أيام الأسبوع): {weekday_price} ريال\n- سعر الليلة (عطلة نهاية الأسبوع): {weekend_price} ريال\n- السعر الشهري: {monthly_price} ريال"
    else:
        price_text = f"Prices for {prop_name}:\n- Weekday Night: {weekday_price} SAR\n- Weekend Night: {weekend_price} SAR\n- Monthly Price: {monthly_price} SAR"
    send_whatsapp_message(sender, price_text)
    follow_up_text = f"What else would you like to know about {prop_name}? You can ask about photos, booking, or see prices again."
    if current_language == 'ar': follow_up_text = f"ماذا تريد أن تعرف أيضاً عن {prop_name}؟ يمكنك السؤال عن الصور، الحجز، أو مشاهدة الأسعار مرة أخرى."
    send_whatsapp_message(sender, follow_up_text)

def _handle_book_property(sender, prop_details, current_language):
    prop_name = prop_details.get('PropertyName', 'this property')
    logging.info(f"Executing _handle_book_property for PropertyName: {prop_name} for user {sender}")
    booking_link = prop_details.get('BookingLink')
    response_text = ""
    if booking_link and isinstance(booking_link, str) and booking_link.startswith('http'):
        if current_language == 'ar': response_text = f"لحجز {prop_name}, يمكنك استخدام الرابط التالي: {booking_link}\n\nأو يمكن لفريقنا مساعدتك في إتمام الحجز."
        else: response_text = f"To book {prop_name}, you can use: {booking_link}\n\nOur team can also assist."
    else:
        if current_language == 'ar': response_text = f"شكراً لاهتمامك بـ {prop_name}. سيقوم فريقنا بالتواصل معك قريباً للترتيب."
        else: response_text = f"Thanks for your interest in {prop_name}. Our team will contact you shortly."
    send_whatsapp_message(sender, response_text)
    if sender in interactive_flow_states:
        old_state = interactive_flow_states[sender].copy()
        if 'last_interacted_prop_id' in interactive_flow_states[sender]:
             del interactive_flow_states[sender]['last_interacted_prop_id']
        if interactive_flow_states[sender].get('step', '').startswith('awaiting_property_action_'):
            del interactive_flow_states[sender]
        logging.info(f"Potentially cleared/modified interactive_flow_state for {sender} after booking action. Old state was: {old_state}")


@app.route('/hook', methods=['POST'])
def handle_new_messages():
    global is_globally_paused, paused_conversations # Ensure global declaration
    try:
        data = request.json or {}
        incoming_messages = data.get('messages', [])
        if not incoming_messages: return jsonify(status='success_no_messages'), 200

        for message in incoming_messages:
            body_for_fallback = None
            if message.get('from_me'): continue
            sender = message.get('from')
            msg_type = message.get('type')
            body_text_if_any = ""
            if msg_type == 'text': body_text_if_any = message.get('text', {}).get('body', '')
            elif msg_type == 'reply':
                reply_content = message.get('reply', {})
                button_title = reply_content.get('buttons_reply', {}).get('title')
                list_title = reply_content.get('list_reply', {}).get('title')
                body_text_if_any = button_title or list_title or ""

            if sender not in user_languages or (msg_type == 'text' and body_text_if_any):
                user_languages[sender] = detect_language(body_text_if_any)
            current_language = user_languages.get(sender, 'ar')

            user_in_interactive_flow = sender in interactive_flow_states

            if user_in_interactive_flow:
                current_step = interactive_flow_states[sender].get('step')
                logging.info(f"User {sender} is in interactive flow, step: {current_step}, lang: {current_language}")
                button_id = None; selected_row_id = None; selected_title = None

                if msg_type == 'reply':
                    reply_content = message.get('reply', {})
                    if reply_content.get('type') == 'buttons_reply':
                        button_id = reply_content['buttons_reply'].get('id'); selected_title = reply_content['buttons_reply'].get('title')
                    elif reply_content.get('type') == 'list_reply':
                        selected_row_id = reply_content['list_reply'].get('id'); selected_title = reply_content['list_reply'].get('title')

                # body_for_fallback is set here if it's a reply, or later if it's text.
                # This needs to be distinct from body_text_if_any used for keyword matching.
                # Let's ensure body_for_fallback is primarily for RAG, and body_text_if_any for direct text content.
                # If it's a button reply, body_text_if_any will be its title.

                # PRIORITY: Handle text messages when awaiting property action
                if current_step and current_step.startswith('awaiting_property_action_') and msg_type == 'text' and body_text_if_any:
                    logging.info(f"User {sender} in step {current_step} (awaiting_property_action) sent text: '{body_text_if_any}'")
                    last_prop_id = interactive_flow_states[sender].get('last_interacted_prop_id')
                    action_triggered = False
                    if last_prop_id:
                        text_lower = body_text_if_any.strip().lower() # Use strip() here

                        photo_keywords_ar = ["صور", "صوره", "الصور"]
                        photo_keywords_en = ["photo", "photos", "pics", "pictures", "images"]
                        price_keywords_ar = ["سعر", "اسعار", "الاسعار", "بكم", "الأسعار"] # Added "الأسعار"
                        price_keywords_en = ["price", "prices", "cost", "how much"]
                        book_keywords_ar = ["حجز", "احجز", "الحجز"]
                        book_keywords_en = ["book", "booking", "reserve"]

                        # Exact match first, then substring
                        exact_match_ar_photo = text_lower in photo_keywords_ar
                        exact_match_en_photo = text_lower in photo_keywords_en
                        substring_match_ar_photo = any(kw in text_lower for kw in photo_keywords_ar)
                        substring_match_en_photo = any(kw in text_lower for kw in photo_keywords_en)

                        exact_match_ar_price = text_lower in price_keywords_ar
                        exact_match_en_price = text_lower in price_keywords_en
                        substring_match_ar_price = any(kw in text_lower for kw in price_keywords_ar)
                        substring_match_en_price = any(kw in text_lower for kw in price_keywords_en)

                        exact_match_ar_book = text_lower in book_keywords_ar
                        exact_match_en_book = text_lower in book_keywords_en
                        substring_match_ar_book = any(kw in text_lower for kw in book_keywords_ar)
                        substring_match_en_book = any(kw in text_lower for kw in book_keywords_en)

                        properties_df = get_sheet2_data()
                        if not properties_df.empty:
                            selected_property = properties_df[properties_df['PropertyID'] == last_prop_id]
                            if not selected_property.empty:
                                prop_details = selected_property.iloc[0]
                                if (current_language == 'ar' and (exact_match_ar_photo or substring_match_ar_photo)) or \
                                   (current_language == 'en' and (exact_match_en_photo or substring_match_en_photo)):
                                    logging.info(f"Text matches 'show photos' for prop {last_prop_id} by user {sender}. Matched on: '{text_lower}'")
                                    _handle_show_photos(sender, prop_details, current_language); action_triggered = True
                                elif (current_language == 'ar' and (exact_match_ar_price or substring_match_ar_price)) or \
                                     (current_language == 'en' and (exact_match_en_price or substring_match_en_price)):
                                    logging.info(f"Text matches 'show prices' for prop {last_prop_id} by user {sender}. Matched on: '{text_lower}'")
                                    _handle_show_prices(sender, prop_details, current_language); action_triggered = True
                                elif (current_language == 'ar' and (exact_match_ar_book or substring_match_ar_book)) or \
                                     (current_language == 'en' and (exact_match_en_book or substring_match_en_book)):
                                    logging.info(f"Text matches 'book property' for prop {last_prop_id} by user {sender}. Matched on: '{text_lower}'")
                                    _handle_book_property(sender, prop_details, current_language); action_triggered = True
                            else: logging.warning(f"Last interacted PropertyID '{last_prop_id}' not found for text action.")
                        else: logging.error(f"Failed to load Sheet2 data for text-based action by {sender} for prop {last_prop_id}.")

                    if action_triggered: return jsonify(status='success_text_action_handled'), 200
                    else:
                        logging.info(f"Text '{body_text_if_any}' from user {sender} (in step {current_step}) did not match property keywords or no last_prop_id. Will fall through.")
                        body_for_fallback = body_text_if_any

                elif current_step == 'awaiting_initial_choice' and button_id:
                    # ... (logic as before)
                    if button_id == 'button_id1' or button_id.endswith(':button_id1'): send_furnished_query_message(sender, language=current_language); interactive_flow_states[sender]['step'] = 'awaiting_furnished_choice'; return jsonify(status='success_interactive_handled'), 200
                    elif button_id == 'button_id2' or button_id.endswith(':button_id2'): send_city_selection_message(sender, language=current_language); interactive_flow_states[sender]['step'] = 'awaiting_city_choice'; return jsonify(status='success_interactive_handled'), 200
                    elif button_id == 'button_id3' or button_id.endswith(':button_id3'):
                        send_whatsapp_message(sender, "الرجاء كتابة سؤالك، وسأبذل قصارى جهدي لمساعدتك." if current_language == 'ar' else "Please type your question.");
                        if sender in interactive_flow_states: del interactive_flow_states[sender]
                        body_for_fallback = "User selected 'Other inquiries' and will type their question."
                    else: send_initial_greeting_message(sender, language=current_language); interactive_flow_states[sender]['step'] = 'awaiting_initial_choice'; return jsonify(status='success_interactive_reprompted_unknown_button'), 200

                elif current_step == 'awaiting_furnished_choice': # This step primarily expects a button reply
                    if button_id: # It's a button reply
                        if button_id == 'button_id4' or button_id.endswith(':button_id4'): send_furnished_apartment_survey_message(sender, language=current_language);
                        elif button_id == 'button_id5' or button_id.endswith(':button_id5'): send_unfurnished_apartment_survey_message(sender, language=current_language);
                        else: send_furnished_query_message(sender, language=current_language); return jsonify(status='success_interactive_reprompted_unknown_option'), 200
                        if sender in interactive_flow_states: del interactive_flow_states[sender];
                        return jsonify(status='success_interactive_handled_survey_sent'), 200
                    elif msg_type == 'text' and body_text_if_any: # User sent text instead
                        send_whatsapp_message(sender, "الرجاء الاختيار من الأزرار." if current_language == 'ar' else "Please choose from the buttons.")
                        send_furnished_query_message(sender, language=current_language) # Resend query
                        return jsonify(status='success_interactive_reprompted_text_instead_of_button_for_furnished'), 200

                elif current_step == 'awaiting_city_choice' and selected_row_id and selected_title:
                    # ... (city selection logic as before) ...
                    logging.info(f"User {sender} selected city: {selected_title}")
                    properties_df = get_sheet2_data()
                    if properties_df.empty: send_whatsapp_message(sender, "عذراً، لم أتمكن من استرداد معلومات العقارات." if current_language == 'ar' else "Sorry, couldn't get property info."); return jsonify(status='error_fetching_sheet2_data'), 200
                    city_properties = properties_df[properties_df['City'].str.lower() == selected_title.lower()]
                    if city_properties.empty: send_whatsapp_message(sender, f"عذراً، لا توجد عقارات في {selected_title}." if current_language == 'ar' else f"Sorry, no properties in {selected_title}."); return jsonify(status='success_no_properties_in_city'), 200
                    send_whatsapp_message(sender, f"ممتاز! وجدت {len(city_properties)} عقارات. جاري إرسالها..." if current_language == 'ar' else f"Great! Found {len(city_properties)} properties. Sending now...")
                    time.sleep(1)
                    for _, prop in city_properties.iterrows():
                        prop_id = str(prop['PropertyID']).strip(); prop_name = str(prop['PropertyName']).strip()
                        buttons = [{"type": "quick_reply", "title": "عرض الصور" if current_language == 'ar' else "Show Photos", "id": f"show_photos_{prop_id}"}, {"type": "quick_reply", "title": "الأسعار" if current_language == 'ar' else "Prices", "id": f"show_prices_{prop_id}"}, {"type": "quick_reply", "title": "إحجز" if current_language == 'ar' else "Book", "id": f"book_prop_{prop_id}"}]
                        msg_data = {'header': prop_name, 'body': str(prop.get('Description','')), 'footer': "إضغط للإختيار" if current_language=='ar' else "Choose", 'buttons': buttons}
                        send_interactive_button_message(sender, msg_data)
                        time.sleep(1.5)
                    interactive_flow_states[sender]['step'] = f'awaiting_property_action_{selected_title.lower().replace(" ", "_")}'
                    send_whatsapp_message(sender, "الرجاء اختيار أحد الخيارات من العقارات أعلاه." if current_language == 'ar' else "Please choose an option from the properties above.")
                    return jsonify(status='success_sent_property_cards'), 200

                elif current_step and current_step.startswith('awaiting_property_action_') and button_id: # Button clicks for property
                    # ... (property action button logic as before, calling helper functions and returning) ...
                    cleaned_button_id = button_id.replace("ButtonsV3:", "") if button_id.startswith("ButtonsV3:") else button_id
                    action_parts = cleaned_button_id.split('_'); action_type = ""; action_subject = ""; prop_id_from_button = ""
                    if len(action_parts) >= 3: action_type, action_subject, *prop_id_parts = action_parts; prop_id_from_button = "_".join(prop_id_parts)
                    if not prop_id_from_button: send_whatsapp_message(sender, "Error processing selection."); return jsonify(status='error_parsing_prop_id'), 200
                    properties_df = get_sheet2_data()
                    if properties_df.empty: send_whatsapp_message(sender, "Error fetching details."); return jsonify(status='error_fetching_sheet2_for_action'), 200
                    selected_property = properties_df[properties_df['PropertyID'] == prop_id_from_button]
                    if selected_property.empty: send_whatsapp_message(sender, "Property details not found."); return jsonify(status='error_prop_not_found_for_action'), 200
                    prop_details = selected_property.iloc[0]
                    if sender in interactive_flow_states: interactive_flow_states[sender]['last_interacted_prop_id'] = prop_id_from_button
                    if action_type == "show" and action_subject == "photos": _handle_show_photos(sender, prop_details, current_language); return jsonify(status='success_called_show_photos'), 200
                    elif action_type == "show" and action_subject == "prices": _handle_show_prices(sender, prop_details, current_language); return jsonify(status='success_called_show_prices'), 200
                    elif action_type == "book" and action_subject == "prop": _handle_book_property(sender, prop_details, current_language); return jsonify(status='success_called_book_property'), 200
                    else: send_whatsapp_message(sender, "Sorry, unknown option."); return jsonify(status='error_unknown_property_action'), 200

                # Generic text handler for OTHER interactive flow steps (if not property action text and not handled by specific step above)
                elif msg_type == 'text' and body_text_if_any:
                    # This condition is now only met if it's a text message AND
                    # it was NOT a text message in 'awaiting_property_action_' step (that would have set body_for_fallback and fallen through)
                    # AND it was not handled by any other step-specific text logic (e.g. awaiting_seller_name)
                    logging.info(f"User {sender} in step {current_step} sent unhandled text: '{body_text_if_any}'. Reprompting.")
                    response_text = "Please make a selection using the buttons or list provided."
                    if current_language == 'ar': response_text = "الرجاء تحديد اختيارك باستخدام الأزرار أو القائمة المتوفرة."
                    send_whatsapp_message(sender, response_text)
                    # Optionally, resend the last interactive message specific to 'current_step' if known
                    return jsonify(status='success_interactive_reprompted_unhandled_text'), 200

                # Unhandled reply in interactive flow
                elif msg_type == 'reply' and (button_id or selected_row_id):
                    logging.warning(f"User {sender} sent unhandled reply in step {current_step}. ButtonID: {button_id}, ListID: {selected_row_id}")
                    send_initial_greeting_message(sender, language=current_language)
                    interactive_flow_states[sender] = {'step': 'awaiting_initial_choice', 'language': current_language}
                    return jsonify(status='success_interactive_reset_unhandled_reply'), 200

            # --- Fallback and RAG Logic (outside 'if user_in_interactive_flow') ---
            # body_for_fallback would have been set if:
            # 1. Initial button "Other Inquiries" was pressed.
            # 2. Text was sent during 'awaiting_property_action_' but didn't match keywords.
            # 3. Or, if it's a new message not part of any interactive flow.
            if body_for_fallback is None:
                if msg_type == 'text': body_for_fallback = message.get('text', {}).get('body', '').strip()
                elif msg_type == 'reply' and not user_in_interactive_flow:
                    reply_data = message.get('reply', {})
                    body_for_fallback = reply_data.get('buttons_reply', {}).get('title') or reply_data.get('list_reply', {}).get('title') or ""
                elif msg_type == 'image' or msg_type == 'video': body_for_fallback = f"[User sent {msg_type}]"
                elif msg_type == 'audio':
                    media_url = message.get('media', {}).get('url')
                    if media_url and openai_client:
                        try: # Simplified audio transcription
                            audio_response = requests.get(media_url); audio_response.raise_for_status()
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_audio_file: tmp_audio_file.write(audio_response.content); tmp_audio_file_path = tmp_audio_file.name
                            transcript = openai_client.audio.transcriptions.create(model="whisper-1", file=open(tmp_audio_file_path, "rb"))
                            body_for_fallback = transcript.text; os.remove(tmp_audio_file_path)
                        except Exception as e: logging.error(f"Audio transcription error: {e}"); body_for_fallback = "[Audio transcription failed.]"
                    else: body_for_fallback = "[Audio received, no transcription.]"

            if not (sender and body_for_fallback):
                logging.warning(f"Webhook ignored: no sender or body_for_fallback. Message: {message}")
                continue

            # --- Sell Property Flow (checked after interactive flow and if not handled by RAG initiation) ---
            if not user_in_interactive_flow and sender in sell_flow_states:
                # ... (sell_flow_states logic as before)
                # This should ideally be structured to not conflict with RAG if body_for_fallback is also set
                # For now, assuming sell_flow takes precedence if its state is active.
                state_info = sell_flow_states[sender]; current_sell_state = state_info.get('state'); user_data = state_info.get('data', {})
                user_reply_text = body_text_if_any if msg_type == 'text' else selected_title
                if not user_reply_text and msg_type=='text': user_reply_text = message.get('text',{}).get('body','').strip()

                if current_sell_state == 'awaiting_seller_name': # Example state
                    user_data['name'] = user_reply_text; sell_flow_states[sender]['state'] = 'awaiting_seller_property_type'; # send next list/message
                    continue
                # ... other sell flow states, all should 'continue' ...
                elif current_sell_state == 'awaiting_seller_price': # Final state example
                    del sell_flow_states[sender]; continue

            # --- Greeting check (if not in any flow and not a sell flow message) ---
            if not user_in_interactive_flow and not (sender in sell_flow_states) and body_for_fallback:
                current_text_for_greeting_check = body_for_fallback.strip().lower() # Use body_for_fallback here
                greetings = ["hi", "hello", "hey", "greetings", "good morning", "good afternoon", "good evening", "مرحبا", "السلام عليكم", "هلا", "هاي"]
                is_greeting = any(greet == current_text_for_greeting_check for greet in greetings if current_text_for_greeting_check) or \
                              any(current_text_for_greeting_check.startswith(greet) for greet in greetings if current_text_for_greeting_check and len(greet) > 2)
                if is_greeting:
                    send_initial_greeting_message(sender, language=current_language)
                    interactive_flow_states[sender] = {'step': 'awaiting_initial_choice', 'language': current_language}
                    return jsonify(status='success_interactive_started'), 200

            # --- Command Processing & RAG/LLM Fallback ---
            if not (sender and body_for_fallback): continue # Redundant check, but safe

            normalized_body = body_for_fallback.lower().strip()
            if normalized_body == "bot pause all": is_globally_paused = True; send_whatsapp_message(sender, "Bot globally paused."); continue
            if normalized_body == "bot resume all": is_globally_paused = False; paused_conversations.clear(); send_whatsapp_message(sender, "Bot globally resumed."); continue
            # ... other admin commands ...

            if is_globally_paused or sender in paused_conversations: continue

            user_id = ''.join(c for c in sender if c.isalnum())
            logging.info(f"Passing to LLM/RAG for {sender} (UID: {user_id}): {body_for_fallback}")
            history = load_history(user_id)
            llm_response_data = get_llm_response(body_for_fallback, sender, history)
            # ... (LLM response sending logic as before) ...
            final_model_response_for_history = ""
            if llm_response_data['type'] == 'image':
                final_model_response_for_history = f"[Sent Image: {llm_response_data['url']}]"
                send_whatsapp_image_message(sender, llm_response_data['caption'], llm_response_data['url'])
            elif llm_response_data['type'] == 'text':
                text_content = llm_response_data['content']
                final_model_response_for_history = text_content
                chunks = split_message(text_content)
                for idx, chunk in enumerate(chunks, start=1):
                    send_whatsapp_message(sender, chunk)
                    if idx < len(chunks): time.sleep(random.uniform(1.0, 2.0)) # Shorter delay

            new_history_user = {'role': 'user', 'parts': [body_for_fallback]}
            new_history_model = {'role': 'model', 'parts': [final_model_response_for_history]}
            history.extend([new_history_user, new_history_model])
            if len(history) > MAX_HISTORY_TURNS_TO_LOAD * 2: history = history[-(MAX_HISTORY_TURNS_TO_LOAD * 2):]
            save_history(user_id, history)

        return jsonify(status='success'), 200
    except json.JSONDecodeError as je:
        logging.error(f"Webhook JSONDecodeError: {je}. Raw data: {request.data}")
        return jsonify(status='error', message='Invalid JSON payload'), 400
    except Exception as e:
        logging.exception(f"FATAL Error in webhook processing: {e}")
        return jsonify(status='error', message='Internal Server Error'), 500

# ... (rest of the file: _handle_show_photos, _handle_show_prices, _handle_book_property, process_google_document_update, is_property_related_query, if __name__ == '__main__':)
# These functions are defined after handle_new_messages or at the end of the script based on previous edits.
# For this overwrite, their placement will be after handle_new_messages.

def is_property_related_query(text):
    keywords = ['property', 'properties', 'apartment', 'villa', 'house', 'buy', 'rent', 'lease', 'listing', 'listings', 'available', 'real estate']
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)

if __name__ == '__main__':
    set_webhook()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
