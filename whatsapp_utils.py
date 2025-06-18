import os
import json
import time
import random
import logging
import requests
import tempfile
from requests_toolbelt.multipart.encoder import MultipartEncoder

from interactive_messages import initial_greeting_message, owner_options_message, furnished_apartment_message, unfurnished_apartment_message, tenant_options_message

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
                # Log the payload for non-media requests
                logging.info(f"Sending Whapi request to '{url}' with method '{method}'. Payload: {json.dumps(params, indent=2, ensure_ascii=False)}")
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
    for btn_data in message_data.get('buttons', []):
        button_entry = {
            "type": btn_data.get("type"), # e.g., "quick_reply" or "url"
            "title": btn_data.get("title"),
            "id": btn_data.get("id") # For quick_reply, this is the postback data. For URL, it's an identifier.
        }
        if btn_data.get("type") == "url":
            button_entry["url"] = btn_data.get("url")
            # 'id' for URL buttons is optional in some APIs, but good to keep if provided for consistency
            # If 'id' is not strictly needed for URL buttons by Whapi, it could be omitted when type is 'url'.
            # However, the problem description implies 'id' is part of the component, so we include it.
        buttons_payload.append(button_entry)

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

    if not buttons_payload:
        logging.error(f"Attempted to send interactive button message to {to} with no buttons.")
        return False
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


def translate_payload(payload, language):
    """
    Recursively translates a message payload dictionary to the specified language.
    It looks for dictionaries with 'ar' and 'en' keys and replaces them with
    the value corresponding to the given language.
    """
    if isinstance(payload, dict):
        if 'ar' in payload and 'en' in payload and len(payload) == 2:
            return payload.get(language, payload.get('en')) # Default to English if lang not found

        new_dict = {}
        for key, value in payload.items():
            new_dict[key] = translate_payload(value, language)
        return new_dict
    elif isinstance(payload, list):
        return [translate_payload(item, language) for item in payload]
    else:
        return payload

message_templates = {
    "initial_greeting": initial_greeting_message,
    "owner_options": owner_options_message,
    "furnished_apartment": furnished_apartment_message,
    "unfurnished_apartment": unfurnished_apartment_message,
    "tenant_options": tenant_options_message,
}

def send_custom_interactive_message(to: str, message_name: str, language: str):
    """
    Sends a custom interactive message (button or list) using a predefined template.
    The message is translated to the specified language before sending.
    """
    if not WHAPI_API_URL or not WHAPI_TOKEN:
        logging.error("WHAPI API_URL or API_TOKEN not configured. Cannot send message.")
        return False

    template = message_templates.get(message_name)
    if not template:
        logging.error(f"Unknown message template name: {message_name}")
        return False

    # Deep copy the template to avoid modifying the original
    message_payload = json.loads(json.dumps(template))

    # Translate the payload
    translated_payload = translate_payload(message_payload, language)

    # Add the 'to' field
    translated_payload['to'] = to

    # The 'type' and 'view_once' are already part of the template

    endpoint = 'messages/interactive'
    logging.info(f"Attempting to send custom interactive message '{message_name}' to {to} in '{language}'.")
    logging.debug(f"Translated payload for '{message_name}': {json.dumps(translated_payload, indent=2, ensure_ascii=False)}")

    response = send_whapi_request(endpoint, translated_payload, method='POST')

    if response and response.get('sent'):
        logging.info(f"Successfully sent custom interactive message '{message_name}' to {to}.")
        return True
    else:
        logging.error(f"Failed to send custom interactive message '{message_name}' to {to}. Response: {response}")
        return False
