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

rsync -az -e "ssh -i $KEY" --exclude '__pycache__' --exclude 'tracker.json' --exclude 'seen.json' --exclude '*.txt' \
  --exclude '.site-login' --exclude '.env' --exclude '*.err' --exclude '*.log' "$(dirname "$0")/" "ubuntu@$IP:internboard/"

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
sudo systemctl daemon-reload
sudo systemctl enable --now internboard caddy
sudo systemctl restart internboard caddy
echo "deployed"
REMOTE
echo "Open https://$HOST  (login: me / password in $LOGIN)"
