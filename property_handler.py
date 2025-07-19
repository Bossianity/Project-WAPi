import os
import json
import gspread
import pandas as pd
import logging
import re # Import regex module
from oauth2client.service_account import ServiceAccountCredentials

# --- Configuration ---
SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

def extract_sheet_id_from_url(url_or_id: str) -> str | None:
    """
    Extracts the Google Sheet ID from a URL.
    If the input is already an ID, it returns it directly.
    Returns None if no ID can be extracted or input is invalid.
    """
    if not url_or_id or not isinstance(url_or_id, str):
        logging.warning(f"Invalid input for sheet ID extraction: {url_or_id}")
        return None

    # Regex to find the Google Sheet ID in a URL
    # Example URL: https://docs.google.com/spreadsheets/d/1jRS261MseRrdHEL354fYznnkcbgt1sFNnCSttxdR4f0/edit#gid=0
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url_or_id)
    if match:
        logging.debug(f"Extracted sheet ID '{match.group(1)}' from URL '{url_or_id}'")
        return match.group(1)

    # Check if the input itself looks like a valid ID.
    # Google Sheet IDs are typically 44 characters long and use base64url characters.
    # This regex checks for a string that looks like a typical ID. Length check is a heuristic.
    if re.fullmatch(r'[a-zA-Z0-9-_]{30,60}', url_or_id): # Typical ID length is around 44.
        logging.debug(f"Input '{url_or_id}' appears to be a direct sheet ID.")
        return url_or_id

    logging.warning(f"Could not extract a valid sheet ID from input: '{url_or_id}'. It's not a recognized URL format and doesn't look like a direct ID.")
    return None

# Expected columns in the Google Sheet
EXPECTED_COLUMNS = [
    'Concept_ID', 'Source_ID', 'Tags', 'Concept_Text'
]

def get_concept_data():
    """
    Fetches all concept data from the Google Sheet specified by environment
    variables and loads it into a pandas DataFrame.
    """
    try:
        sheet_id_input = os.getenv('PROPERTY_SHEET_ID')
        actual_sheet_id = extract_sheet_id_from_url(sheet_id_input)

        if not actual_sheet_id:
            logging.error(f"PROPERTY_SHEET_ID ('{sheet_id_input}') is invalid or could not be parsed.")
            return pd.DataFrame()

        sheet_name = os.getenv('PROPERTY_SHEET_NAME', 'Concepts') # Assuming the sheet name is 'Concepts'

        creds_json_str = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
        if not creds_json_str:
            logging.error("GOOGLE_SHEETS_CREDENTIALS environment variable not set.")
            return pd.DataFrame()

        creds_info = json.loads(creds_json_str)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, SCOPE)
        client = gspread.authorize(creds)

        logging.info(f"Attempting to open sheet '{sheet_name}' with actual ID: {actual_sheet_id}")
        worksheet = client.open_by_key(actual_sheet_id).worksheet(sheet_name)
        records = worksheet.get_all_records()

        if not records:
            logging.warning(f"No data found in Google Sheet '{sheet_name}' with ID: {actual_sheet_id}")
            return pd.DataFrame()

        df = pd.DataFrame(records)

        for col in EXPECTED_COLUMNS:
            if col not in df.columns:
                df[col] = ''

        df.fillna('', inplace=True)

        logging.info(f"Successfully loaded {len(df)} concepts from sheet '{sheet_name}'.")
        return df

    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(f"Spreadsheet with actual ID '{actual_sheet_id}' not found or access denied.")
        return pd.DataFrame()
    except gspread.exceptions.WorksheetNotFound:
        logging.error(f"Worksheet named '{sheet_name}' not found in Spreadsheet ID: {actual_sheet_id}.")
        return pd.DataFrame()
    except Exception as e:
        logging.error(f"Error accessing Google Sheet '{sheet_name}': {e}", exc_info=True)
        return pd.DataFrame()