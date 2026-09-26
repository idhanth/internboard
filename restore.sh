#!/bin/bash
# Restore the board's state from the encrypted backups repo onto a server.
#   ./restore.sh <server-ip>                 latest backup
#   ./restore.sh <server-ip> 2026-10-03      the backup from that date (UTC)
# Decryption happens on the server; if the server lacks the key (fresh server), it's copied from this Mac.
set -euo pipefail
IP=$1; DAY=${2:-}
KEY=~/.ssh/oracle_board; AGEKEY=~/.config/internboard/backup-age.key
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new ubuntu@$IP"
$SSH 'test -f ~/.config/internboard/backup-age.key' || {
  $SSH 'mkdir -p ~/.config/internboard && chmod 700 ~/.config/internboard'
  scp -q -i $KEY "$AGEKEY" "ubuntu@$IP:.config/internboard/backup-age.key"
  $SSH 'chmod 600 ~/.config/internboard/backup-age.key'
}
$SSH "DAY='$DAY' bash -s" <<'REMOTE'
set -euo pipefail
cd ~/backups-repo && git pull -q
REV=HEAD
[ -n "$DAY" ] && REV=$(git rev-list -1 --before="$DAY 23:59:59 +0000" HEAD)
git show "$REV:state.tgz.age" > /tmp/state.tgz.age
echo "restoring backup from $(git log -1 --format=%cd --date=iso "$REV")"
age -d -i ~/.config/internboard/backup-age.key -o /tmp/state.tgz /tmp/state.tgz.age
sudo systemctl stop internboard
tar xzf /tmp/state.tgz -C ~/internboard
rm -f /tmp/state.tgz /tmp/state.tgz.age
sudo systemctl start internboard
echo "restored; board restarted"
REMOTE
