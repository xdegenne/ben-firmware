#!/usr/bin/env bash
# update.sh — → pi-0.9.13 : le garde-fou taint ne tue plus la façade radio,
#                            et le monolithe ben-lora-receiver disparaît du parc.
#
# ⚠️ REPUBLICATION DE pi-0.9.12, BRÛLÉE LE 2026-09-10. Le contenu livré est IDENTIQUE ;
#    seul le contrôle d'effet final était faux : il interrogeait `/info`, une route qui
#    N'EXISTE PAS dans l'API locale (404). L'API était parfaitement saine, le service
#    `active (running)`, tout le reste de l'update appliqué — et le script échouait quand
#    même, laissant `device.json` non bumpé et l'update rejouée à chaque tick, donc la
#    façade radio redémarrée toutes les 10 min. Les routes réelles sont `/ping`, `/health`,
#    `/pdls`, `/live`, `/measurements`, `/curve`, `/chart`, `/consumption`, `/registers`,
#    `/lora-link`, `/events`, `/settings`, `/unprovision`. On contrôle désormais sur
#    `/health` : il exerce `_device_info()` ET une lecture de base, donc il prouve que
#    l'API MARCHE, là où `/ping` prouverait seulement qu'une socket répond (0,15 s mesuré).
#    LEÇON : un contrôle d'effet se vérifie sur la cible AVANT de faire signer le tag, au
#    même titre que le code qu'il contrôle. Un garde-fou faux brûle une version aussi
#    sûrement qu'un vrai défaut.
#
# ═══ (1) LE VERROU TAINT ══════════════════════════════════════════════════════════════════════
#
#   `radio_alive()` commençait par `kernel_died()`. C'est un drapeau PERMANENT — un taint noyau
#   ne se nettoie QUE par un reboot — consulté par un test PÉRIODIQUE dont la seule action
#   corrective est un RESTART DE SERVICE. Aucun restart ne nettoyant un taint, la boucle est
#   sans issue PAR CONSTRUCTION : un oops, n'importe où dans le système, condamne la façade à
#   mourir toutes les 90 s indéfiniment.
#
#   Vécu sur ben-0001 le 2026-09-05 : oops à 14:35 dans le contexte d'un script tiers, sans
#   aucun rapport avec la radio → 208 redémarrages en 6 h, 46 % des mesures perdues — PENDANT
#   QUE la radio acquittait à 130 ms et recevait l'émetteur TIC. Le garde-fou a détruit un
#   service parfaitement sain, et le filet `StartLimitAction=reboot` est passé à 3 s près
#   (cycle de 103 s contre une fenêtre de 300 s pour 3 démarrages).
#
#   La radio est désormais jugée SUR LA RADIO — son self-test SPI — et rien d'autre. Le taint
#   reste un INDICE de diagnostic : signalé UNE fois au journal, sans jamais rien décider.
#
# ═══ (2) LE MONOLITHE ben-lora-receiver EST SUPPRIMÉ ══════════════════════════════════════════
#
#   Découpé en `ben-radio` + `ben-telemetry` en 0.9.1, figé à l'état 0.9.4, exécuté par AUCUN
#   boîtier depuis le cutover 0.9.0. Restaient : 35 Ko de logique dupliquée qui ne pouvait que
#   diverger, et une unit que `install.sh` copiait sur CHAQUE device neuf (`cp *.service`).
#
#   Les références mortes ne dormaient pas tranquilles, elles PRODUISAIENT DES DÉFAUTS :
#
#   • `local_api._unprovision` arrêtait « ben-tic-reader ben-lora-receiver ». Sur un boîtier
#     LoRa moderne, CE STOP N'ARRÊTAIT RIEN : la LED restait tenue pendant le flash d'au
#     revoir, et surtout le `?wipe=1` supprimait une base que `ben-telemetry` gardait ouverte
#     en WAL — elle se recréait dans la seconde. Wipe illusoire, exactement le défaut corrigé
#     le 15/08 dans le script de preshipping, resté intact ici. La liste vient maintenant des
#     CAPABILITIES, avec repli sur l'UNION de tous les services connus : au désappairage, en
#     arrêter trop ne coûte rien (le boîtier s'éteint juste après), en arrêter trop peu casse
#     le wipe.
#
#   • `check_network.READERS_BY_MODEL` était le repli « device pas encore migré ». Mort ET
#     nuisible : mort, parce que tout boîtier arrivé en 0.9.x a franchi la migration
#     0.6.1 → 0.7.0 qui écrit les capabilities ; nuisible, parce que `device.json.model` porte
#     le LABEL COMMERCIAL (« Filaire », « Radio ») depuis la 0.8.0 — la table ne matchait donc
#     PLUS RIEN, on tombait dans la branche « modèle inconnu » et on démarrait le monolithe au
#     lieu de la façade radio. Supprimé : les capabilities sont la norme, et sans elles il n'y
#     a rien à démarrer — on le dit en ERROR plutôt que d'inventer une liste par défaut.
#
#   • `Before=` de `ben-led-release` nommait le monolithe : l'ordonnancement qui libère les
#     pins LED avant les lecteurs ne couvrait plus `ben-radio`/`ben-telemetry`.
#
#   Le répertoire `src/pi/lora-receiver/` RESTE : il héberge `frame_codec`, `curve_codec` et
#   `secure_link`, importés en production par les deux services. Un README l'explique désormais
#   sur place. Le renommer est un chantier à part.
#
# Restart : les services des capabilities déclarées (le correctif taint vit dans ben-radio),
# PUIS ben-local-api — dans cet ordre, l'API ouvrant la base en lecture seule. AUCUNE
# migration, aucune table, aucune colonne. UNIVERSEL (LoRa et filaire, pas de gate).
# Code déjà sur disque après `git checkout pi-0.9.13`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.13"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
grep -q '_taint_signale' "$REPO/src/pi/ben-radio/ben_radio.py" \
    || fail "correctif taint absent de ben_radio (checkout pi-0.9.13 incomplet ?)"
grep -q 'if kernel_died() or not lora_ok' "$REPO/src/pi/ben-radio/ben_radio.py" \
    && fail "radio_alive() consulte ENCORE kernel_died — la boucle de restart survivrait"

[ -f "$REPO/src/pi/lora-receiver/main.py" ] \
    && fail "le monolithe main.py est encore là (checkout incomplet ?)"
[ -f "$REPO/config/systemd/ben-lora-receiver.service" ] \
    && fail "l'unit du monolithe est encore dans le dépôt"
[ -f "$REPO/src/pi/lora-receiver/frame_codec.py" ] \
    || fail "frame_codec.py absent : le répertoire lora-receiver ne devait PAS être supprimé"

grep -rq 'ben-lora-receiver' "$REPO/config/systemd/" \
    && fail "une unit systemd référence encore ben-lora-receiver"
grep -q 'READERS_BY_MODEL' "$REPO/src/pi/provisioner/check_network.py" \
    && fail "le repli par modèle est encore là"
# Ciblé sur l'APPEL, pas sur le nom : le commentaire qui raconte le défaut a sa place
# dans le fichier, et c'est justement lui qui empêche de le réintroduire.
grep -q '"stop", "ben-tic-reader", "ben-lora-receiver"' "$REPO/src/pi/store/local_api.py" \
    && fail "le désappairage arrête encore le monolithe (wipe illusoire)"
grep -q '_reader_units()' "$REPO/src/pi/store/local_api.py" \
    || fail "le désappairage ne dérive pas ses lecteurs des capabilities"

# ⚠️ SURTOUT PAS `python3 -m py_compile` : il ÉCRIT dans __pycache__, qui appartient à root
# sur les boîtiers du parc alors que l'OTA tourne en `ben` → Permission denied, update avortée,
# tag brûlé pour un défaut de droits. `ast.parse` ne touche pas au disque.
python3 - "$REPO" <<'PYEOF' || fail "le code modifié ne compile pas"
import ast, pathlib, sys
repo = pathlib.Path(sys.argv[1]) / "src/pi"
for rel in ("ben-radio/ben_radio.py", "store/local_api.py",
            "provisioner/check_network.py", "provisioner/network_recovery.py"):
    ast.parse((repo / rel).read_text(encoding="utf-8"), filename=rel)
PYEOF

# Le banc couvre désormais QUELS agents démarrent : un device.json sans capabilities ne doit
# RIEN lancer — surtout pas la liste par défaut qui appelait le monolithe.
python3 "$REPO/src/pi/provisioner/test_network_recovery.py" \
    || fail "le banc de récupération échoue — NE PAS déployer en l'état"
log "préflight OK (taint corrigé, monolithe absent, aucune référence vivante)"

# ── Purge de l'unit du monolithe sur CE boîtier ───────────────────────────────────────────────
# Supprimer le fichier du dépôt ne suffit pas : `install.sh` l'a copié dans /etc/systemd/system
# sur tout device provisionné (`cp config/systemd/*.service`). Jamais enable, jamais démarré,
# donc rien ne casse — mais on ne laisse pas derrière soi une unit qu'un `systemctl start`
# malheureux pourrait réveiller sur une base et une radio déjà tenues.
if [ -f /etc/systemd/system/ben-lora-receiver.service ]; then
    sudo systemctl disable --now ben-lora-receiver.service >/dev/null 2>&1 || true
    sudo rm -f /etc/systemd/system/ben-lora-receiver.service || fail "rm unit monolithe"
    log "unit ben-lora-receiver.service supprimée du boîtier"
else
    log "unit ben-lora-receiver.service déjà absente"
fi

# ── Units mises à jour ────────────────────────────────────────────────────────────────────────
sudo install -m 644 -o root -g root \
    "$REPO/config/systemd/ben-ble-provisioner.service" \
    /etc/systemd/system/ben-ble-provisioner.service || fail "install unit provisioner"
sudo install -m 644 -o root -g root \
    "$REPO/config/systemd/ben-led-release.service" \
    /etc/systemd/system/ben-led-release.service || fail "install unit led-release"
sudo systemctl daemon-reload || fail "daemon-reload"
log "units provisioner + led-release à jour (plus de référence au monolithe)"

# ── Redémarrages ──────────────────────────────────────────────────────────────────────────────
# Les services viennent des CAPABILITIES, comme partout ailleurs : ben-radio + ben-telemetry sur
# un boîtier LoRa, ben-tic-reader sur un filaire. Le correctif taint vit dans ben-radio et
# n'aurait sinon pris effet qu'au prochain redémarrage FORTUIT — c'est-à-dire à une date
# inconnue, sur un boîtier qu'on croirait corrigé.
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
[ "$restarted" = 1 ] || log "aucun agent de mesure actif — effectif au prochain démarrage"

# ben-local-api APRÈS les lecteurs : il ouvre la base en LECTURE SEULE et ne peut pas créer le
# schéma. Il porte le désappairage corrigé, et surtout un nouvel import (`capabilities`) — s'il
# était fautif, l'API mourrait au démarrage et l'app perdrait le boîtier. D'où le contrôle.
api_restarted=0
if systemctl is-active --quiet ben-local-api.service; then
    log "restart ben-local-api (désappairage dérivé des capabilities)"
    sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
    api_restarted=1
else
    log "ben-local-api inactif — rien à redémarrer"
fi
sleep 5

# ── Contrôle d'effet ──────────────────────────────────────────────────────────────────────────
systemctl cat ben-lora-receiver.service >/dev/null 2>&1 \
    && fail "systemd connaît TOUJOURS ben-lora-receiver après la purge"

if [ "$api_restarted" = 1 ]; then
    systemctl is-active --quiet ben-local-api.service \
        || fail "ben-local-api ne tourne plus après restart (import capabilities ?)"
    python3 - <<'PYEOF' || fail "l'API locale ne répond plus sur /health"
import json, sys, urllib.request
try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:8087/health", timeout=15).read())
except Exception as e:
    print(f"  /health KO : {e}", file=sys.stderr); sys.exit(1)
# `db: false` = l'API répond mais ne lit plus la base — une panne qu'un simple 200 masque.
if not d.get("db"):
    print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ API locale toujours debout (/health répond, base lisible)"
fi

log "✓ le taint noyau ne pilote plus la vivacité radio : plus de boucle de restart sans issue"
log "  à surveiller : journalctl -u ben-radio | grep -i 'noyau TAINTED'  (une ligne, pas mille)"
log "✓ monolithe ben-lora-receiver supprimé — dépôt, unit du boîtier et références vivantes"
log "  (le répertoire src/pi/lora-receiver/ RESTE : il porte les codecs partagés)"
log "✓ désappairage : les lecteurs à arrêter viennent des capabilities → le wipe ferme la base"
log "✓ update OK"
