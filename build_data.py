import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

api_key = os.environ.get("CONGRESS_API_KEY", "").strip()
if not api_key:
    api_key = input("Paste your Congress.gov API key and press Enter: ").strip()
api_key = api_key.strip('"').strip("'")

CONGRESS = 119
SESSION = 2
HOUSE_WANTED = 12     # how many recent House passage votes to collect
SENATE_WANTED = 12    # how many recent Senate passage votes to collect

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
MENU_URL = ("https://www.senate.gov/legislative/LIS/roll_call_lists/"
            "vote_menu_" + str(CONGRESS) + "_" + str(SESSION) + ".xml")

JSON_HEADERS = {"User-Agent": "Mozilla/5.0 (voting-app-prototype)",
                "Accept": "application/json"}
XML_HEADERS = {"User-Agent": "Mozilla/5.0 (voting-app-prototype)",
               "Accept": "application/xml,text/xml"}
SENATE_PASSAGE = ("on passage", "on the joint resolution",
                  "on the concurrent resolution", "on the resolution")
CODES = {"Yea": "Y", "Aye": "Y", "Nay": "N", "No": "N",
         "Present": "P", "Not Voting": "NV"}


def fetch(url, headers, tries=3):
    for attempt in range(1, tries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return b""
            if error.code in (400, 401, 403):
                print("Server said:", error.code, error.reason)
                return None
        except Exception:
            pass
        time.sleep(2)
    return None


def call(path, extra=None):
    values = {"api_key": api_key, "format": "json"}
    if extra:
        values.update(extra)
    raw = fetch("https://api.congress.gov/v3/" + path + "?"
                + urllib.parse.urlencode(values), JSON_HEADERS)
    if raw is None:
        return None
    if raw == b"":
        return {}
    return json.loads(raw)


def senate_xml(url):
    raw = fetch(url, XML_HEADERS)
    if not raw:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


def child(element, name):
    node = element.find(name)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def bill_details(doc_type, doc_number):
    """Returns title, summary, summary_status, text_url."""
    kind = re.sub(r"[^a-z]", "", doc_type.lower())
    path = "bill/%d/%s/%s" % (CONGRESS, kind, doc_number)
    title, summary, status, text_url = None, None, "none", None

    bill = call(path)
    if bill and "bill" in bill:
        title = bill["bill"].get("title")

    summaries = call(path + "/summaries")
    if summaries is None:
        status = "failed"
    else:
        items = summaries.get("summaries", [])
        if items:
            summary = clean(items[-1].get("text"))[:2500]
            status = "ok"

    text_info = call(path + "/text")
    if text_info:
        versions = text_info.get("textVersions", [])
        if versions:
            for fmt in versions[0].get("formats", []):
                if fmt.get("type") == "Formatted Text":
                    text_url = fmt.get("url")
    return title, summary, status, text_url


votes = []

# ---------- House members (to map state + district to a person) ----------
print("Collecting House members...")
house_members = []
offset = 0
while True:
    data = call("member", {"currentMember": "true", "limit": 250, "offset": offset})
    if not data:
        break
    page = data.get("members", [])
    for m in page:
        if m.get("district") is not None:
            house_members.append({
                "id": m.get("bioguideId"),
                "name": m.get("name"),
                "party": m.get("partyName"),
                "state": m.get("state"),
                "district": m.get("district"),
            })
    if len(page) < 250:
        break
    offset += 250
print("  House members found:", len(house_members))

# ---------- House votes ----------
print("Collecting House passage votes (this takes a few minutes)...")
data = call("house-vote/%d/%d" % (CONGRESS, SESSION), {"limit": 1})
total = int(((data or {}).get("pagination") or {}).get("count") or 0)
found = 0
for n in range(total, 0, -1):
    if found >= HOUSE_WANTED or total - n > 150:
        break
    vote = call("house-vote/%d/%d/%d/members" % (CONGRESS, SESSION, n))
    time.sleep(0.2)
    if not vote:
        continue
    info = vote.get("houseRollCallVoteMemberVotes", {})
    question = info.get("voteQuestion") or ""
    members = info.get("results")
    if "pass" not in question.lower() or not isinstance(members, list):
        continue
    doc_type = info.get("legislationType") or ""
    doc_number = str(info.get("legislationNumber") or "")
    if not doc_type or not doc_number:
        continue
    title, summary, status, text_url = bill_details(doc_type, doc_number)
    cast = {}
    for m in members:
        cast[m.get("bioguideID")] = CODES.get(m.get("voteCast"), "NV")
    votes.append({
        "id": "h" + str(n), "chamber": "House",
        "date": (info.get("startDate") or "")[:10],
        "bill": doc_type + " " + doc_number,
        "title": title, "question": question,
        "result": info.get("result"),
        "summary": summary, "summary_status": status,
        "text_url": text_url, "house_votes": cast,
    })
    found += 1
    print("  House vote", n, doc_type, doc_number, "| summary:", status)

# ---------- Senate votes ----------
print("Collecting Senate passage votes...")
menu = senate_xml(MENU_URL)
rows = []
if menu is not None:
    rows = [v for v in menu.findall(".//vote")
            if child(v, "question").lower().startswith(SENATE_PASSAGE)]
    rows.sort(key=lambda v: int(child(v, "vote_number") or 0), reverse=True)
for v in rows[:SENATE_WANTED]:
    number = int(child(v, "vote_number"))
    url = ("https://www.senate.gov/legislative/LIS/roll_call_votes/vote"
           + str(CONGRESS) + str(SESSION) + "/vote_" + str(CONGRESS) + "_"
           + str(SESSION) + "_" + str(number).zfill(5) + ".xml")
    root = senate_xml(url)
    time.sleep(0.2)
    if root is None:
        continue
    doc_type = child(root, "document/document_type")
    doc_number = child(root, "document/document_number")
    if not doc_type or not doc_number:
        continue
    title, summary, status, text_url = bill_details(doc_type, doc_number)
    cast = []
    for m in root.findall(".//members/member"):
        cast.append({
            "state": child(m, "state"),
            "name": child(m, "last_name"),
            "party": child(m, "party"),
            "cast": CODES.get(child(m, "vote_cast"), "NV"),
        })
    votes.append({
        "id": "s" + str(number), "chamber": "Senate",
        "date": child(root, "vote_date"),
        "bill": doc_type + " " + doc_number,
        "title": title,
        "question": child(root, "vote_question_text"),
        "result": child(root, "vote_result"),
        "summary": summary, "summary_status": status,
        "text_url": text_url, "senate_votes": cast,
    })
    print("  Senate vote", number, doc_type, doc_number, "| summary:", status)

if len(house_members) < 400 or len(votes) < 10:
    print("Something looks wrong (too little data). Keeping the old data.json.")
    raise SystemExit(1)
# ---------- save ----------
output = {
    "generated": time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
    "congress": CONGRESS,
    "session": SESSION,
    "house_members": house_members,
    "votes": votes,
}
with open(OUT_FILE, "w") as f:
    json.dump(output, f, separators=(",", ":"))

size_kb = round(os.path.getsize(OUT_FILE) / 1024)
counts = {}
for v in votes:
    counts[v["summary_status"]] = counts.get(v["summary_status"], 0) + 1
print()
print("Done.")
print("House members:", len(house_members))
print("Votes saved:", len(votes), "| summary status:", counts)
print("File size:", size_kb, "KB")
print("Saved to:", OUT_FILE)
