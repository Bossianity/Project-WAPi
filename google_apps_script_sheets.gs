// Copyright 2024 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/**
 * @OnlyCurrentDoc
 *
 * The above comment directs Apps Script to limit the scope of file
 * access for this script to only the current document.
 */

// --- Configuration ---
// Replace these placeholders with your actual Flask webhook URL and secret token.
const WEBHOOK_URL = "YOUR_FLASK_WEBHOOK_URL_HERE";
const SECRET_TOKEN = "YOUR_SECRET_TOKEN_HERE";

/**
 * This script sends a notification to a webhook URL when a Google Sheet is edited.
 * It is designed to be used with a Flask application that processes these notifications.
 *
 * To use this script:
 * 1. Replace the WEBHOOK_URL and SECRET_TOKEN placeholders above with your actual values.
 * 2. Open your Google Sheet.
 * 3. Go to "Extensions" > "Apps Script".
 * 4. Copy and paste this entire script into the editor, replacing any existing code.
 * 5. Save the script (File > Save).
 * 6. The `onEdit` trigger is usually set up automatically for a function with this exact name.
 *    To verify or manually set it up:
 *    a. Click on the "Triggers" icon (looks like a clock) in the left sidebar.
 *    b. Click "Add Trigger".
 *    c. Ensure the function "onEdit" is selected.
 *    d. Choose "From spreadsheet" as the event source.
 *    e. Select "On edit" as the event type.
 *    f. Click "Save".
 *    g. You may be asked to authorize the script. Follow the prompts to grant necessary permissions.
 */

/**
 * Automatically runs when a user edits a cell in the spreadsheet.
 * This function sends a POST request to the configured WEBHOOK_URL
 * with the spreadsheet ID and a secret token.
 *
 * @param {Object} e The event object.
 */
function onEdit(e) {
  try {
    // Get the ID of the active spreadsheet.
    const spreadsheetId = e.source.getId();

    // Prepare the payload.
    const payload = {
      documentId: spreadsheetId,
      secretToken: SECRET_TOKEN
    };

    // Convert the payload to a JSON string.
    const jsonPayload = JSON.stringify(payload);

    // Define the options for the POST request.
    const options = {
      method: "post",
      contentType: "application/json",
      payload: jsonPayload,
      muteHttpExceptions: true // Important to handle errors manually.
    };

    // Make the POST request.
    Logger.log('Sending POST request to: ' + WEBHOOK_URL);
    Logger.log('Payload: ' + jsonPayload);
    const response = UrlFetchApp.fetch(WEBHOOK_URL, options);

    // Log the response for debugging.
    Logger.log('Response Code: ' + response.getResponseCode());
    Logger.log('Response Content: ' + response.getContentText());

  } catch (error) {
    // Log any errors that occur.
    Logger.log('Error in onEdit function: ' + error.toString());
  }
}
