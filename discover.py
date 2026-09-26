"""Find company job boards in Common Crawl's URL index -> boards.json (merged with earlier runs).
Run occasionally (weekly is plenty): python3 discover.py        board.py picks up boards.json on its next sweep."""
import json, re, sys, time, urllib.request
from pathlib import Path

OUT = Path(__file__).parent / "boards.json"
DONE = Path(__file__).parent / "discover_done.json"  # (crawl, query) pairs already scanned, so reruns resume
CRAWLS = 12  # most recent crawls to scan (~2 years); boards that have since closed are skipped by board.py
QUERIES = ["boards.greenhouse.io/*", "job-boards.greenhouse.io/*", "jobs.lever.co/*", "jobs.ashbyhq.com/*",
           "jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*", "apply.workable.com/*", "myworkdayjobs.com",
           "myworkdaysite.com", "recruitee.com", "breezy.hr", "jobs.gem.com/*", "teamtailor.com", "icims.com", "ats.rippling.com/*"]
# Oracle Recruiting is found only via the lists (its API has no company name, so a crawl-found tenant would be nameless)
PATTERNS = {
    "greenhouse": r"greenhouse\.io/(?:embed/job_app\?for=)?(?!embed|v1|job_app)([\w-]+)",
    "lever": r"jobs\.lever\.co/([\w.-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([^/?#]+)",
    "smartrecruiters": r"jobs\.smartrecruiters\.com/(?!oneclick|sr-jobs)([\w-]+)",
    "workable": r"apply\.workable\.com/(?!api|j/)([\w-]+)",
    "recruitee": r"//(?!www\.)([\w-]+)\.recruitee\.com",
    "workday": r"//([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?!wday)([\w-]+)",
    "workdaysite": r"//(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([\w-]+)",
    "breezy": r"//(?!www\.|app\.|api\.)([\w-]+)\.breezy\.hr",
    "gem": r"jobs\.gem\.com/([\w-]+)",
    "teamtailor": r"//(?!www\.|app\.|api\.|support\.)([\w-]+)\.teamtailor\.com",
    "icims": r"//(?!www\.|cdn|login|developer)([\w-]+)\.icims\.com",
    "oracle": r"//([\w-]+\.fa\.[\w-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}/sites/([\w-]+)",
    "rippling": r"ats\.rippling\.com/(?!api)([\w-]+)",
    # company-hosted career sites, recognised by their URL shape (found via the lists; a wrong guess just 404s)
    "successfactors": r"//([\w.-]+)/job/[^/?#]+/\d{7,}/",
    "phenom": r"//([\w.-]+)/((?:global|[a-z]{2})/[a-z]{2})/job/[\w-]+",
    "jibe": r"//(?!www\.)([\w.-]+)/jobs/\d+\?(?:.*&)?icims=1",  # iCIMS/Jibe on the company's own domain
}
PATTERNS["smartrecruiters"] = r"(?:jobs|careers)\.smartrecruiters\.com/(?!oneclick|sr-jobs)([\w-]+)"


def board_of(url):
    """Company job board a link belongs to, as (kind, id), or None. Lever/Greenhouse/etc. ids are case-insensitive
    (Lever only answers lowercase); Ashby and Workday ids are case-sensitive."""
    if (m := re.search(r"//(?:www\.|careers\.|jobs\.)?([\w-]+)\.\w+/[^?#]*\?(?:.*&)?gh_jid=\d+", url)) \
            and "greenhouse" not in url:  # Greenhouse embedded on the company's own site: guess the board from the domain
        return "greenhouse", m.group(1).lower()
    if (m := re.search(PATTERNS["jibe"], url)) and not m.group(1).endswith(".icims.com"):
        return "jibe", m.group(1).lower()
    for k, pat in PATTERNS.items():
        if k == "jibe":
            continue
        if m := re.search(pat, url):
            if k == "workdaysite":  # same tenants, other hostname; the normal Workday API serves them
                wd, tenant, site = m.groups()
                return "workday", (tenant, wd, site)
            return k, m.groups() if k in ("workday", "oracle", "phenom") else m.group(1) if k == "ashby" else m.group(1).lower()


def get(url, tries=6):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "internboard-discovery"}), timeout=120) as r:
                return r.read().decode()
        except Exception as e:
            print("  retry", i + 1, e, file=sys.stderr)
            time.sleep(10 * (i + 1))  # Common Crawl's index is shared and throttles; be polite
    return ""


def main():
    found = {k: {tuple(b) if isinstance(b, list) else b for b in v} for k, v in json.loads(OUT.read_text()).items()} if OUT.exists() else {}
    for k in PATTERNS:
        found.setdefault(k, set())
    found.pop("workdaysite")
    done = set(map(tuple, json.loads(DONE.read_text()))) if DONE.exists() else set()
    crawls = json.loads(get("https://index.commoncrawl.org/collinfo.json"))[:CRAWLS]
    for c in crawls:
        api = c["cdx-api"]
        for q in QUERIES:
            if (c["id"], q) in done:
                continue
            mt = "" if q.endswith("/*") else "&matchType=domain"
            n = re.search(r'"pages": (\d+)', get(f"{api}?url={q}{mt}&showNumPages=true"))
            for p in range(int(n.group(1)) if n else 0):
                # regex, not json.loads: the index sometimes returns truncated lines
                for url in re.findall(r'"url": "([^"]+)"', get(f"{api}?url={q}{mt}&output=json&fl=url&page={p}")):
                    if b := board_of(url):
                        found[b[0]].add(b[1])
                time.sleep(1)
            print(c["id"], q, {k: len(v) for k, v in found.items()}, flush=True)
            OUT.write_text(json.dumps({k: sorted(list(b) if isinstance(b, tuple) else b for b in v) for k, v in found.items()}))
            done.add((c["id"], q))
            DONE.write_text(json.dumps(sorted(done)))


if __name__ == "__main__":
    main()
