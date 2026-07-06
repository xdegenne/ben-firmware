#!/usr/bin/env bash
# run-banc — orchestre le banc de test BEN (inject scénarios -> DUT -> assert).
#
#  Aujourd'hui : injecteur LOCAL (ben-ops), listener sur ben-0001 (qui a la radio).
#  À terme (ben-ops avec antenne LoRa) : tout local -> export LISTENER_SSH="" et LDIR local.
#
# Prérequis : ben-lora-receiver ARRÊTÉ sur le récepteur (sinon conflit radio + pollution DB).
set -euo pipefail

LISTENER_SSH="${LISTENER_SSH:-ssh -o ConnectTimeout=10 pi@ben-0001.local}"   # "" quand tout sur ben-ops
LDIR="${LDIR:-/opt/ben/repo/src/pi/lora-receiver}"
INJDIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== 1. arrêt du receiver de prod (protège la base) + (re)démarrage du listener ASSERT ==="
$LISTENER_SSH "sudo systemctl stop ben-lora-receiver 2>/dev/null || true;
               sudo systemctl stop banclisten 2>/dev/null || true;
               sudo systemctl reset-failed banclisten 2>/dev/null || true;
               sudo systemd-run --unit=banclisten --collect python3 $LDIR/banc_listen.py"
sleep 3

echo "=== 2. injection des scénarios (1 passe complète) ==="
python3 "$INJDIR/banc_inject.py" /dev/ttyAMA0

echo "=== 3. laisse l'Arduino flusher les derniers batchs (60 s) ==="
sleep 60

echo "=== 4. RAPPORT ==="
$LISTENER_SSH "sudo pkill -USR1 -f banc_listen.py 2>/dev/null; sleep 1;
               journalctl -u banclisten --no-pager -n 60 | sed -n '/RAPPORT BANC/,/scénarios validés/p'"

echo ""
echo "Pour relancer le receiver de prod :  $LISTENER_SSH \"sudo systemctl start ben-lora-receiver\""
echo "Pour arrêter le listener          :  $LISTENER_SSH \"sudo systemctl stop banclisten\""
