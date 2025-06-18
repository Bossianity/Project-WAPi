import os
import re
import json
import gspread
import pandas as pd
import logging
from oauth2client.service_account import ServiceAccountCredentials

# --- Configuration ---
SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

# Expected columns in the Google Sheet.
# IMPORTANT: Please verify these column names match the headers in your Google Sheet.
EXPECTED_COLUMNS = [
    'PropertyID', 'PropertyName', 'Description', 'WeekdayPrice', 'WeekendPrice', 'MonthlyPrice', 'Guests', 'City',
    'Neighborhood', 'Amenities', 'BookingLink', 'VideoURL', 'ImageURL1', 'ImageURL2', 'ImageURL3'
]

def get_sheet_data():
    """
    Fetches all property data from the Google Sheet specified by environment
    variables and loads it into a pandas DataFrame.
    """
    try:
        sheet_id = os.getenv('PROPERTY_SHEET_ID')
        if sheet_id and "docs.google.com/spreadsheets/d/" in sheet_id:
            match = re.search(r"/spreadsheets/d/([^/]+)", sheet_id)
            if match:
                sheet_id = match.group(1)
                logging.info(f"Extracted Google Sheet ID: {sheet_id} from URL.")
            else:
                logging.warning(f"PROPERTY_SHEET_ID looks like a URL but could not extract ID: {sheet_id}")
        if not sheet_id:
            logging.error("PROPERTY_SHEET_ID environment variable not set.")
            return pd.DataFrame()

        sheet_name = os.getenv('PROPERTY_SHEET_NAME', 'Properties') # Default sheet name
    # Check if the fetched sheet_name looks like a URL, which is incorrect.
        if sheet_name and "docs.google.com/spreadsheets/d/" in sheet_name:
            # Use the same default as in os.getenv for consistency
            default_sheet_name_for_fallback = 'Properties'
            logging.warning(
                f"PROPERTY_SHEET_NAME environment variable ('{sheet_name}') appears to be a URL. "
                f"This is incorrect. Falling back to default sheet name '{default_sheet_name_for_fallback}'. "
                "Please ensure PROPERTY_SHEET_NAME is set to the actual tab name of the sheet."
            )
            sheet_name = default_sheet_name_for_fallback

        creds_json_str = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
        if not creds_json_str:
            logging.error("GOOGLE_SHEETS_CREDENTIALS environment variable not set.")
            return pd.DataFrame()

        creds_info = json.loads(creds_json_str)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, SCOPE)
        client = gspread.authorize(creds)

        worksheet = client.open_by_key(sheet_id).worksheet(sheet_name)
        records = worksheet.get_all_records()

        if not records:
            logging.warning(f"No data found in Google Sheet '{sheet_name}' with ID: {sheet_id}")
            return pd.DataFrame()

        df = pd.DataFrame(records)

        # --- Data Cleaning and Type Conversion ---
        df['WeekdayPrice'] = pd.to_numeric(df['WeekdayPrice'], errors='coerce')
        df['WeekendPrice'] = pd.to_numeric(df['WeekendPrice'], errors='coerce')
        df['MonthlyPrice'] = pd.to_numeric(df['MonthlyPrice'], errors='coerce')
        df['Guests'] = pd.to_numeric(df['Guests'], errors='coerce')

        for col in EXPECTED_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA # Use pandas NA for missing columns

        df.fillna(pd.NA, inplace=True)

        logging.info(f"Successfully loaded {len(df)} properties from sheet '{sheet_name}'.")
        return df

    except gspread.exceptions.WorksheetNotFound:
        logging.error(f"Worksheet named '{sheet_name}' not found in Google Sheet ID: {sheet_id}.")
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

            if key in ['WeekdayPrice', 'WeekendPrice', 'MonthlyPrice', 'Guests']:
                # Ensure the column exists and has numeric data before filtering
                if pd.api.types.is_numeric_dtype(filtered_df[key]):
                    value = float(value)
                    if operator == '<':
                        filtered_df = filtered_df[filtered_df[key] <= value]
                    elif operator == '>':
                        filtered_df = filtered_df[filtered_df[key] >= value]
                    elif operator == '=':
                        filtered_df = filtered_df[filtered_df[key] == value]

            elif key in ['City', 'Neighborhood', 'PropertyName', 'Amenities']:
                 # Ensure the column exists and is of string type
                if pd.api.types.is_string_dtype(filtered_df[key]) or pd.api.types.is_object_dtype(filtered_df[key]):
                    filtered_df = filtered_df[filtered_df[key].str.contains(str(value), case=False, na=False)]

        except (ValueError, TypeError) as e:
            logging.error(f"Error applying filter for key '{key}' with value '{value}': {e}")
            continue

    logging.info(f"Filtering completed. Found {len(filtered_df)} matching properties.")
    return filtered_df
