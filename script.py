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
import dateparser

# Ensure rag_handler.py is in the same directory or accessible via PYTHONPATH
import property_handler
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
from whatsapp_utils import (
    send_whatsapp_message,
    send_whatsapp_image_message,
    set_webhook,
    send_interactive_list_message,
    send_interactive_button_message
)
from calendar_handler import (
    parse_user_date_input,
    get_calendar_service as get_cal_service_from_handler,
    find_or_create_calendar_by_property_id,
    check_calendar_availability,
    create_booking_event, # Added import
    DEFAULT_USER_INPUT_TIMEZONE as CAL_HANDLER_USER_TZ,
    EVENT_STORAGE_TIMEZONE as CAL_HANDLER_STORAGE_TZ,
    find_soonest_availability
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
PERSONA_NAME = "USMLE Step 2 HY Bot"
BASE_PROMPT_TEMPLATE = (
    "You are a helpful and friendly educational bot. "
    "Your primary goal is to provide high-yield summaries of USMLE Step 2 exam concepts based on a provided Google Sheet. "
    "Your tone is that of a knowledgeable and encouraging study partner. "
    "CRITICAL RULE: Your response MUST be based *only* on the information from the provided Google Sheet. Do not add any information from external sources. "
    "When providing summaries, rephrase the concepts to be more understandable and create a narrative where possible, but the core meaning must remain the same. "
    "Every fact you provide MUST be cited with its source ID in square brackets, like this: [Source_ID]. "
    "If a user asks a question that is not covered in the sheet, state that the information is not available in the provided materials. "
    "TEXT STYLING: No emojis, asterisks, or markdown. Plain text only. "
    "Critical STYLING RULE: Make your responses WELL STRUCTURED, if its in paragraphs make sure there is an empty line in between paragaphs. "
)
try:
    with open(PERSONA_FILE) as f:
        p = json.load(f)
    logging.info(f"Original persona name from {PERSONA_FILE} was '{p.get('name')}'. Script now uses dynamic naming ('EduBot') for LLM prompts based on BASE_PROMPT_TEMPLATE.")
except Exception as e:
    logging.warning(f"Could not load {PERSONA_FILE} or parse it: {e}. Using dynamic naming ('EduBot') for LLM prompts based on BASE_PROMPT_TEMPLATE.")

AI_MODEL = None
if OPENAI_API_KEY:
    AI_MODEL = ChatOpenAI(
    model="o3-mini",
    reasoning={"effort": "high"}
)
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
#       # ...
#       pass

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


def get_date_from_text_with_llm(text, language='en'):
    """
    Uses the LLM to extract a date from a user's text, especially for colloquial terms.
    Returns a datetime object or None.
    """
    if not AI_MODEL:
        logging.error("AI_MODEL not configured. Cannot get date from text.")
        return None

    today = datetime.now(pytz.timezone('Asia/Dubai')).strftime('%Y-%m-%d')

    if language == 'ar':
        prompt = (
            f"المرجع الزمني لـ 'اليوم' هو {today}. "
            f"من النص التالي: \"{text}\"، الرجاء استخراج التاريخ المطلوب بصيغة YYYY-MM-DD. "
            "إذا كان النص 'بكرة' أو 'غدا'، فهذا يعني يوما واحدا بعد اليوم. "
            "إذا لم يتم العثور على تاريخ، قم بالرد بـ 'None'."
            "قم بالرد فقط بالتاريخ بصيغة YYYY-MM-DD."
        )
    else:
        prompt = (
            f"The reference for 'today' is {today}. "
            f"From the following text: \"{text}\", please extract the requested date in YYYY-MM-DD format. "
            "If the text is 'tomorrow', it means one day after today. "
            "If no date is found, respond with 'None'. "
            "Only respond with the date in YYYY-MM-DD format."
        )

    try:
        messages = [HumanMessage(content=prompt)]
        response = AI_MODEL.invoke(messages)
        date_str = response.content.strip()

        if date_str and date_str.lower() != 'none':
            # Validate that the response is a plausible date format
            if re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                logging.info(f"LLM extracted date: '{date_str}' from text: '{text}'")
                # Use dateparser to get a proper timezone-aware datetime object
                # This helps standardize the output
                parsed_date = dateparser.parse(date_str)
                if parsed_date:
                    return CAL_HANDLER_USER_TZ.localize(parsed_date)
            else:
                logging.warning(f"LLM returned a non-date string for date extraction: '{date_str}'")

    except Exception as e:
        logging.error(f"Error getting date from LLM: {e}", exc_info=True)

    return None

def get_intent_from_text(text, possible_intents, language='en'):
    """
    Uses the LLM to classify the user's text into one of the possible intents.
    """
    if not AI_MODEL:
        logging.error("AI_MODEL not configured. Cannot get intent from text.")
        return None

    # Create a dynamic prompt based on the language and available intents
    intent_list_str = ", ".join([f"'{intent}'" for intent in possible_intents])
    if language == 'ar':
        prompt_template = (
            "الرجاء تحليل النص التالي وتحديد النية الأساسية للمستخدم. النص هو: \"{text}\". "
            "يجب أن تكون النية واحدة من الخيارات التالية: {intent_list}. "
            "أمثلة لـ 'book_soonest': 'نعم، احجز هذا'، 'اوكي احجزلي في هذا التاريخ'، 'نعم'. "
            "أمثلة لـ 'choose_another_room': 'لا، أرني غرفة أخرى'، 'غرفة أخرى'. "
            "إذا لم يتطابق النص مع أي من الخيارات، قم بالرد بـ 'None'. "
            "قم بالرد فقط باسم النية المحددة."
        )
    else:
        prompt_template = (
            "Please analyze the following text and determine the user's primary intent. The text is: \"{text}\". "
            "The intent must be one of the following options: {intent_list}. "
            "Examples for 'book_soonest': 'Yes, book this one', 'Okay book it for that date', 'yes'. "
            "Examples for 'choose_another_room': 'No, show me another room', 'another room'. "
            "If the text does not match any of the options, respond with 'None'. "
            "Respond only with the name of the identified intent."
        )

    prompt = prompt_template.format(text=text, intent_list=intent_list_str)

    try:
        messages = [HumanMessage(content=prompt)]
        response = AI_MODEL.invoke(messages)
        intent = response.content.strip().replace("'", "") # Clean up response

        if intent in possible_intents:
            logging.info(f"LLM identified intent: '{intent}' for text: '{text}'")
            return intent
        else:
            logging.warning(f"LLM response '{intent}' is not in the list of possible intents. Text was: '{text}'")
            return None
    except Exception as e:
        logging.error(f"Error getting intent from LLM: {e}", exc_info=True)
        return None


def rag_pipeline(query: str, sender_id: str):
    """
    The new RAG pipeline.
    """
    send_whatsapp_message(sender_id, "Thinking...")
    vector_store = current_app.config.get('VECTOR_STORE') or vector_store_rag
    if not vector_store:
        return "The educational database is currently unavailable. Please try again later."

    # 1. Retrieve relevant concepts
    retrieved_docs = query_vector_store(query, vector_store, k=10)
    if not retrieved_docs:
        return "I could not find any high-yield facts related to your query in the provided materials."

    send_whatsapp_message(sender_id, "Relevant NBME questions found...")

    # 2. Generate a summary
    context_parts = []
    for doc in retrieved_docs:
        source_id = doc.metadata.get('Source_ID', 'Unknown')
        context_parts.append(f"Fact from {source_id}: {doc.page_content}")
    context_str = "\n\n".join(context_parts)

    send_whatsapp_message(sender_id, "Summarizing and compiling a response...")

    system_prompt_content = (
        "You are a USMLE Step 2 tutor. Your goal is to help students understand high-yield concepts for their NBME and CMS forms. "
        "Take the user's question and the provided facts, and explain the concepts in a clear, concise, and educational manner, as a tutor would. "
        "Instead of saying 'The provided facts focus on...', start with phrases like 'The NBME likes to test on...' or 'Contraception is a high-yield topic that gets tested a lot on the boards...'. "
        "Then, explain how the concepts are tested. For example, 'You need to know that barrier methods are considered safe in women with cardiovascular risk factors like smoking because they have no hormonal effects. Therefore, if you see a question with a person with these risk factors, give them barrier contraception.' "
        "CRITICAL RULE: Your response MUST be based *only* on the provided facts. Do not add any external information. "
        "Every fact you provide MUST be cited with its source ID in square brackets, like this: [Source_ID]. "
        "If the user's question cannot be answered from the facts, state that the information is not available in the provided materials. "
        "TEXT STYLING: No emojis, asterisks, or markdown. Plain text only. Do not include any code snippets or dictionary formatting in your response. "
         "Critical STYLING RULE: Make your responses WELL STRUCTURED, if its in paragraphs make sure there is an empty line in between paragaphs. "
        "Respond with plain text only, no JSON or dictionary structures."
    )

    messages = [
        SystemMessage(content=system_prompt_content),
        HumanMessage(content=f"Please provide a summary for the following query based on the provided text:\n\nQuery: {query}\n\nText: {context_str}")
    ]

    try:
        resp = AI_MODEL.invoke(messages)

        # Handle different response formats
        response_text = ""
        
        if hasattr(resp, 'content'):
            # Standard AIMessage response
            if isinstance(resp.content, str):
                response_text = resp.content
            elif isinstance(resp.content, list):
                # Handle list of content blocks
                text_parts = []
                for item in resp.content:
                    if isinstance(item, dict):
                        text_parts.append(item.get('text', str(item)))
                    else:
                        text_parts.append(str(item))
                response_text = " ".join(text_parts)
            else:
                response_text = str(resp.content)
        elif isinstance(resp, dict):
            # Handle dictionary responses (like from o3-mini with reasoning)
            if 'text' in resp:
                response_text = resp['text']
            elif 'content' in resp:
                response_text = resp['content']
            else:
                # Try to extract text from any nested structures
                response_text = str(resp)
        else:
            # Fallback for any other format
            response_text = str(resp)

        # Clean up the response to ensure it's plain text
        response_text = response_text.strip()
        
        # Remove any remaining dictionary-like structures that might have leaked through
        import re
        # Remove patterns like {'type': 'text', 'text': '...'}
        response_text = re.sub(r'\{[^}]*\'type\'[^}]*\'text\'[^}]*\}', '', response_text)
        # Remove any JSON-like structures
        response_text = re.sub(r'\{[^}]*\}', '', response_text)
        
        if not response_text or len(response_text.strip()) == 0:
            return "I am having trouble processing your request."
            
        return response_text

    except Exception as e:
        logging.error(f"Error during RAG pipeline summary generation: {e}", exc_info=True)
        return "I am having trouble processing your request."
def get_llm_response(text, sender_id, history_dicts=None, retries=3):
    if not AI_MODEL:
        return {'type': 'text', 'content': "AI Model not configured."}

    # All educational queries will now go through the RAG pipeline
    response_text = rag_pipeline(text, sender_id)
    return {'type': 'text', 'content': response_text}

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
                    action = None
                    if button_id:
                        if button_id == 'button_id1' or button_id.endswith(':button_id1'): action = 'operate'
                        elif button_id == 'button_id2' or button_id.endswith(':button_id2'): action = 'rent'
                        elif button_id == 'button_id3' or button_id.endswith(':button_id3'): action = 'other'
                    elif msg_type == 'text' and body_text_if_any:
                        possible_intents = ['operate', 'rent', 'other']
                        action = get_intent_from_text(body_text_if_any, possible_intents, language=current_language)

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
                        if button_id == 'button_id4' or button_id.endswith(':button_id4'):
                            send_furnished_apartment_survey_message(sender, language=current_language)
                        elif button_id == 'button_id5' or button_id.endswith(':button_id5'):
                            send_unfurnished_apartment_survey_message(sender, language=current_language)
                        elif button_id == 'speak_to_agent':
                            owner_number = os.getenv("OWNER_WHATSAPP_NUMBER")
                            if owner_number:
                                send_whatsapp_message(owner_number, f"Client {sender} wants to speak to an agent.")
                            send_whatsapp_message(sender, "An agent will contact you shortly." if current_language == 'en' else "سيتواصل معك وكيل قريبا.")
                            if sender in interactive_flow_states: del interactive_flow_states[sender]
                            return jsonify(status='success_interactive_handled'), 200
                        else:
                            send_furnished_query_message(sender, language=current_language)
                            return jsonify(status='success_interactive_reprompted_unknown_option'), 200
                        if sender in interactive_flow_states: del interactive_flow_states[sender]
                        return jsonify(status='success_interactive_handled_survey_sent'), 200
                    elif msg_type == 'text' and body_text_if_any:
                        send_whatsapp_message(sender, "الرجاء الاختيار من الأزرار." if current_language == 'ar' else "Please choose from the buttons.")
                        send_furnished_query_message(sender, language=current_language)
                        return jsonify(status='success_interactive_reprompted_text_input'), 200
    except Exception as e:
        logging.error(f"Error in webhook: {e}", exc_info=True)
        return jsonify(status='error', error=str(e)), 500

    # Fallback to general LLM response if no interactive flow was handled
    # This is a simplified representation of the logic that would call the LLM
    try:
        data = request.json or {}
        incoming_messages = data.get('messages', [])
        for message in incoming_messages:
            if message.get('from_me'): continue
            
            sender = message.get('from')
            body_text = None
            if message.get('type') == 'text':
                body_text = message['text']['body']
            elif message.get('type') == 'reply':
                 reply_content = message.get('reply', {})
                 body_text = reply_content.get('buttons_reply', {}).get('title') or reply_content.get('list_reply', {}).get('title')


            if body_text and sender not in interactive_flow_states and sender not in paused_conversations and not is_globally_paused:
                # This is where the fixed functions will be called
                response_dict = get_llm_response(body_text, sender)
                response_text = response_dict.get('content', "Sorry, I couldn't process that.")

                # Send the response
                chunks = split_message(response_text)
                for i, chunk in enumerate(chunks):
                    send_whatsapp_message(sender, chunk)
                    if i < len(chunks) - 1:
                        time.sleep(1) # Small delay between messages
                return jsonify(status='success_llm_sent'), 200
    except Exception as e:
         logging.error(f"Error in fallback LLM handling: {e}", exc_info=True)
    
    return jsonify(status='success_end_of_logic'), 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
