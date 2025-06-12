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
    Processes an outreach campaign based on data from a Google Sheet.
    """
    with app_context: # Ensures current_app and other Flask context globals are available
        logging.info(f"Starting outreach campaign for Sheet ID: {sheet_id}, initiated by {agent_sender_id}.")

        # --- Configuration ---
        business_name = os.getenv('BUSINESS_NAME', 'X Dental Clinic') # Direct os.getenv
        try:
            delay_seconds = int(os.getenv('OUTREACH_MESSAGE_DELAY_SECONDS', "5"))
        except ValueError:
            logging.warning("Invalid OUTREACH_MESSAGE_DELAY_SECONDS, defaulting to 5.")
            delay_seconds = 5

        dubai_tz = pytz.timezone('Asia/Dubai') # Define Dubai timezone

        # --- Initialization ---
        sheets_service = get_google_sheets_service()
        if not sheets_service:
            err_msg = f"Failed to initialize Google Sheets service. Campaign for {sheet_id} aborted."
            logging.error(err_msg)
            send_whatsapp_message(agent_sender_id, err_msg)
            return

        rows_data, header_map = read_sheet_data(sheets_service, sheet_id)
        if not rows_data and not header_map: # Check if read_sheet_data indicated a critical error
            err_msg = f"Failed to read or validate data from Sheet ID: {sheet_id}. Ensure required headers are present and sheet is not empty. Campaign aborted."
            logging.error(err_msg)
            send_whatsapp_message(agent_sender_id, err_msg)
            return

        # Dynamically get column indices
        phone_col_idx = header_map.get('PhoneNumber')
        name_col_idx = header_map.get('ClientName')
        status_col_idx = header_map.get('MessageStatus')
        last_contacted_col_idx = header_map.get('LastContactedDate') # Optional

        if None in [phone_col_idx, name_col_idx, status_col_idx]:
            err_msg = f"One or more critical column indices could not be determined from headers in {sheet_id}. Campaign aborted."
            logging.error(f"{err_msg} Header map: {header_map}")
            send_whatsapp_message(agent_sender_id, err_msg)
            return

        logging.info(f"Successfully validated required column indices for sheet {sheet_id}. Header map: {header_map}")
        logging.info(f"Starting campaign loop for sheet {sheet_id}, {len(rows_data)} rows to process.")

        sent_count = 0
        failed_count = 0
        skipped_count = 0

        # --- Campaign Loop ---
        for row_info in rows_data:
            row_values_dict = row_info['data']
            original_row_idx_1_based = row_info['original_row_index']
            logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based} Loop Start. Data: {row_values_dict}")

            phone_number = row_values_dict.get('PhoneNumber')
            client_name = row_values_dict.get('ClientName', 'Valued Customer') # Default if name is blank

            raw_message_status = row_values_dict.get('MessageStatus') # Get the raw value
            # Ensure current_status_for_check is an empty string if raw_message_status is None,
            # otherwise convert to string, strip, and then lowercase for the check.
            current_status_for_check = str(raw_message_status).strip().lower() if raw_message_status is not None else ""

            # Basic Validation
            if not phone_number:
                logging.warning(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: Skipped due to missing PhoneNumber. Update status attempt follows.")
                update_cell_value(sheets_service, sheet_id, DEFAULT_SHEET_NAME, original_row_idx_1_based, status_col_idx, "Failed - Missing PhoneNumber")
                failed_count += 1
                continue

            # Idempotency Check
            if current_status_for_check in ["sent", "replied", "completed", "success"]:
                logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: Skipped due to existing status '{raw_message_status}'.") # Log raw status
                skipped_count += 1
                continue

            # Message Personalization
            effective_client_name = client_name if client_name and client_name.strip() else 'Valued Customer'

            personalized_message = (
                f"Hi {effective_client_name}, this is Layla from {business_name}. "
                "Would you like to learn more about our services or perhaps schedule a consultation? 😊"
            )

            logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: Attempting to send message to {phone_number} with text: '{personalized_message[:75]}...'")
            message_sent_successfully = send_whatsapp_message(phone_number, personalized_message)
            logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: send_whatsapp_message for {phone_number} returned: {message_sent_successfully}")

            current_timestamp_dubai = datetime.now(dubai_tz) # Get current time in Asia/Dubai
            current_timestamp_str = current_timestamp_dubai.strftime("%Y-%m-%d %H:%M:%S") # Format for sheet
            new_status_value = ""

            if message_sent_successfully:
                new_status_value = "Sent"
                logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: Attempting to update status to '{new_status_value}' for {phone_number}.")
                status_update_success = update_cell_value(sheets_service, sheet_id, DEFAULT_SHEET_NAME, original_row_idx_1_based, status_col_idx, new_status_value)
                logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: update_cell_value (for status '{new_status_value}') returned: {status_update_success}")
                if status_update_success:
                    sent_count += 1
                else:
                    logging.error(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: Failed to UPDATE sheet status to '{new_status_value}' for {phone_number} after successful send. This is problematic.")
                    # Message was sent, but status update failed. Consider this a partial success / needs attention.
                    # Not incrementing sent_count here as the record isn't fully processed.
                    # Not incrementing failed_count either as the message *was* sent.
                    # This state might need a special status or manual review.
            else:
                new_status_value = "Failed - API Error"
                logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: Attempting to update status to '{new_status_value}' for {phone_number} (send failed).")
                status_update_success = update_cell_value(sheets_service, sheet_id, DEFAULT_SHEET_NAME, original_row_idx_1_based, status_col_idx, new_status_value)
                logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: update_cell_value (for status '{new_status_value}') returned: {status_update_success}")
                failed_count += 1 # Increment failed_count as message sending failed

            # Update LastContactedDate if column exists
            if last_contacted_col_idx is not None:
                logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: Attempting to update LastContactedDate to '{current_timestamp_str}' for {phone_number}.")
                date_update_success = update_cell_value(sheets_service, sheet_id, DEFAULT_SHEET_NAME, original_row_idx_1_based, last_contacted_col_idx, current_timestamp_str)
                logging.info(f"Sheet {sheet_id} - Row {original_row_idx_1_based}: update_cell_value (for LastContactedDate) returned: {date_update_success}")

            # Delay between messages
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        # --- Completion Notification ---
        summary_message = (
            f"Outreach campaign from Sheet ID {sheet_id} completed.\n"
            f"Successfully Sent: {sent_count}\n"
            f"Failed to Send: {failed_count}\n"
            f"Skipped (already processed or missing data): {skipped_count}"
        )
        send_whatsapp_message(agent_sender_id, summary_message)
        logging.info(f"Campaign {sheet_id} summary: Sent={sent_count}, Failed={failed_count}, Skipped={skipped_count}")

        logging.info(f"Outreach campaign for Sheet ID: {sheet_id} finished.")
