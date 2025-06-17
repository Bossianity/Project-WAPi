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
import threading # <-- Import the threading module

# Ensure other custom modules are in the same directory or accessible via PYTHONPATH
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
from whatsapp_utils import send_whatsapp_message, send_whatsapp_image_message, set_webhook, send_interactive_list_message


# ─── Load Environment and Configuration ──────────────────────────────────────
load_dotenv()

# --- Environment Variable Check ---
CRITICAL_ENV_VARS = [
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "API_URL",        # For Whapi
    "API_TOKEN",      # For Whapi
    "BOT_URL",
    "PROPERTY_SHEET_ID",
    "PROPERTY_SHEET_NAME"
]

logging.info("Performing critical environment variable check...")
for var_name in CRITICAL_ENV_VARS:
    if os.getenv(var_name):
        logging.info(f"Environment Variable Check: {var_name} - Set")
    else:
        logging.warning(f"Environment Variable Check: {var_name} - Not Set")
# --- End Environment Variable Check ---

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PROPERTY_SHEET_ID = os.getenv('PROPERTY_SHEET_ID')
PROPERTY_SHEET_NAME = os.getenv('PROPERTY_SHEET_NAME', 'Properties')

# --- Global Pause Feature ---
is_globally_paused = False
paused_conversations = set()

# ─── Persona and AI Prompt Configuration ──────────────────────────────────
PERSONA_NAME = "مساعد"

BASE_PROMPT = (
    "You are مساعد, the AI assistant for العوجا للتأجير اليومي, operating on WhatsApp. Your role is to handle all pre-booking communication. "
    "Your tone is semi-friendly and professional — warm but not exaggerated. Use natural Saudi phrases like 'حياك الله' or 'بخدمتك'. "
    
    "CRITICAL LANGUAGE RULE: Always reply in the SAME language used by the user in their last message. If the user writes in Arabic, reply in a professional Saudi dialect. If in English, reply in English. Failure to follow this is a critical failure."
    
    "CONTEXT AWARENESS: You have access to property details and prices, but NOT real-time availability. You must remember the entire conversation and not ask for details already provided. If availability is unclear for a request, state that you will check and get back to them, then append [ACTION_NOTIFY_UNANSWERED_QUERY]."

    "BOOKING WORKFLOW: If a user expresses interest in booking, collect these 3 things if not already known: the specific property, check-in/check-out dates, and number of guests. "
    "Once you have these, say: 'Thank you. I have your details for the booking. Our team will verify the availability and contact you shortly to confirm.' Then append [ACTION_CONFIRM_BOOKING_DETAILS] to the very end of your response."
    
    "START OF CONVERSATION: Always begin with: 'This is مساعد from العوجا للتأجير اليومي. How may I assist you?' or the Arabic equivalent based on the user’s language."

    "HANDLING RETRIEVED INFORMATION (RAG CONTEXT): When 'Relevant Information Found' is available, you must read and synthesize the snippets to craft a direct and helpful response. Use natural language only. Your goal is to encourage booking by highlighting unique features and value without inventing any details."

    "MEDIA HANDLING:"
    "1. IMAGE GALLERY TASK: If the context contains an `[ACTION_SEND_IMAGE_GALLERY]` block AND the user explicitly asks for images, pictures, or photos, your entire response MUST be ONLY that block. Do not add any other text."
    "2. VIDEO LINK TASK: If the context contains a `[VIDEO_LINK]` and the user asks for a video or tour, you should incorporate this link naturally into your text response. For example: 'You can watch a video tour of the property here: [video link]'."

    "TEXT RULES: No emojis, no markdown (*, _, etc.). Use only clean plain text."
)

# ─── AI Model and API Client Initialization ────────────────────────────────────
AI_MODEL = None
if OPENAI_API_KEY:
    try:
        AI_MODEL = ChatOpenAI(model_name='gpt-4o', openai_api_key=OPENAI_API_KEY, temperature=0.1)
        logging.info("ChatOpenAI model initialized successfully.")
    except Exception as e:
        logging.error(f"Failed to initialize ChatOpenAI model: {e}", exc_info=True)
        AI_MODEL = None # Ensure AI_MODEL is None if initialization failed
else:
    logging.error("OPENAI_API_KEY not found; AI responses will fail. ChatOpenAI model not initialized.")

# ─── Flask setup ───────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

executor = ThreadPoolExecutor(max_workers=2)

# ─── Conversation history storage ─────────────────────────────────────────────
CONV_DIR = 'conversations'
os.makedirs(CONV_DIR, exist_ok=True)
MAX_HISTORY_TURNS_TO_LOAD = 6

def load_history(uid):
    path = os.path.join(CONV_DIR, f"{uid}.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

        langchain_history = []
        for item in data:
            if isinstance(item, dict) and 'role' in item:
                message_content = ""
                found_content = False

                # 1. Check for 'content' field first
                if isinstance(item.get('content'), str):
                    message_content = item['content']
                    found_content = True

                # 2. Else, check for 'parts' field (backward compatibility)
                elif 'parts' in item:
                    if isinstance(item['parts'], list) and len(item['parts']) > 0:
                        message_content = str(item['parts'][0]) # Ensure content is string
                        found_content = True
                    else:
                        logging.warning(f"Item for {uid} has 'parts' field, but it's empty or not a list: {item}")

                if not found_content:
                    logging.warning(f"Could not find 'content' or valid 'parts' in message item for {uid}: {item}. Using empty content.")

                if item['role'] == 'user':
                    langchain_history.append(HumanMessage(content=message_content))
                elif item['role'] == 'model': # Matching the role used in save_history
                    langchain_history.append(AIMessage(content=message_content))
                # Silently ignore other roles for now, or log if necessary
            else:
                logging.warning(f"Skipping malformed item (missing 'role' or not a dict) in history for {uid}: {item}")

        # Apply MAX_HISTORY_TURNS_TO_LOAD (note: each turn is a user + model message)
        if len(langchain_history) > MAX_HISTORY_TURNS_TO_LOAD * 2:
            return langchain_history[-(MAX_HISTORY_TURNS_TO_LOAD * 2):]
        return langchain_history
    except Exception as e:
        logging.error(f"Error loading or processing history for {uid}: {e}", exc_info=True)
        return []

def save_history(uid, history):
    path = os.path.join(CONV_DIR, f"{uid}.json")
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error saving history for {uid}: {e}")


# ─── Helper Functions ───────────────────────────────────────────────────────────
def split_message(text, max_chars=1600):
    """Splits a long message into chunks."""
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

def format_target_user_id(user_id_input):
    """Formats a phone number to the required WhatsApp ID format."""
    user_id = user_id_input.strip()
    if '@s.whatsapp.net' not in user_id:
        user_id = ''.join(filter(str.isdigit, user_id))
        return f"{user_id}@s.whatsapp.net"
    return user_id

def is_property_related_query(text):
    """Checks if a query is generally about properties using keywords."""
    keywords = [
        'property', 'properties', 'apartment', 'villa', 'house', 'place', 'stay', 'airbnb',
        'book', 'booking', 'rent', 'rental', 'listing', 'listings', 'available', 'accommodation',
        'شقة', 'فيلا', 'حجز', 'ايجار', 'سكن', 'متوفر'
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)

# ─── Generate response from LLM with RAG ───────────────────────────────────
def get_llm_response(text, sender_id, history_dicts=None, retries=3):
    if not AI_MODEL:
        return {'type': 'text', 'content': "AI Model not configured."}

    # Step 1: Intent and Filter Extraction
    analysis_prompt = f"""
    Analyze the user's request: '{text}'
    Determine if it's a property search or a general question.
    Respond with a JSON object with "intent" and "filters".

    Supported filter keys: `WeekdayPrice`, `WeekendPrice`, `MonthlyPrice`, `Guests`, `City`, `Neighborhood`, `PropertyName`.
    - For a generic price query like "under 1000", use the `WeekdayPrice` key for filtering.
    - For numeric keys, the operator can be '<', '>', or '='.

    Example 1: "show me properties below 1000 sar in riyadh"
    {{
      "intent": "property_search",
      "filters": {{
        "WeekdayPrice": {{ "operator": "<", "value": 1000 }},
        "City": {{ "operator": "=", "value": "riyadh" }}
      }}
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
    except Exception as e:
        logging.error(f"Failed to analyze user query with LLM: {e}. Defaulting to general question.")
        intent = "general_question"
        filters = None

    # Step 2: Logic Execution Based on Intent
    context_str = ""
    if (intent == "property_search" or is_property_related_query(text)) and PROPERTY_SHEET_ID:
        all_properties_df = property_handler.get_sheet_data()
        if not all_properties_df.empty:
            filtered_df = property_handler.filter_properties(all_properties_df, filters) if filters else all_properties_df
            if not filtered_df.empty:
                context_str = "Relevant Information Found:\n"
                for _, prop in filtered_df.head(5).iterrows():
                    price_parts = []
                    if pd.notna(prop.get('WeekdayPrice')) and prop.get('WeekdayPrice') > 0: price_parts.append(f"Weekday: {prop.get('WeekdayPrice')} SAR/night")
                    if pd.notna(prop.get('WeekendPrice')) and prop.get('WeekendPrice') > 0: price_parts.append(f"Weekend: {prop.get('WeekendPrice')} SAR/night")
                    if pd.notna(prop.get('MonthlyPrice')) and prop.get('MonthlyPrice') > 0: price_parts.append(f"Monthly: {prop.get('MonthlyPrice')} SAR")
                    price_str = ", ".join(price_parts) or "Price available on request"

                    context_str += (
                        f"PropertyName: {prop.get('PropertyName', 'N/A')}\n"
                        f"Location: {prop.get('Neighborhood', 'N/A')}, {prop.get('City', 'N/A')}\n"
                        f"Price: {price_str}\n"
                        f"Max Guests: {prop.get('Guests', 'N/A')}\n"
                        f"Description: {prop.get('Description', 'No description available.')}\n"
                    )
                    
                    image_urls = [prop.get(f'ImageURL{i}') for i in range(1, 4) if pd.notna(prop.get(f'ImageURL{i}')) and str(prop.get(f'ImageURL{i}')).startswith('http')]
                    if image_urls:
                        context_str += "[ACTION_SEND_IMAGE_GALLERY]\n" + "\n".join(image_urls) + f"\nImages for {prop.get('PropertyName', 'the property')}\n"

                    video_url = prop.get('VideoURL1')
                    if pd.notna(video_url) and str(video_url).startswith('http'):
                        context_str += f"[VIDEO_LINK]: {video_url}\n"
                    context_str += "---\n"
            else:
                context_str = "Relevant Information Found:\nNo properties found matching your criteria."
        else:
             context_str = "Relevant Information Found:\nI was unable to access property listings."
    else:
        vector_store = current_app.config.get('VECTOR_STORE')
        if vector_store:
            retrieved_docs = query_vector_store(text, vector_store, k=3)
            if retrieved_docs:
                context_str = "\n\nRelevant Information Found:\n" + "\n".join([doc.page_content for doc in retrieved_docs])

    # Step 3: Generate Final Response
    final_prompt_to_llm = context_str + f"\n\nUser Question: {text}" if context_str else text
    messages = [SystemMessage(content=BASE_PROMPT)] + history_dicts + [HumanMessage(content=final_prompt_to_llm)]

    for attempt in range(retries):
        try:
            resp = AI_MODEL.invoke(messages)
            raw_llm_output = resp.content.strip()

            if raw_llm_output.startswith("[ACTION_SEND_IMAGE_GALLERY]"):
                lines = raw_llm_output.splitlines()
                urls = [line for line in lines[1:-1] if line.strip().startswith('http')]
                caption = lines[-1] if len(lines) > 1 else "Here are the images:"
                return {'type': 'gallery', 'urls': urls, 'caption': caption}
            else:
                response_text = re.sub(r'\[ACTION_SEND_IMAGE_GALLERY\].*?(\n|$)', '', raw_llm_output, flags=re.DOTALL)
                response_text = re.sub(r'\[VIDEO_LINK\]:.*?\n', '', response_text).strip()
                return {'type': 'text', 'content': response_text}
        except Exception as e:
            logging.warning(f"LLM API error on attempt {attempt+1}/{retries}: {e}")
            if attempt + 1 == retries:
                return {'type': 'text', 'content': "I am having trouble processing your request. Please try again."}
            time.sleep((2 ** attempt))

    return {'type': 'text', 'content': "I could not generate a response after multiple attempts."}

# ─── Health Check Endpoint ─────────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def health_check():
    return jsonify(status="healthy", message="Application is running."), 200

# ─── Main Webhook Handler ──────────────────────────────────────────────────────
@app.route('/hook', methods=['POST'])
def webhook():
    try:
        data = request.json or {}
        incoming_messages = data.get('messages', [])
        if not incoming_messages:
            return jsonify(status='success_no_messages'), 200

        for message in incoming_messages:
            if message.get('from_me'):
                continue

            sender = message.get('from')
            msg_type = message.get('type')
            body_for_fallback = None

            if msg_type == 'text':
                body_for_fallback = message.get('text', {}).get('body')
            elif msg_type == 'image' or msg_type == 'video':
                body_for_fallback = f"[User sent a {msg_type}]"
                if message.get('media', {}).get('caption'):
                    body_for_fallback += f" with caption: {message['media']['caption']}"
            
            if not (sender and body_for_fallback):
                continue

            normalized_body = body_for_fallback.lower().strip()
            global is_globally_paused

            if normalized_body == "bot pause all":
                is_globally_paused = True
                send_whatsapp_message(sender, "Bot is now globally paused.")
                continue
            if normalized_body == "bot resume all":
                is_globally_paused = False
                paused_conversations.clear()
                send_whatsapp_message(sender, "Bot is now globally resumed.")
                continue
            if normalized_body.startswith("bot pause "):
                target_user_input = normalized_body.split("bot pause ", 1)[1].strip()
                if target_user_input:
                    target_user_id = format_target_user_id(target_user_input)
                    paused_conversations.add(target_user_id)
                    send_whatsapp_message(sender, f"Bot interactions will be paused for: {target_user_id}")
                continue
            if normalized_body.startswith("bot resume "):
                target_user_input = normalized_body.split("bot resume ", 1)[1].strip()
                if target_user_input:
                    target_user_id = format_target_user_id(target_user_input)
                    paused_conversations.discard(target_user_id)
                    send_whatsapp_message(sender, f"Bot interactions will be resumed for: {target_user_id}")
                continue

            if is_globally_paused or sender in paused_conversations:
                continue

            user_id = ''.join(c for c in sender if c.isalnum())
            history = load_history(user_id)
            llm_response_data = get_llm_response(body_for_fallback, sender, history)

            final_model_response_for_history = ""
            if llm_response_data.get('type') == 'gallery':
                gallery = llm_response_data
                if gallery.get('urls'):
                    logging.info(f"Sending gallery to {sender}.")
                    for i, url in enumerate(gallery['urls']):
                        send_whatsapp_image_message(sender, gallery['caption'] if i == 0 else "", url)
                        time.sleep(1.5)
                    final_model_response_for_history = f"[Sent gallery of {len(gallery['urls'])} images]"
            
            elif llm_response_data.get('type') == 'text' and llm_response_data.get('content'):
                text_content = llm_response_data['content']
                final_model_response_for_history = text_content
                chunks = split_message(text_content)
                for chunk in chunks:
                    send_whatsapp_message(sender, chunk)
                    time.sleep(1)

            history.extend([
                {'role': 'user', 'content': body_for_fallback},
                {'role': 'model', 'content': final_model_response_for_history}
            ])
            if len(history) > MAX_HISTORY_TURNS_TO_LOAD * 2:
                history = history[-(MAX_HISTORY_TURNS_TO_LOAD * 2):]
            save_history(user_id, history)

        return jsonify(status='success'), 200

    except Exception as e:
        logging.exception(f"FATAL Error in webhook processing: {e}")
        return jsonify(status='error', message='Internal Server Error'), 500

# ─── App Startup ──────────────────────────────────────────────────────────────
# This function will run in a separate thread to avoid blocking the server start
def deferred_startup():
    # Wait a few seconds for the server to bind the port
    time.sleep(5)
    with app.app_context():
        logging.info("Running deferred startup tasks...")
        set_webhook()
        logging.info("Deferred startup tasks completed.")

# Initialize RAG components immediately, as they are needed for responses.
with app.app_context():
    try:
        embeddings_rag = OpenAIEmbeddings(model="text-embedding-ada-002", openai_api_key=os.getenv('OPENAI_API_KEY'))
        vector_store_rag = initialize_vector_store()
        if vector_store_rag and embeddings_rag:
            app.config['EMBEDDINGS'] = embeddings_rag
            app.config['VECTOR_STORE'] = vector_store_rag
            logging.info("RAG components initialized and stored in app config.")
        else:
            logging.error("Failed to initialize RAG components.")
    except Exception as e:
        logging.critical(f"A critical error occurred during RAG initialization: {e}")

# Start the deferred startup tasks in a background thread
# This ensures the server starts immediately and the port is bound.
startup_thread = threading.Thread(target=deferred_startup)
startup_thread.daemon = True
startup_thread.start()

if __name__ == '__main__':
    # This block is for local development and debugging ONLY.
    # It will NOT run on Render, which uses the waitress/gunicorn start command.
    logging.warning("RUNNING IN LOCAL DEVELOPMENT MODE. DO NOT USE IN PRODUCTION.")
    port = int(os.getenv('PORT', 5001)) # Use a different port for local testing
    app.run(host='0.0.0.0', port=port, debug=True)
