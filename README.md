# RAG Chatbot with Google Sheets Integration

## Overview

This project is a Retrieval Augmented Generation (RAG) chatbot that uses content from a Google Sheet as its knowledge base. The chatbot is built with Python, Flask, and LangChain, and it uses OpenAI for embeddings and chat completions.

## Features

*   **Google Sheets Integration:** The chatbot uses a Google Sheet as its knowledge base.
*   **FAISS Vector Store:** Utilizes FAISS for efficient similarity searches in the RAG pipeline.
*   **OpenAI Integration:** Uses OpenAI for embeddings and chat completions.
*   **Render Deployment:** Optimized for deployment on the Render platform.

## Architecture

The data flow is as follows:

1.  **Data Loading:**
    *   At startup, the application fetches the content of a specified Google Sheet.
2.  **Vector Store Creation:**
    *   The application creates a FAISS vector store from the Google Sheet data.
    *   The vector store is saved to disk for persistence.
3.  **Chatbot Usage:**
    *   The chatbot uses the FAISS vector store for its RAG capabilities, providing answers based on the synchronized content.

## Prerequisites

*   **Google Cloud Platform (GCP) Account:** To enable Google APIs and manage service accounts.
*   **OpenAI API Key:** For generating embeddings and powering the chatbot's LLM.
*   **Render Account:** For deploying the Python Flask backend.
*   **Google Workspace Account:** To create and manage Google Sheets.
*   **Git:** For version control and deploying to Render.

## Setup Instructions

### 1. Google Cloud Project Setup

1.  **Create a GCP Project:**
    *   Go to the [Google Cloud Console](https://console.cloud.google.com/).
    *   Create a new project or select an existing one.
2.  **Enable APIs:**
    *   Navigate to "APIs & Services" > "Library".
    *   Search for and enable the following APIs:
        *   Google Sheets API
        *   Google Drive API
3.  **Billing:** Ensure billing is enabled for your GCP project.

### 2. Service Account Setup

1.  **Navigate to Service Accounts:**
    *   In the GCP Console, go to "IAM & Admin" > "Service Accounts".
2.  **Create Service Account:**
    *   Click "+ CREATE SERVICE ACCOUNT".
    *   Enter a name (e.g., "rag-chatbot-integration") and description.
    *   Click "CREATE AND CONTINUE".
3.  **Grant Permissions:**
    *   Share your Google Sheet with the service account's email address, granting it **"Viewer"** permission.
4.  **Create JSON Key:**
    *   Once the service account is created, select the service account.
    *   Go to the "KEYS" tab.
    *   Click "ADD KEY" > "Create new key".
    *   Choose "JSON" as the key type and click "CREATE".
    *   A JSON file will be downloaded. **Keep this file secure.** Its content will be used for the `GOOGLE_SHEETS_CREDENTIALS` environment variable.

### 3. Python Backend Setup (Render)

1.  **Fork & Connect to Render:**
    *   Fork this repository.
    *   On Render Dashboard: "New +" > "Web Service", connect GitHub, select forked repo.
2.  **Render Service Configuration:**
    *   **Name:** e.g., `rag-google-sync-app`.
    *   **Runtime:** Python.
    *   **Build Command:** `pip install -r requirements.txt`.
    *   **Start Command:** `gunicorn script:app`.
3.  **Environment Variables:**
    *   `PYTHON_VERSION`: e.g., `3.11`.
    *   `OPENAI_API_KEY`: Your OpenAI API key.
    *   `PROPERTY_SHEET_ID`: The ID of your Google Sheet.
    *   `GOOGLE_SHEETS_CREDENTIALS`: The full JSON content of the service account key.
    *   `CONCEPT_SHEET_NAME`: The name of the sheet in your Google Sheet (e.g., "Concepts").

## Troubleshooting

*   **Google Apps Script Issues:** Check the logs in the GCP Console.
*   **Flask Backend / Render Issues:** Check the Render "Logs". Verify environment variables, especially credentials and tokens. Ensure the Google Sheet is shared correctly with the service account.
*   **Content Not Updating in Chatbot (RAG):** Check the logs to identify failures in data loading or vector store creation.
