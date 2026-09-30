import os, tempfile
from store import Store, normalize_url, source_key, fallback_key, plan, season_of, company_norm

# URL normalization: tracking / presentation params never make a "new" job (the Qorvo case)
assert normalize_url("https://careers.qorvo.com/job/Richardson-Full-Stack-Web-Developer-Intern-TX-75080/1424716200/") == \
       normalize_url("https://careers.qorvo.com/job/Richardson-Full-Stack-Web-Developer-Intern-TX-75080/1424716200/?a=1&ats=successfactors")
assert normalize_url("https://x.io/jobs/1?utm_source=Simplify&ref=Simplify") == normalize_url("https://WWW.x.io/jobs/1/#apply")
assert normalize_url("https://acme.com/careers?gh_jid=123&utm_source=li") == "acme.com/careers?gh_jid=123"

# source job IDs, same key whichever site lists the job
assert source_key("https://boards.greenhouse.io/stripe/jobs/8172487") == source_key("https://stripe.com/jobs/search?gh_jid=8172487") == "gh:8172487"
assert source_key("https://job-boards.greenhouse.io/stripe/jobs/8172487?utm_source=Simplify") == "gh:8172487"
wd1 = "https://motorolasolutions.wd5.myworkdayjobs.com/Careers/job/Krakow-Poland/Intern-Software-Developer--C---_R69046"
wd2 = "https://motorolasolutions.wd5.myworkdayjobs.com/en-US/Careers/job/Krakow/Intern-Software-Developer--C---_R69046?source=x"
assert source_key(wd1) == source_key(wd2) == "workday:motorolasolutions:R69046"
assert source_key("https://wd5.myworkdaysite.com/recruiting/microchiphr/External/job/AZ/Intern_R2176-25") == "workday:microchiphr:R2176-25"
assert source_key("https://jobs.lever.co/palantir/0c1d4b8e-1111-2222-3333-444455556666/apply").startswith("lever:0c1d4b8e")
assert source_key("https://jobs.ashbyhq.com/ramp/fcf118cc-521a-4a62-9d13-945e5b6e3cb8/application") == "ashby:fcf118cc-521a-4a62-9d13-945e5b6e3cb8"
assert source_key("https://careers.qorvo.com/job/Richardson-X-TX-75080/1424716200/?a") == "sf:careers.qorvo.com:1424716200"
assert source_key("https://careers.amd.com/jobs/86868?icims=1") == "icims:careers.amd.com:86868"
assert source_key("https://www.amazon.jobs/en/jobs/10559746/software-intern") == "amazon:10559746"
assert source_key("https://acme.com/careers/software-intern") is None

# season/term goes into the fallback key: next year's posting is a different job
assert season_of("Software Engineer Intern - Summer 2027") == "summer 2027"
assert season_of("2027 Summer SWE Intern") == "summer 2027"
assert season_of("Software Intern", "Fall 2026") == "fall 2026"
assert fallback_key("Acme", "SWE Intern Summer 2027", "Atlanta, GA") != fallback_key("Acme", "SWE Intern Summer 2028", "Atlanta, GA")
assert fallback_key("Acme Inc.", "SWE Intern - Summer 2027", "Atlanta, GA") == fallback_key("ACME", "SWE Intern (Summer 2027)", "Atlanta")
assert company_norm("Motorola Solutions, Inc.") == "motorola solutions"

# cross-source merge
gh = {"company": "Stripe", "title": "Software Engineer Intern", "location": "SF", "url": "https://boards.greenhouse.io/stripe/jobs/8172487", "source": "Greenhouse", "posted": 100}
simplify_copy = {**gh, "url": "https://boards.greenhouse.io/stripe/jobs/8172487?utm_source=Simplify", "source": "Simplify", "posted": 200}
assert len(plan([gh, simplify_copy])) == 1                     # same Greenhouse ID -> one posting
p = plan([simplify_copy, gh])[0]
assert p["best"]["source"] == "Greenhouse"                      # company's own copy wins as the apply link
req_a = {"company": "Autodesk", "title": "SWE Intern", "location": "SF", "url": wd1, "source": "Workday"}
req_b = {**req_a, "url": wd1.replace("R69046", "R68703")}
assert len(plan([req_a, req_b])) == 2                           # two requisitions, same title = two real jobs
custom = {"company": "Acme", "title": "Software Engineer Intern - Summer 2027", "location": "Atlanta, GA", "url": "https://acme.com/careers/1", "source": "Simplify"}
custom2 = {**custom, "url": "https://acme.com/careers/apply?x=1", "source": "SpeedyApply"}
assert len(plan([custom, custom2])) == 1                        # no ID anywhere: company+title+location+season merge
nextyear = {**custom, "title": "Software Engineer Intern - Summer 2028", "url": "https://acme.com/careers/2"}
assert len(plan([custom, nextyear])) == 2

# alerts: first insert only, one per role (not per city), never on re-scrape / repost / edit / first board check
db = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
new = db.sync([gh, simplify_copy])
assert len(new) == 1 and db.should_alert(new[0])
db.mark_alerted(new[0])
assert db.sync([gh, simplify_copy]) == []                                      # re-scrape: nothing new
assert db.sync([{**gh, "url": gh["url"] + "?gh_src=abc"}]) == []                # repost / same ID, new tracking param
assert db.sync([{**gh, "title": "Software Engineer Intern (Summer 2027)"}]) == []  # edit: updated, not new
nyc = {**gh, "location": "New York, NY", "url": "https://boards.greenhouse.io/stripe/jobs/8172999"}
n2 = db.sync([nyc])
assert len(n2) == 1 and not db.should_alert(n2[0])                             # same role, another city: no 2nd alert
old = {"company": "Oldco", "title": "SWE Intern", "location": "Remote", "url": "https://boards.greenhouse.io/oldco/jobs/5", "source": "Greenhouse"}
n3 = db.sync([old], baseline_urls={old["url"]})
assert len(n3) == 1 and not db.should_alert(n3[0])                             # already there when we started watching
# alerts sent before the database existed carry over
fresh = Store(os.path.join(tempfile.mkdtemp(), "t2.db"))
fresh.sync([gh])
assert fresh.import_alerted_urls(["https://boards.greenhouse.io/stripe/jobs/8172487/?utm_source=x"]) == 1
n4 = fresh.sync([nyc])
assert not fresh.should_alert(n4[0])
print("ok")
