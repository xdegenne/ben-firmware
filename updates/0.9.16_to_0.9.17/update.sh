#!/usr/bin/env bash
# update.sh — → pi-0.9.17 : le lecteur TIC vérifie la parité au lieu de la jeter.
#
# ═══ CE QUE LIVRE CETTE VERSION ══════════════════════════════════════════════════════════════
#
#   1. `src/pi/tic-reader/tic_parite.py` — FICHIER NOUVEAU. `octet_valide()`, le contrôle de
#      parité, isolé exprès pour être testable sans matériel (`main_uart.py` importe
#      `RPi.GPIO` au chargement, donc tant que la fonction y vivait « testable sans boîtier »
#      était faux).
#   2. `src/pi/tic-reader/main_uart.py` — l'appelle, et condamne le GROUPE entier dès qu'un de
#      ses octets échoue.
#   3. `src/pi/tic-reader/test_read_frame.py` — le banc, 11 cas. Pas exécuté ici (il ne tourne
#      pas sur le boîtier), livré pour que le dépôt soit cohérent.
#
#   AUCUNE migration, AUCUNE table, AUCUNE colonne. Le schéma ne change pas, donc pas besoin de
#   redémarrer `ben-local-api` avant le lecteur — cette règle-là ne vaut que quand `_SCHEMA`
#   gagne quelque chose.
#
# ═══ POURQUOI CETTE VERSION EXISTE ═══════════════════════════════════════════════════════════
#
#   La TIC est en 7E1 : chaque caractère porte un bit de parité. Le port étant ouvert en 8N1,
#   l'UART nous le remettait fidèlement — et `& 0x7F` l'effaçait sans jamais le regarder.
#
#   Un boîtier du parc déclarait QUATRE compteurs alors qu'il n'en lit qu'un : le même ADCO,
#   corrompu d'un caractère, à chaque fois sur le BIT 6 (`'0'`=0x30 → `'p'`=0x70).
#
#   🚨 Le checksum ne peut pas le voir : il vaut (somme & 0x3F) + 0x20, donc il ne voit la
#      somme que MODULO 64. Basculer le bit 6 ajoute exactement 64, et le masque jette la
#      retenue. Invisible PAR CONSTRUCTION.
#
#   ⭐ Et le correctif est LOGICIEL, pas un changement de port : mesuré, `pyserial` n'active pas
#      `INPCK` même avec `PARITY_EVEN`. Passer en 7E1 n'aurait rien changé tout en donnant
#      l'air d'un correctif.
#
# ═══ 🚨 LE RISQUE PROPRE À CETTE UPDATE : UN IMPORT AU CHARGEMENT ═════════════════════════════
#
#   `main_uart.py` fait `from tic_parite import octet_valide` AU CHARGEMENT. Si `tic_parite.py`
#   manque ou ne compile pas, le lecteur ne démarre PAS DU TOUT — plus aucune mesure, sur un
#   boîtier qu'on ne peut pas dépanner à distance. C'est le seul geste qui peut rendre ce
#   boîtier muet.
#
#   ⇒ On vérifie la PRÉSENCE, la SYNTAXE et le COMPORTEMENT de ce fichier AVANT de redémarrer
#     quoi que ce soit.
#
# ═══ 🚨 ET LE RISQUE SYMÉTRIQUE : UN CONTRÔLE TROP SÉVÈRE, EN SILENCE ═════════════════════════
#
#   Un `octet_valide` inversé rejetterait 100 % des octets. Le lecteur tournerait, le service
#   serait « active », les journaux seraient calmes — et plus une seule mesure ne serait
#   enregistrée. C'est le pire mode de panne possible : l'absence lue comme absence de panne.
#
#   ⇒ Le contrôle d'effet EXIGE qu'une trame soit lue APRÈS le redémarrage. Mais seulement si
#     le boîtier en lisait AVANT : sinon on ferait échouer l'update pour un câble débranché,
#     l'update rejouerait à chaque tick de 10 minutes, et on aurait brûlé un tag pour un
#     défaut dont on n'est pas responsable. C'est la mécanique qui a brûlé pi-0.9.12.
#
# UNIVERSEL (LoRa et filaire) : le paquet est le même partout. Sur un boîtier LoRa,
# `ben-tic-reader` n'existe pas — le script le dit et s'arrête là, sans échouer.
# Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.17"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
TICDIR="$REPO/src/pi/tic-reader"
SRC_PARITE="$TICDIR/tic_parite.py"
SRC_READER="$TICDIR/main_uart.py"
API="http://127.0.0.1:8087/health"

# ── Préflight : le checkout doit être complet ────────────────────────────────────────────────
for f in "$SRC_PARITE" "$SRC_READER"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.9.17 incomplet ?)"
done

# ⚠️ SURTOUT PAS `python3 -m py_compile` : il ÉCRIT dans __pycache__, qui appartient à root sur
# les boîtiers du parc alors que l'OTA tourne en `ben` → Permission denied, update avortée, tag
# brûlé pour un défaut de droits. `ast.parse` ne touche pas au disque.
python3 - "$SRC_PARITE" "$SRC_READER" <<'PYEOF' || fail "un fichier Python livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a)
    ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# Le lecteur DOIT importer le nouveau module, sinon le checkout n'est pas celui qu'on croit et
# le correctif serait livré sans être branché — une update qui ne change rien, en silence.
grep -q "from tic_parite import octet_valide" "$SRC_READER" \
    || fail "main_uart.py n'importe pas tic_parite — le checkout n'est pas celui attendu"

# 🚨 LE contrôle qui compte, et il est DÉTERMINISTE : on éprouve le comportement de
#    `octet_valide` hors ligne, sans câble, sans compteur, avant de redémarrer.
#
#    Trois affirmations, et la troisième est le TÉMOIN : sans elle, un `octet_valide` qui
#    refuserait TOUT satisferait les deux premières.
python3 - "$TICDIR" <<'PYEOF' || fail "octet_valide ne se comporte pas comme attendu"
import sys
sys.path.insert(0, sys.argv[1])
from tic_parite import octet_valide

def sur_le_fil(c):                       # 7 bits de donnée + parité PAIRE, comme le compteur
    d = ord(c) & 0x7F
    return d | ((bin(d).count("1") & 1) << 7)

LIGNE = "ADCO 061947000000"
ko = []
# 1. tout caractère sain d'un groupe réel doit passer
for c in LIGNE:
    if not octet_valide(sur_le_fil(c)):
        ko.append(f"{c!r} sain est refusé")
# 2. le bit 6 basculé — l'angle mort du checksum — doit être attrapé
for c in LIGNE:
    if octet_valide(sur_le_fil(c) ^ 0x40):
        ko.append(f"{c!r} avec le bit 6 basculé est accepté")
# 3. ⚖️ LE TÉMOIN : il ne doit pas tout refuser, sinon le lecteur se taira
if not any(octet_valide(sur_le_fil(c)) for c in LIGNE):
    ko.append("octet_valide refuse TOUT — le lecteur n'enregistrerait plus rien")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr)
    sys.exit(1)
PYEOF
log "préflight OK (les deux fichiers compilent, l'import est branché, octet_valide éprouvé)"

# ── Boîtier sans lecteur filaire : rien à faire, et surtout pas d'échec ──────────────────────
if ! systemctl cat ben-tic-reader.service >/dev/null 2>&1; then
    log "ben-tic-reader absent (boîtier LoRa) — le code est livré, rien à redémarrer"
    log "✓ update OK"
    exit 0
fi

# ── ÉTAT AVANT, pour que le contrôle d'effet ait un point de comparaison ─────────────────────
#
# ⭐ C'est ce relevé qui rend le contrôle honnête : on n'exigera une lecture APRÈS que si le
#    boîtier en faisait AVANT. Un câble TIC débranché ne doit pas faire échouer l'update.
LISAIT_AVANT=0
TS_AVANT=0
if systemctl is-active --quiet ben-local-api.service; then
    AVANT="$(python3 - "$API" <<'PYEOF' || true
import json, sys, urllib.request
try:
    d = json.loads(urllib.request.urlopen(sys.argv[1], timeout=15).read())
except Exception:
    sys.exit(0)
ts, now = d.get("last_tic_ts") or 0, d.get("now") or 0
print(f"{ts} {1 if ts and (now - ts) < 300 else 0}")
PYEOF
)"
    if [ -n "${AVANT:-}" ]; then
        TS_AVANT="$(echo "$AVANT" | cut -d' ' -f1)"
        LISAIT_AVANT="$(echo "$AVANT" | cut -d' ' -f2)"
    fi
fi
if [ "$LISAIT_AVANT" = "1" ]; then
    log "avant : le boîtier lisait (dernière trame à $TS_AVANT) — une lecture sera EXIGÉE après"
else
    warn "avant : aucune lecture récente — la lecture ne sera pas exigée après (câble ? LoRa ?)"
fi

# ── Redémarrage du lecteur ───────────────────────────────────────────────────────────────────
sudo systemctl restart ben-tic-reader.service
log "ben-tic-reader redémarré"

# ── Contrôle d'effet ─────────────────────────────────────────────────────────────────────────
#
# On vérifie CE DONT L'UPDATE EST RESPONSABLE :
#   • le lecteur est debout (il ne plante pas à l'import du nouveau module) ;
#   • il LIT encore — si et seulement si il lisait avant ;
#   • la base reste lisible.
#
# On ne vérifie PAS le nombre d'octets rejetés : il dépend du bruit de la ligne, pas de nous.

sleep 8
systemctl is-active --quiet ben-tic-reader.service \
    || fail "ben-tic-reader ne démarre plus — voir « journalctl -u ben-tic-reader -n 40 »"
log "✓ lecteur debout"

if [ "$LISAIT_AVANT" = "1" ]; then
    # La détection de mode prend ~7 s, puis une trame arrive chaque seconde et le décalage
    # observé va de 1 à 15 s. 90 s est donc très large — et bien en dessous du tick de 10 min
    # de l'agent, qui ne doit pas se chevaucher.
    python3 - "$API" "$TS_AVANT" <<'PYEOF' || fail "le lecteur ne lit plus après le redémarrage — le contrôle de parité rejette-t-il tout ?"
import json, sys, time, urllib.request
api, avant = sys.argv[1], int(sys.argv[2])
fin = time.time() + 90
dernier = None
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(api, timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"
        time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    ts = d.get("last_tic_ts") or 0
    if ts > avant:
        print(f"  trame lue après le redémarrage (last_tic_ts {avant} → {ts})")
        sys.exit(0)
    dernier = f"last_tic_ts toujours à {ts}, inchangé depuis {avant}"
    time.sleep(5)
print(f"  {dernier}", file=sys.stderr)
sys.exit(1)
PYEOF
    log "✓ le lecteur lit toujours, et la base est lisible"
elif systemctl is-active --quiet ben-local-api.service; then
    python3 - "$API" <<'PYEOF' || fail "l'API locale ne répond plus sur /health"
import json, sys, urllib.request
try:
    d = json.loads(urllib.request.urlopen(sys.argv[1], timeout=15).read())
except Exception as e:
    print(f"  /health KO : {e}", file=sys.stderr); sys.exit(1)
if not d.get("db"):
    print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ base lisible (lecture non exigée, elle ne se faisait pas avant)"
fi

# Informatif seulement — ce n'est pas cette update qui touche au publisher.
if systemctl is-active --quiet ben-publisher.service; then
    log "✓ publisher toujours actif"
else
    warn "publisher inactif — à vérifier, mais sans lien avec cette update"
fi

log "✓ le lecteur TIC vérifie désormais la parité"
log "  à surveiller :"
log "    journalctl -u ben-tic-reader | grep 'hors parité'"
log "    → « N octet(s) rejeté(s) sur parité dans M trame(s) en 5 min » si la ligne est bruitée,"
log "      et RIEN du tout si elle est saine : le silence est l'information."
log "✓ update OK"
