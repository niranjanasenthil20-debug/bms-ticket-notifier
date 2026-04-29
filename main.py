"""
BMS Ticket Checker — CI/Headless mode for GitHub Actions.
Runs once, checks all configured watches, emails on changes.
State is persisted via a JSON artifact.

Configure via environment variables or edit the CONFIG below.
"""
# (same imports as before)
import os, re, sys, json, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests

# CONFIG
CONFIG = {
    "url": os.getenv("BMS_URL", ""),
    "dates": os.getenv("BMS_DATES", ""),
    "theatre": os.getenv("BMS_THEATRE", ""),
    "time_period": os.getenv("BMS_TIME", ""),
}

GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
STATE_FILE = "bms_state.json"

AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT", "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE", "🟢"),
}

# ─────────────────────────────────────────

def parse_bms_url(url):
    parts = urlparse(url).path.split("/")
    event_code = next((p for p in parts if p.startswith("ET")), "")
    region = parts[2] if len(parts) > 2 else ""
    date = next((p for p in parts if p.isdigit() and len(p) == 8), "")
    return event_code, region, date

# ─────────────────────────────────────────

API_URL = "https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic"

def fetch(event, date):
    params = {"eventCode": event, "dateCode": date}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(API_URL, params=params, headers=headers, timeout=10)
    return r.json() if r.status_code == 200 else None

# ─────────────────────────────────────────

def parse_shows(data):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        for g in w.get("data", []):
            for card in g.get("data", []):
                vname = card.get("additionalData", {}).get("venueName", "")
                for st in card.get("showtimes", []):
                    time = st.get("title", "")
                    for cat in st.get("additionalData", {}).get("categories", []):
                        shows.append({
                            "venue": vname,
                            "time": time,
                            "cat": cat.get("priceDesc", ""),
                            "price": cat.get("curPrice", ""),
                            "status": cat.get("availStatus", ""),
                        })
    return shows

# ─────────────────────────────────────────

def load_state():
    try:
        return json.load(open(STATE_FILE))
    except:
        return {}

def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))

# ─────────────────────────────────────────

def detect_changes(old, new):
    changes = []
    for k, v in new.items():
        if k not in old or old[k] == "0" and v != "0":
            changes.append(k)
    return changes

# ─────────────────────────────────────────

def send_email(subject, body):
    sender = GMAIL_USER.strip()
    password = GMAIL_APP_PASSWORD.strip()
    raw = RESEND_TO_EMAIL.strip()

    # Clean emails
    raw = raw.replace("\n", "").replace("\r", "").replace(" ", "")
    emails = [e for e in raw.split(",") if e]

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = sender
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            for e in emails:
                msg["To"] = e
                server.sendmail(sender, e, msg.as_string())
                print(f"✅ Sent to {e}")
    except Exception as e:
        print("❌ Email failed:", e)

# ─────────────────────────────────────────

def main():
    event, region, date = parse_bms_url(CONFIG["url"])
    data = fetch(event, date)

    if not data:
        print("❌ No data")
        return

    movie_name = data.get("data", {}).get("eventName", event)

    shows = parse_shows(data)
    state = {
        f"{s['venue']}|{s['time']}|{s['cat']}": s["status"]
        for s in shows
    }

    old = load_state()
    changes = detect_changes(old, state)
    save_state(state)

    if changes:
        subject = f"🎟 {movie_name} Tickets Update ({len(changes)})"
        body = f"🎬 Movie: {movie_name}\n\nChanges detected:\n"
        for c in changes:
            body += f"• {c}\n"
        send_email(subject, body)
    else:
        print("No changes")

# ─────────────────────────────────────────

if __name__ == "__main__":
    main()
