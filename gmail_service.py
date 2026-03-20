"""
gmail_service.py
-----------------
Connects to Gmail using IMAP and fetches/parses emails.
Uses Python's built-in imaplib — no external packages needed.

FIXES + ADDITIONS:
  1. Body no longer hard-capped at 2000 chars
  2. HTML fallback: strips HTML to plain text when no text/plain exists
  3. HTML entities decoded (fixes &#8199; spam character bleed)
  4. fetch_emails shows total inbox count
  5. Media extraction: inline images (CID), linked images (URLs), attachments
"""

import imaplib
import logging
import re
import base64
import urllib.request
from email import message_from_bytes
from email.header import decode_header as _decode_header

logger = logging.getLogger(__name__)

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT   = 993

MAX_INLINE_IMAGE_BYTES = 3 * 1024 * 1024   # 3 MB
MAX_LINKED_IMAGE_BYTES = 2 * 1024 * 1024   # 2 MB


# ─────────────────────────────────────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────────────────────────────────────

def connect_gmail(email_address: str, app_password: str) -> imaplib.IMAP4_SSL:
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(email_address, app_password)
        print(f"✅ Logged in as {email_address}")
        return mail
    except imaplib.IMAP4.error as e:
        raise ConnectionError(
            f"\n❌ Gmail login failed: {e}"
            f"\n👉 Use an App Password, not your regular Gmail password."
            f"\n   Generate one at: https://myaccount.google.com/apppasswords"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fetching
# ─────────────────────────────────────────────────────────────────────────────

def fetch_emails(mail: imaplib.IMAP4_SSL, max_emails: int = 30) -> list[dict]:
    mail.select("inbox")
    status, data = mail.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        print("⚠️  No messages found in inbox.")
        return []

    all_ids        = data[0].split()
    total_in_inbox = len(all_ids)
    selected_ids   = list(reversed(all_ids[-max_emails:]))

    print(f"📥 Inbox has {total_in_inbox} emails total — fetching latest {len(selected_ids)}...")

    emails = []
    for i, eid in enumerate(selected_ids):
        try:
            parsed = _parse_email(mail, eid)
            if parsed:
                emails.append(parsed)
        except Exception as e:
            logger.warning("Failed to parse email ID %s: %s", eid, e)

        if (i + 1) % 5 == 0 or (i + 1) == len(selected_ids):
            print(f"  📩 {i + 1}/{len(selected_ids)} parsed...", end="\r")

    print(f"\n✅ Successfully parsed {len(emails)} emails.\n")
    return emails


def fetch_all_folders(mail: imaplib.IMAP4_SSL, max_per_folder: int = 50) -> list[dict]:
    folders    = ["inbox", '"[Gmail]/Sent Mail"', '"[Gmail]/Starred"', '"[Gmail]/All Mail"']
    all_emails = []
    seen_ids   = set()

    for folder in folders:
        try:
            status, _ = mail.select(folder)
            if status != "OK":
                continue
            _, data = mail.search(None, "ALL")
            if not data or not data[0]:
                continue
            folder_ids = list(reversed(data[0].split()[-max_per_folder:]))
            print(f"  📁 {folder}: {len(folder_ids)} emails")
            for eid in folder_ids:
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                try:
                    parsed = _parse_email(mail, eid)
                    if parsed:
                        parsed["folder"] = folder.strip('"')
                        all_emails.append(parsed)
                except Exception as e:
                    logger.warning("Failed email %s in %s: %s", eid, folder, e)
        except Exception as e:
            logger.warning("Could not access folder %s: %s", folder, e)

    return all_emails


# ─────────────────────────────────────────────────────────────────────────────
# Parsing a single email
# ─────────────────────────────────────────────────────────────────────────────

def _parse_email(mail: imaplib.IMAP4_SSL, email_id: bytes) -> dict | None:
    status, msg_data = mail.fetch(email_id, "(RFC822)")
    if status != "OK" or not msg_data or msg_data[0] is None:
        return None

    raw_bytes = msg_data[0][1]
    if not isinstance(raw_bytes, bytes):
        return None

    msg = message_from_bytes(raw_bytes)
    body, inline_images, linked_image_urls = _get_body_and_images(msg)
    attachments = _get_attachments(msg)

    return {
        "id":              email_id.decode(),
        "sender":          _safe_decode_header(msg.get("From", "Unknown")),
        "subject":         _safe_decode_header(msg.get("Subject", "(No Subject)")),
        "date":            msg.get("Date", "Unknown"),
        "body":            body,
        "category":        None,
        # ── Media ───────────────────────────────────────────────
        "attachments":     attachments,          # [{filename, mime_type, size_str, data_b64}]
        "inline_images":   inline_images,        # [{cid, mime_type, data_b64, data_uri}]
        "linked_images":   linked_image_urls,    # [url, url, ...]  — fetched on demand
        "has_attachments": len(attachments) > 0,
        "has_images":      len(inline_images) > 0 or len(linked_image_urls) > 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Body + Inline/Linked image extraction
# ─────────────────────────────────────────────────────────────────────────────

def _get_body_and_images(msg) -> tuple[str, list, list]:
    text_plain    = ""
    text_html     = ""
    inline_images = []
    cid_map       = {}

    if msg.is_multipart():
        for part in msg.walk():
            ctype       = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            content_id  = part.get("Content-ID", "").strip("<>")

            if "attachment" in disposition:
                continue

            if ctype == "text/plain" and not text_plain:
                text_plain = _decode_part(part)

            elif ctype == "text/html" and not text_html:
                text_html = _decode_part(part)

            elif ctype.startswith("image/") and content_id:
                payload = part.get_payload(decode=True)
                if payload and len(payload) <= MAX_INLINE_IMAGE_BYTES:
                    b64      = base64.b64encode(payload).decode()
                    data_uri = f"data:{ctype};base64,{b64}"
                    cid_map[content_id] = data_uri
                    inline_images.append({
                        "cid":       content_id,
                        "mime_type": ctype,
                        "data_b64":  b64,
                        "data_uri":  data_uri,
                        "size":      len(payload),
                        "size_str":  _format_size(len(payload)),
                    })
    else:
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            text_plain = _decode_part(msg)
        elif ctype == "text/html":
            text_html = _decode_part(msg)

    # Swap CID references in HTML with actual base64 data URIs
    if text_html and cid_map:
        for cid, data_uri in cid_map.items():
            text_html = text_html.replace(f"cid:{cid}", data_uri)

    # Extract linked image URLs from HTML (fetched on demand via /api/email/<id>/images)
    linked_image_urls = _extract_image_urls(text_html) if text_html else []

    # Decide body text
    if text_plain.strip():
        body = text_plain.strip()
    elif text_html.strip():
        body = _strip_html(text_html).strip()
    else:
        body = ""

    return body, inline_images, linked_image_urls


def _extract_image_urls(html: str) -> list[str]:
    """Extract <img src> URLs — skip data URIs, CID refs, and tracker pixels."""
    urls = []
    for match in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
        url = match.group(1)
        if url.startswith("data:") or url.startswith("cid:"):
            continue
        if url not in urls:
            urls.append(url)
    return urls[:10]   # cap at 10 per email


def fetch_linked_images(urls: list[str]) -> list[dict]:
    """
    Fetch linked images from URLs on demand.
    Called by /api/email/<id>/images endpoint — not during initial parse.
    """
    results = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                data = resp.read(MAX_LINKED_IMAGE_BYTES)
                if len(data) < 100:    # skip tracker pixels
                    continue
                b64 = base64.b64encode(data).decode()
                results.append({
                    "url":       url,
                    "mime_type": content_type,
                    "data_b64":  b64,
                    "data_uri":  f"data:{content_type};base64,{b64}",
                    "size":      len(data),
                    "size_str":  _format_size(len(data)),
                })
        except Exception as e:
            logger.debug("Could not fetch image %s: %s", url[:60], e)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Attachment extraction
# ─────────────────────────────────────────────────────────────────────────────

def _get_attachments(msg) -> list[dict]:
    attachments = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" not in disposition:
            continue

        filename = part.get_filename()
        if not filename:
            continue

        filename = _safe_decode_header(filename)
        payload  = part.get_payload(decode=True)
        if not payload:
            continue

        mime_type = part.get_content_type() or "application/octet-stream"
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        attachments.append({
            "filename":  filename,
            "mime_type": mime_type,
            "extension": extension,
            "size":      len(payload),
            "size_str":  _format_size(len(payload)),
            "data_b64":  base64.b64encode(payload).decode(),
        })

    return attachments


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_decode_header(value: str) -> str:
    try:
        parts = _decode_header(value)
        result = ""
        for part, charset in parts:
            if isinstance(part, bytes):
                result += part.decode(charset or "utf-8", errors="replace")
            else:
                result += str(part)
        return result.strip()
    except Exception:
        return str(value)


def _decode_part(part) -> str:
    try:
        charset = part.get_content_charset() or "utf-8"
        payload = part.get_payload(decode=True)
        if payload:
            return payload.decode(charset, errors="replace")
    except Exception:
        pass
    return ""


def _format_size(n: int) -> str:
    if n < 1024:       return f"{n} B"
    if n < 1024 ** 2:  return f"{n/1024:.1f} KB"
    return f"{n/1024**2:.1f} MB"


def _strip_html(html: str) -> str:
    import html as html_module
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", "", html)
    html = html_module.unescape(html)
    # Strip invisible spam characters
    html = re.sub(r"[\u00ad\u034f\u200b\u200c\u200d\u2007\u2060\ufeff\xa0]+", " ", html)
    # Collapse ALL multiple newlines/blank lines into a single newline
    # (email HTML has dozens of empty spacer divs that produce giant gaps)
    html = re.sub(r"\n[\s]*\n+", "\n", html)
    html = re.sub(r"[ \t]+", " ", html)
    # Clean up lines that are just whitespace
    lines = [line.strip() for line in html.splitlines()]
    lines = [l for l in lines if l]   # remove empty lines entirely
    return "\n".join(lines)
