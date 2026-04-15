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
 
VERIFY_PROMPT = """What is the exact kick-off time in CT (Central Time / Dallas time) for {home} vs {away} on {date}?
This is a {comp} match.
Search for the fixture and return ONLY the kick-off time in CT in this exact format: HH:MM AM or HH:MM PM
For example: 9:00 AM or 2:00 PM
Return ONLY the time, nothing else. Convert from UK/BST (subtract 6hrs), ET (subtract 1hr), or PT (add 2hrs) as needed."""
 
 
def get_cosm_fixtures():
    """Use Playwright to scrape Cosm soccer page."""
    rows = []
    now = datetime.now()
    seen = set()
 
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
        page.wait_for_timeout(5000)
 
        text = page.inner_text("body")
        print(f"  Page text length: {len(text)}")
 
        # Cosm page times are already in CT regardless of location shown
        pt_offset = 0
        print("  Using page times as CT directly")
 
        browser.close()
 
    lines = [l.strip() for l in text.splitlines() if l.strip()]
 
    current_day = ""
    current_month_day = ""
    current_time = ""
 
    i = 0
    while i < len(lines):
        line = lines[i]
 
        if re.match(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$', line, re.I):
            current_day = line
            current_month_day = ""
            current_time = ""
            i += 1
            continue
 
        if re.match(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}$', line, re.I):
            current_month_day = line
            current_time = ""
            i += 1
            continue
 
        if re.match(r'^\d{1,2}:\d{2}[ap]m$', line, re.I):
            current_time = line
            i += 1
            continue
 
        line_lower = line.lower()
        if any(kw in line_lower for kw in SOCCER_KEYWORDS) and "vs" in line_lower:
            if not current_month_day or not current_time:
                i += 1
                continue
 
            try:
                md = re.match(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})', current_month_day, re.I)
                month_str = md.group(1)[:3]
                day_num = int(md.group(2))
                year = now.year
                dt = datetime.strptime(f"{month_str} {day_num} {year}", "%b %d %Y")
                if dt < now - timedelta(days=1):
                    dt = dt.replace(year=year + 1)
                if dt < now - timedelta(days=1):
                    i += 1
                    continue
            except Exception as e:
                print(f"  Date error: {e}")
                i += 1
                continue
 
            try:
                t = datetime.strptime(current_time.upper(), "%I:%M%p")
                ko_dt = dt.replace(hour=t.hour, minute=t.minute)
                ko_dt = ko_dt + timedelta(hours=pt_offset)
            except Exception as e:
                print(f"  Time error: {e}")
                ko_dt = dt
 
            dedup_key = f"{line}_{dt.strftime('%Y%m%d')}"
            if dedup_key in seen:
                i += 1
                continue
            seen.add(dedup_key)
 
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
            print(f"  Found: {comp} | {home} vs {away} | {day_name} {date_out} {ko_time}")
 
        i += 1
 
    return rows
 
 
def verify_and_fix_times(rows, client):
    """Cross-reference each fixture's kick-off time against a live search."""
    if not rows:
        return rows
 
    verified = []
    for row in rows:
        comp, home, away, day_name, date_out, ko_time, finish_time, notes = row[:8]
 
        # Only verify PL and CL — skip EFL etc
        if comp not in ("Premier League", "Champions League"):
            verified.append(row)
            continue
 
        try:
            print(f"  Verifying: {home} vs {away} on {date_out}...")
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=50,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": VERIFY_PROMPT.format(
                    home=home, away=away, date=date_out, comp=comp
                )}],
            )
            result_text = ""
            for block in response.content:
                if block.type == "text":
                    result_text = block.text.strip()
 
            # Parse verified time
            time_match = re.search(r'(\d{1,2}:\d{2}\s*[AP]M)', result_text, re.I)
            if time_match:
                verified_time = time_match.group(1).strip()
                # Recalculate finish time
                try:
                    t = datetime.strptime(verified_time.upper().replace(" ", ""), "%I:%M%p")
                    finish_delta = timedelta(hours=2.5) if comp == "Champions League" else timedelta(hours=2)
                    finish_dt = t + finish_delta
                    new_finish = finish_dt.strftime("%I:%M %p").lstrip("0")
                    if verified_time != ko_time:
                        print(f"  Corrected: {ko_time} -> {verified_time}")
                    row = [comp, home, away, day_name, date_out, verified_time, new_finish, notes]
                except Exception:
                    pass
            else:
                print(f"  Could not parse verified time from: {result_text}")
 
            # Rate limit protection
            time.sleep(15)
 
        except Exception as e:
            print(f"  Verify error for {home} vs {away}: {e}")
            time.sleep(30)
 
        verified.append(row)
 
    return verified
 
 
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
        # Skip header rows and any row where date field looks like a header
        if len(parts) >= 7 and parts[0].lower() not in ("competition", "comp") and parts[4].lower() != "date":
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
 
    if cosm_rows:
        print(f"  Verifying {len(cosm_rows)} fixture times via web search...")
        cosm_rows = verify_and_fix_times(cosm_rows, client)
 
    cosm_sheet = spreadsheet.worksheet("Cosm")
    append_to_sheet(cosm_sheet, cosm_rows)
 
    print("Waiting 90 seconds before PL search...")
    time.sleep(90)
 
    # ── PL Early KOs tab ─────────────────────────────────────────────────────
    print("Fetching PL early kick-offs...")
    pl_rows = get_pl_fixtures(client)
    pl_sheet = spreadsheet.worksheet("PL Early KOs")
    append_to_sheet(pl_sheet, pl_rows)
 
    print("Done.")
 
 
if __name__ == "__main__":
    main()
 
