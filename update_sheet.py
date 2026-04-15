import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ── Config ──────────────────────────────────────────────────────────────────
SPREADSHEET_ID = "1Iu_A3hs0WbsZ9HELl0von2ihNfiKeEM2zd1OtlLxZrc"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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

COSM_PROMPT = """Search the web for upcoming soccer matches at Cosm Dallas (cosm.com/location/dallas or cosm.com/categories/sports/soccer).
Today is {today}. Return ONLY future matches.
For each match return one line in this EXACT comma-separated format with no headers or extra text:
Competition,Home,Away,Day,Date,KO Time,Finish Time,Notes

- Competition: Premier League or Champions League
- Date format: DD-Mon (e.g. 18-Apr)
- Times in CT (Dallas time) using AM/PM
- Finish Time: 2 hours after KO for PL, 2.5 hours for Champions League
- Notes: "Could run longer with extra time" for Champions League knockouts, blank otherwise
- If none found return: NONE"""


def get_cosm_fixtures_via_api():
    """Try multiple Cosm API URL formats to find events."""
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

    # Try different URL formats
    urls = [
        # Without category filter
        "https://api.cosm.io/event/%24oid%3A659db49056ef4642c65d9196/events?limit=20&sort=startTime",
        # With raw (unencoded) IDs
        "https://api.cosm.io/event/$oid:659db49056ef4642c65d9196/events?categoryIds=$oid:65fc7411ee01793ee2d67b59&limit=20&sort=startTime",
        # Dallas location name instead of ID
        "https://api.cosm.io/event/dallas/events?categoryIds=soccer&limit=20&sort=startTime",
    ]

    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            print(f"  Tried: {url[:80]}... status={resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", data.get("events", data.get("data", [])))
                print(f"  Results count: {len(results)}")
                if results:
                    print(f"  First result keys: {list(results[0].keys())}")
                    return results
        except Exception as e:
            print(f"  Error: {e}")

    return []


def parse_cosm_events(events):
    """Parse raw Cosm API events into sheet rows."""
    rows = []
    now = datetime.now(timezone.utc)

    for event in events:
        start_raw = (event.get("startTime") or event.get("start_time") or
                    event.get("startDate") or event.get("start") or "")
        title = event.get("title") or event.get("name") or ""
        print(f"  Parsing: '{title}' at '{start_raw}'")

        if not start_raw:
            continue
        try:
            dt_utc = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            if dt_utc < now:
                continue
            dt_ct = dt_utc - timedelta(hours=5)
        except Exception as e:
            print(f"  Date parse error: {e}")
            continue

        comp = "Soccer"
        title_lower = title.lower()
        if "champions league" in title_lower or "ucl" in title_lower:
            comp = "Champions League"
        elif "premier league" in title_lower or "epl" in title_lower:
            comp = "Premier League"

        home, away = "", ""
        if " vs " in title:
            home, away = [p.strip() for p in title.split(" vs ", 1)]
        elif " v " in title:
            home, away = [p.strip() for p in title.split(" v ", 1)]
        else:
            home = title

        day = dt_ct.strftime("%A")
        date = dt_ct.strftime("%d-%b")
        ko_time = dt_ct.strftime("%I:%M %p").lstrip("0")
        finish_delta = timedelta(hours=2.5) if comp == "Champions League" else timedelta(hours=2)
        finish_time = (dt_ct + finish_delta).strftime("%I:%M %p").lstrip("0")
        notes = "Could run longer with extra time" if comp == "Champions League" else ""

        rows.append([comp, home, away, day, date, ko_time, finish_time, notes])

    return rows


def get_cosm_fixtures_via_claude(client):
    """Fallback: use Claude web search to find Cosm fixtures."""
    today = datetime.now().strftime("%d-%b-%Y")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": COSM_PROMPT.format(today=today)}],
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
        if len(parts) >= 7 and parts[0].lower() not in ("competition", "comp"):
            rows.append(parts)
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
        if len(parts) >= 7 and parts[0].lower() not in ("competition", "comp"):
            rows.append(parts)
    return rows


def append_to_sheet(sheet, rows):
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
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)

    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    client = anthropic.Anthropic(api_key=anthropic_key)

    # ── Cosm tab ─────────────────────────────────────────────────────────────
    print("Fetching Cosm Dallas fixtures via API...")
    events = get_cosm_fixtures_via_api()
    cosm_rows = parse_cosm_events(events)

    if not cosm_rows:
        print("  API returned no results, trying Claude web search fallback...")
        time.sleep(10)
        cosm_rows = get_cosm_fixtures_via_claude(client)

    cosm_sheet = spreadsheet.worksheet("Cosm")
    append_to_sheet(cosm_sheet, cosm_rows)

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
