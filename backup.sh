#!/bin/bash
# Nightly (cron, on the server): encrypt the board's state with age and push it to the private backups repo.
# One commit per night, so any day can be restored (see restore.sh).
set -euo pipefail
cd ~/internboard
FILES="tracker.json watch.json detected.json seen.json closed.json verdicts2.json signals.json notified.json history.json
       boards.json discover_done.json pages.json .env"
tar czf /tmp/state.tgz $(ls $FILES 2>/dev/null)
age -r "$(cat ~/.config/internboard/backup-age.pub)" -o ~/backups-repo/state.tgz.age /tmp/state.tgz
rm -f /tmp/state.tgz
cd ~/backups-repo
git add state.tgz.age
git -c user.name=internboard -c user.email=internboard@localhost commit -q -m "backup $(date -u +%F)" || exit 0  # nothing changed
git push -q origin HEAD:main
