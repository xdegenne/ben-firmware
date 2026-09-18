#!/usr/bin/env bash
# update.sh — → pi-0.9.14 : la rétention des mesures passe de 90 à 180 jours.
#
# ═══ POURQUOI ════════════════════════════════════════════════════════════════════════════════
#
#   Le parc atteignait 3 mois alors que RETENTION_DAYS valait 90. Autrement dit : le plus
#   ancien jour d'historique s'effaçait chaque jour, et il n'existait NULLE PART ailleurs —
#   il n'y a aucune sauvegarde des bases embarquées.
#
#   L'ingestion cloud est en construction (docs/chantier-ingestion-cloud.md). La construire
#   avant que le passé n'ait disparu était une course perdue d'avance : chaque semaine de
#   chantier coûtait une semaine d'historique. 180 jours SUPPRIMENT l'échéance au lieu de
#   courir après — pour le prix d'une constante.
#
#   Mesuré sur ben-0001 le 2026-09-18 : 5 441 400 lignes, exactement 90 jours (20/06 → 18/09,
#   la purge mordait bel et bien), 420 Mo de base pour 9,5 Go libres sur la carte. Doubler la
#   rétention coûte ~420 Mo. Ce n'est pas l'espace qui limite, c'est la pression mémoire sur
#   les 512 Mo du Pi Zero et la taille du WAL — d'où 180 et non 365.
#
#   🎁 Le mouvement s'inversera : une fois le cloud alimenté, cette valeur pourra DESCENDRE
#      (7 à 30 jours) et le boîtier deviendra plus rapide. Les 90 jours ne servaient qu'à être
#      la seule copie existante.
#
# ═══ POURQUOI UN REDÉMARRAGE EST OBLIGATOIRE ═════════════════════════════════════════════════
#
#       def prune(conn, retention_days: int = RETENTION_DAYS)
#
#   En Python, un argument par défaut est évalué UNE FOIS, à l'exécution du `def` — donc à
#   l'IMPORT du module. Un lecteur déjà lancé garde la valeur qu'il avait au démarrage.
#   Changer le fichier sans redémarrer ne ferait STRICTEMENT RIEN, et l'update passerait pour
#   réussie. C'est le genre de non-effet qu'on ne découvre que trois mois plus tard.
#
# AUCUNE migration, aucune table, aucune colonne. UNIVERSEL (LoRa et filaire, pas de gate).
# Code déjà sur disque après `git checkout pi-0.9.14`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.14"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
DB="/var/lib/ben-firmware/measurements.db"

# ── Préflight : le code attendu doit être présent ─────────────────────────────────────────────
grep -qE '^RETENTION_DAYS = 180\b' "$REPO/src/pi/store/db.py" \
    || fail "RETENTION_DAYS n'est pas à 180 dans le dépôt (checkout pi-0.9.14 incomplet ?)"

# ⚠️ SURTOUT PAS `python3 -m py_compile` : il ÉCRIT dans __pycache__, qui appartient à root sur
# les boîtiers du parc alors que l'OTA tourne en `ben` → Permission denied, update avortée, tag
# brûlé pour un défaut de droits. `ast.parse` ne touche pas au disque.
python3 - "$REPO" <<'PYEOF' || fail "db.py ne compile pas"
import ast, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "src/pi/store/db.py"
ast.parse(p.read_text(encoding="utf-8"), filename="store/db.py")
PYEOF

# La valeur doit être celle que verra un IMPORT, pas seulement celle qu'on lit au grep :
# un `RETENTION_DAYS` redéfini plus bas dans le fichier passerait le grep et pas ceci.
python3 - "$REPO" <<'PYEOF' || fail "à l'import, RETENTION_DAYS ne vaut pas 180"
import importlib.util, sys, pathlib
spec = importlib.util.spec_from_file_location(
    "bendb", pathlib.Path(sys.argv[1]) / "src/pi/store/db.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
assert m.RETENTION_DAYS == 180, m.RETENTION_DAYS
PYEOF
log "préflight OK (RETENTION_DAYS = 180 à l'import)"

# ── Garde-fou espace disque ───────────────────────────────────────────────────────────────────
# Doubler la rétention double à terme la base. Sur un boîtier au disque déjà tendu, ce serait
# échanger une perte d'historique contre une carte pleine — nettement pire. Le seuil est large
# (ben-0001 : 420 Mo de base pour 9,5 Go libres), il ne se déclenchera qu'en cas pathologique.
if [ -f "$DB" ]; then
    db_mo=$(( $(stat -c %s "$DB") / 1048576 ))
    libre_mo=$(df -Pm /var/lib/ben-firmware | awk 'NR==2 {print $4}')
    log "base ${db_mo} Mo · ${libre_mo} Mo libres"
    # Il faut pouvoir accueillir une base doublée, plus une marge pour le WAL.
    besoin=$(( db_mo + 512 ))
    [ "$libre_mo" -ge "$besoin" ] \
        || fail "espace insuffisant : ${libre_mo} Mo libres, ${besoin} Mo nécessaires pour doubler la rétention"
else
    warn "pas encore de base ($DB) — boîtier neuf ?"
fi

# ── Redémarrage des lecteurs ──────────────────────────────────────────────────────────────────
# Les services viennent des CAPABILITIES, comme partout ailleurs : ben-radio + ben-telemetry sur
# un boîtier LoRa, ben-tic-reader sur un filaire. Sans ce redémarrage la constante reste à 90
# dans les processus vivants — cf. l'argument par défaut plus haut.
restarted=0
for cap in $(python3 "$REPO/src/pi/capabilities.py" list 2>/dev/null); do
    for unit in $(python3 "$REPO/src/pi/capabilities.py" services "$cap" 2>/dev/null); do
        if systemctl is-active --quiet "$unit"; then
            log "restart $unit"
            sudo systemctl restart "$unit" || fail "restart $unit"
            restarted=1
        fi
    done
done
[ "$restarted" = 1 ] || warn "aucun agent de mesure actif — effectif au prochain démarrage"
sleep 5

# ── Contrôle d'effet ──────────────────────────────────────────────────────────────────────────
# ⚠️ Ce bloc a été EXÉCUTÉ TEL QUEL sur un boîtier avant que le tag ne soit signé. Un garde-fou
#    faux brûle une version aussi sûrement qu'un vrai défaut (leçon de pi-0.9.12, brûlée sur une
#    route inexistante).
#
# Ce qu'on peut prouver DANS la fenêtre de l'update : que le code chargé vaut 180, et que les
# lecteurs ont survécu au redémarrage. Ce qu'on NE PEUT PAS prouver ici : que la purge garde
# bien 180 jours — elle ne tourne qu'environ une fois par heure. D'où la ligne de surveillance
# en fin de script, qui dit exactement quoi regarder et quand.
for cap in $(python3 "$REPO/src/pi/capabilities.py" list 2>/dev/null); do
    for unit in $(python3 "$REPO/src/pi/capabilities.py" services "$cap" 2>/dev/null); do
        systemctl is-active --quiet "$unit" \
            || fail "$unit ne tourne plus après restart"
    done
done

# L'API locale ouvre la base en lecture seule ; si elle répond avec db:true, la base est
# intacte après le redémarrage des lecteurs. Un simple code 200 masquerait une base illisible.
if systemctl is-active --quiet ben-local-api.service; then
    python3 - <<'PYEOF' || fail "l'API locale ne répond plus sur /health"
import json, sys, urllib.request
try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:8087/health", timeout=15).read())
except Exception as e:
    print(f"  /health KO : {e}", file=sys.stderr); sys.exit(1)
if not d.get("db"):
    print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ API locale debout (/health répond, base lisible)"
fi

log "✓ rétention portée à 180 jours — le passé cesse de s'effacer sous le chantier cloud"
log "  à surveiller (la purge tourne ~1×/h) :"
log "    journalctl -u ben-radio -u ben-tic-reader | grep 'store purge'"
log "    → doit afficher « store purge (>180j) », plus jamais « >90j »"
log "✓ update OK"
