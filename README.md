# Dynamic Google Docs/Sheets Integration for RAG Chatbot

## Overview

This project enables a Retrieval Augmented Generation (RAG) chatbot to dynamically use content from Google Documents and Google Sheets as its knowledge base. Updates in specified Google Docs or Sheets trigger a webhook, causing the backend to fetch the latest content, process it, and update the RAG system's vector store (FAISS). This ensures the chatbot's responses are based on the most current information available in these documents.

The system is designed for deployment on Render, utilizing Google Apps Script for event detection in Google Workspace, and a Flask backend for webhook handling and RAG pipeline management.

## Features

*   **Dynamic Knowledge Base:** Automatically updates the RAG chatbot's knowledge from Google Docs and Sheets.
*   **Google Docs Integration:** Manually trigger updates from Google Docs via a custom menu.
*   **Google Sheets Integration:** Automatically trigger updates when a Google Sheet is edited.
*   **Secure Webhook:** Uses a secret token to authenticate webhook calls from Google Apps Script.
*   **Asynchronous Processing:** Webhook responses are quick, with content fetching and RAG processing handled in the background.
*   **FAISS Vector Store:** Utilizes FAISS for efficient similarity searches in the RAG pipeline.
*   **Render Deployment:** Optimized for deployment on the Render platform.
*   **OpenAI Integration:** Uses OpenAI for embeddings and chat completions (configurable).
*   **Pause/Resume Functionality:** Supports global and conversation-specific pause and resume of bot responses via chat commands.
*   **Outbound WhatsApp Campaigns:** Allows agents to initiate personalized outbound messaging campaigns using Google Sheets.

## Pause/Resume Functionality

This feature allows for administrative control over the bot's responsiveness directly through WhatsApp messages. The bot supports both a global pause (affecting all users) and the ability to pause/resume interactions with specific user IDs.

### Commands

The following commands can be sent to the bot's WhatsApp number:

*   `bot pause all`
    *   Pauses the bot globally. It will stop responding to all users.
*   `bot resume all`
    *   Resumes the bot globally and clears all specific conversation pauses.
*   `bot pause <target_user_id>`
    *   Pauses the bot for a specific user. Replace `<target_user_id>` with the user's WhatsApp ID (e.g., `11234567890@s.whatsapp.net` or just `+11234567890` - the exact format added to the pause list will be what the command provides, ensure it matches the `sender` ID format seen by the bot, which is typically `xxxxxxxxxxx@s.whatsapp.net`).
*   `bot resume <target_user_id>`
    *   Resumes the bot for a specific user.

**Command Case Sensitivity:**
*   The command keywords (e.g., "bot pause all") are **case-insensitive**. So, `bot pause all`, `Bot Pause All`, or `BOT PAUSE ALL` will all work.
*   The `<target_user_id>` is treated as **case-sensitive** by the system when adding or removing from the pause list. However, WhatsApp IDs themselves are typically numbers and not case-sensitive in nature.

### Access Control

**Important:** Any user interacting with the bot's WhatsApp number can issue these commands. There is no authorization or restriction based on the sender's number. This means any user can pause or resume the bot globally or for any specific conversation if they know the command and the target user ID.

### Behavior

*   **Global Pause (`bot pause all`):**
    *   When activated, the bot will stop processing new messages for responses for all users.
    *   Incoming messages will still be received by the webhook and logged by the system, but no response will be generated or sent back to any user.
    *   The user issuing the `bot pause all` command will receive a confirmation: "Bot is now globally paused."
*   **Global Resume (`bot resume all`):**
    *   The bot resumes normal message processing and response generation for all users.
    *   This command also clears any specific conversation pauses that were previously set. All users will be able to interact with the bot again.
    *   The user issuing the `bot resume all` command will receive a confirmation: "Bot is now globally resumed. All specific conversation pauses have been cleared."
*   **Specific Conversation Pause (`bot pause <target_user_id>`):**
    *   The bot will stop processing messages for responses only from the specified `<target_user_id>`.
    *   Messages from this paused user will be logged but not responded to.
    *   Other users are unaffected and can continue to interact with the bot unless a global pause is also active.
    *   The user issuing the command (e.g., an admin) will receive a confirmation like: "Bot interactions will be paused for: <target_user_id>".
    *   If the command format is invalid (e.g., no `<target_user_id>` provided), the issuer receives: "Invalid command format. Use: bot pause <target_user_id>".
*   **Specific Conversation Resume (`bot resume <target_user_id>`):**
    *   The bot will resume processing messages and responding to the specified `<target_user_id>`.
    *   The user issuing the command will receive a confirmation: "Bot interactions will be resumed for: <target_user_id>".
    *   If the command format is invalid, the issuer receives: "Invalid command format. Use: bot resume <target_user_id>".

### State Persistence

**Important:** The pause states (both global `is_globally_paused` and the `paused_conversations` set) are stored **in-memory**. This means that if the bot application restarts or is redeployed (e.g., on Render due to a new deployment, manual restart, or platform maintenance), all pause states will be lost.

Upon restart, the bot will default to its initial active state:
*   `is_globally_paused` will be `False`.
*   `paused_conversations` will be an empty set.

Any previous pause configurations will need to be reapplied manually using the chat commands if desired after a restart.

## Outbound WhatsApp Campaigns

This feature allows authorized agents to initiate personalized outbound WhatsApp messaging campaigns to a list of contacts defined in a Google Sheet.

### Purpose

To enable targeted, personalized outreach to clients or leads for promotions, updates, or follow-ups, directly managed via a Google Sheet and triggered by a simple bot command.

### Agent Commands

*   `bot start outreach`
    *   Initiates an outreach campaign using a default Google Sheet ID specified by the `DEFAULT_OUTREACH_SHEET_ID` environment variable.
    *   The agent will be notified if the default ID is not set.
*   `bot start outreach <specific_google_sheet_id>`
    *   Initiates an outreach campaign using the Google Sheet ID provided in the command.
    *   Example: `bot start outreach 1aBcDeFgHiJkLmNoPqRsTuVwXyZ-0123456789`

Upon initiation, the agent receives a confirmation. Once the campaign is complete, a summary report (sent, failed, skipped counts) is sent back to the agent.

### Environment Variables

The following environment variables are used to configure the outbound campaign feature:

*   `DEFAULT_OUTREACH_SHEET_ID` (Optional):
    *   The Google Sheet ID to be used for campaigns when the `bot start outreach` command is used without a specific ID.
    *   If not set, agents must always provide a specific Sheet ID.
*   `GOOGLE_SHEETS_CREDENTIALS` (Required):
    *   The JSON content of the Google Service Account key. This service account must have permissions to read from and write to any Google Sheet intended for outreach campaigns.
*   `BUSINESS_NAME` (Optional, defaults to "Our Clinic/Business Name"):
    *   The name of your business or clinic. This is used in the default personalized message template.
    *   Example: "Hi {ClientName}, this is Layla from {BUSINESS_NAME}..."
*   `OUTREACH_MESSAGE_DELAY_SECONDS` (Optional, defaults to 5):
    *   The delay in seconds between sending each message in a campaign. This helps in avoiding rate limits by WhatsApp or the messaging API provider.

### Google Sheet Structure

The Google Sheet used for campaigns must adhere to a specific structure. The bot expects the first row to be headers.

**Required Columns:**

*   `PhoneNumber`: The WhatsApp number of the recipient.
    *   *Expected Format*: E.164 format (e.g., `+1234567890`) or a format compatible with the `WASENDER_API_TOKEN`.
*   `ClientName`: The name of the client or lead. Used for personalizing the message.
*   `InterestedService`: The service or topic the client is interested in. Used for personalization.
*   `MessageStatus`: The bot uses this column to track the status of each message. Initially, it can be blank or have statuses like "Pending". The bot will update it after attempting to send a message.

**Optional Column:**

*   `LastContactedDate`: If this column exists, the bot will update it with a timestamp when a message is sent or an attempt is made.

The bot dynamically identifies columns by their header names, so the order of columns does not strictly matter as long as the required headers are present.

### Google Service Account Permissions

The Google Service Account whose JSON key is provided in `GOOGLE_SHEETS_CREDENTIALS` **must have "Editor" permissions** on any Google Sheet used for outreach campaigns. This is because the bot needs to read the contact list and then write back the `MessageStatus` and `LastContactedDate`.

### MessageStatus Values

The bot will update the `MessageStatus` column for each row with one ofthe following values:

*   `Sent`: Message was successfully sent.
*   `Failed - API Error`: The WhatsApp API (WaSenderAPI) reported an error during sending.
*   `Failed - Missing PhoneNumber`: The `PhoneNumber` field was blank for that row.
*   *(Other specific error messages may be added in future updates)*

Rows with a `MessageStatus` like "Sent", "Replied", "Completed", or "Success" (case-insensitive check) will be skipped if the campaign is run again on the same sheet, to prevent re-messaging already processed contacts.

### Inter-Message Delay

The `OUTREACH_MESSAGE_DELAY_SECONDS` environment variable controls the pause duration between sending consecutive messages. This is crucial for:

*   Respecting potential rate limits imposed by WhatsApp or the WaSenderAPI.
*   Reducing the risk of being flagged as spam.
*   Distributing the load on the messaging service.

The default is 5 seconds, but you can adjust this based on your provider's guidelines and campaign volume.

### Access Control Note

Currently, any user who can message the bot can trigger an outreach campaign if they know the command. Future updates might include role-based access control for this feature.

### IMPORTANT: User Consent & WhatsApp Policy

**Ensure all recipients in the outreach list have given explicit consent (opted-in) to receive these messages via WhatsApp. Sending unsolicited messages violates WhatsApp's policies and can lead to your number being blocked. Use this feature responsibly and in compliance with all applicable regulations and WhatsApp's Commerce Policy and Business Policy.**

## Architecture

The data flow is as follows:

1.  **Google Apps Script (GAS) Event Detection:**
    *   **Google Sheets:** An `onEdit(e)` trigger in GAS fires when a user edits the sheet.
    *   **Google Docs:** An `onOpen()` trigger creates a custom menu. A user action ("Sync Now") on this menu initiates the process.
2.  **Webhook Notification:**
    *   The GAS script sends a POST request (webhook) to the Flask backend (`/webhook-google-sync`). This request includes the `documentId` and a `secretToken`.
3.  **Flask Backend (Webhook Handling):**
    *   The Flask app receives the webhook call.
    *   It authenticates the request by verifying the `secretToken`.
    *   If valid, it acknowledges the request immediately (202 Accepted) and submits a background task to a `ThreadPoolExecutor`.
4.  **Background Task (Content Fetching & RAG Update):**
    *   The background task in the Flask app:
        *   Determines the file's MIME type using the Google Drive API.
        *   Fetches the content of the Google Doc or Sheet using the appropriate Google API (Docs API or Sheets API) via functions in `google_drive_handler.py`.
        *   Processes the fetched text content:
            *   Deletes any existing data associated with that `documentId` from the FAISS vector store.
            *   Splits the new content into chunks.
            *   Creates embeddings for these chunks using OpenAI.
            *   Adds the new chunks and their embeddings to the FAISS vector store.
            *   Saves the updated FAISS index. (Handled by `rag_handler.py`)
5.  **Chatbot Usage:**
    *   The chatbot (via `script.py`'s main webhook `/webhook`) uses the updated FAISS vector store for its RAG capabilities, providing answers based on the latest synchronized content. It also handles administrative commands like pause/resume and outreach campaigns.

## Prerequisites

*   **Google Cloud Platform (GCP) Account:** To enable Google APIs and manage service accounts.
*   **OpenAI API Key:** For generating embeddings and powering the chatbot's LLM.
*   **Render Account:** For deploying the Python Flask backend.
*   **Google Workspace Account:** To create and manage Google Docs and Sheets.
*   **Git:** For version control and deploying to Render.
*   **`gcloud` CLI (Optional):** For managing GCP resources via command line.
*   **Python Environment (Optional, for local testing):** Python 3.9+

## Setup Instructions

### 1. Google Cloud Project Setup

1.  **Create a GCP Project:**
    *   Go to the [Google Cloud Console](https://console.cloud.google.com/).
    *   Create a new project or select an existing one.
2.  **Enable APIs:**
    *   Navigate to "APIs & Services" > "Library".
    *   Search for and enable the following APIs:
        *   Google Docs API
        *   Google Sheets API
        *   Google Drive API (provides `drive.files.get` used for MIME type)
3.  **Billing:** Ensure billing is enabled for your GCP project.

### 2. Service Account Setup

1.  **Navigate to Service Accounts:**
    *   In the GCP Console, go to "IAM & Admin" > "Service Accounts".
2.  **Create Service Account:**
    *   Click "+ CREATE SERVICE ACCOUNT".
    *   Enter a name (e.g., "rag-chatbot-integration") and description.
    *   Click "CREATE AND CONTINUE".
3.  **Grant Permissions (Important for File Access):**
    *   The service account needs permission to read the specific Google Docs and Sheets you intend to use for RAG, and read/write for Outreach Campaigns.
    *   **Enable the APIs** as mentioned above (Docs, Sheets, Drive).
    *   After creating the service account, note its email address.
    *   **Share your Google Docs/Sheets/Folders:**
        *   For RAG documents: Open the specific Google Drive files or folders, click "Share", and add the service account's email address, granting it **"Viewer"** permission.
        *   For Outreach Campaign Sheets: Share the Google Sheets with the service account's email, granting it **"Editor"** permission.
4.  **Create JSON Key:**
    *   Once the service account is created, select the service account.
    *   Go to the "KEYS" tab.
    *   Click "ADD KEY" > "Create new key".
    *   Choose "JSON" as the key type and click "CREATE".
    *   A JSON file will be downloaded. **Keep this file secure.** Its content will be used for `GOOGLE_APPLICATION_CREDENTIALS_JSON` (for RAG) and `GOOGLE_SHEETS_CREDENTIALS` (for Outreach, can be the same key).

### 3. Google Apps Script Setup (for RAG content sync)

#### Common Instructions:

*   Open the Google Doc or Sheet you want to integrate for RAG.
*   Go to "Extensions" > "Apps Script".
*   Delete any existing code in the `Code.gs` file.
*   Copy the entire content of the relevant `.gs` file from this repository (`google_apps_script_sheets.gs` or `google_apps_script_docs.gs`) and paste it into the Apps Script editor.

#### a) Google Sheets Script (`google_apps_script_sheets.gs`)

1.  **Paste Script:** Copy the content from `google_apps_script_sheets.gs` into the Apps Script editor of your Google Sheet.
2.  **Configure:**
    *   Modify the `WEBHOOK_URL` placeholder: Replace `"YOUR_FLASK_WEBHOOK_URL_HERE"` with the URL of your deployed Flask application's sync webhook (e.g., `https://your-app-name.onrender.com/webhook-google-sync`).
    *   Modify the `SECRET_TOKEN` placeholder: Replace `"YOUR_SECRET_TOKEN_HERE"` with a strong, unique secret token. This token must match the `FLASK_SECRET_TOKEN` environment variable in your Flask backend.
3.  **Save Script:** Click the save icon (💾).
4.  **Trigger Setup:**
    *   The `onEdit(e)` function is a simple trigger that should automatically run when any cell in the spreadsheet is edited.
5.  **Authorization:** The first time the script runs, Google will ask for authorization.

#### b) Google Docs Script (`google_apps_script_docs.gs`)

1.  **Paste Script:** Copy the content from `google_apps_script_docs.gs` into the Apps Script editor of your Google Doc.
2.  **Configure:**
    *   Modify `WEBHOOK_URL` and `SECRET_TOKEN` as for the Sheets script.
3.  **Save Script** and **Reload your Google Document** to see the new "Chatbot Sync" menu. Authorize when first using "Sync Now".

### 4. Python Backend Setup (Render)

1.  **Fork & Connect to Render:**
    *   Fork this repository.
    *   On Render Dashboard: "New +" > "Web Service", connect GitHub, select forked repo.
2.  **Render Service Configuration:**
    *   **Name:** e.g., `rag-google-sync-app`.
    *   **Runtime:** Python.
    *   **Build Command:** `pip install -r requirements.txt`.
    *   **Start Command:** `gunicorn script:app --timeout 120 --log-level info`.
3.  **Environment Variables (Essential):**
    *   `PYTHON_VERSION`: e.g., `3.10.13`.
    *   `OPENAI_API_KEY`: Your OpenAI API key.
    *   `FLASK_SECRET_TOKEN`: Matches token in Google Apps Scripts (for RAG sync).
    *   `GOOGLE_APPLICATION_CREDENTIALS_JSON`: Full JSON content of the service account key (for RAG).
    *   **Outreach Campaign Variables (if using feature):**
        *   `GOOGLE_SHEETS_CREDENTIALS`: Full JSON content of the service account key (can be same as above if permissions allow, requires Editor on campaign sheets).
        *   `DEFAULT_OUTREACH_SHEET_ID` (Optional)
        *   `BUSINESS_NAME` (Optional)
        *   `OUTREACH_MESSAGE_DELAY_SECONDS` (Optional)
    *   **WhatsApp Integration (if using):**
        *   `WASENDER_API_TOKEN`, `WASENDER_API_URL`.
    *   *(Other optional variables for email/calendar as needed)*
4.  **Deploy:** Click "Create Web Service". Use the deployed URL for `WEBHOOK_URL` in GAS.

## Usage

*   **RAG Content Sync (Google Sheets/Docs):**
    *   Sheets: Edit cells. Sync is automatic.
    *   Docs: Use "Chatbot Sync" > "Sync Now" menu.
*   **Chatbot:**
    *   Interact for RAG-based answers.
    *   Use Pause/Resume commands for control.
    *   Use Outreach commands (e.g., `bot start outreach <sheet_id>`) to initiate campaigns.

## Document Parsing Strategy (for RAG)

*   **Google Docs (`get_google_doc_content`):** Extracts text from paragraphs. Complex structures (tables, images) are not parsed.
*   **Google Sheets (`get_google_sheet_content`):** Concatenates text from all cells, tab-separated within rows, newline-separated between rows. Each sheet's content is prefixed with `Sheet: {sheet_title}`.

## Troubleshooting

*   **Google Apps Script Issues:** Use "Executions" logs in Apps Script editor. Check permissions, `WEBHOOK_URL`, `SECRET_TOKEN`.
*   **Flask Backend / Render Issues:** Check Render "Logs". Verify environment variables, especially credentials and tokens. Ensure files are shared correctly with the service account.
*   **Content Not Updating in Chatbot (RAG):** Trace from GAS logs to Render logs to identify failures in sync, fetch, or processing steps.
*   **Pause/Resume/Outreach Commands Not Working:** Check command syntax. Review Flask logs for command processing details. Remember pause states are in-memory. For outreach, ensure Sheet ID is correct and sheet structure/permissions are valid.

## Security Best Practices

*   **Secret Management:** Keep tokens, API keys, and service account JSON content confidential. Use environment variables.
*   **Least Privilege:** Grant "Viewer" for RAG-source documents and "Editor" only for outreach campaign sheets to the service account.
*   **Webhook Security:** The secret token is a basic auth layer.
*   **Access Control for Commands:** Be aware of current open access for bot commands. Implement user-based authorization if needed.
*   **WhatsApp Policies:** Adhere strictly to WhatsApp policies, especially regarding user consent for outbound messages.

## Contributing

Contributions are welcome! Please fork the repository, make your changes, and submit a pull request.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details (if one is added).
