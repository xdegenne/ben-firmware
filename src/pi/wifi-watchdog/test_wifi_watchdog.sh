#!/usr/bin/env bash
# Banc du watchdog réseau — les QUATRE combinaisons de ses deux gardes.
#
# 🚨 CE QUI SE JOUE ICI N'EST PAS LE CHEMIN HEUREUX, c'est le déballage. Un watchdog qui redémarre
#    NetworkManager pendant la fenêtre BLE touche à une radio PARTAGÉE entre WiFi et BLE sur Pi
#    Zero W — ce qui a déjà coûté un décrochage en déballage Android. Les deux gardes existent pour
#    ça, et sans banc on ne saurait pas qu'elles tiennent.
#
# ⚖️ Et le CONTRE-TÉMOIN est la moitié du banc : un watchdog qui ne redémarrerait JAMAIS passerait
#    les trois cas de refus. C'est le quatrième qui le fait tomber.
#
# Les commandes extérieures sont remplacées par des faux dans un PATH temporaire : le banc tourne
# donc sur un Mac comme sur un boîtier, sans NetworkManager.
#
#     bash src/pi/wifi-watchdog/test_wifi_watchdog.sh
set -uo pipefail
ICI="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$ICI/wifi_watchdog.sh"
KO=0

# cas <nom> <nmcli: oui|non|mort> <fichier conn 0|1> <ble: aucun|frais|perime> <ping 0|1> \
#     <attendu restart 0|1> <pourquoi>
cas() {
    local nom="$1" nm="$2" fichier="$3" ble="$4" ping_ok="$5" attendu="$6" pourquoi="$7"
    local T; T="$(mktemp -d)"
    mkdir -p "$T/bin" "$T/conn"

    # ── les faux ──────────────────────────────────────────────────────────────────────────────
    case "$nm" in
        # 🚨 `mort` = NetworkManager en panne : nmcli écrit sur stderr et SORT EN ERREUR. C'est le
        #    cas qui annulait tout le watchdog, et il doit le faire AGIR.
        mort) printf '#!/bin/sh\necho "Error: NetworkManager is not running." >&2\nexit 8\n' \
                  > "$T/bin/nmcli" ;;
        oui)  printf '#!/bin/sh\necho ben-provisioned\n'      > "$T/bin/nmcli" ;;
        non)  printf '#!/bin/sh\necho une-autre-connexion\n'  > "$T/bin/nmcli" ;;
    esac
    # ⭐ Sous son nom TEMPORAIRE, comme sur ben-0001 — c'est le CONTENU qui porte l'identité.
    if [ "$fichier" = 1 ]; then
        printf '[connection]\nid=ben-provisioned\n' \
            > "$T/conn/ben-provisioned-tmp-1783764212.nmconnection"
    elif [ "$fichier" = leurre ]; then
        # ⚖️ Un keyfile dont le NOM commence par `ben-provisioned` mais qui pointe AILLEURS : un
        #    glob sur le nom l'accepterait à tort.
        printf '[connection]\nid=ben-provisioned-autre-chose\n' \
            > "$T/conn/ben-provisioned-autre.nmconnection"
    fi
    printf '#!/bin/sh\nexit %d\n' "$([ "$ping_ok" = 1 ] && echo 0 || echo 1)" > "$T/bin/ping"
    # ⭐ `systemctl` TRACE au lieu d'agir : c'est la trace qui est l'oracle du banc.
    printf '#!/bin/sh\necho "$@" >> %s/systemctl.trace\n' "$T" > "$T/bin/systemctl"
    printf '#!/bin/sh\necho "$@" >> %s/logger.trace\n' "$T" > "$T/bin/logger"
    printf '#!/bin/sh\nexit 0\n' > "$T/bin/sleep"
    chmod +x "$T/bin/"*

    local flag="$T/pas-de-session-ble"
    case "$ble" in
        frais)  flag="$T/ble-connected"; : > "$flag" ;;
        # ⭐ Un drapeau de 2 h : le provisioner n'a pas de gestionnaire SIGTERM, donc le fichier
        #    survit à son arrêt. Sans péremption, il bloquait le watchdog JUSQU'AU REDÉMARRAGE.
        perime) flag="$T/ble-connected"; : > "$flag"
                touch -t "$(date -v-2H +%Y%m%d%H%M 2>/dev/null \
                          || date -d '-2 hours' +%Y%m%d%H%M)" "$flag" ;;
    esac

    PATH="$T/bin:$PATH" BEN_BLE_FLAG="$flag" BEN_NM_RESTARTS="$T/nm-restarts" \
        BEN_NM_CONN_DIR="$T/conn" bash "$SCRIPT" >/dev/null 2>&1
    local code=$?

    local restart=0
    grep -q "restart NetworkManager" "$T/systemctl.trace" 2>/dev/null && restart=1

    # 🚨 LE COMPTEUR DOIT SUIVRE LE REDÉMARRAGE, exactement : sans lui, un boîtier malade qu'on
    #    rattrape toutes les deux heures est indiscernable d'un boîtier sain.
    local compte; compte=$(cat "$T/nm-restarts" 2>/dev/null | tr -dc '0-9')
    compte=${compte:-0}
    if [ "$compte" != "$attendu" ]; then
        printf "  KO   %-46s compteur=%s, attendu %s — le compteur doit suivre le redémarrage\n" \
            "$nom" "$compte" "$attendu"
        KO=$((KO+1)); rm -rf "$T"; return
    fi

    if [ "$restart" != "$attendu" ]; then
        printf "  KO   %-46s restart=%s, attendu %s — %s\n" "$nom" "$restart" "$attendu" "$pourquoi"
        KO=$((KO+1))
    elif [ "$code" != 0 ]; then
        printf "  KO   %-46s sortie %s — un watchdog ne doit jamais échouer\n" "$nom" "$code"
        KO=$((KO+1))
    else
        printf "  ok   %s\n" "$nom"
    fi
    rm -rf "$T"
}

# ── GARDE ① : LE BOÎTIER EST-IL DÉBALLÉ ? ─────────────────────────────────────────────────────
cas "neuf__ne_touche_a_rien"                     non 0 aucun 0 0 \
    "un boîtier neuf n'a PAS de réseau par construction : agir le ferait boucler"
cas "neuf__meme_si_le_ping_passe"                non 0 aucun 1 0 \
    "la garde ① ne dépend pas du ping : on sort AVANT de tester"
cas "deballe_par_le_FICHIER_sous_son_nom_TEMPORAIRE" non 1 aucun 0 1 \
    "⭐ relevé sur ben-0001 : le keyfile s'appelle ben-provisioned-tmp-<ts>.nmconnection et porte
     id=ben-provisioned. NM ne renomme pas le fichier, donc c'est le CONTENU qui fait foi"
cas "un_keyfile_LEURRE_ne_compte_PAS"            non leurre aucun 0 0 \
    "⚖️ un fichier nommé ben-provisioned-autre mais pointant AILLEURS : un glob sur le NOM
     l'accepterait à tort, le test sur id= le refuse"

# 🚨 LE CAS QUI ANNULAIT TOUT LE WATCHDOG
cas "NM_MORT__AGIT_QUAND_MEME"                   mort 0 aucun 0 1 \
    "🚨 nmcli en erreur ⇒ NM est en panne ⇒ c'est EXACTEMENT le moment d'agir. Conclure « pas
     déballé » reproduisait la coupure de 8 h 40 que ce script existe pour éviter"
cas "NM_mort_avec_le_fichier__AGIT"              mort 1 aucun 0 1 \
    "le fichier tranche d'abord, et nmcli n'est même pas interrogé"

# ── GARDE ② : LA SESSION BLE, ET SA PÉREMPTION ────────────────────────────────────────────────
cas "session_BLE_FRAICHE__ne_touche_pas_radio"   oui 1 frais 0 0 \
    "la radio est PARTAGÉE WiFi/BLE sur Pi Zero W — redémarrer NM casserait le déballage"
cas "drapeau_BLE_PERIME__agit_quand_meme"        oui 1 perime 0 1 \
    "⚠️ le provisioner n'a pas de SIGTERM : le drapeau survit à son arrêt. Sans péremption il
     bloquait le watchdog JUSQU'AU REDÉMARRAGE — le cas même qu'il doit couvrir"

# ── LE TÉMOIN ET SON CONTRE-TÉMOIN ────────────────────────────────────────────────────────────
cas "deballe_sans_reseau__REDEMARRE"             oui 1 aucun 0 1 \
    "⚖️ LE TÉMOIN : c'est le cas de ben-0005, et sans lui le banc passerait sur un script inerte"
cas "deballe_avec_reseau__ne_redemarre_PAS"      oui 1 aucun 1 0 \
    "⚖️ LE CONTRE-TÉMOIN : sinon on relance NM toutes les 2 min sur les 8 boîtiers, pour rien"

# ── LE COMPTEUR S'INCRÉMENTE, IL NE SE RÉÉCRIT PAS ───────────────────────────────────────────
T="$(mktemp -d)"; mkdir -p "$T/bin"
printf '#!/bin/sh\necho ben-provisioned\n' > "$T/bin/nmcli"
printf '#!/bin/sh\nexit 1\n'               > "$T/bin/ping"
printf '#!/bin/sh\nexit 0\n'               > "$T/bin/systemctl"
printf '#!/bin/sh\nexit 0\n'               > "$T/bin/logger"
printf '#!/bin/sh\nexit 0\n'               > "$T/bin/sleep"
chmod +x "$T/bin/"*
echo 7 > "$T/nm-restarts"          # ⭐ un boîtier qui a DÉJÀ été rattrapé 7 fois
mkdir -p "$T/conn"; printf '[connection]\nid=ben-provisioned\n' > "$T/conn/c.nmconnection"
PATH="$T/bin:$PATH" BEN_BLE_FLAG="$T/absent" BEN_NM_RESTARTS="$T/nm-restarts" \
    BEN_NM_CONN_DIR="$T/conn" bash "$SCRIPT" >/dev/null 2>&1
n=$(cat "$T/nm-restarts" | tr -dc '0-9')
if [ "$n" = 8 ]; then
    echo "  ok   le_compteur_s_INCREMENTE_et_ne_se_reecrit_pas"
else
    printf "  KO   le_compteur_s_INCREMENTE_et_ne_se_reecrit_pas  7 → %s, attendu 8 — remettre à 1 effacerait l'historique d'un boîtier malade\n" "$n"
    KO=$((KO+1))
fi
rm -rf "$T"

# ⚖️ ET UN FICHIER VIDE VAUT ZÉRO, PAS UNE ERREUR : une coupure pendant l'écriture laisserait un
#    fichier vide, et un `$(( + 1 ))` sur du vide planterait le watchdog.
T="$(mktemp -d)"; mkdir -p "$T/bin"
printf '#!/bin/sh\necho ben-provisioned\n' > "$T/bin/nmcli"
printf '#!/bin/sh\nexit 1\n' > "$T/bin/ping"
for f in systemctl logger sleep; do printf '#!/bin/sh\nexit 0\n' > "$T/bin/$f"; done
chmod +x "$T/bin/"*
: > "$T/nm-restarts"               # fichier VIDE, comme après une coupure
mkdir -p "$T/conn"; printf '[connection]\nid=ben-provisioned\n' > "$T/conn/c.nmconnection"
PATH="$T/bin:$PATH" BEN_BLE_FLAG="$T/absent" BEN_NM_RESTARTS="$T/nm-restarts" \
    BEN_NM_CONN_DIR="$T/conn" bash "$SCRIPT" >/dev/null 2>&1
code=$?; n=$(cat "$T/nm-restarts" | tr -dc '0-9')
if [ "$code" = 0 ] && [ "$n" = 1 ]; then
    echo "  ok   un_compteur_VIDE_vaut_zero_et_ne_fait_pas_echouer"
else
    printf "  KO   un_compteur_VIDE_vaut_zero_et_ne_fait_pas_echouer  sortie=%s compteur=%s\n" "$code" "$n"
    KO=$((KO+1))
fi
rm -rf "$T"

echo
if [ "$KO" = 0 ]; then echo "12/12"; else echo "$((12-KO))/12"; fi
exit $([ "$KO" = 0 ] && echo 0 || echo 1)
