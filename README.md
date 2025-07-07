# Dynamic Google Docs/Sheets Integration for RAG Chatbot & Interactive WhatsApp Assistant

## Overview

This project enables a Retrieval Augmented Generation (RAG) chatbot to dynamically use content from Google Documents and Google Sheets as its knowledge base. It also powers an interactive WhatsApp assistant for common user queries, particularly for property rentals, leveraging structured data from a dedicated Google Sheet (`Sheet2`). Updates in specified Google Docs or Sheets (for RAG) trigger a webhook, causing the backend to fetch the latest content, process it, and update the RAG system's vector store (FAISS). This ensures the chatbot's RAG responses are based on the most current information. The interactive flows for property services directly query `Sheet2` in real-time.

The system is designed for deployment on Render, utilizing Google Apps Script for RAG content sync event detection, a Flask backend for webhook handling and RAG pipeline management, and Whapi.Cloud for WhatsApp message integration.

## Features

*   **Dynamic RAG Knowledge Base:** Automatically updates the RAG chatbot's knowledge from Google Docs and the primary Google Sheet.
*   **Google Docs Integration (RAG):** Manually trigger updates from Google Docs via a custom menu for RAG.
*   **Google Sheets Integration (RAG & Interactive):**
    *   RAG: Automatically trigger updates when the main Google Sheet (for general knowledge) is edited.
    *   Interactive Flows: Directly fetches data from `Sheet2` for property listings, details, images, and captions.
*   **Interactive WhatsApp Flows:** Guides users through predefined conversation flows using interactive buttons and lists for tasks like property inquiries and service selection.
*   **Dynamic Image Captions:** Displays custom image captions sourced directly from `Sheet2` (`ImageCaption1` - `ImageCaption10` columns) alongside property images in the interactive flow.
*   **Whapi.Cloud Integration:** Uses Whapi.Cloud for sending and receiving WhatsApp messages, including text, images, and interactive messages (buttons, lists).
*   **Secure Webhook (RAG Sync):** Uses a secret token to authenticate webhook calls from Google Apps Script for RAG updates.
*   **Asynchronous Processing (RAG Sync):** RAG content fetching and processing are handled in the background.
*   **FAISS Vector Store (RAG):** Utilizes FAISS for efficient similarity searches in the RAG pipeline.
*   **Render Deployment:** Optimized for deployment on the Render platform.
*   **OpenAI Integration:** Uses OpenAI for embeddings (RAG) and chat completions (RAG and some internal processing).
*   **Pause/Resume Functionality:** Supports global and conversation-specific pause and resume of bot responses via chat commands.
*   **Outbound WhatsApp Campaigns:** Allows agents to initiate personalized outbound messaging campaigns using a separate Google Sheet.

## Interactive WhatsApp Flows

The bot now features rich interactive message flows to guide users, primarily for property-related services. This enhances user experience by providing clear options and reducing the need for free-text input for common queries. These flows are mainly powered by data from a sheet named `Sheet2` in the Google Sheet specified by `PROPERTY_SHEET_ID`.

### Main Greeting and Initial Options

Upon first interaction or a greeting (e.g., "hello", "مرحبا"), the user is presented with:
*   A welcome message from "Mosaed (مساعد)".
*   Interactive buttons for primary actions (translatable to Arabic/English based on detected user language):
    *   "I own an apartment and want to operate it" (أملك شقة حابي أشغلها)
    *   "I want to rent an apartment" (ابي أستاجر شقة)
    *   "Other inquiries" (أستفسارات أخرى)

### 1. Flow: "I own an apartment and want to operate it"

*   **Furnished/Unfurnished Query:** If selected, the bot asks if the user's apartment is furnished via buttons.
    *   "Yes, furnished" (نعم مؤثثة) -> Provides a button linking to a Typeform survey for furnished apartments.
    *   "No, unfurnished" (لا غير مؤثثة) -> Provides a button linking to a Typeform survey for unfurnished apartments (and may include information about furnishing services).

### 2. Flow: "I want to rent an apartment"

*   **City Selection:** The bot prompts the user to select a city from an interactive list of Saudi Arabian cities (e.g., Riyadh, Jeddah, Dammam).
*   **Property Listing:**
    *   Upon city selection, the bot fetches relevant properties from `Sheet2` by filtering the 'City' column.
    *   Each matching property is displayed as an interactive button message card, showing:
        *   Property Name (Header) - from `PropertyName` column in `Sheet2`.
        *   Description (Body) - from `Description` column in `Sheet2`.
        *   Interactive buttons for:
            *   "Show Photos" (عرض الصور)
            *   "Prices" (الأسعار)
            *   "Book" (إحجز)
*   **Property Actions (triggered by buttons or text keywords):**
    *   **Show Photos:** Displays up to 10 images for the selected property using URLs from `ImageURL1` to `ImageURL10` columns in `Sheet2`. Captions for these images are sourced from corresponding `ImageCaption1` through `ImageCaption10` columns. If a specific caption is missing or empty, a default one like "PropertyName - Image X" is used.
    *   **Prices:** Shows weekday, weekend, and monthly prices from `WeekdayPrice`, `WeekendPrice`, `MonthlyPrice` columns in `Sheet2`.
    *   **Book:** Provides a booking link from the `BookingLink` column in `Sheet2` if available, or a message indicating the team will follow up.
    *   Users can also type keywords (e.g., "photos", "price", "book" in English or Arabic) after a property card is shown to trigger these actions for the last viewed property.

### 3. Flow: "Other inquiries"

*   The bot prompts the user to type their question. This query is then handled by the RAG (Retrieval Augmented Generation) system, using the knowledge base from synced Google Docs/Sheets (typically the first sheet or other specifically named sheets for general RAG, not `Sheet2` unless configured for it) and the LLM for a response.

## Google Sheet Structure for Interactive Property Listings (`Sheet2`)

For the "I want to rent an apartment" flow, the bot relies on a specific sheet, expected to be named `Sheet2`, within the Google Spreadsheet defined by the `PROPERTY_SHEET_ID` environment variable.

**Key Columns for `Sheet2`:**

*   `PropertyID` (Text): Unique identifier for the property.
*   `PropertyName` (Text): Name of the property (e.g., "Cozy Studio in Downtown"). Used as header in property cards.
*   `Description` (Text): Short description of the property. Used as body in property cards.
*   `WeekdayPrice` (Number): Price per night on weekdays.
*   `WeekendPrice` (Number): Price per night on weekends.
*   `MonthlyPrice` (Number): Price for a monthly rental.
*   `Guests` (Number): Maximum number of guests allowed.
*   `City` (Text): City where the property is located (e.g., "Riyadh", "Jeddah"). Used for filtering.
*   `Neighborhood` (Text, Optional): Specific area or neighborhood.
*   `Amenities` (Text, Optional): Comma-separated list of amenities.
*   `BookingLink` (URL, Optional): Direct link for booking the property.
*   `VideoURL` (URL, Optional): Link to a video tour of the property.
*   `ImageURL1`, `ImageURL2`, ..., `ImageURL10` (URL, Optional): URLs for property images.
*   `ImageCaption1`, `ImageCaption2`, ..., `ImageCaption10` (Text, Optional): Captions for the corresponding images. If blank, a default caption is generated.

## Pause/Resume Functionality

This feature allows for administrative control over the bot's responsiveness directly through WhatsApp messages. The bot supports both a global pause (affecting all users) and the ability to pause/resume interactions with specific user IDs.

*(Rest of Pause/Resume, Outbound Campaigns, Architecture, Prerequisites, Setup, Usage, Document Parsing, Troubleshooting, Security, Contributing, License sections remain largely the same as before, with minor clarifications where necessary regarding different sheet usages and new integrations.)*

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
*   The command keywords (e.g., "bot pause all") are **case-insensitive**.
*   The `<target_user_id>` is treated as **case-sensitive** by the system.

### Access Control & State Persistence

Refer to previous detailed sections; these aspects remain unchanged.

## Outbound WhatsApp Campaigns

This feature, using a separate Google Sheet for contact lists, remains unchanged. Refer to previous detailed sections.

## Architecture

The data flow is as follows:

1.  **User Interaction (WhatsApp):**
    *   User sends a message to the bot's WhatsApp number.
    *   Whapi.Cloud forwards the message to the Flask backend's `/hook` endpoint.
2.  **Flask Backend (`/hook`):**
    *   Receives message, determines language, checks for interactive flow states.
    *   **Interactive Flows:** If the user is in an interactive flow (e.g., property rental), it processes button/list replies or text input based on `Sheet2` data (via `property_handler.py -> get_sheet2_data()`).
    *   **RAG/LLM Queries:** If not an interactive flow action, or if "Other inquiries" is chosen, the query is passed to the RAG system or directly to an LLM.
    *   **Admin Commands:** Handles commands like `bot pause all`.
    *   Sends responses back to the user via Whapi.Cloud (using `whatsapp_utils.py`).
3.  **RAG Content Sync (Google Apps Script & `/webhook-google-sync`):**
    *   **Google Sheets (for RAG):** An `onEdit(e)` trigger in GAS fires when a *RAG-source* sheet is edited.
    *   **Google Docs (for RAG):** An `onOpen()` trigger creates a custom menu. A user action ("Sync Now") initiates the process.
    *   GAS sends a POST request to `/webhook-google-sync` with `documentId` and `secretToken`.
    *   Flask backend authenticates, then (asynchronously):
        *   Fetches Doc/Sheet content using `google_drive_handler.py`.
        *   Processes content and updates FAISS vector store using `rag_handler.py`.
4.  **Chatbot RAG Usage:**
    *   For general queries, the bot uses the FAISS vector store for RAG, providing answers based on the latest synchronized content from designated RAG sources.

## Prerequisites

*   **Google Cloud Platform (GCP) Account:** As before.
*   **OpenAI API Key:** As before.
*   **Render Account:** As before.
*   **Google Workspace Account:** As before.
*   **Whapi.Cloud Account & API Token:** For WhatsApp messaging.
*   **Git, `gcloud` CLI, Python Environment:** As before.

## Setup Instructions

### 1. Google Cloud Project Setup & 2. Service Account Setup

These sections remain the same. Ensure the service account has **Viewer** access to the Google Sheet containing `Sheet2` if `GOOGLE_APPLICATION_CREDENTIALS_JSON` is used by `property_handler.py` (gspread uses `GOOGLE_SHEETS_CREDENTIALS` which needs **Editor** if it's the same service account key and you write back, but for read-only of `Sheet2` via `property_handler.py` and `gspread`, Viewer on the sheet for the service account defined in `GOOGLE_SHEETS_CREDENTIALS` is sufficient).

### 3. Google Apps Script Setup (for RAG content sync)

This section remains the same, applicable for syncing RAG documents. Interactive flow data from `Sheet2` is fetched directly, not via this sync mechanism.

### 4. Python Backend Setup (Render)

1.  **Fork & Connect to Render:** As before.
2.  **Render Service Configuration:** As before.
3.  **Environment Variables (Essential - additions highlighted):**
    *   `PYTHON_VERSION`: e.g., `3.10.13`.
    *   `OPENAI_API_KEY`: Your OpenAI API key.
    *   `FLASK_SECRET_TOKEN`: Matches token in Google Apps Scripts (for RAG sync).
    *   `GOOGLE_APPLICATION_CREDENTIALS_JSON`: Full JSON content of the service account key (for RAG via `google_drive_handler.py`).
    *   `GOOGLE_SHEETS_CREDENTIALS`: Full JSON content of the service account key (for `property_handler.py` and `outreach_handler.py` - needs permissions on relevant sheets).
    *   `PROPERTY_SHEET_ID`: The ID (or URL) of the Google Spreadsheet containing `Sheet2` (for interactive property listings) and potentially other sheets for RAG or outreach.
    *   `PROPERTY_SHEET_NAME`: (Optional, defaults to 'Properties') Name of the default sheet for RAG if using the older `get_sheet_data()` function. `Sheet2` is hardcoded for the interactive flow.
    *   **Whapi.Cloud Variables:**
        *   `API_URL`: Your Whapi.Cloud API URL (e.g., `https://gate.whapi.cloud`).
        *   `API_TOKEN`: Your Whapi.Cloud API token.
    *   `BOT_URL`: The publicly accessible URL of your deployed Render application (e.g., `https://your-app-name.onrender.com/hook`), used by Whapi.Cloud to send messages.
    *   **Outreach Campaign Variables (if using feature):**
        *   `DEFAULT_OUTREACH_SHEET_ID` (Optional)
        *   `BUSINESS_NAME` (Optional)
        *   `OUTREACH_MESSAGE_DELAY_SECONDS` (Optional)
    *   *(Other optional variables for email/calendar as needed)*
4.  **Deploy:** Click "Create Web Service". Use the deployed URL for `WEBHOOK_URL` in GAS (for RAG sync) and configure it in your Whapi.Cloud settings for the `/hook` endpoint.

## Usage

*   **Interactive Chatbot:**
    *   Send a message (e.g., "Hello") to the bot's WhatsApp number.
    *   Follow the interactive buttons/lists for property searches or other services.
    *   Ask general questions if you select "Other inquiries".
*   **RAG Content Sync (Google Sheets/Docs):**
    *   Sheets (for RAG): Edit cells in the RAG-source sheet. Sync is automatic via Apps Script.
    *   Docs (for RAG): Use "Chatbot Sync" > "Sync Now" menu in the Google Doc.
*   **Administrative Commands:**
    *   Use Pause/Resume commands (`bot pause all`, etc.) for control.
    *   Use Outreach commands (e.g., `bot start outreach <sheet_id>`) to initiate campaigns.

## Document Parsing Strategy (for RAG)

*   **Google Docs (`get_google_doc_content`):** Extracts text from paragraphs. Complex structures (tables, images) are not parsed.
*   **Google Sheets (`get_google_sheet_content` for RAG):** Concatenates text from all cells, tab-separated within rows, newline-separated between rows. Each sheet's content is prefixed with `Sheet: {sheet_title}`. This applies to sheets intended for the general RAG knowledge base, not `Sheet2` which is parsed differently by `property_handler.py`.

## Troubleshooting

*   **Google Apps Script Issues:** Use "Executions" logs in Apps Script editor. Check permissions, `WEBHOOK_URL`, `SECRET_TOKEN`.
*   **Flask Backend / Render Issues:** Check Render "Logs". Verify environment variables.
    *   For interactive flows: Ensure `PROPERTY_SHEET_ID` is correct, `Sheet2` exists and has the right columns, and `GOOGLE_SHEETS_CREDENTIALS` are valid with access to this sheet.
    *   For Whapi.Cloud: Check `API_URL`, `API_TOKEN`. Ensure `BOT_URL` is correctly set in Whapi.Cloud dashboard to point to your Render app's `/hook` endpoint.
*   **Content Not Updating in Chatbot (RAG):** Trace from GAS logs to Render logs.
*   **Interactive Messages Not Working:** Check `whatsapp_utils.py` and the Whapi.Cloud documentation for payload structures. Verify `Sheet2` data.
*   **Pause/Resume/Outreach Commands Not Working:** Check command syntax. Review Flask logs.

## Security Best Practices

*   **Secret Management:** Keep tokens, API keys, and service account JSON content confidential.
*   **Least Privilege:** Grant appropriate permissions (Viewer/Editor) to service accounts for Google Sheets.
*   **Webhook Security:** The RAG sync webhook uses a secret token. Whapi.Cloud webhooks should be secured by HTTPS.
*   **Access Control for Commands:** Be aware of current open access for bot commands.
*   **WhatsApp Policies:** Adhere strictly to WhatsApp policies.

## Contributing

Contributions are welcome! Please fork the repository, make your changes, and submit a pull request.

## License

This project is licensed under the MIT License. See the `LICENSE` file for details (if one is added).
