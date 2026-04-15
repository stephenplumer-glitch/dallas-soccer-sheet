import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import os
import re
import time
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
 
# ── Config ──────────────────────────────────────────────────────────────────
SPREADSHEET_ID = "1Iu_A3hs0WbsZ9HELl0von2ihNfiKeEM2zd1OtlLxZrc"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
COSM_URL = "https://www.cosm.com/categories/sports/soccer"
 
SOCCER_KEYWORDS = ["premier league", "champions league", "efl", "epl", "ucl", "fa cup"]
 
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
 
 
def parse_events_from_text(text, location="dallas"):
    """Parse soccer events from page text, only for the specified location section."""
    rows = []
    now = datetime.now()
    seen = set()
 
    # Split text into Dallas and LA sections
    text_lower = text.lower()
    dallas_start = text_lower.find("cosm dallas")
    la_start = text_lower.find("cosm los angeles")
 
    if dallas_start == -1:
        print(f"  'Cosm Dallas' not found in page text")
        print(f"  Full text:\n{text[:2000]}")
        return rows
 
    # Extract just the Dallas section (stop before LA section if it comes after)
    if la_start != -1 and la_start > dallas_start:
        dallas_text = text[dallas_start:la_start]
    else:
        dallas_text = text[dallas_start:]
 
    print(f"  Dallas section length: {len(dallas_text)}")
    print(f"  Dallas section preview: {dallas_text[:500]}")
 
    lines = [l.strip() for l in dallas_text.splitlines() if l.strip()]
 
    # The page format is:
    # Day  Month Date  Time
    # Title
    # So look for date+time lines followed by event title lines
 
    i = 0
    current_date = None
    current_time = None
 
    while i < len(lines):
        line = lines[i]
 
        # Check for date pattern: "Wed Apr 15" or "Sat Apr 18"
        date_match = re.search(
            r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+'
            r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})',
            line, re.I
        )
        if date_match:
            current_date = line
            current_time = None
            # Look for time on same line or next line
            time_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', line, re.I)
            if time_match:
                current_time = time_match.group(1)
            i += 1
            continue
 
        # Check for standalone time
        time_match = re.search(r'^(\d{1,2}:\d{2}\s*[ap]m)$', line, re.I)
        if time_match:
            current_time = time_match.group(1)
            i += 1
            continue
 
        # Check if this is a soccer event title
        line_lower = line.lower()
        if any(kw in line_lower for kw in SOCCER_KEYWORDS) and "vs" in line_lower:
            print(f"  Found event: {line} | date={current_date} | time={current_time}")
 
            if not current_date:
                print(f"  Skipping - no date context")
                i += 1
                continue
 
            # Parse date
            try:
                dm = re.search(
                    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})',
                    current_date, re.I
                )
                month_str = dm.group(1)[:3]
                day_num = dm.group(2)
                year = now.year
                dt = datetime.strptime(f"{month_str} {day_num} {year}", "%b %d %Y")
                if dt < now - timedelta(days=1):
                    dt = dt.replace(year=year + 1)
                if dt < now - timedelta(days=1):
                    i += 1
                    continue
            except Exception as e:
                print(f"  Date parse error: {e}")
                i += 1
                continue
 
            # Parse time
            ko_dt = dt
            if current_time:
                try:
                    t_str = current_time.strip().upper().replace(" ", "")
                    t = datetime.strptime(t_str, "%I:%M%p")
                    ko_dt = dt.replace(hour=t.hour, minute=t.minute)
                except Exception as e:
                    print(f"  Time parse error: {e} for '{current_time}'")
 
            # Dedup
            dedup_key = f"{line}_{ko_dt.strftime('%Y%m%d%H%M')}"
            if dedup_key in seen:
                i += 1
                continue
            seen.add(dedup_key)
 
            # Competition
            comp = "Soccer"
            tl = line.lower()
            if "premier league" in tl:
                comp = "Premier League"
            elif "champions league" in tl:
                comp = "Champions League"
            elif "efl" in tl:
                comp = "EFL"
            elif "fa cup" in tl:
                comp = "FA Cup"
 
            # Teams
            home, away = "", ""
            team_match = re.search(r':\s*(.+?)\s+vs\.?\s+(.+?)$', line, re.I)
            if team_match:
                home, away = team_match.group(1).strip(), team_match.group(2).strip()
            else:
                vs_match = re.search(r'(.+?)\s+vs\.?\s+(.+)', line, re.I)
                if vs_match:
                    home, away = vs_match.group(1).strip(), vs_match.group(2).strip()
                else:
                    home = line
 
            day_name = ko_dt.strftime("%A")
            date_out = ko_dt.strftime("%d-%b")
            ko_time = ko_dt.strftime("%I:%M %p").lstrip("0")
            finish_delta = timedelta(hours=2.5) if comp == "Champions League" else timedelta(hours=2)
            finish_time = (ko_dt + finish_delta).strftime("%I:%M %p").lstrip("0")
            notes = "Could run longer with extra time" if comp == "Champions League" else ""
 
            rows.append([comp, home, away, day_name, date_out, ko_time, finish_time, notes])
            print(f"  Parsed: {comp} | {home} vs {away} | {day_name} {date_out} {ko_time}")
 
        i += 1
 
    return rows
 
 
def get_cosm_fixtures():
    """Use Playwright to scrape the Cosm Dallas soccer page."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
 
        page = context.new_page()
        print(f"  Opening {COSM_URL}...")
        page.goto(COSM_URL, wait_until="domcontentloaded", timeout=60000)
 
        # Click Dallas location button
        try:
            page.wait_for_selector("button:has-text('Dallas'), [data-location='dallas']", timeout=10000)
            dallas_btn = page.locator("button:has-text('Dallas')").first
            dallas_btn.click()
            print("  Clicked Dallas button")
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  Could not click Dallas button: {e}")
 
        # Wait for content
        page.wait_for_timeout(5000)
 
        text = page.inner_text("body")
        print(f"  Page text length: {len(text)}")
        browser.close()
 
    return parse_events_from_text(text)
 
 
def get_pl_fixtures(client):
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
 
    print("Fetching Cosm Dallas fixtures via Playwright...")
    cosm_rows = get_cosm_fixtures()
    cosm_sheet = spreadsheet.worksheet("Cosm")
    append_to_sheet(cosm_sheet, cosm_rows)
 
    print("Waiting 90 seconds before next API call...")
    time.sleep(90)
 
    print("Fetching PL early kick-offs...")
    pl_rows = get_pl_fixtures(client)
    pl_sheet = spreadsheet.worksheet("PL Early KOs")
    append_to_sheet(pl_sheet, pl_rows)
 
    print("Done.")
 
 
if __name__ == "__main__":
    main()
 
