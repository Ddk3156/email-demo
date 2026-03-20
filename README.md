# 🤖 AI Email Recognizer

Logs into your Gmail via IMAP, fetches your inbox, and uses Google Gemini AI to categorize every email.

**No Google Cloud. No OAuth. No .env file. Just run it.**

---

## Setup (2 steps only)

### Step 1 — Get a free Gemini API key
👉 https://aistudio.google.com/app/apikey  
Click **Get API key** → Copy it → Paste it into `main.py`:
```python
GEMINI_API_KEY = "AIzaSy..."
```

### Step 2 — Create a Gmail App Password
Your normal Gmail password won't work for IMAP — Google requires an App Password.

1. Go to 👉 https://myaccount.google.com/apppasswords  
   *(You need 2-Step Verification enabled on your Google account)*
2. Select app: **Mail** / device: **Windows Computer** → **Generate**
3. Copy the 16-character password (e.g. `abcd efgh ijkl mnop`)

---

## Run

```bash
pip install -r requirements.txt
python main.py
```

Enter your Gmail + App Password when prompted. That's it.

---

## CLI Commands

```
show summary              — Category counts
show category Work        — List Work emails
show category Finance     — List Finance emails
show sender amazon        — Emails from senders containing "amazon"
search invoice            — Search subject + body for "invoice"
detail 3                  — Full body of email #3 from last list
refresh                   — Re-fetch from Gmail
help / quit
```

---

## Categories

Gemini classifies each email as one of:
`Work` | `Personal` | `Finance` | `Promotions` | `Spam` | `Important`

---

## Files

```
email_ai/
├── main.py                ← Run this. Paste API key here.
├── gmail_service.py       ← IMAP login + email fetching
├── gemini_classifier.py   ← Gemini AI categorization
├── email_processor.py     ← Storage, search, summary
├── requirements.txt       ← Only 1 package to install
└── emails_cache.json      ← Auto-created after first run
```
