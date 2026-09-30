import calendar
from datetime import datetime, timezone
from signals import extract, combine, company_matcher, claimed_time

match = company_matcher({"Capital One", "Jane Street", "Google", "Ramp", "Microsoft"})
mon = calendar.timegm(datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc).timetuple())  # a Monday, 11am ET
post = lambda title, text="", tier="community", author="a": {"id": title, "source": "r/csMajors", "author": author,
                                                            "link": "x", "posted": mon, "title": title, "text": text, "tier": tier}

s = extract(post("Capital One SWE internship applications open tomorrow at 9am ET"), match)
assert len(s) == 1 and s[0]["company"] == "Capital One" and s[0]["tier"] == "community", s
assert datetime.fromtimestamp(s[0]["claimed"], timezone.utc).strftime("%m-%d %H") == "09-29 13", s[0]["claimed"]
assert not extract(post("Rejected from Google after final round"), match)            # chatter, no drop claim
assert not extract(post("Ramp is the best company"), match)                         # common word, no context
assert extract(post("Just heard from recruiter: Jane Street internships opening Monday"), match)[0]["cites_recruiter"]
emp = extract(post("I work at Microsoft, our intern apps open next week"), match)
assert emp[0]["tier"] == "employee", emp

# independent sources on the same company/day compound; the same author twice does not
a = extract(post("Capital One SWE intern apps open Tuesday", author="u1"), match)
b = extract(post("Cap One internships dropping tomorrow", author="u2"), match)
c = extract(post("Capital One opens tomorrow for SWE interns", author="u1"), match)
one, = combine(a + c)
two, = combine(a + b)
assert two["confidence"] > one["confidence"] and two["independent"] == 2, (one, two)
assert claimed_time("opens soon", mon) == (None, "soon")
print("ok")

# Likely Dropping Soon: history near its date + ATS activity + agreeing community signals rank a company;
# a company that already has SWE roles open this cycle is left out
import time, board
now = time.time()
board.history = {"Acme": now + 3 * 86400 - 365 * 86400 * 0, "Openco": now}
board.intern_activity = {"acme": 2}
board.jobs = [{"company": "Openco", "kind": "swe", "posted": now - 86400, "detected": now - 86400, "title": "Software Engineer Intern"}]
# fresh rumor posts made "now" (the fixed-date posts above are only for date-parsing checks and are now in the past)
live = post("Capital One SWE intern apps open tomorrow", author="u1") | {"posted": int(now)}
live2 = post("Cap One internships dropping tomorrow", author="u2") | {"posted": int(now)}
board.sig_store = {x["id"]: x for x in extract(live, match) + extract(live2, match)}
r = board.likely_soon()
names = [c["company"] for c in r["companies"]]
assert "Openco" not in names, names
acme = next(c for c in r["companies"] if c["company"] == "Acme")
assert acme["score"] >= 40 and len(acme["reasons"]) == 2, acme
assert any(c["company"] == "Capital One" for c in r["companies"]), names
print("ok")
