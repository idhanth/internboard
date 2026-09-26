#!/bin/bash
# Deploy / update the board on an Ubuntu server (Oracle Cloud free tier).
#   ./deploy.sh <server-ip>
# First run installs everything; later runs just push code and restart. Tracker + seen data on the server are kept.
set -euo pipefail
IP=$1
KEY=~/.ssh/oracle_board
HOST="$(echo "$IP" | tr . -).sslip.io"   # free hostname for the IP, so Caddy can get an HTTPS certificate
LOGIN="$(dirname "$0")/.site-login"      # site username/password, generated once, kept only on this Mac
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new ubuntu@$IP"

[ -f "$LOGIN" ] || printf "user=me\npassword=%s\n" "$(openssl rand -base64 18 | tr -d '/+=')" > "$LOGIN"
PASS=$(sed -n 's/^password=//p' "$LOGIN")

# Ship code only: exactly the files tracked in git. Never data (the server's tracker, watchlist, detection history
# etc. are its own and are restored from backups, not from this Mac) and never secrets (.env, .site-login).
(cd "$(dirname "$0")" && git ls-files) | rsync -az -e "ssh -i $KEY" --files-from=- "$(dirname "$0")/" "ubuntu@$IP:internboard/"

# --- backups: age key on server + this Mac only; write-only-to-backups-repo deploy key on the server ---
AGEKEY=~/.config/internboard/backup-age.key
$SSH 'command -v age >/dev/null || (sudo apt-get update -qq && sudo apt-get install -y -qq age); mkdir -p ~/.config/internboard; chmod 700 ~/.config/internboard'
if [ -f "$AGEKEY" ]; then
  $SSH 'test -f ~/.config/internboard/backup-age.key' || scp -q -i $KEY "$AGEKEY" "ubuntu@$IP:.config/internboard/backup-age.key"
else
  $SSH 'test -f ~/.config/internboard/backup-age.key || age-keygen -o ~/.config/internboard/backup-age.key 2>/dev/null'
  mkdir -p ~/.config/internboard && chmod 700 ~/.config/internboard
  scp -q -i $KEY "ubuntu@$IP:.config/internboard/backup-age.key" "$AGEKEY"
fi
chmod 600 "$AGEKEY"
$SSH 'chmod 600 ~/.config/internboard/backup-age.key; age-keygen -y ~/.config/internboard/backup-age.key > ~/.config/internboard/backup-age.pub'
if ! $SSH 'test -f ~/.ssh/backup_deploy'; then
  $SSH 'ssh-keygen -q -t ed25519 -N "" -C internboard-backups -f ~/.ssh/backup_deploy'
  $SSH 'cat ~/.ssh/backup_deploy.pub' | gh repo deploy-key add - --repo idhanth/internboard-backups --allow-write --title "internboard server $IP"
fi

$SSH "HOST=$HOST PASS='$PASS' bash -s" <<'REMOTE'
set -euo pipefail
if ! command -v caddy >/dev/null; then
  sudo apt-get update -qq && sudo apt-get install -y -qq caddy iptables-persistent
  # Oracle's Ubuntu image blocks everything but SSH at the OS firewall too
  sudo iptables -I INPUT 5 -p tcp --dport 80 -j ACCEPT
  sudo iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT
  sudo netfilter-persistent save
fi
HASH=$(caddy hash-password --plaintext "$PASS")
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$HOST {
  basicauth {
    me $HASH
  }
  encode gzip
  reverse_proxy 127.0.0.1:8000
}
EOF
sudo tee /etc/systemd/system/internboard.service >/dev/null <<EOF
[Unit]
Description=SWE intern board
After=network-online.target
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/internboard
ExecStart=/usr/bin/python3 board.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF
# re-scan Common Crawl for new company job boards every Sunday
( crontab -l 2>/dev/null | grep -v discover.py || true  # a fresh server has no crontab yet
  echo "0 4 * * 0 cd /home/ubuntu/internboard && python3 discover.py >> discover.log 2>&1" ) | crontab -
# backups repo checkout (deploy key scoped to that one repo)
grep -q github-backups ~/.ssh/config 2>/dev/null || printf "Host github-backups\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/backup_deploy\n  IdentitiesOnly yes\n" >> ~/.ssh/config
ssh-keygen -F github.com >/dev/null || ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null
[ -d ~/backups-repo ] || git clone -q git@github-backups:idhanth/internboard-backups.git ~/backups-repo
# nightly encrypted backup at 07:00 UTC (3 AM Eastern)
( crontab -l 2>/dev/null | grep -v backup.sh || true
  echo "0 7 * * * /home/ubuntu/internboard/backup.sh >> /home/ubuntu/internboard/backup.log 2>&1" ) | crontab -
# automatic security updates; reboot if one needs it at 08:00 UTC (4 AM Eastern), the board restarts itself
sudo apt-get install -y -qq unattended-upgrades >/dev/null
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null
printf 'Unattended-Upgrade::Automatic-Reboot "true";\nUnattended-Upgrade::Automatic-Reboot-Time "08:00";\n' | sudo tee /etc/apt/apt.conf.d/52internboard-reboot >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now internboard caddy
sudo systemctl restart internboard caddy
echo "deployed"
REMOTE
echo "Open https://$HOST  (login: me / password in $LOGIN)"
