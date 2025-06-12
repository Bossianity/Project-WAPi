# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_google_credentials():
    """
    Retrieves Google API credentials from an environment variable.

    Returns:
        google.oauth2.service_account.Credentials: Credentials object or None if an error occurs.
    """
    credentials_json_str = os.getenv('GOOGLE_APPLICATION_CREDENTIALS_JSON')
    if not credentials_json_str:
        logging.error("GOOGLE_APPLICATION_CREDENTIALS_JSON environment variable not set.")
        return None

    try:
        credentials_info = json.loads(credentials_json_str)
    except json.JSONDecodeError as e:
        logging.error(f"Error parsing GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}")
        return None

    SCOPES = [
        'https://www.googleapis.com/auth/documents.readonly',
        'https://www.googleapis.com/auth/spreadsheets.readonly',
        'https://www.googleapis.com/auth/drive.metadata.readonly' # Added for get_google_drive_file_mime_type
    ]

    try:
        creds = service_account.Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        return creds
    except Exception as e:
        logging.error(f"Error creating credentials from service account info: {e}")
        return None

def get_google_doc_content(document_id):
    """
    Fetches and extracts text content from a Google Document.

    Args:
        document_id (str): The ID of the Google Document.

    Returns:
        str: The extracted text content of the document, or None if an error occurs.
    """
    creds = get_google_credentials()
    if not creds:
        logging.error("Failed to get Google credentials. Cannot fetch document.")
        return None

    try:
        service = build('docs', 'v1', credentials=creds)
        logging.info(f"Fetching Google Doc with ID: {document_id}")
        document = service.documents().get(documentId=document_id).execute()

        doc_content_parts = []
        body_content = document.get('body', {}).get('content', [])

        for element in body_content:
            if 'paragraph' in element:
                for paragraph_element in element.get('paragraph', {}).get('elements', []):
                    if 'textRun' in paragraph_element:
                        text_run = paragraph_element.get('textRun')
                        if text_run and 'content' in text_run:
                            doc_content_parts.append(text_run.get('content'))
            # Google Docs API might have other content types like tables, lists etc.
            # This basic implementation focuses on paragraphs and textRuns.
            # For a more comprehensive extraction, one would need to handle these other types.

        full_content = "".join(doc_content_parts) # Google Docs content often includes \n directly.
        logging.info(f"Successfully fetched and parsed content for Google Doc ID: {document_id}. Length: {len(full_content)}")
        return full_content
    except Exception as e:
        logging.error(f"Error fetching or parsing Google Doc (ID: {document_id}): {e}", exc_info=True)
        return None

def get_google_sheet_content(spreadsheet_id):
    """
    Fetches and extracts text content from all sheets in a Google Spreadsheet.
    Content from each sheet is concatenated. Cells in a row are tab-separated.
    Rows are newline-separated. Sheets are separated by a double newline and a title.

    Args:
        spreadsheet_id (str): The ID of the Google Spreadsheet.

    Returns:
        str: The combined text content of all sheets, or None if an error occurs.
    """
    creds = get_google_credentials()
    if not creds:
        logging.error("Failed to get Google credentials. Cannot fetch spreadsheet.")
        return None

    try:
        service = build('sheets', 'v4', credentials=creds)
        logging.info(f"Fetching Google Sheet with ID: {spreadsheet_id}")

        sheet_metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = sheet_metadata.get('sheets', [])

        if not sheets:
            logging.warning(f"No sheets found in Google Sheet ID: {spreadsheet_id}")
            return "" # Return empty string if no sheets

        all_sheets_content_parts = []

        for sheet in sheets:
            sheet_properties = sheet.get('properties', {})
            sheet_title = sheet_properties.get('title')

            if not sheet_title:
                logging.warning(f"Skipping sheet without a title in spreadsheet ID: {spreadsheet_id}")
                continue

            logging.info(f"Fetching content for sheet: '{sheet_title}' in spreadsheet ID: {spreadsheet_id}")
            # The range covering the entire sheet can be specified by just its title
            result = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=sheet_title).execute()
            rows = result.get('values', [])

            sheet_rows_content = []
            if rows:
                for row_cells in rows:
                    # Ensure all cell values are strings before joining
                    processed_row = [str(cell) if cell is not None else "" for cell in row_cells]
                    sheet_rows_content.append("\t".join(processed_row))

            current_sheet_full_content = "\n".join(sheet_rows_content)
            all_sheets_content_parts.append(f"Sheet: {sheet_title}\n{current_sheet_full_content}")

        combined_content = "\n\n".join(all_sheets_content_parts)
        logging.info(f"Successfully fetched and parsed content for Google Sheet ID: {spreadsheet_id}. Total length: {len(combined_content)}")
        return combined_content

    except Exception as e:
        logging.error(f"Error fetching or parsing Google Sheet (ID: {spreadsheet_id}): {e}", exc_info=True)
        return None

if __name__ == '__main__':
    # Example Usage (requires environment variable GOOGLE_APPLICATION_CREDENTIALS_JSON to be set)
    # And the specified DOC_ID and SHEET_ID to exist and be accessible by the service account

    # Test Google Docs
    test_doc_id = os.getenv("TEST_GOOGLE_DOC_ID") # Create this env var with a real Doc ID
    if test_doc_id:
        logging.info(f"--- Testing get_google_doc_content with DOC_ID: {test_doc_id} ---")
        doc_text = get_google_doc_content(test_doc_id)
        if doc_text is not None:
            logging.info(f"Fetched Google Doc Content (first 500 chars):\n{doc_text[:500]}")
        else:
            logging.error("Failed to fetch Google Doc content.")
    else:
        logging.info("TEST_GOOGLE_DOC_ID environment variable not set. Skipping Google Docs test.")

    # Test Google Sheets
    test_sheet_id = os.getenv("TEST_GOOGLE_SHEET_ID") # Create this env var with a real Sheet ID
    if test_sheet_id:
        logging.info(f"--- Testing get_google_sheet_content with SHEET_ID: {test_sheet_id} ---")
        sheet_text = get_google_sheet_content(test_sheet_id)
        if sheet_text is not None:
            logging.info(f"Fetched Google Sheet Content (first 500 chars):\n{sheet_text[:500]}")
        else:
            logging.error("Failed to fetch Google Sheet content.")
    else:
        logging.info("TEST_GOOGLE_SHEET_ID environment variable not set. Skipping Google Sheets test.")

    if not test_doc_id and not test_sheet_id:
        logging.info("Set TEST_GOOGLE_DOC_ID and/or TEST_GOOGLE_SHEET_ID environment variables to run example usage.")

def get_google_drive_file_mime_type(file_id: str) -> str | None:
    """
    Fetches the MIME type of a Google Drive file.

    Args:
        file_id (str): The ID of the Google Drive file.

    Returns:
        str | None: The MIME type of the file, or None if an error occurs or MIME type isn't found.
    """
    creds = get_google_credentials()
    if not creds:
        logging.error(f"Failed to get Google credentials. Cannot fetch MIME type for file ID: {file_id}")
        return None

    try:
        service = build('drive', 'v3', credentials=creds)
        logging.info(f"Fetching MIME type for Google Drive file ID: {file_id}")

        # Request only the mimeType field for efficiency
        file_metadata = service.files().get(fileId=file_id, fields='mimeType').execute()

        mime_type = file_metadata.get('mimeType')
        if mime_type:
            logging.info(f"Successfully fetched MIME type for file ID {file_id}: {mime_type}")
            return mime_type
        else:
            logging.warning(f"MIME type not found in metadata for file ID {file_id}. Metadata: {file_metadata}")
            return None

    except Exception as e:
        logging.error(f"Error fetching MIME type for Google Drive file ID {file_id}: {e}", exc_info=True)
        return None
