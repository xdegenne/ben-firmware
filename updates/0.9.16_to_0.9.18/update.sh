#!/usr/bin/env bash
# update.sh — 0.9.16 → pi-0.9.18 : la parité TIC, ET la réparation des dégâts de 0.9.17.
#
# ═══ 🚨 POURQUOI CETTE TRANSITION EXISTE ET PAS SEULEMENT UN BUMP ══════════════════════════════
#
#   pi-0.9.17 est BRÛLÉE. Son script portait deux défauts enchaînés :
#
#   ① La garde censée épargner les boîtiers sans lecteur filaire ne jouait JAMAIS :
#
#          if ! systemctl cat ben-tic-reader.service >/dev/null 2>&1; then … exit 0
#
#      `systemctl cat` réussit dès que le FICHIER D'UNITÉ existe, et l'image dorée le pose sur
#      TOUS les boîtiers. Tester l'existence d'un fichier ne dit RIEN sur ce qui doit tourner.
#
#   ② Donc le script atteignait `systemctl restart ben-tic-reader`. Or cette unité est `static` :
#      pas de [Install], pas activée, c'est `check_network` qui la démarre d'après les
#      CAPABILITIES. ⚠️ Et `restart` sur un service ARRÊTÉ le DÉMARRE.
#
#      Résultat mesuré sur un boîtier radio : 0 plantage du lecteur dans les trois jours
#      précédant le tag, 2 538 dans les neuf heures suivantes — il perd la course au GPIO de la
#      LED contre `ben-radio`, qui en est le propriétaire (`lgpio.error: 'GPIO not allocated'`),
#      et systemd le relance sans fin. Le contrôle d'effet échouait, `device.json` n'était pas
#      bumpé, l'update REJOUAIT toutes les 10 min : 53 cycles. La mécanique de pi-0.9.12.
#
# ⭐ LA RÈGLE QUI EN SORT, ET QUI VAUT POUR TOUTE UPDATE FUTURE :
#
#      UNE UPDATE REDÉMARRE CE QUI TOURNE. ELLE NE DÉMARRE JAMAIS CE QUI NE TOURNE PAS.
#
#    Ce qui doit tourner est une décision de `check_network` à partir des capabilities ; une
#    update n'a pas à la reprendre, et surtout pas à la contredire.
#
# ═══ CE QUE FAIT CE SCRIPT ════════════════════════════════════════════════════════════════════
#
#   1. Préflight universel : les deux fichiers du correctif de parité sont là, compilent, sont
#      branchés, et `octet_valide` se comporte comme attendu. Vrai sur tout modèle.
#   2. RÉPARATION : si ce boîtier ne déclare PAS `tic-uart` et que le lecteur tourne quand même,
#      c'est 0.9.17 qui l'a démarré à tort → on l'arrête.
#   3. Si le boîtier déclare `tic-uart` : on ne redémarre le lecteur QUE s'il tournait déjà, et on
#      exige qu'une trame arrive ensuite — seulement s'il en lisait avant.
#
#   Les trois modèles, et le troisième est celui que 0.9.17 aurait cassé aussi :
#      pi0-wired        tic-uart                                → redémarrage encadré
#      pi0-lora         lora + lora-tic-receiver                → ARRÊT si 0.9.17 l'a lancé
#      pi0-lora-wired   lora + lora-tic-receiver + tic-uart     → les deux lecteurs coexistent,
#                                                                 et `ben-radio` possède le GPIO
#
# AUCUNE migration, aucune table, aucune colonne.
# Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.18"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
TICDIR="$REPO/src/pi/tic-reader"
SRC_PARITE="$TICDIR/tic_parite.py"
SRC_READER="$TICDIR/main_uart.py"
API="http://127.0.0.1:8087/health"
UNITE="ben-tic-reader.service"

# ── Préflight, universel ─────────────────────────────────────────────────────────────────────
for f in "$SRC_PARITE" "$SRC_READER"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.9.18 incomplet ?)"
done

# ⚠️ SURTOUT PAS `python3 -m py_compile` : il ÉCRIT dans __pycache__, qui appartient à root sur
# les boîtiers du parc alors que l'OTA tourne en `ben`. `ast.parse` ne touche pas au disque.
python3 - "$SRC_PARITE" "$SRC_READER" <<'PYEOF' || fail "un fichier Python livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a)
    ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

grep -q "from tic_parite import octet_valide" "$SRC_READER" \
    || fail "main_uart.py n'importe pas tic_parite — le checkout n'est pas celui attendu"

# 🚨 Contrôle DÉTERMINISTE du comportement, sans câble et sans compteur. Trois affirmations,
#    dont un TÉMOIN : sans lui, un `octet_valide` qui refuserait TOUT passerait les deux autres.
python3 - "$TICDIR" <<'PYEOF' || fail "octet_valide ne se comporte pas comme attendu"
import sys
sys.path.insert(0, sys.argv[1])
from tic_parite import octet_valide

def sur_le_fil(c):
    d = ord(c) & 0x7F
    return d | ((bin(d).count("1") & 1) << 7)

LIGNE, ko = "ADCO 061947000000", []
for c in LIGNE:
    if not octet_valide(sur_le_fil(c)):            ko.append(f"{c!r} sain est refusé")
    if octet_valide(sur_le_fil(c) ^ 0x40):         ko.append(f"{c!r} bit 6 basculé est accepté")
if not any(octet_valide(sur_le_fil(c)) for c in LIGNE):
    ko.append("octet_valide refuse TOUT — le lecteur n'enregistrerait plus rien")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "préflight OK (fichiers présents, compilent, import branché, octet_valide éprouvé)"

# ── LA question : ce boîtier doit-il faire tourner un lecteur FILAIRE ? ───────────────────────
#
# ⭐ On interroge la MÊME source que `check_network` : `capabilities.py`, qui lit le `device.json`
#    du boîtier. Pas le modèle — `device.json.model` porte depuis 0.8.0 un LABEL commercial
#    (« Radio », « Filaire »), pas un modèle technique, et s'y fier a déjà été supprimé en 0.9.12.
DOIT_TOURNER=$(python3 - "$REPO/src/pi" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
try:
    import capabilities as caps
    print("oui" if caps.has("tic-uart") else "non")
except Exception as e:
    print(f"indecidable:{e}")
PYEOF
)
case "$DOIT_TOURNER" in
    oui|non) log "capability tic-uart : $DOIT_TOURNER" ;;
    *) fail "capabilities illisibles ($DOIT_TOURNER) — on ne touche à AUCUN service dans le doute" ;;
esac

# 🚨 `is-active --quiet` NE SUFFIT PAS, et la répétition l'a prouvé : il rend non-zéro pour
#    `activating`. Or un service en boucle de plantage — l'état EXACT que 0.9.17 laisse derrière
#    lui — oscille entre `activating`, `active`, `auto-restart` et `failed`. La détection ratait
#    donc précisément le cas qu'elle doit traiter, et l'update annonçait « rien à réparer » sur un
#    boîtier qui plantait toutes les cinq secondes.
#
# ⭐ On lit l'ÉTAT COMPLET, et tout ce qui n'est pas franchement arrêté compte comme « tourne ».
ETAT=$(systemctl is-active "$UNITE" 2>/dev/null || true)
case "$ETAT" in
    active|activating|reloading|deactivating) TOURNE=oui ;;
    *)                                        TOURNE=non ;;
esac
log "lecteur filaire : état systemd « $ETAT » → tourne : $TOURNE"

# ── ② RÉPARATION des boîtiers abîmés par 0.9.17 ──────────────────────────────────────────────
if [ "$DOIT_TOURNER" = "non" ]; then
    # ⭐ ON AFFIRME L'ÉTAT VOULU, ON NE LE DÉTECTE PAS. Sur un boîtier sans `tic-uart`, l'état
    #    correct est « arrêté », point. Un `stop` sur un service déjà arrêté est un no-op qui
    #    rend 0, donc ce geste est idempotent — et immunisé contre les états transitoires qui
    #    avaient fait échouer la première version de cette réparation.
    [ "$TOURNE" = "oui" ] \
        && log "ce boîtier ne déclare pas tic-uart et le lecteur tourne : c'est 0.9.17 qui l'a lancé" \
        || log "le lecteur filaire ne tourne pas — on s'en assure quand même"
    sudo systemctl stop "$UNITE"

    # ⚖️ Il doit être arrêté ET RESTER arrêté. On laisse passer plus d'un intervalle de
    #    `RestartSec` : un service en boucle a une relance en vol au moment du `stop`, et
    #    vérifier trop tôt le déclarerait arrêté alors qu'il revient.
    sleep 15
    ETAT=$(systemctl is-active "$UNITE" 2>/dev/null || true)
    case "$ETAT" in
        inactive|failed) log "✓ lecteur filaire arrêté et stable (état « $ETAT »)" ;;
        *) fail "le lecteur est reparti après l'arrêt (état « $ETAT ») — qui le relance ?" ;;
    esac
    # Les agents radio, eux, doivent être intacts — c'est eux qui mesurent ici.
    for u in ben-radio.service ben-telemetry.service; do
        if systemctl cat "$u" >/dev/null 2>&1; then
            systemctl is-active --quiet "$u" \
                && log "✓ $u actif" \
                || warn "$u inactif — à vérifier, mais ce n'est pas cette update qui l'arrête"
        fi
    done

# ── ③ Boîtier qui DOIT faire tourner le lecteur filaire ──────────────────────────────────────
else
    if [ "$TOURNE" = "non" ]; then
        # 🚨 ON NE LE DÉMARRE PAS. C'est exactement l'erreur de 0.9.17. S'il est arrêté alors
        #    qu'il devrait tourner, la cause est ailleurs (réseau, provisioning) et c'est
        #    `check_network` qui s'en charge au prochain boot. Le code est livré, il servira.
        warn "le lecteur devrait tourner mais ne tourne pas — on ne le démarre PAS ici"
        warn "  c'est check_network qui décide de le lancer ; le code neuf est en place"
        log "✓ update OK (code livré, aucun service touché)"
        exit 0
    fi

    # État AVANT, pour que le contrôle d'effet ait un point de comparaison.
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
        && log "avant : le boîtier lisait — une lecture sera EXIGÉE après" \
        || warn "avant : aucune lecture récente — la lecture ne sera pas exigée (câble ?)"

    sudo systemctl restart "$UNITE"
    log "lecteur filaire redémarré"
    sleep 8
    systemctl is-active --quiet "$UNITE" \
        || fail "le lecteur ne démarre plus — voir « journalctl -u ben-tic-reader -n 40 »"
    log "✓ lecteur debout"

    if [ "$LISAIT" = "1" ]; then
        # 🚨 Le contrôle qui attrape le pire cas : un `octet_valide` trop sévère laisserait le
        #    service « active » et les journaux calmes, sans plus enregistrer une seule mesure.
        python3 - "$API" "$TS_AVANT" <<'PYEOF' || fail "le lecteur ne lit plus après le redémarrage"
import json, sys, time, urllib.request
api, avant = sys.argv[1], int(sys.argv[2])
fin, dernier = time.time() + 90, None
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(api, timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    ts = d.get("last_tic_ts") or 0
    if ts > avant:
        print(f"  trame lue après le redémarrage (last_tic_ts {avant} → {ts})"); sys.exit(0)
    dernier = f"last_tic_ts toujours à {ts}, inchangé depuis {avant}"
    time.sleep(5)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
        log "✓ le lecteur lit toujours, et la base est lisible"
    fi
fi

if systemctl is-active --quiet ben-publisher.service; then
    log "✓ publisher toujours actif"
else
    warn "publisher inactif — sans lien avec cette update"
fi

log "✓ update OK"
