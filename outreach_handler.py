import os
import time
import logging
import json
from datetime import datetime
import pytz # Added for timezone support
from googleapiclient.discovery import build
from google.oauth2 import service_account
from flask import current_app # To be used within app_context

# Import send_whatsapp_message from the new whatsapp_utils module
from whatsapp_utils import send_whatsapp_message

# --- Constants and Configuration ---
DEFAULT_SHEET_NAME = "Sheet1"
REQUIRED_HEADERS = ['PhoneNumber', 'ClientName', 'MessageStatus']
OPTIONAL_HEADERS = ['LastContactedDate']


# --- Utility Functions ---
def col_num_to_letter(n_zero_based):
    """Converts a 0-based column index into a spreadsheet column letter (A, B, ..., Z, AA, ...)."""
    string = ""
    n = n_zero_based
    while n >= 0:
        string = chr(ord('A') + n % 26) + string
        n = n // 26 - 1
    return string


# --- Google Sheets Service ---
def get_google_sheets_service():
    """Initializes and returns the Google Sheets API service."""
    try:
        credentials_json_str = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
        if not credentials_json_str:
            logging.error("GOOGLE_SHEETS_CREDENTIALS environment variable not set.")
            return None

        credentials_info = json.loads(credentials_json_str)
        creds = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        service = build('sheets', 'v4', credentials=creds)
        logging.info("Google Sheets service initialized successfully.")
        return service
    except json.JSONDecodeError:
        logging.error("Failed to parse GOOGLE_SHEETS_CREDENTIALS JSON.")
        return None
    except Exception as e:
        logging.error(f"Error initializing Google Sheets service: {e}", exc_info=True)
        return None


# --- Sheet Data Reading ---
def read_sheet_data(service, sheet_id, sheet_name=DEFAULT_SHEET_NAME):
    """
    Reads data from the specified Google Sheet.
    Returns a list of row data (as dicts) and a map of header names to their 0-based column indices.
    """
    rows_with_original_indices = []
    header_to_index_map = {}

    try:
        result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=sheet_name).execute()
        values = result.get('values', [])

        if not values:
            logging.warning(f"Sheet '{sheet_name}' in {sheet_id} is empty or no data found.")
            return [], {}

        headers = values[0]
        for req_header in REQUIRED_HEADERS:
            if req_header not in headers:
                logging.error(f"Missing required header '{req_header}' in sheet {sheet_id}/{sheet_name}. Found headers: {headers}")
                return [], {}

        for i, header_name in enumerate(headers):
            header_to_index_map[header_name] = i

        for row_num_0_based, row_values in enumerate(values[1:], start=1): # Data rows start from index 1 of 'values'
            row_data_dict = {}
            for col_num_0_based, cell_value in enumerate(row_values):
                if col_num_0_based < len(headers): # Ensure we don't go out of bounds if row is longer than headers
                    header_name = headers[col_num_0_based]
                    row_data_dict[header_name] = cell_value

            # Fill missing optional columns with None if they were not in this specific row
            for header_name in headers:
                if header_name not in row_data_dict:
                    row_data_dict[header_name] = None

            rows_with_original_indices.append({
                'data': row_data_dict,
                'original_row_index': row_num_0_based + 1 # 1-based index for sheet interaction
            })

        logging.info(f"Successfully read {len(rows_with_original_indices)} data rows from {sheet_id}/{sheet_name}.")
        return rows_with_original_indices, header_to_index_map

    except Exception as e:
        logging.error(f"Error reading sheet data from {sheet_id}/{sheet_name}: {e}", exc_info=True)
        return [], {}


# --- Sheet Data Updating ---
def update_cell_value(service, sheet_id, sheet_name, row_index_1_based, col_index_0_based, value):
    """
    Updates a single cell in the Google Sheet.
    row_index_1_based: The 1-based row number in the sheet.
    col_index_0_based: The 0-based column number.
    """
    try:
        column_letter = col_num_to_letter(col_index_0_based)
        range_to_update = f"{sheet_name}!{column_letter}{row_index_1_based}"

        body = {'values': [[value]]}
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_to_update,
            valueInputOption='USER_ENTERED',
            body=body
        ).execute()
        logging.info(f"Updated cell {range_to_update} in {sheet_id} to '{value}'.")
        return True
    except Exception as e:
        logging.error(f"Error updating cell {sheet_name}!{col_num_to_letter(col_index_0_based)}{row_index_1_based} in {sheet_id}: {e}", exc_info=True)
        return False


# --- Main Campaign Processing Logic ---
def process_outreach_campaign(sheet_id, agent_sender_id, app_context):
    """
    Processes an outreach campaign, reading contacts and message templates
    from dynamically named sheets and executing the full sending loop.
    """
    with app_context:
        logging.info(f"Starting outreach campaign for Sheet ID: {sheet_id}, initiated by {agent_sender_id}.")

        sheets_service = get_google_sheets_service()
        if not sheets_service:
            send_whatsapp_message(agent_sender_id, "Error: Could not connect to Google Sheets service.")
            return

        # --- Get config from environment variables ---
        template_sheet_name = os.getenv('MESSAGE_TEMPLATE_SHEET_NAME', 'MessageTemplate')
        contacts_sheet_name = os.getenv('CONTACTS_SHEET_NAME', 'Sheet1')
        delay_seconds = int(os.getenv('OUTREACH_MESSAGE_DELAY_SECONDS', 5))
        dubai_tz = pytz.timezone('Asia/Dubai')

        # --- Step 1: Fetch Message Template ---
        interactive_template, simple_template, is_interactive = {}, "", False
        try:
            template_range = f"'{template_sheet_name}'!A1:D3"
            template_sheet_data = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range=template_range).execute()
            values = template_sheet_data.get('values', [])
            
            def get_value(r, c):
                try: return values[r][c]
                except IndexError: return ""

            if get_value(0, 0) == "INTERACTIVE_MESSAGE":
                is_interactive = True
                interactive_template = {
                    'header': get_value(0, 1), 'body': get_value(1, 1), 'footer': get_value(2, 1),
                    'buttons': [
                        {'title': get_value(0, 2), 'id': get_value(0, 3)},
                        {'title': get_value(1, 2), 'id': get_value(1, 3)},
                        {'title': get_value(2, 2), 'id': get_value(2, 3)},
                    ]
                }
                interactive_template['buttons'] = [b for b in interactive_template['buttons'] if b.get('title') and b.get('id')]
                logging.info("Successfully loaded INTERACTIVE message template.")
            else:
                simple_template = get_value(0, 0)
                logging.info("Loaded SIMPLE text message template.")
        except Exception as e:
            logging.warning(f"Could not read '{template_sheet_name}' sheet. Using default message. Error: {e}")

        # --- Step 2: Read Contact Data ---
        rows_data, header_map = read_sheet_data(sheets_service, sheet_id, contacts_sheet_name)
        if not rows_data:
            err_msg = f"Failed to read contact data from sheet '{contacts_sheet_name}'. Please check the sheet name and format."
            send_whatsapp_message(agent_sender_id, err_msg)
            return

        # --- Step 3: THE MISSING CAMPAIGN LOOP ---
        sent_count, failed_count, skipped_count = 0, 0, 0
        for row_info in rows_data:
            row_values_dict = row_info['data']
            original_row_idx_1_based = row_info['original_row_index']
            
            phone_number = row_values_dict.get('PhoneNumber')
            if not phone_number:
                skipped_count += 1
                continue

            current_status = str(row_values_dict.get('MessageStatus', '')).strip().lower()
            if current_status in ["sent", "replied", "completed", "success"]:
                skipped_count += 1
                continue
            
            placeholders = {
                'ClientName': row_values_dict.get('ClientName', 'Valued Customer').strip(),
                'ServiceName': row_values_dict.get('InterestedService', 'our services')
            }

            message_sent = False
            if is_interactive and interactive_template.get('buttons'):
                personalized_data = {
                    'header': interactive_template['header'].format(**placeholders),
                    'body': interactive_template['body'].format(**placeholders),
                    'footer': interactive_template['footer'].format(**placeholders),
                    'buttons': interactive_template['buttons']
                }
                logging.info(f"Row {original_row_idx_1_based}: Sending INTERACTIVE message to {phone_number}.")
                message_sent = send_interactive_button_message(phone_number, personalized_data)
            else:
                if not simple_template:
                    simple_template = f"Hi {{ClientName}}, this is Layla from Your Business. Would you like to learn more about {{ServiceName}}?"
                personalized_message = simple_template.format(**placeholders)
                logging.info(f"Row {original_row_idx_1_based}: Sending SIMPLE text message to {phone_number}.")
                message_sent = send_whatsapp_message(phone_number, personalized_message)
            
            new_status = "Sent" if message_sent else "Failed - API Error"
            update_cell_value(sheets_service, sheet_id, contacts_sheet_name, original_row_idx_1_based, header_map['MessageStatus'], new_status)
            
            if message_sent:
                sent_count += 1
            else:
                failed_count += 1

            if 'LastContactedDate' in header_map:
                timestamp = datetime.now(dubai_tz).strftime("%Y-%m-%d %H:%M:%S")
                update_cell_value(sheets_service, sheet_id, contacts_sheet_name, original_row_idx_1_based, header_map['LastContactedDate'], timestamp)

            time.sleep(delay_seconds)

        # --- Step 4: Completion Notification ---
        summary_message = (
            f"Outreach campaign from Sheet ID {sheet_id} completed.\n"
            f"Successfully Sent: {sent_count}\n"
            f"Failed to Send: {failed_count}\n"
            f"Skipped (already processed or no phone number): {skipped_count}"
        )
        send_whatsapp_message(agent_sender_id, summary_message)
        logging.info(f"Campaign {sheet_id} finished. Summary: {summary_message}")


        # --- Step 2: Read Contact Data ---
        rows_data, header_map = read_sheet_data(sheets_service, sheet_id)
        if not rows_data or 'MessageStatus' not in header_map:
            error_msg = "Failed to read contact data or missing required columns. Please check the sheet format."
            logging.error(error_msg)
            send_whatsapp_message(agent_sender_id, error_msg)
            return
            
        # --- Step 3: Campaign Loop ---
        for row_info in rows_data:
            # Skip if already contacted or missing phone number
            if row_info['data'].get('MessageStatus') == 'Sent' or not row_info['data'].get('PhoneNumber'):
                continue
            
            # --- Message Personalization ---
            placeholders = {
                'ClientName': row_info['data'].get('ClientName', 'Valued Customer').strip(),
                'ServiceName': row_info['data'].get('InterestedService', 'our services') 
            }

            message_sent = False
            if is_interactive and interactive_template.get('buttons'):
                # Personalize interactive message components
                personalized_data = {
                    'header': interactive_template['header'].format(**placeholders),
                    'body': interactive_template['body'].format(**placeholders),
                    'footer': interactive_template['footer'].format(**placeholders),
                    'buttons': interactive_template['buttons'] # IDs are not personalized
                }
                message_sent = send_interactive_button_message(row_info['data']['PhoneNumber'], personalized_data)
            else:
                # Use simple template if it exists, otherwise use hardcoded default
                if not simple_template:
                     simple_template = f"Hi {{ClientName}}, this is Emran from X realtors. We just wanted to know if you are interested in buying, selling or renting any properties?"
                
                personalized_message = simple_template.format(**placeholders)
                message_sent = send_whatsapp_message(row_info['data']['PhoneNumber'], personalized_message)

            # --- Update Status ---
            if message_sent:
                # Update the status in the sheet
                status_range = f'Contacts!{header_map["MessageStatus"]}{row_info["original_row_index"]}'
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range=status_range,
                    valueInputOption='RAW',
                    body={'values': [['Sent']]}
                ).execute()

                # Update last contacted date
                if 'LastContactedDate' in header_map:
                    date_range = f'Contacts!{header_map["LastContactedDate"]}{row_info["original_row_index"]}'
                    current_time = datetime.now(dubai_tz).strftime('%Y-%m-%d %H:%M:%S')
                    sheets_service.spreadsheets().values().update(
                        spreadsheetId=sheet_id,
                        range=date_range,
                        valueInputOption='RAW',
                        body={'values': [[current_time]]}
                    ).execute()

            # Add delay between messages
            time.sleep(delay_seconds)

        # --- Completion Notification ---
        completion_msg = (
            f"Outreach campaign completed!\n"
            f"Sheet ID: {sheet_id}\n"
            f"Total contacts processed: {len(rows_data)}"
        )
        send_whatsapp_message(agent_sender_id, completion_msg)
        logging.info(f"Outreach campaign completed for Sheet ID: {sheet_id}")
