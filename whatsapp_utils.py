import os
import json
import time
import random
import logging
import requests

WASENDER_API_URL = os.getenv('WASENDER_API_URL', "https://www.wasenderapi.com/api/send-message")
WASENDER_API_TOKEN = os.getenv('WASENDER_API_TOKEN')
HTTP_SESSION = requests.Session()

# Standard logging calls (logging.info, logging.error, etc.) will be used.
# These will inherit the basicConfig from the main script.py if this module is imported,
# or use default Python logging if run standalone (though it's not designed for standalone).

def send_whatsapp_message(to, text):
    """Sends a text message via WaSenderAPI with robust retry logic."""
    # These variables are now module-level in whatsapp_utils.py
    # global WASENDER_API_URL, WASENDER_API_TOKEN, HTTP_SESSION

    if not all([WASENDER_API_URL, WASENDER_API_TOKEN, HTTP_SESSION]):
        logging.error("WASender API URL, Token, or HTTP_SESSION not configured. Cannot send message.")
        return False

    clean_to = to.split('@')[0] if "@s.whatsapp.net" in to else to
    payload = {'to': clean_to, 'text': text}
    headers = {'Authorization': f'Bearer {WASENDER_API_TOKEN}', 'Content-Type': 'application/json'}
    max_retries = 4 # Align with send_whatsapp_image_message

    for attempt in range(max_retries):
        try:
            logging.info(f"Attempting to send message to {clean_to} (Attempt {attempt+1}/{max_retries}). Text: {text[:50]}...")
            resp = HTTP_SESSION.post(WASENDER_API_URL, json=payload, headers=headers, timeout=15) # Existing timeout for text

            if not (200 <= resp.status_code < 300):
                logging.error(f"Error sending message to {clean_to} on attempt {attempt+1}/{max_retries}. Status: {resp.status_code}. Response: {resp.text[:500]}")
                if resp.status_code == 401:
                    logging.error("WASender API Token is unauthorized (401). Cannot send message.")
                    return False # No retry for auth error
                if resp.status_code == 400:
                    logging.error(f"WASender API returned 400 Bad Request for message send. Payload: {json.dumps(payload)}, Response: {resp.text[:500]}")
                # Fall through to retry logic for other non-2xx codes

            else: # Status code is 2xx
                try:
                    data = resp.json()
                    logging.info(f"Message send API response for {clean_to} (Attempt {attempt+1}): {data.get('message', 'No message field in JSON response')}")
                    if data.get("success") is True:
                        logging.info(f"Successfully sent message to {clean_to}: {text[:50]}...")
                        return True
                    else:
                        logging.warning(f"API call for message send to {clean_to} (HTTP {resp.status_code}) was successful but 'success' field is false or missing. Attempt {attempt+1}/{max_retries}. JSON: {data}")
                except requests.exceptions.JSONDecodeError as e_json:
                    logging.error(f"Failed to decode JSON response for message send to {clean_to} on attempt {attempt+1}/{max_retries}. Status: {resp.status_code}. Response text: {resp.text[:500]}. Error: {e_json}")

            # If we reach here, it means the attempt failed

        except requests.exceptions.Timeout:
            logging.warning(f"Message send attempt {attempt+1}/{max_retries} to {clean_to} timed out after 15 seconds.")
        except requests.exceptions.RequestException as e_req:
            logging.warning(f"Message send attempt {attempt+1}/{max_retries} to {clean_to} failed with RequestException: {e_req}")

        if attempt < max_retries - 1:
            wait_time = (2 ** attempt) + random.uniform(0.1, 0.9)
            logging.info(f"Retrying message send to {clean_to} in {wait_time:.2f} seconds...")
            time.sleep(wait_time)

    logging.error(f"All {max_retries} attempts to send message to {clean_to} failed. Message: {text[:50]}...")
    return False

def send_whatsapp_image_message(to, caption, image_url):
    """Sends an image message via WaSenderAPI with retry logic."""
    # These variables are now module-level in whatsapp_utils.py
    # global WASENDER_API_URL, WASENDER_API_TOKEN, HTTP_SESSION

    if not all([WASENDER_API_URL, WASENDER_API_TOKEN, HTTP_SESSION]):
        logging.error("WASender API URL, Token, or HTTP_SESSION not configured. Cannot send image.")
        return False

    clean_to = to.split('@')[0] if "@s.whatsapp.net" in to else to
    payload = {'to': clean_to, 'imageUrl': image_url}
    if caption and isinstance(caption, str) and caption.strip():
        payload['text'] = caption.strip()

    headers = {'Authorization': f'Bearer {WASENDER_API_TOKEN}', 'Content-Type': 'application/json'}
    max_retries = 4

    for attempt in range(max_retries):
        try:
            logging.info(f"Attempting to send image to {clean_to} (Attempt {attempt+1}/{max_retries}). URL: {image_url}, Caption: {caption}")
            resp = HTTP_SESSION.post(WASENDER_API_URL, json=payload, headers=headers, timeout=20)

            if not (200 <= resp.status_code < 300):
                logging.error(f"Error sending image to {clean_to} on attempt {attempt+1}/{max_retries}. Status: {resp.status_code}. Response: {resp.text[:500]}")
                if resp.status_code == 401:
                    logging.error("WASender API Token is unauthorized (401). Cannot send image.")
                    return False # No retry for auth error
                if resp.status_code == 400:
                     logging.error(f"WASender API returned 400 Bad Request for image send. Payload: {json.dumps(payload)}, Response: {resp.text[:500]}")
                # Fall through to retry logic for other non-2xx codes

            else: # Status code is 2xx
                try:
                    data = resp.json()
                    logging.info(f"Image send API response for {clean_to} (Attempt {attempt+1}): {data.get('message', 'No message field in JSON response')}")
                    if data.get("success") is True:
                        logging.info(f"Successfully sent image to {clean_to} with caption '{caption}'. URL: {image_url}")
                        return True
                    else:
                        logging.warning(f"API call for image send to {clean_to} (HTTP {resp.status_code}) was successful but 'success' field is false or missing. Attempt {attempt+1}/{max_retries}. JSON: {data}")
                except requests.exceptions.JSONDecodeError as e:
                    logging.error(f"Failed to decode JSON response for image send to {clean_to} on attempt {attempt+1}/{max_retries}. Status: {resp.status_code}. Response text: {resp.text[:500]}. Error: {e}")

            # If we reach here, it means the attempt failed (non-2xx, or 2xx but not "success:true", or JSON decode error)

        except requests.exceptions.Timeout:
            logging.warning(f"Image send attempt {attempt+1}/{max_retries} to {clean_to} timed out after 20 seconds.")
        except requests.exceptions.RequestException as e:
            logging.warning(f"Image send attempt {attempt+1}/{max_retries} to {clean_to} failed with RequestException: {e}")

        if attempt < max_retries - 1:
            wait_time = (2 ** attempt) + random.uniform(0.1, 0.9)
            logging.info(f"Retrying image send to {clean_to} in {wait_time:.2f} seconds...")
            time.sleep(wait_time)

    logging.error(f"All {max_retries} attempts to send image to {clean_to} failed. URL: {image_url}")
    return False
