#!/usr/bin/env bash
# update.sh — 0.9.18 → pi-0.9.19 : NE JAMAIS ÉCRIRE SOUS UN pdl_index DEVINÉ.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   Un `pdl_index` doit toujours venir d'un ADCO lu. Trois portes le violaient (issue #6) :
#
#   ① `resolve_pdl` créait un PDL pour tout ADCO inconnu — son garde testait le VIDE, pas la
#      FORME. Or `.strip()` ne retire pas les octets NUL : `'\x00\x00'` passait, et deux octets
#      de rien ont créé un PDL portant 13 056 mesures sur un boîtier du parc.
#
#   ② `PDL_INDEX` valait 0 à l'amorce du lecteur filaire. Or 0 est l'index du PREMIER compteur
#      de tout boîtier : l'amorce et une vraie réponse étaient indiscernables. Un boîtier déplacé
#      puis redémarré écrivait ses premières mesures sous son ANCIEN compteur, en silence.
#
#   ③ Un ADCO non conforme condamne désormais la TRAME ENTIÈRE. On n'atteint ce contrôle que par
#      un groupe ayant passé LA PARITÉ ET LE CHECKSUM sans avoir la forme d'un ADCO — donc par
#      l'amputation dans l'angle mort du checksum. Cet angle mort vaut pour TOUS les groupes de
#      la trame : l'ADCO est le seul champ dont la forme soit connue d'avance, donc le seul
#      témoin qu'on ait.
#
# ═══ 🚨 CE QUE CE SCRIPT NE DOIT SURTOUT PAS FAIRE : REDÉMARRER ben-radio ═════════════════════
#
#   `capabilities.py` mappe `lora-tic-receiver` sur DEUX services : ben-radio ET ben-telemetry.
#   Utiliser le helper générique `capabilities.py restart lora-tic-receiver` redémarrerait donc
#   la FAÇADE RADIO — qui n'est PAS touchée par cette version (elle n'importe même pas `db.py`).
#
#   ⚠️ Redémarrer ben-radio n'est pas neutre : c'est le seul maître du RFM95 et du SPI, et le
#      verrou taint de 0.9.12 est né d'un redémarrage de trop. On ne paie pas ce risque pour un
#      changement qui ne la concerne pas.
#
#   ⇒ On se sert de `capabilities has` pour DÉCIDER, et on nomme les unités À LA MAIN.
#
# AUCUNE migration, AUCUNE table, AUCUNE colonne. Le schéma est inchangé.

set -euo pipefail
TR="→ pi-0.9.19"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
API="http://127.0.0.1:8087/health"

# ═══ PRÉFLIGHT ════════════════════════════════════════════════════════════════════════════════
for f in "$REPO/src/pi/store/db.py" "$REPO/src/pi/tic-reader/main_uart.py" \
         "$REPO/src/pi/ben-telemetry/ben_telemetry.py"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.9.19 incomplet ?)"
done

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$REPO/src/pi/store/db.py" "$REPO/src/pi/tic-reader/main_uart.py" \
           "$REPO/src/pi/ben-telemetry/ben_telemetry.py" <<'PYEOF' || fail "un fichier livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# 🚨 LE CONTRÔLE QUI COMPTE : un garde FAUX brûle une version aussi sûrement qu'un vrai défaut.
#    On ÉPROUVE le prédicat sur la cible, avec son témoin — sans le témoin, un `adco_valide` qui
#    refuserait TOUT passerait tous les cas de refus, et le boîtier cesserait de créer le moindre
#    PDL sans que rien ne le signale.
python3 - "$REPO/src/pi/store" <<'PYEOF' || fail "adco_valide ne se comporte pas comme attendu"
import sys
sys.path.insert(0, sys.argv[1])
import db
ko = []
for mauvais in ("", "   ", "\x00\x00", "\x00" * 12, "06194700", "0619470000000",
                "06194700000A", "061947 00000", "²" * 12):
    if db.adco_valide(mauvais.strip()):
        ko.append(f"adco_valide accepte {mauvais!r}")
if not db.adco_valide("061947000000"):
    ko.append("adco_valide REFUSE un ADCO valide — aucun PDL ne serait plus jamais créé")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "préflight OK (fichiers présents, compilent, adco_valide éprouvé avec son témoin)"

# ═══ QUELLES UNITÉS CE BOÎTIER DOIT-IL REDÉMARRER ? ═══════════════════════════════════════════
#
# ⭐ On interroge la MÊME source de vérité que le boot : `capabilities.py`, qui lit le
#    `device.json`. Pas `device.json.model`, qui porte un LABEL commercial (« Radio »,
#    « Filaire ») et dont se fier a déjà été supprimé en 0.9.12.
CAPS="$REPO/src/pi/capabilities.py"
[ -f "$CAPS" ] || fail "capabilities.py absent — on ne touche à AUCUN service dans le doute"

UNITES=""
python3 "$CAPS" has tic-uart          >/dev/null 2>&1 && UNITES="$UNITES ben-tic-reader.service"
python3 "$CAPS" has lora-tic-receiver >/dev/null 2>&1 && UNITES="$UNITES ben-telemetry.service"
UNITES="${UNITES# }"

# ⚠️ Un boîtier peut porter LES DEUX (pi0-lora-wired). Et un boîtier qui n'en porte aucune n'est
#    pas une anomalie : le code est livré, il n'y a simplement rien à redémarrer.
[ -n "$UNITES" ] \
    && log "unités concernées : $UNITES   (ben-radio VOLONTAIREMENT exclue)" \
    || { log "aucune capability de lecture déclarée — code livré, rien à redémarrer" ; log "✓ update OK" ; exit 0 ; }

# ═══ ÉTAT AVANT, pour que le contrôle d'effet ait un point de comparaison ═════════════════════
LISAIT=0; TS_AVANT=0
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
    [ -n "${AVANT:-}" ] && { TS_AVANT=${AVANT%% *}; LISAIT=${AVANT##* }; }
fi
[ "$LISAIT" = "1" ] \
    && log "avant : le boîtier lisait (last_tic_ts=$TS_AVANT) — une lecture sera EXIGÉE après" \
    || warn "avant : aucune lecture récente — la lecture ne sera pas exigée (câble ? émetteur ?)"

# ═══ REDÉMARRAGE ══════════════════════════════════════════════════════════════════════════════
for U in $UNITES; do
    # 🚨 `is-active --quiet` NE SUFFIT PAS : il rend non-zéro pour `activating`, donc il rate
    #    précisément un service en boucle de plantage. On lit l'ÉTAT COMPLET.
    ETAT=$(systemctl is-active "$U" 2>/dev/null || true)
    case "$ETAT" in
        active|activating|reloading|deactivating) TOURNE=oui ;;
        *)                                        TOURNE=non ;;
    esac
    log "$U : état systemd « $ETAT » → tourne : $TOURNE"

    # ⭐ ON NE DÉMARRE PAS ce qui ne tournait pas. Ce qui doit tourner est une décision de
    #    `check_network` à partir des capabilities ; l'update livre du code, elle ne décide pas
    #    de la composition du boîtier. (Leçon de 0.9.17, qui a lancé un lecteur filaire sur des
    #    boîtiers radio.)
    if [ "$TOURNE" != "oui" ]; then
        warn "$U ne tourne pas — on ne la démarre PAS ici, le code neuf est en place"
        continue
    fi
    sudo systemctl restart "$U"
    sleep 8
    systemctl is-active --quiet "$U" \
        || fail "$U ne démarre plus — voir « journalctl -u ${U%.service} -n 40 »"
    log "✓ $U debout"
done

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas, et l'avoir interrogée a brûlé
#    pi-0.9.12. /health prouve EN PLUS que la base est lisible (`db: true`), là où un simple
#    code 200 masquerait la panne.
#
# ⭐ Et c'est le contrôle qui attrape le pire cas de CETTE version : un garde trop sévère
#    laisserait le service « active » et les journaux calmes, sans plus enregistrer une mesure.
if [ "$LISAIT" = "1" ]; then
    python3 - "$API" "$TS_AVANT" <<'PYEOF' || fail "le boîtier ne lit plus après le redémarrage"
import json, sys, time, urllib.request
api, avant = sys.argv[1], int(sys.argv[2])
fin, dernier = time.time() + 120, None
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(api, timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    ts = d.get("last_tic_ts") or 0
    if ts > avant:
        print(f"  mesure enregistrée après le redémarrage (last_tic_ts {avant} → {ts})"); sys.exit(0)
    dernier = f"last_tic_ts toujours à {ts}, inchangé depuis {avant}"
    time.sleep(5)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ le boîtier enregistre toujours, et la base est lisible"
else
    warn "lecture non exigée — contrôle d'effet limité au démarrage des unités"
fi

if systemctl is-active --quiet ben-publisher.service; then
    log "✓ publisher toujours actif"
else
    warn "publisher inactif — sans lien avec cette update"
fi

log "✓ update OK"
