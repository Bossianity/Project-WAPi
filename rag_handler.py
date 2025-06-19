import os
import logging
import json
import shutil
import time
from langchain_openai import OpenAIEmbeddings # MODIFIED
from langchain_community.vectorstores import FAISS
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader
from langchain_community.document_loaders import TextLoader
from langchain_core.documents import Document

# --- Global Constants ---
VECTOR_STORE_PATH = "faiss_index"
# EMBEDDING_MODEL_NAME = "models/embedding-001" # REMOVED
PROCESSED_FILES_LOG_PATH = os.path.join(VECTOR_STORE_PATH, "processed_files.log")


# Set up basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def initialize_vector_store():
    """
    Initializes or loads a FAISS vector store with a retry mechanism
    to handle potential startup timeouts.
    """
    openai_api_key_local = os.getenv('OPENAI_API_KEY') # MODIFIED
    if not openai_api_key_local:
        logging.error("OPENAI_API_KEY environment variable not set. Cannot initialize vector store.") # MODIFIED
        return None

    embeddings_object = None
    max_retries = 3
    retry_delay = 1  # Initial delay in seconds

    for attempt in range(max_retries):
        try:
            embeddings_object = OpenAIEmbeddings(model="text-embedding-3-large", openai_api_key=openai_api_key_local) # MODIFIED
            logging.info("OpenAIEmbeddings initialized successfully.") # MODIFIED
            break  # Exit loop if successful
        except Exception as e: # MODIFIED (removed specific Google API error)
            logging.error(f"Attempt {attempt + 1}/{max_retries} encountered an unexpected error during OpenAIEmbeddings initialization: {e}", exc_info=True) # MODIFIED
            time.sleep(retry_delay)
            retry_delay *= 2 # Exponential backoff

    if embeddings_object is None:
        logging.critical("Failed to initialize OpenAIEmbeddings after %s retries. RAG functionality will be unavailable.", max_retries) # MODIFIED
        return None

    # Logic for forcing a re-index remains the same
    force_reindex = os.getenv('FORCE_REINDEX', 'false').lower() == 'true'
    if force_reindex and os.path.exists(VECTOR_STORE_PATH):
        logging.info(f"FORCE_REINDEX is true. Removing existing vector store at {VECTOR_STORE_PATH}.")
        try:
            shutil.rmtree(VECTOR_STORE_PATH)
        except Exception as e:
            logging.error(f"Error removing directory {VECTOR_STORE_PATH}: {e}", exc_info=True)

    if not os.path.exists(VECTOR_STORE_PATH):
        os.makedirs(VECTOR_STORE_PATH, exist_ok=True)

    # Attempt to load an existing index first
    if os.path.exists(os.path.join(VECTOR_STORE_PATH, "index.faiss")):
        try:
            logging.info(f"Attempting to load existing FAISS index from {VECTOR_STORE_PATH}")
            faiss_store = FAISS.load_local(VECTOR_STORE_PATH, embeddings_object, allow_dangerous_deserialization=True)
            logging.info("FAISS index loaded successfully.")
            return faiss_store
        except Exception as e:
            logging.error(f"Failed to load existing FAISS index: {e}. Will attempt to create a new one.", exc_info=True)

    # --- NEW: Create a new index with a retry loop ---
    logging.info("No existing index found. Creating new FAISS index...")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # This is the line that was failing.
            faiss_store = FAISS.from_texts(["init"], embeddings_object)
            faiss_store.save_local(VECTOR_STORE_PATH)
            logging.info("New FAISS index created and saved successfully.")
            return faiss_store
        except Exception as e: # MODIFIED (removed specific Google API error)
            # Catch other potential errors during creation
            logging.error(f"An unexpected error occurred on attempt {attempt + 1}/{max_retries} while creating index: {e}", exc_info=True)
            time.sleep(2 ** attempt)

    # If all retries fail
    logging.critical("All attempts to create a new FAISS index failed. RAG system will be unavailable.")
    return None


# --- Processed Files Log Management ---
def get_processed_files_log() -> dict:
    """
    Loads the processed files log.
    Returns a dictionary of processed files and their metadata.
    """
    if not os.path.exists(PROCESSED_FILES_LOG_PATH):
        logging.info(f"Processed files log not found at {PROCESSED_FILES_LOG_PATH}. Returning empty log.")
        return {}
    try:
        with open(PROCESSED_FILES_LOG_PATH, 'r') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        logging.error(f"Error reading or parsing processed files log at {PROCESSED_FILES_LOG_PATH}: {e}", exc_info=True)
        return {}

def update_processed_files_log(processed_files: dict):
    """
    Writes the processed_files dictionary to the log file.
    """
    try:
        os.makedirs(os.path.dirname(PROCESSED_FILES_LOG_PATH), exist_ok=True)
        with open(PROCESSED_FILES_LOG_PATH, 'w') as f:
            json.dump(processed_files, f, indent=4)
    except IOError as e:
        logging.error(f"Error writing processed files log to {PROCESSED_FILES_LOG_PATH}: {e}", exc_info=True)

# --- Document Processing ---
def process_document(file_path: str, vector_store: FAISS, embeddings: OpenAIEmbeddings): # MODIFIED
    """
    Processes a single document (PDF or TXT), splits it into chunks,
    and adds the chunks to the vector store.
    """
    if not vector_store or not embeddings:
        logging.error("process_document: Vector store or embeddings object not provided.")
        return False

    try:
        file_extension = os.path.splitext(file_path)[1].lower()
        if file_extension == '.pdf':
            loader = PyPDFLoader(file_path)
        elif file_extension == '.txt':
            loader = TextLoader(file_path)
        else:
            logging.warning(f"process_document: Unsupported file type '{file_extension}' for file '{file_path}'.")
            return False
    except Exception as e:
        logging.error(f"process_document: Error determining file type for '{file_path}': {e}", exc_info=True)
        return False

    try:
        logging.info(f"process_document: Loading document: {file_path}")
        documents = loader.load()
        if not documents:
            logging.warning(f"process_document: No content found in document: {file_path}")
            return False
    except Exception as e:
        logging.error(f"process_document: Error loading document '{file_path}': {e}", exc_info=True)
        return False

    try:
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        texts = text_splitter.split_documents(documents)
        if not texts:
            logging.warning(f"process_document: Document splitting resulted in no chunks for: {file_path}")
            return False
    except Exception as e:
        logging.error(f"process_document: Error splitting document '{file_path}': {e}", exc_info=True)
        return False

    try:
        logging.info(f"process_document: Adding {len(texts)} chunks from {file_path} to the vector store.")
        vector_store.add_documents(texts)
        vector_store.save_local(VECTOR_STORE_PATH)
        logging.info(f"process_document: Successfully processed '{file_path}' and saved index.")

        try:
            file_mtime = os.path.getmtime(file_path)
            processed_logs = get_processed_files_log()
            processed_logs[file_path] = {'mtime': file_mtime, 'status': 'processed'}
            update_processed_files_log(processed_logs)
        except Exception as e:
            logging.error(f"process_document: Failed to update processed files log for '{file_path}': {e}", exc_info=True)
        return True
    except Exception as e:
        logging.error(f"process_document: Error adding documents from '{file_path}' to vector store: {e}", exc_info=True)
        return False

def remove_document_from_store(file_path: str, vector_store: FAISS) -> bool:
    """
    Logs the removal of a document. Actual vector removal from FAISS is not implemented here.
    """
    logging.info(f"remove_document_from_store: File '{file_path}' detected as removed.")
    logging.warning("Automated removal from FAISS is not implemented. Re-index for complete removal.")

    try:
        processed_logs = get_processed_files_log()
        if file_path in processed_logs:
            processed_logs[file_path]['status'] = 'removed_from_source'
            update_processed_files_log(processed_logs)
            logging.info(f"Updated status of '{file_path}' to 'removed_from_source' in log.")
        return True
    except Exception as e:
        logging.error(f"remove_document_from_store: Error updating log for '{file_path}': {e}", exc_info=True)
        return False

# --- Google Drive Document Processing ---
def delete_document_from_vector_store(document_id: str, vector_store: FAISS) -> bool:
    """
    Deletes all vectors associated with a given document_id from the FAISS vector store.
    """
    logging.info(f"Attempting to delete document with ID '{document_id}' from vector store.")
    if not all([vector_store, vector_store.index, vector_store.docstore, hasattr(vector_store.docstore, '_dict')]):
        logging.warning("delete_document_from_vector_store: Vector store is not fully initialized. Nothing to delete.")
        return False

    try:
        ids_to_remove = [
            doc_uuid for doc_uuid, doc in vector_store.docstore._dict.items()
            if doc.metadata.get('source') == document_id
        ]
        if not ids_to_remove:
            logging.info(f"No document chunks found with source ID '{document_id}'. Nothing to delete.")
            return False

        vector_store.delete(ids_to_remove)
        logging.info(f"Successfully deleted {len(ids_to_remove)} chunks for document ID '{document_id}'.")
        return True
    except Exception as e:
        logging.error(f"Error during deletion of document ID '{document_id}': {e}", exc_info=True)
        return False

def process_google_document_text(document_id: str, text_content: str, vector_store: FAISS, embeddings: OpenAIEmbeddings) -> bool: # MODIFIED
    """
    Processes text from a Google Document, deletes old entries, and adds new ones.
    """
    logging.info(f"Processing Google document ID '{document_id}'.")
    if not vector_store or not embeddings:
        logging.error("process_google_document_text: Vector store or embeddings not provided.")
        return False

    try:
        # Step 1: Delete existing entries for this document
        delete_document_from_vector_store(document_id, vector_store)

        # If new content is empty, we are done after deletion.
        if not text_content or not text_content.strip():
            logging.warning(f"Text content for document ID '{document_id}' is empty. Ensured no entries exist.")
            vector_store.save_local(VECTOR_STORE_PATH)
            return True

        # Step 2: Split new text and create Document objects
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
        chunks = text_splitter.split_text(text_content)
        if not chunks:
            logging.warning(f"No text chunks generated for document ID '{document_id}'.")
            vector_store.save_local(VECTOR_STORE_PATH) # Save after potential deletion
            return True # Not an error, just nothing to add

        docs = [Document(page_content=chunk, metadata={'source': document_id}) for chunk in chunks]

        # Step 3: Add new documents and save
        vector_store.add_documents(docs)
        vector_store.save_local(VECTOR_STORE_PATH)
        logging.info(f"Successfully added {len(docs)} new chunks for document ID '{document_id}' and saved index.")
        return True

    except Exception as e:
        logging.error(f"Error processing Google document text for ID '{document_id}': {e}", exc_info=True)
        return False

# --- Querying ---
def query_vector_store(query_text: str, vector_store: FAISS, k: int = 4):
    """
    Queries the vector store for similar documents.
    """
    if not vector_store:
        logging.warning("query_vector_store: Vector store not initialized.")
        return []

    if not hasattr(vector_store, 'index_to_docstore_id') or len(vector_store.index_to_docstore_id) <= 1:
        logging.warning("query_vector_store: Vector store may be empty or contain only the 'init' document.")
        # Allow query to proceed, may return the 'init' doc.

    try:
        logging.info(f"Performing similarity search for query: '{query_text}' with k={k}")
        results = vector_store.similarity_search(query_text, k=k)
        logging.info(f"Found {len(results)} results.")
        return results
    except Exception as e:
        logging.error(f"Error during similarity search: {e}", exc_info=True)
        return []

# --- Main Test Block ---
if __name__ == '__main__':
    logging.info("Starting RAG Handler test sequence...")

    openai_api_key_main_test = os.getenv('OPENAI_API_KEY') # MODIFIED
    if not openai_api_key_main_test:
        print("Please set the OPENAI_API_KEY environment variable to run tests.") # MODIFIED
        logging.warning("OPENAI_API_KEY not set, RAG tests will be skipped.") # MODIFIED
        exit()

    vs = initialize_vector_store()
    if not vs:
        logging.error("Failed to initialize vector store. Aborting tests.")
        exit()

    logging.info("Vector store initialized successfully.")

    try:
        current_embeddings_for_test = OpenAIEmbeddings(model="text-embedding-3-large", openai_api_key=openai_api_key_main_test) # MODIFIED
    except Exception as e:
        logging.error(f"Test block: Failed to create embeddings for testing: {e}")
        current_embeddings_for_test = None

    if not current_embeddings_for_test:
        logging.error("Could not create embeddings object. Aborting document processing tests.")
        exit()

    # Create a dummy text file for testing
    sample_txt_path = "sample_document.txt"
    with open(sample_txt_path, "w") as f:
        f.write("This is a sample document for testing the RAG system with OpenAI embeddings. ") # MODIFIED
        f.write("Langchain provides powerful tools for building AI applications. ")
        f.write("OpenAI's models offer state-of-the-art performance.") # MODIFIED

    # Process the dummy file
    logging.info(f"Attempting to process {sample_txt_path}")
    process_success_txt = process_document(sample_txt_path, vs, current_embeddings_for_test)

    if process_success_txt:
        logging.info(f"Successfully processed {sample_txt_path}")

        # Query the vector store
        logging.info("Querying for 'OpenAI embedding performance'") # MODIFIED
        query_results = query_vector_store("OpenAI embedding performance", vs) # MODIFIED
        if query_results:
            for i, doc in enumerate(query_results):
                # Filter out the 'init' document from results
                if "init" not in doc.page_content:
                    logging.info(f"Query Result {i+1}: {doc.page_content[:100]}... (Source: {doc.metadata.get('source')})")
        else:
            logging.info("No relevant results found for 'OpenAI embedding performance'.") # MODIFIED
    else:
        logging.warning(f"Failed to process {sample_txt_path}, skipping query tests.")

    # Clean up dummy file
    if os.path.exists(sample_txt_path):
        os.remove(sample_txt_path)
        logging.info(f"Cleaned up {sample_txt_path}")

    logging.info("RAG Handler test sequence finished.")
