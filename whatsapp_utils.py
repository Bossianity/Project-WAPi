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
        buttons_payload.append({
            "type": "quick_reply",
            "title": btn.get('title'),
            "id": btn.get('id')
        })

    # This payload structure now exactly matches the working example.
    payload = {
        "to": to,
        "type": "button",
        "header": { "text": message_data.get('header', '') },
        "body": { "text": message_data.get('body', '') },
        "footer": { "text": message_data.get('footer', '') },
        "action": { "buttons": buttons_payload },
        "view_once": False # Added for full compatibility with the working example
    }

    # Clean up empty optional fields to prevent API errors
    if not payload["header"]["text"]: del payload["header"]
    if not payload["footer"]["text"]: del payload["footer"]
    if not payload["action"]["buttons"]:
        logging.error(f"Attempted to send interactive message to {to} with no buttons.")
        return False

    logging.info(f"Attempting to send interactive message to {to}. Final payload (before sending):")
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
    Sends an interactive list message using the Whapi.Cloud API.
    """
    endpoint = 'messages/interactive'

    action_sections = []
    for section_data in message_data.get('sections', []):
        rows = []
        for row_data in section_data.get('rows', []):
            row = {"id": row_data["id"], "title": row_data["title"]}
            if "description" in row_data:
                row["description"] = row_data["description"]
            rows.append(row)

        current_section = {"rows": rows}
        if "title" in section_data:
            current_section["title"] = section_data["title"]
        action_sections.append(current_section)

    if not action_sections or not any(s.get("rows") for s in action_sections):
        logging.error(f"Attempted to send list message to {to} with no sections or rows.")
        return False

    payload = {
        "to": to,
        "type": "list",
        "body": {"text": message_data.get('body', '')},
        "action": {
            "button": message_data.get('label', 'View Options'), # Default label if not provided
            "sections": action_sections
        }
    }

    if 'header' in message_data and message_data['header']:
        payload['header'] = {"type": "text", "text": message_data['header']}

    if 'footer' in message_data and message_data['footer']:
        payload['footer'] = {"text": message_data['footer']}

    logging.info(f"Attempting to send interactive list message to {to}. Final payload (before sending):")
    logging.info(json.dumps(payload, indent=2))

    response = send_whapi_request(endpoint, payload)

    if response and response.get('sent'):
        logging.info(f"Successfully sent interactive list message to {to}.")
        return True
    else:
        logging.error(f"Failed to send interactive list message to {to}. Response: {response}")
        return False
