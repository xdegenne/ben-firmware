#!/usr/bin/env bash
# Banc du watchdog réseau — ses TROIS gardes, dans tous leurs états.
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

# cas <nom> <nmcli: oui|non|mort> <fichier conn 0|1|leurre> <radio> <ping 0|1> \
#     <attendu restart 0|1> <pourquoi>
#
# <radio> = QUI TIENT LA RADIO — c'est la même question pour les gardes ② et ③ :
#   aucun · frais (téléphone connecté) · perime (drapeau abandonné)
#   provisioner · recovery · recovery_qui_demarre (unité `activating`)
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
    # ⭐ `systemctl` TRACE au lieu d'agir — SAUF `is-active`, que la garde ③ interroge : le faux
    #    doit lui répondre. `BEN_TEST_ACTIFS` est lu par le FAUX (il hérite de l'environnement),
    #    jamais par le script éprouvé : celui-ci ne connaît que la commande `systemctl`.
    cat > "$T/bin/systemctl" <<'FAUX'
#!/bin/sh
# 🚨 CE FAUX DOIT ÊTRE AUSSI SÉVÈRE QUE LE VRAI, et il ne l'était pas : il sortait en 0 pour
#    N'IMPORTE QUEL état, donc une mutation qui testait le CODE de `is-active --quiet` au lieu du
#    MOT restait VERTE. Le vrai `systemctl` sort en 0 pour `active` SEULEMENT — `activating` sort
#    en 3 — et `--quiet` n'affiche RIEN. Un faux plus permissif que la réalité, c'est un banc qui
#    valide le défaut.
if [ "$1" = "is-active" ]; then
    shift
    q=0; [ "$1" = "--quiet" ] && { q=1; shift; }
    etat=inactive
    for u in $BEN_TEST_ACTIFS; do [ "$u" = "$1" ] && etat="$BEN_TEST_ETAT"; done
    [ "$q" = 1 ] || echo "$etat"
    [ "$etat" = active ] && exit 0
    exit 3
fi
echo "$@" >> "$BEN_TEST_TRACE"
FAUX
    printf '#!/bin/sh\necho "$@" >> %s/logger.trace\n' "$T" > "$T/bin/logger"
    printf '#!/bin/sh\nexit 0\n' > "$T/bin/sleep"
    chmod +x "$T/bin/"*

    local flag="$T/pas-de-session-ble" actifs="" etat=active
    case "$ble" in
        # ⭐ GARDE ③ — une UNITÉ active, et non un drapeau. Le drapeau ne dit que « téléphone
        #    CONNECTÉ » ; pendant les 300 s où `ben-network-recovery` ne fait qu'OFFRIR le BLE,
        #    personne n'est connecté et il n'y a AUCUN drapeau.
        provisioner) actifs="ben-ble-provisioner.service" ;;
        recovery)    actifs="ben-network-recovery.service" ;;
        # ⚖️ Une unité qui DÉMARRE tient déjà la radio : `is-active` rend `activating`, sur quoi
        #    `is-active --quiet` sortirait en ERREUR — d'où un test sur le MOT, pas sur le code.
        recovery_qui_demarre) actifs="ben-network-recovery.service"; etat=activating ;;
        frais)  flag="$T/ble-connected"; : > "$flag" ;;
        # ⭐ Un drapeau de 2 h : le provisioner n'a pas de gestionnaire SIGTERM, donc le fichier
        #    survit à son arrêt. Sans péremption, il bloquait le watchdog JUSQU'AU REDÉMARRAGE.
        perime) flag="$T/ble-connected"; : > "$flag"
                touch -t "$(date -v-2H +%Y%m%d%H%M 2>/dev/null \
                          || date -d '-2 hours' +%Y%m%d%H%M)" "$flag" ;;
    esac

    PATH="$T/bin:$PATH" BEN_BLE_FLAG="$flag" BEN_NM_RESTARTS="$T/nm-restarts" \
        BEN_NM_CONN_DIR="$T/conn" BEN_TEST_ACTIFS="$actifs" BEN_TEST_ETAT="$etat" \
        BEN_TEST_TRACE="$T/systemctl.trace" bash "$SCRIPT" >/dev/null 2>&1
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

# ── GARDE ③ : UN AGENT DE PROVISIONING TIENT-IL LA RADIO ? ────────────────────────────────────
#
# 🚨 LE CAS QUE LE DRAPEAU NE VOYAIT PAS, et c'est le plus exposé des deux : coupure de courant, le
#    boîtier revient AVANT la box. Il est DÉJÀ déballé (la garde ① le laisse donc passer), personne
#    n'est connecté en BLE (donc aucun drapeau), et `ben-network-recovery` offre pourtant le BLE
#    pendant 300 s en se contentant de PINGUER — exprès, pour ne pas toucher à la radio partagée.
#    Le watchdog part 60 s après le boot puis toutes les 2 min : il tirerait DEUX OU TROIS FOIS
#    dans cette fenêtre, chaque fois avec un scan WiFi complet.
cas "fenetre_RECOVERY_sans_telephone__ne_touche_pas_radio" oui 1 recovery 0 0 \
    "network_recovery offre le BLE 300 s sans qu'aucun téléphone soit connecté : pas de drapeau,
     et la radio est prise quand même"
cas "RE-provisioning_entre_deux_reconnexions__s_abstient"  oui 1 provisioner 0 0 \
    "le provisioner EFFACE le drapeau à chaque démarrage (os._exit(1) + Restart=on-failure) :
     l'unité active est le seul témoin qui tienne dans cet intervalle"
cas "unite_qui_DEMARRE_compte_comme_prise"                 oui 1 recovery_qui_demarre 0 0 \
    "is-active rend « activating » : une unité qui démarre tient déjà la radio"

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
printf '#!/bin/sh\n[ "$1" = is-active ] && { echo inactive; exit 3; }\nexit 0\n' > "$T/bin/systemctl"
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
for f in logger sleep; do printf '#!/bin/sh\nexit 0\n' > "$T/bin/$f"; done
printf '#!/bin/sh\n[ "$1" = is-active ] && { echo inactive; exit 3; }\nexit 0\n' > "$T/bin/systemctl"
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
if [ "$KO" = 0 ]; then echo "15/15"; else echo "$((15-KO))/15"; fi
exit $([ "$KO" = 0 ] && echo 0 || echo 1)
