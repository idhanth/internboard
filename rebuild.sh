#!/bin/bash
# Rebuild everything on a brand-new Ubuntu server in one command (e.g. if Oracle reclaims the old one):
#   ./rebuild.sh <new-server-ip>
# = install + configure the board (deploy.sh), then restore the latest encrypted backup (restore.sh).
# Afterwards: update the monitor's SITE_URL secret if the IP changed (the script prints the command).
set -euo pipefail
cd "$(dirname "$0")"
./deploy.sh "$1"
./restore.sh "$1"
echo "If the address changed, run:  gh secret set SITE_URL --repo idhanth/internboard --body https://$(echo "$1" | tr . -).sslip.io"
