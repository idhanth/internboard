"""Posting identity + persistence (SQLite, stdlib).

Identity (one row per real job, however many places list it):
  1. the source's own job ID, parsed from the job's URL (so a Simplify/SpeedyApply copy of a Greenhouse job gets the
     same key as the Greenhouse original), e.g. "gh:8172487", "workday:nvidia:JR2026170";
  2. only when there is no source ID: a fallback hash of normalized company + title + location + season/term, so
     next year's "Summer 2028" posting is never merged into this year's.
A posting without a source ID is matched to an existing posting with the same company/title/location/season (so the
list copy and the company's own copy merge), but sourced postings never merge on title alone: two requisitions with
the same title are two jobs.

Alerts: a posting alerts only when its row is first inserted, and only if no posting in the same card group
(same company + title, any city) has alerted before. Re-scrapes, reposts with the same ID and edits never re-alert.
"""
import hashlib, json, re, sqlite3, threading, time
from urllib.parse import parse_qsl, urlencode, urlsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL, company_norm TEXT NOT NULL, title TEXT NOT NULL, title_norm TEXT NOT NULL,
  location TEXT NOT NULL DEFAULT '', loc_norm TEXT NOT NULL DEFAULT '', season TEXT NOT NULL DEFAULT '',
  group_key TEXT NOT NULL,                  -- card group: company + title words (all cities of one role)
  url TEXT NOT NULL, source TEXT NOT NULL,  -- best apply link (company's own system preferred over a list's copy)
  posted_at INTEGER, posted_precision TEXT NOT NULL DEFAULT 'none',   -- exact | day | at_least | listed | none
  first_seen_at INTEGER NOT NULL, first_seen_baseline INTEGER NOT NULL DEFAULT 0,
  last_seen_at INTEGER NOT NULL, content_hash TEXT NOT NULL,
  closed_at INTEGER, notified_at INTEGER
);
CREATE INDEX IF NOT EXISTS postings_group ON postings(group_key);
CREATE INDEX IF NOT EXISTS postings_match ON postings(company_norm, title_norm, loc_norm, season);
CREATE INDEX IF NOT EXISTS postings_first_seen ON postings(first_seen_at);
CREATE INDEX IF NOT EXISTS postings_posted ON postings(posted_at);
CREATE TABLE IF NOT EXISTS posting_keys (key TEXT PRIMARY KEY, posting_id INTEGER NOT NULL REFERENCES postings(id));
CREATE INDEX IF NOT EXISTS posting_keys_posting ON posting_keys(posting_id);
CREATE TABLE IF NOT EXISTS alert_log (
  id INTEGER PRIMARY KEY, posting_id INTEGER REFERENCES postings(id), group_key TEXT, kind TEXT, sent_at INTEGER, title TEXT
);
"""

# ---------------------------------------------------------------- normalization
IDENTITY_PARAMS = {"gh_jid", "jobid", "job_id", "jobreqid", "reqid", "jk", "id", "job", "pid", "token"}


def normalize_url(url):
    """Lowercase host, no scheme/www/fragment/trailing slash, and no query params except ones that identify the job
    (gh_jid, jobId, ...). Tracking and presentation params (utm_*, ref, ?a=, ats=, feedId, mobile=...) are dropped."""
    p = urlsplit(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    keep = sorted((k.lower(), v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() in IDENTITY_PARAMS)
    path = re.sub(r"/+$", "", p.path)
    return host + path + ("?" + urlencode(keep) if keep else "")


SOURCE_ID = [  # (name, regex over the normalized URL, key builder)
    ("gh", r"gh_jid=(\d+)", lambda m: f"gh:{m.group(1)}"),
    ("gh", r"greenhouse\.io/[\w-]+/jobs/(\d+)", lambda m: f"gh:{m.group(1)}"),
    ("lever", r"jobs\.lever\.co/[\w.-]+/([0-9a-f-]{36})", lambda m: f"lever:{m.group(1)}"),
    ("ashby", r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{36})", lambda m: f"ashby:{m.group(1)}"),
    ("rippling", r"ats\.rippling\.com/[\w-]+/jobs/([0-9a-f-]{36})", lambda m: f"rippling:{m.group(1)}"),
    ("workday", r"([\w-]+)\.wd\d+\.myworkdayjobs\.com/.*/job/.*_([A-Za-z]*[-_]?\d[\w-]*)$",
     lambda m: f"workday:{m.group(1)}:{m.group(2).upper()}"),
    ("workday", r"wd\d+\.myworkdaysite\.com/.*recruiting/([\w-]+)/.*/job/.*_([A-Za-z]*[-_]?\d[\w-]*)$",
     lambda m: f"workday:{m.group(1)}:{m.group(2).upper()}"),
    ("sr", r"smartrecruiters\.com/[\w-]+/(\d{6,})", lambda m: f"sr:{m.group(1)}"),
    ("oracle", r"([\w-]+\.fa\.[\w-]+\.oraclecloud\.com)/.*/job/(\d+)", lambda m: f"oracle:{m.group(1)}:{m.group(2)}"),
    ("workable", r"apply\.workable\.com/(?:[\w-]+/)?j/([0-9a-f]{10})", lambda m: f"workable:{m.group(1)}"),
    ("breezy", r"([\w-]+)\.breezy\.hr/p/([0-9a-f]{12})", lambda m: f"breezy:{m.group(1)}:{m.group(2)}"),
    ("gem", r"jobs\.gem\.com/[\w-]+/(\d+|[0-9a-f-]{36})", lambda m: f"gem:{m.group(1)}"),
    ("teamtailor", r"([\w-]+)\.teamtailor\.com/jobs/(\d+)", lambda m: f"teamtailor:{m.group(1)}:{m.group(2)}"),
    ("amazon", r"amazon\.jobs/(?:\w+/)?jobs/(\d+)", lambda m: f"amazon:{m.group(1)}"),
    ("tiktok", r"lifeattiktok\.com/search/(\d+)", lambda m: f"tiktok:{m.group(1)}"),
    ("google", r"google\.com/about/careers/applications/jobs/results/(\d+)", lambda m: f"google:{m.group(1)}"),
    ("phenom", r"([\w.-]+)/(?:global|[a-z]{2})/[a-z]{2}/job/([\w-]+)", lambda m: f"phenom:{m.group(1)}:{m.group(2).lower()}"),
    # generic URL shapes last, so a specific platform above always wins
    ("icims", r"([\w.-]+)/jobs/(\d+)(?:/|$)", lambda m: f"icims:{m.group(1)}:{m.group(2)}"),
    ("sf", r"([\w.-]+)/job/[^/]+/(\d{7,})$", lambda m: f"sf:{m.group(1)}:{m.group(2)}"),
]


def source_key(url):
    u = normalize_url(url)
    for _, pat, build in SOURCE_ID:
        if m := re.search(pat, u, re.I):
            return build(m)
    return None


SUFFIX = re.compile(r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|holdings|the)\b")


def company_norm(name):
    s = re.sub(r"&", " and ", (name or "").lower())
    s = SUFFIX.sub(" ", re.sub(r"[^\w\s&]", " ", s))
    return re.sub(r"\s+", " ", s).strip()


SEASON = re.compile(r"\b(summer|fall|autumn|winter|spring)\b\W{0,3}(20\d\d)|\b(20\d\d)\W{0,3}(summer|fall|autumn|winter|spring)\b", re.I)


def season_of(title, terms=""):
    """The posting's stated term, e.g. "summer 2027"; a bare year if only that is stated; "" if neither."""
    for text in (title, terms or ""):
        if m := SEASON.search(text):
            s, y = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            return f"{'fall' if s.lower() == 'autumn' else s.lower()} {y}"
    if m := re.search(r"\b(20\d\d)\b", f"{title} {terms or ''}"):
        return m.group(1)
    return ""


NOISE = {"intern", "internship", "interns", "co", "op", "coop", "the", "and", "summer", "fall", "autumn", "winter",
         "spring", "hq", "us", "usa"}


def title_norm(title):
    words = re.findall(r"[a-z0-9+#]+", re.sub(r"engineering", "engineer", (title or "").lower()))
    return " ".join(sorted(set(w for w in words if w not in NOISE and not re.fullmatch(r"20\d\d", w))))


def loc_norm(location):
    first = re.split(r"[,;|·(]", location or "")[0].strip().lower()
    return {"nyc": "new york", "sf": "san francisco", "la": "los angeles"}.get(first, first)


def fallback_key(company, title, location, terms=""):
    raw = "|".join((company_norm(company), title_norm(title), loc_norm(location), season_of(title, terms)))
    return "fb:" + hashlib.sha1(raw.encode()).hexdigest()[:20]


def group_key(company, title):
    return company_norm(company) + "|" + title_norm(title)


def content_hash(j):
    return hashlib.sha1(json.dumps([j.get("title"), j.get("location"), j.get("url")], sort_keys=True).encode()).hexdigest()[:16]


LIST_SOURCES = {"Simplify", "SpeedyApply", "GitHub"}


# ---------------------------------------------------------------- planning (pure: used by dry run and by sync)
def plan(raw_jobs):
    """Group raw job listings into postings without touching any database.
    Returns (postings, stats): postings are dicts with their member listings and identity keys."""
    by_key, postings, by_match = {}, [], {}
    for j in raw_jobs:
        sk = source_key(j["url"])
        uk = "url:" + normalize_url(j["url"])
        keys = [k for k in (sk, uk) if k]
        match = (company_norm(j["company"]), title_norm(j["title"]), loc_norm(j.get("location", "")),
                 season_of(j["title"], j.get("terms", "")))
        p = next((by_key[k] for k in keys if k in by_key), None)
        if p is None and not sk:  # no source ID: match an existing posting on company/title/location/season
            p = by_match.get(match)
            keys.append(fallback_key(j["company"], j["title"], j.get("location", ""), j.get("terms", "")))
        if p is None:
            p = {"keys": set(), "members": [], "match": match, "has_source_id": bool(sk)}
            postings.append(p)
            by_match.setdefault(match, p)
        p["keys"].update(keys)
        p["members"].append(j)
        for k in keys:
            by_key[k] = p
    for p in postings:  # best listing: the company's own system beats a list's copy; then exact dates
        p["best"] = min(p["members"], key=lambda j: (j["source"] in LIST_SOURCES, bool(j.get("approx")), -(j.get("posted") or 0)))
        p["companies"] = sorted({j["company"] for j in p["members"]})
    return postings


def dry_run_report(raw_jobs, card_key):
    """What a cleanup would do, without writing: merges, cards removed, company-name merges."""
    postings = plan(raw_jobs)
    merged = [p for p in postings if len(p["members"]) > 1]
    cards_before = len({card_key(j) for j in raw_jobs})
    cards_after = len({(company_norm(p["best"]["company"]), title_norm(p["best"]["title"])) for p in postings})
    name_merges = {}
    for p in postings:
        norms = {company_norm(c) for c in p["companies"]}
        if len(norms) > 1:
            name_merges.setdefault(tuple(sorted(p["companies"])), []).append(p["best"]["title"])
    by_reason = {"same source job ID": 0, "same normalized URL": 0, "no ID: same company/title/location/season": 0}
    for p in merged:
        ks = p["keys"]
        if any(not k.startswith(("url:", "fb:")) for k in ks) and len({source_key(j["url"]) for j in p["members"]} - {None}) == 1 \
                and len({normalize_url(j["url"]) for j in p["members"]}) > 1:
            by_reason["same source job ID"] += 1
        elif len({normalize_url(j["url"]) for j in p["members"]}) == 1:
            by_reason["same normalized URL"] += 1
        else:
            by_reason["no ID: same company/title/location/season"] += 1
    return {"raw_listings": len(raw_jobs), "postings": len(postings), "postings_with_merges": len(merged),
            "listings_merged_away": len(raw_jobs) - len(postings), "merge_reasons": by_reason,
            "cards_before": cards_before, "cards_after": cards_after, "cards_removed": cards_before - cards_after,
            "company_name_merges": [{"names": list(k), "example_titles": v[:3], "postings": len(v)}
                                    for k, v in sorted(name_merges.items(), key=lambda x: -len(x[1]))],
            "with_source_id": sum(p["has_source_id"] for p in postings),
            "fallback_only": sum(not p["has_source_id"] for p in postings)}


# ---------------------------------------------------------------- persistence
class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.key_map = dict(self.db.execute("SELECT key, posting_id FROM posting_keys"))
        self.match_map = {tuple(r[1:]): r[0] for r in self.db.execute(
            "SELECT id, company_norm, title_norm, loc_norm, season FROM postings")}
        self.alerted_groups = {r[0] for r in self.db.execute("SELECT group_key FROM postings WHERE notified_at IS NOT NULL")}
        self.seen_touch = {}  # posting id -> last time last_seen_at was written (throttled to hourly)

    def sync(self, raw_jobs, now=None, baseline_urls=frozenset()):
        """Insert new postings, attach new keys to known ones, refresh changed ones. Returns the NEW postings
        (dicts with id, group_key, ...). Idempotent: syncing the same listings again inserts nothing."""
        now = int(now or time.time())
        new = []
        with self.lock:
            self.db.execute("BEGIN")
            for p in plan(raw_jobs):
                j = p["best"]
                pid = next((self.key_map[k] for k in p["keys"] if k in self.key_map), None)
                if pid is None and not p["has_source_id"]:
                    pid = self.match_map.get(p["match"])
                if pid is None:
                    gk = group_key(j["company"], j["title"])
                    cur = self.db.execute(
                        "INSERT INTO postings (company, company_norm, title, title_norm, location, loc_norm, season, group_key,"
                        " url, source, posted_at, posted_precision, first_seen_at, first_seen_baseline, last_seen_at, content_hash)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (j["company"], p["match"][0], j["title"], p["match"][1], j.get("location", ""), p["match"][2],
                         p["match"][3], gk, j["url"], j["source"], j.get("posted") or None,
                         j.get("precision") or ("day" if j.get("approx") else "exact" if j.get("posted") else "none"),
                         now, int(any(m["url"] in baseline_urls for m in p["members"])), now, content_hash(j)))
                    pid = cur.lastrowid
                    self.match_map.setdefault(p["match"], pid)
                    new.append({"id": pid, "group_key": gk, "url": j["url"], "company": j["company"], "title": j["title"],
                                "baseline": any(m["url"] in baseline_urls for m in p["members"])})
                else:
                    h = content_hash(j)
                    row = self.db.execute("SELECT content_hash FROM postings WHERE id=?", (pid,)).fetchone()
                    if row and row[0] != h:  # edited: refresh, never re-alert
                        self.db.execute("UPDATE postings SET title=?, location=?, url=?, source=?, content_hash=?, last_seen_at=? WHERE id=?",
                                        (j["title"], j.get("location", ""), j["url"], j["source"], h, now, pid))
                        self.seen_touch[pid] = now
                    elif now - self.seen_touch.get(pid, 0) > 3600:
                        self.db.execute("UPDATE postings SET last_seen_at=? WHERE id=?", (now, pid))
                        self.seen_touch[pid] = now
                for k in p["keys"]:
                    if k not in self.key_map:
                        self.db.execute("INSERT OR IGNORE INTO posting_keys (key, posting_id) VALUES (?,?)", (k, pid))
                        self.key_map[k] = pid
            self.db.execute("COMMIT")
        return new

    def posting_for_url(self, url):
        return self.key_map.get(source_key(url) or "") or self.key_map.get("url:" + normalize_url(url))

    def should_alert(self, posting):
        """First insert only, and only the first posting of a card group (one alert per role, not per city)."""
        return not posting.get("baseline") and posting["group_key"] not in self.alerted_groups

    def mark_alerted(self, posting, kind="posting", now=None):
        now = int(now or time.time())
        with self.lock:
            self.db.execute("UPDATE postings SET notified_at=? WHERE id=? AND notified_at IS NULL", (now, posting["id"]))
            self.db.execute("INSERT INTO alert_log (posting_id, group_key, kind, sent_at, title) VALUES (?,?,?,?,?)",
                            (posting["id"], posting["group_key"], kind, now, f"{posting['company']}: {posting['title']}"))
            self.alerted_groups.add(posting["group_key"])

    def import_alerted_urls(self, urls, now=None):
        """Carry over alerts sent before the database existed, so nothing already alerted alerts again."""
        now, hit = int(now or time.time()), 0
        for u in urls:
            pid = self.posting_for_url(u)
            if pid:
                gk = self.db.execute("SELECT group_key FROM postings WHERE id=?", (pid,)).fetchone()[0]
                self.db.execute("UPDATE postings SET notified_at=COALESCE(notified_at, ?) WHERE id=?", (now, pid))
                self.alerted_groups.add(gk)
                hit += 1
        return hit


if __name__ == "__main__":  # python3 store.py dry-run   (on the server; reads the live board, writes nothing)
    import sys, urllib.request
    if sys.argv[1:] == ["dry-run"]:
        data = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/admin/raw", timeout=120))
        card = lambda j: (j["company"].lower(), " ".join(sorted(set(re.findall(r"[a-z0-9+#]+", re.sub(
            r"engineering", "engineer", j["title"].lower()))) - {"intern", "internship", "co", "op", "summer", "fall",
            "winter", "spring", "2026", "2027", "us", "usa", "the", "and", "hq"})))  # the board's current card grouping
        report = dry_run_report(data["listings"], card)
        postings = plan(data["listings"])
        key_of = {}
        for p in postings:
            for k in p["keys"]:
                key_of[k] = p
        mapped = sum(1 for u in data["alerted_urls"] if key_of.get(source_key(u) or "") or key_of.get("url:" + normalize_url(u)))
        report["old_alerts"] = {"total": len(data["alerted_urls"]), "matched_to_a_current_posting": mapped}
        print(json.dumps(report, indent=1))
