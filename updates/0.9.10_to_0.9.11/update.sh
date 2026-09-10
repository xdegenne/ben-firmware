#!/usr/bin/env bash
# update.sh — → pi-0.9.11 : un boîtier qui redémarre AVANT sa box ne reste plus bloqué en
#                            provisioning BLE : fenêtre BLE au boot, puis COLLECTE hors ligne.
#
# ═══ LE DÉFAUT ════════════════════════════════════════════════════════════════════════════════
#
#   `check_network.py` est un ONESHOT : il tranche UNE fois au boot, puis rend la main. Sur un
#   device DÉJÀ provisionné qui démarre sans réseau — cas parfaitement banal d'une coupure de
#   courant, le boîtier rebootant plus vite que la box — il partait en provisioning BLE et
#   PERSONNE ne revenait jamais tester le réseau.
#
#   Rien ne le rattrapait, et c'est vérifiable dans le dépôt :
#     • `ben-network-check.service` est `Type=oneshot`, `WantedBy=multi-user.target`, sans
#       aucun timer ni aucun autre service qui le relance ;
#     • les readers n'ont PAS de `[Install]`/`WantedBy` — ils ne démarrent QUE par un appel
#       explicite de `check_network._start_readers()`.
#
#   Résultat : violet-jaune, aucune mesure, jusqu'à un débranchement manuel. Une panne de
#   courant de 30 s pouvait ainsi coûter des jours de données.
#
#   ⚠️ ET AUCUN FILET NE RATTRAPAIT ÇA. On pourrait croire que `wifi-watchdog` (relance de
#   NetworkManager toutes les 2 min) sauvait au moins le réseau : il n'est INSTALLÉ NULLE PART.
#   `install.sh` ne copie jamais le script vers /usr/local/bin et n'active jamais le timer,
#   aucun update.sh ne le fait — vérifié sur ben-0001 : script absent, timer `disabled`.
#   La seule reprise réelle est l'autoconnect de NetworkManager, qui abandonne après ses
#   `autoconnect-retries` (défaut 4). Le déploiement du watchdog est un chantier À PART.
#
# ═══ LE CORRECTIF ═════════════════════════════════════════════════════════════════════════════
#
#   Nouvel agent `ben-network-recovery.service` (`network_recovery.py`), démarré par
#   `check_network` dans la SEULE branche « déjà provisionné mais réseau KO ». Son cycle :
#
#     1. provisioning BLE (300 s) — le boîtier reste joignable par l'app : une coupure peut
#        aussi être un vrai changement de box, l'utilisateur doit pouvoir reconfigurer ;
#     2. VEILLE PASSIVE pendant cette fenêtre, un ping toutes les 30 s. C'est le cas nominal :
#        NM se reconnecte seul quand la box revient, et on le voit SANS RIEN COUPER ;
#     3. fenêtre écoulée → on arrête le BLE et ON COLLECTE, réseau ou pas.
#
#   DÉROULÉ LINÉAIRE, SANS BOUCLE NI COMPTEUR. Une première version alternait N fois
#   BLE ↔ retentative WiFi avant de renoncer. Inutile : puisqu'on collecte DE TOUTE FAÇON
#   au bout de la fenêtre, la retentative ne décide plus de rien. Il ne reste qu'une
#   relance `nmcli` best-effort juste avant de basculer — elle n'améliore que les chances
#   d'avoir l'heure NTP juste dès le premier point.
#
#   POURQUOI COLLECTER SANS RÉSEAU. Parce que le boîtier n'en a pas besoin pour faire son
#   travail : la base SQLite est LOCALE et l'app lit l'API locale du device (Phase 1, pas de
#   push cloud). Mieux : un boîtier qui perd le réseau EN MARCHE continue de collecter — rien
#   ne l'arrête, `check_network` est un oneshot déjà terminé. Refuser de collecter après un
#   REDÉMARRAGE était donc une INCOHÉRENCE, pas une précaution : la même panne donnait deux
#   comportements opposés selon qu'elle survenait avant ou après le boot.
#
#   L'HORODATAGE HORS LIGNE EST DÉCALÉ, PAS CORROMPU. Le Pi Zero n'a pas de RTC, mais systemd
#   restaure la dernière heure connue au boot et seulement vers l'AVANT (journal ben-0001 :
#   « System time advanced to timestamp on /var/lib/systemd/timesync/clock »). Aucun point ne
#   peut donc s'écrire dans le passé de la base. L'erreur vaut la durée de la coupure ; le
#   retour du NTP la rattrape d'un saut en avant. En mode STANDARD elle est même évitable :
#   `meter_ts` porte déjà l'horodate du COMPTEUR sur 100 % des points (mesuré sur ben-0001 :
#   3802/3802, écart max 8 s avec NTP) — caler l'horloge dessus est un chantier À PART.
#
#   LE BLE N'EST PAS SACRIFIÉ. CHAQUE boot sans réseau rejoue cette fenêtre : un simple
#   débranchement rouvre 5 min de re-provisionnement. C'est ce qui permet de
#   rendre la mesure prioritaire sans enfermer l'utilisateur — le seul chemin de configuration
#   hors ligne étant le BLE, il fallait garantir qu'il reste atteignable.
#
#   UNE SESSION BLE EN COURS RETIENT LA BASCULE : on patiente tant qu'elle dure, sans quoi la
#   radio se couperait sous un utilisateur en pleine configuration. L'expiration du drapeau
#   (15 min) empêche en retour une session oubliée de retenir la collecte indéfiniment.
#
#   POURQUOI LA VEILLE EST UN PING ET PAS UN RESCAN. Sur Pi Zero W la radio WiFi et BLE est
#   PARTAGÉE : c'est exactement pour ça que le rescan périodique avait été supprimé du
#   provisioner en 0.8.2 (il affamait le lien et faisait décrocher les téléphones). Un ping ne
#   coûte rien ; la relance `nmcli` n'a lieu qu'APRÈS l'arrêt du BLE.
#
#   CE QU'ON NE COUPE JAMAIS : une session BLE en cours. Nouveau drapeau
#   `provisioning_state` (fichier dans /run, donc effacé à chaque boot) posé par le
#   provisioner à `on_connect` et retiré à `on_disconnect`. Arrêter la radio pendant qu'un
#   utilisateur saisit son mot de passe changerait la récupération EN PANNE. Le drapeau EXPIRE
#   (15 min) et `main()` l'efface à chaque démarrage : un provisioner tué en pleine session ne
#   peut pas condamner la récupération au silence.
#
#   LE PREMIER UNBOXING N'EST PAS CONCERNÉ. Un boîtier jamais provisionné n'a pas de connexion
#   `ben-provisioned` : `check_network` part en BLE direct, sans récupération. Rester en BLE
#   indéfiniment est son mode NOMINAL, pas une panne — l'alternance n'aurait aucun sens et
#   rendrait le boîtier fuyant pendant l'unboxing.
#
# ═══ AU PASSAGE : UNE EXCLUSION MUTUELLE QUI ÉTAIT FAUSSE ═════════════════════════════════════
#
#   `ben-ble-provisioner.service` déclarait `Conflicts=ben-tic-reader ben-lora-receiver`. Or le
#   monolithe `ben-lora-receiver` a été DÉCOUPÉ en `ben-radio` + `ben-telemetry` en 0.9.1 : sur
#   tout boîtier LoRa en capabilities, aucune des deux units listées n'est jamais active, et
#   l'exclusion ne protégeait donc plus rien. Le provisioner pouvait coexister avec la façade
#   radio et se disputer le SX127x et la LED. Les deux units sont ajoutées ; les noms legacy
#   restent pour les boîtiers non migrés.
#
# EFFET DIFFÉRÉ, ET C'EST VOULU. Cet OTA n'atteint qu'un boîtier EN LIGNE, donc en mode normal,
# donc dont la récupération n'a rien à faire. On installe et on recharge systemd : le correctif
# prend effet au PROCHAIN BOOT — celui qui suivra la prochaine coupure. Aucun service n'est
# redémarré, aucune mesure n'est perdue.
#
# Code (`provisioner/network_recovery.py`, `provisioner/provisioning_state.py`,
# `provisioner/check_network.py`, `provisioner/main.py`) + banc de non-régression
# (`provisioner/test_network_recovery.py`, joué par ce script) + 2 units systemd. AUCUNE migration,
# aucune table, aucune colonne. UNIVERSEL (LoRa et filaire, pas de gate).
# Code déjà sur disque après `git checkout pi-0.9.11`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.11"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
[ -f "$REPO/src/pi/provisioner/network_recovery.py" ] \
    || fail "network_recovery.py absent (checkout pi-0.9.11 incomplet ?)"
[ -f "$REPO/src/pi/provisioner/provisioning_state.py" ] \
    || fail "provisioning_state.py absent"
[ -f "$REPO/config/systemd/ben-network-recovery.service" ] \
    || fail "unit ben-network-recovery.service absente"
grep -q '_start_recovery()' "$REPO/src/pi/provisioner/check_network.py" \
    || fail "check_network part encore en BLE direct (correctif absent)"
grep -q '_start_readers()' "$REPO/src/pi/provisioner/network_recovery.py" \
    || fail "la récupération ne bascule jamais en collecte : le boîtier resterait en BLE"
grep -q 'provisioning_state.set_ble_session()' "$REPO/src/pi/provisioner/main.py" \
    || fail "le provisioner ne signale pas ses sessions BLE"
grep -q 'ben-radio.service ben-telemetry.service' \
     "$REPO/config/systemd/ben-ble-provisioner.service" \
    || fail "Conflicts du provisioner toujours limité aux units legacy"

# Le code DOIT être syntaxiquement valide : un défaut ne se verrait qu'au prochain boot sans
# réseau, c'est-à-dire au pire moment possible et sans personne pour le lire.
# ⚠️ SURTOUT PAS `python3 -m py_compile` : il ÉCRIT dans __pycache__, qui appartient à root
# sur les boîtiers du parc (vérifié sur ben-0001) alors que l'OTA tourne en `ben`. Il
# échouerait en Permission denied et ferait avorter une update pourtant saine — un tag brûlé
# pour un défaut de droits. `ast.parse` ne touche pas au disque.
python3 - "$REPO" <<'PYEOF' || fail "le code de récupération ne compile pas"
import ast, pathlib, sys
repo = pathlib.Path(sys.argv[1]) / "src/pi/provisioner"
for name in ("network_recovery.py", "provisioning_state.py", "check_network.py", "main.py"):
    ast.parse((repo / name).read_text(encoding="utf-8"), filename=name)
PYEOF

# Et il doit se COMPORTER : compiler ne prouve rien sur une machine à états. Le banc déroule
# les cycles avec GPIO/systemctl/ping stubbés — c'est le SEUL contrôle d'effet possible ici,
# puisque le correctif ne s'exercera qu'au prochain boot sans réseau. Il vérifie notamment
# qu'une session BLE en cours n'est jamais coupée : s'y tromper transformerait la
# récupération en panne d'unboxing.
python3 "$REPO/src/pi/provisioner/test_network_recovery.py" \
    || fail "le banc de récupération échoue — NE PAS déployer en l'état"
log "préflight OK (code présent, compilable, comportement vérifié)"

# ── Units systemd ─────────────────────────────────────────────────────────────────────────────
# La nouvelle unit n'a PAS d'[Install] : rien à `enable`. Elle est démarrée exclusivement par
# check_network, et seulement pour un device déjà provisionné.
sudo install -m 644 -o root -g root \
    "$REPO/config/systemd/ben-network-recovery.service" \
    /etc/systemd/system/ben-network-recovery.service || fail "install unit recovery"
log "unit ben-network-recovery.service installée"

sudo install -m 644 -o root -g root \
    "$REPO/config/systemd/ben-ble-provisioner.service" \
    /etc/systemd/system/ben-ble-provisioner.service || fail "install unit provisioner"
log "unit ben-ble-provisioner.service mise à jour (Conflicts complété)"

sudo systemctl daemon-reload || fail "daemon-reload"

# ── Contrôle d'effet ──────────────────────────────────────────────────────────────────────────
# systemd doit CONNAÎTRE la nouvelle unit, sinon le `systemctl start` de check_network échouera
# silencieusement au prochain boot sans réseau — et on aurait exactement le défaut d'origine.
systemctl cat ben-network-recovery.service >/dev/null 2>&1 \
    || fail "systemd ne connaît pas ben-network-recovery.service après daemon-reload"

# Le provisioner ne doit PAS tourner ici : on est en ligne, donc en mode normal. S'il tourne,
# c'est que le boîtier était déjà en récupération — on ne le touche pas, il fera son cycle.
if systemctl is-active ben-ble-provisioner.service >/dev/null 2>&1; then
    log "⚠ provisioner actif pendant l'update — non touché, le cycle reprendra la main"
fi

log "✓ ben-network-recovery installée : fenêtre BLE de 5 min au boot sans réseau"
log "✓ après 5 min de fenêtre BLE, le boîtier COLLECTE hors ligne au lieu d'attendre"
log "  (chaque boot sans réseau rejoue cette fenêtre → re-provisionnement toujours possible)"
log "  effet au PROCHAIN boot sans réseau (rien n'est redémarré maintenant)"
log "  à surveiller : journalctl -u ben-network-recovery -b"
log "✓ update OK"
