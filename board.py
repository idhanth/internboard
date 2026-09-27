"""Software-intern board + application tracker. Serves http://localhost:8000.
Run: python3 board.py   (stdlib only)

Sources:
  - SimplifyJobs + SpeedyApply lists (their links also reveal company job boards)
  - boards.json: company job boards found in Common Crawl by discover.py
  - Every Greenhouse / Lever / Ashby / SmartRecruiters / Workable / Recruitee / Breezy / Gem / Teamtailor / Workday
    board above, polled directly,
    plus Amazon's jobs API.
Each board is checked on its own schedule by tier (see scheduler()): companies with SWE-internship history every
minute, other intern-posting companies every 4 min, the long tail every 15 min. Aggregator lists every 10 min.
"""
import calendar, functools, gzip, html, json, os, random, re, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from discover import board_of, OUT as DISCOVERED
import signals as community

HERE = Path(__file__).parent
# Settings that must not live in code or git (e.g. the ntfy topic): KEY=VALUE lines in .env next to this file
for line in (HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # empty = phone alerts off
SITE_URL = os.environ.get("SITE_URL", "http://localhost:8000")


def save(path, obj, **kw):
    """Write JSON atomically (temp file + rename) so a reader or a crash never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(obj, **kw)
    tmp.write_text(data)
    os.replace(tmp, path)
    STATS["disk_bytes_written"] += len(data)
    STATS["disk_writes"] += 1


# ---------------------------------------------------------------- measurement (/api/stats)
# Every HTTP request is counted per platform via a thin wrapper around urlopen; the calling thread says which
# platform it's working for (tl.kind). Plus rebuild timings/overlap and disk writes. Counters reset on restart.
STATS = {"since": time.time(), "http": {}, "disk_bytes_written": 0, "disk_writes": 0, "rebuilds": 0, "rebuild_seconds": 0.0,
         "rebuild_jobs_scanned": 0, "rebuild_overlaps": 0, "rebuilds_running": 0, "loops": {}, "latency": {}}
tl = threading.local()
_urlopen = urllib.request.urlopen


def _counting_urlopen(req, *a, **k):
    """Counts requests/bytes per platform, and asks for gzip (job feeds are text: ~5-8x smaller on the wire)."""
    st = STATS["http"].setdefault(getattr(tl, "kind", "other"), {"requests": 0, "bytes": 0, "errors": 0, "not_modified": 0})
    st["requests"] += 1
    if isinstance(req, str):
        req = urllib.request.Request(req)
    if not req.has_header("Accept-encoding"):
        req.add_header("Accept-Encoding", "gzip")
    try:
        resp = _urlopen(req, *a, **k)
    except urllib.error.HTTPError as e:
        st["not_modified" if e.code == 304 else "errors"] += 1
        raise
    except Exception:
        st["errors"] += 1
        raise
    read, zipped = resp.read, resp.headers.get("Content-Encoding", "") == "gzip"

    def counted(*ra, **rk):
        data = read()
        st["bytes"] += len(data)
        return gzip.decompress(data) if zipped else data
    resp.read = counted
    return resp


urllib.request.urlopen = _counting_urlopen
TRACKER = HERE / "tracker.json"
SEEN = HERE / "seen.json"  # first time we saw jobs whose source has no exact post time (Workday)
LISTS = ["https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
         "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
         "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json"]
SPEEDYAPPLY = ["https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md",
               "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/INTERN_INTL.md"]
DEAD_FOR = 86400  # a board that 404s is skipped for a day
# Seeds so big names are covered even if they drop out of the lists.
SEED = {"greenhouse": {"stripe", "airbnb", "databricks", "robinhood", "figma", "discord", "coinbase", "cloudflare",
                       "roblox", "datadog", "anthropic", "pinterest", "lyft", "dropbox", "reddit", "asana", "brex"},
        "lever": {"palantir", "plaid", "spotify"},
        "ashby": {"ramp", "openai", "notion", "linear", "vercel", "perplexity", "cursor", "replit"},
        "smartrecruiters": set(), "workday": set(), "workable": set(), "recruitee": set(),
        "breezy": set(), "gem": set(), "teamtailor": set(), "icims": set(), "oracle": set(),
        "successfactors": set(), "phenom": set(), "rippling": set(), "jibe": set()}
# Companies found by hand (not on any list yet): board -> company name
SEED_BOARDS = {("greenhouse", "riotgames"): "Riot Games", ("workday", ("mitre", "wd5", "MITRE")): "MITRE",
               ("phenom", ("careers.synovus.com", "us/en")): "Synovus", ("oracle", ("eluq.fa.us2.oraclecloud.com", "CX_2001")): "Kroger",
               ("jibe", "careers.amd.com"): "AMD", ("jibe", "jobs.statefarm.com"): "State Farm",
               ("jibe", "careers.jhuapl.edu"): "Johns Hopkins APL", ("jibe", "careers.chick-fil-a.com"): "Chick-fil-A"}
for (k, b), n in SEED_BOARDS.items():
    SEED[k].add(b)

INTERN = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b", re.I)
# Strictly software engineering: AI/ML/data roles only count if the title also says software/SWE/developer.
SWE = re.compile(r"software|\bswe\b|\bsde\b|developer|back[- ]?end|front[- ]?end|full[- ]?stack|web (dev|eng)"
                 r"|\bios\b|android|mobile (app|dev|eng)|programmer", re.I)
NOT_SWE = re.compile(r"sales|marketing|recruit|business develop|account(ing|ant)|\bhr\b|legal|developer (relations|advocate)|asset management"
                     r"|mechanical|electrical|civil|manufacturing|field eng|construction|biomedical|sales eng"
                     r"|\bbi\b[\s/]*developer", re.I)
# Titles that are often-but-not-always software: never excluded outright, never trusted outright. The job
# description decides whether the work is mainly software (see desc_score).
SOFT = re.compile(r"network|hardware|embedded|firmware|\bsystems?\b", re.I)
# Tech programs that hide SWE work behind broad titles ("Technology Analyst Intern", "IT Application Development
# Intern", "Engineering Intern"): shown only if the job description reads like software development.
TECH = re.compile(r"technolog|\btech\b|\bit\b|information (systems|technology)|applications? (dev|eng|sys)|digital|computer science"
                  r"|\bcs\b|automation|cloud|platform|infrastructure|product eng|devops|site reliability|\bsre\b|engineering intern"
                  r"|rotational|development program"
                  # data / ML / AI / security engineering: in scope only when the description is real software work
                  r"|data eng|data platform|data infrastructure|machine learning|\bml\b|mlops|\bai\b|artificial intelligence|\bllm"
                  r"|security eng|security software|product security|application security|appsec|cyber", re.I)
NOT_TECH = re.compile(r"help ?desk|desktop support|technician|clinical|nurs|pharm|audit|financ|supply chain|hr tech"
                      # analyst roles are out, except bank-style tech programs ("Technology Analyst") - description decides those
                      r"|(?<!technology )(?<!software )(?<!programmer )(?<!systems )analyst|analytics|data scien|scientist|research|statistic"
                      r"|business intel|\bbi\b|governance|compliance"
                      r"|\bgrc\b|\brisk\b|security operations|\bsoc\b|penetration test|threat intel|policy|annotat|labell?ing|prompt writ"
                      r"|consult|methods|property|project delivery|operations|solutions eng|general", re.I)
LANGS = re.compile(r"python|\bjava\b|c\+\+|c#|\.net|javascript|typescript|golang|\bgo\b|kotlin|swift|rust|ruby|scala", re.I)
# Strong software signals, grouped so one noisy group can't carry a score on its own.
SIGNALS = {
    "lang": LANGS,
    "web": re.compile(r"\bapis?\b|rest(ful)?\b|graphql|microservices?|web (services|applications?)", re.I),
    "stack": re.compile(r"back[- ]?end|front[- ]?end|full[- ]?stack|react|angular|vue|node\.?js|django|flask|spring boot|\.net core", re.I),
    "db": re.compile(r"\bsql\b|postgres\w*|mysql|mongodb|nosql|databases?", re.I),
    "vcs": re.compile(r"\bgit(hub|lab)?\b|version control|code reviews?", re.I),
    "cloud": re.compile(r"\baws\b|azure|\bgcp\b|docker|kubernetes|ci/cd|devops", re.I),
    "dev": re.compile(r"software (development|engineering)|develop(ing)? (software|applications|features)|write (clean )?code"
                      r"|coding|programming|object[- ]oriented|unit test\w*|debug\w*|agile|scrum", re.I),
}
# Signals that the job is mostly something else (IT support, hardware bench work, network admin).
NOT_SOFTWARE = re.compile(r"help ?desk|troubleshoot\w* (hardware|end[- ]user|laptops|printers)|desktop support|ticketing|imaging"
                          r"|schematics?|\bpcb\b|circuit|oscilloscope|solder\w*|multimeter|\bfpga\b|verilog|vhdl|analog"
                          r"|cabling|wiring|ccna|routers?\b|switch(es)? config\w*|lan/wan|test equipment|lab equipment"
                          # analysis / research rather than building software
                          r"|tableau|power bi|looker|dashboards?|publications?|publish(ing)? (papers|research)|statistical (analysis|model\w*)"
                          r"|a/b test analysis|literature review", re.I)
CODE = re.compile("|".join(p.pattern for p in SIGNALS.values()), re.I)
ATS_HOSTS = re.compile(r"greenhouse|lever\.co|ashbyhq|smartrecruiters|myworkday|icims|workable|jobvite|oraclecloud"
                       r"|eightfold|rippling|applytojob|bamboohr|recruitee|breezy|taleo|successfactors|dayforce|paylocity|adp")

jobs, status = [], {"polled": 0, "boards": 0, "ok": 0}
cache = {}   # (kind, board) -> last good job list; kept when a fetch fails so jobs don't flicker away
lists, boards, names = [], {}, {}  # list-sourced jobs, discovered boards, (kind, board) -> company name
sites, salaries = {}, {}  # company (lower) -> real website domain; dedupe key or company -> "$53/hr" (from SpeedyApply)
first_seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}  # url -> ts
DETECTED = HERE / "detected.json"  # job url -> when this board first saw it (independent of the source's own date)
detected = json.loads(DETECTED.read_text()) if DETECTED.exists() else {}
health = {}  # (kind, board) -> {"ok": last success ts, "err": last error ts, "why": error, "ms": response ms, "n": jobs}
CLOSED = HERE / "closed.json"  # url -> last known job + "closed" ts; shown as Closed for CLOSED_KEEP
closed = json.loads(CLOSED.read_text()) if CLOSED.exists() else {}
CLOSED_KEEP = 14 * 86400
# Sources whose feed is a company's *complete* job list, so a job vanishing from it really means it closed.
# (Keyword searches / newest-N feeds like Workday, iCIMS, Oracle, SuccessFactors can't tell closed from pushed out.)
COMPLETE = {"greenhouse", "lever", "ashby", "workable", "recruitee", "breezy", "gem", "teamtailor", "rippling"}
WATCH = HERE / "watch.json"  # starred companies (lowercase names)
BATCH_WINDOW, BATCH_MIN = 3600, 4  # a company detecting 4+ new roles within an hour = batch drop
SIGNALS_FILE = HERE / "signals.json"   # community signal id -> signal (kept 30 days)
HISTORY = HERE / "history.json"   # company -> when it first posted a SWE internship last cycle (from Simplify's git history)
sig_store = json.loads(SIGNALS_FILE.read_text()) if SIGNALS_FILE.exists() else {}
page_seen = {}
submissions = []  # "New Internship" GitHub issues: real postings, often before the lists merge them
intern_activity = {}  # company (lower) -> non-SWE internship roles it detected in the last 30 days
PAID = re.compile(r"\$\s?\d{2,3}(\.\d{1,2})?\s*(/|per|an)\s*(hr|hour)|\$\s?\d{2,3},\d{3}|hourly (rate|pay|wage)|\bpaid (intern|position|role)"
                  r"|stipend|salary range|pay range|base pay|compensation range|hourly range", re.I)
UNPAID = re.compile(r"\bunpaid\b|\bvolunteer\b|academic credit only|for (academic|course) credit|no (monetary )?compensation", re.I)
pay_info = {}  # job url -> "paid" | "unpaid", from any description text we've seen; absent = unknown
VERDICTS = HERE / "verdicts2.json"  # hidden-SWE job url -> {"s": 0-100 score, "why": [matched skills]} (checked once)
verdicts = json.loads(VERDICTS.read_text()) if VERDICTS.exists() else {}
hot, dead = set(), {}
SOURCES = HERE / "sources.json"  # registry state that must survive restarts: SWE history (tier 1) and failure counts
_as_key = lambda k, b: (k, tuple(b) if isinstance(b, list) else b)
shown_urls = {}  # url -> job currently on the board (for closed-job detection)  # boards with SWE intern roles; 404'd boards -> when
tracker_lock = threading.Lock()


class NotModified(Exception):
    """The source answered 304: nothing changed since our last look, so there's nothing to download or process."""


etags = {}  # url -> ETag from the last full response


def http(url, body=None, headers=None, etag=False):
    """GET/POST JSON. etag=True sends If-None-Match and raises NotModified on 304. Only for single-request
    fetchers, where "this URL is unchanged" means "this board is unchanged"."""
    hdrs = {"User-Agent": "Mozilla/5.0 internboard", "Content-Type": "application/json", **(headers or {})}
    if etag and url in etags:
        hdrs["If-None-Match"] = etags[url]
    req = urllib.request.Request(url, json.dumps(body).encode() if body else None, hdrs)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if etag and r.headers.get("ETag"):
                etags[url] = r.headers["ETag"]
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 304:
            raise NotModified() from None
        raise


def iso(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def job(company, title, location, url, posted, source, approx=False):
    """approx=True: source only gives the day ("Posted 3 Days Ago"), so the UI shows days, not hours.
    posted=0: source gives no date at all."""
    p = urlsplit(url)  # drop tracking params (Simplify appends ?utm_source=Simplify&ref=Simplify) -> clean ATS link
    url = urlunsplit(p._replace(query=urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                                                 if not re.fullmatch(r"utm_\w+|ref|gh_src", k)])))
    location = re.sub(r"\s*(,\s*)+", ", ", (location or "").strip(" ,"))
    return {"company": company, "title": title.strip(), "location": location, "url": url,
            "posted": int(posted), "source": source, "approx": approx}


def midnight(days_ago=0):
    t = time.localtime(time.time() - 86400 * days_ago)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def speedyapply(url):
    """Rows of the markdown tables: | <a href=site><strong>Co</strong></a> | Title | Location | [Salary |] <a href=apply> | 3d |"""
    req = urllib.request.Request(url, headers={"User-Agent": "internboard"})
    with urllib.request.urlopen(req, timeout=20) as r:
        md = r.read().decode()
    out = []
    for line in md.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.startswith("| <a") or len(cells) not in (5, 6):
            continue
        site, company = re.search(r'href="([^"]+)".*?<strong>(.*?)</strong>', cells[0]).groups()
        apply = re.search(r'href="([^"]+)"', cells[-2]).group(1)
        days = int(re.sub(r"\D", "", cells[-1]) or 0)
        j = tag(job(company, cells[1], cells[2], apply, midnight(days), "SpeedyApply", True), desc={"s": 85, "why": ["SpeedyApply SWE list"]})
        sites[company.lower()] = re.sub(r"^www\.", "", site.split("/")[2].lower())
        if len(cells) == 6 and cells[3].startswith("$"):
            salaries[dedupe_key(j)] = salaries[company.lower()] = cells[3]  # per role, and per company as fallback
        out.append(j)
    return out


list_cache = {}  # list url -> last parsed rows (reused on 304)


def fetch_lists():
    out, boards, every = [], {k: set(v) for k, v in SEED.items()}, []
    for u in LISTS:
        try:
            rows = list_cache[u] = http(u, etag=True)
        except NotModified:
            rows = list_cache[u]
        every += [tag(job(j["company_name"], j["title"], ", ".join(j.get("locations", [])), j["url"], j["date_posted"], "Simplify")
                      | {"terms": ", ".join(j.get("terms") or ([j["season"]] if j.get("season") else []))},
                      desc={"s": 80, "why": ["Simplify: Software"]} if "Software" in (j.get("category") or "") else "")
                  | {"live": j.get("active") and j.get("is_visible", True)} for j in rows]
    out = [j for j in every if j.pop("live")]
    live_urls = {j["url"] for j in out}
    for j in every:  # a list marking a role inactive = closed
        if j["url"] not in live_urls and j["url"] in shown_urls and j["url"] not in closed:
            closed[j["url"]] = {**shown_urls[j["url"]], "closed": int(time.time())}
    now, baseline = int(time.time()), not lists  # first load of the lists: their jobs aren't new to anyone
    for j in out:
        detected.setdefault(j["url"], j["posted"] if baseline else now)
    out += [job(x["company"], x["title"], x["location"], x["url"], x["posted"], "GitHub")
            for x in submissions if x["open"] and time.time() - x["posted"] < 30 * 86400]
    for u in SPEEDYAPPLY:
        try:
            out += speedyapply(u)
        except Exception as e:  # markdown layout can change; don't take the whole board down
            print("speedyapply failed:", e, flush=True)
    for j in every + out:  # every link, even to closed jobs, is a clue to a company job board worth polling
        if key := board_of(j["url"]):
            boards[key[0]].add(key[1])
            names[key] = j["company"]
            hot.add(key)  # companies on intern lists: tier 2 at least
            if is_swe(j):
                swe_boards.add(key)  # has posted SWE internships: tier 1
    for key, n in SEED_BOARDS.items():
        names.setdefault(key, n)
        hot.add(key)
    if DISCOVERED.exists():
        for k, bs in json.loads(DISCOVERED.read_text()).items():
            boards.setdefault(k, set()).update(tuple(b) if isinstance(b, list) else b for b in bs)
    return out, boards


def greenhouse(s):
    return [tag(job(s, j["title"], j["location"]["name"], j["absolute_url"],
                    iso(j.get("first_published") or j["updated_at"]), "Greenhouse"),
                detail=f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs/{j['id']}")
            for j in http(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", etag=True)["jobs"]]


def lever(s):
    return [tag(job(s, j["text"], j["categories"].get("location"), j["hostedUrl"], j["createdAt"] / 1000, "Lever"),
                desc=" ".join(texts([j.get("descriptionPlain"), j.get("lists")])))
            for j in http(f"https://api.lever.co/v0/postings/{s}?mode=json", etag=True)]


def ashby(s):
    return [tag(job(s, j["title"], j.get("location"), j["jobUrl"], iso(j["publishedAt"]), "Ashby"),
                desc=j.get("descriptionPlain") or "")
            for j in http(f"https://api.ashbyhq.com/posting-api/job-board/{s}", etag=True)["jobs"]]


def smartrecruiters(s):
    content = {j["id"]: j for q in ("intern", "co-op")
               for j in http(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?q={q}&limit=100")["content"]}
    return [tag(job(j["company"]["name"], j["name"], j["location"].get("fullLocation"),
                    f"https://jobs.smartrecruiters.com/{s}/{j['id']}", iso(j["releasedDate"]), "SmartRecruiters"),
                detail=f"https://api.smartrecruiters.com/v1/companies/{s}/postings/{j['id']}")
            for j in content.values()]


def workday(b):
    """Workday only says "Posted Today / N Days Ago" (day precision; fetch() sharpens new ones to first-seen time)."""
    tenant, wd, site = b
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    # one page of the best matches per search; companies known to hire interns also get a "technology intern"
    # search for tech programs. ponytail: a company with >20 matches per search loses the tail
    posts = {}
    for q in ["software intern"] + (["technology intern", "software co-op"] if tier(("workday", b)) == 1 else []):
        for p in http(f"{base}/wday/cxs/{tenant}/{site}/jobs",
                      {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q}).get("jobPostings", []):
            posts[p["externalPath"]] = p
    out = []
    for j in posts.values():
        p = j.get("postedOn", "")
        n = re.search(r"(\d+)", p)
        posted = midnight(0 if "Today" in p else 1 if "Yesterday" in p else int(n.group(1)) if n else 30)
        out.append(tag(job(tenant, j["title"], j.get("locationsText"), f"{base}/{site}{j['externalPath']}", posted, "Workday", True),
                       detail=f"{base}/wday/cxs/{tenant}/{site}{j['externalPath']}"))
    return out


def workable(s):
    d = http(f"https://apply.workable.com/api/v1/widget/accounts/{s}")
    return [tag(job(d["name"], j["title"], ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")])),
                    j["url"], datetime.strptime(j["published_on"], "%Y-%m-%d").timestamp(), "Workable", True),
                detail=f"https://apply.workable.com/api/v2/accounts/{s}/jobs/{j['shortcode']}")
            for j in d["jobs"]]


def recruitee(s):
    return [tag(job(j.get("company_name") or s, j["title"], j.get("location"), j["careers_url"],
                    iso(j["published_at"].replace(" UTC", "+00:00").replace(" ", "T")), "Recruitee"),
                desc=(j.get("description") or "") + (j.get("requirements") or ""))
            for j in http(f"https://{s}.recruitee.com/api/offers/")["offers"]]


def amazon(_):
    d = http("https://www.amazon.jobs/en/search.json?base_query=software%20intern&result_limit=100&sort=recent")
    return [job("Amazon", j["title"], j["normalized_location"], "https://www.amazon.jobs" + j["job_path"],
                datetime.strptime(j["posted_date"], "%B %d, %Y").timestamp(), "Amazon", True) for j in d["jobs"]]


def breezy(s):
    return [job(s, j["name"], (j.get("location") or {}).get("name"), j["url"], iso(j["published_date"]), "Breezy")
            for j in http(f"https://{s}.breezy.hr/json")]


def gem(s):
    return [tag(job(s, j["title"], (j.get("location") or {}).get("name"), j["absolute_url"],
                    iso(j.get("first_published_at") or j["created_at"]), "Gem"), desc=j.get("content_plain") or "")
            for j in http(f"https://api.gem.com/job_board/v0/{s}/job_posts/")]


def teamtailor(s):
    out = []
    for j in http(f"https://{s}.teamtailor.com/jobs.json")["items"]:
        p = j.get("_jobposting") or {}
        addr = ((p.get("jobLocation") or [{}])[0].get("address") or {})
        loc = ", ".join(filter(None, [addr.get("addressLocality"), addr.get("addressCountry")]))
        company = (p.get("hiringOrganization") or {}).get("name") or s
        out.append(tag(job(company, j["title"], loc, j["url"], iso(j["date_published"]), "Teamtailor"),
                       desc=j.get("content_html") or p.get("description") or ""))
    return out


def oracle(b):
    """Oracle Recruiting Cloud's public candidate-experience API (newest first, day-precision dates)."""
    host, site = b
    reqs = {r["Id"]: r for q in ("intern", "co-op") for r in (http(
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
        f"&finder=findReqs;siteNumber={site},keyword={q},limit=50,sortBy=POSTING_DATES_DESC").get("items") or [{}])[0]
        .get("requisitionList", [])}
    return [tag(job(host.split(".")[0], r["Title"], r.get("PrimaryLocation"),
                    f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}",
                    datetime.strptime(r["PostedDate"], "%Y-%m-%d").timestamp(), "Oracle", True),
                desc=r.get("ShortDescriptionStr") or "")
            for r in reqs.values()]


def icims(sub):
    """iCIMS has no JSON API; read its (stable) search-results HTML. No dates: fetch() times new ones on sight."""
    page = ""
    for q in ("intern", "co-op"):
        host = sub if "." in sub else f"{sub}.icims.com"  # iCIMS on the company's own domain (careers.amd.com) or *.icims.com
        req = urllib.request.Request(f"https://{host}/jobs/search?ss=1&searchKeyword={q}&in_iframe=1",
                                     headers={"User-Agent": "Mozilla/5.0 internboard"})
        with urllib.request.urlopen(req, timeout=20) as r:
            page += r.read().decode("utf-8", "replace")
    out, seen_links = [], set()
    for card in page.split("iCIMS_JobCardItem")[1:]:
        link = re.search(r'href="(https://[^"]+/jobs/\d+/[^"]+/job)', card)
        title = re.search(r"<h3[^>]*>(.*?)</h3>", card, re.S)
        if not (link and title) or link.group(1) in seen_links:
            continue
        seen_links.add(link.group(1))
        loc = re.search(r"Job Locations</span>\s*<span[^>]*>(.*?)</span>", card, re.S)
        desc = re.search(r'class="col-xs-12 description">(.*?)</div>', card, re.S)
        out.append(tag(job(sub.split(".")[1] if "." in sub else sub.removeprefix("careers-"), html.unescape(title.group(1)).strip(),
                           html.unescape(loc.group(1)).strip() if loc else "", link.group(1), 0, "iCIMS", True),
                       desc=desc.group(1) if desc else ""))
    return out


def successfactors(host):
    """SAP SuccessFactors career sites (jobs.l3harris.com, careers.qorvo.com, ...): their RSS feed is the company's
    20 newest jobs with full descriptions. ponytail: a company posting >20 jobs between two checks loses the overflow."""
    req = urllib.request.Request(f"https://{host}/services/rss/job/?locale=en_US", headers={"User-Agent": "Mozilla/5.0 internboard"})
    with urllib.request.urlopen(req, timeout=20) as r:
        xml = r.read().decode("utf-8", "replace")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = re.search(r"<title><!\[CDATA\[(.*?)\]\]>", it, re.S).group(1)
        m = re.match(r"(.*?)\s*\(([^()]*)\)\s*$", title)
        link = html.unescape(re.search(r"<link>(.*?)</link>", it).group(1))
        when = datetime.strptime(re.search(r"<pubDate>(.*?)</pubDate>", it).group(1).strip(), "%a, %d %b %Y %H:%M:%S %Z")
        desc = re.search(r"<description><!\[CDATA\[(.*?)\]\]>", it, re.S)
        out.append(tag(job(host, m.group(1) if m else title, m.group(2) if m else "", link.split("?")[0],
                           calendar.timegm(when.timetuple()), "SuccessFactors", True), desc=desc.group(1) if desc else ""))
    return out


def phenom(b):
    """Phenom career sites (careers.cisco.com, careers.united.com, ...) embed their search results as JSON."""
    host, prefix = b
    out = {}
    for q in ("software intern", "technology intern", "intern"):
        req = urllib.request.Request(f"https://{host}/{prefix}/search-results?keywords={q.replace(' ', '%20')}",
                                     headers={"User-Agent": "Mozilla/5.0 internboard"})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", "replace")
        ddo = re.search(r"phApp\.ddo = (\{.*?\});\s*phApp", page, re.S)
        if not ddo:
            raise ValueError("not a Phenom site")
        for p in json.loads(ddo.group(1)).get("eagerLoadRefineSearch", {}).get("data", {}).get("jobs", []):
            url = re.sub(r"/apply$", "", p.get("applyUrl") or "") or f"https://{host}/{prefix}/job/{p['jobId']}"
            out[url] = tag(job(host, p["title"], p.get("location") or p.get("cityStateCountry"), url,
                               iso(p["postedDate"]) if p.get("postedDate") else 0, "Phenom", True),
                           desc=" ".join(texts([p.get("descriptionTeaser"), p.get("ml_skills")])))
    return list(out.values())


def jibe(host):
    """iCIMS/Jibe career sites on a company's own domain (careers.amd.com, jobs.statefarm.com, careers.jhuapl.edu):
    JSON API, newest first, exact posting times and full descriptions."""
    out = {}
    for q in ("intern", "co-op"):
        d = http(f"https://{host}/api/jobs?keywords={q}&page=1&limit=100&sortBy=posted_date&descending=true")
        for p in (x["data"] for x in d.get("jobs", [])):
            url = f"https://{host}/jobs/{p.get('slug') or p['req_id']}"
            out[url] = tag(job(p.get("hiring_organization") or host.split(".")[1], p["title"],
                               p.get("full_location") or p.get("location_name"), url,
                               iso(p["posted_date"].replace("+0000", "+00:00")) if p.get("posted_date") else 0, "iCIMS"),
                           desc=" ".join(filter(None, [p.get("description"), p.get("qualifications"), p.get("responsibilities")])))
    return list(out.values())


def rippling(slug):
    d = http(f"https://ats.rippling.com/api/v2/board/{slug}/jobs?page=0&pageSize=100")
    return [tag(job(slug, j["name"], "; ".join(l.get("name", "") for l in j.get("locations", [])), j["url"], 0, "Rippling", True),
                detail=f"https://ats.rippling.com/api/v2/board/{slug}/jobs/{j['id']}") for j in d.get("items", [])]


def tiktok(_):
    """TikTok's careers API (no posting time: new ones are timed on sight)."""
    out = {}
    for q in ("software engineer intern", "software development intern", "frontend intern", "backend intern"):
        d = http("https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts",
                 {"keyword": q, "limit": 100, "offset": 0, "recruitment_id_list": [], "job_category_id_list": [],
                  "subject_id_list": [], "location_code_list": []}, {"website-path": "tiktok"})
        for p in d["data"]["job_post_list"]:
            url = f"https://lifeattiktok.com/search/{p['id']}"
            out[url] = tag(job("TikTok", p["title"], (p.get("city_info") or {}).get("en_name"), url, 0, "TikTok", True),
                           desc=(p.get("description") or "") + (p.get("requirement") or ""))
    return list(out.values())


def google(_):
    """Google's careers results page, newest intern roles. ponytail: parses Google's HTML (class names can change);
    the Health tab will show it failing if they do."""
    req = urllib.request.Request("https://www.google.com/about/careers/applications/jobs/results?q=software&"
                                 "target_level=INTERN_AND_APPRENTICE&sort_by=date", headers={"User-Agent": "Mozilla/5.0 internboard"})
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", "replace")
    out = []
    for card in page.split('<h3 class="QJPWVe">')[1:]:
        link = re.search(r"jobs/results/(\d+-[\w-]+)", card)
        if not link:
            continue
        locs = [html.unescape(x).strip("; ") for x in re.findall(r'class="r0wTof[^"]*">([^<]+)', card)]
        out.append(tag(job("Google", html.unescape(card.split("</h3>")[0]), " · ".join(dict.fromkeys(locs)),
                           f"https://www.google.com/about/careers/applications/jobs/results/{link.group(1)}", 0, "Google", True),
                       desc=re.sub(r"<[^>]+>", " ", card[:6000])))
    if not out:
        raise ValueError("Google careers layout changed")
    return out


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
            "workday": workday, "workable": workable, "recruitee": recruitee, "breezy": breezy, "gem": gem, "teamtailor": teamtailor, "oracle": oracle, "icims": icims, "amazon": amazon,
            "successfactors": successfactors, "phenom": phenom, "jibe": jibe, "rippling": rippling, "tiktok": tiktok, "google": google}


def sharpen(key, res):
    """Day-precision sources: a job that wasn't there on our previous check of this board was posted since then,
    so record the time we first saw it (persisted in seen.json)."""
    prev, now = cache.get(key), int(time.time())
    prev_urls = {j["url"] for j in prev or ()}
    for j in res:
        if j["approx"] and prev is not None and j["url"] not in prev_urls and j["url"] not in first_seen \
                and (j["posted"] >= midnight(1) or j["posted"] == 0):
            first_seen[j["url"]] = now
        if j["url"] in first_seen:
            j["posted"], j["approx"] = first_seen[j["url"]], False
    return res


# Politeness: a 429 pauses the whole platform (Retry-After if given, else 30s doubling to 10 min); successes ease it
# back. A 403 is a deliberate block: that board is skipped for a day and never worked around.
cooldown, backoff = {}, {}  # kind -> paused-until ts, kind -> current backoff seconds
errlog = {}  # kind -> [(ts, "429" | "403" | "404" | "5xx" | "other")], pruned to the last hour


def note_error(kind, code):
    now = time.time()
    log = errlog.setdefault(kind, [])
    log.append((now, code))
    while log and log[0][0] < now - 3600:
        log.pop(0)


def classify(e):
    """What kind of failure this was, for the per-source report."""
    if isinstance(e, urllib.error.HTTPError):
        return {429: "rate_limited", 403: "blocked_403", 404: "not_found", 410: "not_found"}.get(e.code, f"http_{e.code}")
    if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
        return "timeout"
    if isinstance(e, (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError, IndexError)):
        return "parse_error"
    if isinstance(e, urllib.error.URLError):
        return "network_error"
    return "other_error"


def fetch(key):
    kind, b = key
    tl.kind = kind
    h = health.setdefault(key, {})
    if time.time() < cooldown.get(kind, 0):
        return  # platform is rate-limiting us: skip until the pause ends
    t = time.time()
    try:
        try:
            raw = FETCHERS[kind](b)
        except NotModified:  # 304: the board is exactly as we last saw it
            status["polled"] = int(t)
            h.update(ok=int(t), ms=int(1000 * (time.time() - t)), unchanged=h.get("unchanged", 0) + 1, fails=0,
                     status=h.get("status") if h.get("status") in ("ok", "zero_jobs") else "ok")
            return
        # Keep internships only. About 97% of postings are full-time roles we never show; holding and re-scanning
        # them was most of the CPU and RAM. Closed-job detection works on this subset too.
        res = [j for j in raw if INTERN.search(j["title"])]
        digest = hash(tuple((j["url"], j["title"], j["location"], j["posted"]) for j in res))
        status["polled"] = int(t)
        if key in cache and h.get("digest") == digest:  # same internships as last time: nothing to reprocess
            h.update(ok=int(t), ms=int(1000 * (time.time() - t)), n=len(raw), status="ok" if raw else "zero_jobs",
                     unchanged=h.get("unchanged", 0) + 1, fails=0)
            return
        h["digest"] = digest
        if res:
            intern_boards.add(key)
        baseline = key not in cache  # first check of this board: its jobs aren't "new", just newly known
        now = int(time.time())
        for j in res:
            if j["url"] not in detected and INTERN.search(j["title"]):  # only internships matter
                # first check of a board: its jobs aren't new. Dated ones keep their date; undated ones get 0
                # ("date unknown") so they never look fresh or trigger alerts/batches.
                detected[j["url"]] = j["posted"] if baseline else now
                if not baseline and j["posted"] and not j.get("approx") and 0 <= now - j["posted"] < 7 * 86400:
                    lat = STATS["latency"].setdefault(kind, [])
                    lat.append(now - j["posted"])
                    del lat[:-500]
        if kind in COMPLETE and key in cache:  # jobs that vanished from a complete feed have closed
            gone = {j["url"] for j in cache[key]} - {j["url"] for j in res}
            for j in cache[key]:
                if j["url"] in gone and j["url"] in shown_urls:
                    closed[j["url"]] = {**shown_urls[j["url"]], "closed": now}
        for j in res:
            closed.pop(j["url"], None)  # reposted / reopened
        cache[key] = sharpen(key, res)
        status["polled"] = int(t)
        h.update(ok=int(t), ms=int(1000 * (time.time() - t)), n=len(raw), interns=len(res), status="ok" if raw else "zero_jobs",
                 fails=0, changed=h.get("changed", 0) + 1)
        dirty.set()  # the rebuild thread will pick this up
        backoff[kind] = backoff.get(kind, 0) // 2  # ease back after successes
        return
    except urllib.error.HTTPError as e:
        h.update(err=int(t), why=f"HTTP {e.code}", status=classify(e))
        if e.code == 429:
            note_error(kind, "429")
            retry_after = e.headers.get("Retry-After", "")
            backoff[kind] = int(retry_after) if retry_after.isdigit() else min(600, max(30, backoff.get(kind, 0) * 2))
            cooldown[kind] = time.time() + backoff[kind]
        elif e.code == 403:
            note_error(kind, "403")
            dead[key] = time.time()  # blocked: respect it, skip this board for DEAD_FOR
        elif e.code in (404, 410):
            note_error(kind, "404")
            h["fails"] = h.get("fails", 0) + 1  # renamed/closed board: 3 in a row -> inactive, rechecked weekly
        else:
            note_error(kind, "5xx" if e.code >= 500 else "other")
        return
    except Exception as e:
        h.update(err=int(t), why=f"{type(e).__name__}: {str(e)[:120]}", status=classify(e), fails=h.get("fails", 0) + 1)
        note_error(kind, "other")
        return


def health_report():
    """Per source system: how many boards we know, how many answered recently, what's failing."""
    now, out = time.time(), {}
    for (kind, b), h in list(health.items()):
        r = out.setdefault(kind, {"known": len(boards.get(kind, ())), "checked": 0, "healthy": 0, "failing": 0,
                                  "dead": 0, "listings": 0, "ms": [], "last_ok": 0, "errors": {}, "examples": []})
        r["checked"] += 1
        if h.get("ok", 0) >= h.get("err", 0):
            r["healthy"] += 1
            r["listings"] += h.get("n", 0)
            r["ms"].append(h.get("ms", 0))
            r["last_ok"] = max(r["last_ok"], h.get("ok", 0))
        elif now - dead.get((kind, b), 0) < DEAD_FOR:
            r["dead"] += 1
        else:
            r["failing"] += 1
            r["errors"][h["why"]] = r["errors"].get(h["why"], 0) + 1
            if len(r["examples"]) < 8:
                r["examples"].append({"board": " / ".join(b) if isinstance(b, tuple) else b, "why": h["why"],
                                      "since": h["err"], "last_ok": h.get("ok", 0)})
    for kind, r in out.items():
        ms = sorted(r.pop("ms"))
        r["median_ms"] = ms[len(ms) // 2] if ms else None
        recent = [c for t, c in errlog.get(kind, []) if t > now - 3600]
        r.update(rate_limited_1h=recent.count("429"), blocked_1h=recent.count("403"),
                 errors_1h=sum(c in ("5xx", "other") for c in recent), paused_until=int(cooldown.get(kind, 0)) if cooldown.get(kind, 0) > now else 0)
    return out


# ---------------------------------------------------------------- scheduler
# Every board has its own next-check time; nothing is checked "all at once". Tiers:
#   T1  companies that have posted SWE internships (on the board now or before, seeds, watched): every TIER_S[1]
#   T2  companies that post internships of any kind (on the intern lists, or intern roles seen): every TIER_S[2]
#   T3  boards that have had internship postings before, or not checked yet: every TIER_S[3]
#   T4  checked, and have never shown a single internship posting: every TIER_S[4] (hourly)
# New boards get a random first slot inside their interval, and every check reschedules with +/-10% jitter, so load
# spreads evenly across time. Per-platform caps keep throttling platforms (Workday, Workable) polite.
TIER_S = {1: 60, 2: 240, 3: 900, 4: 3600}
WORKERS = 80  # 2 OCPUs: doubled from 40 (1 OCPU). Measured per run: see bench.py results
PLATFORM_CAP = {"workday": 4, "workable": 2, "recruitee": 2}  # concurrent requests per platform
INACTIVE_AFTER, INACTIVE_RETRY = 3, 7 * 86400  # 3 failures in a row -> checked weekly instead
swe_boards = set()  # boards that have ever had a SWE/hidden-SWE internship on the board (-> tier 1)
intern_boards = set()  # boards that have ever had any internship posting (-> never tier 4)
verified_boards = set()  # boards that have had a US, undergrad-eligible, SWE internship (the number that matters)
next_due, inflight = {}, set()
sched_lock = threading.Lock()


def tier(key):
    if key in swe_boards or key in SEED_BOARDS or key[0] in ("amazon", "tiktok", "google"):
        return 1
    if key in hot or (key in cache and cache[key]):
        return 2
    if key in intern_boards or "ok" not in health.get(key, {}):
        return 3
    return 4


def interval(key):
    if health.get(key, {}).get("fails", 0) >= INACTIVE_AFTER:
        return INACTIVE_RETRY
    if time.time() - dead.get(key, 0) < DEAD_FOR:  # 403: blocked, respect it for a day
        return DEAD_FOR
    return TIER_S[tier(key)]


def run_board(key):
    try:
        fetch(key)
    finally:
        with sched_lock:
            inflight.discard(key)
            next_due[key] = time.time() + interval(key) * random.uniform(0.9, 1.1)


def scheduler():
    """Feed due boards to a bounded worker pool, oldest-due first, respecting platform caps and rate-limit pauses."""
    pool, last_scan = ThreadPoolExecutor(WORKERS), 0
    while True:
        now = time.time()
        with sched_lock:
            if now - last_scan > 30:  # newly discovered boards get a random slot inside their interval
                last_scan = now
                for k, bs in list(boards.items()):
                    for b in bs:
                        if (k, b) not in next_due:
                            next_due[(k, b)] = now + random.uniform(0, TIER_S[tier((k, b))])
            # tier first, then oldest-due: tier 1 is never stuck behind an overdue long tail; lower tiers absorb any lag
            due_keys = sorted(((t, key) for key, t in next_due.items() if t <= now and key not in inflight),
                              key=lambda x: (tier(x[1]), x[0]))
            running = {}
            for key in inflight:
                running[key[0]] = running.get(key[0], 0) + 1
            free = WORKERS - len(inflight)
            for t, key in due_keys:
                if free <= 0:
                    break
                if now < cooldown.get(key[0], 0):
                    next_due[key] = cooldown[key[0]] + random.uniform(0, 30)  # platform paused: come back after
                    continue
                if running.get(key[0], 0) >= PLATFORM_CAP.get(key[0], WORKERS):
                    continue
                inflight.add(key)
                running[key[0]] = running.get(key[0], 0) + 1
                free -= 1
                SCHED["lag_total"] += now - t
                SCHED["started"] += 1
                tl_ = SCHED["by_tier"].setdefault(tier(key), [0, 0.0])
                tl_[0] += 1
                tl_[1] += now - t
                pool.submit(run_board, key)
            SCHED["overdue"] = len(due_keys)
            SCHED["overdue_by_tier"] = {}
            for _, key in due_keys:
                SCHED["overdue_by_tier"][tier(key)] = SCHED["overdue_by_tier"].get(tier(key), 0) + 1
        time.sleep(0.5)


SCHED = {"started": 0, "lag_total": 0.0, "overdue": 0, "by_tier": {}}
if SOURCES.exists():  # restore SWE history (tier 1) and failure counts from the last run
    _reg = json.loads(SOURCES.read_text())
    swe_boards.update(_as_key(k, b) for k, b in _reg.get("swe_boards", []))
    intern_boards.update(_as_key(k, b) for k, b in _reg.get("intern_boards", []))
    verified_boards.update(_as_key(k, b) for k, b in _reg.get("verified_boards", []))
    for k, b, n in _reg.get("fails", []):
        health.setdefault(_as_key(k, b), {})["fails"] = n
    for k, b, ok, err, st, n in _reg.get("health", []):
        h = health.setdefault(_as_key(k, b), {})
        h.update({f: v for f, v in (("ok", ok), ("err", err)) if v}, status=st, n=n)


def is_swe(j):
    return _is_swe(j["title"])


@functools.lru_cache(maxsize=200_000)  # the same titles come back every rebuild: classify each once
def _is_swe(t):
    """Title alone is enough: clearly a software role."""
    return bool(INTERN.search(t) and SWE.search(t) and not NOT_SWE.search(t) and not SOFT.search(t))


def is_candidate(j):
    return _is_candidate(j["title"])


@functools.lru_cache(maxsize=200_000)
def _is_candidate(t):
    """Title might be software (tech programs, or networking/hardware/embedded/firmware/systems roles); the job
    description decides."""
    if not INTERN.search(t) or NOT_SWE.search(t):
        return False
    if SOFT.search(t):
        return True
    return bool(TECH.search(t) and not SWE.search(t) and not NOT_TECH.search(t))


PRETTY = {"apis": "APIs", "api": "APIs", "rest": "REST", "restful": "REST", "sql": "SQL", "aws": "AWS", "gcp": "GCP",
          "ci/cd": "CI/CD", "git": "Git", "c++": "C++", "c#": "C#", ".net": ".NET", "javascript": "JavaScript",
          "typescript": "TypeScript", "nodejs": "Node.js", "node.js": "Node.js"}
SHOW_AT = 60  # minimum score to list a hidden-SWE role


def desc_score(text):
    """ponytail: keyword heuristic, not a model. Scores how strongly a description reads like software development:
    - needs a programming language AND 2+ other signal groups (APIs, backend/frontend, databases, Git, cloud,
      software-development practice), else capped below the SHOW_AT line;
    - IT-support / hardware-bench / network-admin terms pull it down, and if they outnumber the software terms the
      role is treated as mainly not software.
    Returns (score, matched skills) so the UI can show *why*."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(text or "")))
    found, groups = [], set()
    for g, pat in SIGNALS.items():
        for m in pat.finditer(text):
            groups.add(g)
            w = m.group().lower()
            w = PRETTY.get(w, w if len(w) <= 3 else w.title())
            if w not in found:
                found.append(w)
    negatives = len({m.group().lower() for m in NOT_SOFTWARE.finditer(text)})
    others = len(groups - {"lang"})
    score = 25 + 11 * others + 3 * min(len(found), 10) - 8 * negatives
    if "lang" not in groups or others < 2:
        score = min(score, SHOW_AT - 5)
    if negatives > len(found) / 2:  # mostly support/hardware work that happens to mention some code
        score = min(score, SHOW_AT - 10)
    return max(0, min(99, score)), found[:6]


def desc_ok(text):
    return desc_score(text)[0] >= SHOW_AT


def verdict(text):
    s, why = desc_score(text)
    return {"s": s, "why": why}


def note_pay(j, text):
    if text and INTERN.search(j["title"]):
        text = text if isinstance(text, str) else " ".join(texts(text))
        if UNPAID.search(text):
            pay_info[j["url"]] = "unpaid"
        elif PAID.search(text):
            pay_info[j["url"]] = "paid"


def pay_status(j, salary):
    """confirmed paid / confirmed unpaid / unknown. Unknown is never treated as paid."""
    if UNPAID.search(j["title"]) or pay_info.get(j["url"]) == "unpaid":
        return "unpaid"
    return "paid" if salary or pay_info.get(j["url"]) == "paid" else "unknown"


def tag(j, desc=None, detail=None):
    """For tech-program candidates: judge the description now if the feed includes it, else remember where to
    fetch it (the verifier loop does that once per job)."""
    if isinstance(desc, str):
        note_pay(j, desc)
    if is_candidate(j) and j["url"] not in verdicts:
        if isinstance(desc, dict):  # a curated list vouches for it...
            if not (SOFT.search(j["title"]) and not SWE.search(j["title"])):  # ...but "Systems Engineer" needs a description
                verdicts[j["url"]] = desc
        elif desc is not None:
            verdicts[j["url"]] = verdict(desc)
        elif detail:
            j["detail"] = detail
    return j


def texts(x):
    """All strings inside a JSON response: detail endpoints nest the description differently per system."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from texts(v)
    elif isinstance(x, list):
        for v in x:
            yield from texts(v)


def verify(j):
    tl.kind = "descriptions (" + j["source"] + ")"
    try:
        text = " ".join(texts(http(j["detail"])))
        note_pay(j, text)
        verdicts[j["url"]] = verdict(text)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            verdicts[j["url"]] = {"s": 0, "why": []}
    except Exception:
        pass  # retry next round


@functools.lru_cache(maxsize=100_000)
def domain(company, url):
    """Best guess at the company's website, for its logo. ponytail: name-based guess, wrong for some companies
    (they fall back to a letter badge); a curated name->domain map fixes stragglers."""
    if company.lower() in sites:
        return sites[company.lower()]
    host = re.sub(r"^(www|careers?|jobs|apply|recruiting|talent)\.", "", url.split("/")[2].lower())
    if not ATS_HOSTS.search(host):
        return ".".join(host.split(".")[-2:])
    name = re.sub(r"\b(inc|llc|ltd|corp|corporation|company|co|group|holdings|the|plc)\b|[^a-z0-9 ]", "", company.lower())
    return name.replace(" ", "") + ".com"


NOISE = {"intern", "internship", "co", "op", "summer", "fall", "winter", "spring", "2026", "2027", "us", "usa", "the", "and", "hq"}
CITY_ALIAS = {"nyc": "new york", "sf": "san francisco", "la": "los angeles"}


def dedupe_key(j):
    """Same company + same words (order, -ing, seasons ignored): "Software Engineering Intern, iOS - Summer 2027"
    == "Software Engineer Intern - iOS"."""
    words = re.sub(r"engineering", "engineer", j["title"].lower())
    return j["company"].lower(), " ".join(sorted(set(re.findall(r"[a-z0-9+#]+", words)) - NOISE))


dirty = threading.Event()  # set whenever board data changes; the rebuild thread picks it up
REBUILD_EVERY, SAVE_EVERY = 10, 60  # rebuild at most every 10 s, write state files at most every 60 s
_last_save = [0.0]
rebuild_lock = threading.Lock()


def rebuild_loop():
    """The only place the board is rebuilt: at most every REBUILD_EVERY seconds, and only when something changed."""
    while True:
        dirty.wait()
        time.sleep(REBUILD_EVERY)
        dirty.clear()
        try:
            rebuild()
            tl.kind = "ntfy"
            send_alerts()
        except Exception as e:
            print("rebuild failed:", e, flush=True)


def rebuild():
    with rebuild_lock:  # never two at once
        STATS["rebuilds_running"] += 1
        if STATS["rebuilds_running"] > 1:
            STATS["rebuild_overlaps"] += 1
        t0 = time.time()
        try:
            _rebuild()
        finally:
            STATS["rebuilds_running"] -= 1
            STATS["rebuilds"] += 1
            STATS["rebuild_seconds"] += time.time() - t0


def _rebuild():
    global jobs
    out = {}
    # direct-from-company results first so they win dedupe (exact timestamps, canonical links)
    direct = [{**j, "company": names.get(k, j["company"][:1].upper() + j["company"][1:])} for k, r in list(cache.items()) for j in r]
    shown = lambda j: is_swe(j) or (is_candidate(j) and verdicts.get(j["url"], {}).get("s", 0) >= SHOW_AT)
    month_ago = time.time() - 30 * 86400
    activity = {}
    for j in direct:  # companies already posting (other) internships this season tend to post SWE soon
        if INTERN.search(j["title"]) and not shown(j) and detected.get(j["url"], 0) > month_ago:
            activity[j["company"].lower()] = activity.get(j["company"].lower(), 0) + 1
    intern_activity.clear()
    intern_activity.update(activity)
    swe_boards.update(k for k, r in list(cache.items()) if any(shown(j) for j in r))  # -> tier 1
    verified_boards.update(k for k, r in list(cache.items()) if any(shown(j) and us_undergrad(j) for j in r))
    STATS["rebuild_jobs_scanned"] += len(direct) + len(lists)
    for j in direct + lists:
        if not shown(j):
            continue
        k = dedupe_key(j)
        if k not in out:
            ver = verdicts.get(j["url"], {}) if not is_swe(j) else {}
            out[k] = {**{f: v for f, v in j.items() if f != "detail"}, "kind": "swe" if SWE.search(j["title"]) else "tech",
                      "score": ver.get("s"), "why": ver.get("why", []), "detected": detected.get(j["url"], j["posted"]),
                      "domain": domain(j["company"], j["url"]), "salary": salaries.get(k) or salaries.get(k[0], "")}
            out[k]["pay"] = pay_status(j, out[k]["salary"])
        elif (city := re.split(r"[,;(]", j["location"])[0].strip()) \
                and CITY_ALIAS.get(city.lower(), city.lower())[:6] not in out[k]["location"].lower():  # same role in another city: one card, merged locations
            out[k]["location"] += " · " + city
        if j.get("terms") and not out[k].get("terms"):  # direct copy wins, but keep the list's season label
            out[k]["terms"] = j["terms"]
    live = {j["url"] for j in out.values()}
    now = time.time()
    for u, c in list(closed.items()):  # recently closed roles stay visible (marked) unless a live copy exists
        if now - c["closed"] > CLOSED_KEEP:
            closed.pop(u, None)
        elif u not in live and dedupe_key(c) not in out:
            out[dedupe_key(c)] = c
    jobs = sorted(out.values(), key=lambda j: -j["posted"])
    shown_urls.clear()
    shown_urls.update({j["url"]: j for j in jobs if "closed" not in j})
    # batch drops: companies whose new roles landed together
    recent = {}
    for j in jobs:
        if "closed" not in j and j["detected"] and now - j["detected"] < BATCH_WINDOW:
            recent.setdefault(j["company"], []).append(j)
    status["batches"] = sorted(({"company": c, "n": len(js), "since": min(j["detected"] for j in js), "domain": js[0]["domain"]}
                                for c, js in recent.items() if len(js) >= BATCH_MIN), key=lambda b: -b["n"])
    status.update(version=int(time.time() * 1000), boards=sum(map(len, boards.values())) + 1, ok=len(cache),
                  verified=coverage_report()["verified_companies_monitored_directly"])
    if time.time() - _last_save[0] >= SAVE_EVERY:  # state files: at most once a minute
        _last_save[0] = time.time()
        cutoff = time.time() - 60 * 86400
        save(SEEN, {u: t for u, t in list(first_seen.items()) if t > cutoff})
        save(VERDICTS, verdicts)
        save(DETECTED, {u: t for u, t in list(detected.items()) if t > cutoff or u in live})
        save(CLOSED, closed)
        save(SOURCES, {"swe_boards": [[k, b] for k, b in swe_boards], "intern_boards": [[k, b] for k, b in intern_boards],
                       "verified_boards": [[k, b] for k, b in verified_boards],
                       "fails": [[k, b, h["fails"]] for (k, b), h in list(health.items()) if h.get("fails")],
                       # last successful check / last error / status / job count per board, so KPIs survive restarts
                       "health": [[k, b, h.get("ok", 0), h.get("err", 0), h.get("status", ""), h.get("n", 0)]
                                  for (k, b), h in list(health.items()) if "ok" in h or "err" in h]})


LISTS_EVERY = 600  # aggregator lists (Simplify, SpeedyApply, vanshb03, GitHub submissions) are fallback coverage


def lists_loop():
    """Refresh the aggregator lists (with ETag: unchanged lists cost a 304) and the set of known boards."""
    global lists, boards
    while True:
        t0 = time.time()
        try:
            tl.kind = "aggregator lists"
            new_lists, new_boards = fetch_lists()
            new_boards.update(amazon={"amazon"}, tiktok={"tiktok"}, google={"google"})  # single-company sources
            lists, boards = new_lists, new_boards
            STATS["lists_seconds_last"] = round(time.time() - t0, 1)
            dirty.set()
            print(f"{time.strftime('%H:%M:%S')} lists: {len(lists)} list jobs, {sum(map(len, boards.values()))} boards known",
                  flush=True)
        except Exception as e:
            print("lists loop failed:", e, flush=True)
        time.sleep(max(60, LISTS_EVERY - (time.time() - t0)))


def verify_loop():
    """Fetch descriptions for tech-program candidates, once each. Workday detail requests are few per cycle,
    so they share Workday's budget without starving it."""
    while True:
        pending = [j for r in list(cache.values()) for j in r if "detail" in j and j["url"] not in verdicts]
        slow = [j for j in pending if j["source"] in ("Workday", "Workable")][:40]
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(verify, [j for j in pending if j["source"] not in ("Workday", "Workable")] + slow))
        if pending:
            dirty.set()
            print(f"{time.strftime('%H:%M:%S')} verify: {len(pending)} hidden-SWE descriptions pending, "
                  f"{sum(v['s'] >= SHOW_AT for v in verdicts.values())} SWE-like so far", flush=True)
        time.sleep(20)


def build_history():
    """Last cycle's first SWE-internship date per company, from Simplify's listings as of Dec 31 last year."""
    if HISTORY.exists():
        return {k.strip(): v for k, v in json.loads(HISTORY.read_text()).items()}
    until = f"{time.gmtime().tm_year - 1}-12-31T00:00:00Z"
    sha = http(f"https://api.github.com/repositories/266417772/commits?path=.github/scripts/listings.json&until={until}&per_page=1")[0]["sha"]
    first = {}
    for j in http(f"https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/{sha}/.github/scripts/listings.json"):
        if j.get("category") in ("Software", "Software Engineering") and "Summer" in " ".join(j.get("terms") or ["Summer"]):
            c = j["company_name"].strip()
            first[c] = min(first.get(c, j["date_posted"]), j["date_posted"])
    save(HISTORY, first)
    return first


history = {}


def signals_loop():
    """Every 10 min: Reddit, GitHub issues, watched pages -> community signals (+ GitHub posting submissions)."""
    global history, submissions
    while True:
        try:
            if not history:
                history = build_history()
            tl.kind = "community signals"
            match = community.company_matcher({j["company"] for j in jobs} | set(history) | set(names.values()))
            items = community.reddit()
            gh, subs = community.github_issues()
            submissions = subs
            for it in items + gh + community.pages(page_seen):
                for sg in community.extract(it, match):
                    sig_store.setdefault(sg["id"], sg)
            cutoff = time.time() - 30 * 86400
            for k in [k for k, v in sig_store.items() if v["posted"] < cutoff]:
                sig_store.pop(k)
            save(SIGNALS_FILE, sig_store)
            send_soon_alerts(likely_soon()["alerts"])
            print(f"{time.strftime('%H:%M:%S')} signals: {len(items)} reddit, {len(gh)} github, {len(sig_store)} signals, "
                  f"{sum(x['open'] for x in subs)} open submissions", flush=True)
        except Exception as e:
            print("signals loop failed:", e, flush=True)
        time.sleep(600)


def likely_soon():
    """Likely Dropping Soon: last cycle's timing + this season's ATS activity + community signals, per company.
    ponytail: hand-weighted score (history <=40, ATS <=20, community <=50), not a trained model."""
    now = time.time()
    open_now = {j["company"].lower() for j in jobs if j["kind"] == "swe" and "closed" not in j
                and now - (j["posted"] or j["detected"] or now) < 150 * 86400 and "2026" not in j["title"]}
    clusters = community.combine(list(sig_store.values()))
    by_co = {}
    for c in clusters:
        by_co.setdefault(c["company"].lower(), []).append(c)
    out = []
    for co in set(map(str.lower, history)) | set(by_co) | set(intern_activity):
        name = next((n for n in history if n.lower() == co), None) or (by_co.get(co) or [{}])[0].get("company") or co.title()
        reasons, score, expected, source = [], 0, None, None
        if co in open_now:
            continue  # already dropped this cycle
        if name in history:
            last = datetime.fromtimestamp(history[name])
            exp = last.replace(year=datetime.now().year)
            delta = (exp.timestamp() - now) / 86400
            if 0 <= delta <= 21:
                score += 40 - 1.5 * delta
                reasons.append(f"Last year posted SWE internships on {last:%b %-d}; expected in ~{delta:.0f} days")
            elif -45 <= delta < 0:  # overdue: maybe soon, maybe not hiring this year
                score += 22 + delta / 5
                reasons.append(f"Last year posted on {last:%b %-d}; {-delta:.0f} days overdue and not open yet")
            expected, source = exp.timestamp(), "history"
        if n := intern_activity.get(co):
            score += min(20, 5 * n)
            reasons.append(f"Already posted {n} other internship role{'s' if n > 1 else ''} in the last 30 days")
        upcoming = [c for c in by_co.get(co, []) if (c["claimed"] or c["latest"]) > now - 6 * 3600]
        if upcoming:
            best = max(upcoming, key=lambda c: c["confidence"])
            score += 50 * best["confidence"]
            reasons.append(f"{best['label']}: {len(best['signals'])} post{'s' if len(best['signals']) > 1 else ''} from "
                           f"{best['independent']} independent source{'s' if best['independent'] > 1 else ''}")
            if best["claimed"] and best["confidence"] >= 0.45:
                expected, source = best["claimed"], best["label"]
        if score >= 20:
            out.append({"company": name, "score": round(min(99, score)), "expected": expected, "expected_from": source,
                        "reasons": reasons, "domain": domain(name, "https://x/"), "clusters": by_co.get(co, [])})
    alerts = [{"id": f"{c['company']}|{c['claimed']}", "company": c["company"], "claimed": c["claimed"], "label": c["label"],
               "confidence": c["confidence"]} for c in clusters
              if c["claimed"] and now <= c["claimed"] <= now + 36 * 3600
              and (c["top_tier"] == "official" or c["confidence"] >= 0.7) and c["company"].lower() not in open_now]
    return {"companies": sorted(out, key=lambda x: -x["score"]), "alerts": alerts,
            "signals": sorted(sig_store.values(), key=lambda s: -s["posted"])[:300]}


# ---------------------------------------------------------------- phone alerts (ntfy)
NOTIFIED = HERE / "notified.json"  # what we've already alerted on, so restarts never re-send
notified = set(json.loads(NOTIFIED.read_text())) if NOTIFIED.exists() else set()
US = re.compile(r"united states|\busa?\b|remote|\d+ locations|,\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
                r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b|\bsf\b|\bnyc\b|"
                r"california|new york|texas|washington|massachusetts|illinois|georgia|virginia|colorado|new jersey", re.I)
NON_US = re.compile(r"canada|united kingdom|\buk\b|india|germany|france|singapore|ireland|netherlands|poland|israel|china|"
                    r"japan|australia|mexico|brazil|spain|italy|sweden|switzerland|romania|korea|taiwan|hong kong", re.I)
GRAD = re.compile(r"\bph\.?d|master'?s|\bmba\b|\bm\.?s\.?\b|(?<!under)graduate student|\bgrad(uate)? (intern|program)", re.I)
AGG_SOURCES = {"Simplify", "SpeedyApply", "GitHub"}
MAX_PER_ROUND = 8  # more than this in one round becomes a single summary alert


def ntfy(title, message, click=None, tags=(), priority=3):
    """Publish via ntfy's JSON API (UTF-8 safe titles). No topic configured = no-op."""
    if not NTFY_TOPIC:
        return False
    body = {"topic": NTFY_TOPIC, "title": title[:250], "message": message[:1000], "tags": list(tags), "priority": priority}
    if click:
        body["click"] = click
    try:
        urllib.request.urlopen(urllib.request.Request(NTFY_SERVER, json.dumps(body).encode(),
                                                      {"Content-Type": "application/json"}), timeout=15)
        return True
    except Exception as e:
        print("ntfy failed:", e, flush=True)
        return False


def is_priority(j, now):
    """Same rule as the page: newly detected + US + undergrad + 2027 (or no year) + strong SWE match."""
    loc, title = j.get("location", ""), j["title"]
    years = set(re.findall(r"\b20(2[6-9])\b", title + " " + j.get("terms", "")))
    strong = (j.get("score") is None or j["score"] >= 70) if j["kind"] == "swe" else (j.get("score") or 0) >= 80
    return (j.get("detected") and now - j["detected"] < 30 * 60 and "closed" not in j
            and (not loc or (US.search(loc) and not NON_US.search(loc))) and not GRAD.search(title)
            and (not years or "27" in years) and strong)


def send_alerts():
    now = time.time()
    watched = set(json.loads(WATCH.read_text())) if WATCH.exists() else set()
    due = [j for j in jobs if j["url"] not in notified and "closed" not in j
           and (is_priority(j, now) or (j["company"].lower() in watched and j.get("detected") and now - j["detected"] < 30 * 60))]
    for j in due[:MAX_PER_ROUND]:
        star = "★ " if j["company"].lower() in watched else ""
        ntfy(f"{star}{j['company']}: {j['title']}", f"{j['location'] or 'Location not listed'}\nvia {j['source']}",
             click=j["url"], tags=["briefcase"], priority=4)
    if len(due) > MAX_PER_ROUND:
        ntfy(f"+{len(due) - MAX_PER_ROUND} more new SWE internships", "Open the board to see them all.", click=SITE_URL, tags=["sparkles"])
    for b in status.get("batches", []):
        key = f"batch:{b['company']}:{int(b['since'] // 3600)}"
        if key not in notified and ntfy(f"📦 Batch drop: {b['company']}", f"{b['n']} internships in the last hour",
                                        click=f"{SITE_URL}/?q={quote(b['company'])}", tags=["package"], priority=4):
            notified.add(key)
    notified.update(j["url"] for j in due)
    save(NOTIFIED, sorted(notified))


def send_soon_alerts(alerts):
    for a in alerts:
        key = "soon:" + a["id"]
        if key not in notified and ntfy(f"⏰ {a['company']} may open SWE internships soon",
                                        f"{a['label']} ({round(a['confidence'] * 100)}% confidence)\n"
                                        f"Claimed: {datetime.fromtimestamp(a['claimed']):%a %b %-d, %-I:%M %p}",
                                        click=f"{SITE_URL}/#soon", tags=["alarm_clock"], priority=4):
            notified.add(key)
    save(NOTIFIED, sorted(notified))


def sched_report():
    """How well the scheduler keeps up: boards per tier, target vs achieved interval, overdue backlog."""
    now, tiers = time.time(), {}
    for key in list(next_due):
        t = "inactive" if health.get(key, {}).get("fails", 0) >= INACTIVE_AFTER else tier(key)
        tiers[t] = tiers.get(t, 0) + 1
    up = max(1, now - STATS["since"])
    return {"workers": WORKERS, "inflight": len(inflight), "overdue_now": SCHED["overdue"],
            "overdue_by_tier": SCHED.get("overdue_by_tier", {}),
            "avg_start_lag_s": round(SCHED["lag_total"] / max(1, SCHED["started"]), 1),
            "checks_per_min": round(SCHED["started"] / up * 60, 1),
            "avg_start_lag_by_tier_s": {t: round(v[1] / max(1, v[0]), 1) for t, v in SCHED["by_tier"].items()},
            "boards_by_tier": tiers, "target_interval_s": TIER_S,
            "demand_checks_per_min": round(sum(n * 60 / TIER_S[t] for t, n in tiers.items() if t in TIER_S), 1)}


def us_undergrad(j):
    """US (or unspecified) location and not grad-only. Paid isn't verifiable from most postings yet: assumed."""
    loc = j.get("location", "")
    return (not loc or (US.search(loc) and not NON_US.search(loc))) and not GRAD.search(j["title"])


def coverage_report():
    """The number that matters, kept apart from raw discovered boards."""
    working = lambda k: health.get(k, {}).get("ok", 0) >= health.get(k, {}).get("err", 0) and "ok" in health.get(k, {})
    canon = lambda k: re.sub(r"[^a-z0-9]", "", names.get(k, k[1] if isinstance(k[1], str) else k[1][0]).lower())
    by_kind = {}
    for k in verified_boards:
        if working(k):
            by_kind.setdefault(k[0], set()).add(canon(k))
    paid_boards = {k for k, r in list(cache.items()) for j in r if pay_status(j, salaries.get(dedupe_key(j), "")) == "paid"}
    unpaid_only = {k for k in verified_boards if all(pay_status(j, "") == "unpaid" for j in cache.get(k, [])) and cache.get(k)}
    known = sum(len(v) for v in boards.values())
    checked = sum(1 for h in list(health.values()) if "ok" in h or "err" in h)
    inactive = sum(1 for h in list(health.values()) if h.get("fails", 0) >= INACTIVE_AFTER)
    verified = {canon(k) for k in verified_boards if working(k) and k not in unpaid_only}
    return {"verified_companies_monitored_directly": len(verified),
            "verified_confirmed_paid": len({canon(k) for k in verified_boards if working(k) and k in paid_boards}),
            "verified_pay_unknown": len(verified) - len({canon(k) for k in verified_boards if working(k) and k in paid_boards}),
            "verified_confirmed_unpaid": len({canon(k) for k in verified_boards if working(k) and k in unpaid_only}),
            "active_boards": known - inactive, "inactive_boards": inactive,
            "checked_at_least_once_pct": round(100 * checked / max(1, known), 1), "boards_checked": checked,
            "verified_by_platform": {k: len(v) for k, v in sorted(by_kind.items(), key=lambda x: -len(x[1]))},
            "companies_with_any_internship_history": len({canon(k) for k in intern_boards}),
            "raw_discovered_boards": sum(len(v) for v in boards.values()),
            "definition": "company whose own job board we poll successfully and that has had a US (or unspecified), "
                          "non-grad-only SWE-type internship (software, backend/frontend/mobile, platform/infra, DevOps/SRE, "
                          "data/ML/AI engineering, firmware/embedded, security engineering: judged on title and description); "
                          "companies whose only such roles are marked unpaid are excluded; pay is shown as confirmed or unknown"}


def stats_report():
    up = time.time() - STATS["since"]
    rss = int(open("/proc/self/status").read().split("VmRSS:")[1].split()[0]) // 1024 if os.path.exists("/proc/self/status") else None
    status = {}
    for (k, _), h in list(health.items()):
        c = status.setdefault(k, {})
        c[h.get("status", "unchecked")] = c.get(h.get("status", "unchecked"), 0) + 1
    return {"uptime_min": round(up / 60, 1), "rss_mb": rss, "threads": threading.active_count(),
            "cached_jobs": sum(len(r) for r in list(cache.values())), "boards_in_cache": len(cache),
            "boards_known": {k: len(v) for k, v in boards.items()}, "source_status": status,
            "http": {k: {**v, "req_per_min": round(v["requests"] / up * 60, 1), "mb": round(v["bytes"] / 1e6, 1),
                         "mb_per_min": round(v["bytes"] / 1e6 / up * 60, 2)} for k, v in sorted(STATS["http"].items())},
            "rebuilds": STATS["rebuilds"], "rebuild_avg_s": round(STATS["rebuild_seconds"] / max(1, STATS["rebuilds"]), 2),
            "rebuild_share_of_time": round(STATS["rebuild_seconds"] / up, 2), "rebuild_overlaps": STATS["rebuild_overlaps"],
            "rebuild_jobs_scanned_per_run": STATS["rebuild_jobs_scanned"] // max(1, STATS["rebuilds"]),
            "disk_mb_written": round(STATS["disk_bytes_written"] / 1e6, 1), "disk_writes": STATS["disk_writes"],
            "lists_seconds_last": STATS.get("lists_seconds_last"),
            "scheduler": sched_report(),
            "listings_shown": len(jobs), "companies_shown": len({j["company"].lower() for j in jobs}),
            "coverage": coverage_report(),
            "detection_latency_s": {k: {"n": len(v), "median": sorted(v)[len(v) // 2], "p95": sorted(v)[int(len(v) * .95)]}
                                    for k, v in STATS["latency"].items() if v}}


class H(BaseHTTPRequestHandler):
    def send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/jobs"):
            if self.path.endswith(f"?v={status.get('version')}"):  # page already has this version: nothing to send
                self.send_response(204)
                self.send_header("X-Polled", str(status.get("polled", 0)))  # keeps "Updated Xs ago" live
                return self.end_headers()
            self.send(json.dumps({**status, "jobs": jobs}).encode())
        elif self.path == "/api/soon":
            self.send(json.dumps(likely_soon()).encode())
        elif self.path == "/api/stats":
            self.send(json.dumps(stats_report()).encode())
        elif self.path == "/api/sources":  # every source's last outcome, failures included (CSV)
            rows = ["kind,board,status,jobs,last_ok,last_error,detail"]
            for (k, b), h in sorted(health.items(), key=lambda x: (x[0][0], str(x[0][1]))):
                rows.append(",".join(str(v).replace(",", ";") for v in (k, " / ".join(b) if isinstance(b, tuple) else b,
                            h.get("status", "unchecked"), h.get("n", ""), h.get("ok", ""), h.get("err", ""), h.get("why", ""))))
            self.send("\n".join(rows).encode(), "text/csv; charset=utf-8")
        elif self.path == "/api/health":
            self.send(json.dumps(health_report()).encode())
        elif self.path == "/api/tracker":
            self.send(TRACKER.read_bytes() if TRACKER.exists() else b"[]")
        elif self.path == "/api/watch":
            self.send(WATCH.read_bytes() if WATCH.exists() else b"[]")
        else:
            self.send((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")

    def do_POST(self):  # whole tracker / watch list is replaced on each save (single user)
        target = {"/api/tracker": TRACKER, "/api/watch": WATCH}.get(self.path)
        if not target:
            return self.send_error(404)
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not isinstance(data, list):
            return self.send_error(400)
        with tracker_lock:
            save(target, data, indent=1)
        self.send(b"{}")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if sys.argv[1:] == ["test-alert"]:  # python3 board.py test-alert: a real, current job, formatted like a real alert
        for _ in range(30):  # right after a restart the board is still doing its first sweep
            live = [j for j in json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/jobs", timeout=15))["jobs"]
                    if j["kind"] == "swe" and "closed" not in j]
            if live:
                break
            time.sleep(5)
        j = next((j for j in live if j["source"] not in AGG_SOURCES), live[0])  # prefer a company's own posting
        ok = ntfy(f"{j['company']}: {j['title']}", f"{j['location'] or 'Location not listed'}\nvia {j['source']} · test alert",
                  click=j["url"], tags=["white_check_mark"], priority=4)
        sys.exit(print(f"sent: {j['company']}: {j['title']} -> {j['url']}" if ok else "not sent (is NTFY_TOPIC set in .env?)"))
    threading.Thread(target=lists_loop, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=rebuild_loop, daemon=True).start()
    threading.Thread(target=verify_loop, daemon=True).start()
    threading.Thread(target=signals_loop, daemon=True).start()
    print("http://localhost:8000", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()
