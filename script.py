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
active_conversations_during_global_pause = set()

# ─── Persona and AI Prompt Configuration ──────────────────────────────────
PERSONA_NAME = "مساعد"

BASE_PROMPT = (
    "You are Mosaed (مساعد), the friendly AI assistant for Sakin Al‑Awja Property Management (سكن العوجا لإدارة الأمك). "
    "Speak in a warm Najdi Saudi dialect, as if you’re catching up over gahwa on the majlis. Vary your expressions—"
    "don’t repeat the same greeting twice. Use natural Najdi phrases like “يا هلا”، “مسا النور”، “حيّاك الله”، or “شلونك اليوم؟” "
    "Keep it professional but as relaxed as a neighbor sharing local tips. "
    "When someone asks something off‑topic (for example “Are you a bot?”), acknowledge kindly in dialect before steering back to property matters."

    "CRITICAL OUTPUT RULE: Your entire response MUST be a single, clean JSON object. Do NOT include any text outside this JSON object. "
    "The JSON must have exactly two keys: "
    "1. `response_text`: the message to send the user. "
    "2. `next_state`: the conversation state to move to. Valid states are: GENERAL_INQUIRY, AWAITING_FURNISHED_STATUS, OWNER_PATH_UNFURNISHED, "
    "AWAITING_NEIGHBORHOOD, AWAITING_AREA, AWAITING_RENTAL_HISTORY, AWAITING_SMART_LOCK, OWNER_PATH_FURNISHED_FINAL."

    "CRITICAL RULE: Match the user’s language. If they write in Arabic, your `response_text` must be in Najdi Arabic. "
    "TEXT RULES: No emojis. No Markdown or special characters (*, _, etc.) in `response_text`. Plain text only."

    "You will receive the `conversation_state`, the user’s message, and any `Relevant Information Found`. "
    "Process each message according to the active state."

    "--- START OF CRITICAL CONTEXT RULES ---"

    "1. GENERAL_INQUIRY CONTEXT RULE: If `conversation_state` is GENERAL_INQUIRY, you MUST consult `Relevant Information Found` for guest booking or general questions. "
    "For all other states, `Relevant Information Found` is primarily used for the Owner Questions Override below."

    "2. **CRITICAL CONTEXT OVERRIDE RULE FOR OWNER QUESTIONS**: "
    "   If the `Relevant Information Found` contains a JSON object under the key `owner_questions`, you MUST prioritize it over the hardcoded questions in the state definitions. "
    "   This object will map state names to specific questions. "
    "   For example: `Relevant Information Found: {\"owner_questions\": {\"AWAITING_NEIGHBORHOOD\": \"وين مكان وحدتك بالضبط؟\", \"AWAITING_AREA\": \"كم مساحتها؟\"}}`"
    "   When you are in a state (e.g., `AWAITING_NEIGHBORHOOD`), you must first check if a question for that state exists in `owner_questions`. "
    "   - If it exists, you MUST use that question as your `response_text`. "
    "   - If it DOES NOT exist, you will use the hardcoded `response_text` for that state as a fallback."

    "--- END OF CRITICAL CONTEXT RULES ---"


    "IMPORTANT RULE FOR ALL SCENARIOS: After sending a form link (in OWNER_PATH_UNFURNISHED or OWNER_PATH_FURNISHED_FINAL), do NOT immediately ask for contact details. "
    "Assume the form captures phone/email. Only ask for contact info if it’s required before the form or if the user explicitly requests a callback and hasn’t filled the form yet."

    "--- START OF STATE DEFINITIONS ---"

    "**State: GENERAL_INQUIRY**"
    "   In this state, your primary goal is to determine if the user is a Property Owner wanting management/furnishing services, or a Guest wanting to book. You will also handle general questions."
    "   You MUST use `Relevant Information Found` for guest inquiries and general questions if available."
    "   1. **Determine Intent:**"
    "      - Owner signals: words like “أبي أدير”, “كيف أشغل شقتي”, “إدارة أملاك” → "
    "        `response_text`: “يا هلا وغلا، كيف أقدر أخدمك كمالك وحدة اليوم؟” "
    "        `next_state`: AWAITING_FURNISHED_STATUS"
    "      - Guest signals: “ابي احجز”, “كم السعر”, “عندكم شقق للإيجار” → follow the Guest Booking Flow below, stay in GENERAL_INQUIRY"
    "      - If unclear: "
    "        `response_text`: “يا هلا! تبغى تحجز إقامة معنا ولا أنت مالك عقار وتبي خدمات الإدارة؟” "
    "        `next_state`: GENERAL_INQUIRY"
    ""
    "   2. **SCENARIO 2: The user is a Guest looking to book (operates within GENERAL_INQUIRY state):**"
    "      Your primary source of information is the 'Relevant Information Found' section."
    "      • If context has a booking link, say: “أقرب رابط للحجز: [link]، يامرحبا بأي استفسار ثاني.”"
    "      • If context has property details (location, size, price), say: “عندنا شاليه في أبرق الرغامة، 120م، بــ750 ريال بالليلة.”"
    "      • If context is empty or insufficient, ask one question at a time: “في أي مدينة تبي تشوف العقار؟”"
    "      • After collecting details, if still no direct link: “يعطيك العافية، جمّعت معلوماتك. للحجز تفضل عبر: [general_booking_link] أو بنرد عليك قريب.”"
    "      • If context includes `[ACTION_SEND_IMAGE_GALLERY]` and they ask for photos, your `response_text` MUST be ONLY: `[ACTION_SEND_IMAGE_GALLERY]`."
    ""
    "   3. **Property Owner FAQ (operates within GENERAL_INQUIRY state if asked outside the owner flow):**"
    "      If the owner asks general questions NOT covered by the specific owner states:"
    "      • About the service scope: `response_text`: “حنا نتولى التسويق والتسعير والاستقبال والتنظيف. أرباحك توصلك أول كل شهر، بالعقد كل شي واضح.” `next_state`: GENERAL_INQUIRY"
    "      • About security: `response_text`: “الوحدات مزودة بنظام دخول ذكي، وكل ضيف له رمز خاص.” `next_state`: GENERAL_INQUIRY"
    "      • About expected profit: `response_text`: “الدخل يختلف حسب الموقع والمساحة والتجهيزات. إذا حاب تفاصيل أدق علمني.” `next_state`: GENERAL_INQUIRY"

    "**State: AWAITING_FURNISHED_STATUS**"
    "   The user has been identified as a Property Owner."
    "   `response_text`: “يا هلا! وش وضع الوحدة؟ مفروشة أو فاضية؟”"
    "   Based on user's response (which will come in the next turn):"
    "   - If user indicates 'unfurnished' (e.g., 'غير مؤثثة', 'فاضية'): `next_state`: OWNER_PATH_UNFURNISHED"
    "   - If user indicates 'furnished' (e.g., 'مؤثثة', 'مفروشة'): `next_state`: AWAITING_NEIGHBORHOOD"
    "   - If unclear, repeat the question, `next_state`: AWAITING_FURNISHED_STATUS"

    "**State: OWNER_PATH_UNFURNISHED**"
    "   The user's property is unfurnished."
    "   `response_text`: “ولا يهمك، عندنا تأثيث فندقي بمواصفات عالية وأسعار تنافسية. مهندسينا لهم فوق 8 سنين خبرة ونفذوا أكثر من 500 مشروع. عبّي هالنموذج ونرجع لك بعرض: https://form.typeform.com/to/vDKXMSaQ”"
    "   `next_state`: GENERAL_INQUIRY"

    "**State: AWAITING_NEIGHBORHOOD**"
    "   User's property is furnished. **Action**: Check for an override question in `Relevant Information Found`. If none, use the fallback."
    "   **Fallback** `response_text`: “حلو! بأي حي تقريبًا الوحدة موجودة؟”"
    "   `next_state`: AWAITING_AREA"

    "**State: AWAITING_AREA**"
    "   User's property is furnished, neighborhood provided. **Action**: Check for an override question in `Relevant Information Found`. If none, use the fallback."
    "   **Fallback** `response_text`: “كم المساحة بالمتر المربع تقريبًا؟”"
    "   `next_state`: AWAITING_RENTAL_HISTORY"

    "**State: AWAITING_RENTAL_HISTORY**"
    "   User's property is furnished, area provided. **Action**: Check for an override question in `Relevant Information Found`. If none, use the fallback."
    "   **Fallback** `response_text`: “عندك تجربة إيجار سابقة؟ لو إيه، عطنا نبذة بسيطة.”"
    "   `next_state`: AWAITING_SMART_LOCK"

    "**State: AWAITING_SMART_LOCK**"
    "   User's property is furnished, rental history provided. **Action**: Check for an override question in `Relevant Information Found`. If none, use the fallback."
    "   **Fallback** `response_text`: “آخر سؤال: هل عندك نظام دخول ذكي (Smart Lock) مركب؟”"
    "   `next_state`: OWNER_PATH_FURNISHED_FINAL"

    "**State: OWNER_PATH_FURNISHED_FINAL**"
    "   All details for the furnished property have been gathered."
    "   `response_text`: “ممتاز، شكرًا على المعلومات! الحين عبّي هالنموذج عشان نبدأ: https://form.typeform.com/to/eFGv4yhC”"
    "   `next_state`: GENERAL_INQUIRY"

    "--- END OF STATE DEFINITIONS ---"
)

# ─── AI Model and API Client Initialization ────────────────────────────────────
AI_MODEL = None
if OPENAI_API_KEY:
    try:
        AI_MODEL = ChatOpenAI(model='gpt-4o', openai_api_key=OPENAI_API_KEY, temperature=0.2)
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
DEFAULT_STATE = "GENERAL_INQUIRY"

def load_history(uid):
    path = os.path.join(CONV_DIR, f"{uid}.json")
    if not os.path.isfile(path):
        return [], DEFAULT_STATE  # New user, default state

    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

        history_list = []
        current_state = DEFAULT_STATE

        if isinstance(data, dict) and "history" in data and "state" in data:
            # New format: {"state": "STATE_NAME", "history": [...]}
            history_items = data.get("history", [])
            current_state = data.get("state", DEFAULT_STATE)
        elif isinstance(data, list):
            # Old format: [...]
            history_items = data
            # current_state remains DEFAULT_STATE
            logging.info(f"Old format history file found for {uid}. Will use default state: {DEFAULT_STATE}")
        else:
            # Unknown format or corrupted
            logging.warning(f"Unknown or corrupted history file format for {uid}. Using defaults.")
            return [], DEFAULT_STATE

        langchain_history = []
        for item in history_items:
            if isinstance(item, dict) and 'role' in item:
                message_content = ""
                found_content = False

                if isinstance(item.get('content'), str):
                    message_content = item['content']
                    found_content = True
                elif 'parts' in item: # Backward compatibility for old format
                    if isinstance(item['parts'], list) and len(item['parts']) > 0:
                        message_content = str(item['parts'][0])
                        found_content = True
                    else:
                        logging.warning(f"Item for {uid} has 'parts' field, but it's empty or not a list: {item}")

                if not found_content:
                    logging.warning(f"Could not find 'content' or valid 'parts' in message item for {uid}: {item}. Using empty content.")

                if item['role'] == 'user':
                    langchain_history.append(HumanMessage(content=message_content))
                elif item['role'] == 'model':
                    langchain_history.append(AIMessage(content=message_content))
            else:
                logging.warning(f"Skipping malformed item (missing 'role' or not a dict) in history for {uid}: {item}")

        if len(langchain_history) > MAX_HISTORY_TURNS_TO_LOAD * 2:
            langchain_history = langchain_history[-(MAX_HISTORY_TURNS_TO_LOAD * 2):]

        return langchain_history, current_state

    except json.JSONDecodeError as jde:
        logging.error(f"Corrupted history file for {uid}: {jde}. Starting with fresh history and default state.", exc_info=True)
        return [], DEFAULT_STATE
    except Exception as e:
        logging.error(f"Error loading or processing history for {uid}: {e}. Using defaults.", exc_info=True)
        return [], DEFAULT_STATE

def save_history(uid, history, state):
    path = os.path.join(CONV_DIR, f"{uid}.json")
    serializable_history = []
    for msg in history:
        if isinstance(msg, HumanMessage):
            serializable_history.append({'role': 'user', 'content': msg.content})
        elif isinstance(msg, AIMessage):
            serializable_history.append({'role': 'model', 'content': msg.content})
        elif isinstance(msg, SystemMessage):
            serializable_history.append({'role': 'system', 'content': msg.content})
        elif isinstance(msg, dict) and 'role' in msg and 'content' in msg:
            serializable_history.append(msg)
        else:
            logging.warning(f"Skipping unknown message type in history for {uid} during save: {type(msg)}")

    data_to_save = {
        "state": state,
        "history": serializable_history
    }

    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error saving history for {uid}: {e}", exc_info=True)


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
def get_llm_response(text, sender_id, history_dicts=None, current_state="GENERAL_INQUIRY", retries=3):
    if not AI_MODEL:
        # This dictionary structure is for consistency with the expected final return.
        return {
            'response_text': "AI Model not configured.",
            'next_state': current_state # Return current_state if AI model fails
        }

    context_str = ""
    if current_state == "GENERAL_INQUIRY":
        vector_store = current_app.config.get('VECTOR_STORE')
        if vector_store:
            retrieved_docs = query_vector_store(text, vector_store, k=3)
            if retrieved_docs:
                context_str = "\n\nRelevant Information Found:\n" + "\n".join([doc.page_content for doc in retrieved_docs])
                logging.info(f"RAG generated context for GENERAL_INQUIRY: {context_str}")
            else:
                logging.info("RAG: No documents found from vector store for GENERAL_INQUIRY.")
        else:
            logging.warning("RAG: Vector store not available for GENERAL_INQUIRY.")

        # Generic message if RAG context is empty and it's a property query (only for GENERAL_INQUIRY)
        if not context_str and is_property_related_query(text):
            context_str = "\n\nRelevant Information Found:\nI currently don't have specific details for this property query from my documents. Please ask more general questions or I can try to help with other information."
            logging.info("No RAG context for property query in GENERAL_INQUIRY. Added generic message.")
    else:
        logging.info(f"Skipping RAG search as current_state is '{current_state}'.")

    # Constructing the prompt for the LLM
    # The BASE_PROMPT already instructs the LLM on how to use conversation_state and Relevant Information Found.
    # We just need to ensure the input text clearly labels these pieces of information.

    prompt_input_parts = [f"Current Conversation State: {current_state}"]
    if context_str: # Only add "Relevant Information Found" if it's not empty
        prompt_input_parts.append(context_str)
    prompt_input_parts.append(f"\nUser Question: {text}")

    final_prompt_to_llm = "\n".join(prompt_input_parts)

    if history_dicts is None:
        history_dicts = []

    messages = [SystemMessage(content=BASE_PROMPT)] + history_dicts + [HumanMessage(content=final_prompt_to_llm)]

    raw_llm_output = ""
    for attempt in range(retries):
        try:
            resp = AI_MODEL.invoke(messages)
            raw_llm_output = resp.content.strip()

            # Clean potential markdown fences if LLM wraps JSON in them
            if raw_llm_output.startswith("```json") and raw_llm_output.endswith("```"):
                # Remove the first line (```json) and last line (```)
                lines = raw_llm_output.splitlines()
                if len(lines) > 1: # Ensure there's content between the fences
                    raw_llm_output = "\n".join(lines[1:-1]).strip()
                else: # Handle case where it might be just ```json``` or similar
                    raw_llm_output = "" # Set to empty to cause JSONDecodeError handled below
            elif raw_llm_output.startswith("```") and raw_llm_output.endswith("```"):
                # More general case for just ``` wrapping
                 raw_llm_output = raw_llm_output[3:-3].strip()

            # Attempt to parse JSON
            parsed_llm_response = json.loads(raw_llm_output)
            response_text = parsed_llm_response.get("response_text")
            next_state = parsed_llm_response.get("next_state")

            if response_text is not None and next_state is not None:
                # Successfully parsed and keys are present
                logging.info(f"LLM JSON Response: response_text='{response_text}', next_state='{next_state}'")
                return {'response_text': response_text, 'next_state': next_state}
            else:
                missing_keys = []
                if response_text is None: missing_keys.append("response_text")
                if next_state is None: missing_keys.append("next_state")
                logging.error(f"LLM response missing critical keys: {', '.join(missing_keys)}. Raw: '{raw_llm_output}'")

        except json.JSONDecodeError as jde:
            logging.error(f"Failed to parse LLM JSON response on attempt {attempt+1}. Error: {jde}. Raw: '{raw_llm_output}'")
        except Exception as e:
            logging.warning(f"LLM API error or unexpected issue on attempt {attempt+1}/{retries}: {e}. Raw output: '{raw_llm_output}'")

        if attempt + 1 < retries:
            time.sleep((2 ** attempt))
        else: # Last attempt failed
            logging.error(f"All {retries} attempts to get valid LLM response failed. Raw output on last attempt: '{raw_llm_output}'")
            break # Exit loop after last attempt

    # Default response if all retries fail or parsing errors persist
    default_response_text = "أواجه صعوبة في فهم طلبك حالياً. هل يمكنك إعادة صياغته؟"
    default_next_state = "GENERAL_INQUIRY"
    logging.info(f"Returning default response: response_text='{default_response_text}', next_state='{default_next_state}'")
    return {'response_text': default_response_text, 'next_state': default_next_state}

# ─── Health Check Endpoint ─────────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def health_check():
    return jsonify(status="healthy", message="Application is running."), 200

# ─── Google Docs Sync Webhook ──────────────────────────────────────────────────
@app.route('/webhook-google-sync', methods=['POST'])
def google_docs_webhook_sync():
    data = request.get_json()
    if not data:
        logging.error("Google Docs sync: No data received.")
        return jsonify(status="error", message="No data received"), 400

    logging.info(f"Google Docs sync: Received request data: {json.dumps(data)}")

    # --- Secret Token Validation ---
    EXPECTED_GOOGLE_TOKEN = os.getenv('GOOGLE_SYNC_SECRET_TOKEN')
    received_token = data.get('secretToken')

    if not EXPECTED_GOOGLE_TOKEN:
        logging.critical("Google Docs sync: GOOGLE_SYNC_SECRET_TOKEN is not set in environment. Cannot authenticate requests.")
        return jsonify(status="error", message="Authentication service not configured."), 500

    if not received_token or received_token != EXPECTED_GOOGLE_TOKEN:
        logging.warning(f"Google Docs sync: Unauthorized access attempt. Received token: '{received_token}'")
        return jsonify(status="error", message="Unauthorized: Invalid or missing secret token"), 401

    document_id = data.get('documentId')
    if not document_id:
        logging.error(f"Google Docs sync: 'documentId' missing from request after token validation: {json.dumps(data)}")
        return jsonify(status="error", message="'documentId' is required"), 400

    logging.info(f"Google Docs sync: Authorized request for documentId: {document_id}")

    # --- RAG Processing Logic ---
    try:
        vector_store = current_app.config.get('VECTOR_STORE')
        embeddings = current_app.config.get('EMBEDDINGS')

        if not vector_store or not embeddings:
            logging.critical("Google Docs sync: RAG components (vector store or embeddings) not found in app config.")
            return jsonify(status="error", message="RAG system not configured properly."), 500

        # 1. Fetch Google Doc content
        logging.info(f"Google Docs sync: Fetching content for documentId: {document_id}")
        text_content = get_google_doc_content(document_id)

        if text_content is None:
            logging.error(f"Google Docs sync: Failed to fetch content for documentId: {document_id}. get_google_doc_content returned None.")
            return jsonify(status="error", message=f"Failed to fetch content for document {document_id}."), 500

        # If content is empty string, it might be an empty doc, which is fine.
        logging.info(f"Google Docs sync: Successfully fetched content for documentId: {document_id}. Content length: {len(text_content)}")

        # 2. Process and sync the document text with the RAG system
        logging.info(f"Google Docs sync: Processing documentId: {document_id} with RAG system.")
        sync_success = process_google_document_text(
            document_id=document_id,
            text_content=text_content,
            vector_store=vector_store,
            embeddings=embeddings
        )

        if sync_success:
            logging.info(f"Google Docs sync: Successfully processed and synced documentId: {document_id}")
            return jsonify(status="success", message=f"Document {document_id} processed and synced."), 200
        else:
            logging.error(f"Google Docs sync: Failed to process/sync documentId: {document_id} using process_google_document_text.")
            return jsonify(status="error", message=f"Failed to process/sync document {document_id}."), 500

    except Exception as e:
        logging.exception(f"Google Docs sync: An unexpected error occurred while processing documentId: {document_id}: {e}")
        return jsonify(status="error", message=f"An unexpected error occurred while processing document {document_id}."), 500

# ─── Main Webhook Handler ──────────────────────────────────────────────────────
@app.route('/hook', methods=['POST'])
def webhook():
    global is_globally_paused, paused_conversations, active_conversations_during_global_pause
    try:
        data = request.json or {}
        incoming_messages = data.get('messages', [])
        if not incoming_messages:
            return jsonify(status='success_no_messages'), 200

        for message in incoming_messages:
            if message.get('from_me'):
                continue

            sender = message.get('from') # Original line
            if not sender: # Add a check for sender validity
                logging.warning("Webhook: Message received without a 'from' field. Skipping.")
                continue
            sender = format_target_user_id(sender) # Normalize the sender ID here

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
            logging.info(f"Webhook: Processing message from sender: {sender}, body: '{normalized_body}'")
            # Removed global declarations from here

            if normalized_body == "stop all":
                is_globally_paused = True
                # active_conversations_during_global_pause is intentionally NOT cleared
                send_whatsapp_message(sender, "Bot is now globally paused. Individually started conversations will continue.")
                continue
            if normalized_body == "start all":
                is_globally_paused = False
                paused_conversations.clear()
                active_conversations_during_global_pause.clear()
                send_whatsapp_message(sender, "Bot is now globally resumed for all conversations.")
                continue
            if normalized_body.startswith("stop "):
                target_user_input = normalized_body.split("stop ", 1)[1].strip()
                if target_user_input:
                    target_user_id = format_target_user_id(target_user_input)
                    logging.info(f"COMMAND 'stop {target_user_input}': target_user_id: {target_user_id}")
                    logging.info(f"COMMAND 'stop {target_user_input}': BEFORE: paused_conversations: {paused_conversations}, active_conversations_during_global_pause: {active_conversations_during_global_pause}")
                    paused_conversations.add(target_user_id)
                    active_conversations_during_global_pause.discard(target_user_id) # Remove if present
                    logging.info(f"COMMAND 'stop {target_user_input}': AFTER: paused_conversations: {paused_conversations}, active_conversations_during_global_pause: {active_conversations_during_global_pause}")
                    send_whatsapp_message(sender, f"Bot interactions will be paused for: {target_user_id}")
                continue
            if normalized_body.startswith("start "):
                target_user_input = normalized_body.split("start ", 1)[1].strip()
                if target_user_input:
                    target_user_id = format_target_user_id(target_user_input)
                    logging.info(f"COMMAND 'start {target_user_input}': target_user_id: {target_user_id}, is_globally_paused: {is_globally_paused}")
                    logging.info(f"COMMAND 'start {target_user_input}': BEFORE: paused_conversations: {paused_conversations}, active_conversations_during_global_pause: {active_conversations_during_global_pause}")
                    if is_globally_paused:
                        active_conversations_during_global_pause.add(target_user_id)
                        paused_conversations.discard(target_user_id) # Ensure it's not in both
                        send_whatsapp_message(sender, f"Bot interactions will be resumed for: {target_user_id}. Other conversations remain paused.")
                    else:
                        paused_conversations.discard(target_user_id)
                        # active_conversations_during_global_pause.discard(target_user_id) # Not strictly necessary here but good for consistency
                        send_whatsapp_message(sender, f"Bot interactions will be resumed for: {target_user_id}")
                    logging.info(f"COMMAND 'start {target_user_input}': AFTER: paused_conversations: {paused_conversations}, active_conversations_during_global_pause: {active_conversations_during_global_pause}")
                continue

            logging.info(f"Webhook: PRE-SKIP CHECK for sender: {sender}")
            logging.info(f"Webhook: PRE-SKIP CHECK: is_globally_paused: {is_globally_paused}")
            logging.info(f"Webhook: PRE-SKIP CHECK: paused_conversations: {repr(paused_conversations)}")
            logging.info(f"Webhook: PRE-SKIP CHECK: active_conversations_during_global_pause: {repr(active_conversations_during_global_pause)}")
            # Check if conversation is individually paused
            if sender in paused_conversations:
                continue

            # Check if globally paused AND this sender is not specifically allowed
            if is_globally_paused and sender not in active_conversations_during_global_pause:
                continue

            user_id = ''.join(c for c in sender if c.isalnum())
            history_messages, current_conversation_state = load_history(user_id)
            
            # Pass current_conversation_state to get_llm_response
            llm_output = get_llm_response(body_for_fallback, sender, history_messages, current_conversation_state)

            response_text_to_user = llm_output['response_text']
            next_conversation_state = llm_output['next_state']

            # Check if the response_text is an ACTION_SEND_IMAGE_GALLERY command
            if response_text_to_user.startswith("[ACTION_SEND_IMAGE_GALLERY]"):
                lines = response_text_to_user.splitlines()
                # Ensure there are at least 3 lines: ACTION_SEND_IMAGE_GALLERY, url, caption
                if len(lines) >= 2: # Expecting at least marker and one URL. Caption is optional.
                    urls = [line for line in lines[1:-1] if line.strip().startswith('http')]
                    caption = lines[-1] if len(lines) > 1 and not lines[-1].strip().startswith('http') else "Here are the images:"

                    if urls: # Check if any URLs were actually extracted
                        logging.info(f"Sending gallery to {sender} based on LLM response.")
                        for i, url in enumerate(urls):
                            # Send caption only with the first image, or if it's the only content after URLs
                            current_caption = caption if i == 0 else ""
                            send_whatsapp_image_message(sender, current_caption, url)
                            time.sleep(1.5) # Stagger messages
                        final_model_response_for_history = f"[Sent gallery of {len(urls)} images with caption: '{caption}']"
                    else: # Fallback if parsing gallery from LLM fails
                        logging.warning(f"LLM indicated gallery, but no URLs found or format was incorrect: {response_text_to_user}")
                        send_whatsapp_message(sender, "I tried to send images, but there was an issue. Please try again.")
                        final_model_response_for_history = "[Attempted to send gallery, but failed due to formatting]"
                else: # Fallback if format is incorrect
                    logging.warning(f"LLM indicated gallery, but format was incorrect: {response_text_to_user}")
                    send_whatsapp_message(sender, "I tried to send images, but there was an issue. Please try again.")
                    final_model_response_for_history = "[Attempted to send gallery, but failed due to formatting]"
            else:
                # Standard text response
                final_model_response_for_history = response_text_to_user
                chunks = split_message(response_text_to_user)
                for chunk in chunks:
                    send_whatsapp_message(sender, chunk)
                    time.sleep(1) # Stagger messages

            # Append Langchain message objects to history
            history_messages.append(HumanMessage(content=body_for_fallback))
            history_messages.append(AIMessage(content=final_model_response_for_history)) # Save the actual action/text

            # Trim history
            if len(history_messages) > MAX_HISTORY_TURNS_TO_LOAD * 2:
                history_messages = history_messages[-(MAX_HISTORY_TURNS_TO_LOAD * 2):]

            # Save history with the new state returned by the LLM
            save_history(user_id, history_messages, next_conversation_state)

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
        embeddings_rag = OpenAIEmbeddings(model="text-embedding-3-large", openai_api_key=os.getenv('OPENAI_API_KEY'))
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
