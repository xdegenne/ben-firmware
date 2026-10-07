#!/bin/bash
# wifi_watchdog.sh — le filet du réseau, sur un boîtier EN MARCHE.
#
# Lancé par `wifi-watchdog.timer`, toutes les 2 minutes.
#
# ═══ POURQUOI IL EXISTE ═══════════════════════════════════════════════════════════════════════
#
#   Rien ne surveillait le réseau d'un boîtier déjà démarré : `ben-network-check` est un ONESHOT du
#   boot, `ben-network-recovery` ne vit que le temps d'une fenêtre. ben-0005 a passé 8 h 40 sans
#   réseau le 2026-10-06 — il mesurait, il ne publiait plus, et l'OTA échouait pour la même cause.
#   Il a fallu aller le redémarrer à la main. Chantier `ben-docs#17`, sous-tâche #46.
#
# ⚠️ CE SCRIPT EXISTAIT DÉJÀ ET N'ÉTAIT INSTALLÉ NULLE PART. `install.sh` copie les unités en bloc
#    (`cp config/systemd/*`) mais la liste des `enable` est explicite, un par un, et le watchdog n'y
#    était pas — d'où une unité `loaded` sur les 8 boîtiers et un timer `disabled` sur les 8. Les
#    deux faits avaient l'air de se contredire ; ils venaient d'une ligne manquante.
#
# 🚨 ET IL NE DOIT RIEN FAIRE PENDANT LE DÉBALLAGE. Il y a un moment où l'absence de réseau est
#    VOULUE : la fenêtre BLE. Redémarrer NetworkManager toutes les 2 minutes pendant que le
#    téléphone écrit les identifiants toucherait à la radio — et sur un Pi Zero W **la radio est
#    partagée** entre WiFi et BLE, ce qui a déjà coûté un décrochage en déballage Android.
#
# ⭐ D'OÙ LES DEUX GARDES CI-DESSOUS, ET LE FAIT QU'ELLES SOIENT DANS LE SCRIPT. On pourrait
#    n'activer le timer qu'à la fin du provisioning : ce serait un geste de plus à ne pas oublier,
#    et les 8 boîtiers déjà déballés n'y passeraient jamais. Ici le « quand » est une PROPRIÉTÉ du
#    script, relue à chaque tour — même doctrine que la déclaration de version de pi-0.10.0 : une
#    condition se re-vérifie, un geste s'oublie.

# ── GARDE ① : le boîtier est-il déballé ? ─────────────────────────────────────────────────────
#
# `ben-provisioned` est le nom de connexion que `check_network._has_been_provisioned()` interroge
# déjà — on ne réinvente pas le critère, on emploie le même.
# ⓘ Sortie 0 et non 1 : ne pas être déballé n'est pas une panne, et un `wifi-watchdog.service` en
#   échec toutes les 2 minutes polluerait le journal du boîtier neuf.
if ! nmcli -t -f NAME connection show 2>/dev/null | grep -qx ben-provisioned; then
    exit 0
fi

# ── GARDE ② : une session BLE est-elle en cours ? ─────────────────────────────────────────────
#
# Le drapeau de `provisioning_state.ble_session_active()`. Il peut y avoir une session BLE sur un
# boîtier DÉJÀ déballé — reconfiguration du WiFi, remise d'un ticket — donc la garde ① ne suffit
# pas.
# ⓘ Surchargeable pour que la garde soit ÉPROUVABLE — un banc ne peut pas écrire dans /run/ben.
#   Même forme que les `BEN_*` du publisher : un défaut qui est la valeur de production.
BLE_FLAG="${BEN_BLE_FLAG:-/run/ben/ble-central-connected}"
if [ -f "$BLE_FLAG" ]; then
    logger -t wifi_watchdog "session BLE en cours — on ne touche pas à la radio"
    exit 0
fi

# ── LE TEST, ET CE QU'IL NE COUVRE PAS ───────────────────────────────────────────────────────
#
# `1.1.1.1` par ADRESSE, donc sans résolution de noms. C'est volontaire et c'est une limite connue :
# les deux chemins qui comptent passent par un NOM (`api.benpilote.fr` pour publier, `github.com`
# pour l'OTA), donc un boîtier qui route mais ne résout pas PASSERAIT ce test.
#
# ⚠️ Ce cas est PLAUSIBLE ET NON OBSERVÉ. Celui qu'on a vécu, lui, est franc : la Freebox ne voyait
#    AUCUN équipement, le boîtier n'était pas associé, donc ce ping aurait échoué. Tester aussi la
#    résolution demande d'abord de trancher une question ouverte — les boîtiers du tailnet résolvent
#    par le MagicDNS de Tailscale (100.100.100.100), les autres par leur box, donc un test de
#    résolution testerait deux choses différentes selon le boîtier. ⇒ `ben-docs#17`.
if ! ping -c1 -W3 1.1.1.1 &>/dev/null; then
    logger -t wifi_watchdog "pas de connectivité IP — redémarrage de NetworkManager"

    # 🚨 ON COMPTE, ET C'EST AUSSI IMPORTANT QUE LE REDÉMARRAGE LUI-MÊME. Un filet qui rattrape
    #    en silence rend un boîtier malade INDISCERNABLE d'un boîtier sain : celui qui perd sa
    #    radio toutes les deux heures et qu'on relance chaque fois publie normalement, et personne
    #    ne sait. C'est le piège que `ben-docs#17` nomme — le watchdog masque le symptôme, il
    #    n'explique rien. Ce compteur est ce qui l'empêche de le masquer POUR DE BON.
    #
    # ⚠️ ET AUCUN CHAMP SYSTEMD NE LE DONNE. `NRestarts` compte les redémarrages AUTOMATIQUES de
    #    systemd (politique `Restart=`), pas un `systemctl restart` lancé par un script. Et
    #    `health.errors()` ne lit que la priorité 3 (`err`), donc une ligne `logger` en `notice` ne
    #    remonterait pas. Sans ce fichier, le cloud ne verrait rien.
    #
    # ⓘ Dans `/var/lib`, PAS `/run` : il doit survivre aux redémarrages, sinon un boîtier qui
    #   reboote efface la preuve — et un boîtier malade reboote.
    COMPTEUR="${BEN_NM_RESTARTS:-/var/lib/ben-firmware/nm-restarts}"
    mkdir -p "$(dirname "$COMPTEUR")" 2>/dev/null
    N=$(( $(cat "$COMPTEUR" 2>/dev/null | head -1 | tr -dc '0-9') + 1 ))
    # ⭐ Écriture ATOMIQUE : une coupure de courant pendant un `>` laisserait un fichier vide, et
    #    un fichier vide se relit comme « zéro redémarrage » — donc comme un boîtier sain.
    printf '%d\n' "$N" > "$COMPTEUR.tmp" && mv -f "$COMPTEUR.tmp" "$COMPTEUR"

    systemctl restart NetworkManager
    sleep 10
fi
