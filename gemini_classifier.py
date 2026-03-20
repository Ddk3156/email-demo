"""
gemini_classifier.py
---------------------
Sends each email to Google Gemini API for categorization.
Gemini freely decides the best category based on email content.
No predefined categories — fully AI-driven.
"""

import time
import logging
import re
import google.generativeai as genai

logger = logging.getLogger(__name__)

DEFAULT_CATEGORY = "General"


class GeminiClassifier:
    """Categorizes emails using Gemini AI — categories decided freely by the model."""

    def __init__(self, api_key: str):
        if not api_key or api_key == "PASTE_YOUR_GEMINI_API_KEY_HERE":
            raise ValueError(
                "❌ Gemini API key not set!\n"
                "   Open main.py and paste your key on line 22.\n"
                "   Get a free key at: https://aistudio.google.com/app/apikey"
            )
        genai.configure(api_key=api_key)

        self.model = None
        self.model_name = None

        print("🔍 Detecting available Gemini models...")
        available = self._list_models()

        preferred = [
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-flash-latest",
            "gemini-1.5-pro",
            "gemini-1.5-pro-latest",
            "gemini-2.5-pro-exp-03-25",
        ]

        candidates = preferred + [m for m in available if m not in preferred]

        for name in candidates:
            try:
                m = genai.GenerativeModel(name)
                m.generate_content("Say OK")
                self.model = m
                self.model_name = name
                print(f"✅ Using Gemini model: {name}\n")
                break
            except Exception as e:
                logger.debug("Model '%s' failed: %s", name, e)

        if not self.model:
            print(f"\nAvailable models: {available}")
            raise RuntimeError(
                "❌ No working Gemini model found.\n"
                "   Check your API key at: https://aistudio.google.com/app/apikey"
            )

    def _list_models(self) -> list:
        try:
            models = genai.list_models()
            names = [
                m.name.replace("models/", "")
                for m in models
                if "generateContent" in m.supported_generation_methods
            ]
            if names:
                print(f"  Found {len(names)} models: {', '.join(names[:5])}{'...' if len(names) > 5 else ''}")
            return names
        except Exception as e:
            logger.warning("Could not list models: %s", e)
            return []

    def categorize(self, email: dict) -> str:
        prompt = self._build_prompt(email)
        for attempt in range(3):  # retry up to 3 times
            try:
                response = self.model.generate_content(prompt)
                raw = response.text.strip()
                return self._parse(raw)
            except Exception as e:
                if "429" in str(e):  # rate limit hit
                    wait = 10 * (attempt + 1)
                    print(f"\n  ⏳ Rate limit hit, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    logger.warning("Gemini error for '%s': %s", email.get("subject", "")[:40], e)
                    return DEFAULT_CATEGORY
        return DEFAULT_CATEGORY

    def categorize_all(self, emails: list, delay: float = 0.5) -> list:
        """Categorize all emails, printing each result as it comes."""
        total = len(emails)
        print(f"🤖 Classifying {total} emails with Gemini ({self.model_name})...\n")
        for i, email in enumerate(emails):
            email["category"] = self.categorize(email)
            cat  = email["category"]
            subj = email.get("subject", "")[:55]
            print(f"  [{i+1:>3}/{total}] {cat:<18} — {subj}")
            if i < total - 1:
                time.sleep(delay)
        print()
        return emails

    def _build_prompt(self, email: dict) -> str:
        sender  = email.get("sender", "")
        subject = email.get("subject", "")
        body    = email.get("body", "")[:800]

        return f"""You are an intelligent email classifier.

Read the email below and assign it the most accurate category label.
You choose the category freely — pick whatever single word or short phrase (max 3 words) best describes this email.

Examples of good categories: Work, Finance, Shopping, Travel, Social, Newsletter, Security, Family, Health, Education, Legal, Government, Spam, Promotions, Events, Subscriptions, Receipts

Rules:
- Reply with ONLY the category label — nothing else, no explanation
- Maximum 3 words
- Be specific and meaningful (e.g. "Flight Booking" is better than "Personal")
- Capitalize each word

EMAIL:
From: {sender}
Subject: {subject}
Body: {body}

Category:"""

    def _parse(self, raw: str) -> str:
        """Clean up Gemini's response into a usable category label."""
        # Take only the first line in case Gemini adds extra text
        first_line = raw.strip().splitlines()[0].strip()

        # Remove any punctuation at the end
        first_line = first_line.strip(".,!?:-")

        # Limit to 3 words max
        words = first_line.split()[:3]
        category = " ".join(w.capitalize() for w in words)

        return category if category else DEFAULT_CATEGORY