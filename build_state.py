import datetime
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ.get("OPEN_STATES_API_KEY", "").strip()
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "states")
OS_BASE = "https://v3.openstates.org"
HEADERS = {"User-Agent": "Mozilla/5.0 (voting-app-prototype)",
           "Accept": "application/json"}

# The pilot covers one state. More can be added later.
STATES = {"PA": "Pennsylvania"}
DAYS_BACK = 270          # how far back to look for bills with recent activity
MAX_BILL_PAGES = 80      # 20 bills per page
KEEP_BILLS = 100         # newest bills with floor votes that go into the file
STOP_AT = 160            # stop paging once this many are found (a buffer before trimming)
PAUSE = 7                # seconds between requests (free tier allows about 10 a minute)

OPTION_CODES = {"yes": "Y", "no": "N"}


def get_json(path, params):
    url = OS_BASE + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(1, 4):
        try:
            request = urllib.request.Request(url, headers=dict(HEADERS, **{"X-API-KEY": API_KEY}))
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
            time.sleep(PAUSE)
            return data
        except urllib.error.HTTPError as error:
            print("Server said:", error.code, error.reason, "for", path)
            if error.code == 429:
                print("Rate limit reached. Waiting 60 seconds...")
                time.sleep(60)
                continue
            if error.code in (400, 401, 403, 404, 422):
                return None
        except Exception as error:
            print("Attempt", attempt, "failed:", str(error).replace(API_KEY, "[KEY HIDDEN]"))
        time.sleep(5)
    return None


def words_of(text):
    return re.sub(r"[^a-z ]", " ", (text or "").lower()).split()


def same_person(voter_name, leg):
    """Whole-word family name must match; first initials must agree when a first name is given."""
    words = words_of(voter_name)
    if not words or not leg["family"] or leg["family"] not in words:
        return False
    others = [w for w in words if w != leg["family"]]
    initials = {w[0] for w in others}
    if initials and leg["given"] and leg["given"][0] not in initials:
        return False
    return True


def assign_casts(legs, records):
    """One character per legislator: Y, N, V (did not vote) or - (could not tell)."""
    owners = {}
    mine = {}
    for i, leg in enumerate(legs):
        for j, record in enumerate(records):
            if same_person(record.get("voter_name"), leg):
                mine.setdefault(i, []).append(j)
                owners.setdefault(j, []).append(i)
    chars = []
    for i in range(len(legs)):
        js = mine.get(i, [])
        if len(js) == 1 and len(owners[js[0]]) == 1:
            option = (records[js[0]].get("option") or "").lower()
            chars.append(OPTION_CODES.get(option, "V"))
        else:
            chars.append("-")
    return "".join(chars)


def fetch_legislators(state_name):
    people = []
    page = 1
    while True:
        data = get_json("/people", {"jurisdiction": state_name, "per_page": 50, "page": page})
        if data is None:
            return None
        people.extend(data.get("results") or [])
        max_page = (data.get("pagination") or {}).get("max_page") or 1
        if page >= max_page or page >= 20:
            break
        page += 1
    out = {"upper": [], "lower": []}
    for p in people:
        role = p.get("current_role") or {}
        chamber = role.get("org_classification")
        if chamber not in out:
            continue
        name = p.get("name") or ""
        parts = words_of(name)
        family = words_of(p.get("family_name"))[-1:] or parts[-1:]
        given = words_of(p.get("given_name")) or parts[:1]
        out[chamber].append({
            "id": p.get("id"), "name": name, "party": p.get("party") or "",
            "district": str(role.get("district") or ""),
            "family": family[0] if family else "", "given": given[0] if given else "",
        })
    for chamber in out:
        out[chamber].sort(key=lambda x: (x["district"], x["name"]))
    return out


def is_bill(b):
    kinds = b.get("classification") or []
    if kinds:
        return "bill" in kinds
    return bool(re.match(r"^(HB|SB)\b", (b.get("identifier") or "").upper()))


def latest_vote_date(bill):
    return max([v.get("date") or "" for v in bill["votes"].values()] or [""])


def trim(bills):
    """Keep only the newest bills, judged by their latest floor vote."""
    ordered = sorted(bills, key=latest_vote_date, reverse=True)
    return ordered[:KEEP_BILLS]


def fetch_bills(state_name, legs):
    since = (datetime.date.today() - datetime.timedelta(days=DAYS_BACK)).isoformat()
    kept = []
    for page in range(1, MAX_BILL_PAGES + 1):
        data = get_json("/bills", [("jurisdiction", state_name), ("include", "votes"),
                                   ("action_since", since), ("sort", "latest_action_desc"),
                                   ("per_page", 20), ("page", page)])
        if data is None:
            return None
        results = data.get("results") or []
        for b in results:
            if not is_bill(b):
                continue
            chambers = {}
            for v in b.get("votes") or []:
                org = (v.get("organization") or {}).get("classification")
                if org not in legs or "passage" not in (v.get("motion_classification") or []):
                    continue
                old = chambers.get(org)
                if old and (old["date"] or "") >= (v.get("start_date") or ""):
                    continue
                counts = {c.get("option"): c.get("value") for c in v.get("counts") or []}
                chambers[org] = {
                    "date": v.get("start_date") or "",
                    "result": v.get("result") or "",
                    "yes": counts.get("yes", 0),
                    "no": counts.get("no", 0),
                    "other": sum(val or 0 for opt, val in counts.items() if opt not in ("yes", "no")),
                    "c": assign_casts(legs[org], v.get("votes") or []),
                }
            if chambers:
                kept.append({
                    "id": (b.get("session") or "") + "-" + (b.get("identifier") or ""),
                    "bill": b.get("identifier"),
                    "title": b.get("title") or "",
                    "session": b.get("session"),
                    "url": b.get("openstates_url") or "",
                    "votes": chambers,
                })
        print("  page", page, "| bills so far with floor votes:", len(kept))
        if len(results) < 20 or len(kept) >= STOP_AT:
            break
    return trim(kept)


def main():
    if not API_KEY:
        print("OPEN_STATES_API_KEY is missing.")
        raise SystemExit(1)
    tmp = OUT_DIR + ".tmp"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    written = 0
    for abbr, state_name in STATES.items():
        print("State:", state_name)
        legs = fetch_legislators(state_name)
        if not legs or len(legs["upper"]) < 10 or len(legs["lower"]) < 40:
            print("Could not load the legislators. Keeping the old files.")
            raise SystemExit(1)
        print("  legislators - senate:", len(legs["upper"]), "| house:", len(legs["lower"]))
        bills = fetch_bills(state_name, legs)
        if bills is None:
            print("A request failed. Keeping the old files.")
            raise SystemExit(1)
        matched = total = 0
        for b in bills:
            for chamber, v in b["votes"].items():
                total += len(v["c"])
                matched += len(v["c"]) - v["c"].count("-")
        rate = round(100 * matched / total) if total else 0
        print("  bills kept:", len(bills), "| legislator votes matched:", rate, "percent")
        public = {chamber: [{"id": l["id"], "name": l["name"], "party": l["party"],
                             "district": l["district"], "family": l["family"]} for l in people]
                  for chamber, people in legs.items()}
        payload = {"generated": stamp, "state": abbr, "name": state_name,
                   "legislators": public, "bills": bills}
        with open(os.path.join(tmp, abbr + ".json"), "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        written += 1
    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.rename(tmp, OUT_DIR)
    print()
    print("Done. State files written:", written)


if __name__ == "__main__":
    main()
