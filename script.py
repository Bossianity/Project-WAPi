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

IS_APP_INITIALIZED = False  # Readiness flag to prevent processing during startup
STALE_MESSAGE_THRESHOLD_SECONDS = 90
is_globally_paused = True
paused_conversations = set()
active_conversations_during_global_pause = set()
users_in_interactive_flow = set()
CONV_DIR = 'conversations'
MAX_HISTORY_TURNS_TO_LOAD = 6
os.makedirs(CONV_DIR, exist_ok=True)


# --- Flask App Definition ---
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
executor = ThreadPoolExecutor(max_workers=2)


# --- All Helper Functions (load_history, save_history, etc.) ---
# These functions are unchanged and are defined here. Omitted for brevity.
# Make sure to include all your functions like:
# detect_language_from_text, prepare_interactive_message_data, BASE_PROMPT,
# load_history, save_history, get_llm_response, etc. in your actual file.
PERSONA_NAME = "مساعد"
BASE_PROMPT = (
    "You are Mosaed (مساعد), the AI assistant for Sakin Al-Awja Property Management (سكن العوجا لإدارة الأملاك). Your tone is friendly and professional, using a natural Saudi dialect of Arabic. Use a variety of welcoming phrases like 'حياك الله', 'بخدمتك', 'أبشر', 'سم', 'تفضل', or 'تحت أمرك' to sound natural. ALso, try to sound smart when they ask you if you are a bot or something similar that is unrelated to property rentals, do not give rigid responces, this is the only exception to the rule for answering using given context only"
    
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
        # The rest of your webhook logic is unchanged and goes here...
        # It is omitted for brevity.
        pass # Placeholder for your existing webhook code
    except Exception as e:
        logging.exception(f"FATAL Error in webhook processing: {e}")
        return jsonify(status='error', message='Internal Server Error'), 500


# --- Application Startup Logic ---
def initialize_app_state():
    """
    Function to handle all time-consuming initializations in a background thread.
    """
    global IS_APP_INITIALIZED, AI_MODEL

    with app.app_context():
        logging.info("Starting critical initializations in background...")

        # Get environment variables
        APP_CONFIG = {
            "OPENAI_API_KEY": os.getenv('OPENAI_API_KEY'),
            "GEMINI_API_KEY": os.getenv('GEMINI_API_KEY'),
            "API_URL": os.getenv('API_URL'),
            "API_TOKEN": os.getenv('API_TOKEN'),
            "BOT_URL": os.getenv('BOT_URL')
        }

        # 1. Initialize AI Model
        if APP_CONFIG["OPENAI_API_KEY"]:
            try:
                AI_MODEL = ChatOpenAI(model='gpt-4o', openai_api_key=APP_CONFIG["OPENAI_API_KEY"], temperature=0.4)
                logging.info("ChatOpenAI model initialized successfully.")
            except Exception as e:
                logging.error(f"Failed to initialize ChatOpenAI model: {e}", exc_info=True)
        else:
            logging.error("OPENAI_API_KEY not found; AI responses will fail.")

        # 2. Initialize RAG components
        try:
            embeddings_rag = OpenAIEmbeddings(model="text-embedding-ada-002", openai_api_key=APP_CONFIG["OPENAI_API_KEY"])
            vector_store_rag = initialize_vector_store()
            if vector_store_rag and embeddings_rag:
                app.config['EMBEDDINGS'] = embeddings_rag
                app.config['VECTOR_STORE'] = vector_store_rag
                logging.info("RAG components initialized and stored in app config.")
            else:
                logging.error("Failed to initialize RAG components.")
        except Exception as e:
            logging.critical(f"A critical error occurred during RAG initialization: {e}")

        # 3. Mark the app as fully initialized
        IS_APP_INITIALIZED = True
        logging.info("Application is now fully initialized and ready to accept webhooks.")

        # 4. Run non-critical deferred tasks
        time.sleep(2) # A short delay before setting the webhook
        logging.info("Running non-critical deferred startup tasks...")
        set_webhook(APP_CONFIG.get("BOT_URL"), APP_CONFIG.get("API_URL"), APP_CONFIG.get("API_TOKEN"))
        logging.info("Deferred startup tasks completed.")


# This block runs when the application starts
if __name__ != '__main__':
    # This condition is true when running with Gunicorn/Waitress on Render
    init_thread = threading.Thread(target=initialize_app_state)
    init_thread.daemon = True
    init_thread.start()

if __name__ == '__main__':
    # This block is for local development ONLY
    logging.warning("RUNNING IN LOCAL DEVELOPMENT MODE. DO NOT USE IN PRODUCTION.")
    init_thread = threading.Thread(target=initialize_app_state)
    init_thread.daemon = True
    init_thread.start()
    port = int(os.getenv('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
