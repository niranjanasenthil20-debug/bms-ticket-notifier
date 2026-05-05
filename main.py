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
                    seats_avail = sa.get("seatsAvailable", "")
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
                            "seats": str(cat.get("seatsAvailable", seats_avail or "N/A")),
                        })
    return shows


def filter_shows(shows):
    theatre = CONFIG["theatre"]
    dates = CONFIG["dates"]

    kws = [k.strip().lower() for k in theatre.split(",") if k.strip()] if theatre else []
    dates_set = set(d.strip() for d in dates.split(",") if d.strip()) if dates else set()

    result = []
    for s in shows:
        if kws and not any(k in s["venue"].lower() for k in kws):
            continue
        if dates_set and s["date"] and s["date"] not in dates_set:
            continue
        result.append(s)
    return result


def format_date(date_code):
    try:
        dt = datetime.strptime(str(date_code), "%Y%m%d")
        return dt.strftime("%d %b %Y (%A)")
    except:
        return date_code


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
            "seats": s["seats"],
        }
    return state


def detect_changes(old_state, new_state):
    changes = []
    for key in set(new_state) - set(old_state):
        s = new_state[key]
        lbl, ico = AVAIL_STATUS_MAP.get(s["status"], ("AVAILABLE", "🟢"))
        changes.append(
            f"{ico} NEW: {s['venue']} | {s['time']} | {format_date(s['date'])} | "
            f"{s['cat']} ₹{s['price']} | Seats: {s['seats']} | {lbl}"
        )
    for key, new_s in new_state.items():
        old_s = old_state.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(new_s["status"], ("AVAILABLE", "🟢"))
            changes.append(
                f"{ico} SEATS OPEN: {new_s['venue']} | {new_s['time']} | "
                f"{format_date(new_s['date'])} | {new_s['cat']} ₹{new_s['price']} | "
                f"Seats: {new_s['seats']} | {lbl}"
            )
        # Seat count changed
        if old_s and old_s.get("seats") != new_s.get("seats") and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(new_s["status"], ("AVAILABLE", "🟢"))
            changes.append(
                f"🔄 SEATS UPDATED: {new_s['venue']} | {new_s['time']} | "
                f"{format_date(new_s['date'])} | {new_s['cat']} ₹{new_s['price']} | "
                f"Seats: {old_s.get('seats', '?')} → {new_s['seats']} | {lbl}"
            )
    return changes


def send_email(subject, changes, shows, movie_name):
    sender = GMAIL_USER.strip()
    password = GMAIL_APP_PASSWORD.strip()
    raw = RESEND_TO_EMAIL.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    emails = [e for e in raw.split(",") if e]

    if not sender or not password or not emails:
        print(f"  ⚠️  Skipping email — sender:{bool(sender)} password:{bool(password)} emails:{bool(emails)}")
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

    body = f"🎬 Movie: {movie_name}\n"
    body += f"📅 Checked at: {now_str}\n\n"

    if changes:
        body += "═" * 50 + "\n"
        body += "⚡ CHANGES DETECTED:\n"
        body += "═" * 50 + "\n"
        for c in changes:
            body += f"  {c}\n"
        body += "\n"

    body += "═" * 50 + "\n"
    body += "🎭 ALL CURRENT SHOWTIMES:\n"
    body += "═" * 50 + "\n"

    # Group by date then venue
    date_groups = {}
    for s in shows:
        date_groups.setdefault(s["date"], {}).setdefault(s["venue"], []).append(s)

    for date_code in sorted(date_groups.keys()):
        body += f"\n📅 {format_date(date_code)}\n"
        body += "-" * 40 + "\n"
        for vname, vshows in date_groups[date_code].items():
            body += f"\n  🎦 {vname}\n"
            for s in vshows:
                lbl, ico = AVAIL_STATUS_MAP.get(s["status"], ("UNKNOWN", "⚪"))
                fmt = f" [{s['screen']}]" if s["screen"] else ""
                seats_info = f" | Seats: {s['seats']}" if s["seats"] != "N/A" else ""
                body += f"    {ico} {s['time']}{fmt} — {s['cat']} ₹{s['price']}{seats_info} ({lbl})\n"

    body += "\n" + "═" * 50 + "\n"
    body += "This is an automated alert from BMS Ticket Notifier.\n"

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

    urls = [u.strip() for u in CONFIG["url"].split(",") if u.strip()]

    all_changes = []
    all_filtered = []
    movie_names = []

    for url in urls:
        movie_name = extract_movie_name(url)
        movie_names.append(movie_name)

        parsed = parse_bms_url(url)
        event_code = parsed["event_code"]
        region_slug = parsed["region_slug"]
        url_date = parsed.get("date_code", "")

        if not event_code or not region_slug:
            print(f"  ❌ Invalid URL for {movie_name}, skipping.")
            continue

        region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)

        raw_dates = CONFIG["dates"].strip()
        if raw_dates:
            date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
        elif url_date:
            date_list = [url_date]
        else:
            date_list = [""]

        print(f"\n  🎬 Movie: {movie_name}")
        print(f"  Event: {event_code}  Region: {region_code}  Dates: {date_list}")

        all_shows = []
        for dc in date_list:
            data = fetch_bms(event_code, dc, region_code, region_slug_r, lat, lon, geohash)
            if not data:
                print(f"  ⚠️  No data for date {dc or '(default)'}")
                continue
            all_shows.extend(parse_shows(data))

        if not all_shows:
            print(f"  ❌ No showtimes found for {movie_name}.")
            continue

        filtered = filter_shows(all_shows)
        print(f"  📊 {len(filtered)} showtime(s) after filters")

        state_key = f"bms_state_{event_code}.json"
        old_state = {}
        try:
            with open(state_key) as f:
                old_state = json.load(f)
        except:
            pass

        new_state = build_state(filtered)

        if old_state:
            changes = detect_changes(old_state, new_state)
        else:
            changes = [f"🧪 Test alert — notifications working for {movie_name}!"]

        with open(state_key, "w") as f:
            json.dump(new_state, f, indent=2)

        if changes:
            print(f"  ⚡ {len(changes)} change(s) detected for {movie_name}")
            for c in changes:
                all_changes.append(f"[{movie_name}] {c}")
        else:
            print(f"  ✅ No changes for {movie_name}.")

        all_filtered.extend(filtered)

    if all_changes:
        movies_str = " & ".join(movie_names)
        send_email(
            f"🎟 BMS Alert: {movies_str} — {len(all_changes)} update(s)",
            all_changes, all_filtered, movies_str
        )

    print("\n  Done.")


if __name__ == "__main__":
    main()
