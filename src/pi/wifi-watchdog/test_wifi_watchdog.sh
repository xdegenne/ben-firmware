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

cas() {  # cas <nom> <provisionné 0|1> <session BLE 0|1> <ping 0|1> <attendu restart 0|1> <pourquoi>
    local nom="$1" prov="$2" ble="$3" ping_ok="$4" attendu="$5" pourquoi="$6"
    local T; T="$(mktemp -d)"
    mkdir -p "$T/bin"

    # ── les faux ──────────────────────────────────────────────────────────────────────────────
    if [ "$prov" = 1 ]; then
        printf '#!/bin/sh\necho ben-provisioned\n' > "$T/bin/nmcli"
    else
        printf '#!/bin/sh\necho une-autre-connexion\n' > "$T/bin/nmcli"
    fi
    printf '#!/bin/sh\nexit %d\n' "$([ "$ping_ok" = 1 ] && echo 0 || echo 1)" > "$T/bin/ping"
    # ⭐ `systemctl` TRACE au lieu d'agir : c'est la trace qui est l'oracle du banc.
    printf '#!/bin/sh\necho "$@" >> %s/systemctl.trace\n' "$T" > "$T/bin/systemctl"
    printf '#!/bin/sh\necho "$@" >> %s/logger.trace\n' "$T" > "$T/bin/logger"
    printf '#!/bin/sh\nexit 0\n' > "$T/bin/sleep"
    chmod +x "$T/bin/"*

    local flag="$T/pas-de-session-ble"
    [ "$ble" = 1 ] && { flag="$T/ble-connected"; : > "$flag"; }

    PATH="$T/bin:$PATH" BEN_BLE_FLAG="$flag" BEN_NM_RESTARTS="$T/nm-restarts" \
        bash "$SCRIPT" >/dev/null 2>&1
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

# ── LES DEUX GARDES ───────────────────────────────────────────────────────────────────────────
cas "pas_deballe__ne_touche_a_rien"            0 0 0 0 \
    "un boîtier neuf n'a PAS de réseau par construction : agir le ferait boucler"
cas "pas_deballe__meme_si_le_ping_passe"       0 0 1 0 \
    "la garde ① ne dépend pas du ping : on sort AVANT de tester"
cas "session_BLE__ne_touche_pas_a_la_radio"    1 1 0 0 \
    "la radio est PARTAGÉE WiFi/BLE sur Pi Zero W — redémarrer NM casserait le déballage"

# ── LE TÉMOIN ET SON CONTRE-TÉMOIN ────────────────────────────────────────────────────────────
cas "deballe_sans_reseau__REDEMARRE"           1 0 0 1 \
    "⚖️ LE TÉMOIN : c'est le cas de ben-0005, et sans lui le banc passerait sur un script inerte"
cas "deballe_avec_reseau__ne_redemarre_PAS"    1 0 1 0 \
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
PATH="$T/bin:$PATH" BEN_BLE_FLAG="$T/absent" BEN_NM_RESTARTS="$T/nm-restarts" \
    bash "$SCRIPT" >/dev/null 2>&1
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
PATH="$T/bin:$PATH" BEN_BLE_FLAG="$T/absent" BEN_NM_RESTARTS="$T/nm-restarts" \
    bash "$SCRIPT" >/dev/null 2>&1
code=$?; n=$(cat "$T/nm-restarts" | tr -dc '0-9')
if [ "$code" = 0 ] && [ "$n" = 1 ]; then
    echo "  ok   un_compteur_VIDE_vaut_zero_et_ne_fait_pas_echouer"
else
    printf "  KO   un_compteur_VIDE_vaut_zero_et_ne_fait_pas_echouer  sortie=%s compteur=%s\n" "$code" "$n"
    KO=$((KO+1))
fi
rm -rf "$T"

echo
if [ "$KO" = 0 ]; then echo "7/7"; else echo "$((7-KO))/7"; fi
exit $([ "$KO" = 0 ] && echo 0 || echo 1)
