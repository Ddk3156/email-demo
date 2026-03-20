"""
main.py  —  AI Email Recognizer
================================
HOW TO USE:
  1. Paste your Gemini API key below
     Get one free at: https://aistudio.google.com/app/apikey
  2. Run:  python main.py
  3. Choose mode:
       [1] Real-Time Monitor  — watches inbox live, categorizes instantly on arrival
       [2] Fetch & Browse     — fetches recent emails, lets you search/filter them

GMAIL APP PASSWORD (required):
  • Go to https://myaccount.google.com/apppasswords
  • Select Mail → Generate
  • Use that 16-character password when prompted
"""

import sys
import logging

# ─────────────────────────────────────────────────────────────────────────────
#  🔑  PASTE YOUR GEMINI API KEY HERE
# ─────────────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = "AIzaSyDYyCuDT--Th9eZMpWs6Iw3kyrzikaAeV4"
# ─────────────────────────────────────────────────────────────────────────────

MAX_EMAILS = 30    # How many emails to fetch in Browse mode

logging.basicConfig(level=logging.WARNING)

from gmail_service     import connect_gmail, fetch_emails
from gemini_classifier import GeminiClassifier
from email_processor   import EmailProcessor
from realtime_monitor  import RealtimeMonitor


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def print_banner():
    print("""
╔══════════════════════════════════════════════════════╗
║          🤖  AI Email Recognizer  📧                ║
║        Gmail IMAP  +  Google Gemini AI              ║
╚══════════════════════════════════════════════════════╝
""")


# ---------------------------------------------------------------------------
# Gmail login prompt (shared by both modes)
# ---------------------------------------------------------------------------

def prompt_gmail_login():
    print("─" * 50)
    print("📬  Gmail Login")
    print("─" * 50)
    address  = input("  Enter your Gmail address : ").strip()
    password = input("  Enter your App Password  : ").strip()
    print()
    return address, password


# ---------------------------------------------------------------------------
# Mode 1 — Real-Time Monitor
# ---------------------------------------------------------------------------

def run_realtime(gmail_address: str, gmail_password: str):
    """Watch inbox live — classify every new email instantly as it arrives."""
    if GEMINI_API_KEY == "PASTE_YOUR_GEMINI_API_KEY_HERE":
        print("❌ Gemini API key not set! Open main.py and paste your key on line 22.")
        sys.exit(1)

    monitor = RealtimeMonitor(
        email_address  = gmail_address,
        app_password   = gmail_password,
        gemini_api_key = GEMINI_API_KEY,
    )
    monitor.start()


# ---------------------------------------------------------------------------
# Mode 2 — Fetch & Browse
# ---------------------------------------------------------------------------

def run_fetch_and_browse(gmail_address: str, gmail_password: str):
    """Fetch recent emails, classify them, then browse interactively."""

    # Connect + fetch
    print("🔗 Connecting to Gmail...")
    try:
        mail = connect_gmail(gmail_address, gmail_password)
    except ConnectionError as e:
        print(e)
        sys.exit(1)

    emails = fetch_emails(mail, max_emails=MAX_EMAILS)
    mail.logout()

    if not emails:
        print("⚠️  No emails found.")
        sys.exit(0)

    print(f"✅ Fetched {len(emails)} emails.\n")

    # Classify
    if GEMINI_API_KEY == "PASTE_YOUR_GEMINI_API_KEY_HERE":
        print("❌ Gemini API key not set! Open main.py and paste your key on line 22.")
        sys.exit(1)

    try:
        classifier = GeminiClassifier(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"❌ Gemini setup failed: {e}")
        sys.exit(1)

    classifier.categorize_all(emails, delay=5.0)

    # Load and show summary
    processor = EmailProcessor()
    processor.load(emails)
    processor.save_cache()
    processor.print_summary()

    # Interactive CLI
    print_help()
    run_cli(processor)


# ---------------------------------------------------------------------------
# CLI (Browse mode)
# ---------------------------------------------------------------------------

def print_help():
    print("""
Commands:
  show summary            — Category counts
  show category <n>    — e.g.  show category Work
  show sender <query>     — e.g.  show sender amazon
  search <keyword>        — e.g.  search invoice
  detail <number>         — Full email  e.g. detail 3
  help / quit
""")


def run_cli(processor: EmailProcessor):
    last_results = []
    while True:
        try:
            raw = input("email-ai> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye! 👋")
            break

        if not raw:
            continue

        parts = raw.split(None, 2)
        verb  = parts[0].lower()

        if verb in ("quit", "exit", "q"):
            print("Goodbye! 👋")
            break
        elif verb == "help":
            print_help()
        elif verb == "show":
            if len(parts) < 2:
                print("  Usage: show summary | show category <n> | show sender <q>")
                continue
            sub = parts[1].lower()
            arg = parts[2].strip() if len(parts) > 2 else ""
            if sub == "summary":
                processor.print_summary()
            elif sub == "category":
                last_results = processor.show_emails(arg) if arg else []
            elif sub == "sender":
                last_results = processor.show_sender_emails(arg) if arg else []
            else:
                print(f"  Unknown: '{sub}'")
        elif verb == "search":
            arg = parts[1] if len(parts) > 1 else ""
            last_results = processor.search_emails(arg) if arg else []
        elif verb == "detail":
            if not last_results:
                print("  Run a list command first.")
            elif len(parts) < 2 or not parts[1].isdigit():
                print("  Usage: detail <number>")
            else:
                idx = int(parts[1]) - 1
                if 0 <= idx < len(last_results):
                    EmailProcessor.print_detail(last_results[idx])
                else:
                    print(f"  Out of range (1–{len(last_results)})")
        else:
            print(f"  Unknown command '{raw}'. Type 'help'.")


# ---------------------------------------------------------------------------
# Entry point — choose mode
# ---------------------------------------------------------------------------

def main():
    print_banner()

    print("  Choose a mode:")
    print("  [1] 📡 Real-Time Monitor  — get notified & categorized instantly as emails arrive")
    print("  [2] 📥 Fetch & Browse     — fetch recent emails and explore them\n")

    choice = input("  Enter 1 or 2: ").strip()

    gmail_address, gmail_password = prompt_gmail_login()

    if choice == "1":
        run_realtime(gmail_address, gmail_password)
    elif choice == "2":
        run_fetch_and_browse(gmail_address, gmail_password)
    else:
        print("Invalid choice. Please enter 1 or 2.")
        sys.exit(1)


if __name__ == "__main__":
    main()