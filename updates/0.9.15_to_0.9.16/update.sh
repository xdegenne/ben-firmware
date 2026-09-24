#!/usr/bin/env bash
# update.sh — → pi-0.9.16 : le boîtier entretient son propre certificat.
#
# ═══ CE QUE LIVRE CETTE VERSION ══════════════════════════════════════════════════════════════
#
#   1. `ben_certd.py` — l'agent. Pointe une fois par jour (gigue pleine), obéit à une
#      directive, et ne se souvient de rien : l'état vit côté serveur.
#   2. `ben-certd.service` — l'unité systemd, sans [Install]/WantedBy.
#   3. `/etc/sudoers.d/ben-certd` — UN verbe, UNE unité : `systemctl restart ben-publisher`.
#   4. `check_network.py` démarre `ben-certd` à côté du publisher (déjà sur disque après le
#      checkout — rien à installer).
#
#   Côté boîtier → docs/pki-renouvellement.md
#
# ═══ POURQUOI CETTE VERSION EXISTE ═══════════════════════════════════════════════════════════
#
#   Mesuré le 2026-09-23 sur un iPhone 11 / iOS 26.5, trois certificats portant LA MÊME clé
#   privée, seul le certificat changeant :
#
#       CN seul, zéro extension, 10 ans   (le parc)     Android ✓   iOS ✗
#       CN + subjectAltName,     10 ans                 Android ✓   iOS ✗
#       CN + subjectAltName,    397 jours                           iOS ✓
#
#   ⭐ Une seule variable : la DURÉE. Apple plafonne les certificats SERVEUR à ~398 jours,
#      MÊME avec une CA privée fournie par l'application. Les certificats du parc (3650 jours)
#      sont donc inutilisables pour le futur HTTPS local, et il faut pouvoir les remplacer
#      — à répétition, tous les six mois, sans se déplacer. D'où cet agent.
#
# ═══ 🚨 LE RISQUE PROPRE À CETTE UPDATE : LE SUDOERS ═════════════════════════════════════════
#
#   Un fichier malformé dans /etc/sudoers.d/ casse `sudo` POUR TOUT LE MONDE, sur un boîtier
#   qu'on ne peut pas dépanner à distance. C'est le seul geste de ce script qui peut rendre
#   une machine inexploitable.
#
#   ⇒ On valide la SOURCE avec `visudo -cf` AVANT de l'installer, et on échoue si elle ne
#     passe pas. Vérifier après l'installation serait vérifier depuis l'intérieur du trou.
#
# ═══ CE QUE CE SCRIPT NE FAIT PAS ÉCHOUER ════════════════════════════════════════════════════
#
#   🚨 Un boîtier sans certificat, ou dont le cloud est injoignable, ne fait PAS échouer cette
#      update. `ben-certd` a `Restart=always`, il n'y a JAMAIS urgence à renouveler (l'ancien
#      certificat reste valide jusqu'à son échéance) et un échec le laisse exactement dans
#      l'état où il était.
#
#      Faire échouer l'update dans ce cas la ferait REJOUER à chaque tick de 10 minutes, pour
#      toujours. C'est la mécanique qui a brûlé pi-0.9.12. Un contrôle d'effet ne vérifie QUE
#      ce dont l'update est responsable, jamais ce qui dépend d'un état extérieur.
#
# AUCUNE migration, aucune table, aucune colonne — certd ne touche pas à la base.
# UNIVERSEL (LoRa et filaire) : entretenir son certificat n'est pas une propriété du matériel.
# Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.16"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
CERTS="/etc/ben-firmware/certs"
UNIT="/etc/systemd/system/ben-certd.service"
SUDOERS="/etc/sudoers.d/ben-certd"

SRC_AGENT="$REPO/src/pi/certd/ben_certd.py"
SRC_UNIT="$REPO/config/systemd/ben-certd.service"
SRC_SUDO="$REPO/config/etc/sudoers.d/ben-certd"
SRC_CHECK="$REPO/src/pi/provisioner/check_network.py"

# ── Préflight : le checkout doit être complet ────────────────────────────────────────────────
for f in "$SRC_AGENT" "$SRC_UNIT" "$SRC_SUDO" "$SRC_CHECK"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.9.16 incomplet ?)"
done

# ⚠️ SURTOUT PAS `python3 -m py_compile` : il ÉCRIT dans __pycache__, qui appartient à root sur
# les boîtiers du parc alors que l'OTA tourne en `ben` → Permission denied, update avortée, tag
# brûlé pour un défaut de droits. `ast.parse` ne touche pas au disque.
#
# ⭐ On contrôle AUSSI check_network.py : c'est lui qui décide, à chaque démarrage, qui prend la
#    main. Une erreur de syntaxe dedans ne casse pas cette update — elle casse le PROCHAIN BOOT,
#    silencieusement, et laisse un boîtier sans aucun lecteur. Il coûte une ligne de le lire ici.
python3 - "$SRC_AGENT" "$SRC_CHECK" <<'PYEOF' || fail "un fichier Python livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a)
    ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# check_network.py DOIT démarrer certd — sinon l'agent ne repartirait pas au prochain boot et
# l'update n'aurait un effet que jusqu'au prochain redémarrage. Un défaut invisible.
grep -q "ben-certd.service" "$SRC_CHECK" \
    || fail "check_network.py ne démarre pas ben-certd — le checkout n'est pas celui attendu"

# 🚨 LE contrôle qui évite de casser sudo. Sur la SOURCE, AVANT toute installation.
sudo visudo -cf "$SRC_SUDO" >/dev/null \
    || fail "le sudoers livré ne passe PAS visudo — on n'installe rien"
log "préflight OK (agent et check_network compilent, sudoers valide, certd bien câblé au boot)"

# ── Le sudoers ───────────────────────────────────────────────────────────────────────────────
# Mode 0440 root:root : sudo REFUSE de lire un fichier de /etc/sudoers.d qui serait accessible
# en écriture à autrui, et il le refuse en silence.
sudo install -o root -g root -m 440 "$SRC_SUDO" "$SUDOERS"
# Et on revalide l'ensemble du jeu de règles, pas seulement notre fichier : c'est la
# configuration COMPLÈTE que sudo relira au prochain appel.
sudo visudo -c >/dev/null || fail "la configuration sudo globale est invalide après installation"
log "✓ sudoers installé et configuration globale revalidée"

# ── L'unité systemd ──────────────────────────────────────────────────────────────────────────
# Pas d'[Install]/WantedBy : c'est `ben-network-check` qui la lancera au démarrage, dans la même
# branche que les lecteurs (provisionné + réseau présent). Hors ligne, certd n'a personne à qui
# parler et son certificat actuel reste valide.
sudo install -o root -g root -m 644 "$SRC_UNIT" "$UNIT"
sudo systemctl daemon-reload
log "✓ ben-certd.service installé"

# ── Démarrage, EN MEILLEUR EFFORT ────────────────────────────────────────────────────────────
if [ -f "$CERTS/device.crt" ] && [ -f "$CERTS/device.key" ]; then
    sudo systemctl restart ben-certd.service || warn "certd n'a pas démarré — il réessaiera"
    sleep 6
else
    warn "certificats de boîtier absents — certd non démarré (rien à entretenir)"
fi

# ── Contrôle d'effet ─────────────────────────────────────────────────────────────────────────
# On vérifie CE DONT L'UPDATE EST RESPONSABLE :
#   • le sudoers est en place, bien formé, et sudo relit une configuration valide ;
#   • l'unité est installée et connue de systemd ;
#   • l'agent est lisible par `ben`, l'utilisateur qui l'exécute ;
#   • la base reste lisible (l'API locale répond avec db:true).
#
# On ne vérifie PAS que certd a joint ben-api : ça dépend du réseau et du serveur, pas de nous.
# On ne vérifie PAS que le certificat a été renouvelé : rien n'est armé pour ce boîtier, et le
# renouvellement est un processus de plusieurs étapes qui s'étale sur des heures.

[ -f "$SUDOERS" ] || fail "$SUDOERS absent après installation"
sudo visudo -c >/dev/null || fail "configuration sudo invalide"

systemctl cat ben-certd.service >/dev/null 2>&1 \
    || fail "ben-certd.service n'est pas connu de systemd après daemon-reload"

# L'agent tourne en `ben` : s'il ne peut pas le lire, l'unité échouera en boucle.
sudo -u ben test -r "$SRC_AGENT" || fail "ben ne peut pas lire $SRC_AGENT"

# L'API locale ouvre la base en LECTURE SEULE ; si elle répond db:true, la base est intacte.
# Un simple code 200 masquerait une base illisible — c'est la leçon de pi-0.9.12, brûlée sur
# un contrôle qui interrogeait `/info`, une route qui n'existe pas.
if systemctl is-active --quiet ben-local-api.service; then
    python3 - <<'PYEOF' || fail "l'API locale ne répond plus sur /health"
import json, sys, urllib.request
try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:8087/health", timeout=15).read())
except Exception as e:
    print(f"  /health KO : {e}", file=sys.stderr); sys.exit(1)
if not d.get("db"):
    print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ API locale debout (/health répond, base lisible)"
fi

# Le publisher ne doit PAS avoir été perturbé : c'est lui qui porte les mesures.
if systemctl is-active --quiet ben-publisher.service; then
    log "✓ publisher toujours actif"
else
    warn "publisher inactif — vérifier, mais ce n'est pas cette update qui l'arrête"
fi

# Informatif seulement — jamais bloquant.
if systemctl is-active --quiet ben-certd.service; then
    log "✓ certd actif"
else
    warn "certd pas encore actif — normal sans réseau ou sans certificat"
fi

log "✓ le boîtier entretient désormais son certificat"
log "  à surveiller :"
log "    journalctl -u ben-certd -f"
log "    → « CSR déposé (202) — motif : … »  puis, une fois signé côté opérateur,"
log "      « certificat remplacé », « ben-publisher redémarré », « acquitté auprès de ben-api »"
log "✓ update OK"
