#!/usr/bin/env bash
# update.sh — 0.11.1 → pi-0.11.2 : LE FILET DU RÉSEAU EST ENFIN POSÉ.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   Chantier `ben-docs#17`, sous-tâche #46. ben-0005 a passé **8 h 40 sans réseau** le 2026-10-06 :
#   il mesurait, il ne publiait plus, et l'OTA échouait pour la même cause — donc le seul canal de
#   réparation à distance était fermé en même temps que la publication. Il a fallu aller le
#   redémarrer à la main.
#
#   ⚠️ LE REMÈDE EXISTAIT DÉJÀ ET N'ÉTAIT INSTALLÉ NULLE PART. `wifi_watchdog.sh` est dans le dépôt
#      depuis des mois avec son unité et son timer, mais `install.sh` copiait les unités EN BLOC
#      (`cp config/systemd/*`) alors que la liste des `enable` est explicite, un par un — et le
#      watchdog n'y était pas. D'où une unité `loaded` sur les 8 boîtiers et un timer `disabled`
#      sur les 8. Les deux faits avaient l'air de se contredire ; ils venaient d'UNE LIGNE
#      MANQUANTE.
#
#   ⭐ Et il aurait suffi : la Freebox ne voyait AUCUN équipement, donc le boîtier n'était pas
#      associé, donc `ping 1.1.1.1` aurait échoué et le watchdog aurait relancé NetworkManager
#      toutes les 2 minutes jusqu'à la reprise.
#
# ═══ 🚨 CE QU'IL NE FAIT PAS, ET QUAND IL NE FAIT RIEN ════════════════════════════════════════
#
#   Il y a un moment où l'absence de réseau est VOULUE : la fenêtre BLE du déballage. Toucher à
#   NetworkManager pendant que le téléphone écrit les identifiants toucherait à une radio PARTAGÉE
#   entre WiFi et BLE sur Pi Zero W — ce qui a déjà coûté un décrochage en déballage Android.
#
#   ⇒ Le script porte DEUX GARDES, et elles sont DANS LE SCRIPT, pas dans un `enable` placé au bon
#     moment : pas de connexion `ben-provisioned` ⇒ sortie immédiate ; `/run/ben/ble-central-connected`
#     présent ⇒ sortie immédiate. Le « quand » est une PROPRIÉTÉ du script, relue à chaque tour —
#     même doctrine que la déclaration de version de pi-0.10.0 : une condition se re-vérifie, un
#     geste s'oublie. Et elle vaut pour les boîtiers neufs comme pour les 8 existants.
#
# ═══ ET IL COMPTE, CE QUI EST AUSSI IMPORTANT QUE DE RATTRAPER ════════════════════════════════
#
#   Un filet qui rattrape en silence rend un boîtier malade INDISCERNABLE d'un boîtier sain :
#   celui qui perd sa radio toutes les deux heures et qu'on relance chaque fois publie normalement.
#   Le watchdog incrémente donc `/var/lib/ben-firmware/nm-restarts`, et `health` le fait monter À
#   PLAT (`nm_restarts`).
#
#   ⚠️ Aucun champ systemd ne le donnait : `NRestarts` compte les redémarrages AUTOMATIQUES de
#      systemd (politique `Restart=`), pas un `systemctl restart` lancé par un script. Et
#      `health.errors()` ne lit que la priorité 3 (`err`), donc la ligne `logger` du watchdog, en
#      `notice`, n'y remonterait pas.
#
#   ⭐ ET `health` REMONTE DÉSORMAIS LES TIMERS, ce qu'il ne faisait pas du tout : il suffixait
#      chaque nom par `.service`, donc AUCUN timer n'arrivait au cloud — pas même
#      `ben-update.timer`, c'est-à-dire qu'on ne pouvait pas voir si un boîtier prend encore ses
#      OTA. C'est ce qui permettra de vérifier que CETTE update a pris sur les six boîtiers qu'on
#      ne peut pas joindre en SSH.
#
# ═══ CE QUE ÇA NE FAIT PAS ════════════════════════════════════════════════════════════════════
#
#   Aucune migration, aucune table, aucune colonne. Aucun lecteur redémarré. Le nouveau `health.py`
#   est lu par `ben-publisher`, que l'AGENT redémarre à l'étape ⑩ — rien à faire ici.
#   ⇒ Retour arrière vers 0.11.1 : rien à restaurer. Le script et le timer resteraient en place,
#     ce qui est sans danger — ils étaient le but.

set -euo pipefail
TR="→ pi-0.11.2"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
SRC="$REPO/src/pi"
WD="$SRC/wifi-watchdog/wifi_watchdog.sh"
CIBLE=/usr/local/bin/wifi_watchdog.sh
API="http://127.0.0.1:8087/health"

# ═══ PRÉFLIGHT ① — les fichiers livrés sont là et se tiennent ═════════════════════════════════
for f in "$WD" "$SRC/wifi-watchdog/test_wifi_watchdog.sh" "$SRC/publisher/health.py" \
         "$SRC/publisher/test_health.py"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.11.2 incomplet ?)"
done
bash -n "$WD" || fail "le watchdog livré ne passe pas `bash -n`"
# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root sur
#    les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$SRC/publisher/health.py" <<'PYEOF' || fail "health.py ne compile pas"
import ast, pathlib, sys
p = pathlib.Path(sys.argv[1]); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF
log "préflight ① OK (4 fichiers présents, syntaxe bash et python vérifiées)"

# ═══ PRÉFLIGHT ② — LES DEUX BANCS LIVRÉS, SUR LA CIBLE ════════════════════════════════════════
#
#   Celui du watchdog monte de faux `nmcli`/`ping`/`systemctl` dans un PATH temporaire : il tourne
#   donc sans NetworkManager, et il éprouve les QUATRE combinaisons de ses deux gardes plus le
#   compteur. ⚖️ Son contre-témoin est la moitié du banc — un watchdog qui ne redémarrerait JAMAIS
#   passerait les trois cas de refus.
# ⚠️ `TMPDIR=/var/tmp` et pas /tmp : /tmp peut être un tmpfs étroit sur un Pi Zero.
TMPDIR=/var/tmp bash "$SRC/wifi-watchdog/test_wifi_watchdog.sh" \
    || fail "le banc du watchdog échoue — NE PAS déployer en l'état"
TMPDIR=/var/tmp python3 "$SRC/publisher/test_health.py" \
    || fail "le banc de health échoue — NE PAS déployer en l'état"
log "préflight ② OK (banc watchdog : 7 cas · banc health : 35 cas, sur le Python du boîtier)"

# ═══ PRÉFLIGHT ③ — 🚨 LE CORRECTIF EST BRANCHÉ, PROUVÉ SUR L'ARBRE ════════════════════════════
#
# 🚨 SUR L'ARBRE SYNTAXIQUE, JAMAIS UN `grep` : `health.py` NOMME `reseau`, `nm_restarts` et
#    `TIMERS` dans ses commentaires, longuement. Un grep serait VERT sur un fichier qui ne les
#    emploie jamais.
python3 - "$SRC/publisher/health.py" <<'PYEOF' || fail "le correctif est livré mais PAS branché"
import ast, pathlib, sys
arbre = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
definies = {n.name for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef)}
affectees = {t.id for n in arbre.body if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}
# La sonde doit être DÉFINIE et CITÉE dans la liste des sondes de `snapshot` (un tuple de couples).
sondes = {e.elts[0].value for n in ast.walk(arbre) if isinstance(n, ast.Tuple)
          for e in n.elts if isinstance(e, ast.Tuple) and len(e.elts) == 2
          and isinstance(e.elts[0], ast.Constant) and isinstance(e.elts[0].value, str)}
ko = []
if "reseau" not in definies:
    ko.append("reseau() ABSENTE")
if "reseau" not in sondes:
    ko.append("reseau n'est pas dans la liste des sondes de snapshot — jamais appelée, "
              "donc une mesure qu'on croit avoir")
for c in ("TIMERS", "NM_RESTARTS", "FLAT"):
    if c not in affectees:
        ko.append(f"{c} n'est pas défini au niveau du module")
src = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
# ⚖️ Le témoin inverse : `reseau` doit être À PLAT, sinon la requête SQL change de forme.
for n in arbre.body:
    if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "FLAT" for t in n.targets):
        plats = {e.value for e in n.value.elts if isinstance(e, ast.Constant)}
        if "reseau" not in plats:
            ko.append("reseau n'est pas dans FLAT — il remonterait IMBRIQUÉ, et `units` a déjà "
                      "montré qu'une structure imbriquée ne s'interroge pas en SQL")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
print(f"  {len(definies)} fonctions · reseau définie, branchée et à plat · TIMERS en place")
PYEOF
log "préflight ③ OK (la sonde est branchée dans snapshot, et à plat)"

# ═══ LA POSE — LE SCRIPT D'ABORD, LE TIMER ENSUITE ════════════════════════════════════════════
#
# 🚨 L'ORDRE N'EST PAS INDIFFÉRENT. L'unité est DÉJÀ installée sur le parc (le `cp` en bloc
#    d'`install.sh`), et son `ExecStart` pointe vers /usr/local/bin. Activer le timer avant de
#    copier le script produirait un échec toutes les 2 minutes, indéfiniment.
sudo install -m 755 "$WD" "$CIBLE" || fail "copie de $CIBLE impossible"
[ -x "$CIBLE" ] || fail "$CIBLE n'est pas exécutable après la copie"
log "✓ $CIBLE posé, exécutable"

sudo systemctl enable wifi-watchdog.timer >/dev/null 2>&1 || fail "enable du timer impossible"
sudo systemctl start  wifi-watchdog.timer >/dev/null 2>&1 || warn "start du timer refusé"
ETAT="$(systemctl is-enabled wifi-watchdog.timer 2>/dev/null || true)"
[ "$ETAT" = "enabled" ] || fail "wifi-watchdog.timer est « $ETAT », attendu « enabled »"
log "✓ wifi-watchdog.timer : enabled"

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# ⭐ CELUI QUI COMPTE ICI EST LE PREMIER PASSAGE RÉEL DU WATCHDOG. Le reste de cette update ne
#    touche à rien ; ce qui pourrait mal tourner est un script qui échoue à chaque déclenchement —
#    et un `ExecStart` dans le vide en est précisément la forme. On le déclenche donc À LA MAIN,
#    une fois, et on EXIGE qu'il sorte proprement.
# ⚠️ Sur un boîtier qui a du réseau, il ne doit RIEN redémarrer : c'est aussi le contre-témoin.
AVANT="$(cat /var/lib/ben-firmware/nm-restarts 2>/dev/null | tr -dc '0-9' || true)"
AVANT=${AVANT:-0}
sudo systemctl start wifi-watchdog.service >/dev/null 2>&1 || true
sleep 2
if systemctl is-failed wifi-watchdog.service >/dev/null 2>&1; then
    journalctl -u wifi-watchdog -n 10 --no-pager >&2 || true
    fail "wifi-watchdog a ÉCHOUÉ à son premier passage — voir les lignes ci-dessus"
fi
APRES="$(cat /var/lib/ben-firmware/nm-restarts 2>/dev/null | tr -dc '0-9' || true)"
APRES=${APRES:-0}
log "✓ premier passage du watchdog sans échec (compteur $AVANT → $APRES)"
[ "$APRES" != "$AVANT" ] && warn "il a relancé NetworkManager — ce boîtier n'avait donc pas de \
connectivité IP à cet instant. Ce n'est pas un défaut de l'update." || true

# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas et l'avoir interrogée a brûlé pi-0.9.12.
if systemctl is-active --quiet ben-local-api.service; then
    python3 - "$API" <<'PYEOF' || fail "/health ne répond pas, ou la base n'est plus lisible"
import json, sys, time, urllib.request
api, fin, dernier = sys.argv[1], time.time() + 60, None
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(api, timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    print("  /health OK — db lisible")
    sys.exit(0)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ /health répond et la base est lisible"
else
    warn "ben-local-api ne tourne pas — contrôle d'effet sauté, et ce script ne l'a pas touchée"
fi

# ═══ CE QU'IL RESTE À REGARDER ════════════════════════════════════════════════════════════════
log "── à vérifier APRÈS cette update (hors de portée de ce script) ──"
log "   systemctl list-timers wifi-watchdog.timer   → il doit apparaître, toutes les 2 min"
log "   🚨 ET SURTOUT, CÔTÉ CLOUD, au prochain battement quotidien :"
log "      health.units contient maintenant les TIMERS, avec leur état de fichier :"
log "        { \"n\": \"wifi-watchdog.timer\", \"f\": \"enabled\" }   ← le filet est branché"
log "        { \"n\": \"ben-update.timer\",    \"f\": \"enabled\" }   ← ce boîtier prend ses OTA"
log "      et health.nm_restarts, À PLAT, apparaît dès le PREMIER rattrapage."
log "   ⇒ C'est ce qui dira si cette update a pris sur les six boîtiers qu'on ne peut pas"
log "     joindre en SSH — et c'est aussi ce qui empêchera le filet de MASQUER la maladie."
log "   ⚠️ Le compteur qui monte n'est PAS une bonne nouvelle : il dit qu'un boîtier perd son"
log "      réseau régulièrement. Le watchdog le rattrape, il ne l'explique pas (ben-docs#17 ③)."

log "✓ update OK"
