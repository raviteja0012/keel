#!/usr/bin/env bash
# Provision a Keel node on Hetzner Cloud.
#
# Hetzner is the named host because it is the cheapest box that is not a
# compromise: real dedicated-ish vCPU, local NVMe (which is what SQLite WAL
# wants), a proper cloud firewall, and roughly $5/month all-in. Costs and the
# comparison with Fly.io, Railway and the India options are in
# docs/DEPLOYMENT-LINUX.md §2 — read that before picking a region, because for
# an India-resident operator the choice is a legal one before it is a technical
# one (ARCHITECTURE-V3 §6).
#
# Requires: hcloud CLI, authenticated (`hcloud context create keel`).
#
# Usage:
#   ./provision-hetzner.sh keel-usa   ash  ~/.ssh/id_ed25519.pub
#   ./provision-hetzner.sh keel-india sin  ~/.ssh/id_ed25519.pub
#
# Regions: fsn1 nbg1 hel1 (EU) | ash hil (US) | sin (Singapore — closest
# Hetzner region to India; see §2 for Mumbai alternatives).
set -euo pipefail

NAME="${1:-}"
LOCATION="${2:-fsn1}"
SSH_KEY_FILE="${3:-$HOME/.ssh/id_ed25519.pub}"
TYPE="${KEEL_SERVER_TYPE:-cx22}"     # 2 vCPU / 4 GB / 40 GB NVMe
IMAGE="${KEEL_IMAGE:-ubuntu-24.04}"
TZ_NAME="${KEEL_TZ:-UTC}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -n "$NAME" ] || { echo "usage: provision-hetzner.sh <name> [location] [ssh-pubkey]" >&2; exit 1; }
[ -f "$SSH_KEY_FILE" ] || { echo "!! no public key at $SSH_KEY_FILE" >&2; exit 1; }
command -v hcloud >/dev/null || { echo "!! hcloud CLI not found: https://github.com/hetznercloud/cli" >&2; exit 1; }

PUBKEY="$(cat "$SSH_KEY_FILE")"
USERDATA="$(mktemp)"; trap 'rm -f "$USERDATA"' EXIT

# `sed` rather than envsubst: the cloud-init file contains $() shell expansions
# for the docker apt repo that must survive to the target verbatim.
sed -e "s|__SSH_KEY__|${PUBKEY}|" -e "s|__TZ__|${TZ_NAME}|" \
    "$HERE/cloud-init.yaml" > "$USERDATA"

echo "==> $NAME  type=$TYPE  location=$LOCATION  image=$IMAGE  tz=$TZ_NAME"

# SSH key registered with the project so the rescue console works too.
KEYNAME="keel-$(printf '%s' "$PUBKEY" | sha256sum | cut -c1-8)"
hcloud ssh-key describe "$KEYNAME" >/dev/null 2>&1 || \
  hcloud ssh-key create --name "$KEYNAME" --public-key "$PUBKEY"

# ---------------------------------------------------------------------------
# Firewall: inbound SSH only.
#
# Every Keel port is published on 127.0.0.1 by docker-compose.yml, so there is
# genuinely nothing else to open. This firewall runs on Hetzner's side of the
# NIC, which is the one place a rule cannot be bypassed by docker's iptables
# chains — the trap that makes host-level ufw unreliable in front of published
# container ports. If you ever publish a port to 0.0.0.0, this is what is still
# standing between it and the internet.
# ---------------------------------------------------------------------------
FWNAME="keel-ssh-only"
if ! hcloud firewall describe "$FWNAME" >/dev/null 2>&1; then
  hcloud firewall create --name "$FWNAME"
  hcloud firewall add-rule "$FWNAME" --direction in --protocol tcp --port 22 \
    --source-ips 0.0.0.0/0 --source-ips ::/0 --description "ssh"
fi
echo "    firewall '$FWNAME': inbound tcp/22 only"
echo "    tighten it to your own address when you have a static one:"
echo "      hcloud firewall delete-rule $FWNAME --direction in --protocol tcp --port 22 --source-ips 0.0.0.0/0 --source-ips ::/0"

hcloud server create \
  --name "$NAME" \
  --type "$TYPE" \
  --image "$IMAGE" \
  --location "$LOCATION" \
  --ssh-key "$KEYNAME" \
  --firewall "$FWNAME" \
  --user-data-from-file "$USERDATA"

IP="$(hcloud server ip "$NAME")"
echo
echo "==> $NAME is up at $IP"
echo "    cloud-init takes another 2-4 minutes (docker install + apt upgrade)."
echo
echo "    watch it:   ssh keel@$IP 'cloud-init status --wait && docker --version'"
echo
echo "    then:"
echo "      ssh keel@$IP"
echo "      git clone <your PRIVATE repo url> /opt/keel/slc-trading-bot"
echo "      cd /opt/keel/slc-trading-bot"
echo "      docker compose build && docker compose up -d"
echo "      docker compose exec engine python tests/test_risk_rails.py"
echo
echo "    reach the control dashboard (it is not on any public interface):"
echo "      ssh -N -L 8767:127.0.0.1:8767 keel@$IP"
echo "      open http://127.0.0.1:8767"
echo "      token: ssh keel@$IP 'cd /opt/keel/slc-trading-bot && scripts/deploy/keel-token.sh'"
echo
echo "    LICENSE.md forbids distribution outside the team. Clone from a PRIVATE"
echo "    repo and never push this image to a public registry."
