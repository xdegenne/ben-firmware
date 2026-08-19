#!/usr/bin/env bash
# update.sh — → pi-0.9.10  : (1) le récepteur sait lire STGE, seul porteur de la couleur Tempo
#                                de DEMAIN ; (2) l'anti-rollback d'index compare enfin PAR REGISTRE ;
#                                (3) la façade radio PUBLIE l'issue des commandes descendantes.
#
# ═══ (1) STGE — LA COULEUR DU LENDEMAIN ════════════════════════════════════════════════════════
#
#   Le TLV 0x23 (`STGE`, registre de statuts du mode standard, 32 bits bruts) est désormais
#   NOMMÉ et INTERPRÉTÉ par `frame_codec`. Il devient « connu mais non stocké » et part dans
#   `log_uncabled()` à chaque changement, sous forme lisible :
#       non câblé : STGE='0x013A4401 jour=bleu demain=néant' (collecté, pas stocké)
#
#   POURQUOI CE CHAMP. La couleur du LENDEMAIN n'existe nulle part ailleurs dans la TIC
#   standard. `NJOURF+1`, le candidat évident, renvoie au calendrier FOURNISSEUR — qu'EDF ne
#   programme pas pour Tempo : il vaut 0 en permanence (vérifié sur ben-0001 les 12, 13 et
#   14/08, contrat Tempo actif depuis le 13/08 06:00). L'émetteur 0.1.8 a d'ailleurs DÉSACTIVÉ
#   NJOURF/NJOURF+1 pour financer STGE : 6 octets d'air échangés à l'identique, du vide contre
#   de la donnée — et 74 octets de flash récupérés au passage.
#
#   L'OFFSET DES BITS VIENT D'UNE TRAME RÉELLE, PAS D'UNE DOC. Deux sources publiques se
#   contredisaient d'un bit (24-25/26-27 contre 25-26/27-28). La capture du 14/08
#   (`data/tic-ben0001-20260814-1343.bin`, sha256 18173ebf…) donne STGE=013A4401 : la
#   convention 24-25 = jour rend « BLEU », conforme au terrain ET à l'API publique — et
#   surtout TOUS les autres champs du registre tombent juste avec elle (index fournisseur = 2
#   concorde avec NTARF=02, sortie télé-info = standard, mode consommateur). Un décalage d'un
#   bit ferait dérailler la table entière. Cf. docs/tic-stge-capture-2026-08-14.md.
#
#   CE QUE ÇA NE FAIT PAS : aucun stockage, aucune colonne. On OBSERVE d'abord — on ignore
#   encore si les bits 26-27 se peuplent (« néant » à 13:43 le 14/08 alors que l'API publique
#   connaissait déjà la couleur du 15 ; la littérature dit ~20 h).
#
#   PRÉREQUIS ÉMETTEUR (LoRa uniquement) : `tic-reader` ≥ 0.1.8 (reflash manuel, pas d'OTA sur
#   AVR). En dessous, aucun TLV 0x23 n'arrive : sans effet, sans risque.
#
#   LE FILAIRE EST SERVI AUSSI, ET SANS INTERVENTION SUR SITE. `main_uart.py` lit la TIC
#   directement : il parse `STGE` et le journalise par le même `log_uncabled`, au même format.
#   Aucune contrainte de flash ni de charge utile là-bas — donc rien à sacrifier, NJOURF et
#   NJOURF+1 y restent actifs (par symétrie de lecture ; ils valent 0 de toute façon).
#   Asymétrie à connaître : sur LoRa `STGE` exige un reflash physique de l'Arduino, sur filaire
#   cet OTA suffit. Un boîtier filaire en Tempo standard pourrait donc trancher la question de
#   la couleur du lendemain AVANT le parc LoRa. Aucun n'est en service à ce jour (pi10jd75 est
#   en historique, donc sans STGE) : le code part non éprouvé, en attente d'un tel boîtier.
#
# ═══ (2) ANTI-ROLLBACK D'INDEX — COMPARER PAR REGISTRE ═════════════════════════════════════════
#
#   Deux défauts, découverts le 14/08 sur ben-0001 après sa bascule en Tempo.
#
#   (a) UN SEUL CASIER POUR TOUS LES REGISTRES STANDARD. `active_name` vaut None pour tous les
#       registres standard (ils sont opaques, libellés par le LTARF fournisseur, pas de table
#       en dur) — et c'est lui qui servait de clé. Les 10 registres Tempo partageaient donc un
#       unique casier. Le registre 1 y avait déposé 15 415 362 Wh (le Linky ne remet pas ses
#       EASF à zéro en changeant d'offre : tout l'historique pré-Tempo est tombé dedans), et le
#       registre 2, né à ~4 000 Wh le 13/08 à 06:00, passait pour un rollback de 15 millions.
#       Aggravant : la mise à jour de l'état vivait dans la branche `else`, donc l'alerte se
#       VERROUILLAIT — une ligne toutes les 40 s, des heures durant.
#
#   (b) LA CLÉ NE SURVIVAIT PAS AU JSON. `None` se sérialise en la CHAÎNE "null", que
#       `.get(None)` ne retrouve plus au rechargement → deux clés "null" en double dans
#       lora-state.json, et un garde-fou AMNÉSIQUE repartant de 0 à chaque redémarrage. Il
#       avait donc l'air de marcher (plus d'alerte) alors qu'il ne protégeait plus rien.
#
#   CORRECTIF : clé texte explicite, `s<index_id>` en standard et le nom canonique en
#   historique (BASE/HCHC/BBRHCJB… — il EXISTE là-bas, et les boîtiers historiques l'ont déjà
#   en base, migration `last_base` comprise). Le préfixe évite en prime la collision entre
#   l'ère historique et l'ère standard d'un MÊME boîtier : ben-0001 a fait cette bascule, son
#   BASE et son registre standard n°1 ne sont pas le même compteur physique.
#   La mise à jour devient INCONDITIONNELLE : on SIGNALE une fois, on ne verrouille plus. La
#   vraie défense contre un compteur étranger reste le garde ADCO.
#   `load_state()` purge les clés "null" héritées — elles ne désignent aucun registre.
#
# ═══ (3) L'ISSUE D'UNE COMMANDE DESCENDANTE DEVIENT OBSERVABLE ═════════════════════════════════
#
#   `ben-radio` publie désormais, sur `ben/lora/tx/ack`, le résultat de chaque commande émise
#   sur le canal descendant :
#       {ts, to, cmd, cnt, hid, ack: bool, rtt_ms, id}     `ack:false` = 3 essais sans réponse
#
#   POURQUOI. `send_acked()` connaît le sort d'un ordre dès l'ACK RadioHead — à ~130 ms près —
#   mais ne le disait QU'AU JOURNAL. Un client du bus ne pouvait donc pas distinguer « ordre
#   parti » de « ordre acquitté par la cible » : il affichait le même « envoyé » dans les deux
#   cas. Conséquence structurelle, indépendante de tout actionneur : une cible devenue SOURDE
#   est indiscernable d'une cible qui marche, et une panne qui était datable à la seconde
#   devient une plage d'incertitude de plusieurs heures. Le canal descendant n'avait aucun
#   retour d'exécution — c'est ce trou-là qu'on ferme.
#
#   SANS ÉTAT ET SANS CONSOMMATEUR OBLIGATOIRE. `id` est recopié TEL QUEL depuis la demande
#   reçue sur `ben/lora/tx` : c'est le client qui corrèle, la façade ne mémorise rien (elle
#   reste stateless, cf. CONSTITUTION). Publication en `qos=0` et sous `try/except` : un broker
#   qui râle ne doit jamais faire échouer l'émission radio, qui a déjà eu lieu. Aucun abonné
#   n'est requis — le topic est purement additif, rien ne change pour qui l'ignore.
#
# Pur code (`lora-receiver/frame_codec.py`, `ben-telemetry/ben_telemetry.py`,
# `tic-reader/main_uart.py`, `ben-radio/ben_radio.py`). AUCUNE migration,
# aucune table, aucune colonne. UNIVERSEL (LoRa et filaire) : pas de gate capability — un
# boîtier filaire ne reçoit simplement jamais le TLV 0x23, et le correctif (2) le sert aussi.
# Code déjà sur disque après `git checkout pi-0.9.10`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.10"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
grep -q '^T_STGE = 0x23' "$REPO/src/pi/lora-receiver/frame_codec.py" \
    || fail "tag STGE absent de frame_codec (checkout pi-0.9.10 incomplet ?)"
grep -q 'def stge_couleurs' "$REPO/src/pi/lora-receiver/frame_codec.py" \
    || fail "décodeur de couleurs STGE absent"
grep -q 'reg_key = f"s{index_id}" if src_standard else active_name' \
     "$REPO/src/pi/ben-telemetry/ben_telemetry.py" \
    || fail "anti-rollback toujours clé sur active_name"
grep -q 'def _stge_lisible' "$REPO/src/pi/tic-reader/main_uart.py" \
    || fail "STGE absent du lecteur filaire"
grep -q '^TOPIC_TX_ACK = "ben/lora/tx/ack"' "$REPO/src/pi/ben-radio/ben_radio.py" \
    || fail "topic d'issue TX absent de la façade radio"

# ── Redémarrages ──────────────────────────────────────────────────────────────────────────────
# Les lecteurs portent (1) et (2) — décodage TLV et anti-rollback vivent entièrement là.
# `ben-radio` porte (3) : SANS ce restart, le topic d'issue n'apparaîtrait qu'au prochain
# redémarrage FORTUIT de la façade, donc à une date inconnue — exactement le genre de « déployé
# mais pas actif » qui fait croire à un correctif en place.
# `ben-local-api` n'est PAS concerné : STGE n'est ni stocké ni exposé, l'anti-rollback vit dans
# le lecteur, et le canal descendant ne passe pas par l'API. Rien ne change côté API.
# `is-active` filtre naturellement : un boîtier sans façade radio (filaire pur) saute ben-radio.
restarted=0
for svc in ben-radio ben-telemetry ben-tic-reader; do
    if systemctl is-active "$svc.service" >/dev/null 2>&1; then
        log "restart $svc"
        sudo systemctl restart "$svc.service" || fail "restart $svc"
        restarted=1
    fi
done
[ "$restarted" = 1 ] || log "aucun service actif — effectif au prochain démarrage"
sleep 5

# ── Contrôle d'effet : plus aucune clé "null" dans l'état persisté ─────────────────────────────
STATE=/var/lib/ben-firmware/lora-state.json
if [ -f "$STATE" ] && grep -q '"null"' "$STATE"; then
    log "⚠ clés \"null\" encore présentes — la purge s'applique au prochain chargement d'état"
fi

log "✓ STGE nommé et interprété : couleur du jour ET du lendemain lisibles au journal"
log "  à surveiller : journalctl -u ben-telemetry -f | grep -i stge"
log "✓ anti-rollback comparé PAR REGISTRE, et la clé survit au JSON"
log "✓ issue des commandes descendantes publiée sur ben/lora/tx/ack (ack + rtt_ms)"
log "  à surveiller : mosquitto_sub -t 'ben/lora/tx/ack' -v"
log "✓ update OK"
