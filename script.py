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
import dateparser

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
from property_handler import get_sheet2_data
from outreach_handler import process_outreach_campaign
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
from calendar_handler import (
    parse_user_date_input,
    get_calendar_service as get_cal_service_from_handler,
    find_or_create_calendar_by_property_id,
    check_calendar_availability,
    create_booking_event,
    DEFAULT_USER_INPUT_TIMEZONE as CAL_HANDLER_USER_TZ,
    EVENT_STORAGE_TIMEZONE as CAL_HANDLER_STORAGE_TZ
)

COMPANY_DATA_FOLDER = 'company_data'
sell_flow_states = {}
interactive_flow_states = {}
user_languages = {}

# Mapping from English city IDs (from whatsapp_utils) to Arabic names (in Google Sheet)
CITY_NAME_MAPPING = {
    "riyadh": "الرياض",
    "jeddah": "جدة",
    "dammam": "الدمام",
    "makkah": "مكة المكرمة",
    "medina": "المدينة المنورة",
    "khobar": "الخبر",
    "dhahran": "الظهران",
    "tabuk": "تبوك",
    "buraidah": "بريدة",
    "hail": "حائل"
}

SCOPES = ['https://www.googleapis.com/auth/calendar']
TARGET_DISPLAY_TIMEZONE = pytz.timezone('Asia/Dubai')
EVENT_STORAGE_TIMEZONE = pytz.timezone('America/New_York')
TIMEZONE = EVENT_STORAGE_TIMEZONE

OPERATIONAL_START_HOUR_DUBAI = 20
OPERATIONAL_END_HOUR_DUBAI = 8
DUBAI_TIMEZONE = pytz.timezone('Asia/Dubai')

load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PROPERTY_SHEET_ID = os.getenv('PROPERTY_SHEET_ID')
PROPERTY_SHEET_NAME = os.getenv('PROPERTY_SHEET_NAME', 'Properties')
LEAD_EMAIL_RECEIVER = os.getenv('LEAD_EMAIL_RECEIVER')
OWNER_WHATSAPP_NUMBER = os.getenv('OWNER_WHATSAPP_NUMBER')

is_globally_paused = False
paused_conversations = set()

PERSONA_FILE = 'persona.json'
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

def send_booking_notification_email(subject, body_text, recipient_email):
    sender_email = os.getenv('BOOKING_EMAIL_SENDER', os.getenv('LEAD_EMAIL_SENDER'))
    sender_password = os.getenv('BOOKING_EMAIL_PASSWORD', os.getenv('LEAD_EMAIL_PASSWORD'))
    smtp_server = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
    smtp_port = int(os.getenv('SMTP_PORT', 587))

    if not all([sender_email, sender_password, recipient_email]):
        logging.error("Email credentials/receiver not configured for booking notifications.")
        return False

    msg = MIMEText(body_text, _charset='utf-8')
    msg['Subject'] = subject
    msg['From'] = sender_email
    msg['To'] = recipient_email

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipient_email, msg.as_string())
        logging.info(f"Booking notification email sent successfully to {recipient_email}.")
        return True
    except Exception as e:
        logging.error(f"Error sending booking notification email: {e}", exc_info=True)
        return False

logging.info("Initializing RAG components...")
OPENAI_API_KEY_RAG = os.getenv('OPENAI_API_KEY_RAG', os.getenv('OPENAI_API_KEY'))
embeddings_rag = None; vector_store_rag = None
if OPENAI_API_KEY_RAG:
    try:
        embeddings_rag = OpenAIEmbeddings(model="text-embedding-ada-002", openai_api_key=OPENAI_API_KEY_RAG)
        vector_store_rag = initialize_vector_store()
        if 'app' in globals() and app: app.config['EMBEDDINGS'] = embeddings_rag; app.config['VECTOR_STORE'] = vector_store_rag
        if vector_store_rag and embeddings_rag: logging.info("RAG components initialized successfully.")
        else: logging.error("Failed to initialize RAG components.")
    except Exception as e: logging.error(f"Error initializing RAG components: {e}", exc_info=True)
else: logging.error("OPENAI_API_KEY_RAG not found; RAG functionality will be disabled.")

CONV_DIR = 'conversations'; os.makedirs(CONV_DIR, exist_ok=True)
MAX_HISTORY_TURNS_TO_LOAD = 6
def load_history(uid):
    path = os.path.join(CONV_DIR, f"{uid}.json");
    if not os.path.isfile(path): return []
    try:
        with open(path, encoding='utf-8') as f: data = json.load(f)
        history_messages = [item for item in data if isinstance(item, dict) and 'role' in item and 'parts' in item] if isinstance(data, list) else []
        if len(history_messages) > MAX_HISTORY_TURNS_TO_LOAD * 2: history_messages = history_messages[-(MAX_HISTORY_TURNS_TO_LOAD * 2):]
        return history_messages
    except Exception as e: logging.error(f"Error loading history for {uid}: {e}"); return []

def save_history(uid, history):
    path = os.path.join(CONV_DIR, f"{uid}.json")
    try:
        serializable_history = [msg for msg in history if isinstance(msg, dict) and 'role' in msg and 'parts' in msg]
        with open(path, 'w', encoding='utf-8') as f: json.dump(serializable_history, f, indent=2, ensure_ascii=False)
    except Exception as e: logging.error(f"Error saving history for {uid}: {e}")

def get_llm_response(text, sender_id, history_dicts=None, retries=3):
    # ... (omitted for brevity, assume unchanged)
    return {'type': 'text', 'content': "LLM response placeholder"}

def split_message(text, max_lines=25, max_chars_per_msg=1500):
    # ... (omitted for brevity, assume unchanged)
    return [text]

@app.route('/')
def home(): return "WhatsApp Bot is running!"

def detect_language(text):
    if not text: return 'en'
    arabic_chars = re.findall(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', text)
    non_space_text_len = len(text.replace(" ", ""))
    if non_space_text_len == 0 and len(arabic_chars) > 0: return 'ar'
    if non_space_text_len > 0 and (len(arabic_chars) > 2 or (len(arabic_chars) / non_space_text_len > 0.3)): return 'ar'
    return 'en'

@app.route('/webhook-google-sync', methods=['POST'])
def webhook_google_sync():
    # ... (omitted for brevity, assume unchanged)
    return jsonify(status='success', message='Document update task queued.'), 202

def _handle_show_photos(sender, prop_details, current_language):
    # ... (omitted for brevity, assume unchanged)
    pass

def _handle_show_prices(sender, prop_details, current_language):
    # ... (omitted for brevity, assume unchanged)
    pass

def _handle_book_property(sender, prop_details, current_language):
    prop_id = str(prop_details.get('PropertyID'))
    prop_name = str(prop_details.get('PropertyName', 'this property'))
    logging.info(f"Initiating booking flow for PropertyID: {prop_id} ({prop_name}), User: {sender}, Language: {current_language}")
    if sender not in interactive_flow_states:
        interactive_flow_states[sender] = {}
    interactive_flow_states[sender]['step'] = 'awaiting_booking_date'
    interactive_flow_states[sender]['booking_property_id'] = prop_id
    interactive_flow_states[sender]['booking_property_name'] = prop_name
    interactive_flow_states[sender]['booking_language'] = current_language
    prompt_message = f"الرجاء إدخال تاريخ تسجيل الدخول المطلوب لـ \"{prop_name}\" (مثال: YYYY-MM-DD أو 'غداً'):" if current_language == 'ar' else f"Please enter the desired check-in date for \"{prop_name}\" (e.g., YYYY-MM-DD or 'tomorrow'):"
    send_whatsapp_message(sender, prompt_message)
    logging.info(f"Sent prompt for booking date to {sender} for property {prop_id}. State: {interactive_flow_states[sender]}")

@app.route('/hook', methods=['POST'])
def handle_new_messages():
    global is_globally_paused, paused_conversations
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

                if current_step and current_step.startswith('awaiting_property_action_') and msg_type == 'text' and body_text_if_any:
                    # ... (logic omitted for brevity)
                    pass

                elif current_step == 'awaiting_initial_choice' and button_id:
                    # ... (logic omitted for brevity)
                    pass

                elif current_step == 'awaiting_furnished_choice':
                    # ... (logic omitted for brevity)
                    pass

                elif current_step == 'awaiting_booking_date' and msg_type == 'text' and body_text_if_any:
                    # ... (logic omitted for brevity)
                    pass

                elif current_step == 'awaiting_booking_days' and msg_type == 'text' and body_text_if_any:
                    # ... (logic omitted for brevity)
                    pass

                if interactive_flow_states[sender].get('step') == 'awaiting_calendar_check':
                    # ... (logic omitted for brevity)
                    pass

                elif current_step == 'awaiting_client_name_confirmation' and msg_type == 'text' and body_text_if_any:
                    # ... (logic omitted for brevity)
                    pass

                if interactive_flow_states[sender].get('step') == 'awaiting_booking_creation':
                    # ... (logic omitted for brevity)
                    pass

                elif current_step == 'awaiting_city_choice' and selected_row_id and selected_title:
                    logging.info(f"User {sender} selected city via list. ID: '{selected_row_id}', Title: '{selected_title}'")
                    properties_df = get_sheet2_data()
                    if properties_df.empty:
                        send_whatsapp_message(sender, "عذراً، لم أتمكن من استرداد معلومات العقارات." if current_language == 'ar' else "Sorry, couldn't get property info.")
                        return jsonify(status='error_fetching_sheet2_data'), 200

                    # --- CITY NAME MAPPING LOGIC ---
                    city_to_filter = CITY_NAME_MAPPING.get(selected_row_id.lower(), selected_title)
                    logging.info(f"Filtering properties for city: '{city_to_filter}' (Original selection ID: '{selected_row_id}')")

                    # Ensure case-insensitive comparison with Arabic text if needed by normalizing both
                    # For pandas, this is often handled well, but explicit lower/strip can help.
                    # Assuming sheet data is clean. str.lower() might not be ideal for Arabic, but direct comparison should work.
                    city_properties = properties_df[properties_df['City'] == city_to_filter]

                    if city_properties.empty:
                        send_whatsapp_message(sender, f"عذراً، لا توجد عقارات في {selected_title}." if current_language == 'ar' else f"Sorry, no properties in {selected_title}.")
                        return jsonify(status='success_no_properties_in_city'), 200

                    send_whatsapp_message(sender, f"ممتاز! وجدت {len(city_properties)} عقارات. جاري إرسالها..." if current_language == 'ar' else f"Great! Found {len(city_properties)} properties. Sending now...")
                    time.sleep(1)
                    for _, prop in city_properties.iterrows():
                        prop_id_card = str(prop['PropertyID']).strip(); prop_name_card = str(prop['PropertyName']).strip()
                        buttons = [{"type": "quick_reply", "title": "عرض الصور" if current_language == 'ar' else "Show Photos", "id": f"show_photos_{prop_id_card}"},
                                   {"type": "quick_reply", "title": "الأسعار" if current_language == 'ar' else "Prices", "id": f"show_prices_{prop_id_card}"},
                                   {"type": "quick_reply", "title": "إحجز" if current_language == 'ar' else "Book", "id": f"book_prop_{prop_id_card}"}]
                        msg_data = {'header': prop_name_card, 'body': str(prop.get('Description','')), 'footer': "إضغط للإختيار" if current_language=='ar' else "Choose", 'buttons': buttons}
                        send_interactive_button_message(sender, msg_data)
                        time.sleep(1.5)
                    interactive_flow_states[sender]['step'] = f'awaiting_property_action_{selected_row_id}'
                    interactive_flow_states[sender]['selected_city_id'] = selected_row_id
                    send_whatsapp_message(sender, "الرجاء اختيار أحد الخيارات من العقارات أعلاه." if current_language == 'ar' else "Please choose an option from the properties above.")
                    return jsonify(status='success_sent_property_cards'), 200

                elif current_step and current_step.startswith('awaiting_property_action_') and button_id:
                    # ... (logic omitted for brevity)
                    pass

                elif msg_type == 'text' and body_text_if_any:
                    # ... (logic omitted for brevity)
                    pass

                elif msg_type == 'reply' and (button_id or selected_row_id):
                    # ... (logic omitted for brevity)
                    pass

            # ... (Fallback and RAG logic omitted for brevity) ...

        return jsonify(status='success'), 200
    except Exception as e:
        logging.exception(f"FATAL Error in webhook processing: {e}")
        return jsonify(status='error', message='Internal Server Error'), 500

def is_property_related_query(text):
    keywords = ['property', 'properties', 'apartment', 'villa', 'house', 'buy', 'rent', 'lease', 'listing', 'listings', 'available', 'real estate']
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)

if __name__ == '__main__':
    set_webhook()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
