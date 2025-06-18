import os
import json
import time
import random
import logging
import requests
import tempfile
from requests_toolbelt.multipart.encoder import MultipartEncoder

# Load environment variables for Whapi.Cloud
WHAPI_API_URL = os.getenv('API_URL')
WHAPI_TOKEN = os.getenv('API_TOKEN')

# Standard logging calls (logging.info, logging.error, etc.) will be used.
# These will inherit the basicConfig from the main script.py if this module is imported,
# or use default Python logging if run standalone (though it's not designed for standalone).

def send_whapi_request(endpoint, params=None, method='POST', is_media=False, timeout=30):
    """
    Generic function to send requests to the Whapi.Cloud API with retry logic.
    """
    if not WHAPI_API_URL or not WHAPI_TOKEN:
        logging.error("WHAPI API_URL or API_TOKEN not configured in environment variables.")
        return None

    headers = {'Authorization': f"Bearer {WHAPI_TOKEN}"}
    url = f"{WHAPI_API_URL}/{endpoint}"
    max_retries = 3

    for attempt in range(max_retries):
        try:
            if is_media:
                file_path, mime_type = params.pop('media')
                with open(file_path, 'rb') as file:
                    multipart_data = MultipartEncoder(
                        fields={**params, 'media': (os.path.basename(file_path), file, mime_type)}
                    )
                    headers['Content-Type'] = multipart_data.content_type
                    response = requests.post(url, data=multipart_data, headers=headers, timeout=timeout)
            else:
                headers['Content-Type'] = 'application/json'
                response = requests.request(method, url, json=params, headers=headers, timeout=timeout)

            response.raise_for_status()
            response_json = response.json()
            logging.info(f"Whapi response from '{endpoint}': {response_json}")
            return response_json

        except requests.exceptions.RequestException as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed for endpoint '{endpoint}': {e}")
            if hasattr(e, 'response') and e.response:
                logging.error(f"Whapi error response: {e.response.text}")
            if attempt < max_retries - 1:
                time.sleep((2 ** attempt) + random.uniform(0.1, 0.5))
            else:
                return None
    return None

def send_whatsapp_message(to, text):
    """Sends a text message using the Whapi.Cloud API."""
    endpoint = 'messages/text'
    payload = {'to': to, 'body': text}
    response = send_whapi_request(endpoint, payload)
    return response and response.get('sent')

def send_whatsapp_image_message(to, caption, image_url):
    """Downloads an image from a URL and sends it via the Whapi.Cloud API."""
    try:
        image_response = requests.get(image_url, stream=True, timeout=20)
        image_response.raise_for_status()
        content_type = image_response.headers.get('Content-Type', 'image/jpeg')

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_file:
            tmp_file.write(image_response.content)
            tmp_file_path = tmp_file.name
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to download image from URL {image_url}: {e}")
        return False

    endpoint = 'messages/image'
    payload = {
        'to': to,
        'caption': caption,
        'media': (tmp_file_path, content_type)
    }

    try:
        response = send_whapi_request(endpoint, payload, is_media=True)
    finally:
        os.remove(tmp_file_path)

    return response and response.get('sent')

def set_webhook():
    """Sets the bot's webhook URL with Whapi.Cloud on startup."""
    bot_url = os.getenv('BOT_URL')
    if not bot_url:
        logging.warning("BOT_URL environment variable not set. Cannot set webhook.")
        return

    logging.info(f"Attempting to set webhook to: {bot_url}")
    settings = {
        'webhook': {
            'url': bot_url,
            'events': ['messages', 'statuses']
        }
    }
    response = send_whapi_request('settings', settings, method='PATCH', timeout=15)
    if response:
        logging.info("Webhook set successfully")
    else:
        logging.error("Failed to set webhook")

def send_interactive_button_message(to, message_data):
    """
    Sends an interactive message with buttons, precisely matching the
    working payload structure from the Whapi documentation.
    """
    endpoint = 'messages/interactive'

    buttons_payload = []
    for btn in message_data.get('buttons', []):
        button_type = btn.get('type', 'quick_reply') # Default to quick_reply if not specified
        current_button = {
            "type": button_type,
            "title": btn.get('title'),
            "id": btn.get('id')
        }
        if button_type == 'url':
            if not btn.get('url'):
                logging.error(f"URL button for interactive message to {to} is missing 'url' field. Skipping button: {btn.get('title')}")
                continue # Skip this button
            current_button['url'] = btn.get('url')

        buttons_payload.append(current_button)

    if not buttons_payload: # Check if any valid buttons were added
        logging.error(f"Attempted to send interactive button message to {to} with no valid buttons after processing.")
        return False

    payload = {
        "to": to,
        "type": "button",
        "view_once": False
    }

    header_text = message_data.get('header')
    if header_text:
        payload['header'] = {"text": header_text}

    body_text = message_data.get('body')
    if body_text:
        payload['body'] = {"text": body_text}
    else:
        logging.error(f"Attempted to send interactive button message to {to} with no body text.")
        return False

    footer_text = message_data.get('footer')
    if footer_text:
        payload['footer'] = {"text": footer_text}

    payload['action'] = {"buttons": buttons_payload}

    logging.info(f"Attempting to send interactive button message to {to}. Final payload (before sending):")
    logging.info(json.dumps(payload, indent=2))

    response = send_whapi_request(endpoint, payload)

    if response and response.get('sent'):
        logging.info(f"Successfully sent interactive button message to {to}.")
        return True
    else:
        logging.error(f"Failed to send interactive button message to {to}. Response: {response}")
        return False


def send_interactive_list_message(to, message_data):
    """
    Sends an interactive list message using the Whapi.Cloud API,
    matching the specified payload structure.
    """
    endpoint = 'messages/interactive'

    payload = {
        "to": to,
        "type": "list",
        "view_once": False
    }

    header_text = message_data.get('header')
    if header_text:
        payload['header'] = {"text": header_text}

    body_text = message_data.get('body')
    if body_text:
        payload['body'] = {"text": body_text}
    else:
        logging.error(f"Attempted to send interactive list message to {to} with no body text.")
        return False

    footer_text = message_data.get('footer')
    if footer_text:
        payload['footer'] = {"text": footer_text}

    raw_sections = message_data.get('sections', [])
    if not raw_sections:
        logging.error(f"Attempted to send interactive list message to {to} with no sections.")
        return False

    sections_payload = []
    for section_data in raw_sections:
        row_payload = []
        raw_rows = section_data.get('rows', [])
        if not raw_rows:
             logging.warning(f"Skipping section with no rows for interactive list message to {to}. Section title: {section_data.get('title')}")
             continue

        for row_data in raw_rows:
            if not row_data.get('id') or not row_data.get('title'):
                logging.warning(f"Skipping row with missing ID or title for interactive list message to {to}.")
                continue

            current_row = {
                "id": row_data['id'],
                "title": row_data['title']
            }
            if row_data.get('description'):
                current_row['description'] = row_data['description']
            row_payload.append(current_row)

        if not row_payload:
            logging.warning(f"Section '{section_data.get('title')}' resulted in no valid rows for list message to {to}.")
            continue

        current_section = {"rows": row_payload}
        section_title = section_data.get('title')
        if section_title: # Only add title key if it exists and is not empty
            current_section['title'] = section_title
        sections_payload.append(current_section)

    if not sections_payload:
        logging.error(f"Attempted to send interactive list message to {to} but no valid sections or rows could be constructed.")
        return False

    payload['action'] = {
        "list": {
            "label": message_data.get('label', "View Options"),
            "sections": sections_payload
        }
    }

    logging.info(f"Attempting to send interactive list message to {to}. Final payload (before sending):")
    logging.info(json.dumps(payload, indent=2))

    response = send_whapi_request(endpoint, payload)

    if response and response.get('sent'):
        logging.info(f"Successfully sent interactive list message to {to}.")
        return True
    else:
        logging.error(f"Failed to send interactive list message to {to}. Response: {response}")
        return False

# --- Translation Helper and New Message Functions ---

def _get_translated_text(key_path, language, text_map, default_value=None):
    """
    Retrieves translated text from a nested dictionary structure.
    key_path: A string or list representing the path to the desired text, e.g., "body" or ['buttons', 0, 'title'].
    language: The language code, e.g., 'ar' or 'en'.
    text_map: The dictionary containing translations.
    default_value: Value to return if the key_path or language is not found.
    """
    try:
        lang_map = text_map.get(language, text_map.get('en', {})) # Fallback to 'en' if language not found

        current_level = lang_map
        if isinstance(key_path, str):
            key_path = [key_path]

        for key in key_path:
            if isinstance(current_level, dict):
                current_level = current_level[key]
            elif isinstance(current_level, list) and isinstance(key, int):
                current_level = current_level[key]
            else:
                raise KeyError(f"Invalid path component: {key} for data type: {type(current_level)}")
        return current_level
    except (KeyError, IndexError) as e:
        logging.warning(f"Translation key path '{'.'.join(map(str, key_path))}' not found for language '{language}'. Error: {e}. Returning default.")
        # Try to get the default from the 'en' map if it was a specific language request that failed
        if language != 'en':
            try:
                en_map = text_map.get('en', {})
                current_level = en_map
                for key in key_path:
                    if isinstance(current_level, dict):
                        current_level = current_level[key]
                    elif isinstance(current_level, list) and isinstance(key, int):
                        current_level = current_level[key]
                    else:
                        raise KeyError
                return current_level
            except (KeyError, IndexError):
                logging.warning(f"Fallback translation key path '{'.'.join(map(str, key_path))}' also not found in 'en' map.")
        return default_value


initial_greeting_text_map = {
    'ar': {
        'header': "هلا ! أنا مساعد من شركة عوجا لإدارة الأملاك",
        'body': "كيف ممكن أخدمك اليوم؟",
        'footer': "أضغط لتختار:",
        'buttons': [
            {'id': 'button_id1', 'title': "أملك شقة حابي أشغلها", 'type': 'quick_reply'},
            {'id': 'button_id2', 'title': "ابي أستاجر شقة", 'type': 'quick_reply'},
            {'id': 'button_id3', 'title': "أستفسارات أخرى", 'type': 'quick_reply'}
        ]
    },
    'en': {
        'header': "Hello! I am an assistant from Al Awja Property Management.",
        'body': "How can I help you today?",
        'footer': "Click to choose:",
        'buttons': [
            {'id': 'button_id1', 'title': "I own an apartment and want to operate it", 'type': 'quick_reply'},
            {'id': 'button_id2', 'title': "I want to rent an apartment", 'type': 'quick_reply'},
            {'id': 'button_id3', 'title': "Other inquiries", 'type': 'quick_reply'}
        ]
    }
}

def send_initial_greeting_message(to, language='ar'):
    """Sends the initial greeting interactive message."""
    texts = initial_greeting_text_map.get(language, initial_greeting_text_map['en']) # Fallback to English

    message_data = {
        'header': texts['header'],
        'body': texts['body'],
        'footer': texts['footer'],
        'buttons': texts['buttons']
    }
    logging.info(f"Sending initial greeting message to {to} in {language}.")
    return send_interactive_button_message(to, message_data)

furnished_query_text_map = {
    'ar': {
        'header': "نتشرف بيك!",
        'body': "بس حابين نعرف اذا هي مؤثثة(مفروشة) أو لا؟",
        'footer': "أضغط لتختار:",
        'buttons': [
            {"type": "quick_reply", "title": "نعم مؤثثة", "id": "button_id4"},
            {"type": "quick_reply", "title": "لا غير مؤثثة", "id": "button_id5"}
        ]
    },
    'en': {
        'header': "We are honored to have you!",
        'body': "We'd just like to know if it's furnished or not?",
        'footer': "Click to choose:",
        'buttons': [
            {"type": "quick_reply", "title": "Yes, furnished", "id": "button_id4"},
            {"type": "quick_reply", "title": "No, unfurnished", "id": "button_id5"}
        ]
    }
}

def send_furnished_query_message(to, language='ar'):
    """Sends a message asking if the user's apartment is furnished."""
    texts = furnished_query_text_map.get(language, furnished_query_text_map['en'])

    message_data = {
        'header': texts['header'],
        'body': texts['body'],
        'footer': texts['footer'],
        'buttons': texts['buttons']
    }
    logging.info(f"Sending furnished query message to {to} in {language}.")
    return send_interactive_button_message(to, message_data)

furnished_apartment_survey_text_map = {
    'ar': {
        'header': "الرجاء ملء الاستبيان",
        'body': "عشان نقدر نخدمك ممكن تملى الإستبيان؟",
        'footer': "أضغط لفتح الرابط:",
        'buttons': [{"type": "url", "title": "استبيان الشقق المؤثثة", "id": "button_id7", "url": "https://form.typeform.com/to/eFGv4yhC"}]
    },
    'en': {
        'header': "Please fill out the survey",
        'body': "So we can serve you, could you please fill out the survey?",
        'footer': "Click to open the link:",
        'buttons': [{"type": "url", "title": "Furnished Apartments Survey", "id": "button_id7", "url": "https://form.typeform.com/to/eFGv4yhC"}]
    }
}

def send_furnished_apartment_survey_message(to, language='ar'):
    """Sends a message with a URL button for the furnished apartment survey."""
    texts = furnished_apartment_survey_text_map.get(language, furnished_apartment_survey_text_map['en'])

    message_data = {
        'header': texts['header'],
        'body': texts['body'],
        'footer': texts['footer'],
        'buttons': texts['buttons'] # Button type is 'url', handled by send_interactive_button_message
    }
    logging.info(f"Sending furnished apartment survey message to {to} in {language}.")
    return send_interactive_button_message(to, message_data)

unfurnished_apartment_survey_text_map = {
    'ar': {
        'header': "ولا يهمك، عندنا خدمة تأثيث بمعايير فندقية وأسعار تنافسية",
        'body': "مهندسينا خبرتهم أكثر من 8 سنوات ومنفذين فوق 500 مشروع.",
        'footer': "فقط عبي الإستبيان:",
        'buttons': [{"type": "url", "title": "استبيان التأثيث", "id": "button_id8", "url": "https://form.typeform.com/to/vDKXMSaQ"}]
    },
    'en': {
        'header': "No worries, we have a furnishing service with hotel standards and competitive prices.",
        'body': "Our engineers have more than 8 years of experience and have completed over 500 projects.",
        'footer': "Just fill out the survey:",
        'buttons': [{"type": "url", "title": "Furnishing Survey", "id": "button_id8", "url": "https://form.typeform.com/to/vDKXMSaQ"}]
    }
}

def send_unfurnished_apartment_survey_message(to, language='ar'):
    """Sends a message with a URL button for the unfurnished apartment/furnishing survey."""
    texts = unfurnished_apartment_survey_text_map.get(language, unfurnished_apartment_survey_text_map['en'])

    message_data = {
        'header': texts['header'],
        'body': texts['body'],
        'footer': texts['footer'],
        'buttons': texts['buttons'] # Button type is 'url'
    }
    logging.info(f"Sending unfurnished apartment survey message to {to} in {language}.")
    return send_interactive_button_message(to, message_data)

city_selection_text_map = {
    'ar': {
        'header': "إختيار المدينة",
        'body': "في أي مدينة تبغي تحجز؟",
        'footer': "إختار من القائمة:",
        'list_label': "قائمة المدن السعودية",
        'rows': [
            {"title": "الرياض", "id": "riyadh"}, {"title": "جدة", "id": "jeddah"},
            {"title": "الدمام", "id": "dammam"}, {"title": "مكة المكرمة", "id": "makkah"},
            {"title": "المدينة المنورة", "id": "medina"}, {"title": "الخبر", "id": "khobar"},
            {"title": "الظهران", "id": "dhahran"}, {"title": "تبوك", "id": "tabuk"},
            {"title": "بريدة", "id": "buraidah"}, {"title": "حائل", "id": "hail"}
        ]
    },
    'en': {
        'header': "Select City",
        'body': "In which city do you want to book?",
        'footer': "Choose from the list:",
        'list_label': "List of Saudi Cities",
        'rows': [
            {"title": "Riyadh", "id": "riyadh"}, {"title": "Jeddah", "id": "jeddah"},
            {"title": "Dammam", "id": "dammam"}, {"title": "Makkah", "id": "makkah"},
            {"title": "Medina", "id": "medina"}, {"title": "Khobar", "id": "khobar"},
            {"title": "Dhahran", "id": "dhahran"}, {"title": "Tabuk", "id": "tabuk"},
            {"title": "Buraidah", "id": "buraidah"}, {"title": "Hail", "id": "hail"}
        ]
    }
}

def send_city_selection_message(to, language='ar'):
    """Sends an interactive list message for city selection."""
    texts = city_selection_text_map.get(language, city_selection_text_map['en'])

    message_data = {
        'header': texts['header'],
        'body': texts['body'],
        'footer': texts['footer'],
        'label': texts['list_label'], # This is the button text for opening the list
        'sections': [{
            # 'title': texts['list_label'], # Optional: Title for the section within the list
            'rows': texts['rows']
        }]
    }
    logging.info(f"Sending city selection list message to {to} in {language}.")
    return send_interactive_list_message(to, message_data)
