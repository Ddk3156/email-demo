"""
realtime_monitor.py
--------------------
Monitors Gmail inbox in real-time using IMAP IDLE.
When a new email arrives, it is instantly fetched,
classified by Gemini, and displayed in the terminal.
"""

import imaplib
import time
import logging
import socket
from datetime import datetime

from gmail_service import connect_gmail, _parse_email
from gemini_classifier import GeminiClassifier

logger = logging.getLogger(__name__)

# How long to wait in IDLE before refreshing the connection (seconds)
IDLE_TIMEOUT = 60 * 5   # 5 minutes


class RealtimeMonitor:
    """
    Keeps a live IMAP connection open and watches for new emails.
    Classifies each new email with Gemini as soon as it arrives.
    """

    def __init__(self, email_address: str, app_password: str, gemini_api_key: str):
        self.email_address = email_address
        self.app_password  = app_password
        self.classifier    = GeminiClassifier(api_key=gemini_api_key)
        self.mail          = None
        self.known_ids     = set()   # IDs we've already seen

    # ------------------------------------------------------------------
    # Start monitoring
    # ------------------------------------------------------------------

    def start(self):
        """Connect to Gmail and begin watching for new emails."""
        print("\n" + "=" * 55)
        print("📡  Real-Time Email Monitor Started")
        print("   Watching your inbox for new emails...")
        print("   Press Ctrl+C to stop.")
        print("=" * 55 + "\n")

        while True:
            try:
                self._connect()
                self._load_existing_ids()
                self._watch()
            except KeyboardInterrupt:
                print("\n\n👋 Monitor stopped.")
                break
            except Exception as e:
                print(f"\n⚠️  Connection lost: {e}")
                print("🔄 Reconnecting in 10 seconds...")
                time.sleep(10)
            finally:
                self._disconnect()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _connect(self):
        """Open a fresh IMAP connection."""
        self.mail = connect_gmail(self.email_address, self.app_password)
        self.mail.select("inbox")

    def _disconnect(self):
        """Safely close the IMAP connection."""
        try:
            if self.mail:
                self.mail.logout()
        except Exception:
            pass
        self.mail = None

    # ------------------------------------------------------------------
    # Seed known IDs so we don't re-process old emails on startup
    # ------------------------------------------------------------------

    def _load_existing_ids(self):
        """Record all current inbox IDs so we only process truly new ones."""
        _, data = self.mail.search(None, "ALL")
        if data and data[0]:
            self.known_ids = set(data[0].split())
        print(f"📬 Watching inbox ({len(self.known_ids)} existing emails ignored)\n")

    # ------------------------------------------------------------------
    # Main watch loop
    # ------------------------------------------------------------------

    def _watch(self):
        """
        Poll Gmail every 15 seconds for new emails.
        Falls back to polling if IMAP IDLE is not supported.
        """
        supports_idle = b"IDLE" in self.mail.capabilities

        if supports_idle:
            self._watch_with_idle()
        else:
            self._watch_with_polling()

    def _watch_with_idle(self):
        """Use IMAP IDLE for instant push-style notifications."""
        print("⚡ Using IMAP IDLE (instant notifications)\n")
        while True:
            # Send IDLE command — server will push updates
            self.mail.send(b"IDLE\r\n")

            # Wait for server notification or timeout
            self.mail.socket().settimeout(IDLE_TIMEOUT)
            try:
                line = self.mail.readline()
                if b"EXISTS" in line or b"RECENT" in line:
                    # Tell server we're done idling
                    self.mail.send(b"DONE\r\n")
                    self.mail.readline()  # Read server response
                    self._check_new_emails()
            except socket.timeout:
                # Timeout — send DONE and re-issue IDLE to keep connection alive
                self.mail.send(b"DONE\r\n")
                self.mail.readline()

    def _watch_with_polling(self):
        """Poll every 15 seconds as fallback."""
        print("🔄 Using polling (checks every 15 seconds)\n")
        while True:
            self._check_new_emails()
            time.sleep(15)

    # ------------------------------------------------------------------
    # Check + process new emails
    # ------------------------------------------------------------------

    def _check_new_emails(self):
        """Search inbox for IDs we haven't seen yet and process them."""
        try:
            self.mail.select("inbox")
            _, data = self.mail.search(None, "ALL")
            if not data or not data[0]:
                return

            all_ids    = set(data[0].split())
            new_ids    = all_ids - self.known_ids

            for eid in new_ids:
                self.known_ids.add(eid)
                self._process_new_email(eid)

        except Exception as e:
            logger.warning("Error checking new emails: %s", e)
            raise   # Let the outer loop handle reconnection

    def _process_new_email(self, email_id: bytes):
        """Fetch, classify, and display a single new email."""
        try:
            parsed = _parse_email(self.mail, email_id)
            if not parsed:
                return

            # Classify with Gemini
            category = self.classifier.categorize(parsed)
            parsed["category"] = category

            # Display
            self._print_new_email(parsed)

        except Exception as e:
            logger.warning("Failed to process email ID %s: %s", email_id, e)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    @staticmethod
    def _print_new_email(email: dict):
        now = datetime.now().strftime("%H:%M:%S")
        print("\n" + "🔔 " + "─" * 55)
        print(f"  ⏰ Received  : {now}")
        print(f"  📂 Category  : {email.get('category', 'Unknown')}")
        print(f"  👤 From      : {email.get('sender', '')}")
        print(f"  📝 Subject   : {email.get('subject', '')}")
        print(f"  📅 Date      : {email.get('date', '')}")
        body = email.get("body", "").strip()
        if body:
            preview = body[:200].replace("\n", " ")
            print(f"  💬 Preview   : {preview}...")
        print("─" * 57 + "\n")
