import os
import json
import gspread
import pandas as pd
import logging
from oauth2client.service_account import ServiceAccountCredentials

# --- Configuration ---
SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

# Expected columns in the Google Sheet
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
        sheet_id = os.getenv('PROPERTY_SHEET_ID')
        if not sheet_id:
            logging.error("PROPERTY_SHEET_ID environment variable not set.")
            return pd.DataFrame()

        # **MODIFIED**: Allow specifying sheet name via env var, default to 'Properties'
        sheet_name = os.getenv('PROPERTY_SHEET_NAME', 'Properties') 

        creds_json_str = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
        if not creds_json_str:
            logging.error("GOOGLE_SHEETS_CREDENTIALS environment variable not set.")
            return pd.DataFrame()

        creds_info = json.loads(creds_json_str)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, SCOPE)
        client = gspread.authorize(creds)

        # **MODIFIED**: Open sheet by name instead of the hardcoded first sheet
        worksheet = client.open_by_key(sheet_id).worksheet(sheet_name)
        records = worksheet.get_all_records()

        if not records:
            logging.warning(f"No data found in Google Sheet '{sheet_name}' with ID: {sheet_id}")
            return pd.DataFrame()

        df = pd.DataFrame(records)

        # --- Data Cleaning and Type Conversion ---
        df['Price_AED'] = pd.to_numeric(df['Price_AED'], errors='coerce')
        df['Bedrooms'] = pd.to_numeric(df['Bedrooms'], errors='coerce')

        for col in EXPECTED_COLUMNS:
            if col not in df.columns:
                df[col] = ''

        df.fillna('', inplace=True)

        logging.info(f"Successfully loaded {len(df)} properties from sheet '{sheet_name}'.")
        return df

    except gspread.exceptions.WorksheetNotFound:
        logging.error(f"Worksheet named '{sheet_name}' not found in Google Sheet ID: {sheet_id}. Please check the sheet name and environment variable.")
        return pd.DataFrame()
    except Exception as e:
        logging.error(f"Error accessing Google Sheet: {e}", exc_info=True)
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