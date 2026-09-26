"""Community / public signals that a company's SWE internships are about to open ("Likely Dropping Soon").

Sources (public, no logins):
  - Reddit RSS: r/csMajors, r/cscareerquestions, r/internships
  - GitHub issues on the internship-list repos (drop talk + "New Internship" submissions)
  - pages.json: any public pages you want watched (university career centers, company early-career pages),
    each tagged with its trust tier. Format: [{"url": "...", "tier": "university" | "official", "company": "optional"}]
Not covered (need logins / paid APIs / forbid scraping): TikTok, LinkedIn, X, Handshake, Discord.

Every signal is kept with its evidence; rumors are never treated as confirmed. Several independent sources
agreeing on the same company and day raise the combined confidence.
"""
import calendar, html, json, re, time, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
PAGES = HERE / "pages.json"
SUBREDDITS = ["csMajors", "cscareerquestions", "internships"]
REPOS = ["SimplifyJobs/Summer2027-Internships", "speedyapply/2027-SWE-College-Jobs", "vanshb03/Summer2027-Internships"]
UA = {"User-Agent": "internboard/1.0 (personal internship tracker)"}

# Trust hierarchy -> base confidence of a single signal
TIERS = {"official": 0.9, "university": 0.75, "employee": 0.55, "community": 0.3}
TIER_LABEL = {"official": "Official", "university": "University / career center", "employee": "Employee / intern (claimed)",
              "community": "Community rumor"}

DROP = re.compile(
    r"(?:app(?:lication)?s?|internships?|portal|postings?|roles?|swe|it|they|recruiting)\s+(?:will\s+|should\s+|are\s+|is\s+)?"
    r"(?:open(?:s|ing)?|drop(?:s|ping)?|go(?:es|ing)?\s+live|releas(?:e|es|ing)|launch(?:es|ing)?|start(?:s|ing)?|com(?:e|es|ing)\s+out)"
    r"|(?:open(?:s|ing)?|drop(?:s|ping)?)\s+(?:up\s+)?(?:tomorrow|today|tonight|soon|next|on|at|this|in\s)"
    r"|recruiting\s+(?:starts|begins|opens|season starts)|heard\s+from\s+(?:a\s+|my\s+|the\s+)?recruiter"
    r"|recruiter\s+(?:said|says|told|mentioned|confirmed)", re.I)
CONTEXT = re.compile(r"intern|internship|\bswe\b|co-?op|2027|summer|early career|university recruiting", re.I)
NOT_CONTEXT = re.compile(r"new\s*grad|full[- ]time only|\bresults?\b.*\bout\b|offer (came|letter)|rejected|ghosted", re.I)
EMPLOYEE = re.compile(r"\b(i work at|i'?m an? (?:intern|engineer|swe|recruiter|employee) at|as an? recruiter|our (?:team|university recruiting)"
                      r"|we(?:'re| are) (?:opening|hiring|launching)|(?:current|former|incoming) intern at)", re.I)
CITES_RECRUITER = re.compile(r"recruiter\s+(?:said|says|told|mentioned|confirmed)|heard from (?:a |my |the )?recruiter", re.I)
ALIASES = {"JPM": "JPMorgan Chase", "JPMorgan": "JPMorgan Chase", "JP Morgan": "JPMorgan Chase", "Goldman": "Goldman Sachs",
           "GS": "Goldman Sachs", "CapOne": "Capital One", "Cap One": "Capital One", "MSFT": "Microsoft", "FB": "Meta",
           "Facebook": "Meta", "AWS": "Amazon", "Jane St": "Jane Street", "HRT": "Hudson River Trading", "Citsec": "Citadel Securities",
           "Two Sigma": "Two Sigma", "BofA": "Bank of America", "Nvidia": "NVIDIA", "Tiktok": "TikTok", "Bytedance": "ByteDance"}
# Company names that are also everyday words; only matched with a clear internship context in the same sentence
COMMON = {"Box", "Square", "Ramp", "Notion", "Linear", "Plaid", "Chime", "Apple", "Oracle", "Target", "Visa", "Snap", "Block",
          "Scale", "Nuro", "Mercury", "Brex", "Figma", "Discord", "Slack", "Zoom", "Twitch", "Reddit", "Wish", "Gap", "Ford"}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
TZ = {"et": -4, "est": -5, "edt": -4, "ct": -5, "cst": -6, "cdt": -5, "mt": -6, "pt": -7, "pst": -8, "pdt": -7}  # ponytail: fixed offsets


def get(url, as_json=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


def strip(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(h or "")))).strip()


# ---------------------------------------------------------------- fetching
def reddit():
    out = []
    # one combined feed for all subreddits: a single request per round keeps us under Reddit's anonymous limit
    try:
        feeds = [get(f"https://www.reddit.com/r/{'+'.join(SUBREDDITS)}/new/.rss?limit=100")]
    except Exception as e:  # 429s happen; try again next round
        print(f"reddit: {e}", flush=True)
        feeds = []
    for feed in feeds:
        for e in re.findall(r"<entry>(.*?)</entry>", feed, re.S):
            title = html.unescape(re.search(r"<title>(.*?)</title>", e, re.S).group(1))
            body = strip(re.search(r"<content[^>]*>(.*?)</content>", e, re.S).group(1)) if "<content" in e else ""
            body = re.sub(r"submitted by /u/\S+.*$", "", body)
            sub = re.search(r'<category term="([^"]+)"', e)
            out.append({"id": "reddit:" + re.search(r"<id>(.*?)</id>", e).group(1), "source": f"r/{sub.group(1) if sub else 'reddit'}",
                        "author": re.search(r"<name>(.*?)</name>", e).group(1),
                        "link": re.search(r'<link href="([^"]+)"', e).group(1),
                        "posted": int(datetime.fromisoformat(re.search(r"<published>(.*?)</published>", e).group(1)).timestamp()),
                        "title": title, "text": body, "tier": "community"})
    return out


def github_issues():
    items, submissions = [], []
    for repo in REPOS:
        for i in get(f"https://api.github.com/repos/{repo}/issues?state=all&per_page=50&sort=created&direction=desc", True):
            posted = int(datetime.fromisoformat(i["created_at"].replace("Z", "+00:00")).timestamp())
            body = i.get("body") or ""
            if i["title"].startswith("New Internship"):
                link = re.search(r"### Link to Internship Posting\s+(\S+)", body)
                co = re.search(r"### Company Name\s+(.+)", body)
                title = re.search(r"### Internship Title\s+(.+)", body)
                if link and co and title and "_No response_" not in co.group(1) + title.group(1):
                    submissions.append({"company": co.group(1).strip(), "title": title.group(1).strip(), "url": link.group(1),
                                        "posted": posted, "location": (re.search(r"### Location\s+(.+)", body) or [None, ""])[1].strip()
                                        .replace("_No response_", ""), "open": i["state"] == "open"})
                continue
            items.append({"id": f"github:{i['html_url']}", "source": f"GitHub {repo.split('/')[0]}", "author": i["user"]["login"],
                          "link": i["html_url"], "posted": posted, "title": i["title"], "text": strip(body), "tier": "community"})
    return items, submissions


def pages(seen_text):
    """Watched public pages: every sentence that talks about opening dates becomes a signal the first time we see it."""
    out = []
    if not PAGES.exists():
        return out
    for p in json.loads(PAGES.read_text()):
        try:
            text = strip(get(p["url"]))
        except Exception:
            continue
        for sent in re.split(r"(?<=[.!?])\s+", text):
            if DROP.search(sent) and CONTEXT.search(sent):
                key = "page:" + p["url"] + "#" + str(hash(sent))
                first = seen_text.setdefault(key, int(time.time()))
                out.append({"id": key, "source": p["url"].split("/")[2], "author": p["url"].split("/")[2], "link": p["url"],
                            "posted": first, "title": sent[:200], "text": "", "tier": p.get("tier", "university"),
                            "company": p.get("company")})
    return out


# ---------------------------------------------------------------- extraction
def company_matcher(names):
    names = {n for n in names if len(n) >= 3 and not n.islower()} | set(ALIASES)
    pat = re.compile(r"(?<![\w&])(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w&])")
    return lambda text: [ALIASES.get(m, m) for m in dict.fromkeys(pat.findall(text))]


def claimed_time(text, posted):
    """Turn "opens tomorrow at 9am ET" / "drops Monday" / "Oct 1st" into (utc_ts, precision), relative to the post time."""
    t = text.lower()
    base = datetime.fromtimestamp(posted, timezone.utc)
    day, precision = None, None
    if m := re.search(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t):
        day = base.replace(month=MONTHS.index(m.group(1)) + 1, day=int(m.group(2)), hour=12, minute=0, second=0)
        if day < base - timedelta(days=60):
            day = day.replace(year=day.year + 1)
        precision = "day"
    elif m := re.search(r"\b(\d{1,2})/(\d{1,2})\b", t):
        try:
            day = base.replace(month=int(m.group(1)), day=int(m.group(2)), hour=12, minute=0, second=0)
            precision = "day"
        except ValueError:
            pass
    if day is None:
        if "midnight" in t:
            day, precision = (base + timedelta(days=1)).replace(hour=4, minute=0, second=0), "time"  # midnight ET
        elif "tomorrow" in t:
            day, precision = base + timedelta(days=1), "day"
        elif re.search(r"\b(today|tonight)\b", t):
            day, precision = base, "day"
        elif m := re.search(r"\b(next\s+)?(" + "|".join(WEEKDAYS) + r")\b", t):
            ahead = (WEEKDAYS.index(m.group(2)) - base.weekday()) % 7 or 7
            day, precision = base + timedelta(days=ahead + (7 if m.group(1) and ahead < 4 else 0)), "day"
        elif m := re.search(r"\bin\s+(\d+|a few|a couple(?: of)?)\s+(hour|day)s?", t):
            n = {"a few": 3, "a couple": 2, "a couple of": 2}.get(m.group(1), None) or int(m.group(1))
            day, precision = base + (timedelta(hours=n) if m.group(2) == "hour" else timedelta(days=n)), m.group(2)
        elif "next week" in t:
            day, precision = base + timedelta(days=7), "week"
        elif "this week" in t:
            day, precision = base + timedelta(days=2), "week"
        elif "soon" in t:
            return None, "soon"
    if day is None:
        return None, None
    if m := re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(et|est|edt|ct|cst|cdt|mt|pt|pst|pdt)?\b", t):
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        day = day.replace(hour=hour, minute=int(m.group(2) or 0)) - timedelta(hours=TZ.get(m.group(4) or "et", -4))
        precision = "time"
    return calendar.timegm(day.timetuple()), precision


def role_of(text):
    m = re.search(r"((?:software|swe|sde|tech(?:nology)?|engineering|explore|step|early career|university)[\w /&-]{0,40}?"
                  r"(?:intern(?:ship)?s?|program|co-?op))", text, re.I)
    return m.group(1).strip() if m else "Internships"


def extract(item, match):
    """One post -> zero or more signals (one per company it names)."""
    text = f"{item['title']}. {item['text']}"[:4000]
    if not DROP.search(text) or not CONTEXT.search(text) or NOT_CONTEXT.search(item["title"]):
        return []
    companies = [item["company"]] if item.get("company") else match(text)
    if not companies:
        return []
    ts, precision = claimed_time(text, item["posted"])
    if precision is None and item["tier"] == "community":
        return []  # no timing claim at all: chatter, not a drop signal
    tier = "employee" if item["tier"] == "community" and EMPLOYEE.search(text) else item["tier"]
    conf = TIERS[tier] + (0.1 if CITES_RECRUITER.search(text) else 0) + (0.05 if precision == "time" else 0) \
        - (0.1 if precision in ("soon", "week") else 0)
    out = []
    for c in companies[:3]:
        if c in COMMON and not re.search(re.escape(c) + r"[^.]{0,60}(intern|swe|internship)", text, re.I):
            continue
        out.append({"id": f"{item['id']}|{c}", "company": c, "role": role_of(text), "claimed": ts, "precision": precision,
                    "source": item["source"], "author": item["author"], "posted": item["posted"], "tier": tier,
                    "tier_label": TIER_LABEL[tier], "confidence": round(max(0.05, min(0.95, conf)), 2),
                    "cites_recruiter": bool(CITES_RECRUITER.search(text)), "link": item["link"],
                    "evidence": (item["title"] + " — " + item["text"])[:280]})
    return out


def combine(signals):
    """Group each company's signals by claimed day; independent sources (different authors/sites) compound:
    combined = 1 - prod(1 - p) over the strongest signal per author."""
    clusters = []
    for s in sorted(signals, key=lambda s: s["claimed"] or 0):
        for c in clusters:
            same_day = s["claimed"] is None or c["claimed"] is None or abs(s["claimed"] - c["claimed"]) <= 36 * 3600
            if c["company"] == s["company"] and same_day:
                c["signals"].append(s)
                if c["claimed"] is None:
                    c["claimed"], c["precision"] = s["claimed"], s["precision"]
                break
        else:
            clusters.append({"company": s["company"], "claimed": s["claimed"], "precision": s["precision"], "signals": [s]})
    for c in clusters:
        best = {}
        for s in c["signals"]:
            k = (s["source"], s["author"])
            best[k] = max(best.get(k, 0), s["confidence"])
        miss = 1.0
        for p in best.values():
            miss *= 1 - p
        c["confidence"] = round(min(0.97, 1 - miss), 2)
        c["independent"] = len(best)
        c["top_tier"] = min((s["tier"] for s in c["signals"]), key=list(TIERS).index)
        c["label"] = ("Official" if c["top_tier"] == "official" else "High confidence" if c["confidence"] >= 0.7
                      else "Medium confidence" if c["confidence"] >= 0.45 else "Unconfirmed rumor")
        c["latest"] = max(s["posted"] for s in c["signals"])
    return clusters
