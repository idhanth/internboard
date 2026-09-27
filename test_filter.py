from board import is_swe

YES = ["Software Engineer Intern", "Intern - Front End Developer", "Software Development Engineer Co-op (Jan. 2027)",
       "Software Engineering, ML Ops Intern/Co-Op", "Software Engineer Intern - Machine Learning",
       "Backend Engineering Intern", "Software Engineering Co-op - Fall 2027", "Winter 2027 Software Developer Co-op", "iOS Engineer Intern", "SDE Intern", "Full Stack Engineering Intern"]
NO = ["2027 Compliance Analyst Summer Internship Programme", "Mechanical Engineering Intern", "Software Sales Intern",
      "Systems Engineer Intern", "Senior Software Engineer", "Internal Tools Engineer", "Machine Learning Engineer Intern",
      "Data Engineering Intern", "AI Engineer Intern", "Data Scientist Intern",
      "Network Engineer Intern", "Engineering Intern (Electrical / Mechanical / GNC / Software)", "Software Asset Management Intern"]

for t in YES:
    assert is_swe({"title": t}), t
for t in NO:
    assert not is_swe({"title": t}), t
from board import dedupe_key
key = lambda t: dedupe_key({"company": "Ramp", "title": t})
assert key("Software Engineering Intern, iOS") == key("Software Engineer Intern - iOS - Summer 2027")
assert key("Software Engineer Intern - BS") != key("Software Engineer Intern - PhD")
from board import is_candidate, desc_ok
cand = lambda t: is_candidate({"title": t})
for t in ["Technology Analyst Intern", "IT Application Development Intern", "Digital Technology Intern - Summer 2027",
          "Engineering Intern", "Computer Science Intern", "Technology Development Program Intern"]:
    assert cand(t), t
for t in ["Software Engineer Intern", "Data Analyst Intern", "IT Help Desk Intern", "Mechanical Engineering Intern",
          "Marketing Intern", "Data Scientist Intern", "Machine Learning Research Intern", "SOC Analyst Intern",
          "Technology & Management Consulting Intern", "Methods Engineering Intern",
          "General Internship Application 2027", "Solutions Engineering Intern"]:
    assert not cand(t), t
assert desc_ok("<p>Build REST APIs in Java and React, use Git and CI/CD in an agile team.</p>")
from board import desc_score, job
s, why = desc_score("Python, React, REST APIs, PostgreSQL and Git")
assert s >= 60 and "Python" in why and "APIs" in why, (s, why)
assert job("x", "t", "", "https://a.io/jobs/1?utm_source=Simplify&ref=Simplify&id=7", 0, "s")["url"] == "https://a.io/jobs/1?id=7"
assert job("x", "t", "", "https://a.io/jobs/1?utm_source=Simplify", 0, "s")["url"] == "https://a.io/jobs/1"
for t in ["Field Engineering Intern", "Biomedical Software Intern", "Sales Engineering Intern"]:
    assert not is_swe({"title": t}) and not cand(t), t

# networking / hardware / embedded / firmware / systems: never trusted or dropped on title alone
for t in ["Software Engineer Intern - Networking", "Network Software Intern", "Software/Hardware Engineering Intern",
          "Firmware Engineering Intern", "Embedded Systems Intern", "Hardware Validation Intern - Software", "Systems Engineering Intern"]:
    assert not is_swe({"title": t}) and cand(t), t

# description decides
strong = "Build REST APIs in Java and React, use Git for code reviews and CI/CD on AWS, write unit tests."
it_support = "Support end users, troubleshoot laptops and printers, manage the ticketing queue, maintain SQL reports in Python."
hardware = "Design PCB schematics, debug circuits with an oscilloscope and multimeter, solder prototypes; some Python scripting."
embedded_sw = "Write C++ firmware for our devices, add unit tests, use Git and code reviews, build CI/CD pipelines, debug with GDB."
assert desc_ok(strong) and desc_ok(embedded_sw), (desc_score(strong), desc_score(embedded_sw))
assert not desc_ok(it_support) and not desc_ok(hardware), (desc_score(it_support), desc_score(hardware))
assert not desc_ok("Support end users, troubleshoot laptops, reset passwords, maintain SQL reports.")
# closed-job detection: a role vanishing from a complete feed is marked closed; reappearing reopens it
import board, tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())  # rebuild() persists state; keep the test away from the real files
for f in ("SEEN", "VERDICTS", "DETECTED", "CLOSED"):
    setattr(board, f, tmp / f)
feed = [[{"company": "acme", "title": "Software Engineer Intern", "location": "NYC", "url": "https://boards.greenhouse.io/acme/jobs/1", "posted": 1, "source": "Greenhouse", "approx": False}]]
board.FETCHERS["greenhouse"] = lambda b: feed[0]
board.fetch(("greenhouse", "acme")); board.rebuild()
assert "https://boards.greenhouse.io/acme/jobs/1" in board.shown_urls
feed[0] = []
board.fetch(("greenhouse", "acme"))
assert "https://boards.greenhouse.io/acme/jobs/1" in board.closed, "vanished role should be closed"
feed[0] = [{"company": "acme", "title": "Software Engineer Intern", "location": "NYC", "url": "https://boards.greenhouse.io/acme/jobs/1", "posted": 1, "source": "Greenhouse", "approx": False}]
board.fetch(("greenhouse", "acme"))
assert "https://boards.greenhouse.io/acme/jobs/1" not in board.closed, "reappearing role should reopen"
print("ok")

# phone alerts: priority rule + message shape (company/title headline, location/source below, tap opens apply link)
import time as _t
now = _t.time()
base = {"company": "Acme", "title": "Software Engineer Intern", "location": "Atlanta, GA", "url": "https://x/apply", "source": "Greenhouse",
        "kind": "swe", "score": None, "detected": now - 60, "posted": now - 60}
assert board.is_priority(base, now)
assert not board.is_priority({**base, "location": "London, UK"}, now)
assert not board.is_priority({**base, "title": "Software Engineer Intern - PhD"}, now)
assert not board.is_priority({**base, "title": "Software Engineer Intern 2026"}, now)
assert not board.is_priority({**base, "detected": now - 3 * 3600}, now)
sent = []
board.ntfy = lambda *a, **k: sent.append((a, k)) or True
board.NOTIFIED = tmp / "notified"; board.notified = set(); board.WATCH = tmp / "watch"
board.jobs = [base]; board.status["batches"] = []
board.send_alerts(); board.send_alerts()
assert len(sent) == 1, sent  # once only
(title, message), kw = sent[0]
assert title == "Acme: Software Engineer Intern" and message.startswith("Atlanta, GA") and "Greenhouse" in message and kw["click"] == "https://x/apply", sent
print("ok")

# politeness: a 429 pauses the platform (honouring Retry-After) and later boards are skipped; a 403 blocks the board
import urllib.error, email.message
def raise_code(code, retry=None):
    def f(b):
        hdrs = email.message.Message()
        if retry: hdrs["Retry-After"] = retry
        raise urllib.error.HTTPError("u", code, "x", hdrs, None)
    return f
board.FETCHERS["lever"] = raise_code(429, "120")
board.fetch(("lever", "a"))
assert board.cooldown["lever"] > _t.time() + 100 and board.backoff["lever"] == 120
calls = []
board.FETCHERS["lever"] = lambda b: calls.append(b) or []
board.fetch(("lever", "b"))
assert not calls, "paused platform must not be called"
board.FETCHERS["ashby"] = raise_code(403)
board.fetch(("ashby", "x"))
assert ("ashby", "x") in board.dead and board.errlog["ashby"][-1][1] == "403"
r = board.health_report()
assert r["lever"]["rate_limited_1h"] == 1 and r["ashby"]["blocked_1h"] == 1 and r["lever"]["paused_until"] > 0
print("ok")

# scheduler: tier 1 = SWE history; three failures in a row -> checked weekly
board.swe_boards.add(("greenhouse", "acme")); assert board.tier(("greenhouse", "acme")) == 1
assert board.tier(("greenhouse", "never-seen-co")) == 3
board.health[("greenhouse", "gone")] = {"fails": 3}; assert board.interval(("greenhouse", "gone")) == board.INACTIVE_RETRY
print("ok")

# tier 4: checked and never had an internship -> hourly; unchecked boards stay tier 3 until their first result
board.health[("breezy", "no-interns")] = {"ok": 1}; assert board.tier(("breezy", "no-interns")) == 4
assert board.tier(("breezy", "unchecked-yet")) == 3
board.intern_boards.add(("breezy", "had-one")); board.health[("breezy", "had-one")] = {"ok": 1}; assert board.tier(("breezy", "had-one")) == 3
print("ok")

# verified coverage: counts a company once, only if its own board works and it had a US undergrad SWE internship
board.names[("greenhouse", "acme")] = "Acme"; board.health[("greenhouse", "acme")] = {"ok": 5}
board.verified_boards.add(("greenhouse", "acme")); board.verified_boards.add(("greenhouse", "dead-co"))
board.health[("greenhouse", "dead-co")] = {"err": 5}
c = board.coverage_report()["verified_companies_monitored_directly"]; assert c == 1, c
assert board.us_undergrad({"title": "Software Engineer Intern", "location": "Atlanta, GA"})
assert not board.us_undergrad({"title": "Software Engineer Intern, PhD", "location": "Atlanta, GA"})
assert not board.us_undergrad({"title": "Software Engineer Intern", "location": "London, UK"})
print("ok")

# broader SWE-type scope: data / ML / AI / security engineering go to the description check; analysts/research don't
for tt in ["Machine Learning Engineer Intern", "Data Engineering Intern", "AI Engineer Intern", "MLOps Intern", "AI Platform Intern"]:
    assert cand(tt), tt
for tt in ["Data Analyst Intern", "AI Research Scientist Intern", "Business Intelligence Intern", "GRC Intern - Cybersecurity"]:
    assert not cand(tt) and not is_swe({"title": tt}), tt
research = "Conduct research on novel ML methods in Python, publish papers, statistical analysis of experiments, literature review."
assert not desc_ok(research), desc_score(research)
print("ok")

# pay status: confirmed paid / confirmed unpaid / unknown (unknown never counted as paid)
from board import pay_status, note_pay, pay_info
j1 = {"title": "Software Engineer Intern", "url": "https://x/1"}; note_pay(j1, "Pay range: $38 - $45/hr plus housing stipend")
j2 = {"title": "Software Engineer Intern", "url": "https://x/2"}; note_pay(j2, "This is an unpaid internship for academic credit only.")
j3 = {"title": "Software Engineer Intern", "url": "https://x/3"}
assert pay_status(j1, "") == "paid" and pay_status(j2, "") == "unpaid" and pay_status(j3, "") == "unknown"
assert pay_status(j3, "$60/hr") == "paid"
print("ok")

assert is_swe({"title": "AI Developer Intern"})  # a developer role building AI software is in scope
print("ok")
