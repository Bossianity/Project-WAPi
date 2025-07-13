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

# Expected columns in the Google Sheet (for the original get_sheet_data)
EXPECTED_COLUMNS = [
    'PropertyID', 'Title', 'Description', 'Price_AED', 'Bedrooms', 'emirate',
    'city', 'area', 'video1', 'video2', 'img1', 'img2', 'img3',
    'developer', 'building name'
]

def get_sheet_data():
    """
    Fetches all property data from the Google Sheet specified by environment
    variables and loads it into a pandas DataFrame.
    """
    try:
        sheet_id_input = os.getenv('PROPERTY_SHEET_ID')
        actual_sheet_id = extract_sheet_id_from_url(sheet_id_input)

        if not actual_sheet_id:
            logging.error(f"PROPERTY_SHEET_ID ('{sheet_id_input}') is invalid or could not be parsed.")
            return pd.DataFrame()

        sheet_name = os.getenv('PROPERTY_SHEET_NAME', 'Properties')

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

        df['Price_AED'] = pd.to_numeric(df['Price_AED'], errors='coerce')
        df['Bedrooms'] = pd.to_numeric(df['Bedrooms'], errors='coerce')

        for col in EXPECTED_COLUMNS:
            if col not in df.columns:
                df[col] = ''

        df.fillna('', inplace=True)

        logging.info(f"Successfully loaded {len(df)} properties from sheet '{sheet_name}'.")
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

def filter_properties(df, filters):
    """
    Filters the property DataFrame based on criteria extracted by the LLM.
    """
    if filters is None or not isinstance(filters, dict):
        return pd.DataFrame()

    filtered_df = df.copy()

    for key, details in filters.items():
        if key not in filtered_df.columns:
            logging.warning(f"Filter key '{key}' not found in property columns. Skipping.")
            continue

        try:
            operator = details.get('operator')
            value = details.get('value')

            if key in ['Price_AED', 'Bedrooms']:
                value = float(value)
                if operator == '<':
                    filtered_df = filtered_df[filtered_df[key] <= value]
                elif operator == '>':
                    filtered_df = filtered_df[filtered_df[key] >= value]
                elif operator == '=':
                    filtered_df = filtered_df[filtered_df[key] == value]

            elif key in ['emirate', 'city', 'area', 'developer', 'Title', 'building name']:
                filtered_df = filtered_df[filtered_df[key].str.contains(str(value), case=False, na=False)]

        except (ValueError, TypeError) as e:
            logging.error(f"Error applying filter for key '{key}' with value '{value}': {e}")
            continue

    logging.info(f"Filtering completed. Found {len(filtered_df)} matching properties.")
    return filtered_df

SHEET2_COLUMNS = [
    'PropertyID', 'PropertyName', 'Description', 'WeekdayPrice', 'WeekendPrice',
    'MonthlyPrice', 'Guests', 'City', 'Neighborhood', 'Amenities',
    'BookingLink', 'VideoURL', 'ImageURL1', 'ImageURL2', 'ImageURL3',
    'ImageURL4', 'ImageURL5', 'ImageURL6', 'ImageURL7', 'ImageURL8',
    'ImageURL9', 'ImageURL10', 'PropertyName_en', 'Description_en'
]

def get_sheet2_data():
    """
    Fetches all property data from 'Sheet2' of the Google Sheet specified by
    environment variables and loads it into a pandas DataFrame.
    Handles the specific column structure of Sheet2.
    """
    try:
        sheet_id_input = os.getenv('PROPERTY_SHEET_ID')
        actual_sheet_id = extract_sheet_id_from_url(sheet_id_input)

        if not actual_sheet_id:
            logging.error(f"PROPERTY_SHEET_ID ('{sheet_id_input}') is invalid or could not be parsed for Sheet2.")
            return pd.DataFrame()

        sheet_name = 'Sheet2'

        creds_json_str = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
        if not creds_json_str:
            logging.error("GOOGLE_SHEETS_CREDENTIALS environment variable not set. Cannot fetch Sheet2 data.")
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
        logging.info(f"Initial load from '{sheet_name}': {len(df)} records, columns: {df.columns.tolist()}")

        for col in SHEET2_COLUMNS:
            if col not in df.columns:
                logging.warning(f"Column '{col}' expected in '{sheet_name}' not found. Adding as empty column.")
                df[col] = ''
        df = df[SHEET2_COLUMNS]

        price_cols = ['WeekdayPrice', 'WeekendPrice', 'MonthlyPrice']
        for col in price_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df['Guests'] = pd.to_numeric(df['Guests'], errors='coerce')

        numeric_cols_to_fill_na = ['WeekdayPrice', 'WeekendPrice', 'MonthlyPrice', 'Guests']
        for col in numeric_cols_to_fill_na:
            df[col] = df[col].fillna(0)

        for col in df.columns:
            if col not in numeric_cols_to_fill_na:
                 df[col] = df[col].astype(str).fillna('')

        if 'PropertyID' in df.columns:
            df['PropertyID'] = df['PropertyID'].astype(str).str.replace(r'\.0$', '', regex=True)

        logging.info(f"Successfully processed {len(df)} properties from sheet '{sheet_name}'. Final columns: {df.columns.tolist()}")
        return df

    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(f"Spreadsheet with actual ID '{actual_sheet_id}' not found or access denied (for Sheet2).")
        return pd.DataFrame()
    except gspread.exceptions.WorksheetNotFound:
        logging.error(f"Worksheet named '{sheet_name}' not found in Spreadsheet ID: {actual_sheet_id}.")
        return pd.DataFrame()
    except Exception as e:
        logging.error(f"Error accessing or processing Google Sheet '{sheet_name}': {e}", exc_info=True)
        return pd.DataFrame()