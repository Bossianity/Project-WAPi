import os
import json
from datetime import datetime, timedelta
import dateparser
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pytz
import logging

# --- Timezone Configuration ---
EVENT_STORAGE_TIMEZONE = pytz.timezone('America/New_York')
DEFAULT_USER_INPUT_TIMEZONE_STR = os.getenv('DEFAULT_USER_TIMEZONE', 'Asia/Dubai')
DEFAULT_USER_INPUT_TIMEZONE = pytz.timezone(DEFAULT_USER_INPUT_TIMEZONE_STR)

# CREDENTIALS_PATH = os.getenv('GOOGLE_APPLICATION_CREDENTIALS') # Keep for clarity but logic below uses it directly

def get_calendar_service():
    """
    Initialize and return the Google Calendar API service.
    Handles GOOGLE_APPLICATION_CREDENTIALS as either a file path or JSON content.
    """
    credentials_env_var = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
    if not credentials_env_var:
        logging.error("Error: GOOGLE_APPLICATION_CREDENTIALS environment variable not set.")
        return None

    credentials = None
    scopes = ['https://www.googleapis.com/auth/calendar']

    try:
        # Attempt to load as a file path first
        if os.path.isfile(credentials_env_var):
            credentials = service_account.Credentials.from_service_account_file(
                credentials_env_var, scopes=scopes
            )
            logging.info("Initialized calendar service credentials from file path.")
        else:
            # If not a file, attempt to load as JSON content
            logging.info("GOOGLE_APPLICATION_CREDENTIALS is not a file path. Attempting to parse as JSON content.")
            try:
                credentials_info = json.loads(credentials_env_var)
                credentials = service_account.Credentials.from_service_account_info(
                    credentials_info, scopes=scopes
                )
                logging.info("Initialized calendar service credentials from JSON content in environment variable.")
            except json.JSONDecodeError as json_err:
                logging.error(f"Failed to parse GOOGLE_APPLICATION_CREDENTIALS as JSON: {json_err}. Ensure it's either a valid file path or JSON string.")
                return None
            except Exception as info_err: # Catch other potential errors from from_service_account_info
                logging.error(f"Error loading credentials from_service_account_info: {info_err}", exc_info=True)
                return None

        if not credentials:
             logging.error("Failed to load credentials using any method.")
             return None

        service = build('calendar', 'v3', credentials=credentials)
        logging.info("Google Calendar service initialized successfully.")
        return service

    except FileNotFoundError:
        # This specific exception might be caught if it was treated as a path initially but then failed json.loads too.
        # The more specific logging above should cover it.
        logging.error(f"FileNotFoundError: GOOGLE_APPLICATION_CREDENTIALS path '{credentials_env_var}' not found, and it's not valid JSON content either.")
        return None
    except Exception as e:
        logging.error(f"Unexpected error initializing calendar service: {e}", exc_info=True)
        return None

def find_or_create_calendar_by_property_id(service, property_id):
    """
    Finds a calendar by property_id (matching summary). If not found, creates one.
    Returns the calendar ID.
    """
    if not service:
        logging.error("Calendar service not available for find_or_create_calendar.")
        return None
    try:
        calendar_list_page_token = None
        while True:
            calendar_list = service.calendarList().list(pageToken=calendar_list_page_token).execute()
            for calendar_list_entry in calendar_list['items']:
                if calendar_list_entry['summary'] == property_id:
                    logging.info(f"Found existing calendar for property ID '{property_id}': {calendar_list_entry['id']}")
                    return calendar_list_entry['id']
            calendar_list_page_token = calendar_list.get('nextPageToken')
            if not calendar_list_page_token:
                break

        logging.info(f"No calendar found for property ID '{property_id}'. Creating new one...")
        calendar_body = {
            'summary': property_id,
            'timeZone': EVENT_STORAGE_TIMEZONE.zone
        }
        created_calendar = service.calendars().insert(body=calendar_body).execute()
        new_calendar_id = created_calendar['id']
        logging.info(f"Created new calendar for property ID '{property_id}': {new_calendar_id}")

        # Share the newly created calendar with the target email
        share_email = os.getenv('OWNER_CALENDAR_EMAIL_SHARE_TARGET')
        if share_email:
            rule = {
                'scope': {
                    'type': 'user',
                    'value': share_email,
                },
                'role': 'owner' # Grant owner role
            }
            try:
                service.acl().insert(calendarId=new_calendar_id, body=rule, sendNotifications=False).execute() # sendNotifications=False to avoid email spam during testing/setup
                logging.info(f"Successfully shared calendar '{new_calendar_id}' with '{share_email}' as 'owner'.")
            except Exception as acl_err:
                logging.error(f"Error sharing calendar '{new_calendar_id}' with '{share_email}': {acl_err}", exc_info=True)
                # Continue even if sharing fails, calendar is still created.
        else:
            logging.warning("OWNER_CALENDAR_EMAIL_SHARE_TARGET environment variable not set. New calendar will not be automatically shared.")

        return new_calendar_id
    except Exception as e:
        logging.error(f"Error finding or creating calendar for property ID '{property_id}': {e}", exc_info=True)
        return None

def check_calendar_availability(service, calendar_id, start_datetime_utc, end_datetime_utc):
    """
    Checks if a given time slot is available in the specified calendar.
    Considers events that are 'confirmed' or 'tentative'. Does not count 'cancelled' events.
    Args:
        service: Google Calendar API service instance.
        calendar_id (str): The ID of the calendar to check.
        start_datetime_utc (datetime): Start of the slot in UTC.
        end_datetime_utc (datetime): End of the slot in UTC.
    Returns:
        bool: True if available, False otherwise.
    """
    if not service or not calendar_id:
        logging.error("Calendar service or calendar_id not available for checking availability.")
        return False
    try:
        time_min_rfc = start_datetime_utc.isoformat()
        time_max_rfc = end_datetime_utc.isoformat()

        logging.info(f"Checking availability for calendar '{calendar_id}' between {time_min_rfc} and {time_max_rfc} (UTC)")

        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min_rfc,
            timeMax=time_max_rfc,
            singleEvents=True,
            orderBy='startTime',
            timeZone='UTC',
            showDeleted=False
        ).execute()

        items = events_result.get('items', [])

        conflicting_events = []
        for event in items:
            if event.get('status') == 'cancelled':
                continue

            event_start_str = event['start'].get('dateTime', event['start'].get('date'))
            event_end_str = event['end'].get('dateTime', event['end'].get('date'))

            try:
                if 'T' in event_start_str:
                    event_start_utc = datetime.fromisoformat(event_start_str.replace('Z', '+00:00')).astimezone(pytz.utc)
                else:
                    event_start_utc = pytz.utc.localize(datetime.fromisoformat(event_start_str))

                if 'T' in event_end_str:
                    event_end_utc = datetime.fromisoformat(event_end_str.replace('Z', '+00:00')).astimezone(pytz.utc)
                else:
                    event_end_utc = pytz.utc.localize(datetime.fromisoformat(event_end_str))
            except Exception as date_parse_err:
                logging.warning(f"Could not parse event times for event '{event.get('summary')}': {date_parse_err}. Skipping this event in availability check.")
                continue

            if event_start_utc < end_datetime_utc and event_end_utc > start_datetime_utc:
                conflicting_events.append(event)

        if not conflicting_events:
            logging.info(f"No conflicting (non-cancelled) events found. Slot is available in calendar '{calendar_id}'.")
            return True
        else:
            logging.info(f"Found {len(conflicting_events)} conflicting (non-cancelled) event(s) in calendar '{calendar_id}':")
            for item in conflicting_events:
                logging.info(f"  - Event: {item.get('summary')}, Start: {item['start'].get('dateTime', item['start'].get('date'))}, End: {item['end'].get('dateTime', item['end'].get('date'))}, Status: {item.get('status')}")
            return False

    except Exception as e:
        logging.error(f"Error checking calendar availability for calendar '{calendar_id}': {e}", exc_info=True)
        return False

def find_soonest_availability(service, calendar_id, start_datetime_utc, num_days, max_search_days=90):
    """
    Finds the soonest available date after a given start date.
    """
    current_date = start_datetime_utc
    for _ in range(max_search_days):
        end_datetime_utc = current_date + timedelta(days=num_days)
        if check_calendar_availability(service, calendar_id, current_date, end_datetime_utc):
            return current_date
        current_date += timedelta(days=1)
    return None

def create_booking_event(service, calendar_id, property_id_str, start_date_user_tz, num_days, client_name, client_phone, check_in_time_str="2 PM", check_out_time_str="11 AM"):
    """
    Creates a booking event.
    Args:
        start_date_user_tz (datetime): Start DATE of booking in user's local timezone (tz-aware). Time component is ignored.
        num_days (int): Number of nights.
    Returns:
        dict: The created event object if successful, None otherwise.
    """
    if not service or not calendar_id:
        logging.error("Calendar service or calendar_id not available for creating event.")
        return None

    try:
        parsed_check_in_time = dateparser.parse(check_in_time_str).time()
        check_in_datetime_user_tz = start_date_user_tz.replace(
            hour=parsed_check_in_time.hour,
            minute=parsed_check_in_time.minute,
            second=0, microsecond=0
        )
        actual_check_in_datetime_storage_tz = check_in_datetime_user_tz.astimezone(EVENT_STORAGE_TIMEZONE)

        check_out_date_user_tz = (start_date_user_tz.date() + timedelta(days=num_days))
        parsed_check_out_time = dateparser.parse(check_out_time_str).time()

        check_out_datetime_naive = datetime.combine(check_out_date_user_tz, parsed_check_out_time)
        check_out_datetime_user_tz = start_date_user_tz.tzinfo.localize(check_out_datetime_naive)
        actual_check_out_datetime_storage_tz = check_out_datetime_user_tz.astimezone(EVENT_STORAGE_TIMEZONE)

        event_summary_final = f"Booking: {property_id_str} - {client_name}"
        event_description = (
            f"Property: {property_id_str}\n"
            f"Client: {client_name}\n"
            f"Contact: {client_phone}\n"
            f"Check-in: {actual_check_in_datetime_storage_tz.strftime('%Y-%m-%d %I:%M %p %Z')}\n"
            f"Check-out: {actual_check_out_datetime_storage_tz.strftime('%Y-%m-%d %I:%M %p %Z')}\n"
            f"Nights: {num_days}"
        )

        logging.info(f"Creating event in calendar '{calendar_id}' ({EVENT_STORAGE_TIMEZONE.zone}) - "
                     f"Check-in: {actual_check_in_datetime_storage_tz.isoformat()}, "
                     f"Check-out: {actual_check_out_datetime_storage_tz.isoformat()}")

        event_body = {
            'summary': event_summary_final,
            'description': event_description,
            'start': {
                'dateTime': actual_check_in_datetime_storage_tz.isoformat(),
                'timeZone': EVENT_STORAGE_TIMEZONE.zone,
            },
            'end': {
                'dateTime': actual_check_out_datetime_storage_tz.isoformat(),
                'timeZone': EVENT_STORAGE_TIMEZONE.zone,
            },
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'popup', 'minutes': 60 * 24},
                    {'method': 'popup', 'minutes': 60 * 2}
                ],
            },
        }

        logging.debug(f"Calendar_handler: Event payload: {json.dumps(event_body, indent=2)}")
        created_event = service.events().insert(calendarId=calendar_id, body=event_body).execute()

        logging.info(f"Calendar_handler: Event created in '{calendar_id}'. Link: {created_event.get('htmlLink')}, ID: {created_event.get('id')}")
        return created_event

    except Exception as e:
        logging.error(f"Error creating booking event in calendar '{calendar_id}': {e}", exc_info=True)
        return None

def parse_user_date_input(date_str, user_timezone=DEFAULT_USER_INPUT_TIMEZONE):
    """
    Parses a date string (e.g., YYYY-MM-DD, "today", "tomorrow")
    Returns a datetime object for start of that day in user's timezone.
    """
    try:
        parsed_date_obj = dateparser.parse(date_str, settings={'PREFER_DATES_FROM': 'future'})
        if not parsed_date_obj:
            logging.error(f"Dateparser failed for date string: '{date_str}'")
            return None

        parsed_dt_naive = datetime.combine(parsed_date_obj.date(), datetime.min.time())
        parsed_dt_aware = user_timezone.localize(parsed_dt_naive)
        return parsed_dt_aware
    except Exception as e:
        logging.error(f"Failed to parse date string '{date_str}': {e}", exc_info=True)
        return None

def parse_human_datetime(text, reference_timezone_str=DEFAULT_USER_INPUT_TIMEZONE_STR):
    """
    Parse human-readable datetime string to datetime object, localized to reference_timezone.
    """
    try:
        settings = {'PREFER_DATES_FROM': 'future'}
        parsed_date = dateparser.parse(text, settings=settings)

        if parsed_date:
            target_tz = pytz.timezone(reference_timezone_str)
            if parsed_date.tzinfo is None or parsed_date.tzinfo.utcoffset(parsed_date) is None:
                parsed_date = target_tz.localize(parsed_date)
            else:
                parsed_date = parsed_date.astimezone(target_tz)
            return parsed_date
        logging.warning(f"Could not parse human datetime: '{text}'")
        return None
    except Exception as e:
        logging.error(f"Error parsing human datetime '{text}': {e}", exc_info=True)
        return None

# Original create_appointment function (commented out as it's superseded by create_booking_event)
# def create_appointment(summary, start_time_str_event_tz, user_phone, duration_minutes=60):
#    ... (original content) ...
#    pass


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')

    test_property_id = f"TEST_PROP_CAL_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    service = get_calendar_service()
    if not service:
        logging.error("Failed to get calendar service. Exiting test.")
        exit()

    logging.info(f"\n--- Test 1: Find or Create Calendar for Property ID: {test_property_id} ---")
    calendar_id = find_or_create_calendar_by_property_id(service, test_property_id)
    if not calendar_id:
        logging.error(f"Failed to find or create calendar for {test_property_id}. Exiting.")
        exit()
    logging.info(f"Using Calendar ID: {calendar_id} for {test_property_id}.")

    logging.info("\n--- Test 2: User Date Parsing ---")
    user_start_date_input_str = "tomorrow"
    parsed_user_start_date = parse_user_date_input(user_start_date_input_str, DEFAULT_USER_INPUT_TIMEZONE)
    assert parsed_user_start_date, f"Failed to parse user date input: {user_start_date_input_str}"
    logging.info(f"Parsed '{user_start_date_input_str}' to (user_tz): {parsed_user_start_date.isoformat()}")

    specific_date_str = (datetime.now(DEFAULT_USER_INPUT_TIMEZONE) + timedelta(days=5)).strftime("%Y-%m-%d")
    parsed_specific_date = parse_user_date_input(specific_date_str, DEFAULT_USER_INPUT_TIMEZONE)
    assert parsed_specific_date, f"Failed to parse specific date input: {specific_date_str}"
    logging.info(f"Parsed '{specific_date_str}' to (user_tz): {parsed_specific_date.isoformat()}")

    booking_start_date_user_tz = parsed_specific_date
    num_nights_test = 2
    check_in_time_test_str = "3:00 PM"
    check_out_time_test_str = "12:00 PM"

    _parsed_check_in_time = dateparser.parse(check_in_time_test_str).time()
    potential_start_dt_user_tz = booking_start_date_user_tz.replace(
        hour=_parsed_check_in_time.hour, minute=_parsed_check_in_time.minute
    )
    _checkout_date_user_tz = (booking_start_date_user_tz.date() + timedelta(days=num_nights_test))
    _parsed_check_out_time = dateparser.parse(check_out_time_test_str).time()
    _potential_end_dt_naive = datetime.combine(_checkout_date_user_tz, _parsed_check_out_time)
    potential_end_dt_user_tz = booking_start_date_user_tz.tzinfo.localize(_potential_end_dt_naive)

    potential_start_dt_utc = potential_start_dt_user_tz.astimezone(pytz.utc)
    potential_end_dt_utc = potential_end_dt_user_tz.astimezone(pytz.utc)

    logging.info(f"Test booking START (UTC for availability): {potential_start_dt_utc.isoformat()}")
    logging.info(f"Test booking END (UTC for availability): {potential_end_dt_utc.isoformat()}")

    logging.info("\n--- Test 3: Check Calendar Availability (Initial) ---")
    is_available = check_calendar_availability(service, calendar_id, potential_start_dt_utc, potential_end_dt_utc)
    logging.info(f"Initial availability for {test_property_id}: {is_available}")
    assert is_available, "Slot should be available initially."

    if is_available:
        logging.info("\n--- Test 4: Create Booking Event ---")
        created_event = create_booking_event(
            service, calendar_id, test_property_id,
            booking_start_date_user_tz, num_nights_test,
            "Test Client Name", "N/A",
            check_in_time_str=check_in_time_test_str,
            check_out_time_str=check_out_time_test_str
        )
        assert created_event, "Event creation failed."
        logging.info(f"Created event: {created_event.get('htmlLink')}, ID: {created_event.get('id')}")
        event_id_for_deletion = created_event.get('id')

        logging.info("\n--- Test 5: Check Calendar Availability (After Booking) ---")
        is_available_after = check_calendar_availability(service, calendar_id, potential_start_dt_utc, potential_end_dt_utc)
        logging.info(f"Availability after booking: {is_available_after}")
        assert not is_available_after, "Slot should NOT be available after booking."

        logging.info("\n--- Test 6: Attempt to Create Overlapping Booking ---")
        is_still_available_for_overlap = check_calendar_availability(
            service, calendar_id, potential_start_dt_utc, potential_start_dt_utc + timedelta(days=1)
        )
        assert not is_still_available_for_overlap, "Overlap check failed."
        logging.info("Correctly identified unavailability for overlapping event.")

        try:
            if event_id_for_deletion:
                logging.info(f"Deleting test event {event_id_for_deletion}...")
                service.events().delete(calendarId=calendar_id, eventId=event_id_for_deletion).execute()
                logging.info(f"Deleted test event {event_id_for_deletion}.")
        except Exception as e_del:
            logging.error(f"Error deleting test event {event_id_for_deletion}: {e_del}")

    # Optional: Delete test calendar (be cautious)
    # try:
    #     logging.info(f"\n--- Test 7: Deleting Test Calendar {test_property_id} ({calendar_id}) ---")
    #     service.calendars().delete(calendarId=calendar_id).execute()
    #     logging.info(f"Deleted test calendar {test_property_id}.")
    # except Exception e_cal_del:
    #     logging.error(f"Error deleting test calendar {test_property_id} ({calendar_id}): {e_cal_del}")

    logging.info("\n--- Calendar Handler Tests Complete ---")
