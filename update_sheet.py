import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import os
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
SPREADSHEET_ID = "1Iu_A3hs0WbsZ9HELl0von2ihNfiKeEM2zd1OtlLxZrc"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

COSM_PROMPT = """Search the web for upcoming soccer matches being shown at Cosm Dallas.
Return ONLY matches that haven't happened yet (today is {today}).
Return each match on a new line in this EXACT comma-separated format with no headers, no extra text, no blank lines:
Competition,Home,Away,Day,Date,KO Time,Finish Time,Notes

- Date format: DD-Mon (e.g. 15-Apr)
- Times should be in CT (Central Time), using AM/PM (e.g. 9:00 AM)
- Finish Time: assume 2 hours after KO for PL, 2.5 hours for Champions League
- Notes: add "Could run longer with extra time" for knockout Champions League games, otherwise leave blank
- If no upcoming matches are found, return the single word: NONE"""

PL_PROMPT = """Search the web for upcoming Premier League matches in the next 3 weeks.
Return ONLY matches where kick-off is at or before 9:00 AM CT (Central Time), and only matches that haven't happened yet (today is {today}).
Return each match on a new line in this EXACT comma-separated format with no headers, no extra text, no blank lines:
Premier League,Home,Away,Day,Date,KO Time,OPEN,Notes

- Date format: DD-Mon (e.g. 15-Apr)
- Times in CT using AM/PM (e.g. 7:30 AM)
- OPEN: 15 minutes before KO Time (e.g. if KO is 7:30 AM, OPEN is 7:15 AM)
- Notes: leave blank
- If no early kick-offs are found, return the single word: NONE"""


def get_fixtures(client, prompt):
    """Call Claude with web search and return lines of fixture data."""
    today = datetime.now().strftime("%d-%b-%Y")
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt.format(today=today)}],
    )

    # Extract the final text response
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
            rows.append(parts)

    return rows


def append_to_sheet(sheet, rows):
    """Append rows to a Google Sheet tab."""
    if not rows:
        print(f"  No new fixtures to add to '{sheet.title}'")
        return

    # Find the first empty row
    existing = sheet.get_all_values()
    next_row = len(existing) + 1

    for row in rows:
        # Pad to 8 columns (B through I — sheet columns B-I map to indices 1-8)
        padded = row[:8] + [""] * (8 - len(row[:8]))
        # Write starting from column B (col index 2)
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

    # ── Cosm tab ─────────────────────────────────────────────────────────────
    print("Fetching Cosm Dallas fixtures...")
    cosm_rows = get_fixtures(client, COSM_PROMPT)
    cosm_sheet = spreadsheet.worksheet("Cosm")
    append_to_sheet(cosm_sheet, cosm_rows)

    # ── PL Early KOs tab ─────────────────────────────────────────────────────
    print("Fetching PL early kick-offs...")
    pl_rows = get_fixtures(client, PL_PROMPT)
    pl_sheet = spreadsheet.worksheet("PL Early KOs")
    append_to_sheet(pl_sheet, pl_rows)

    print("Done.")


if __name__ == "__main__":
    main()
