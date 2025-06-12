import os
from datetime import datetime, timedelta
import dateparser
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pytz # Added for timezone definitions

# --- Timezone Configuration (to align with main script's workaround) ---
# Timezone for user interaction and display (if this script were to do that)
# TARGET_DISPLAY_TIMEZONE = pytz.timezone('Asia/Dubai') # Not directly used in this version of the handler for event creation
# Timezone for storing events in Google Calendar (workaround)
EVENT_STORAGE_TIMEZONE = pytz.timezone('America/New_York')
# --- End Timezone Configuration ---

# Load credentials path from environment variables
CREDENTIALS_PATH = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
# Using 'primary' calendar for consistency with the main script's approach.
# If a specific CALENDAR_ID is needed, set it via os.getenv('GOOGLE_CALENDAR_ID')
# and ensure it's the correct target.
CALENDAR_ID_TO_USE = 'primary' 
# CALENDAR_ID_TO_USE = os.getenv('GOOGLE_CALENDAR_ID', 'primary') # Alternative if env var is preferred

def get_calendar_service():
    """Initialize and return the Google Calendar API service."""
    try:
        if not CREDENTIALS_PATH:
            print("Error: GOOGLE_APPLICATION_CREDENTIALS environment variable not set.")
            return None
        credentials = service_account.Credentials.from_service_account_file(
            CREDENTIALS_PATH,
            scopes=['https://www.googleapis.com/auth/calendar']
        )
        service = build('calendar', 'v3', credentials=credentials)
        return service
    except Exception as e:
        print(f"Error initializing calendar service: {e}")
        return None

def create_appointment(summary, start_time_str_event_tz, user_phone, duration_minutes=60):
    """
    Create an appointment in Google Calendar, assuming start_time_str_event_tz
    is an ISO string for the EVENT_STORAGE_TIMEZONE (e.g., America/New_York).
    
    Args:
        summary (str): The title/description of the appointment.
        start_time_str_event_tz (str): Start time in ISO format, corresponding to EVENT_STORAGE_TIMEZONE.
                                      Example: "2025-05-30T10:00:00-04:00" for 10 AM New York EDT.
        user_phone (str): User's phone number for reference.
        duration_minutes (int): Duration of the appointment in minutes.
        
    Returns:
        str: HTML link to the created event if successful, None otherwise.
    """
    try:
        service = get_calendar_service()
        if not service:
            return None

        # Parse the start time. datetime.fromisoformat will correctly handle
        # ISO strings with timezone offsets (e.g., "-04:00").
        # The resulting start_time will be timezone-aware.
        start_time_aware = datetime.fromisoformat(start_time_str_event_tz)
        
        # Ensure it's in the EVENT_STORAGE_TIMEZONE if it somehow wasn't, or to be explicit.
        # This step might be redundant if start_time_str_event_tz is guaranteed to be correctly formatted
        # with the EVENT_STORAGE_TIMEZONE's offset.
        start_time_event_tz = start_time_aware.astimezone(EVENT_STORAGE_TIMEZONE)
        
        end_time_event_tz = start_time_event_tz + timedelta(minutes=duration_minutes)

        print(f"Calendar_handler: Creating event with {EVENT_STORAGE_TIMEZONE.zone} times - Start: {start_time_event_tz}, End: {end_time_event_tz}")

        # Create the event
        event_body = {
            'summary': summary,
            'description': f'Appointment for user: {user_phone}. (Handled by calendar_handler.py)',
            'start': {
                'dateTime': start_time_event_tz.isoformat(), # ISO format with correct offset for EVENT_STORAGE_TIMEZONE
                'timeZone': EVENT_STORAGE_TIMEZONE.zone,   # Explicitly 'America/New_York'
            },
            'end': {
                'dateTime': end_time_event_tz.isoformat(),   # ISO format with correct offset for EVENT_STORAGE_TIMEZONE
                'timeZone': EVENT_STORAGE_TIMEZONE.zone,   # Explicitly 'America/New_York'
            },
            # Optional: Add attendees or reminders if needed by this handler
            # 'attendees': [{'email': 'some_attendee@example.com'}],
        }

        print(f"Calendar_handler: Creating calendar event with payload: {json.dumps(event_body, indent=2)}")
        # Insert the event
        created_event = service.events().insert(calendarId=CALENDAR_ID_TO_USE, body=event_body).execute()
        
        print(f"Calendar_handler: Event created. Link: {created_event.get('htmlLink')}, ID: {created_event.get('id')}")
        # === Optional Diagnostic: Fetch event by ID (similar to main script) ===
        # if created_event and created_event.get('id'):
        #     try:
        #         retrieved_event = service.events().get(calendarId=CALENDAR_ID_TO_USE, eventId=created_event.get('id')).execute()
        #         print(f"Calendar_handler DIAGNOSTIC: Successfully retrieved event by ID. Summary: {retrieved_event.get('summary')}")
        #     except Exception as e_get_diag:
        #         print(f"Calendar_handler DIAGNOSTIC ERROR: Failed to retrieve event by ID. Error: {e_get_diag}")
        # === End Optional Diagnostic ===
        
        return created_event.get('htmlLink')

    except Exception as e:
        print(f"Error creating appointment in calendar_handler: {e}")
        return None

def parse_human_datetime(text):
    """
    Parse a human-readable datetime string into a Python datetime object,
    localized to Asia/Dubai.
    
    Args:
        text (str): Human-readable datetime string (e.g., "tomorrow at 4pm")
        
    Returns:
        datetime: Parsed datetime object (Asia/Dubai aware), or None if parsing fails.
    """
    try:
        # Configure dateparser to prefer dates in the future if ambiguous
        settings = {'PREFER_DATES_FROM': 'future'}
        parsed_date = dateparser.parse(text, settings=settings)
        
        if parsed_date:
            dubai_tz = pytz.timezone('Asia/Dubai')
            # If parsed_date is naive, localize to Dubai.
            # If it's already aware (dateparser might sometimes infer a local system timezone),
            # convert it to Dubai.
            if parsed_date.tzinfo is None or parsed_date.tzinfo.utcoffset(parsed_date) is None:
                parsed_date = dubai_tz.localize(parsed_date)
            else:
                parsed_date = parsed_date.astimezone(dubai_tz)
            return parsed_date
        return None
    except Exception as e:
        print(f"Error parsing datetime in calendar_handler: {e}")
        return None

# Example Usage (for testing this handler independently):
if __name__ == '__main__':
    print("Testing calendar_handler.py...")
    
    # This test assumes GOOGLE_APPLICATION_CREDENTIALS is set.
    
    # 1. Test parse_human_datetime
    raw_time_text = "tomorrow 3pm"
    dubai_aware_dt = parse_human_datetime(raw_time_text)
    
    if dubai_aware_dt:
        print(f"Parsed '{raw_time_text}' to Dubai time: {dubai_aware_dt.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        
        # 2. Convert this Dubai time to New York time for event creation
        new_york_aware_dt = dubai_aware_dt.astimezone(EVENT_STORAGE_TIMEZONE)
        print(f"Converted to New York time for storage: {new_york_aware_dt.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        
        # 3. Format as ISO string for create_appointment function
        start_iso_for_handler = new_york_aware_dt.isoformat()
        
        # 4. Test create_appointment
        print(f"\nAttempting to create test appointment using New York time via calendar_handler...")
        event_summary = "Test Appointment via Handler (Stored NY)"
        user_phone_example = "1234567890"
        
        # Ensure GOOGLE_APPLICATION_CREDENTIALS is set in your environment to run this test
        if os.getenv('GOOGLE_APPLICATION_CREDENTIALS'):
            html_link = create_appointment(event_summary, start_iso_for_handler, user_phone_example, duration_minutes=45)
            if html_link:
                print(f"Successfully created test appointment. Link: {html_link}")
            else:
                print("Failed to create test appointment via handler.")
        else:
            print("Skipping create_appointment test as GOOGLE_APPLICATION_CREDENTIALS is not set.")
            
    else:
        print(f"Could not parse '{raw_time_text}'")

