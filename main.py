import os, re, sys, json, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from urllib.parse import urlparse
import requests

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
    "0": ("SOLD OUT",    "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST","🟠"),
    "3": ("AVAILABLE",   "🟢"),
}

TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}

REGION_MAP = {
    "chennai":   ("CHEN",   "chennai",   "13.056", "80.206", "tf3"),
    "mumbai":    ("MUMBAI", "mumbai",    "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR",    "delhi-ncr", "28.613", "77.209", "ttn"),
    "delhi":     ("NCR",    "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG",   "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG",   "bengaluru", "12.972", "77.594", "tdr"),
    "hyderabad": ("HYD",    "hyderabad", "17.385", "78.487", "tep"),
    "kolkata":   ("KOLK",   "kolkata",   "22.573", "88.364", "tun"),
    "pune":      ("PUNE",   "pune",      "18.520", "73.856", "te2"),
    "kochi":     ("KOCH",   "kochi",     "9.932",  "76.267", "t9z"),
}

API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


def extract_movie_name(url):
    try:
        url_parts = url.strip("/").split("/")
        movie_idx = url_parts.index("movies")
        return url_parts[movie_idx + 2].replace("-", " ").title()
    except:
        return "Unknown Movie"


def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")


def fetch_bms(event_code, date_code, region_code, region_slug, lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://in.bookmyshow.com/movies/{region_slug}/buytickets/{event_code}/",
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "", "lsId": "", "subCode": "",
        "lat": lat, "lon": lon,
    }
    try:
        resp = requests.get(API_URL, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"  HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"  Request failed: {e}")
    return None


def parse_shows(data):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")
                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(sa.get("showDateCode", "") or sa.get("dateCode", "")).strip()
                    time = st.get("title", "")
                    time_code = sa.get("showTimeCode", "")
                    screen_attr = st.get("screenAttr", "") or sa.get("attributes", "")
                    for cat in sa.get("categories", []):
                        ca = str(cat.get("availStatus", ""))
                        shows.append({
                            "venue_code": vcode,
                            "venue": vname,
                            "date": date_code,
                            "time": time,
                            "time_code": time_code,
                            "screen": screen_attr,
                            "cat": cat.get("priceDesc", ""),
                            "price": cat.get("curPrice", "0"),
                            "status": ca,
                        })
    return shows


def filter_shows(shows):
    theatre = CONFIG["theatre"]
    time_period = CONFIG["time_period"]
    dates = CONFIG["dates"]

    kws = [k.strip().lower() for k in theatre.split(",") if k.strip()] if theatre else []
    periods = [p.strip().lower() for p in time_period.split(",") if p.strip()] if time_period else []
    dates_set = set(d.strip() for d in dates.split(",") if d.strip()) if dates else set()

    result = []
    for s in shows:
        if kws and not any(k in s["venue"].lower() for k in kws):
            continue
        if dates_set and s["date"] and s["date"] not in dates_set:
            continue
        if periods:
            try:
                tc = int(s["time_code"])
            except:
                tc = 0
            if not any(TIME_PERIODS[p][0] <= tc < TIME_PERIODS[p][1] for p in periods if p in TIME_PERIODS):
                continue
        result.append(s)
    return result


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def build_state(shows):
    state = {}
    for s in shows:
        key = f"{s['venue_code']}|{s['date']}|{s['time']}|{s['cat']}"
        state[key] = {
            "venue": s["venue"],
            "time": s["time"],
            "date": s["date"],
            "cat": s["cat"],
            "price": s["price"],
            "status": s["status"],
        }
    return state


def detect_changes(old_state, new_state):
    changes = []
    for key in set(new_state) - set(old_state):
        s = new_state[key]
        changes.append(
            f"🆕 NEW: {s['venue']} {s['time']} [{s['date']}] — {s['cat']} ₹{s['price']}"
        )
    for key, new_s in new_state.items():
        old_s = old_state.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(new_s["status"], ("AVAILABLE", "🟢"))
            changes.append(
                f"{ico} SEATS OPEN: {new_s['venue']} {new_s['time']} [{new_s['date']}] — {new_s['cat']} → {lbl}"
            )
    return changes


def send_email(subject, changes, shows, movie_name):
    sender = GMAIL_USER.strip()
    password = GMAIL_APP_PASSWORD.strip()
    raw = RESEND_TO_EMAIL.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    emails = [e for e in raw.split(",") if e]

    if not sender or not password or not emails:
        print("  ⚠️  Skipping email — credentials not set.")
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

    body = f"🎬 Movie: {movie_name}\n"
    body += f"Checked at: {now_str}\n\n"

    if changes:
        body += "Changes Detected:\n"
        for c in changes:
            body += f"  {c}\n"
        body += "\n"

    body += "Current Showtimes:\n"
    venue_groups = {}
    for s in shows:
        venue_groups.setdefault(s["venue"], []).append(s)
    for vname, vshows in venue_groups.items():
        body += f"\n{vname}\n"
        for s in vshows:
            lbl = AVAIL_STATUS_MAP.get(s["status"], ("UNKNOWN", ""))[0]
            fmt = f" [{s['screen']}]" if s["screen"] else ""
            body += f"  {s['time']}{fmt} — {s['cat']} ₹{s['price']} ({lbl})\n"

    body += "\nThis is an automated alert from BMS Ticket Notifier."

    for email in emails:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = email
        msg.attach(MIMEText(body, "plain"))
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(sender, password)
                server.sendmail(sender, email, msg.as_string())
            print(f"  ✅ Email sent to {email}")
        except Exception as e:
            print(f"  ❌ Email failed for {email}: {e}")


def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] BMS Ticket Checker — CI mode")

    url = CONFIG["url"]
    movie_name = extract_movie_name(url)

    parsed = parse_bms_url(url)
    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code", "")

    if not event_code or not region_slug:
        print("  ❌ Invalid BMS_URL. Could not extract event/region.")
        sys.exit(1)

    region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)

    raw_dates = CONFIG["dates"].strip()
    if raw_dates:
        date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
    elif url_date:
        date_list = [url_date]
    else:
        date_list = [""]

    print(f"  Movie: {movie_name}")
    print(f"  Event: {event_code}  Region: {region_code}  Dates: {date_list}")

    all_shows = []
    for dc in date_list:
        data = fetch_bms(event_code, dc, region_code, region_slug_r, lat, lon, geohash)
        if not data:
            print(f"  ⚠️  No data for date {dc or '(default)'}")
            continue
        all_shows.extend(parse_shows(data))

    if not all_shows:
        print("  ❌ No showtimes found.")
        sys.exit(0)

    filtered = filter_shows(all_shows)
    print(f"  📊 {len(filtered)} showtime(s) after filters")

    new_state = build_state(filtered)
    old_state = load_state()

    if old_state:
        changes = detect_changes(old_state, new_state)
    else:
        changes = [f"🧪 Test alert — notifications are working for {movie_name}!"]

    save_state(new_state)

    if changes:
        print(f"  ⚡ {len(changes)} change(s) detected")
        send_email(
            f"🎟 BMS Alert: {movie_name} — {len(changes)} update(s)",
            changes, filtered, movie_name
        )
    else:
        print("  ✅ No changes since last run.")

    print(f"\n  Current status ({len(filtered)} shows):")
    for s in filtered:
        lbl = AVAIL_STATUS_MAP.get(s["status"], ("UNKNOWN", ""))[0]
        print(f"    {s['venue']} — {s['time']} [{s['date']}] — {s['cat']} ₹{s['price']} ({lbl})")

    print("\n  Done.")


if __name__ == "__main__":
    main()
