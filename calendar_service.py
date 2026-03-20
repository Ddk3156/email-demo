"""
calendar_service.py
--------------------
Google Calendar API integration for the meeting scheduler.

FIRST-TIME SETUP (do this once before running):
================================================
1. Go to https://console.cloud.google.com
2. Create a project (or select existing)
3. Enable "Google Calendar API"
4. Go to APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
5. Application type: Desktop App
6. Download the JSON → save as  credentials.json  in this folder
7. Run this file directly:  python calendar_service.py
   → Browser opens, sign in, grant access
   → token.json is created automatically (never commit this file)
8. Done — token.json is reused on every subsequent run

SCOPES used:
  calendar.readonly  — read events / check free-busy
  calendar.events    — create / update events
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# OAuth scopes needed
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE       = "token.json"


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

def get_calendar_service():
    """
    Return an authenticated Google Calendar API service object.
    Handles token refresh automatically.
    Raises RuntimeError if credentials.json is missing.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "\n❌ Google Calendar dependencies not installed.\n"
            "   Run:  pip install google-auth google-auth-oauthlib "
            "google-auth-httplib2 google-api-python-client"
        )

    if not os.path.exists(CREDENTIALS_FILE):
        raise RuntimeError(
            f"\n❌ {CREDENTIALS_FILE} not found!\n"
            "   Follow the setup steps at the top of calendar_service.py\n"
            "   to create OAuth credentials from Google Cloud Console."
        )

    creds = None

    # Load existing token
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    # Refresh or re-authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for next run
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        print(f"✅ Calendar token saved to {TOKEN_FILE}")

    return build("calendar", "v3", credentials=creds)


def check_calendar_setup() -> dict:
    """
    Check if calendar is properly set up.
    Returns status dict for the API to report to UI.
    """
    has_credentials = os.path.exists(CREDENTIALS_FILE)
    has_token       = os.path.exists(TOKEN_FILE)

    if not has_credentials:
        return {
            "ready": False,
            "step":  "missing_credentials",
            "message": "credentials.json not found. Follow setup steps in calendar_service.py.",
        }

    if not has_token:
        return {
            "ready": False,
            "step":  "needs_auth",
            "message": "OAuth not yet authorized. Call /api/calendar/auth to open browser flow.",
        }

    # Try a quick API call to verify token works
    try:
        service = get_calendar_service()
        service.calendarList().list(maxResults=1).execute()
        return {"ready": True, "step": "ready", "message": "Google Calendar connected."}
    except Exception as e:
        return {
            "ready": False,
            "step":  "auth_failed",
            "message": f"Token invalid or expired: {e}. Call /api/calendar/auth to re-authenticate.",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Free / Busy checking
# ─────────────────────────────────────────────────────────────────────────────

def get_free_busy(service, attendee_emails: list[str],
                  time_min: datetime, time_max: datetime) -> dict:
    """
    Query Google Calendar Free/Busy API for multiple attendees.

    Returns:
        {
          "email@x.com": {
            "busy": [{"start": "...", "end": "..."}],
            "errors": []
          },
          ...
        }
    """
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "timeZone": "UTC",
        "items": [{"id": email} for email in attendee_emails],
    }

    try:
        result   = service.freebusy().query(body=body).execute()
        calendars = result.get("calendars", {})
        return {
            email: {
                "busy":   cal.get("busy", []),
                "errors": cal.get("errors", []),
            }
            for email, cal in calendars.items()
        }
    except Exception as e:
        logger.error("Free/busy query failed: %s", e)
        raise


def find_free_slots(service, attendee_emails: list[str],
                    duration_minutes: int = 60,
                    days_ahead: int = 7,
                    working_hours: tuple = (9, 18),
                    timezone_offset: float = 5.5) -> list[dict]:
    """
    Find time slots where ALL attendees are free.

    Args:
        attendee_emails:  list of Gmail addresses to check
        duration_minutes: required meeting length in minutes
        days_ahead:       how many days into the future to look
        working_hours:    (start_hour, end_hour) in local time
        timezone_offset:  UTC offset of desired working hours (default IST +5.5)

    Returns:
        List of free slot dicts:
        [
          {
            "start_utc":   "2026-03-15T09:00:00+00:00",
            "end_utc":     "2026-03-15T10:00:00+00:00",
            "start_local": "2026-03-15T14:30:00+05:30",
            "end_local":   "2026-03-15T15:30:00+05:30",
            "duration_minutes": 60,
            "attendees_count": 3,
          },
          ...
        ]
    """
    now      = datetime.now(timezone.utc)
    time_min = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    time_max = time_min + timedelta(days=days_ahead)

    # Fetch busy periods for all attendees
    busy_data = get_free_busy(service, attendee_emails, time_min, time_max)

    # Build unified busy list (merge all attendees' busy times)
    all_busy = []
    for email, data in busy_data.items():
        for period in data.get("busy", []):
            try:
                start = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
                end   = datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
                all_busy.append((start, end))
            except Exception:
                continue

    # Sort and merge overlapping busy periods
    all_busy.sort(key=lambda x: x[0])
    merged_busy = _merge_intervals(all_busy)

    # Scan for free slots within working hours
    free_slots  = []
    slot_delta  = timedelta(minutes=duration_minutes)
    tz_offset   = timedelta(hours=timezone_offset)
    check_time  = time_min

    while check_time < time_max and len(free_slots) < 10:
        # Convert to local time to check working hours
        local_time  = check_time + tz_offset
        hour        = local_time.hour

        # Skip weekends
        if local_time.weekday() >= 5:
            check_time += timedelta(hours=1)
            continue

        # Skip outside working hours
        if hour < working_hours[0] or hour >= working_hours[1]:
            # Jump to next working day start
            if hour >= working_hours[1]:
                check_time = (check_time + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                check_time += timedelta(hours=working_hours[0]) - tz_offset
            else:
                check_time = check_time.replace(hour=0, minute=0, second=0, microsecond=0)
                check_time += timedelta(hours=working_hours[0]) - tz_offset
            continue

        slot_end = check_time + slot_delta

        # Check if this slot overlaps with any busy period
        is_free = not any(
            busy_start < slot_end and busy_end > check_time
            for busy_start, busy_end in merged_busy
        )

        if is_free:
            local_start = check_time + tz_offset
            local_end   = slot_end   + tz_offset
            free_slots.append({
                "start_utc":        check_time.isoformat(),
                "end_utc":          slot_end.isoformat(),
                "start_local":      local_start.isoformat(),
                "end_local":        local_end.isoformat(),
                "duration_minutes": duration_minutes,
                "attendees_count":  len(attendee_emails),
                "display":          _format_slot(local_start, local_end),
            })
            check_time = slot_end   # next check starts after this slot
        else:
            check_time += timedelta(minutes=30)  # step forward 30 min

    return free_slots


def _merge_intervals(intervals: list) -> list:
    """Merge overlapping time intervals."""
    if not intervals:
        return []
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _format_slot(start: datetime, end: datetime) -> str:
    """Human-readable slot display string."""
    day  = start.strftime("%a, %b %-d")
    s    = start.strftime("%-I:%M %p")
    e    = end.strftime("%-I:%M %p")
    return f"{day}  ·  {s} – {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Event creation
# ─────────────────────────────────────────────────────────────────────────────

def create_event(service, title: str, start_utc: str, end_utc: str,
                 attendees: list[str], description: str = "",
                 location: str = "") -> dict:
    """
    Create a Google Calendar event and send invites to all attendees.

    Args:
        title:       event summary/title
        start_utc:   ISO datetime string (UTC)
        end_utc:     ISO datetime string (UTC)
        attendees:   list of email addresses
        description: optional event description
        location:    optional location or video link

    Returns:
        Created event dict from Calendar API
    """
    event = {
        "summary":     title,
        "description": description,
        "location":    location,
        "start": {
            "dateTime": start_utc,
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": end_utc,
            "timeZone": "UTC",
        },
        "attendees": [{"email": email} for email in attendees],
        "conferenceData": {
            "createRequest": {
                "requestId":             f"meet-{int(datetime.now().timestamp())}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email",  "minutes": 24 * 60},
                {"method": "popup",  "minutes": 15},
            ],
        },
        "guestsCanModifyEvent": False,
        "sendUpdates": "all",    # sends invite emails to all attendees
    }

    result = service.events().insert(
        calendarId="primary",
        body=event,
        conferenceDataVersion=1,   # needed for Meet link generation
        sendNotifications=True,
    ).execute()

    return {
        "event_id":   result.get("id"),
        "html_link":  result.get("htmlLink"),
        "meet_link":  result.get("hangoutLink"),
        "title":      result.get("summary"),
        "start":      result.get("start", {}).get("dateTime"),
        "end":        result.get("end",   {}).get("dateTime"),
        "attendees":  [a["email"] for a in result.get("attendees", [])],
        "status":     result.get("status"),
    }


def check_duplicate_event(service, title: str,
                           start_utc: str, attendees: list[str]) -> dict | None:
    """
    Check if an event with the same title and time already exists.
    Returns the existing event dict if found, None otherwise.
    Prevents duplicate meeting creation.
    """
    try:
        start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
        # Search ±30 minutes around the proposed start time
        time_min = (start - timedelta(minutes=30)).isoformat()
        time_max = (start + timedelta(minutes=30)).isoformat()

        events = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            q=title,
            singleEvents=True,
        ).execute()

        for event in events.get("items", []):
            if event.get("summary", "").lower() == title.lower():
                return event   # duplicate found

        return None
    except Exception as e:
        logger.warning("Duplicate check failed: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test — run directly to verify setup
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🗓  Google Calendar Setup Test\n" + "=" * 40)

    status = check_calendar_setup()
    print(f"Status: {status['step']}")
    print(f"Message: {status['message']}")

    if status["ready"]:
        service = get_calendar_service()
        print("\n✅ Calendar connected! Testing free slot finder...")

        # Test with your own email
        your_email = input("\nEnter your Gmail address to test free/busy: ").strip()
        slots = find_free_slots(service, [your_email], duration_minutes=60)

        print(f"\n📅 Found {len(slots)} free 1-hour slots in next 7 days:")
        for i, s in enumerate(slots[:5], 1):
            print(f"  {i}. {s['display']}")
