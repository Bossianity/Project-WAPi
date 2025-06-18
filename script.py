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
import threading

# Import other custom modules
from interactive_messages import initial_greeting_message_components, owner_options_message_components, furnished_apartment_message_components, unfurnished_apartment_message_components, tenant_options_message_components
from rag_handler import initialize_vector_store, process_google_document_text, query_vector_store
from google_drive_handler import get_google_doc_content, get_google_sheet_content, get_google_drive_file_mime_type
from outreach_handler import process_outreach_campaign
from whatsapp_utils import send_whatsapp_message, send_whatsapp_image_message, set_webhook, send_interactive_list_message, send_interactive_button_message

# --- Load Environment and Global State ---
load_dotenv()

IS_APP_INITIALIZED = False
STALE_MESSAGE_THRESHOLD_SECONDS = 90
is_globally_paused = True
paused_conversations = set()
active_conversations_during_global_pause = set()
users_in_interactive_flow = set()
CONV_DIR = 'conversations'
MAX_HISTORY_TURNS_TO_LOAD = 6
os.makedirs(CONV_DIR, exist_ok=True)
AI_MODEL = None

# --- Flask App Definition ---
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
executor = ThreadPoolExecutor(max_workers=2)

# --- Helper Functions ---
def load_conversation_history(sender_id):
    """Load conversation history from file"""
    history_file = os.path.join(CONV_DIR, f"{sender_id}.json")
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('messages', [])[-MAX_HISTORY_TURNS_TO_LOAD:]
        except Exception as e:
            logging.error(f"Error loading history for {sender_id}: {e}")
    return []

def save_conversation_history(sender_id, messages):
    """Save conversation history to file"""
    history_file = os.path.join(CONV_DIR, f"{sender_id}.json")
    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump({'messages': messages}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Error saving history for {sender_id}: {e}")

def get_llm_response(user_message, sender_id):
    """Get response from LLM with context"""
    global AI_MODEL
    
    if not AI_MODEL:
        logging.error("AI_MODEL not initialized")
        return "عذراً، الخدمة غير متاحة حالياً. يرجى المحاولة لاحقاً."
    
    try:
        # Load conversation history
        history = load_conversation_history(sender_id)
        
        # Build context with RAG if available
        context = ""
        if current_app.config.get('VECTOR_STORE') and current_app.config.get('EMBEDDINGS'):
            try:
                context = query_vector_store(user_message, current_app.config['VECTOR_STORE'], current_app.config['EMBEDDINGS'])
            except Exception as e:
                logging.warning(f"RAG query failed: {e}")
        
        # Build message chain
        messages = [SystemMessage(content=BASE_PROMPT)]
        
        if context:
            messages.append(SystemMessage(content=f"Context: {context}"))
        
        # Add conversation history
        for msg in history:
            if msg.get('role') == 'user':
                messages.append(HumanMessage(content=msg.get('content', '')))
            elif msg.get('role') == 'assistant':
                messages.append(AIMessage(content=msg.get('content', '')))
        
        # Add current message
        messages.append(HumanMessage(content=user_message))
        
        # Get AI response
        response = AI_MODEL.invoke(messages)
        return response.content
        
    except Exception as e:
        logging.error(f"Error getting LLM response: {e}")
        return "عذراً، حدث خطأ. يرجى المحاولة مرة أخرى."

def is_greeting_message(text):
    """Check if message is a greeting"""
    if not text:
        return False
    
    text_lower = text.lower().strip()
    arabic_greetings = ['السلام عليكم', 'السلام', 'مرحبا', 'أهلا', 'هلا', 'صباح الخير', 'مساء الخير']
    english_greetings = ['hello', 'hi', 'hey', 'good morning', 'good evening', 'greetings']
    
    return any(greeting in text_lower for greeting in arabic_greetings + english_greetings)

def handle_interactive_button_click(sender, button_id):
    """Handle interactive button clicks"""
    try:
        if button_id == "owner_services":
            send_interactive_button_message(sender, owner_options_message_components)
        elif button_id == "guest_booking":
            send_interactive_button_message(sender, tenant_options_message_components)
        elif button_id == "furnished_apartment":
            send_interactive_button_message(sender, furnished_apartment_message_components)
        elif button_id == "unfurnished_apartment":
            send_interactive_button_message(sender, unfurnished_apartment_message_components)
        else:
            # Handle other button IDs or fall back to LLM
            response = get_llm_response(f"User clicked button: {button_id}", sender)
            send_whatsapp_message(sender, response)
    except Exception as e:
        logging.error(f"Error handling button click {button_id} for {sender}: {e}")
        send_whatsapp_message(sender, "عذراً، حدث خطأ. يرجى المحاولة مرة أخرى.")

# --- Persona Configuration ---
PERSONA_NAME = "مساعد"
BASE_PROMPT = (
    "You are Mosaed (مساعد), the AI assistant for Sakin Al-Awja Property Management (سكن العوجا لإدارة الأملاك). Your tone is friendly and professional, using a natural Saudi dialect of Arabic. Use a variety of welcoming phrases like 'حياك الله', 'بخدمتك', 'أبشر', 'سم', 'تفضل', or 'تحت أمرك' to sound natural. Also, try to sound smart when they ask you if you are a bot or something similar that is unrelated to property rentals, do not give rigid responses, this is the only exception to the rule for answering using given context only."
    
    "CRITICAL RULE: Always reply in the SAME language as the user's last message. If they use Arabic, you must use Arabic."

    "Your primary goal is to determine the user's intent: are they a **Property Owner** wanting management/furnishing services, or a **Guest** looking to book a daily rental?"

    "--- START OF SCENARIOS ---"

    "**SCENARIO 1: The user is a Property Owner.**"
    "If the user asks about 'تشغيل', 'إدارة أملاك' (property management), or how to list their property with you, you MUST follow this sequence precisely:"

    "1.  **First, and most importantly, ask if the unit is furnished.** Your exact response must be:"
        "حياك الله، بخدمتك دوم! قبل كل شيء ودي أعرف، الوحدة مؤثثة أو لا؟"

    "2.  **If the user says it is NOT furnished ('غير مؤثثة'):**"
        "   Respond with the following text and link. Do not change the wording."
        "   'ولا يهمك، عندنا خدمة تأثيث بمعايير فندقية وأسعار تنافسية، مهندسينا خبرتهم أكثر من 8 سنوات ومنفذين فوق 500 مشروع. عبّ النموذج ونرجع لك بتصميم يناسب وحدتك: https://form.typeform.com/to/vDKXMSaQ'"

    "3.  **If the user says it IS furnished ('مؤثثة'):**"
        "   First, respond with: 'ممتاز! أبي منك بعض المعلومات عشان نخدمك بأفضل شكل.'"
        "   Then, ask the following questions ONE BY ONE. Wait for the user's answer before asking the next question."
        "   -   'مساحة الوحدة؟' (Unit area?)"
        "   -   'في أي حي؟' (In which neighborhood?)"
        "   -   'هل سبق تم تأجيرها من قبل؟' (Has it been rented out before?)"
        "   -   'هل متوفر فيها دخول ذاتي أو مدخل ذكي؟' (Does it have self-check-in or a smart lock?)"
        "   **After you have received answers to all questions**, you must provide the final instructions and link:"
        "   'بعد ما علمتني هالتفاصيل، عبي النموذج التالي عشان نبدأ إجراءات التشغيل: https://form.typeform.com/to/eFGv4yhC'"
    
    "4.  **Property Owner FAQ:** If the owner asks other questions, use these answers:"
        "   -   About the service: 'حنا ندير الوحدة كاملة: من التسويق والتسعير إلى استقبال الضيوف والتنظيف. أرباحك توصلك أول كل شهر، بعقد واضح بدون عمولات خفية.'"
        "   -   About security: 'جميع وحداتنا فيها نظام دخول ذاتي آمن، وكل ضيف له رمز دخول خاص به.'"
        "   -   About expected profit: 'يعتمد الدخل على مساحة الوحدة، موقعها وتجهيزاتها. لو حاب تفاصيل أكثر، ممكن نحجز لك مكالمة نناقش فيها كل التفاصيل.'"

    "**SCENARIO 2: The user is a Guest looking to book.**"
    "If the user asks about booking, availability, prices for a stay, or property details, follow this workflow:"

    "1.  **Use the Property Listings:** Analyze the user's request for filters (price, location, guests). Use the retrieved property information to answer their questions directly."
    "2.  **Collect Booking Details:** If they want to book, collect the required information: specific property, check-in/check-out dates, and number of guests."
    "3.  **Confirm and Handoff:** Once you have these details, respond with: 'Thank you. I have your details for the booking. Our team will verify the availability and contact you shortly to confirm.'"
    "4.  **Handle Media:** If the context has `[ACTION_SEND_IMAGE_GALLERY]` and the user asks for photos, your entire response must be ONLY that block. If it has `[VIDEO_LINK]`, include it naturally in your text."

    "--- END OF SCENARIOS ---"
    
    "If the user's intent is unclear, ask for clarification: 'حياك الله! هل تبحث عن حجز إقامة لدينا، أو أنت مالك عقار ومهتم بخدماتنا لإدارة الأملاك؟' (Welcome! Are you looking to book a stay, or are you a property owner interested in our management services?)"
    "TEXT RULES: No emojis, no markdown (*, _, etc.). Use only clean plain text."
)

# --- Webhook Handlers ---
@app.route('/', methods=['GET'])
def health_check():
    return jsonify(status="healthy", message="Application is running."), 200

@app.route('/hook', methods=['POST'])
def webhook():
    global is_globally_paused, paused_conversations, active_conversations_during_global_pause

    if not IS_APP_INITIALIZED:
        logging.warning("Webhook received before app is fully initialized. Responding with 503.")
        return jsonify(status="error", message="Service is initializing"), 503

    try:
        data = request.json or {}
        incoming_messages = data.get('messages', [])
        
        if not incoming_messages:
            return jsonify(status='success_no_messages'), 200

        for message in incoming_messages:
            # Skip messages from bot itself
            if message.get('from_me'):
                continue
            
            sender = message.get('from')
            if not sender:
                continue
            
            # Stale message check
            message_timestamp = message.get('t')
            if message_timestamp:
                try:
                    message_dt = datetime.utcfromtimestamp(int(message_timestamp))
                    if (datetime.utcnow() - message_dt).total_seconds() > STALE_MESSAGE_THRESHOLD_SECONDS:
                        logging.warning(f"Ignoring stale message from {sender}.")
                        continue
                except (ValueError, TypeError):
                    logging.warning(f"Could not parse timestamp for stale check.")

            # Parse message content
            msg_type = message.get('type')
            body_for_fallback = None
            clicked_button_id = None
            
            if msg_type == 'text':
                body_for_fallback = message.get('text', {}).get('body')
            elif msg_type == 'interactive':
                interactive_data = message.get('interactive', {})
                if interactive_data.get('type') == 'button_reply':
                    clicked_button_id = interactive_data.get('button_reply', {}).get('id')
                    body_for_fallback = f"[Clicked: {interactive_data.get('button_reply', {}).get('title')}]"
                elif interactive_data.get('type') == 'list_reply':
                    clicked_button_id = interactive_data.get('list_reply', {}).get('id')
                    body_for_fallback = f"[Selected: {interactive_data.get('list_reply', {}).get('title')}]"
            
            if not body_for_fallback:
                logging.warning(f"No processable content in message from {sender}")
                continue

            # Handle Admin Commands
            normalized_body = body_for_fallback.lower().strip()
            
            if normalized_body == "stop all":
                is_globally_paused = True
                active_conversations_during_global_pause.clear()
                logging.info("Global pause activated by admin command")
                continue
            elif normalized_body == "start all":
                is_globally_paused = False
                paused_conversations.clear()
                active_conversations_during_global_pause.clear()
                logging.info("Global pause deactivated by admin command")
                continue
            elif normalized_body.startswith("stop "):
                user_id = normalized_body[5:].strip()
                paused_conversations.add(user_id)
                active_conversations_during_global_pause.discard(user_id)
                logging.info(f"Paused conversation for user: {user_id}")
                continue
            elif normalized_body.startswith("start "):
                user_id = normalized_body[6:].strip()
                paused_conversations.discard(user_id)
                if is_globally_paused:
                    active_conversations_during_global_pause.add(user_id)
                logging.info(f"Resumed conversation for user: {user_id}")
                continue

            # Check if conversation is paused
            if sender in paused_conversations:
                logging.info(f"Ignoring message from paused conversation: {sender}")
                continue
            
            if is_globally_paused and sender not in active_conversations_during_global_pause:
                logging.info(f"Ignoring message due to global pause: {sender}")
                continue

            # Handle interactive button clicks
            if clicked_button_id:
                logging.info(f"Processing button click: {clicked_button_id} from {sender}")
                handle_interactive_button_click(sender, clicked_button_id)
                continue

            # Handle greeting messages
            if is_greeting_message(body_for_fallback):
                logging.info(f"Sending greeting message to {sender}")
                try:
                    send_interactive_button_message(sender, initial_greeting_message_components)
                    users_in_interactive_flow.add(sender)
                except Exception as e:
                    logging.error(f"Error sending greeting to {sender}: {e}")
                    # Fallback to text message
                    send_whatsapp_message(sender, "حياك الله! كيف ممكن أساعدك اليوم؟")
                continue

            # Process regular messages with LLM
            try:
                logging.info(f"Processing message from {sender}: {body_for_fallback[:100]}...")
                
                # Get LLM response
                response = get_llm_response(body_for_fallback, sender)
                
                # Check for special action tags in response
                if "[ACTION_SEND_IMAGE_GALLERY]" in response:
                    # Handle image gallery request
                    try:
                        # You would implement the image sending logic here
                        # For now, just send the response without the action tag
                        clean_response = response.replace("[ACTION_SEND_IMAGE_GALLERY]", "").strip()
                        if clean_response:
                            send_whatsapp_message(sender, clean_response)
                    except Exception as e:
                        logging.error(f"Error sending image gallery to {sender}: {e}")
                        send_whatsapp_message(sender, "عذراً، لا أستطيع إرسال الصور حالياً.")
                else:
                    # Send regular text response
                    send_whatsapp_message(sender, response)
                
                # Save conversation history
                history = load_conversation_history(sender)
                history.extend([
                    {"role": "user", "content": body_for_fallback, "timestamp": datetime.now().isoformat()},
                    {"role": "assistant", "content": response, "timestamp": datetime.now().isoformat()}
                ])
                save_conversation_history(sender, history)
                
            except Exception as e:
                logging.error(f"Error processing message from {sender}: {e}")
                try:
                    send_whatsapp_message(sender, "عذراً، حدث خطأ. يرجى المحاولة مرة أخرى.")
                except:
                    logging.error(f"Failed to send error message to {sender}")

        return jsonify(status='success', message='Webhook processed'), 200

    except Exception as e:
        logging.exception(f"FATAL Error in webhook processing: {e}")
        return jsonify(status='error', message='Internal Server Error'), 500

# --- Application Startup Logic ---
def initialize_app_state():
    """
    Handles all time-consuming initializations in a background thread.
    """
    global IS_APP_INITIALIZED, AI_MODEL

    with app.app_context():
        logging.info("Starting critical initializations in background...")
        APP_CONFIG = {
            "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
            "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
            "API_URL": os.getenv("API_URL"),
            "API_TOKEN": os.getenv("API_TOKEN"),
            "BOT_URL": os.getenv("BOT_URL")
        }

        # Initialize AI Model
        if APP_CONFIG["OPENAI_API_KEY"]:
            try:
                AI_MODEL = ChatOpenAI(
                    model='gpt-4o', 
                    openai_api_key=APP_CONFIG["OPENAI_API_KEY"], 
                    temperature=0.4
                )
                logging.info("ChatOpenAI model initialized successfully.")
            except Exception as e:
                logging.error(f"Failed to initialize ChatOpenAI model: {e}")
        else:
            logging.error("OPENAI_API_KEY not found in environment variables.")

        # Initialize RAG components
        try:
            if APP_CONFIG["OPENAI_API_KEY"]:
                embeddings_rag = OpenAIEmbeddings(
                    model="text-embedding-ada-002", 
                    openai_api_key=APP_CONFIG["OPENAI_API_KEY"]
                )
                vector_store_rag = initialize_vector_store()
                
                if vector_store_rag and embeddings_rag:
                    app.config['EMBEDDINGS'] = embeddings_rag
                    app.config['VECTOR_STORE'] = vector_store_rag
                    logging.info("RAG components initialized successfully.")
                else:
                    logging.error("Failed to initialize RAG components.")
            else:
                logging.warning("Skipping RAG initialization due to missing OpenAI API key.")
        except Exception as e:
            logging.error(f"Error during RAG initialization: {e}")

        # Mark the app as ready to handle requests
        IS_APP_INITIALIZED = True
        logging.info("Application is now fully initialized and ready to accept webhooks.")

        # Run non-critical deferred tasks
        try:
            time.sleep(2)
            logging.info("Running non-critical deferred startup tasks...")
            if APP_CONFIG.get("BOT_URL") and APP_CONFIG.get("API_URL") and APP_CONFIG.get("API_TOKEN"):
                set_webhook(APP_CONFIG["BOT_URL"], APP_CONFIG["API_URL"], APP_CONFIG["API_TOKEN"])
                logging.info("Webhook set successfully.")
            else:
                logging.warning("Skipping webhook setup due to missing configuration.")
            logging.info("Deferred startup tasks completed.")
        except Exception as e:
            logging.error(f"Error in deferred startup tasks: {e}")

# --- Main Execution Block ---
if __name__ != '__main__':
    # For production (Gunicorn/Waitress)
    init_thread = threading.Thread(target=initialize_app_state)
    init_thread.daemon = True
    init_thread.start()

if __name__ == '__main__':
    # For local development
    init_thread = threading.Thread(target=initialize_app_state)
    init_thread.daemon = True
    init_thread.start()
    port = int(os.getenv('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
