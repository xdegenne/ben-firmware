#!/usr/bin/env bash
# update.sh — 0.9.27 → pi-0.9.28 : LE BOÎTIER APPREND LA VERSION DE SON ÉMETTEUR, ET LA DIT.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   Volet ② du chantier `ben-docs#12` (sous-tâche #28). L'émetteur Arduino se livre par reflash
#   PHYSIQUE — il n'y a pas d'OTA sur AVR — donc sa version n'était lisible qu'à son banner
#   série, un FTDI en main, DEVANT le boîtier. Depuis `tic-reader` 0.1.10 il l'annonce dans sa
#   trame de boot (TLV `T_FW`, trois octets) ; cette version la décode, la range et la fait
#   monter.
#
#     `frame_codec`   T_FW = 0x07 → "0.1.11", nommé « FW », déclaré STOCKÉ
#     `emitter`       + colonne `fw_version` (NULLABLE, sans DEFAULT — voir plus bas)
#     `ben-telemetry` décode le TLV à la trame de boot et range la valeur
#     `health`        la fait monter dans la ligne de SON émetteur (`health.emitter[].fw`)
#
#   🚨 UNE LIGNE PAR ÉMETTEUR, PAS UN CHAMP DE BOÎTIER. La version appartient au SATELLITE, donc
#      au compteur qu'il lit, et un boîtier peut en écouter PLUSIEURS. Un champ unique à côté de
#      `sw` aurait été faux dès le second émetteur, et faux EN SILENCE.
#
#   ⭐ ET ELLE REMPLACE DEUX VALEURS QUI MENTENT, relevées sur ben-0001 le 2026-10-04 :
#
#        "arduinoFirmwareVersion": "0.0.6"       ← pas même la lignée de numéros de l'émetteur
#        "lora-tic-receiver": { "fw": "0.1.2" }  ← et pas 0.1.3, que le code sème au provisioning
#
#      Trois valeurs, trois faussetés différentes, pour UN émetteur qui tourne en 0.1.8. Les deux
#      cessent de monter : `caps_for_model` ne les écrit plus, et `health.versions()` les retire
#      À L'ÉMISSION — ce qui vaut pour les `device.json` DÉJÀ posés, sans réécrire le fichier
#      (l'agent d'OTA le réécrit, et le toucher ici a déjà coûté des tours de boucle).
#
# ═══ 🚨 CE QU'IL FAUT ATTENDRE APRÈS CETTE UPDATE, ET QUI N'EST PAS UNE PANNE ═════════════════
#
#   `fw_version` restera `NULL` SUR TOUT LE PARC jusqu'au prochain BOOT de chaque émetteur.
#
#   Ce n'est pas un défaut de livraison, c'est la mécanique : l'émetteur ne redémarre pas quand
#   le Pi redémarre. Il reste en STREAMING, ses courbes continuent d'arriver, et il ne rejoue sa
#   trame de boot — la SEULE à porter `T_FW` — qu'en revenant en REGISTERING. Or l'ACK applicatif
#   qui l'y maintient est émis par `ben-radio`, que ce script NE redémarre PAS (voir plus bas).
#
#   ⇒ Un `NULL` partout au lendemain de cette update est l'état ATTENDU. Pour le remplir il faut
#     couper l'alim d'un émetteur, ou attendre la prochaine coupure de courant du site.
#
#   ⚠️ Et c'est aussi pour ça que `NULL` ne vaut PAS « antérieur à la campagne ». Trois états,
#      jamais deux (cf. le commentaire de la colonne dans `store/db.py`) :
#
#        "0.1.11"              annoncée par l'émetteur — fait MESURÉ
#        "anterieur-campagne"  trame de boot reçue SANS le TLV ⇒ émetteur < 0.1.10, À REFLASHER
#        NULL                  aucune trame de boot vue — on n'a pas encore regardé
#
#      Confondre les deux derniers, c'est perdre la seule information qui permette de piloter une
#      campagne de reflash sur des faits.
#
# ═══ LA MIGRATION, ET POURQUOI ELLE EST SANS DANGER — MESURÉ, PAS PLAIDÉ ═════════════════════
#
#   `ALTER TABLE emitter ADD COLUMN fw_version TEXT` — NULLABLE, SANS DEFAULT. En SQLite c'est
#   une opération de MÉTADONNÉE SEULE : la ligne de schéma est réécrite, aucune page de données
#   ne l'est, et les lignes existantes relisent `NULL` sans être touchées.
#
#   ⭐ ÉPROUVÉ SUR LA BASE RÉELLE DE ben-0001 LE 2026-10-04 — 535 Mo, lecteur en marche :
#
#        db.connect() avec l'ALTER .......  62 ms
#        db.connect() suivant (no-op) ....  34 ms      ⇒ l'ALTER a coûté ~28 ms
#        user_version ....................  1, INCHANGÉ ⇒ le backfill one-shot n'a PAS rejoué
#        ligne émetteur .................. (31, '031864467282', 0, 1791066099, None) — intacte
#        last_tic_ts ..................... 1791097096 → 1791097178  ⇒ le lecteur a enregistré
#                                                                     PENDANT et après
#        ben-publisher (code 0.9.27) ..... a publié 86 points APRÈS l'ajout, « reste ~0 »
#
#   🚨 LA DERNIÈRE LIGNE EST LA GARANTIE DE RETOUR ARRIÈRE, et elle est désormais mesurée et non
#      argumentée : l'ANCIEN code tourne sur le NOUVEAU schéma. Tous les accès à `emitter` du
#      dépôt nomment leurs colonnes (aucun `SELECT *`), donc un service qui n'a pas redémarré se
#      comporte à l'identique. C'est ce qui rend le retour arrière possible SANS restaurer quoi
#      que ce soit — la règle de toutes les migrations de `db.py`.
#
#   ⓘ Et donc : RIEN À SAUVEGARDER. Une sauvegarde de 535 Mo tiendrait un verrou de lecture
#     pendant toute la copie sur un Pi Zero mono-cœur — elle COÛTERAIT des trames LoRa, pour
#     assurer contre un risque qu'un ADD COLUMN nullable ne porte pas. La règle « sauvegarder
#     avant validation » vise les SUPPRESSIONS, pas ça.
#
#   ⚠️ ben-0001 a DÉJÀ la colonne (migrée à la main le 2026-10-04, pour ce test). L'`ALTER` est
#      conditionnel, donc cette update y est un no-op de schéma — et le préflight le vérifie.
#
# ═══ 🚨 QUI REDÉMARRE, ET CE QUI NE DOIT SURTOUT PAS ══════════════════════════════════════════
#
#   ✓ `ben-telemetry` — OBLIGATOIRE, et c'est le seul redémarrage que cette update PAIE. Le
#     décodeur `T_FW` vit là. Sans ce redémarrage la colonne existe et RIEN ne la remplit jamais.
#
#     🚨 Et ça ne peut pas être l'agent qui le fasse : il ne redémarre QUE `ben-publisher`
#        (`updater/check_update.py`). Omettre cette ligne livrerait un chantier inerte, sans
#        qu'aucun contrôle ne s'en plaigne.
#
#     ⚠️ LE PRIX, nommé : un redémarrage de ce service coûte les trames reçues pendant sa
#        reconnexion MQTT (quelques secondes). À la cadence du batch (~40 s) c'est au pire un
#        lot. Il n'y a pas d'alternative : le code neuf est là.
#
#   🚨 PAS `ben-radio`, EN AUCUN CAS — et surtout pas via le helper générique
#      `capabilities.py restart lora-tic-receiver`, qui mappe la capability sur DEUX services et
#      l'emporterait avec. Elle est seule maîtresse du RFM95 et du SPI ; le verrou taint de
#      0.9.12 est né d'un redémarrage de trop (208 redémarrages, 46 % des mesures perdues). On se
#      sert de `capabilities has` pour DÉCIDER, et on nomme les unités À LA MAIN.
#
#   ✓ `ben-local-api`, et seulement si elle tournait : dernier consommateur de `db.py`, et son
#     redémarrage ne coûte ni mesure, ni GPIO, ni radio. ⚠️ APRÈS la migration — elle ouvre en
#     lecture seule et ne peut rien créer.
#
#   ✗ `ben-tic-reader` — PAS touché. Sur un boîtier filaire ce chantier est un no-op : le lecteur
#     filaire n'appelle jamais `record_emitter_fw`, et le nouveau `db.py` lui est compatible.
#     Redémarrer un lecteur pour rien coûte des mesures (leçons de 0.9.17 et 0.9.21).
#
#   ⓘ `ben-publisher` porte le `health.py` neuf — l'agent le redémarre lui-même APRÈS nous
#     (étape ⑩). Rien à faire ici, et surtout pas le doubler.
#
# MIGRATION : OUI — une colonne, AJOUT SEUL, nullable, sans DEFAULT.
#
#   ⚠️ Un `NOT NULL DEFAULT 'anterieur-campagne'` aurait été faux DEUX FOIS : il aurait fabriqué
#      un fait d'apparence mesurée pour les émetteurs des 7 boîtiers sans qu'une seule trame de
#      boot soit arrivée, et il aurait détruit la distinction `NULL` / « antérieur » sur laquelle
#      tout ce chantier repose. La nullabilité n'est pas incidentelle, elle PORTE le sens.

set -euo pipefail
TR="→ pi-0.9.28"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
SRC="$REPO/src/pi"
STORE="$SRC/store"
API="http://127.0.0.1:8087/health"

# ═══ PRÉFLIGHT ① — les fichiers livrés sont là et compilent ═══════════════════════════════════
FICHIERS=(
    "$STORE/db.py"
    "$SRC/lora-receiver/frame_codec.py"
    "$SRC/ben-telemetry/ben_telemetry.py"
    "$SRC/publisher/health.py"
    "$SRC/capabilities.py"
)
for f in "${FICHIERS[@]}"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.9.28 incomplet ?)"
done

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "${FICHIERS[@]}" <<'PYEOF' || fail "un fichier livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF
log "préflight ① OK (5 fichiers présents et compilables)"

# ═══ PRÉFLIGHT ② — 🚨 LE BANC QUE LE TAG LIVRE, EXÉCUTÉ SUR LE PYTHON DU BOÎTIER ══════════════
#
#   13 cas, du TLV jusqu'à `health.snapshot()`, AUCUN matériel. Compiler ne prouve rien d'un
#   décodage ni d'un rangement ; ce banc le prouve, et il le prouve sur l'interpréteur du
#   boîtier plutôt que sur celui d'un runner x86.
#
# ⚖️ LE CAS QUI COMPTE LE PLUS Y VIENT EN PREMIER, et il protège le parc : AUCUN émetteur du parc
#    n'émet encore ce TLV. Un contrôle trop strict ferait cesser l'enregistrement de TOUS les
#    boîtiers radio d'un coup. Le banc exige qu'une trame de boot SANS `0x07` produise exactement
#    ce qu'elle produisait — PDL créé, émetteur lié, abonnement, époque tarifaire, mesure
#    d'unboxing — et que son absence soit rangée comme « antérieur », jamais comme un trou.
#
# ⓘ Précédent : `0.9.10_to_0.9.11` et `0.9.11_to_0.9.13` exécutent déjà un banc livré.
# ⚠️ `TMPDIR=/var/tmp` et pas /tmp : /tmp peut être un tmpfs étroit sur un Pi Zero, et les bases
#    jetables du banc n'ont aucune raison de disputer de la RAM au lecteur.
BANC="$SRC/ben-telemetry/test_fw_emetteur.py"
[ -f "$BANC" ] || fail "le banc du chantier est absent : $BANC"
TMPDIR=/var/tmp python3 "$BANC" \
    || fail "le banc de la version d'émetteur échoue — NE PAS déployer en l'état"
log "préflight ② OK (banc livré : 13 cas, décodage + rangement + hello, témoins des deux sens)"

# ═══ LA MIGRATION SUR LA CIBLE, ET LE GARDE QUI LA PROUVE ════════════════════════════════════
#
# ⚠️ C'est un ALTER sur une base qu'un lecteur écrit en continu. `db.connect()` a un `timeout=5` ;
#    un lecteur qui groupe ses écritures peut tenir le verrou plus longtemps. On réessaie, et on
#    ÉCHOUE FRANCHEMENT si la base reste inaccessible : l'update sera rejouée au tick suivant
#    (`device.json` non bumpé), ce qui est exactement le bon comportement.
#
# 🚨 ET C'EST ICI QUE VIT LE GARDE-FOU DE CETTE VERSION. Si `fw_version` n'est pas là après
#    l'ouverture en écriture, on échoue BRUYAMMENT — plutôt que de laisser `health` se replier en
#    silence et le chantier être inerte pour toujours. Un échec ne bumpe pas `device.json`, donc
#    l'update rejoue, donc ça se VOIT. Même forme que 0.9.27 pour `pdl.ref`.
ETAT_AVANT="$(python3 - "$SRC" <<'PYEOF'
import sqlite3, sys, time
sys.path[:0] = [sys.argv[1]]
from store import db

derniere = None
conn = None
for _ in range(6):
    try:
        conn = db.connect()          # ÉCRITURE → rejoue les migrations idempotentes
        break
    except sqlite3.Error as e:
        derniere = e
        time.sleep(5)
if conn is None:
    print(f"base inouvrable en écriture après 6 essais sur 30 s : {derniere}", file=sys.stderr)
    sys.exit(1)

cols = [r[1] for r in conn.execute("PRAGMA table_info(emitter)")]
if "fw_version" not in cols:
    print("  emitter.fw_version ABSENTE après l'ouverture en écriture — le décodage n'aurait "
          "rien où ranger, et `health` se replierait en silence : colonnes = %r" % (cols,),
          file=sys.stderr)
    sys.exit(1)

# ⓘ Idempotence VÉRIFIÉE sur la cible, et pas seulement sur une base jetable : ben-0001 a été
#   migré à la main le 2026-10-04, donc il traverse ce chemin-là et pas celui de l'ALTER.
conn.close()
conn = db.connect()
n = conn.execute("SELECT count(*) FROM emitter").fetchone()[0]
connus = conn.execute(
    "SELECT count(*) FROM emitter WHERE fw_version IS NOT NULL").fetchone()[0]
conn.close()
print(f"{n} {connus}")
PYEOF
)" || fail "la migration de schéma n'a pas pu être appliquée ni vérifiée sur la cible"
EMETTEURS=${ETAT_AVANT%% *}; CONNUS=${ETAT_AVANT##* }
log "✓ emitter.fw_version présente sur la base du boîtier (migration idempotente vérifiée)"
log "  $EMETTEURS émetteur(s) connu(s), dont $CONNUS avec une version déjà rangée"
# ⚠️ 0 est l'état ATTENDU ici, et pour longtemps : voir l'en-tête. Ce n'est PAS un échec.
[ "$CONNUS" = "0" ] && log "  ⓘ 0 version rangée = NORMAL : aucun émetteur n'a encore rejoué sa trame de boot" || true
[ "$EMETTEURS" = "0" ] && log "  ⓘ 0 émetteur = boîtier filaire, ou radio n'ayant jamais reçu de trame de boot" || true

# ═══ COMPOSITION DU BOÎTIER — on INTERROGE, on ne devine pas ══════════════════════════════════
#
# ⭐ Même source de vérité que le boot : `capabilities.py`, qui lit `device.json`. Jamais
#    `device.json.model`, qui porte un LABEL commercial (« Radio », « Filaire ») — s'y fier a
#    déjà été supprimé en 0.9.12.
CAPS="$SRC/capabilities.py"
[ -f "$CAPS" ] || fail "capabilities.py absent — on ne touche à AUCUN service dans le doute"

RADIO=0
if python3 "$CAPS" has lora-tic-receiver >/dev/null 2>&1; then
    RADIO=1
    log "capability lora-tic-receiver → ben-telemetry SERA redémarré (le décodeur T_FW vit là)"
else
    log "pas de capability lora-tic-receiver → aucun décodeur à recharger, ce chantier est un"
    log "  no-op sur ce boîtier. ben-tic-reader n'est PAS touché (le nouveau db.py lui convient)"
fi

# ═══ ÉTAT AVANT côté /health, pour que le contrôle d'effet ait un point de comparaison ════════
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
    # ⚠️ `|| true` OBLIGATOIRE : sous `set -e`, un `[ … ] && { … }` qui est la DERNIÈRE commande
    #    d'un bloc `then` et qui rend non-zéro abat le script. Ça mord quand ben-local-api est
    #    « active » mais /health injoignable — l'update échouerait sans message explicatif.
    [ -n "${AVANT:-}" ] && { TS_AVANT=${AVANT%% *}; LISAIT=${AVANT##* }; } || true
fi
# ⚠️ Si le boîtier NE LISAIT PAS avant, on n'exige pas de lecture après : un câble TIC débranché
#    ou un émetteur muet ferait échouer l'update, donc la ferait REJOUER toutes les 10 min pour
#    toujours. C'est la mécanique exacte qui a brûlé pi-0.9.12.
[ "$LISAIT" = "1" ] \
    && log "avant : le boîtier lisait (last_tic_ts=$TS_AVANT) — une lecture sera EXIGÉE après" \
    || { LISAIT=0; warn "avant : aucune lecture récente — la lecture ne sera pas exigée"; }

# ═══ REDÉMARRAGES — ben-telemetry PUIS ben-local-api, et JAMAIS ben-radio ═════════════════════
#
# ⚠️ L'ordre : le décodeur d'abord, l'API ensuite. La migration est déjà faite plus haut, donc
#    les deux démarreront sur un schéma complet.
redemarre() {
    # 🚨 `is-active --quiet` NE SUFFIT PAS : il rend non-zéro pour `activating`, donc il rate
    #    précisément un service en boucle de plantage. On lit l'ÉTAT COMPLET.
    local unite="$1" etat tourne
    etat=$(systemctl is-active "$unite" 2>/dev/null || true)
    case "$etat" in
        active|activating|reloading|deactivating) tourne=oui ;;
        *)                                        tourne=non ;;
    esac
    log "$unite : état systemd « $etat » → tourne : $tourne"
    # ⭐ ON NE DÉMARRE PAS ce qui ne tournait pas : ce qui doit tourner est une décision de
    #    `check_network` à partir des capabilities. Une update livre du code, elle ne décide pas
    #    de la composition du boîtier (leçon de 0.9.17, 2 538 courses au GPIO perdues).
    if [ "$tourne" = "oui" ]; then
        sudo systemctl restart "$unite"
        sleep 8
        systemctl is-active --quiet "$unite" \
            || fail "$unite ne démarre plus — voir « journalctl -u ${unite%.service} -n 40 »"
        log "✓ $unite debout"
    else
        warn "$unite ne tourne pas — on ne la démarre PAS ici, le code neuf est en place"
    fi
}

if [ "$RADIO" = "1" ]; then
    # 🚨 NOMMÉE À LA MAIN, et jamais `capabilities.py restart lora-tic-receiver` : ce helper
    #    mappe la capability sur ben-radio ET ben-telemetry, et emporterait la façade radio.
    redemarre ben-telemetry.service
fi
redemarre ben-local-api.service

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas, et l'avoir interrogée a brûlé
#    pi-0.9.12. /health prouve EN PLUS que la base est LISIBLE (`db: true`), là où un simple code
#    200 masquerait la panne.
#
# ⭐ Et c'est le contrôle du pire cas de CETTE version : on a redémarré le DÉCODEUR. S'il ne
#    revenait pas, ou revenait sans retrouver sa base, le service serait « active », les journaux
#    calmes, et le boîtier cesserait simplement d'enregistrer. `last_tic_ts` qui avance est la
#    seule preuve que la chaîne radio → décodage → base est debout.
#
# 🚨 ON N'EXIGE RIEN DE `fw_version` ICI, et c'est délibéré. Aucun émetteur du parc n'émet le
#    TLV, et aucun ne rejouera sa trame de boot du fait de cette update — exiger une version
#    serait un contrôle IMPOSSIBLE À TENIR, qui ferait échouer puis rejouer l'update toutes les
#    10 minutes. C'est exactement ce qui a brûlé pi-0.9.12 : un garde-fou faux brûle une version
#    aussi sûrement qu'un vrai défaut.
#
# ⓘ 150 s : l'écriture du lecteur est groupée, `last_tic_ts` met une quinzaine de secondes à
#   bouger même quand tout va bien, et un batch LoRa arrive toutes les ~40 s.
if [ "$LISAIT" = "1" ]; then
    python3 - "$API" "$TS_AVANT" <<'PYEOF' || fail "le boîtier ne lit plus après le redémarrage du décodeur"
import json, sys, time, urllib.request
api, avant = sys.argv[1], int(sys.argv[2])
fin, dernier = time.time() + 150, None
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(api, timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    ts = d.get("last_tic_ts") or 0
    if ts > avant:
        print(f"  mesure enregistrée après le redémarrage (last_tic_ts {avant} → {ts})")
        sys.exit(0)
    dernier = f"last_tic_ts toujours à {ts}, inchangé depuis {avant}"
    time.sleep(5)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ le boîtier enregistre toujours, et la base est lisible après la migration"
else
    warn "lecture non exigée sur ce boîtier — la migration reste VÉRIFIÉE plus haut"
fi

# ═══ CE QU'IL RESTE À REGARDER, ET QUE CE SCRIPT NE PEUT PAS VÉRIFIER ═════════════════════════
log "── à vérifier APRÈS cette update (hors de portée de ce script) ──"
log "   🚨 \`fw_version\` restera NULL jusqu'au prochain BOOT de chaque émetteur, et CE N'EST"
log "      PAS UNE PANNE : l'émetteur ne redémarre pas quand le Pi redémarre, il reste en"
log "      STREAMING et ne rejoue pas sa trame de boot (la seule à porter T_FW)."
log "   ⇒ pour le remplir : couper l'alim d'un émetteur, ou attendre une coupure du site."
log "     Attendu alors : 'anterieur-campagne' (émetteurs du parc en 0.1.8, pas de TLV) —"
log "     c'est le témoin ① en grandeur réelle. Une VRAIE version demande un reflash ≥ 0.1.10."
log "   journalctl -u ben-telemetry -n 40 --no-pager   → « émetteur 0x.. : firmware ... »"
log "   health.emitter[].fw dans le battement quotidien → la vue parc, côté cloud"

log "✓ update OK"
