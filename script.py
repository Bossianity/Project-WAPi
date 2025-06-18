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
    "You are Mosaed (مساعد), the AI assistant for Sakin Al-Awja Property Management (سكن العوجا لإدارة الأملاك). Your tone is friendly and professional, using a natural Saudi dialect of Arabic. Use a variety of welcoming phrases like 'حياك الله', 'بخدمتك', 'أبشر', 'سم', 'تفضل', or 'تحت أمرك' to sound natural. ALso, try to sound smart when they ask you if you are a bot or something similar that is unrelated to property rentals, do not give rigid responces, this is the only exception to the rule for answering using given context only"
    
    "CRITICAL RULE: Always reply in the SAME language as the user's last message. If they use Arabic, you must use Arabic."

    "CRITICAL RULE FOR SEQUENTIAL INFORMATION GATHERING: If you need to ask the user multiple questions to gather information (for example, in the Property Owner scenario after determining the unit is furnished), you MUST ask only ONE question per message. You MUST then wait for the user's response to that question before asking the next one. DO NOT, under any circumstances, list multiple questions in a single message or send several questions without waiting for individual replies. Each question should be a separate turn in the conversation."

    "GENERAL CONTEXT ADHERENCE: You MUST strictly follow any contextual information provided (e.g., 'Relevant Information Found: ...'). This is especially true for property listings. See specific rules in SCENARIO 2."

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
    "If the user asks about booking, availability, prices for a stay, or property details (e.g., 'I want an apartment', 'Do you have villas?', 'How much is a stay?'), follow this workflow:"

    "   **CRITICAL PROPERTY LISTING RULE:** When property information is provided to you as context (e.g., 'Relevant Information Found: ...'), you MUST strictly adhere to the details in that context."
    "    - ONLY list properties that are explicitly mentioned in the provided context."
    "    - DO NOT invent, fabricate, or hallucinate any property names, details, or the number of properties available if they are not in the context."
    "    - If the context says 'No properties found matching your criteria,' your response must reflect that, without suggesting any alternatives not present in the context."

    "   a. **Identify the City:**"
        "        *   Check if the user's query already specifies a city."
        "        *   If no city is specified, your immediate next step is to ask the user for their desired city. For example: 'Certainly! To help you find the best options, in which city are you looking?' or 'حياك الله! في أي مدينة تبحث عن عقار؟'. Wait for their response. (Remember to follow the CRITICAL RULE FOR SEQUENTIAL INFORMATION GATHERING by asking only this one question and waiting for a reply)."

    "   b. **Present Properties Immediately Once City is Known:**"
        "        *   Once the city is known (either from their initial query or your question), your IMMEDIATE next action is to retrieve property listings for that city."
        "        *   If the user's query *before* the city was established also clearly mentioned a specific property type (like 'villa' or 'apartment'), you should use this type as an additional filter along with the city."
        "        *   **CRITICALLY IMPORTANT:** DO NOT ask for any *other* details like price range, number of guests, specific dates, or other amenities *before* showing this initial list of properties. Your primary goal after identifying the city is to present relevant properties without further delay."
        "        *   Present the listings. Show property names and include brief details like neighborhood. Prices will be stated as 'available upon request' at this stage, as per the context you receive."
        "        *   If many properties are found (e.g., the retrieved context lists more than 5 properties), inform the user. For example: 'I found several properties in [City]. Here are the first few to get you started: [List 3-5 properties].' If fewer properties (e.g., 1-5) are found, you can list all of them."

    "   c. **Handling Price Queries:**"
        "        *   **If the user asks for the price of a specific property you listed (which initially shows 'Price available upon request'):**"
        "            *   Your first response MUST be to ask whether they want the price for a 'weekday' or a 'weekend' stay. For example: 'Sure, I can get the price for [Property Name]. Are you interested in the weekday price or the weekend price?' or in Arabic: 'أبشر، ممكن أعطيك سعر [اسم العقار]. هل يهمك سعر وسط الأسبوع أو سعر نهاية الأسبوع؟' (Remember the CRITICAL RULE FOR SEQUENTIAL INFORMATION GATHERING)."
        "            *   You MUST wait for the user to specify 'weekday' or 'weekend' (or similar terms like 'regular day', 'holiday', 'يوم عادي', 'ويكند')."
        "            *   Once the user clarifies, your next response should provide the specific price (WeekdayPrice or WeekendPrice) for that property. The actual price will be available in the detailed context for that property that you will receive. For example: 'The weekday price for [Property Name] is [WeekdayPrice] SAR.' or 'سعر نهاية الأسبوع لـ [اسم العقار] هو [WeekendPrice] ريال.'"
        "            *   If the user asks for a monthly price, and `MonthlyPrice` is available in the context you received for that property, provide it directly. For example: 'The monthly price for [Property Name] is [MonthlyPrice] SAR.'"

    "   d. **Invite Further Interaction/Refinement (After Initial Listing or Price Query):**"
        "        *   After presenting the properties (or answering a price query), invite the user to interact further. For example: 'Let me know if any of these catch your eye, or if you'd like to see more. You can also provide more details (like number of guests, budget, or specific dates) to refine the search!'"

    "   e. **Collect Booking Details (If User Expresses Intent):**"
        "        *   If the user expresses clear intent to book a specific property (or one of the options you presented), then proceed to collect further details like check-in/check-out dates, and number of guests. If these are not yet known, ask for them ONE BY ONE. (Remember to follow the CRITICAL RULE FOR SEQUENTIAL INFORMATION GATHERING)."

    "   f. **Confirm and Handoff:** Once you have all necessary details (property, city, dates, guests), respond with: 'Thank you. I have your details for the booking. Our team will verify the availability and contact you shortly to confirm.'"

    "   g. **Handle Media:** If the context (retrieved property information) has `[ACTION_SEND_IMAGE_GALLERY]` and the user asks for photos of a specific property, your entire response must be ONLY that block. If it has `[VIDEO_LINK]`, include it naturally in your text when describing the property."

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
    # Flag to prevent RAG fallback if a definitive city-based property search was done.
    city_property_search_handled = False

    if intent == "property_search" and PROPERTY_SHEET_ID:
        city_filter_value = None
        if filters and "City" in filters and isinstance(filters["City"], dict) and "value" in filters["City"]:
            city_filter_value = filters["City"]["value"]

        if city_filter_value: # If a city is specified, this is a definitive search
            city_property_search_handled = True

        all_properties_df = property_handler.get_sheet_data()
        if not all_properties_df.empty:
            filtered_df = property_handler.filter_properties(all_properties_df, filters) if filters else all_properties_df

            if not filtered_df.empty:
                context_str = "Relevant Information Found:\n"
                for _, prop in filtered_df.head(5).iterrows():
                    # price_parts = []
                    # if pd.notna(prop.get('WeekdayPrice')) and prop.get('WeekdayPrice') > 0: price_parts.append(f"Weekday: {prop.get('WeekdayPrice')} SAR/night")
                    # if pd.notna(prop.get('WeekendPrice')) and prop.get('WeekendPrice') > 0: price_parts.append(f"Weekend: {prop.get('WeekendPrice')} SAR/night")
                    # if pd.notna(prop.get('MonthlyPrice')) and prop.get('MonthlyPrice') > 0: price_parts.append(f"Monthly: {prop.get('MonthlyPrice')} SAR")
                    price_str = "Price available upon request" # Directly set this line

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
            else: # No properties found after filtering
                if city_filter_value:
                    context_str = f"Relevant Information Found:\nNo properties found matching your criteria in {city_filter_value}."
                else:
                    context_str = "Relevant Information Found:\nNo properties found matching your criteria."
        else: # Unable to access property listings from the sheet
             context_str = "Relevant Information Found:\nI was unable to access property listings at the moment."

    elif intent == "price_clarification" and PROPERTY_SHEET_ID and filters:
        prop_name_filter_val = None
        price_type_filter_val = None

        if isinstance(filters.get("PropertyName"), dict) and "value" in filters.get("PropertyName"):
            prop_name_filter_val = filters.get("PropertyName")["value"]
        elif isinstance(filters.get("PropertyName"), str): # Simpler case if LLM returns string directly
             prop_name_filter_val = filters.get("PropertyName")


        if isinstance(filters.get("price_type"), dict) and "value" in filters.get("price_type"):
            price_type_filter_val = filters.get("price_type")["value"]
        elif isinstance(filters.get("price_type"), str): # Simpler case
            price_type_filter_val = filters.get("price_type")


        if prop_name_filter_val and price_type_filter_val:
            property_name_to_find = prop_name_filter_val
            requested_price_type = price_type_filter_val.lower()

            all_properties_df = property_handler.get_sheet_data()
            if not all_properties_df.empty:
                property_specific_filter_dict = {"PropertyName": {"operator": "=", "value": property_name_to_find}}
                target_property_df = property_handler.filter_properties(all_properties_df, property_specific_filter_dict)

                if not target_property_df.empty:
                    prop_details = target_property_df.iloc[0]
                    price_value = None
                    price_column_name = ""
                    price_type_display = requested_price_type

                    if requested_price_type == "weekday":
                        price_column_name = "WeekdayPrice"
                    elif requested_price_type == "weekend":
                        price_column_name = "WeekendPrice"
                    elif requested_price_type == "monthly":
                        price_column_name = "MonthlyPrice"
                    else:
                        context_str = f"Relevant Information Found:\nI'm sorry, I can only provide 'weekday', 'weekend', or 'monthly' prices. You asked for '{requested_price_type}' for {property_name_to_find}."

                    if price_column_name: # If price_type was valid
                        if pd.notna(prop_details.get(price_column_name)) and prop_details.get(price_column_name) > 0:
                            price_value = prop_details.get(price_column_name)
                            context_str = (
                                f"Relevant Information Found:\n"
                                f"PropertyName: {property_name_to_find}\n"
                                f"RequestedPrice: {price_value} SAR for {price_type_display} stay."
                            )
                        else:
                            context_str = (
                                f"Relevant Information Found:\n"
                                f"The {price_type_display} price for {property_name_to_find} is not currently available."
                            )
                else:
                    context_str = (
                        f"Relevant Information Found:\n"
                        f"Sorry, I couldn't find the specific details for a property named '{property_name_to_find}'. "
                        f"Please ensure the name is correct, or I can show you the list of properties again."
                    )
            else:
                context_str = "Relevant Information Found:\nI was unable to access property listings at the moment to check the price."
            city_property_search_handled = True # Critical: Mark as handled
        else:
            logging.warning(f"Price clarification intent for '{text}' but filters were missing or malformed: {filters}")
            context_str = "I seem to have missed which property or price type you're asking about. Could you please clarify?"
            city_property_search_handled = True # Critical: Mark as handled

    # Fallback for general property-related queries if not handled by specific intent and not a city search
    elif is_property_related_query(text) and not city_property_search_handled and PROPERTY_SHEET_ID:
        all_properties_df = property_handler.get_sheet_data()
        if not all_properties_df.empty:
            # Generic queries might not have 'filters' from intent analysis, or they might be broad.
            filtered_df = property_handler.filter_properties(all_properties_df, filters) if filters else all_properties_df
            if not filtered_df.empty:
                context_str = "Relevant Information Found:\n"
                for _, prop in filtered_df.head(5).iterrows(): # Identical formatting as above
                    # price_parts = []
                    # if pd.notna(prop.get('WeekdayPrice')) and prop.get('WeekdayPrice') > 0: price_parts.append(f"Weekday: {prop.get('WeekdayPrice')} SAR/night")
                    # if pd.notna(prop.get('WeekendPrice')) and prop.get('WeekendPrice') > 0: price_parts.append(f"Weekend: {prop.get('WeekendPrice')} SAR/night")
                    # if pd.notna(prop.get('MonthlyPrice')) and prop.get('MonthlyPrice') > 0: price_parts.append(f"Monthly: {prop.get('MonthlyPrice')} SAR")
                    price_str = "Price available upon request" # Directly set this line
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
                context_str = "Relevant Information Found:\nNo properties found matching your current query."
        else:
             context_str = "Relevant Information Found:\nI was unable to access property listings."

    # RAG fallback: Only if context_str is still empty AND it wasn't a city-specific property search that should be exclusive.
    if not context_str and not city_property_search_handled:
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
