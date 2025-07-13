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
    send_interactive_button_message,
    CITY_TRANSLATIONS
)
from calendar_handler import (
    parse_user_date_input,
    get_calendar_service as get_cal_service_from_handler,
    find_or_create_calendar_by_property_id,
    check_calendar_availability,
    create_booking_event, # Added import
    DEFAULT_USER_INPUT_TIMEZONE as CAL_HANDLER_USER_TZ,
    EVENT_STORAGE_TIMEZONE as CAL_HANDLER_STORAGE_TZ
)

COMPANY_DATA_FOLDER = 'company_data'
sell_flow_states = {}
interactive_flow_states = {}
user_languages = {}

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

SCOPES = ['https://www.googleapis.com/auth/calendar']
TARGET_DISPLAY_TIMEZONE = pytz.timezone('Asia/Dubai')
EVENT_STORAGE_TIMEZONE = pytz.timezone('America/New_York') # Used by old calendar functions
TIMEZONE = EVENT_STORAGE_TIMEZONE # Used by old calendar functions

OPERATIONAL_START_HOUR_DUBAI = 20
OPERATIONAL_END_HOUR_DUBAI = 8
DUBAI_TIMEZONE = pytz.timezone('Asia/Dubai')

load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PROPERTY_SHEET_ID = os.getenv('PROPERTY_SHEET_ID')
PROPERTY_SHEET_NAME = os.getenv('PROPERTY_SHEET_NAME', 'Properties')
LEAD_EMAIL_RECEIVER = os.getenv('LEAD_EMAIL_RECEIVER')
OWNER_WHATSAPP_NUMBER = os.getenv('OWNER_WHATSAPP_NUMBER') # For owner notifications

is_globally_paused = False
paused_conversations = set()

PERSONA_FILE = 'persona.json'
PERSONA_NAME = "Barq (برق)"
BASE_PROMPT_TEMPLATE = (
    "You are a helpful and friendly assistant from X-BnB. "
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


# Old calendar service init - may conflict or be redundant with calendar_handler's one
# def get_calendar_service():
#     # ...
#     pass

# New function for sending booking notification emails
def send_booking_notification_email(subject, body_text, recipient_email):
    sender_email = os.getenv('BOOKING_EMAIL_SENDER', os.getenv('LEAD_EMAIL_SENDER')) # Fallback to LEAD_EMAIL_SENDER if specific not set
    sender_password = os.getenv('BOOKING_EMAIL_PASSWORD', os.getenv('LEAD_EMAIL_PASSWORD'))
    smtp_server = os.getenv('SMTP_SERVER', 'smtp.gmail.com') # Generic SMTP server
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

# ... (other functions like detect_scheduling_intent, extract_datetime_with_ai, create_calendar_event (old), check_availability (old), scan_company_data_folder, etc. remain as they were) ...
# For brevity, I'm omitting the unchanged functions that were previously listed.
# The key changes are within handle_new_messages and the new email function.

def get_calendar_service(): # This is the old one from script.py, might need to ensure calendar_handler's is used for new booking flow
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

# ... (scan_company_data_folder, RAG init, old CALENDAR_SERVICE init, send_appointment_request_email, send_property_lead_email, load_history, save_history, extract_appointment_details_for_email, get_llm_response, handle_appointment_scheduling, split_message, home, detect_language, extract_sheet_id_from_url, webhook_google_sync)
# These functions are assumed to be present and mostly unchanged as per previous context.

logging.info("Initializing RAG components...")
OPENAI_API_KEY_RAG = os.getenv('OPENAI_API_KEY_RAG', os.getenv('OPENAI_API_KEY'))
embeddings_rag = None; vector_store_rag = None
if OPENAI_API_KEY_RAG:
    try:
        embeddings_rag = OpenAIEmbeddings(model="text-embedding-ada-002", openai_api_key=OPENAI_API_KEY_RAG)
        vector_store_rag = initialize_vector_store()
        if 'app' in globals() and app: app.config['EMBEDDINGS'] = embeddings_rag; app.config['VECTOR_STORE'] = vector_store_rag
        if vector_store_rag and embeddings_rag: logging.info("RAG components initialized successfully."); # scan_company_data_folder(vector_store_rag, embeddings_rag) # Call if needed
        else: logging.error("Failed to initialize RAG components.")
    except Exception as e: logging.error(f"Error initializing RAG components: {e}", exc_info=True)
else: logging.error("OPENAI_API_KEY_RAG not found; RAG functionality will be disabled.")

# CALENDAR_SERVICE = get_calendar_service() # This is the old one. New flow uses get_cal_service_from_handler()
# if CALENDAR_SERVICE: logging.info("Google Calendar service initialized successfully.")
# else: logging.warning("Google Calendar service could not be initialized.")


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
    if not AI_MODEL: return {'type': 'text', 'content': "AI Model not configured."}
    analysis_prompt = f"Analyze: '{text}'. JSON: intent ('property_search'/'general_question'), filters (dict/null). Filters: Price_AED, Bedrooms, emirate, city, area, developer, Title."
    try:
        analysis_response = AI_MODEL.invoke([HumanMessage(content=analysis_prompt)]); response_text = analysis_response.content.strip()
        if response_text.startswith('```json'): response_text = response_text[len('```json'):].strip()
        if response_text.endswith('```'): response_text = response_text[:-len('```')].strip()
        analysis_json = json.loads(response_text); intent = analysis_json.get("intent"); filters = analysis_json.get("filters")
    except Exception as e: logging.error(f"LLM Query analysis failed: {e}"); intent = "general_question"; filters = None
    context_str = ""
    if (intent == "property_search" or is_property_related_query(text)) and PROPERTY_SHEET_ID:
        all_properties_df = property_handler.get_sheet_data() # This uses the old sheet
        if not all_properties_df.empty:
            filtered_df = property_handler.filter_properties(all_properties_df, filters) if filters else all_properties_df
            if not filtered_df.empty:
                context_str = "Relevant Information Found:\n"
                for _, prop in filtered_df.head(3).iterrows(): # Limit to 3 for brevity
                    context_str += f"Title: {prop.get('Title', 'N/A')}, Location: {prop.get('area', '')}, {prop.get('city', '')}, Price: {prop.get('Price_AED', 'N/A')} AED\n---\n"
            else: context_str = "Relevant Information Found:\nNo properties found matching your criteria from our primary list."
        else: context_str = "Relevant Information Found:\nUnable to access primary property listings."
    else:
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

def split_message(text, max_lines=25, max_chars_per_msg=1500): # WhatsApp limits are higher
    lines = text.split('\n')
    chunks = []
    current_chunk = ""
    for line in lines:
        if len(current_chunk) + len(line) + 1 > max_chars_per_msg or current_chunk.count('\n') >= max_lines:
            chunks.append(current_chunk.strip())
            current_chunk = ""
        current_chunk += line + "\n"
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    return chunks if chunks else [text]


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
    # ... (Existing logic) ...
    return jsonify(status='success', message='Document update task queued.'), 202


def _handle_show_photos(sender, prop_details, current_language):
    if current_language == 'en':
        prop_name = prop_details.get('PropertyName_en', prop_details.get('PropertyName', 'this property'))
    else:
        prop_name = prop_details.get('PropertyName', 'this property')

    logging.info(f"Executing _handle_show_photos for PropertyName: {prop_name} for user {sender}")
    image_urls = []
    for i in range(1, 11):
        img_col = f'ImageURL{i}'
        url_val = prop_details.get(img_col)

        # Caption logic based on language
        if current_language == 'en':
            caption_col = f'ImageCaption{i}_en'
            caption_val = prop_details.get(caption_col)
            if not caption_val or str(caption_val).strip() == "":
                caption_val = f"{prop_name} - Image {i}"
        else:
            caption_col = f'ImageCaption{i}'
            caption_val = prop_details.get(caption_col)
            if not caption_val or str(caption_val).strip() == "":
                caption_val = f"{prop_name} - صورة {i}"

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
        time.sleep(0.5) # Brief pause before sending images
        for img_data in image_urls:
            send_whatsapp_image_message(sender, img_data['caption'], img_data['url'])
            time.sleep(random.uniform(1.0, 2.0)) # Stagger image sending

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
    prop_id = str(prop_details.get('PropertyID'))
    prop_name = str(prop_details.get('PropertyName', 'this property'))
    logging.info(f"Initiating booking flow for PropertyID: {prop_id} ({prop_name}), User: {sender}, Language: {current_language}")

    if sender not in interactive_flow_states:
        interactive_flow_states[sender] = {}

    interactive_flow_states[sender]['step'] = 'awaiting_booking_date'
    interactive_flow_states[sender]['booking_property_id'] = prop_id
    interactive_flow_states[sender]['booking_property_name'] = prop_name
    interactive_flow_states[sender]['booking_language'] = current_language

    prompt_message = ""
    if current_language == 'ar':
        prompt_message = f"الرجاء إدخال تاريخ تسجيل الدخول المطلوب لـ \"{prop_name}\" (مثال: YYYY-MM-DD أو 'غداً'):"
    else:
        prompt_message = f"Please enter the desired check-in date for \"{prop_name}\" (e.g., YYYY-MM-DD or 'tomorrow'):"

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
            sender = message.get('from') # This is the user's WhatsApp ID
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
                    # ... (text handling during property action as before)
                    pass # Assuming this logic is correct from previous steps

                elif current_step == 'awaiting_initial_choice':
                    # Keywords for each option
                    rent_keywords_ar = ["استاجر", "أستاجر", "استئجار", "إستئجار", "اجار", "إيجار", "ابي استاجر", "ابي أستأجر"]
                    rent_keywords_en = ["rent", "lease", "i want to rent"]
                    operate_keywords_ar = ["اشغلها", "أشغلها", "تشغيل", "امل", "أملك"]
                    operate_keywords_en = ["operate", "i own", "run my apartment"]
                    other_keywords_ar = ["اخرى", "أخرى", "استفسار", "سؤال"]
                    other_keywords_en = ["other", "inquiries", "question", "query"]

                    # Combine all keywords for the current language
                    rent_keywords = rent_keywords_ar if current_language == 'ar' else rent_keywords_en
                    operate_keywords = operate_keywords_ar if current_language == 'ar' else operate_keywords_en
                    other_keywords = other_keywords_ar if current_language == 'ar' else other_keywords_en

                    action = None
                    if button_id:
                        if button_id == 'button_id1' or button_id.endswith(':button_id1'): action = 'operate'
                        elif button_id == 'button_id2' or button_id.endswith(':button_id2'): action = 'rent'
                        elif button_id == 'button_id3' or button_id.endswith(':button_id3'): action = 'other'
                    elif msg_type == 'text' and body_text_if_any:
                        text_lower = body_text_if_any.lower().strip()
                        if any(keyword in text_lower for keyword in operate_keywords): action = 'operate'
                        elif any(keyword in text_lower for keyword in rent_keywords): action = 'rent'
                        elif any(keyword in text_lower for keyword in other_keywords): action = 'other'

                    if action == 'operate':
                        send_furnished_query_message(sender, language=current_language)
                        interactive_flow_states[sender]['step'] = 'awaiting_furnished_choice'
                        return jsonify(status='success_interactive_handled'), 200
                    elif action == 'rent':
                        send_city_selection_message(sender, language=current_language)
                        interactive_flow_states[sender]['step'] = 'awaiting_city_choice'
                        return jsonify(status='success_interactive_handled'), 200
                    elif action == 'other':
                        send_whatsapp_message(sender, "الرجاء كتابة سؤالك، وسأبذل قصارى جهدي لمساعدتك." if current_language == 'ar' else "Please type your question.");
                        if sender in interactive_flow_states: del interactive_flow_states[sender]
                        body_for_fallback = "User selected 'Other inquiries' and will type their question."
                    else: # If no button, no text match, or unknown button ID
                        send_initial_greeting_message(sender, language=current_language)
                        interactive_flow_states[sender]['step'] = 'awaiting_initial_choice'
                        return jsonify(status='success_interactive_reprompted_unknown_input'), 200

                elif current_step == 'awaiting_furnished_choice':
                    if button_id:
                        if button_id == 'button_id4' or button_id.endswith(':button_id4'): send_furnished_apartment_survey_message(sender, language=current_language);
                        elif button_id == 'button_id5' or button_id.endswith(':button_id5'): send_unfurnished_apartment_survey_message(sender, language=current_language);
                        else: send_furnished_query_message(sender, language=current_language); return jsonify(status='success_interactive_reprompted_unknown_option'), 200
                        if sender in interactive_flow_states: del interactive_flow_states[sender];
                        return jsonify(status='success_interactive_handled_survey_sent'), 200
                    elif msg_type == 'text' and body_text_if_any:
                        send_whatsapp_message(sender, "الرجاء الاختيار من الأزرار." if current_language == 'ar' else "Please choose from the buttons.")
                        send_furnished_query_message(sender, language=current_language)
                        return jsonify(status='success_interactive_reprompted_text_instead_of_button_for_furnished'), 200

                elif current_step == 'awaiting_booking_date' and msg_type == 'text' and body_text_if_any:
                    logging.info(f"User {sender} (state: awaiting_booking_date) sent date: {body_text_if_any}")
                    booking_language = interactive_flow_states[sender].get('booking_language', current_language)
                    prop_name = interactive_flow_states[sender].get('booking_property_name', 'the property')
                    parsed_date = parse_user_date_input(body_text_if_any, user_timezone=CAL_HANDLER_USER_TZ)

                    if parsed_date:
                        today_user_tz = datetime.now(CAL_HANDLER_USER_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
                        if parsed_date < today_user_tz:
                            error_msg = "التاريخ المدخل في الماضي. الرجاء إدخال تاريخ اليوم أو تاريخ مستقبلي." if booking_language == 'ar' else "The date entered is in the past. Please enter today's date or a future date."
                            send_whatsapp_message(sender, error_msg)
                            prompt_message = f"الرجاء إدخال تاريخ تسجيل الدخول مرة أخرى لـ \"{prop_name}\" (مثال: YYYY-MM-DD أو 'غداً'):" if booking_language == 'ar' else f"Please re-enter the check-in date for \"{prop_name}\" (e.g., YYYY-MM-DD or 'tomorrow'):"
                            send_whatsapp_message(sender, prompt_message)
                            return jsonify(status='success_booking_reprompted_past_date'), 200

                        interactive_flow_states[sender]['booking_start_date_str'] = parsed_date.strftime('%Y-%m-%d')
                        interactive_flow_states[sender]['step'] = 'awaiting_booking_days'
                        prompt_message = "ممتاز! كم عدد الليالي التي ترغب في الإقامة بها؟" if booking_language == 'ar' else "Great! For how many nights would you like to stay?"
                        send_whatsapp_message(sender, prompt_message)
                        logging.info(f"Stored date {parsed_date.strftime('%Y-%m-%d')} for {sender}, now awaiting_booking_days.")
                    else:
                        error_msg = "لم أتمكن من فهم التاريخ. الرجاء إدخاله بصيغة YYYY-MM-DD أو كلمة مثل 'اليوم' أو 'غداً'." if booking_language == 'ar' else "I couldn't understand the date. Please enter it in YYYY-MM-DD format or a word like 'today' or 'tomorrow'."
                        send_whatsapp_message(sender, error_msg)
                        prompt_message = f"الرجاء إدخال تاريخ تسجيل الدخول مرة أخرى لـ \"{prop_name}\" (مثال: YYYY-MM-DD أو 'غداً'):" if booking_language == 'ar' else f"Please re-enter the check-in date for \"{prop_name}\" (e.g., YYYY-MM-DD or 'tomorrow'):"
                        send_whatsapp_message(sender, prompt_message)
                    return jsonify(status='success_booking_date_processed'), 200

                elif current_step == 'awaiting_booking_days' and msg_type == 'text' and body_text_if_any:
                    logging.info(f"User {sender} (state: awaiting_booking_days) sent num_days: {body_text_if_any}")
                    booking_language = interactive_flow_states[sender].get('booking_language', current_language)
                    try:
                        num_days = int(body_text_if_any)
                        if num_days <= 0: raise ValueError("Number of days must be positive.")
                        interactive_flow_states[sender]['booking_num_days'] = num_days
                        interactive_flow_states[sender]['step'] = 'awaiting_calendar_check'
                        prop_name = interactive_flow_states[sender].get('booking_property_name', 'the property')
                        start_date_str = interactive_flow_states[sender].get('booking_start_date_str', 'N/A')
                        ack_message = f"حسناً، تم تسجيل طلبك لحجز \"{prop_name}\" ابتداءً من تاريخ {start_date_str} لمدة {num_days} ليلة/ليالٍ. سأقوم الآن بالتحقق من التوفر..." if booking_language == 'ar' else f"Okay, noted your request for \"{prop_name}\" starting {start_date_str} for {num_days} night(s). I will now check availability..."
                        send_whatsapp_message(sender, ack_message)
                        logging.info(f"Stored num_days {num_days} for {sender}. All info for booking collected.")
                        pass # Fall through to awaiting_calendar_check
                    except ValueError:
                        error_msg = "الرجاء إدخال عدد صحيح موجب لعدد الليالي." if booking_language == 'ar' else "Please enter a valid positive number for the nights."
                        send_whatsapp_message(sender, error_msg)
                        prompt_message = "كم عدد الليالي التي ترغب في الإقامة بها؟" if booking_language == 'ar' else "For how many nights would you like to stay?"
                        send_whatsapp_message(sender, prompt_message)
                        return jsonify(status='success_booking_reprompted_invalid_days'), 200

                if interactive_flow_states[sender].get('step') == 'awaiting_calendar_check':
                    logging.info(f"User {sender} (state: awaiting_calendar_check). Proceeding with calendar operations.")
                    state_data = interactive_flow_states[sender]
                    prop_id = state_data['booking_property_id']; prop_name = state_data['booking_property_name']
                    start_date_str = state_data['booking_start_date_str']; num_days = state_data['booking_num_days']
                    booking_lang = state_data.get('booking_language', current_language)
                    cal_service = get_cal_service_from_handler()
                    if not cal_service:
                        err_msg = "عذراً، هناك مشكلة فنية في الوصول إلى نظام الحجوزات حالياً." if booking_lang == 'ar' else "Sorry, there's a technical issue accessing the booking system."
                        send_whatsapp_message(sender, err_msg); del interactive_flow_states[sender]
                        return jsonify(status='error_calendar_service_unavailable'), 200
                    target_calendar_id = find_or_create_calendar_by_property_id(cal_service, prop_id)
                    if not target_calendar_id:
                        err_msg = "عذراً، لم نتمكن من العثور على أو إنشاء تقويم الحجوزات لهذه الشقة." if booking_lang == 'ar' else "Sorry, we couldn't find or create the booking calendar for this apartment."
                        send_whatsapp_message(sender, err_msg); del interactive_flow_states[sender]
                        return jsonify(status='error_calendar_find_create_failed'), 200
                    state_data['booking_calendar_id'] = target_calendar_id
                    start_date_obj_user_tz = parse_user_date_input(start_date_str, user_timezone=CAL_HANDLER_USER_TZ)
                    check_in_time_dt = dateparser.parse("2:00 PM").time()
                    check_out_time_dt = dateparser.parse("11:00 AM").time()
                    event_start_dt_user_tz = CAL_HANDLER_USER_TZ.localize(datetime.combine(start_date_obj_user_tz.date(), check_in_time_dt))
                    checkout_date_user_tz = start_date_obj_user_tz.date() + timedelta(days=num_days)
                    event_end_dt_user_tz = CAL_HANDLER_USER_TZ.localize(datetime.combine(checkout_date_user_tz, check_out_time_dt))
                    event_start_dt_utc = event_start_dt_user_tz.astimezone(pytz.utc)
                    event_end_dt_utc = event_end_dt_user_tz.astimezone(pytz.utc)
                    available = check_calendar_availability(cal_service, target_calendar_id, event_start_dt_utc, event_end_dt_utc)
                    if available:
                        state_data['step'] = 'awaiting_client_name_confirmation'
                        formatted_start_date = start_date_obj_user_tz.strftime("%B %d, %Y") if booking_lang == 'en' else start_date_obj_user_tz.strftime("%Y-%m-%d")
                        msg_body = f"أخبار رائعة! \"{prop_name}\" متاح من تاريخ {formatted_start_date} لمدة {num_days} ليلة/ليالٍ. الرجاء تزويدنا باسمك الكامل لإتمام الحجز:" if booking_lang == 'ar' else f"Great news! \"{prop_name}\" is available from {formatted_start_date} for {num_days} night(s). Please provide your full name to complete the booking:"
                        send_whatsapp_message(sender, msg_body)
                        logging.info(f"Property {prop_id} available. Awaiting client name.")
                    else:
                        msg_body = f"للأسف، \"{prop_name}\" غير متاح للتواريخ المختارة. هل ترغب في تجربة تواريخ أخرى؟" if booking_lang == 'ar' else f"Unfortunately, \"{prop_name}\" is not available for the selected dates. Would you like to try different dates?"
                        send_whatsapp_message(sender, msg_body)
                        del interactive_flow_states[sender] # Reset flow
                        # Or send back to city selection / property selection
                    return jsonify(status='success_calendar_check_done'), 200

                elif current_step == 'awaiting_client_name_confirmation' and msg_type == 'text' and body_text_if_any:
                    logging.info(f"User {sender} (state: awaiting_client_name_confirmation) sent name: {body_text_if_any}")
                    client_name = body_text_if_any.strip()
                    booking_lang = interactive_flow_states[sender].get('booking_language', current_language)
                    if not client_name:
                        err_msg = "الرجاء إدخال اسم صحيح." if booking_lang == 'ar' else "Please enter a valid name."
                        send_whatsapp_message(sender, err_msg) # Re-prompt implicitly by not changing state
                        return jsonify(status='success_booking_reprompted_empty_name'), 200
                    interactive_flow_states[sender]['booking_client_name'] = client_name
                    interactive_flow_states[sender]['step'] = 'awaiting_booking_creation'
                    ack_msg = f"شكراً لك، {client_name}. جاري الآن تأكيد حجزك..." if booking_lang == 'ar' else f"Thank you, {client_name}. I am now confirming your booking..."
                    send_whatsapp_message(sender, ack_msg)
                    logging.info(f"Client name '{client_name}' received for {sender}.")
                    pass # Fall through to awaiting_booking_creation

                if interactive_flow_states[sender].get('step') == 'awaiting_booking_creation':
                    logging.info(f"User {sender} (state: awaiting_booking_creation). Proceeding with event creation.")
                    state_data = interactive_flow_states[sender]
                    cal_service = get_cal_service_from_handler() # Should still be available
                    if not cal_service: # Should not happen if previous step worked, but good check
                        err_msg = "حدث خطأ في نظام الحجز. يرجى المحاولة مرة أخرى." if state_data.get('booking_language') == 'ar' else "A booking system error occurred. Please try again."
                        send_whatsapp_message(sender, err_msg); del interactive_flow_states[sender]
                        return jsonify(status='error_calendar_service_gone'), 200

                    prop_id = state_data['booking_property_id']
                    prop_name = state_data['booking_property_name']
                    calendar_id = state_data['booking_calendar_id']
                    start_date_obj = parse_user_date_input(state_data['booking_start_date_str'], user_timezone=CAL_HANDLER_USER_TZ)
                    num_days = state_data['booking_num_days']
                    client_name = state_data['booking_client_name']
                    client_phone = sender # Basic phone, might need cleaning/formatting
                    booking_lang = state_data.get('booking_language', current_language)

                    # Default check-in/out times from original request and calendar_handler
                    check_in_time_setting = "2 PM"
                    check_out_time_setting = "11 AM" # As per calendar_handler's create_booking_event default

                    created_event = create_booking_event(
                        cal_service, calendar_id, prop_id,
                        start_date_obj, # This is the start DATE in user's local timezone
                        num_days, client_name, client_phone,
                        check_in_time_str=check_in_time_setting,
                        check_out_time_str=check_out_time_setting
                    )

                    if created_event:
                        # Client Confirmation
                        # Format start_date_obj for user message
                        formatted_start_date_msg = start_date_obj.strftime("%B %d, %Y") if booking_lang == 'en' else start_date_obj.strftime("%Y-%m-%d")
                        confirm_msg = f"تم تأكيد حجزك لـ \"{prop_name}\" من تاريخ {formatted_start_date_msg} لمدة {num_days} ليلة/ليالٍ! تسجيل الدخول الساعة 2 مساءً. نتطلع لاستضافتك، {client_name}!" if booking_lang == 'ar' \
                                      else f"Your booking for \"{prop_name}\" from {formatted_start_date_msg} for {num_days} night(s) is confirmed! Check-in is at 2 PM. We look forward to hosting you, {client_name}!"
                        send_whatsapp_message(sender, confirm_msg)

                        # Owner & Email Notification Details
                        booking_details_text = (
                            f"New Booking Notification:\n"
                            f"Property ID: {prop_id} ({prop_name})\n"
                            f"Client Name: {client_name}\n"
                            f"Client Phone: {client_phone}\n"
                            f"Check-in Date: {start_date_obj.strftime('%Y-%m-%d')} (at {check_in_time_setting})\n"
                            f"Number of Nights: {num_days}\n"
                            f"Booking ID (Calendar Event): {created_event.get('id')}"
                        )

                        # Owner WhatsApp Notification
                        if OWNER_WHATSAPP_NUMBER:
                            send_whatsapp_message(OWNER_WHATSAPP_NUMBER, booking_details_text)
                            logging.info(f"Sent booking notification to owner {OWNER_WHATSAPP_NUMBER}")
                        else:
                            logging.warning("OWNER_WHATSAPP_NUMBER not set. Cannot send owner WhatsApp notification.")

                        # Email Notification
                        if LEAD_EMAIL_RECEIVER:
                            email_subject = f"New Booking Confirmed: {prop_name} for {client_name}"
                            send_booking_notification_email(email_subject, booking_details_text, LEAD_EMAIL_RECEIVER)
                        else:
                            logging.warning("LEAD_EMAIL_RECEIVER not set. Cannot send booking email notification.")

                        del interactive_flow_states[sender] # Clear flow state
                        logging.info(f"Booking for {prop_id} by {client_name} successful. Event ID: {created_event.get('id')}")
                    else:
                        err_msg = "واجهتنا مشكلة أثناء محاولة تأكيد حجزك لـ \"{prop_name}\". الرجاء التواصل مع الدعم أو المحاولة مرة أخرى قريباً." if booking_lang == 'ar' \
                                  else f"We encountered an issue while trying to finalize your booking for \"{prop_name}\". Please contact support or try again shortly."
                        send_whatsapp_message(sender, err_msg)
                        logging.error(f"Failed to create calendar event for {prop_id} for user {sender}.")
                        del interactive_flow_states[sender] # Clear flow state
                    return jsonify(status='success_booking_creation_attempted'), 200


                elif current_step == 'awaiting_city_choice' and selected_row_id and selected_title:
                    logging.info(f"User {sender} selected city: {selected_title}")
                    properties_df = get_sheet2_data()
                    if properties_df.empty: send_whatsapp_message(sender, "عذراً، لم أتمكن من استرداد معلومات العقارات." if current_language == 'ar' else "Sorry, couldn't get property info."); return jsonify(status='error_fetching_sheet2_data'), 200

                    # Translate city name if the user is interacting in English
                    search_city = selected_title
                    if current_language == 'en':
                        search_city = CITY_TRANSLATIONS.get(selected_title, selected_title)
                        logging.info(f"Language is English, translating '{selected_title}' to '{search_city}' for search.")

                    # Now filter using the possibly translated city name
                    city_properties = properties_df[properties_df['City'] == search_city]

                    if city_properties.empty:
                        # Display the original English name in the error message for clarity
                        error_message = f"عذراً، لا توجد عقارات في {selected_title}." if current_language == 'ar' else f"Sorry, no properties found in {selected_title}."
                        send_whatsapp_message(sender, error_message)
                        # Optional: Reset the flow or reprompt
                        # send_city_selection_message(sender, language=current_language)
                        # interactive_flow_states[sender]['step'] = 'awaiting_city_choice'
                        return jsonify(status='success_no_properties_in_city'), 200

                    send_whatsapp_message(sender, f"ممتاز! وجدت {len(city_properties)} عقارات. جاري إرسالها..." if current_language == 'ar' else f"Great! Found {len(city_properties)} properties. Sending now...")
                    time.sleep(1)
                    for _, prop in city_properties.iterrows():
                        prop_id_card = str(prop['PropertyID']).strip()
                        if current_language == 'en':
                            prop_name_card = str(prop.get('PropertyName_en') or prop['PropertyName']).strip()
                            prop_description = str(prop.get('Description_en') or prop.get('Description', '')).strip()
                        else:
                            prop_name_card = str(prop['PropertyName']).strip()
                            prop_description = str(prop.get('Description', '')).strip()

                        buttons = [{"type": "quick_reply", "title": "عرض الصور" if current_language == 'ar' else "Show Photos", "id": f"show_photos_{prop_id_card}"},
                                   {"type": "quick_reply", "title": "الأسعار" if current_language == 'ar' else "Prices", "id": f"show_prices_{prop_id_card}"},
                                   {"type": "quick_reply", "title": "إحجز" if current_language == 'ar' else "Book", "id": f"book_prop_{prop_id_card}"}]
                        msg_data = {'header': prop_name_card, 'body': prop_description, 'footer': "إضغط للإختيار" if current_language=='ar' else "Choose", 'buttons': buttons}
                        send_interactive_button_message(sender, msg_data)
                        time.sleep(1.5) # Stagger messages
                    interactive_flow_states[sender]['step'] = f'awaiting_property_action_{selected_title.lower().replace(" ", "_")}'
                    interactive_flow_states[sender]['selected_city_id'] = selected_title.lower().replace(" ", "_") # Store for potential reset
                    send_whatsapp_message(sender, "الرجاء اختيار أحد الخيارات من العقارات أعلاه." if current_language == 'ar' else "Please choose an option from the properties above.")
                    return jsonify(status='success_sent_property_cards'), 200

                elif current_step and current_step.startswith('awaiting_property_action_'):
                    action_type = None; prop_id_to_use = None

                    # Case 1: User clicks a button
                    if button_id:
                        cleaned_button_id = button_id.replace("ButtonsV3:", "") if button_id.startswith("ButtonsV3:") else button_id
                        action_parts = cleaned_button_id.split('_')
                        if len(action_parts) >= 3:
                            action, subject, *prop_id_parts = action_parts
                            prop_id_to_use = "_".join(prop_id_parts)
                            if action == "show" and subject == "photos": action_type = "photos"
                            elif action == "show" and subject == "prices": action_type = "prices"
                            elif action == "book" and subject == "prop": action_type = "book"
                            if action_type:
                                # Store the property ID from the button click as the last interacted
                                interactive_flow_states[sender]['last_interacted_prop_id'] = prop_id_to_use
                        else:
                            send_whatsapp_message(sender, "Error processing selection."); return jsonify(status='error_parsing_prop_id'), 200

                    # Case 2: User types a text command
                    elif msg_type == 'text' and body_text_if_any:
                        text_lower = body_text_if_any.lower().strip()
                        # Keywords for text commands
                        photos_keywords = ["photos", "images", "pics", "صور"]
                        prices_keywords = ["prices", "price", "cost", "أسعار", "سعر", "اسعار"]
                        book_keywords = ["book", "booking", "reserve", "حجز", "إحجز", "احجز"]

                        if any(keyword in text_lower for keyword in photos_keywords): action_type = "photos"
                        elif any(keyword in text_lower for keyword in prices_keywords): action_type = "prices"
                        elif any(keyword in text_lower for keyword in book_keywords): action_type = "book"

                        if action_type:
                            prop_id_to_use = interactive_flow_states[sender].get('last_interacted_prop_id')
                            if not prop_id_to_use:
                                no_prop_msg = "Please select a property first by clicking one of its buttons."
                                if current_language == 'ar': no_prop_msg = "الرجاء تحديد عقار أولاً بالضغط على أحد أزراره."
                                send_whatsapp_message(sender, no_prop_msg)
                                return jsonify(status='success_reprompted_no_prop_selected_for_text_command'), 200

                    # Execute the action if one was determined
                    if action_type and prop_id_to_use:
                        properties_df = get_sheet2_data()
                        if properties_df.empty:
                            send_whatsapp_message(sender, "Error fetching details."); return jsonify(status='error_fetching_sheet2_for_action'), 200

                        selected_property = properties_df[properties_df['PropertyID'] == prop_id_to_use]
                        if selected_property.empty:
                            send_whatsapp_message(sender, "Property details not found."); return jsonify(status='error_prop_not_found_for_action'), 200

                        prop_details = selected_property.iloc[0].to_dict()

                        if action_type == "photos":
                            _handle_show_photos(sender, prop_details, current_language)
                            return jsonify(status='success_called_show_photos'), 200
                        elif action_type == "prices":
                            _handle_show_prices(sender, prop_details, current_language)
                            return jsonify(status='success_called_show_prices'), 200
                        elif action_type == "book":
                            _handle_book_property(sender, prop_details, current_language)
                            return jsonify(status='success_called_book_property'), 200

                    # If it's a text message that doesn't match any command, it might be a general question.
                    # We can either ignore it, reprompt, or pass to LLM. For now, let's reprompt.
                    elif msg_type == 'text' and body_text_if_any:
                         reprompt_msg = "Please choose an option from the buttons, or type 'prices', 'photos', or 'book' for the last property you selected."
                         if current_language == 'ar': reprompt_msg = "الرجاء اختيار أحد الخيارات من الأزرار، أو اكتب 'الأسعار' أو 'الصور' أو 'الحجز' لآخر عقار قمت بتحديده."
                         send_whatsapp_message(sender, reprompt_msg)
                         return jsonify(status='success_reprompted_unhandled_text_in_prop_action'), 200

                elif msg_type == 'text' and body_text_if_any:
                    logging.info(f"User {sender} in step {current_step} sent unhandled text: '{body_text_if_any}'. Reprompting.")
                    response_text = "Please make a selection using the buttons or list provided."
                    if current_language == 'ar': response_text = "الرجاء تحديد اختيارك باستخدام الأزرار أو القائمة المتوفرة."
                    send_whatsapp_message(sender, response_text)
                    return jsonify(status='success_interactive_reprompted_unhandled_text'), 200

                elif msg_type == 'reply' and (button_id or selected_row_id):
                    logging.warning(f"User {sender} sent unhandled reply in step {current_step}. ButtonID: {button_id}, ListID: {selected_row_id}")
                    send_initial_greeting_message(sender, language=current_language)
                    interactive_flow_states[sender] = {'step': 'awaiting_initial_choice', 'language': current_language}
                    return jsonify(status='success_interactive_reset_unhandled_reply'), 200

            if body_for_fallback is None:
                if msg_type == 'text': body_for_fallback = message.get('text', {}).get('body', '').strip()
                elif msg_type == 'reply' and not user_in_interactive_flow:
                    reply_data = message.get('reply', {})
                    body_for_fallback = reply_data.get('buttons_reply', {}).get('title') or reply_data.get('list_reply', {}).get('title') or ""
                elif msg_type == 'image' or msg_type == 'video': body_for_fallback = f"[User sent {msg_type}]"
                elif msg_type == 'audio':
                    # ... (audio transcription logic)
                    pass

            if not (sender and body_for_fallback):
                logging.warning(f"Webhook ignored: no sender or body_for_fallback. Message: {message}")
                continue

            if not user_in_interactive_flow and sender in sell_flow_states:
                # ... (sell_flow_states logic)
                pass

            if not user_in_interactive_flow and not (sender in sell_flow_states) and body_for_fallback:
                current_text_for_greeting_check = body_for_fallback.strip().lower()
                greetings = ["hi", "hello", "hey", "greetings", "good morning", "good afternoon", "good evening", "مرحبا", "السلام عليكم", "هلا", "هاي"]
                is_greeting = any(greet == current_text_for_greeting_check for greet in greetings if current_text_for_greeting_check) or \
                              any(current_text_for_greeting_check.startswith(greet) for greet in greetings if current_text_for_greeting_check and len(greet) > 2)
                if is_greeting:
                    send_initial_greeting_message(sender, language=current_language)
                    interactive_flow_states[sender] = {'step': 'awaiting_initial_choice', 'language': current_language}
                    return jsonify(status='success_interactive_started'), 200

            if not (sender and body_for_fallback): continue

            normalized_body = body_for_fallback.lower().strip()
            if normalized_body == "bot pause all": is_globally_paused = True; send_whatsapp_message(sender, "Bot globally paused."); continue
            if normalized_body == "bot resume all": is_globally_paused = False; paused_conversations.clear(); send_whatsapp_message(sender, "Bot globally resumed."); continue

            if is_globally_paused or sender in paused_conversations: continue

            user_id = ''.join(c for c in sender if c.isalnum())
            logging.info(f"Passing to LLM/RAG for {sender} (UID: {user_id}): {body_for_fallback}")
            history = load_history(user_id)
            llm_response_data = get_llm_response(body_for_fallback, sender, history)

            final_model_response_for_history = ""
            if llm_response_data['type'] == 'image':
                # ... (image sending)
                pass
            elif llm_response_data['type'] == 'text':
                text_content = llm_response_data['content']
                final_model_response_for_history = text_content
                chunks = split_message(text_content)
                for idx, chunk in enumerate(chunks, start=1):
                    send_whatsapp_message(sender, chunk)
                    if idx < len(chunks): time.sleep(random.uniform(0.5, 1.0))

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

def is_property_related_query(text):
    keywords = ['property', 'properties', 'apartment', 'villa', 'house', 'buy', 'rent', 'lease', 'listing', 'listings', 'available', 'real estate']
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)

if __name__ == '__main__':
    set_webhook()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
