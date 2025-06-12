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
 * This script adds a custom menu to Google Docs to manually trigger a sync
 * notification to a webhook URL. This is useful for telling a chatbot
 * or other service to re-index or process the document's content.
 *
 * To use this script:
 * 1. Replace the WEBHOOK_URL and SECRET_TOKEN placeholders above with your actual values.
 * 2. Open your Google Document.
 * 3. Go to "Extensions" > "Apps Script".
 * 4. Copy and paste this entire script into the editor, replacing any existing code.
 * 5. Save the script (File > Save).
 * 6. Reload the Google Document. You should see a new menu item "Chatbot Sync".
 * 7. You may be asked to authorize the script when you first try to run "Sync Now".
 *    Follow the prompts to grant necessary permissions.
 */

/**
 * Runs when the Google Document is opened.
 * This function creates a custom menu item "Chatbot Sync" with an option "Sync Now".
 */
function onOpen() {
  DocumentApp.getUi()
      .createMenu('Chatbot Sync')
      .addItem('Sync Now', 'triggerManualSync')
      .addToUi();
}

/**
 * Sends a notification to the configured WEBHOOK_URL with the document ID.
 * This function is called when the user clicks "Sync Now" from the custom menu.
 */
function triggerManualSync() {
  const ui = DocumentApp.getUi();
  try {
    // Get the ID of the active document.
    const documentId = DocumentApp.getActiveDocument().getId();

    // Prepare the payload.
    const payload = {
      documentId: documentId,
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
    const responseCode = response.getResponseCode();
    const responseContent = response.getContentText();

    // Log the response for debugging.
    Logger.log('Response Code: ' + responseCode);
    Logger.log('Response Content: ' + responseContent);

    // Provide user feedback.
    if (responseCode === 200 || responseCode === 202) {
      ui.alert('Sync initiated successfully!');
    } else {
      ui.alert('Sync failed. Response code: ' + responseCode + '. Please check the script log for details.');
    }

  } catch (error) {
    // Log any errors that occur and inform the user.
    Logger.log('Error in triggerManualSync function: ' + error.toString());
    ui.alert('Sync failed due to an error: ' + error.message + ' Please check the script log.');
  }
}
