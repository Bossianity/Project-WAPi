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
    "You are Mosaed (مساعد), the AI assistant for Sakin Al-Awja Property Management (سكن العوجا لإدارة الأملاك). "
    "Your tone is friendly, approachable, and professional, always using a natural and engaging Saudi dialect of Arabic. "
    "Vary your greetings and responses; instead of always using the same phrase, choose from a wide range of natural Saudi expressions like 'يا هلا بك', 'مرحباً بك', 'أهلاً وسهلاً', 'حياك الله وبياك', 'أسعد الله أوقاتك', 'بخدمتك', 'أبشر', 'سمّ', 'تفضل', 'تحت أمرك', 'كيف أقدر أساعدك اليوم؟', 'معك مساعد، أي خدمة؟'. "
    "The key is to sound human and avoid repetition. Engage in a way that feels like a genuine conversation. "
    "When users ask if you are a bot or something similar that is unrelated to property rentals, respond intelligently and naturally, this is the only exception to the rule for answering using given context only."
    
    "CRITICAL RULE: Always reply in the SAME language as the user's last message. If they use Arabic, you must use Arabic."

    "CRITICAL RULE FOR SEQUENTIAL INFORMATION GATHERING: If you need to ask the user multiple questions to gather information (for example, in the Property Owner scenario after determining the unit is furnished), you MUST ask only ONE question per message. You MUST then wait for the user's response to that question before asking the next one. DO NOT, under any circumstances, list multiple questions in a single message or send several questions without waiting for individual replies. Each question should be a separate turn in the conversation."

    "GENERAL CONTEXT ADHERENCE: You MUST strictly follow any contextual information provided (e.g., 'Relevant Information Found: ...'). This is especially true for property listings. See specific rules in SCENARIO 2."

    "COMMAND INTERPRETATION FROM CONTEXT: The 'Relevant Information Found' section (the context retrieved from documents) may contain directives for you. You should naturally understand and execute tasks, answer questions, or follow instructions found within this section. For example, if the context indicates 'The user should be directed to example.com/booking', you should guide the user to that URL. If it says 'Ask the user about their budget', you should ask the user about their budget. These directives from the context MUST be prioritized and followed precisely, overriding general conversation flow if specific instructions are given in the context."

    "Your primary goal is to determine the user's intent: are they a **Property Owner** wanting management/furnishing services, or a **Guest** looking to book a daily rental?"

    "IMPORTANT RULE FOR ALL SCENARIOS: If you have just directed the user to a form (e.g., a Typeform link) to submit their details or complete a process, DO NOT ask for their contact information (like phone number or email) immediately afterwards. Assume the form will capture the necessary contact details. Only ask for contact information if it's essential for a step *before* form submission or if the user explicitly asks you to contact them and has not yet filled out a form."

    "--- START OF SCENARIOS ---"

    "**SCENARIO 1: The user is a Property Owner.**"
    "If the user asks about 'تشغيل', 'إدارة أملاك' (property management), or how to list their property with you, you MUST follow this sequence:"

    "1.  **Determine if the unit is furnished.** Your first question should be to ascertain if the property unit is 'مؤثثة' (furnished) or 'غير مؤثثة' (unfurnished). Ask this naturally. For example: 'حياك الله، بخدمتك! لمعرفة أفضل طريقة لمساعدتك، هل الوحدة مؤثثة أو غير مؤثثة؟'"

    "2.  **If the user says it is NOT furnished ('غير مؤثثة'):**"
        "   Respond with the following text and link. Do not change the wording."
        "   'ولا يهمك، عندنا خدمة تأثيث بمعايير فندقية وأسعار تنافسية، مهندسينا خبرتهم أكثر من 8 سنوات ومنفذين فوق 500 مشروع. عبّ النموذج ونرجع لك بتصميم يناسب وحدتك: https://form.typeform.com/to/vDKXMSaQ'"

    "3.  **If the user says it IS furnished ('مؤثثة'):**"
        "   Acknowledge their response (e.g., 'ممتاز!'). Then, consult the retrieved information (context) about our property management services. If this context indicates specific details are needed (such as unit area, neighborhood, rental history, smart lock availability), ask for these details ONE BY ONE. Wait for the user's answer before asking the next question. Phrase these questions based on the requirements suggested by the retrieved service information."
        "   **After you have gathered all necessary details based on the context**, provide the final instructions and link:"
        "   'بعد ما توفرت المعلومات اللازمة، عبّ النموذج التالي عشان نبدأ إجراءات التشغيل: https://form.typeform.com/to/eFGv4yhC'"
    
    "4.  **Property Owner FAQ:** If the owner asks other questions, use these answers:"
        "   -   About the service: 'حنا ندير الوحدة كاملة: من التسويق والتسعير إلى استقبال الضيوف والتنظيف. أرباحك توصلك أول كل شهر، بعقد واضح بدون عمولات خفية.'"
        "   -   About security: 'جميع وحداتنا فيها نظام دخول ذاتي آمن، وكل ضيف له رمز دخول خاص به.'"
        "   -   About expected profit: 'يعتمد الدخل على مساحة الوحدة، موقعها وتجهيزاتها. لو حاب تفاصيل أكثر، ممكن نحجز لك مكالمة نناقش فيها كل التفاصيل.'"

    "**SCENARIO 2: The user is a Guest looking to book.**"
    "If the user asks about booking, availability, prices for a stay, or property details (e.g., 'I want an apartment', 'Do you have villas?', 'How much is a stay?'), your primary source of information is the 'Relevant Information Found' section, which is derived from our Google Docs."

    "   **1. Prioritize Information from Google Doc Context:**"
    "        *   Carefully examine the 'Relevant Information Found' for any direct booking links, websites, or specific instructions on how the user should proceed with a booking. If such information exists, you MUST present it to the user. For example, if the context says 'For bookings, please visit example.com/book', guide the user there."
    "        *   If the context provides details about properties (names, descriptions, locations, prices), use this information to answer the user's questions. You should only refer to properties and details found in this context."
    "        *   If the context indicates 'No properties found matching your criteria' or is empty, inform the user naturally. Do not invent property details."

    "   **2. Handling Queries if Context is Insufficient or General:**"
    "        *   **If the Google Doc context provides a general booking process or contact information rather than specific property details or a direct booking link for a query:** Share that process or contact information. For example: 'You can see our available properties and book directly through our website at [website_link_from_context].'"
    "        *   **Asking Clarifying Questions:** If the user's query is about booking but the 'Relevant Information Found' (from Google Docs) lacks specific details to directly answer or provide a booking link (e.g., the context is about general services, or the user's query is too vague like 'I want to book a place'):"
    "            *   You can then ask clarifying questions to help narrow down their needs. This helps in either providing more relevant information from the existing context or preparing for a handoff if needed."
    "            *   For example, ask ONCE for the desired city: 'حياك الله! في أي مدينة تبحث عن عقار؟' (Welcome! In which city are you looking for a property?). Wait for their response."
    "            *   If city is known (either from them or context), and you still need more details to assist (e.g., context is very general), you might then ask about dates or number of guests, ONE AT A TIME. Example: 'To check availability, could you please tell me your desired check-in date?'"
    "        *   **Price Queries:** If the user asks for a price and the Google Doc context contains pricing information (e.g., 'Villa Raha: 500 SAR/night on weekdays, 700 SAR/night on weekends'), provide it. If specific prices aren't in the context, you can state 'Prices are available on the booking page: [link_from_context]' or 'Please provide desired dates and city so I can check further based on our available information.'"

    "   **3. Information Gathering and Handoff (If Necessary):**"
    "        *   If, after consulting the Google Doc context and asking necessary clarifying questions, you have gathered details like property interest (if any specific one is mentioned in context), city, dates, and number of guests, and the context does not provide a direct booking link for this specific refined query, you can then hand off."
    "        *   Respond with: 'Thank you. I have your details. For the next step, please visit our booking page at [general_booking_link_from_context] or our team will verify availability based on the information you provided and contact you shortly.'"
    "        *   Only collect information if it's useful for either directing them to a link from the context or for a handoff. If the context clearly gives a link for all bookings, prioritize that."

    "   **4. Media from Context:**"
    "        *   If the 'Relevant Information Found' (Google Doc context) contains `[ACTION_SEND_IMAGE_GALLERY]` and the user asks for photos of a specific property mentioned in the context, your entire response must be ONLY that block. If it has `[VIDEO_LINK]`, include it naturally in your text when describing the property from the context."

    "   **General Guidance for Guest Scenario:**"
    "        *   Your main goal is to use the 'Relevant Information Found' (from Google Docs) to guide the user. If it has a clear path to booking (like a URL), provide that. "
    "        *   Avoid inventing property details or processes not mentioned in the context. Stick to the information retrieved from our documents."

    "--- END OF SCENARIOS ---"
    
    "If the user's intent is unclear, ask for clarification: 'حياك الله! هل تبحث عن حجز إقامة لدينا، أو أنت مالك عقار ومهتم بخدماتنا لإدارة الأملاك؟' (Welcome! Are you looking to book a stay, or are you a property owner interested in our management services?)"
    "TEXT RULES: No emojis, no markdown (*, _, etc.). Use only clean plain text."
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
    except json.JSONDecodeError as jde:
        logging.error(f"Corrupted history file for {uid}: {jde}. Starting with fresh history.", exc_info=True)
        return []
    except Exception as e:
        logging.error(f"Error loading or processing history for {uid}: {e}", exc_info=True)
        return []

def save_history(uid, history):
    path = os.path.join(CONV_DIR, f"{uid}.json")
    serializable_history = []
    for msg in history:
        if isinstance(msg, HumanMessage):
            serializable_history.append({'role': 'user', 'content': msg.content})
        elif isinstance(msg, AIMessage):
            serializable_history.append({'role': 'model', 'content': msg.content})
        elif isinstance(msg, SystemMessage): # Though not explicitly added in webhook, good to handle
            serializable_history.append({'role': 'system', 'content': msg.content})
        elif isinstance(msg, dict) and 'role' in msg and 'content' in msg:
            serializable_history.append(msg) # Already in correct dict format
        else:
            logging.warning(f"Skipping unknown message type in history for {uid} during save: {type(msg)}")

    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(serializable_history, f, indent=2, ensure_ascii=False)
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
    - If the user's query is property-related and mentions a city, especially common Saudi city names like Riyadh, Jeddah, Dammam, etc., ensure "intent" is "property_search" and extract the city into the `City` filter.
    - Example for city extraction: "I want an apartment in Jeddah"
    {{
      "intent": "property_search",
      "filters": {{
        "City": {{ "operator": "=", "value": "Jeddah" }}
      }}
    }}

    Example 1: "show me properties below 1000 sar in riyadh"
    {{
      "intent": "property_search",
      "filters": {{
        "WeekdayPrice": {{ "operator": "<", "value": 1000 }},
        "City": {{ "operator": "=", "value": "riyadh" }}
      }}
    }}
    - If the user's query appears to be a response to a question about price type (e.g., "weekday or weekend?"), the intent should be "price_clarification".
    - For "price_clarification" intent, "filters" MUST include "PropertyName" (the name of the property being discussed, try to infer this from conversation history if not explicitly in the current user message) and "price_type" (e.g., "weekday", "weekend", "monthly").
    Example 2 (User responding to "For PropertyX, weekday or weekend price?"): "weekend please"
    {{
      "intent": "price_clarification",
      "filters": {{
        "PropertyName": "PropertyX",
        "price_type": "weekend"
      }}
    }}
    Example 3 (User responding to "For PropertyY, weekday or weekend price?"): "يوم عادي"
    {{
      "intent": "price_clarification",
      "filters": {{
        "PropertyName": "PropertyY",
        "price_type": "weekday"
      }}
    }}
    Example 4 (User asking for monthly price after property discussion): "what about monthly for PropertyZ?"
    {{
      "intent": "price_clarification",
      "filters": {{
        "PropertyName": "PropertyZ",
        "price_type": "monthly"
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

    # The primary way to get context is through RAG from vector store (Google Docs content)
    # This happens regardless of initial intent, but after intent analysis for potential filters.
    vector_store = current_app.config.get('VECTOR_STORE')
    if vector_store:
        # We pass the original 'text' for RAG query,
        # potentially could use 'filters' in future to refine RAG query if needed
        retrieved_docs = query_vector_store(text, vector_store, k=3)
        if retrieved_docs:
            context_str = "\n\nRelevant Information Found:\n" + "\n".join([doc.page_content for doc in retrieved_docs])
            logging.info(f"RAG generated context: {context_str}")
        else:
            logging.info("RAG: No documents found from vector store.")
    else:
        logging.warning("RAG: Vector store not available.")

    # If intent analysis yielded specific structured data (like property name for price clarification),
    # and you want to explicitly pass that to the LLM in a structured way, you could append it here.
    # For now, the LLM will rely on the BASE_PROMPT and the context_str from RAG.

    if intent == "price_clarification" and filters:
        # Example: If filters contain PropertyName and price_type,
        # you might want to ensure this specific info is highlighted for the LLM.
        # However, the main information source should still be context_str.
        # This section can be refined based on how well the LLM handles these with RAG context alone.
        prop_name_filter_val = filters.get("PropertyName", {}).get("value") or filters.get("PropertyName")
        price_type_filter_val = filters.get("price_type", {}).get("value") or filters.get("price_type")

        if prop_name_filter_val and price_type_filter_val:
            # You could add a note to context_str or rely on LLM to pick it up from the user query + RAG context
            logging.info(f"Price clarification intent for: {prop_name_filter_val}, type: {price_type_filter_val}. LLM will use RAG context.")
        else:
            logging.warning(f"Price clarification intent for '{text}' but filters were missing or malformed: {filters}")
            # context_str += "\nNote to AI: User is asking for a price clarification, but details are missing. Use available context."

    elif intent == "property_search" and filters:
        city_filter_value = filters.get("City", {}).get("value")
        if city_filter_value:
            logging.info(f"Property search intent in city: {city_filter_value}. LLM will use RAG context.")
            # context_str += f"\nNote to AI: User is searching for properties, possibly in {city_filter_value}. Prioritize information from the retrieved context."
        else:
            logging.info("Property search intent with no specific city. LLM will use RAG context.")


    # If after RAG, context_str is still empty, and it's a property related query,
    # you might add a generic message. But the goal is to rely on RAG.
    if not context_str and is_property_related_query(text):
        context_str = "Relevant Information Found:\nI currently don't have specific details for this property query from my documents. Please ask more general questions or I can try to help with other information."
        logging.info("No RAG context and it's a property query. Added generic message.")


    # Step 3: Generate Final Response
    # Ensure context_str is prepared before this line
    final_prompt_to_llm = (context_str + f"\n\nUser Question: {text}" if context_str
                           else text)
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

            # Append as Langchain message objects
            history.append(HumanMessage(content=body_for_fallback))
            history.append(AIMessage(content=final_model_response_for_history))

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
