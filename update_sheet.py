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
 
SOCCER_KEYWORDS = ["premier league", "champions league", "efl", "epl", "ucl", "fa cup", "league cup"]
 
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
    """Use Playwright to scrape the Cosm Dallas soccer page."""
    rows = []
    now = datetime.now()
 
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
 
        print(f"  Opening {COSM_URL}...")
        page.goto(COSM_URL, wait_until="networkidle", timeout=30000)
 
        # Make sure Dallas is selected — click the Dallas location button if present
        try:
            dallas_btn = page.locator("button:has-text('Dallas'), a:has-text('Dallas')").first
            if dallas_btn.is_visible():
                dallas_btn.click()
                page.wait_for_load_state("networkidle")
                print("  Clicked Dallas location selector")
        except Exception:
            pass
 
        # Wait for event cards to load
        page.wait_for_timeout(3000)
 
        # Get the full page text
        content = page.content()
        text = page.inner_text("body")
        print(f"  Page loaded, text length: {len(text)}")
 
        browser.close()
 
    # Parse events from page text
    # Look for patterns like "Premier League: Arsenal vs Newcastle\nSat, Apr 25\n11:30 AM"
    lines = [l.strip() for l in text.splitlines() if l.strip()]
 
    i = 0
    while i < len(lines):
        line = lines[i]
        line_lower = line.lower()
 
        # Check if this line is a soccer event title
        if any(kw in line_lower for kw in SOCCER_KEYWORDS) and ("vs" in line_lower or "v." in line_lower):
            print(f"  Found event: {line}")
            title = line
 
            # Look ahead for date and time in next few lines
            date_str = ""
            time_str = ""
            for j in range(i + 1, min(i + 8, len(lines))):
                next_line = lines[j]
                # Date pattern: "Sat, Apr 25" or "April 25" or "Apr 25"
                date_match = re.search(
                    r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*[,.]?\s+'
                    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})',
                    next_line, re.I
                )
                if date_match and not date_str:
                    date_str = next_line
                # Time pattern: "11:30 AM" or "2:00 PM"
                time_match = re.search(r'(\d{1,2}:\d{2}\s*[AP]M)', next_line, re.I)
                if time_match and not time_str:
                    time_str = time_match.group(1)
                if date_str and time_str:
                    break
 
            if not date_str:
                i += 1
                continue
 
            # Parse date
            try:
                date_match = re.search(
                    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})',
                    date_str, re.I
                )
                if not date_match:
                    i += 1
                    continue
                month_str, day_num = date_match.groups()
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
            if time_str:
                try:
                    t = datetime.strptime(time_str.strip(), "%I:%M %p")
                    ko_dt = dt.replace(hour=t.hour, minute=t.minute)
                except Exception:
                    try:
                        t = datetime.strptime(time_str.strip(), "%I:%M%p")
                        ko_dt = dt.replace(hour=t.hour, minute=t.minute)
                    except Exception:
                        pass
 
            # Determine competition
            comp = "Soccer"
            title_lower = title.lower()
            if "premier league" in title_lower or "epl" in title_lower:
                comp = "Premier League"
            elif "champions league" in title_lower or "ucl" in title_lower:
                comp = "Champions League"
            elif "efl" in title_lower:
                comp = "EFL"
            elif "fa cup" in title_lower:
                comp = "FA Cup"
 
            # Extract teams
            home, away = "", ""
            team_match = re.search(r':\s*(.+?)\s+vs\.?\s+(.+?)$', title, re.I)
            if team_match:
                home = team_match.group(1).strip()
                away = team_match.group(2).strip()
            else:
                vs_match = re.search(r'(.+?)\s+vs\.?\s+(.+)', title, re.I)
                if vs_match:
                    home = vs_match.group(1).strip()
                    away = vs_match.group(2).strip()
                else:
                    home = title
 
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
    print("Fetching Cosm Dallas fixtures via Playwright...")
    cosm_rows = get_cosm_fixtures()
    cosm_sheet = spreadsheet.worksheet("Cosm")
    append_to_sheet(cosm_sheet, cosm_rows)
 
    print("Waiting 60 seconds before next API call...")
    time.sleep(60)
 
    # ── PL Early KOs tab ─────────────────────────────────────────────────────
    print("Fetching PL early kick-offs...")
    pl_rows = get_pl_fixtures(client)
    pl_sheet = spreadsheet.worksheet("PL Early KOs")
    append_to_sheet(pl_sheet, pl_rows)
 
    print("Done.")
 
 
if __name__ == "__main__":
    main()
 
