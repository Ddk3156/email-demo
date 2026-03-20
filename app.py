"""
app.py  —  Flask backend for AI Email Recognizer UI
====================================================
Run:   python app.py
Open:  http://localhost:5000
"""

import json
import logging
import threading
from flask import Flask, render_template, jsonify, request, session, Response
import queue
import time

logging.basicConfig(level=logging.WARNING)

# ── Paste your Gemini API key here ────────────────────────────
GEMINI_API_KEY = "AIzaSyDYyCuDT--Th9eZMpWs6Iw3kyrzikaAeV4"
# ─────────────────────────────────────────────────────────────

from gmail_service      import connect_gmail, fetch_emails, fetch_linked_images
from gemini_classifier  import GeminiClassifier
from email_processor    import EmailProcessor
from date_time import DateTimeExtractor

# Calendar imports — graceful if not yet installed
try:
    from calendar_service  import get_calendar_service, find_free_slots, check_calendar_setup
    from meeting_scheduler import MeetingScheduler
    CALENDAR_AVAILABLE = True
except ImportError:
    CALENDAR_AVAILABLE = False

app = Flask(__name__)
app.secret_key = "email_ai_secret_key_123"

# Global state
processor    = EmailProcessor()
classifier   = None
dt_extractor = None
mail_conn    = None
cal_service  = None       # Google Calendar service (after auth)
scheduler    = None       # MeetingScheduler instance
sse_clients  = []


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login", methods=["POST"])
def login():
    """Login to Gmail and fetch + classify emails."""
    global classifier

    data     = request.json
    address  = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not address or not password:
        return jsonify({"error": "Email and password are required."}), 400

    # Connect to Gmail
    try:
        mail = connect_gmail(address, password)
        mail_conn = mail          # keep alive for on-demand media fetching
    except ConnectionError as e:
        return jsonify({"error": str(e)}), 401

    # Fetch emails
    emails = fetch_emails(mail, max_emails=30)
    # Note: intentionally NOT calling mail.logout() here
    # We keep the connection open for on-demand media fetching via /api/email/<id>/media

    if not emails:
        return jsonify({"error": "No emails found in inbox."}), 404

    # Init Gemini classifier
    try:
        classifier   = GeminiClassifier(api_key=GEMINI_API_KEY)
        dt_extractor = DateTimeExtractor(gemini_model=classifier.model)
    except Exception as e:
        return jsonify({"error": f"Gemini error: {e}"}), 500

    # Classify in background thread, stream progress via SSE
    def classify_in_background():
        for i, email in enumerate(emails):
            email["category"] = classifier.categorize(email)
            # Broadcast update to all SSE clients
            _broadcast({
                "type":     "classified",
                "index":    i,
                "total":    len(emails),
                "email":    _safe_email(email),
            })
            time.sleep(0.3)
        _broadcast({"type": "done", "total": len(emails)})
        processor.load(emails)
        processor.save_cache()

    thread = threading.Thread(target=classify_in_background, daemon=True)
    thread.start()

    # Return raw emails immediately so UI can show them while classification runs
    return jsonify({
        "success": True,
        "emails":  [_safe_email(e) for e in emails],
        "total":   len(emails),
    })


@app.route("/api/emails")
def get_emails():
    """Return all classified emails."""
    return jsonify({"emails": [_safe_email(e) for e in processor.emails]})


@app.route("/api/summary")
def get_summary():
    """Return category summary counts."""
    return jsonify({"summary": processor.get_summary()})


@app.route("/api/category/<category>")
def by_category(category):
    emails = processor.show_emails(category)
    return jsonify({"emails": [_safe_email(e) for e in emails]})


@app.route("/api/search")
def search():
    keyword = request.args.get("q", "")
    emails  = processor.search_emails(keyword)
    return jsonify({"emails": [_safe_email(e) for e in emails]})


@app.route("/api/sender")
def by_sender():
    query  = request.args.get("q", "")
    emails = processor.show_sender_emails(query)
    return jsonify({"emails": [_safe_email(e) for e in emails]})


# ─────────────────────────────────────────────────────────────────────────────
# Media Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/email/<email_id>/media")
def get_email_media(email_id):
    """
    Return all media for an email.
    If the cached email has no media fields (old cache), re-fetches from IMAP.
    """
    from gmail_service import _parse_email

    email = _find_email(email_id)
    if not email:
        return jsonify({"error": "Email not found"}), 404

    # Re-fetch from IMAP if media fields are missing (stale cache)
    if "attachments" not in email and mail_conn:
        try:
            mail_conn.select("inbox")
            fresh = _parse_email(mail_conn, email_id.encode())
            if fresh:
                # Merge media fields into cached email
                email["attachments"]    = fresh.get("attachments", [])
                email["inline_images"]  = fresh.get("inline_images", [])
                email["linked_images"]  = fresh.get("linked_images", [])
                email["has_attachments"] = fresh.get("has_attachments", False)
                email["has_images"]      = fresh.get("has_images", False)
        except Exception as e:
            pass   # fall through with empty media

    # Fetch linked images on demand
    linked = []
    linked_urls = email.get("linked_images", [])
    if linked_urls:
        linked = fetch_linked_images(linked_urls)

    return jsonify({
        "email_id":        email_id,
        "attachments":     _safe_attachments(email.get("attachments", [])),
        "inline_images":   _safe_images(email.get("inline_images", [])),
        "linked_images":   _safe_images(linked),
        "has_attachments": email.get("has_attachments", False),
        "has_images":      email.get("has_images", False),
    })


@app.route("/api/email/<email_id>/attachment/<int:index>")
def download_attachment(email_id, index):
    """Serve a single attachment as a downloadable file."""
    from flask import send_file
    import base64
    import io

    email = _find_email(email_id)
    if not email:
        return jsonify({"error": "Email not found"}), 404

    attachments = email.get("attachments", [])
    if index >= len(attachments):
        return jsonify({"error": "Attachment index out of range"}), 404

    att      = attachments[index]
    data     = base64.b64decode(att["data_b64"])
    filename = att["filename"]
    mimetype = att["mime_type"]

    return send_file(
        io.BytesIO(data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/email/<email_id>/analyze-media", methods=["POST"])
def analyze_media_with_gemini(email_id):
    """
    Send email media (images) to Gemini for analysis.
    Accepts: { "type": "inline"|"linked"|"attachment", "index": 0 }
    Returns Gemini's description/analysis of the media.
    """
    global classifier

    if not classifier:
        return jsonify({"error": "Gemini not initialized. Login first."}), 400

    email = _find_email(email_id)
    if not email:
        return jsonify({"error": "Email not found"}), 404

    data       = request.json or {}
    media_type = data.get("type", "inline")
    index      = data.get("index", 0)
    prompt     = data.get("prompt", "Describe what you see in this image. If it's from an email, explain what the sender is trying to communicate.")

    # Get the right media item
    media_item = None
    if media_type == "inline":
        items = email.get("inline_images", [])
        if index < len(items):
            media_item = items[index]
    elif media_type == "linked":
        urls = email.get("linked_images", [])
        fetched = fetch_linked_images(urls)
        if index < len(fetched):
            media_item = fetched[index]
    elif media_type == "attachment":
        items = email.get("attachments", [])
        if index < len(items):
            att = items[index]
            # Only analyze image attachments with Gemini vision
            if not att["mime_type"].startswith("image/"):
                return jsonify({"error": "Gemini vision only works on image attachments"}), 400
            media_item = att

    if not media_item:
        return jsonify({"error": "Media item not found"}), 404

    try:
        import google.generativeai as genai
        import base64

        model = genai.GenerativeModel("gemini-1.5-flash")

        image_data = {
            "mime_type": media_item["mime_type"],
            "data":      media_item["data_b64"],
        }

        response = model.generate_content([
            {"text": prompt},
            {"inline_data": image_data},
        ])

        return jsonify({
            "analysis":  response.text,
            "mime_type": media_item["mime_type"],
            "size_str":  media_item.get("size_str", ""),
        })

    except Exception as e:
        return jsonify({"error": f"Gemini analysis failed: {e}"}), 500


# ─────────────────────────────────────────────────────────────
# DateTime Extraction Routes
# ─────────────────────────────────────────────────────────────

@app.route("/api/email/<email_id>/datetime")
def get_datetime(email_id):
    """
    Extract date/time/scheduling info from a single email.
    Regex fast-pass + Gemini structured extraction when scheduling detected.
    This is the DATA LAYER for the future meeting scheduler.
    """
    email = _find_email(email_id)
    if not email:
        return jsonify({"error": "Email not found"}), 404

    if "datetime_info" in email:
        return jsonify(email["datetime_info"])   # cached

    if not dt_extractor:
        return jsonify({"error": "Extractor not initialized. Login first."}), 400

    result = dt_extractor.extract(email)
    email["datetime_info"] = result
    return jsonify(result)


@app.route("/api/datetime/scan")
def scan_all_datetimes():
    """
    Scan ALL emails for scheduling intent.
    Returns only emails with dates/times/scheduling phrases.
    """
    if not dt_extractor:
        return jsonify({"error": "Extractor not initialized. Login first."}), 400

    results = []
    for email in processor.emails:
        if "datetime_info" not in email:
            email["datetime_info"] = dt_extractor.extract(email)
        info = email["datetime_info"]
        if info["all_raw"] or info["has_scheduling_intent"]:
            results.append({
                "email_id": email.get("id"),
                "subject":  email.get("subject", ""),
                "sender":   email.get("sender", ""),
                "date":     email.get("date", ""),
                "category": email.get("category", ""),
                "datetime": info,
            })

    return jsonify({
        "total_scanned":    len(processor.emails),
        "scheduling_found": len(results),
        "results":          results,
    })


# ─────────────────────────────────────────────────────────────
# Google Calendar Routes
# ─────────────────────────────────────────────────────────────

@app.route("/api/calendar/status")
def calendar_status():
    """Check if Google Calendar is set up and connected."""
    if not CALENDAR_AVAILABLE:
        return jsonify({
            "ready":   False,
            "step":    "not_installed",
            "message": "Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client",
        })
    status = check_calendar_setup()
    return jsonify(status)


@app.route("/api/calendar/auth")
def calendar_auth():
    """Trigger OAuth flow — opens browser for Google sign-in."""
    global cal_service, scheduler
    if not CALENDAR_AVAILABLE:
        return jsonify({"error": "Calendar dependencies not installed."}), 400
    try:
        cal_service = get_calendar_service()
        scheduler   = MeetingScheduler(
            calendar_service = cal_service,
            gemini_model     = classifier.model if classifier else None,
        )
        return jsonify({"success": True, "message": "Google Calendar connected."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calendar/schedule/<email_id>", methods=["POST"])
def schedule_from_email(email_id):
    """
    Run the full scheduling pipeline for one email.
    Finds free slots for all participants.
    Does NOT create the event — returns proposals for user to choose from.

    Body (optional JSON):
      { "duration_minutes": 60, "attendees": ["a@b.com"] }
    """
    if not scheduler:
        return jsonify({
            "error": "Calendar not connected. Call /api/calendar/auth first.",
            "needs_auth": True,
        }), 400

    email = _find_email(email_id)
    if not email:
        return jsonify({"error": "Email not found"}), 404

    # Get or compute datetime info
    if "datetime_info" not in email:
        if not dt_extractor:
            return jsonify({"error": "DateTime extractor not ready."}), 400
        email["datetime_info"] = dt_extractor.extract(email)

    data             = request.json or {}
    duration_minutes = int(data.get("duration_minutes", 60))
    attendees        = data.get("attendees") or None

    try:
        proposal = scheduler.schedule_from_email(
            email            = email,
            datetime_info    = email["datetime_info"],
            duration_minutes = duration_minutes,
            override_attendees = attendees,
        )
        # Cache proposal on email dict for confirm step
        email["_proposal"] = proposal
        return jsonify(proposal)
    except Exception as e:
        logger.error("Scheduling failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/calendar/confirm/<email_id>", methods=["POST"])
def confirm_meeting(email_id):
    """
    Create the Calendar event after user selects a slot.

    Body:
      { "slot": { ...slot dict from schedule response... } }
    """
    if not scheduler:
        return jsonify({"error": "Calendar not connected.", "needs_auth": True}), 400

    email = _find_email(email_id)
    if not email:
        return jsonify({"error": "Email not found"}), 404

    proposal = email.get("_proposal")
    if not proposal:
        return jsonify({"error": "No proposal found. Call /api/calendar/schedule first."}), 400

    data = request.json or {}
    slot = data.get("slot")
    if not slot:
        return jsonify({"error": "No slot provided in request body."}), 400

    # Allow attendee override at confirm time too
    if data.get("attendees"):
        proposal["attendees"] = data["attendees"]

    try:
        result = scheduler.confirm_and_create(proposal, slot)
        return jsonify(result)
    except Exception as e:
        logger.error("Event creation failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/calendar/free-slots", methods=["POST"])
def get_free_slots():
    """
    Check free slots for a list of attendees.

    Body: { "attendees": ["a@b.com","b@c.com"], "duration_minutes": 60 }
    """
    if not cal_service:
        return jsonify({"error": "Calendar not connected.", "needs_auth": True}), 400

    data     = request.json or {}
    attendees = data.get("attendees", [])
    duration  = int(data.get("duration_minutes", 60))

    if not attendees:
        return jsonify({"error": "No attendees provided."}), 400

    try:
        slots = find_free_slots(cal_service, attendees, duration_minutes=duration)
        return jsonify({"slots": slots, "attendees": attendees, "duration": duration})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Server-Sent Events (real-time classification progress)
# ─────────────────────────────────────────────────────────────

@app.route("/api/stream")
def stream():
    """SSE endpoint — browser connects here to get live updates."""
    q = queue.Queue()
    sse_clients.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                    if data.get("type") == "done":
                        break
                except queue.Empty:
                    yield "data: {\"type\": \"ping\"}\n\n"
        finally:
            if q in sse_clients:
                sse_clients.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _broadcast(data: dict):
    """Send data to all connected SSE clients."""
    for q in list(sse_clients):
        try:
            q.put_nowait(data)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _find_email(email_id: str) -> dict | None:
    """Find an email by ID across all loaded emails."""
    return next((e for e in processor.emails if e.get("id") == email_id), None)


def _safe_email(e: dict) -> dict:
    """Return a JSON-safe version of an email dict."""
    dt = e.get("datetime_info")
    return {
        "id":                  e.get("id", ""),
        "sender":              e.get("sender", ""),
        "subject":             e.get("subject", "(No Subject)"),
        "date":                e.get("date", ""),
        "body":                e.get("body", ""),
        "snippet":             (e.get("body", "") or "")[:120] + "..." if e.get("body") else "",
        "category":            e.get("category") or "Classifying...",
        "has_attachments":     e.get("has_attachments", False),
        "has_images":          e.get("has_images", False),
        "attachment_count":    len(e.get("attachments", [])),
        "inline_image_count":  len(e.get("inline_images", [])),
        "linked_image_count":  len(e.get("linked_images", [])),
        # Datetime fields — populated after extraction
        "has_datetime":        bool(dt and (dt.get("all_raw") or dt.get("has_scheduling_intent"))),
        "datetime_info":       dt,
    }


def _safe_attachments(attachments: list) -> list:
    """Return attachment metadata without the large base64 data."""
    return [
        {
            "filename":  a["filename"],
            "mime_type": a["mime_type"],
            "extension": a["extension"],
            "size_str":  a["size_str"],
            "index":     i,
            # Include data_b64 so UI can show preview / download
            "data_b64":  a["data_b64"],
        }
        for i, a in enumerate(attachments)
    ]


def _safe_images(images: list) -> list:
    """Return image list with data URIs for direct rendering in browser."""
    return [
        {
            "index":     i,
            "mime_type": img.get("mime_type", "image/jpeg"),
            "data_uri":  img.get("data_uri", ""),
            "size_str":  img.get("size_str", ""),
            "url":       img.get("url", ""),      # only for linked images
            "cid":       img.get("cid", ""),      # only for inline images
        }
        for i, img in enumerate(images)
    ]


# ─────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🚀 Starting AI Email Recognizer...")
    print("   Open http://localhost:5000 in your browser\n")
    app.run(debug=False, threaded=True, port=5000)
