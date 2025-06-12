import base64
import requests
import logging
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend

def get_decryption_keys(media_key_b64, media_type):
    media_key = base64.b64decode(media_key_b64)
    info_map = {
        'image': 'WhatsApp Image Keys',
        'video': 'WhatsApp Video Keys',
        'audio': 'WhatsApp Audio Keys',
        'document': 'WhatsApp Document Keys',
    }

    info = info_map.get(media_type)
    if not info:
        # This log was added in a previous step, ensure it's here or similar
        logging.error(f"Unsupported media type for HKDF info: {media_type}")
        raise ValueError(f"Invalid media type: {media_type}")

    hkdf_output_length = 112 # Explicitly define the expected output length

    # Logging added for diagnostics (as per original plan for this step)
    logging.info(f"Original media_key_b64 length: {len(media_key_b64)}")
    logging.info(f"Decoded media_key length (input to HKDF): {len(media_key)}")
    logging.info(f"Requesting HKDF output length: {hkdf_output_length}")

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=hkdf_output_length, # Use the variable
        salt=None,
        info=info.encode('utf-8'),
        backend=default_backend()
    )

    derived_key = hkdf.derive(media_key)
    logging.info(f"Actual derived_key length (output from HKDF): {len(derived_key)}")
    return derived_key

def decrypt_media(encrypted_data, media_key_b64, media_type):
    keys = get_decryption_keys(media_key_b64, media_type)
    # Logging added for diagnostics (as per original plan for this step)
    logging.info(f"Length of keys received from get_decryption_keys: {len(keys)}")

    # Robust key length check (as per original plan for this step)
    if len(keys) < 48:
        error_msg = f"Derived key material (length {len(keys)}) is too short. Need at least 48 bytes for IV (16) and cipher key (32)."
        logging.error(error_msg)
        raise ValueError(error_msg)

    iv = keys[:16]
    cipher_key = keys[16:48] # 32 bytes for AES-256
    # mac_key = keys[48:80] # Not directly used in openssl_decrypt analog, but part of derived keys
    # ref_key = keys[80:]   # Not directly used

    # Ensure cipher_key is 32 bytes for AES-256 (redundant if len(keys) >= 48, but good for sanity)
    if len(cipher_key) != 32:
        error_msg = f"Extracted cipher_key is not 32 bytes long (actual: {len(cipher_key)}). This should not happen if derived keys are >= 48 bytes."
        logging.error(error_msg)
        raise ValueError(error_msg)

    ciphertext = encrypted_data[:-10] # Remove 10-byte MAC tail

    cipher = Cipher(algorithms.AES(cipher_key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()

    decrypted_data = decryptor.update(ciphertext) + decryptor.finalize()
    return decrypted_data

def download_and_decrypt_media(media_url, media_key_b64, media_type):
    try:
        response = requests.get(media_url, timeout=20)
        response.raise_for_status()
        encrypted_data = response.content
        logging.info(f"Successfully downloaded {len(encrypted_data)} bytes of encrypted media.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to download media file: {e}")
        return None

    try:
        decrypted_data = decrypt_media(encrypted_data, media_key_b64, media_type)
        logging.info(f"Successfully decrypted media. Resulting size: {len(decrypted_data)} bytes.")
        return decrypted_data
    except Exception as e:
        logging.error(f"Failed to decrypt media: {e}", exc_info=True) # exc_info=True is important
        return None
