#!/usr/bin/env bash
# update.sh — 0.14.0 → pi-0.15.0 : LE RÉCEPTEUR CONSOMME LE BIT TRIPHASÉ.
#
# ═══ CE QUE ÇA CHANGE ═════════════════════════════════════════════════════════════════════════
#
#   Une trame courbe LoRa portant CF_TRIPHASE (bit 3) fait écrire `level_profile.phases = 3`
#   pour le PDL de cet émetteur ⇒ la jauge passe de `ISOUSC × 230` à `ISOUSC × 230 × 3`.
#
# ═══ 🚨 POURQUOI, ET POURQUOI C'ÉTAIT LA MOITIÉ MANQUANTE ═════════════════════════════════════
#
#   L'émetteur publie depuis 0.1.12 (#51) le TOTAL des trois phases et lève le bit. Le récepteur
#   le décodait et le JETAIT. Or ISOUSC est exprimé PAR PHASE alors que `maxVa` borne un PAPP qui
#   est un TOTAL : un foyer triphasé 3×20 A tirant 5 000 VA — parfaitement normal — affichait
#   109 %, rouge en permanence. Le courant était juste et la jauge fausse.
#
# ⭐ RIEN DE NOUVEAU EN BASE : la colonne `level_profile.phases` existe depuis pi-0.14.0 (#50) et
#   `db.record_phases` aussi. Elle n'était nourrie que par le lecteur FILAIRE. Cette update
#   n'ajoute donc AUCUNE migration — elle branche un consommateur sur une colonne déjà là.
#
# ═══ 🚨 CE QUI NE DOIT PAS CHANGER : LES BOÎTIERS MONOPHASÉS ══════════════════════════════════
#
#   Les 8 boîtiers du parc sont MONOPHASÉS, et AUCUN émetteur du parc n'émet encore le bit
#   (reflash PHYSIQUE, carte par carte : deux faites le 17/10, deux hors de portée). Rien ne
#   s'active donc tant qu'une trame ne porte pas le bit — `get_phases` rend 1 par défaut.
#
# ⇒ LE CONTRÔLE D'EFFET N'EXIGE PAS LE TRIPHASÉ et ne le peut pas : aucun boîtier ne peut le
#   démontrer aujourd'hui. Il exige /health (db=true), vérifie que le code livré consomme bien
#   le bit, et RAPPORTE les profils sans rien en faire dépendre. Un garde-fou plus exigeant que
#   ce que le terrain peut montrer est un garde-fou faux (pi-0.9.12, brûlée pour ça).
#
# ═══ RETOUR ARRIÈRE ═══════════════════════════════════════════════════════════════════════════
#
#   Rien à restaurer : aucune migration, aucun fichier hors du dépôt, aucune unité modifiée.
#   Une colonne `phases` déjà écrite resterait, et l'ancien code l'ignorerait simplement.

set -euo pipefail
TR="→ pi-0.15.0"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
SRC="$REPO/src/pi"
API="http://127.0.0.1:8087/health"
BASE="${BEN_DB_PATH:-/var/lib/ben-firmware/measurements.db}"

# ═══ PRÉFLIGHT ⓞ — le droit d'écrire hors du dépôt ════════════════════════════════════════════
sudo -n true 2>/dev/null \
    || fail "l'agent (ben) n'a pas de sudo sans mot de passe — /etc/sudoers.d/ben-firmware manque"
log "préflight ⓞ OK (sudo sans mot de passe pour $(id -un))"

# ═══ PRÉFLIGHT ① — les fichiers livrés sont là et compilent ═══════════════════════════════════
for f in "$SRC/ben-telemetry/ben_telemetry.py" "$SRC/lora-receiver/frame_codec.py" \
         "$SRC/store/db.py" "$SRC/ben-telemetry/test_phases_triphase.py"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.15.0 incomplet ?)"
done
# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$SRC" <<'PYEOF' || fail 'un fichier Python livré ne compile pas'
import ast, pathlib, sys
base = pathlib.Path(sys.argv[1])
for rel in ("ben-telemetry/ben_telemetry.py", "lora-receiver/frame_codec.py", "store/db.py"):
    p = base / rel
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
print("  3 fichiers Python compilent")
PYEOF
log "préflight ① OK (4 fichiers présents, syntaxe vérifiée)"

# ═══ PRÉFLIGHT ② — LE BANC LIVRÉ, SUR LE PYTHON DU BOÎTIER ════════════════════════════════════
# ⚠️ `TMPDIR=/var/tmp` et pas /tmp : /tmp peut être un tmpfs étroit sur un Pi Zero. Le banc ouvre
#    une base SQLite jetable dans son propre répertoire temporaire et détourne `STATE_PATH` — il
#    ne touche donc NI measurements.db, NI l'état du boîtier.
cd "$SRC/ben-telemetry"
TMPDIR=/var/tmp python3 test_phases_triphase.py >/dev/null 2>&1 \
    || fail "le banc test_phases_triphase.py échoue sur ce boîtier — NE PAS déployer"
cd "$REPO"
log "préflight ② OK (banc du triphasé récepteur vert, 7 cas, sur le Python du boîtier)"

# ═══ PRÉFLIGHT ③ — 🚨 LES DEUX BARRIÈRES SONT DANS LE CODE ════════════════════════════════════
#
# 🚨 L'invariant « un triphasé ne retombe JAMAIS à 1 » tient sur DEUX gardes indépendantes, et
#    c'est mesuré : lever l'une seule ne casse rien, il faut lever les deux. On vérifie donc les
#    deux, sur l'ARBRE SYNTAXIQUE, docstrings retirées — la prose de ces fichiers cite les
#    clauses qu'on affirme, et un grep passerait au vert sur un code qui ne les applique plus.
python3 - "$SRC/ben-telemetry/ben_telemetry.py" "$SRC/store/db.py" <<'PYEOF' || fail "une garde du triphasé manque"
import ast, pathlib, sys

def code_nu(chemin):
    arbre = ast.parse(pathlib.Path(chemin).read_text(encoding="utf-8"))
    for n in ast.walk(arbre):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (n.body and isinstance(n.body[0], ast.Expr)
                    and isinstance(n.body[0].value, ast.Constant)
                    and isinstance(n.body[0].value.value, str)):
                n.body.pop(0)
    return ast.unparse(arbre)

bt, store = (code_nu(a) for a in sys.argv[1:3])
ko = []
if "db.record_phases(measurements_db, pdl_index, 3)" not in bt:
    ko.append("le récepteur n'écrit pas phases=3 sur le bit TRIPHASÉ")
if "decoded.get('triphase')" not in bt:
    ko.append("le bit `triphase` n'est pas lu de la trame décodée")
# 🚨 BARRIÈRE 2 — `record_phases` doit refuser toute valeur ≠ 3. C'est elle qui tient
#    quand le récepteur se trompe, et elle a été vérifiée par mutation composée.
if "if phases != 3" not in store:
    ko.append("record_phases n'interdit plus les valeurs ≠ 3 — un triphasé pourrait retomber à 1")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
print("  les 2 barrières sont dans le code")
PYEOF
log "préflight ③ OK (gardes vérifiées sur l'arbre, docstrings retirées)"

# ═══ LA POSE ══════════════════════════════════════════════════════════════════════════════════
#
# ⓘ RIEN à installer hors du dépôt, et AUCUNE migration : la colonne existe depuis pi-0.14.0.
#   Il ne reste qu'à faire relire le code par les agents concernés.
#
# ⚠️ `ben-telemetry` est le sink LoRa : il n'existe QUE sur les boîtiers Radio. Sur un Filaire
#    l'unité est absente, et c'est normal — on ne fait pas échouer l'update pour ça.
REDEMARRES=""
for U in ben-telemetry ben-radio; do
    if systemctl cat "$U.service" >/dev/null 2>&1; then
        sudo systemctl restart "$U" 2>/dev/null && REDEMARRES="$REDEMARRES $U"
    fi
done
if [ -n "$REDEMARRES" ]; then
    log "agents radio redémarrés :$REDEMARRES"
else
    log "ⓘ aucun agent radio ici — boîtier FILAIRE, le volet récepteur ne le concerne pas"
fi

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas, et l'avoir interrogée a BRÛLÉ
#    pi-0.9.12. /health prouve en plus que la base est lisible (db: true).
python3 - "$API" <<'PYEOF' || fail ":8087 ne sert pas — l'app du parc et Home Assistant en dépendent"
import json, sys, time, urllib.request
url, dernier = sys.argv[1], "aucune tentative"
for _ in range(12):           # 12 × 5 s = 60 s
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.load(r)
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    print(f"  :8087 /health OK — db lisible, version {d.get('softwareVersion')}")
    sys.exit(0)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
log "✓ :8087 sert, et la base est lisible"

# ⓘ Le triphasé lui-même n'est PAS exigé : aucun émetteur du parc n'émet encore le bit. On
#   RAPPORTE ce que la base sait, sans rien en faire dépendre.
python3 - "$BASE" <<'PYEOF' || true
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=5.0)
    lignes = list(c.execute("SELECT pdl_index, isousc, phases FROM level_profile"))
    c.close()
    for pdl, isousc, phases in lignes:
        n = phases or 1
        print(f"  pdl {pdl} : isousc={isousc} phases={n} → maxVa={(isousc or 0) * 230 * n} VA")
    if not lignes:
        print("  aucun profil encore enregistré")
except Exception as e:
    print(f"  (profils illisibles : {e})")
PYEOF

log "✓ update OK"
