"""
email_processor.py
-------------------
Stores all processed emails and provides retrieval methods.
"""

import json
import os
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)
CACHE_FILE = "emails_cache.json"


class EmailProcessor:
    """Holds all emails and provides filtering/search/summary."""

    def __init__(self):
        self.emails: list[dict] = []
        self._index: dict[str, list[dict]] = defaultdict(list)

    def load(self, emails: list[dict]):
        """Load a list of categorized emails and build the index."""
        self.emails = emails
        self._index = defaultdict(list)
        for e in emails:
            self._index[e.get("category", "Personal")].append(e)

    def save_cache(self):
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.emails, f, ensure_ascii=False, indent=2)

    def load_cache(self) -> bool:
        if not os.path.exists(CACHE_FILE):
            return False
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                self.load(json.load(f))
            print(f"📂 Loaded {len(self.emails)} emails from cache.")
            return True
        except Exception as e:
            logger.warning("Cache load failed: %s", e)
            return False

    def clear_cache(self):
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)

    # ── Summary ─────────────────────────────────────────────────────────

    def print_summary(self):
        print("\n" + "=" * 45)
        print("📊  Category Summary")
        print("=" * 45)
        if not self._index:
            print("  No emails loaded.")
        else:
            for cat, items in sorted(self._index.items()):
                bar = "█" * min(len(items), 25)
                print(f"  {cat:<14} {len(items):>4} emails  {bar}")
        print("=" * 45 + "\n")

    # ── Retrieval ───────────────────────────────────────────────────────

    def show_emails(self, category: str) -> list[dict]:
        """Get emails by category (case-insensitive)."""
        key = category.strip().title()
        results = self._index.get(key, [])
        self._print_list(results, f"Category: {key}")
        return results

    def show_sender_emails(self, query: str) -> list[dict]:
        """Get emails where sender contains the query string."""
        q = query.lower()
        results = [e for e in self.emails if q in e.get("sender", "").lower()]
        self._print_list(results, f"Sender contains: '{query}'")
        return results

    def search_emails(self, keyword: str) -> list[dict]:
        """Search emails by keyword in subject or body."""
        kw = keyword.lower()
        results = [
            e for e in self.emails
            if kw in e.get("subject", "").lower() or kw in e.get("body", "").lower()
        ]
        self._print_list(results, f"Keyword: '{keyword}'")
        return results

    # ── Display ─────────────────────────────────────────────────────────

    @staticmethod
    def _print_list(emails: list[dict], heading: str):
        print(f"\n{'─'*62}")
        print(f"  🔍 {heading}  ({len(emails)} found)")
        print(f"{'─'*62}")
        if not emails:
            print("  No emails found.")
        else:
            for i, e in enumerate(emails, 1):
                date = e.get("date", "")[:16]
                sender = e.get("sender", "")[:28]
                subject = e.get("subject", "")[:40]
                cat = e.get("category", "")
                print(f"  {i:>3}. [{cat:<11}] {date}  {sender:<30} {subject}")
        print(f"{'─'*62}\n")

    @staticmethod
    def print_detail(email: dict):
        print("\n" + "=" * 62)
        print(f"  Subject  : {email.get('subject', '')}")
        print(f"  From     : {email.get('sender', '')}")
        print(f"  Date     : {email.get('date', '')}")
        print(f"  Category : {email.get('category', '')}")
        print("─" * 62)
        body = email.get("body") or "(no body)"
        print(body[:1500])
        print("=" * 62 + "\n")
