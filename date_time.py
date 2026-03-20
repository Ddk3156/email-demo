"""
datetime_extractor.py
----------------------
Extracts dates, times, scheduling intent, and event context from email text.
Uses regex for fast detection + Gemini for structured slot extraction.

Output is designed as the DATA LAYER for a future meeting scheduler:
  - normalized UTC times ready for Google Calendar API
  - scheduling intent classification
  - participant list
  - event title suggestion

No external dependencies — works with stdlib + existing google-generativeai.
"""

import re
import json
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────

# Clock times: 3pm, 3:30 PM, 15:00, 10 AM IST, 3:30–5:00 PM
_TIME_RE = re.compile(
    r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)\s*'
    r'(IST|EST|PST|CST|MST|GMT|UTC|BST|CET|JST|AEST)?\b'
    r'|'
    r'\b([01]?\d|2[0-3]):([0-5]\d)\s*(IST|EST|PST|CST|MST|GMT|UTC)?\b',
    re.IGNORECASE
)

# Time ranges: 2pm–4pm, 10:00 AM to 11:30 AM, between 3 and 5 PM
_RANGE_RE = re.compile(
    r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*'
    r'(?:to|-|until|–|—)\s*'
    r'(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b',
    re.IGNORECASE
)

# Named-month dates: January 5, 5th March 2026, Mar 9
_NAMED_DATE_RE = re.compile(
    r'\b(\d{1,2})(?:st|nd|rd|th)?\s+'
    r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'(?:\s+(\d{4}))?\b'
    r'|\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+'
    r'(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?\b',
    re.IGNORECASE
)

# Numeric dates: 2026-03-15, 15/03/2026
_NUMERIC_DATE_RE = re.compile(
    r'\b(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4})\b'
)

# Day names
_DAY_RE = re.compile(
    r'\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday'
    r'|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b',
    re.IGNORECASE
)

# Relative expressions
_RELATIVE_RE = re.compile(
    r'\b(today|tomorrow|yesterday|'
    r'next\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|week|month)|'
    r'this\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|week|morning|afternoon|evening)|'
    r'(?:in\s+)?\d+\s+(?:days?|weeks?|hours?)(?:\s+time)?)\b',
    re.IGNORECASE
)

# Scheduling-intent trigger phrases
_SCHEDULING_RE = re.compile(
    r'\b(available|availability|free slot|schedule|meeting|call|sync|catch up|'
    r'discuss|let\'?s meet|time works|work for you|convenient|'
    r'let me know when|when are you|can we meet|set up a|book a|'
    r'confirm(?:ation)?|register(?:ed)?|join(?:ing)?|attend(?:ing)?|'
    r'webinar|workshop|conference|event|session|interview|demo)\b',
    re.IGNORECASE
)

# ── Timezone offset map (for normalization) ───────────────────────────────────
_TZ_OFFSETS = {
    "IST": +5.5, "EST": -5, "PST": -8, "CST": -6,
    "MST": -7,   "GMT": 0,  "UTC": 0,  "BST": +1,
    "CET": +1,   "JST": +9, "AEST": +10,
}

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


class DateTimeExtractor:

    def __init__(self, gemini_model=None):
        """
        Args:
            gemini_model: an already-initialized genai.GenerativeModel instance
                          (reuse from GeminiClassifier — no extra API calls)
        """
        self.model = gemini_model

    # ── Public API ────────────────────────────────────────────────────────────

    def extract(self, email: dict) -> dict:
        """
        Full extraction pipeline for one email dict.
        Returns a rich dict ready to be stored and used by the scheduler.
        """
        text    = f"{email.get('subject', '')}\n{email.get('body', '')}"
        sent_at = _parse_sent_date(email.get("date", ""))

        times      = self._find_times(text)
        dates      = self._find_dates(text)
        relatives  = self._find_relative(text)
        ranges     = self._find_ranges(text)
        has_intent = bool(_SCHEDULING_RE.search(text))

        all_raw = list(dict.fromkeys(times + dates + relatives +
                       [f"{r[0]} – {r[1]}" for r in ranges]))

        result = {
            "email_id":          email.get("id"),
            "subject":           email.get("subject", ""),
            "sent_at":           sent_at,
            "has_scheduling_intent": has_intent,
            "times_found":       times,
            "dates_found":       dates,
            "relative_found":    relatives,
            "time_ranges":       [{"start": r[0], "end": r[1]} for r in ranges],
            "all_raw":           all_raw,
            "gemini":            None,
        }

        # Gemini deep parse — only when scheduling intent detected
        if self.model and has_intent and (all_raw or relatives):
            result["gemini"] = self._gemini_extract(email, text)

        return result

    # ── Regex extractors ──────────────────────────────────────────────────────

    def _find_times(self, text: str) -> list[str]:
        results = []
        for m in _TIME_RE.finditer(text):
            raw = m.group(0).strip()
            if raw and raw not in results:
                results.append(raw)
        return results

    def _find_dates(self, text: str) -> list[str]:
        results = []
        for m in _NAMED_DATE_RE.finditer(text):
            raw = m.group(0).strip()
            if raw and raw not in results:
                results.append(raw)
        for m in _NUMERIC_DATE_RE.finditer(text):
            raw = m.group(0).strip()
            if raw not in results:
                results.append(raw)
        for m in _DAY_RE.finditer(text):
            raw = m.group(0).strip()
            if raw not in results:
                results.append(raw)
        return results

    def _find_relative(self, text: str) -> list[str]:
        results = []
        for m in _RELATIVE_RE.finditer(text):
            raw = m.group(0).strip()
            if raw not in results:
                results.append(raw)
        return results

    def _find_ranges(self, text: str) -> list[tuple]:
        results = []
        for m in _RANGE_RE.finditer(text):
            results.append((m.group(1).strip(), m.group(2).strip()))
        return results

    # ── Gemini structured extraction ──────────────────────────────────────────

    def _gemini_extract(self, email: dict, text: str) -> dict | None:
        prompt = f"""Extract ALL scheduling information from this email. 
Return ONLY valid JSON, no markdown, no explanation.

Email Subject: {email.get('subject', '')}
Email Body:
{text[:1500]}

Return exactly this structure:
{{
  "intent": "request_meeting|share_availability|confirm_meeting|event_registration|reminder|other",
  "event_title": "suggested calendar event title or null",
  "slots": [
    {{
      "raw": "exact text as written in email",
      "date": "YYYY-MM-DD or null",
      "start_time": "HH:MM 24h or null",
      "end_time": "HH:MM 24h or null",
      "timezone": "IST/UTC/etc or null",
      "utc_offset_hours": number or null,
      "is_negative": false
    }}
  ],
  "participants": ["email or name mentioned"],
  "urgency": "asap|this_week|next_week|specific_date|flexible|null",
  "location": "physical address or video link or null",
  "notes": "anything ambiguous or needing clarification"
}}"""

        try:
            response = self.model.generate_content(prompt)
            raw = response.text.strip()
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
            data = json.loads(raw)

            # Enrich slots with UTC normalization
            for slot in data.get("slots", []):
                slot["utc_normalized"] = _to_utc(
                    slot.get("date"),
                    slot.get("start_time"),
                    slot.get("timezone"),
                    slot.get("utc_offset_hours"),
                )

            return data

        except json.JSONDecodeError:
            logger.warning("Gemini returned invalid JSON for datetime extraction")
            return {"error": "parse_failed", "raw": response.text[:200]}
        except Exception as e:
            logger.warning("Gemini datetime extraction failed: %s", e)
            return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_sent_date(date_str: str) -> str | None:
    if not date_str or date_str == "Unknown":
        return None
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc).isoformat()
    except Exception:
        return date_str


def _to_utc(date_str, time_str, tz_name, utc_offset) -> str | None:
    """Convert a date+time+timezone to UTC ISO string for calendar API."""
    if not date_str or not time_str:
        return None
    try:
        dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
        offset_h = utc_offset
        if offset_h is None and tz_name:
            offset_h = _TZ_OFFSETS.get(tz_name.upper())
        if offset_h is not None:
            dt -= timedelta(hours=offset_h)
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return None
