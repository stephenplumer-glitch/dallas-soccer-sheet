import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import os
import time
import requests
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────
SPREADSHEET_ID = "1Iu_A3hs0WbsZ9HELl0von2ihNfiKeEM2zd1OtlLxZrc"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Cosm Dallas API config (IDs extracted from cosm.com Next.js data)
COSM_DALLAS_LOCATION_ID = "%24oid%3A659db49056ef4642c65d9196"
COSM_SOCCER_CATEGORY_ID = "%24oid%3A65fc7411ee01793ee2d67b59"
COSM_API_URL = f"https://api.cosm.io/event/{COSM_DALLAS_LOCATION_ID}/events?categoryIds={COSM_SOCCER_CATEGORY_ID}&limit=20&sort=startTime"

PL_PROMPT = """Search the web for upcoming Premier League matches in the next 3 weeks.
Return ONLY matches where kick-off is between 6:30 AM CT and 9:00 AM CT (Central Time / Dallas time), and only matches that haven't happened yet (today is {today}).
IMPORTANT: Always convert times to CT (Dallas time). ET minus 1 hour = CT. PT plus 2 hours = CT. UK/BST minus 6 hours = CT.
Examples: 7:30 AM ET = 6:30 AM CT (include). 10:00 AM ET = 9:00 AM CT (include). 12:30 PM BST = 6:30 AM CT (include). 3:00 PM BST = 9:00 AM CT (include). 7:30 AM BST = 1:30 AM CT (exclude).
Return each match on a new line in this EXACT comma-separated format with no headers, no extra text, no blank lines:
Premier League,Home,Away,Day,Date,KO Time,OPEN,Notes

- Date format: DD-Mon (e.g. 15-Apr)
- Times in CT (Dallas time) using AM/PM (e.g. 7:30 AM)
- OPEN: 15 minutes before KO Time (e.g. if KO is 7:30 AM, OPEN is 7:15 AM)
- Notes: leave blank
- If no matches in that time window are found, return the single word: NONE"""


def get_cosm_fixtures():
    """Fetch soccer fixtures directly from Cosm's internal API."""
    rows = []
    try:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0"
        }
        resp = requests.get(COSM_API_URL, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        now = datetime.now(timezone.utc)
        events = data if isinstance(data, list) else data.get("events", data.get("data", []))

        for event in events:
            # Extract start time
            start_raw = event.get("startTime") or event.get("start_time") or event.get("startDate", "")
            if not start_raw:
                continue

            # Parse ISO datetime and convert to CT
            try:
                from datetime import timedelta
                dt_utc = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                if dt_utc < now:
                    continue  # Skip past events
                # Convert to CT (UTC-5 standard, UTC-6 CDT — use UTC-5 as safe default)
                dt_ct = dt_utc - timedelta(hours=5)
            except Exception:
                continue

            # Extract event details
            title = event.get("title") or event.get("name") or ""
            category = event.get("category") or event.get("program") or ""

            # Try to determine competition
            comp = "Soccer"
            title_lower = title.lower()
            if "champions league" in title_lower or "ucl" in title_lower:
                comp = "Champions League"
            elif "premier league" in title_lower or "epl" in title_lower:
                comp = "Premier League"

            # Try to extract home/away from title (e.g. "Arsenal vs Chelsea")
            home, away = "", ""
            if " vs " in title:
                parts = title.split(" vs ", 1)
                home = parts[0].strip()
                away = parts[1].strip()
            elif " v " in title:
                parts = title.split(" v ", 1)
                home = parts[0].strip()
                away = parts[1].strip()
            else:
                home = title
                away = ""

            day = dt_ct.strftime("%A")
            date = dt_ct.strftime("%d-%b")
            ko_time = dt_ct.strftime("%I:%M %p").lstrip("0")

            # Finish time: 2hrs for PL, 2.5hrs for CL
            from datetime import timedelta
            finish_delta = timedelta(hours=2.5) if comp == "Champions League" else timedelta(hours=2)
            finish_ct = dt_ct + finish_delta
            finish_time = finish_ct.strftime("%I:%M %p").lstrip("0")

            notes = ""
            if comp == "Champions League":
                notes = "Could run longer with extra time"

            rows.append([comp, home, away, day, date, ko_time, finish_time, notes])

    except Exception as e:
        print(f"  Cosm API error: {e}")

    return rows


def get_pl_fixtures(client):
    """Call Claude with web search and return PL early KO rows."""
    today = datetime.now().strftime("%d-%b-%Y")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": PL_PROMPT.format(today=today)}],
    )

    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text.strip()

    if not text or text == "NONE":
        return []

    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line == "NONE":
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 7:
            if parts[0].lower() in ("competition", "comp"):
                continue
            rows.append(parts)

    return rows


def append_to_sheet(sheet, rows):
    """Append rows to a Google Sheet tab."""
    if not rows:
        print(f"  No new fixtures to add to '{sheet.title}'")
        return

    existing = sheet.get_all_values()
    next_row = len(existing) + 1

    for row in rows:
        padded = row[:8] + [""] * (8 - len(row[:8]))
        sheet.insert_row([""] + padded, next_row)
        next_row += 1
        print(f"  Added: {' vs '.join(row[1:3])} on {row[4]}")


def main():
    # ── Google Sheets auth ───────────────────────────────────────────────────
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)

    # ── Anthropic client ─────────────────────────────────────────────────────
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    client = anthropic.Anthropic(api_key=anthropic_key)

    # ── Cosm tab (direct API) ────────────────────────────────────────────────
    print("Fetching Cosm Dallas fixtures via API...")
    cosm_rows = get_cosm_fixtures()
    cosm_sheet = spreadsheet.worksheet("Cosm")
    append_to_sheet(cosm_sheet, cosm_rows)

    # Wait to avoid hitting rate limits
    print("Waiting 30 seconds before next API call...")
    time.sleep(30)

    # ── PL Early KOs tab ─────────────────────────────────────────────────────
    print("Fetching PL early kick-offs...")
    pl_rows = get_pl_fixtures(client)
    pl_sheet = spreadsheet.worksheet("PL Early KOs")
    append_to_sheet(pl_sheet, pl_rows)

    print("Done.")


if __name__ == "__main__":
    main()
