import datetime
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ.get("FEC_API_KEY", "").strip()
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "challengers")
XWALK = "https://unitedstates.github.io/congress-legislators/"
HEADERS = {"User-Agent": "Mozilla/5.0 (voting-app-prototype)",
           "Accept": "application/json"}

TITLES = {"MR", "MRS", "MS", "MISS", "DR", "REV", "HON", "N/A"}
SUFFIXES = {"JR", "SR", "II", "III", "IV"}
ROMAN = {"II", "III", "IV"}
NO_PARTY = {"", "UNKNOWN", "NONE", "N/A"}


def next_election_year(now=None):
    """The next general election year (the election is in early November)."""
    now = now or datetime.datetime.utcnow()
    y = now.year
    if y % 2 == 0:
        return y if (now.month, now.day) <= (11, 8) else y + 2
    return y + 1


def get_json(url):
    for attempt in range(1, 4):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            print("Server said:", error.code, error.reason)
            if error.code in (400, 401, 403, 404, 422):
                return None
        except Exception as error:
            text = str(error)
            if API_KEY:
                text = text.replace(API_KEY, "[KEY HIDDEN]")
            print("Attempt", attempt, "failed:", text)
        time.sleep(3)
    return None


def fetch_all(office, year):
    """All FEC candidates for an office and election year, or None if a request failed."""
    found = []
    page = 1
    while True:
        params = {"api_key": API_KEY, "election_year": year, "office": office,
                  "per_page": 100, "page": page, "sort": "name"}
        data = get_json("https://api.open.fec.gov/v1/candidates/search/?"
                        + urllib.parse.urlencode(params))
        if data is None:
            return None
        found.extend(data.get("results", []))
        pages = (data.get("pagination") or {}).get("pages", 1) or 1
        if page >= pages or page >= 150:
            break
        page += 1
        time.sleep(0.3)
    return found


def id_map(people):
    found = {}
    for p in people or []:
        ids = p.get("id", {})
        for fec_id in ids.get("fec", []) or []:
            found[fec_id] = ids.get("bioguide")
    return found


def fix_case(word):
    if word.upper() in ROMAN:
        return word.upper()
    w = word.title()
    if w.startswith("Mc") and len(w) > 2:
        w = "Mc" + w[2:].capitalize()
    return w


def clean_name(raw):
    """'LAST, FIRST MIDDLE MR JR' -> 'First Middle Last Jr'"""
    text = (raw or "").strip()
    if "," in text:
        last, rest = text.split(",", 1)
    else:
        last, rest = "", text
    words = [w.strip(".") for w in rest.split() if w.strip(".")]
    first = [w for w in words if w.upper() not in TITLES and w.upper() not in SUFFIXES]
    suffix = []
    for w in words:
        if w.upper() in SUFFIXES and w.upper() not in [s.upper() for s in suffix]:
            suffix.append(w)
    parts = first + [last.strip()] + suffix
    return " ".join(fix_case(p) for p in parts if p)


def party_name(c):
    p = (c.get("party_full") or c.get("party") or "").strip()
    return "Party not listed" if p.upper() in NO_PARTY else p.title()


def label_for(c, office):
    code = c.get("incumbent_challenge")
    if code == "I":
        return "Incumbent"
    if office in ("H", "S"):
        if code == "C":
            return "Challenger"
        if code == "O":
            return "Open seat"
    return ""


def tidy(cands, office, xwalk, year):
    """Clean, drop inactive and other-year filings, merge duplicates."""
    best = {}
    for c in cands:
        years = c.get("election_years") or []
        if years and year not in years:
            continue
        if c.get("candidate_inactive"):
            continue
        name = clean_name(c.get("name"))
        raw = c.get("name") or ""
        row = {
            "raw": raw,
            "id": c.get("candidate_id"),
            "name": name,
            "party": party_name(c),
            "label": label_for(c, office),
            "status": c.get("candidate_status") or "",
            "last": re.sub(r"[^a-z]", "", raw.split(",")[0].lower()),
            "member": xwalk.get(c.get("candidate_id")),
        }
        k = re.sub(r"[^a-z]", "", name.lower())
        old = best.get(k)
        if old is None or (row["status"] == "C" and old["status"] != "C"):
            best[k] = row
    return sorted(best.values(), key=lambda r: r["raw"])


def bucket(rows):
    """Candidates over the FEC's $5,000 line are listed; the rest are only counted."""
    above = [r for r in rows if r["status"] == "C"]
    return {
        "candidates": [{"id": r["id"], "name": r["name"], "party": r["party"],
                        "label": r["label"], "last": r["last"], "member": r["member"]}
                       for r in above],
        "other_count": len(rows) - len(above),
    }


def build(year, house, senate, president, xwalk):
    states = {}
    by_seat = {}
    for c in house:
        st = c.get("state")
        dist = str(c.get("district") or "00").zfill(2)
        by_seat.setdefault((st, dist), []).append(c)
    for (st, dist), cands in by_seat.items():
        entry = states.setdefault(st, {"house": {}, "senate": None})
        entry["house"][dist] = bucket(tidy(cands, "H", xwalk, year))
    by_state = {}
    for c in senate:
        by_state.setdefault(c.get("state"), []).append(c)
    for st, cands in by_state.items():
        entry = states.setdefault(st, {"house": {}, "senate": None})
        entry["senate"] = bucket(tidy(cands, "S", xwalk, year))
    return states, bucket(tidy(president, "P", xwalk, year))


def write_outputs(year, states, president):
    tmp = OUT_DIR + ".tmp"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    for st, data in states.items():
        if not st:
            continue
        payload = {"generated": stamp, "election_year": year,
                   "house": data["house"], "senate": data["senate"]}
        with open(os.path.join(tmp, st + ".json"), "w") as f:
            json.dump(payload, f, separators=(",", ":"))
    payload = {"generated": stamp, "election_year": year}
    payload.update(president)
    with open(os.path.join(tmp, "president.json"), "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.rename(tmp, OUT_DIR)


def main():
    if not API_KEY:
        print("FEC_API_KEY is missing.")
        raise SystemExit(1)
    year = int(os.environ.get("ELECTION_YEAR") or next_election_year())
    print("Election year:", year)

    house = fetch_all("H", year)
    senate = fetch_all("S", year)
    president = fetch_all("P", year)
    if house is None or senate is None or president is None:
        print("A request to the FEC failed. Keeping the old files.")
        raise SystemExit(1)
    print("Raw counts - House:", len(house), "| Senate:", len(senate),
          "| President:", len(president))

    print("Loading members of Congress (current and former)...")
    current = get_json(XWALK + "legislators-current.json")
    if not current:
        print("Could not load current members. Keeping the old files.")
        raise SystemExit(1)
    xwalk = id_map(current)
    xwalk.update(id_map(get_json(XWALK + "legislators-historical.json")))

    states, pres = build(year, house, senate, president, xwalk)
    write_outputs(year, states, pres)

    listed = sum(len(s["senate"]["candidates"]) if s["senate"] else 0 for s in states.values())
    listed += sum(len(b["candidates"]) for s in states.values() for b in s["house"].values())
    print()
    print("Done.")
    print("State files written:", len(states))
    print("House and Senate candidates listed (over $5,000):", listed)
    print("Presidential candidates listed (over $5,000):", len(pres["candidates"]),
          "| others registered:", pres["other_count"])


if __name__ == "__main__":
    main()
