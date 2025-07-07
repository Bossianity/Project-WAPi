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

# Columns for Sheet2
SHEET2_COLUMNS = [
    'PropertyID', 'PropertyName', 'Description', 'WeekdayPrice', 'WeekendPrice',
    'MonthlyPrice', 'Guests', 'City', 'Neighborhood', 'Amenities',
    'BookingLink', 'VideoURL', 'ImageURL1', 'ImageURL2', 'ImageURL3',
    'ImageURL4', 'ImageURL5', 'ImageURL6', 'ImageURL7', 'ImageURL8',
    'ImageURL9', 'ImageURL10'
]

def get_sheet2_data():
    """
    Fetches all property data from 'Sheet2' of the Google Sheet specified by
    environment variables and loads it into a pandas DataFrame.
    Handles the specific column structure of Sheet2.
    """
    try:
        sheet_id = os.getenv('PROPERTY_SHEET_ID')
        if not sheet_id:
            logging.error("PROPERTY_SHEET_ID environment variable not set. Cannot fetch Sheet2 data.")
            return pd.DataFrame()

        sheet_name = 'Sheet2'  # Explicitly target 'Sheet2'

        creds_json_str = os.getenv('GOOGLE_SHEETS_CREDENTIALS')
        if not creds_json_str:
            logging.error("GOOGLE_SHEETS_CREDENTIALS environment variable not set. Cannot fetch Sheet2 data.")
            return pd.DataFrame()

        creds_info = json.loads(creds_json_str)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, SCOPE)
        client = gspread.authorize(creds)

        logging.info(f"Attempting to open sheet '{sheet_name}' with ID: {sheet_id}")
        worksheet = client.open_by_key(sheet_id).worksheet(sheet_name)
        records = worksheet.get_all_records()

        if not records:
            logging.warning(f"No data found in Google Sheet '{sheet_name}' with ID: {sheet_id}")
            return pd.DataFrame()

        df = pd.DataFrame(records)
        logging.info(f"Initial load from '{sheet_name}': {len(df)} records, columns: {df.columns.tolist()}")

        # --- Data Cleaning and Type Conversion for Sheet2 ---
        # Ensure all expected columns exist, fill missing ones with empty strings
        for col in SHEET2_COLUMNS:
            if col not in df.columns:
                logging.warning(f"Column '{col}' expected in '{sheet_name}' not found. Adding as empty column.")
                df[col] = ''

        # Select only the expected columns to maintain a consistent structure and order
        df = df[SHEET2_COLUMNS]

        # Convert price columns to numeric, coercing errors to NaN
        price_cols = ['WeekdayPrice', 'WeekendPrice', 'MonthlyPrice']
        for col in price_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            else: # Should not happen due to earlier loop, but as safeguard
                df[col] = pd.Series(dtype='float64')


        # Convert 'Guests' to numeric, coercing errors to NaN
        if 'Guests' in df.columns:
            df['Guests'] = pd.to_numeric(df['Guests'], errors='coerce')
        else: # Safeguard
            df['Guests'] = pd.Series(dtype='float64')


        # Fill NaN values (from coerce errors or empty cells in numeric cols) with a placeholder like 0 or None.
        # For prices and guests, 0 might be a valid value, so using None (which pandas converts to NaN then fillna can handle)
        # or an empty string if downstream code expects strings. Let's use empty string for simplicity with text display later.
        # However, for filtering, numeric NaNs are better. Let's fill with 0 for numeric, and empty string for others.

        # For numeric columns, fill NaN with 0 or a suitable numeric placeholder.
        # If 0 is not appropriate (e.g. price cannot be 0), consider None or handle NaN in consuming code.
        # For this use case, let's assume 0 is acceptable for missing numeric values or we'll handle it in display.
        numeric_cols_to_fill_na = ['WeekdayPrice', 'WeekendPrice', 'MonthlyPrice', 'Guests']
        for col in numeric_cols_to_fill_na:
            if col in df.columns: # Ensure column exists
                 df[col] = df[col].fillna(0)


        # Convert all other columns to string type to ensure consistency, especially for IDs, links, text.
        # This also handles cases where numbers might be read as int/float but should be strings (e.g. PropertyID).
        for col in df.columns:
            if col not in numeric_cols_to_fill_na: # Avoid re-converting already numeric columns
                 df[col] = df[col].astype(str).fillna('') # Convert to string and fill any remaining NaNs (e.g. from all-empty original columns)

        # Specifically ensure PropertyID is string and has no '.0' if it was numeric then string
        if 'PropertyID' in df.columns:
            df['PropertyID'] = df['PropertyID'].astype(str).str.replace(r'\.0$', '', regex=True)


        logging.info(f"Successfully processed {len(df)} properties from sheet '{sheet_name}'. Final columns: {df.columns.tolist()}")
        return df

    except gspread.exceptions.WorksheetNotFound:
        logging.error(f"Worksheet named '{sheet_name}' not found in Google Sheet ID: {sheet_id}.")
        return pd.DataFrame()
    except Exception as e:
        logging.error(f"Error accessing or processing Google Sheet '{sheet_name}': {e}", exc_info=True)
        return pd.DataFrame()