#!/usr/bin/env bash
# update.sh — 0.13.0 → pi-0.14.0 : DEUX correctifs sans rapport l'un avec l'autre, groupés.
#
#   ① LE TRIPHASÉ (#50) — le courant publié devient le TOTAL des trois phases, et la jauge
#      cesse de saturer au tiers de l'abonnement.
#   ② LE BLE AU DÉBALLAGE (#49) — le boîtier ne coupe plus son Bluetooth sous l'utilisateur
#      à l'instant où le WiFi s'établit.
#
# ═══ ① CE QUE LE TRIPHASÉ CHANGE ══════════════════════════════════════════════════════════════
#
#   IINST = IINST1 + IINST2 + IINST3     (historique)
#   IINST = IRMS1  + IRMS2  + IRMS3      (standard)
#   maxVa = ISOUSC × 230 × phases        (nouvelle colonne `phases`, NULL = 1)
#
# 🚨 LE PIRE DES DEUX DÉFAUTS ÉTAIT LE SILENCIEUX. En historique, `IINST` n'est JAMAIS émis par
#    un triphasé (c'est IINST1/2/3) ⇒ la trame était jetée, le compteur ne produisait RIEN ; ça
#    se voyait. En standard, `IRMS1` existe et était mappé vers IINST ⇒ le boîtier publiait LE
#    COURANT D'UNE PHASE SUR TROIS. Aucune alerte, une courbe plausible, une donnée fausse.
#
# ═══ ② CE QUE LE BLE CHANGE ═══════════════════════════════════════════════════════════════════
#
#   `_wait_for_ble_session_end()` était dans la branche `else` de network_recovery.main() — donc
#   consultée UNIQUEMENT quand la fenêtre s'écoule SANS réseau, c'est-à-dire quand personne ne
#   configure rien. Sur le chemin nominal (l'utilisateur donne le WiFi, le boîtier s'associe),
#   le provisioner était tué EN PLEINE SESSION et le service GATT disparaissait sous l'app.
#
# ⓘ CE CORRECTIF N'AGIT QU'AU PROCHAIN DÉMARRAGE : `ben-network-recovery` est un oneshot de
#   boot. L'update ne redémarre donc PAS le boîtier pour lui — rien ne presse, et un reboot
#   imposé couperait les mesures pour un correctif qui ne sert qu'au déballage suivant.
#
# ═══ 🚨 CE QUI NE DOIT PAS CHANGER : LES BOÎTIERS MONOPHASÉS ══════════════════════════════════
#
#   Les 7 boîtiers du parc sont MONOPHASÉS. Rien du volet ① ne s'active sans une 2e ou 3e phase
#   dans la trame : `phases` reste NULL, donc ×1, et `IINST` garde son chemin d'origine.
#   ⇒ Le contrôle d'effet EXIGE /health (db lisible) et la présence de la colonne, et il
#     RAPPORTE le triphasé sans rien en exiger — aucun boîtier du parc ne peut le démontrer, et
#     un garde-fou plus exigeant que ce que le terrain peut montrer est un garde-fou faux
#     (pi-0.9.12, brûlée pour ça).
#
# ═══ 🚨 L'ORDRE DE REDÉMARRAGE N'EST PAS INDIFFÉRENT ══════════════════════════════════════════
#
#   La colonne `phases` est créée par un ALTER TABLE qui ne s'exécute QU'À L'OUVERTURE EN
#   ÉCRITURE (`db.connect()` rejoue la migration). `ben-local-api` ouvre en LECTURE SEULE et ne
#   peut RIEN créer. ⇒ LE LECTEUR D'ABORD, L'API ENSUITE. Inversé, l'API tournerait sans la
#   colonne — sans planter (get_phases a sa garde défensive) mais en rendant une jauge ×1 sur un
#   triphasé, c'est-à-dire le défaut qu'on vient de corriger.
#
# ═══ RETOUR ARRIÈRE ═══════════════════════════════════════════════════════════════════════════
#
#   Rien à restaurer : la migration n'AJOUTE qu'une colonne, donc l'ancien code tourne sur la
#   nouvelle base. Aucun fichier posé hors du dépôt, aucune unité modifiée, aucun sudoers.

set -euo pipefail
TR="→ pi-0.14.0"
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
for f in "$SRC/tic-reader/main_uart.py" "$SRC/store/db.py" "$SRC/store/local_api.py" \
         "$SRC/provisioner/network_recovery.py" \
         "$SRC/tic-reader/test_triphase.py" "$SRC/provisioner/test_network_recovery.py"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.14.0 incomplet ?)"
done
# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$SRC" <<'PYEOF' || fail 'un fichier Python livré ne compile pas'
import ast, pathlib, sys
base = pathlib.Path(sys.argv[1])
for rel in ("tic-reader/main_uart.py", "store/db.py", "store/local_api.py",
            "provisioner/network_recovery.py"):
    p = base / rel
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
print("  4 fichiers Python compilent")
PYEOF
log "préflight ① OK (6 fichiers présents, syntaxe vérifiée)"

# ═══ PRÉFLIGHT ② — LES DEUX BANCS LIVRÉS, SUR LE PYTHON DU BOÎTIER ════════════════════════════
# ⚠️ `TMPDIR=/var/tmp` et pas /tmp : /tmp peut être un tmpfs étroit sur un Pi Zero.
#    Le banc triphasé ouvre une base SQLite jetable dans son propre répertoire temporaire — il
#    ne touche donc JAMAIS /var/lib/ben-firmware/measurements.db.
cd "$SRC/tic-reader"
TMPDIR=/var/tmp python3 test_triphase.py >/dev/null 2>&1 \
    || fail "le banc test_triphase.py échoue sur ce boîtier — NE PAS déployer"
cd "$SRC/provisioner"
TMPDIR=/var/tmp python3 test_network_recovery.py >/dev/null 2>&1 \
    || fail "le banc test_network_recovery.py échoue sur ce boîtier — NE PAS déployer"
cd "$REPO"
log "préflight ② OK (les 2 bancs livrés sont verts, sur le Python du boîtier)"

# ═══ PRÉFLIGHT ③ — 🚨 LES GARDES SONT DANS LE CODE, PAS DANS LES COMMENTAIRES ═════════════════
#
# 🚨 Volet ① : sans ces gardes, une trame qui perd une phase publie une SOMME PARTIELLE — un
#    nombre plausible et FAUX, écrit en base, là où le monophasé perdant son IINST voyait sa
#    trame REJETÉE. Une donnée fausse à la place d'une donnée perdue.
# 🚨 Volet ② : si l'attente retourne dans le `else`, le défaut revient à l'identique — et il est
#    INTERMITTENT (course de 0 à 30 s), donc il ne se verrait pas en essayant une fois.
# ⚠️ Sur le CODE commentaires et chaînes RETIRÉS : la prose de ces fichiers cite les clauses
#    qu'on affirme, et un grep naïf passerait au vert sur un code qui ne les applique plus.
python3 - "$SRC/tic-reader/main_uart.py" "$SRC/store/local_api.py" \
         "$SRC/provisioner/network_recovery.py" <<'PYEOF' || fail "une garde manque dans le code livré"
import ast, pathlib, sys

def code_nu(chemin):
    """Le CODE seul : ni commentaires, ni docstrings.

    ⚠️ On passe par l'ARBRE, pas par `tokenize` : retirer les tokens STRING
       effacerait aussi les littéraux qu'on cherche (`out.pop("IINST", …)`), et
       les garder laisserait la prose des docstrings citer la clause qu'on
       affirme — vert sur un code qui ne l'applique plus.
    """
    arbre = ast.parse(pathlib.Path(chemin).read_text(encoding="utf-8"))
    for n in ast.walk(arbre):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (n.body and isinstance(n.body[0], ast.Expr)
                    and isinstance(n.body[0].value, ast.Constant)
                    and isinstance(n.body[0].value.value, str)):
                n.body.pop(0)
    return ast.unparse(arbre)

mu, api, nr = (code_nu(a) for a in sys.argv[1:4])
ko = []
if mu.count("last_phases == 3 and len(") != 2:
    ko.append("les DEUX gardes de trame incomplète (histo + standard) ne sont pas là")
if "out.pop('IINST', None)" not in mu:
    ko.append("le décodeur ne retire pas IINST quand une phase manque")
if "db.get_phases(measurements_db, pdl)" not in mu:
    ko.append("le raccordement n'est pas relu depuis la base après résolution du PDL")
if "isousc * 230 * phases" not in api:
    ko.append("/live ne multiplie pas maxVa par le nombre de phases")
# 🚨 CONTRÔLE STRUCTUREL, PAS POSITIONNEL. Comparer des indices ne distingue
#    PAS « dans le else » de « après le else » : l'appel reste entre les deux
#    dans les deux cas. Vérifié — la mutation passait au vert. On exige donc que
#    l'appel soit une instruction de PREMIER NIVEAU de main(), et qu'il n'en
#    existe aucun ailleurs dans la fonction.
def appels_directs(fn, nom):
    return [s for s in fn.body
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
            and getattr(s.value.func, "id", None) == nom]

def appels_total(fn, nom):
    return [n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == nom]

arbre_nr = ast.parse(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
mains = [n for n in ast.walk(arbre_nr)
         if isinstance(n, ast.FunctionDef) and n.name == "main"]
if len(mains) != 1:
    ko.append("network_recovery.main() introuvable ou en double")
else:
    NOM = "_wait_for_ble_session_end"
    directs, total = appels_directs(mains[0], NOM), appels_total(mains[0], NOM)
    if len(directs) != 1:
        ko.append(f"{NOM} n'est pas appelée au PREMIER NIVEAU de main() "
                  f"({len(directs)} appel(s) direct(s)) — elle est restée dans le `else`")
    if len(total) != len(directs):
        ko.append(f"{NOM} est aussi appelée dans une branche ({len(total) - len(directs)} "
                  f"appel(s) imbriqué(s)) — la garde serait conditionnelle")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
print("  6 gardes présentes dans le code")
PYEOF
log "préflight ③ OK (gardes vérifiées sur l'arbre, commentaires retirés)"

# ═══ LA POSE ══════════════════════════════════════════════════════════════════════════════════
#
# ⓘ RIEN à installer hors du dépôt : pas de sudoers, pas d'unité systemd, pas de configuration.
#   Le `git checkout pi-0.14.0` fait la totalité de la pose. Il ne reste qu'à faire relire le
#   code — et à provoquer la migration de schéma.

# ① LE LECTEUR D'ABORD — c'est lui qui ouvre la base EN ÉCRITURE, donc lui seul peut créer la
#    colonne. La liste couvre les lecteurs possibles ; les absents sont ignorés sans bruit.
REDEMARRES=""
for U in ben-tic-reader ben-radio ben-telemetry; do
    if systemctl cat "$U.service" >/dev/null 2>&1; then
        sudo systemctl restart "$U" 2>/dev/null && REDEMARRES="$REDEMARRES $U"
    fi
done
[ -n "$REDEMARRES" ] || warn "aucun lecteur redémarré — la migration attendra le prochain boot"
log "lecteurs redémarrés :${REDEMARRES:- aucun}"

# ② LA MIGRATION DOIT AVOIR EU LIEU AVANT QU'ON TOUCHE À L'API — on l'ATTEND, on ne la suppose
#    pas. Le lecteur ouvre la base dans la seconde qui suit son démarrage.
python3 - "$BASE" <<'PYEOF' || fail "la colonne phases n'est pas apparue — aucun lecteur n'a ouvert la base en écriture"
import sqlite3, sys, time
chemin = sys.argv[1]
for _ in range(20):           # 20 × 1 s
    try:
        c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True, timeout=5.0)
        cols = {r[1] for r in c.execute("PRAGMA table_info(level_profile)")}
        c.close()
        if "phases" in cols:
            print("  colonne `phases` présente dans level_profile")
            sys.exit(0)
    except Exception:
        pass
    time.sleep(1)
print("  colonne `phases` toujours absente après 20 s", file=sys.stderr)
sys.exit(1)
PYEOF
log "✓ migration appliquée (level_profile.phases)"

# ③ L'API ENSUITE — elle ouvre en LECTURE SEULE, elle n'aurait rien pu créer.
sudo systemctl restart ben-local-api
log "ben-local-api redémarrée"

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas, et l'avoir interrogée a BRÛLÉ
#    pi-0.9.12. /health prouve en plus que la base est lisible (db: true), là où un simple 200
#    masquerait une base morte.
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

# ⓘ Le TRIPHASÉ lui-même n'est PAS exigé et ne peut pas l'être : aucun boîtier du parc n'en a un.
#   On RAPPORTE ce que la base sait, sans rien en faire dépendre.
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

# ⓘ Le volet BLE ne se contrôle PAS ici : `ben-network-recovery` est un oneshot de BOOT, il ne
#   tourne pas pendant une update. Le préflight ③ a vérifié que la garde est au bon endroit dans
#   le code livré — c'est tout ce qu'on peut affirmer sans redémarrer, et redémarrer pour ça
#   couperait les mesures au profit d'un correctif qui ne sert qu'au déballage suivant.
log "ⓘ volet BLE : actif au PROCHAIN démarrage (oneshot de boot), garde vérifiée au préflight ③"

log "✓ update OK"
