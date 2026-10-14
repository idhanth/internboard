# internboard

A personal software-engineering internship board and application tracker. Public lists like SimplifyJobs can lag hours behind a posting going live, and early applications matter, so this goes to the source: it polls company job boards directly and alerts my phone when a new SWE internship appears.

## How it works

- **Sources** - aggregator lists (SimplifyJobs, SpeedyApply), company job boards discovered in Common Crawl's URL index (`discover.py`), and direct polling of Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable and other ATS APIs.
- **Tiered scheduler** (`board.py`) - a priority heap per tier feeds a thread pool. Companies with SWE-internship history are checked every minute, other intern-posting companies every few minutes, the long tail every 15. Per-platform concurrency caps, rate-limit backoff, ETag / 304 conditional requests, and a day-long pause for boards that start returning 403s.
- **Filtering** - `is_swe` decides whether a posting is actually a software internship (tested against real false positives in `test_filter.py`), plus US-undergrad and pay checks.
- **Dedupe** (`store.py`) - one row per real job no matter how many places list it, keyed on the source's own job ID parsed from the URL, stored in SQLite.
- **"Likely dropping soon"** (`signals.py`) - watches public Reddit and GitHub discussion for signs a company's internships are about to open.
- **Alerts** - push notifications through ntfy.

## Running it

```bash
python3 board.py        # stdlib only, serves the board on localhost
python3 discover.py     # optional: refresh boards.json from Common Crawl (weekly is plenty)
python3 test_filter.py  # tests are plain asserts; same for test_store.py and test_signals.py
```

## Deployment

Runs 24/7 on an Oracle Cloud free-tier Ubuntu server. `deploy.sh` ships the tracked code and restarts the service; secrets stay in `.env` on the server. State is encrypted with `age` and pushed nightly to a private backups repo (`backup.sh` / `restore.sh`). A GitHub Actions workflow (`monitor.yml`) acts as an outside watchdog that alerts if the server itself goes down.
