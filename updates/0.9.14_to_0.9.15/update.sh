#!/usr/bin/env bash
# update.sh — → pi-0.9.15 : le boîtier pousse ses mesures vers le cloud.
#
# ═══ CE QUE LIVRE CETTE VERSION ══════════════════════════════════════════════════════════════
#
#   1. `ben_publisher.py` — le sender. Lit l'outbox `measurements.sent` (présente depuis le
#      jour 1, jamais écrite jusqu'ici), envoie par lots de 500 en mTLS, marque `sent=1`
#      seulement après un 2xx. Du plus ancien vers le plus récent, un seul curseur.
#   2. `ben-publisher.service` — l'unité systemd, qui n'était **installée nulle part**.
#   3. La **CA conforme**, sans laquelle aucun boîtier ne peut parler au serveur.
#
# ═══ 🚨 POURQUOI LA CA D'ABORD, ET PAS APRÈS ══════════════════════════════════════════════════
#
#   La CA du parc (1712 octets, juin) N'A AUCUNE EXTENSION X509v3. `openssl s_client` la
#   valide sans broncher ; Python 3.13, dont `create_default_context()` active
#   VERIFY_X509_STRICT par défaut, la REFUSE — avec un message trompeur :
#
#       CERTIFICATE_VERIFY_FAILED: Missing Authority Key Identifier
#
#   Le vrai motif ne sort qu'avec `openssl verify -x509_strict` : « error 79: invalid CA
#   certificate ». Constaté le 2026-09-19 sur ben-0001, au premier hello réel.
#
#   La CA a donc été RÉÉMISE le 19/09 — même clé, même sujet, extensions conformes
#   (basicConstraints critical CA:TRUE, keyUsage keyCertSign, subjectKeyIdentifier).
#   ⭐ La clé n'a PAS changé : les 11 certificats de boîtiers déjà émis restent valides, ce
#      qui a été vérifié un par un avant tout déploiement.
#
#   ⇒ Démarrer le publisher avant d'avoir posé la nouvelle CA, c'est garantir que son premier
#     hello échoue sur une erreur qui n'aide personne.
#
# ═══ CE QUE CE SCRIPT NE FAIT PAS ÉCHOUER ════════════════════════════════════════════════════
#
#   🚨 Un boîtier SANS certificat de boîtier, ou dont le cloud est injoignable, ne fait PAS
#      échouer cette update. Le publisher a `Restart=always` : il réessaiera tout seul, et les
#      mesures continuent de s'accumuler dans l'outbox — c'est exactement à ça qu'elle sert.
#
#      Faire échouer l'update dans ce cas la ferait REJOUER à chaque tick de 10 minutes, pour
#      toujours. C'est la mécanique qui a brûlé pi-0.9.12 (contrôle d'effet sur une route
#      inexistante → 208 redémarrages). Un contrôle d'effet ne vérifie QUE ce dont l'update est
#      responsable, jamais ce qui dépend d'un état extérieur.
#
# AUCUNE migration, aucune table, aucune colonne — le publisher ouvre la base en LECTURE SEULE.
# UNIVERSEL (LoRa et filaire). Code déjà sur disque après `git checkout pi-0.9.15`.
# Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.15"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
CERTS="/etc/ben-firmware/certs"
UNIT="/etc/systemd/system/ben-publisher.service"

SRC_PUB="$REPO/src/pi/publisher/ben_publisher.py"
SRC_UNIT="$REPO/config/systemd/ben-publisher.service"
SRC_CA="$REPO/config/etc/ca/root-ca.crt"

# ── Préflight : le checkout doit être complet ────────────────────────────────────────────────
for f in "$SRC_PUB" "$SRC_UNIT" "$SRC_CA"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.9.15 incomplet ?)"
done

# ⚠️ SURTOUT PAS `python3 -m py_compile` : il ÉCRIT dans __pycache__, qui appartient à root sur
# les boîtiers du parc alors que l'OTA tourne en `ben` → Permission denied, update avortée, tag
# brûlé pour un défaut de droits. `ast.parse` ne touche pas au disque.
python3 - "$SRC_PUB" <<'PYEOF' || fail "ben_publisher.py ne compile pas"
import ast, pathlib, sys
p = pathlib.Path(sys.argv[1])
ast.parse(p.read_text(encoding="utf-8"), filename="ben_publisher.py")
PYEOF

# La CA du dépôt DOIT être la conforme. Un grep sur la taille ne prouverait rien ; on vérifie
# l'extension qui manquait, et on exige que la CA se valide elle-même en mode STRICT — le mode
# exact qu'applique Python côté boîtier.
openssl x509 -in "$SRC_CA" -noout -text 2>/dev/null | grep -q "X509v3 Subject Key Identifier" \
    || fail "la CA du dépôt n'a pas de Subject Key Identifier — ce n'est pas la version conforme"
openssl verify -x509_strict -CAfile "$SRC_CA" "$SRC_CA" >/dev/null 2>&1 \
    || fail "la CA du dépôt ne passe pas openssl verify -x509_strict"
log "préflight OK (publisher compile, CA conforme et strictement valide)"

# ── 🚨 La CA, AVANT tout le reste ────────────────────────────────────────────────────────────
#
# ⭐ LE GARDE-FOU QUI COMPTE : la nouvelle CA doit valider LE certificat DE CE BOÎTIER. Poser
#    une CA qui ne correspond pas à son certificat le couperait définitivement du cloud, sans
#    aucun message côté boîtier. On vérifie AVANT de remplacer, pas après.
if [ -f "$CERTS/device.crt" ]; then
    openssl verify -CAfile "$SRC_CA" "$CERTS/device.crt" >/dev/null 2>&1 \
        || fail "la nouvelle CA ne valide PAS $CERTS/device.crt — on ne remplace rien"
    log "✓ la nouvelle CA valide bien le certificat de ce boîtier"
else
    warn "pas de device.crt — boîtier non provisionné pour le cloud, on pose quand même la CA"
fi

if [ -f "$CERTS/root-ca.crt" ] && cmp -s "$SRC_CA" "$CERTS/root-ca.crt"; then
    log "CA déjà à jour"
else
    sudo mkdir -p "$CERTS"
    # Sauvegarde horodatée : si quoi que ce soit tourne mal, l'ancienne est à côté.
    if [ -f "$CERTS/root-ca.crt" ]; then
        sudo cp -a "$CERTS/root-ca.crt" "$CERTS/root-ca.crt.bak-$(date +%Y%m%d%H%M%S)"
    fi
    sudo install -o ben -g ben -m 644 "$SRC_CA" "$CERTS/root-ca.crt"
    log "✓ CA conforme installée (ancienne sauvegardée à côté)"
fi

# ── L'unité systemd ──────────────────────────────────────────────────────────────────────────
# Elle n'était installée NULLE PART : `install.sh` ne la posait pas, et l'étape 10 de
# check_update.py (« redémarrer le publisher après chaque update ») ne faisait donc rien.
sudo install -o root -g root -m 644 "$SRC_UNIT" "$UNIT"
sudo systemctl daemon-reload
log "✓ ben-publisher.service installé"

# ── Démarrage, EN MEILLEUR EFFORT ────────────────────────────────────────────────────────────
# Pas d'[Install]/WantedBy dans l'unité : au prochain démarrage c'est `ben-network-check` qui
# la lancera, dans la même branche que les lecteurs (provisionné + réseau). Ici on la démarre
# tout de suite pour que l'update ait un effet immédiat.
if [ -f "$CERTS/device.crt" ] && [ -f "$CERTS/device.key" ]; then
    sudo systemctl restart ben-publisher.service || warn "le publisher n'a pas démarré — il réessaiera"
    sleep 8
else
    warn "certificats de boîtier absents — publisher non démarré (rien à envoyer au cloud)"
fi

# ── Contrôle d'effet ─────────────────────────────────────────────────────────────────────────
# ⚠️ Ce script a été EXÉCUTÉ TEL QUEL sur **ben-0003 le 2026-09-21 à 20:11**, avant signature
#    du tag — boîtier en 0.9.14, filaire, portant encore l'ANCIENNE CA (1712 o), donc exerçant
#    réellement le remplacement. Sortie : code 0, tous les contrôles verts, et effet constaté
#    côté serveur dans la seconde (`hello OK` puis `envoyé 500 · inséré 500`).
#
# On vérifie CE DONT L'UPDATE EST RESPONSABLE :
#   • la CA sur disque est bien la conforme, et elle valide le certificat du boîtier ;
#   • l'unité est installée et connue de systemd ;
#   • la base reste lisible (l'API locale répond avec db:true).
# On ne vérifie PAS que le publisher parle au cloud : ça dépend du réseau et du serveur, pas
# de nous. Voir l'encadré en tête de fichier.

cmp -s "$SRC_CA" "$CERTS/root-ca.crt" || fail "la CA installée ne correspond pas à celle du dépôt"
if [ -f "$CERTS/device.crt" ]; then
    openssl verify -CAfile "$CERTS/root-ca.crt" "$CERTS/device.crt" >/dev/null 2>&1 \
        || fail "la CA installée ne valide plus le certificat du boîtier"
fi

systemctl cat ben-publisher.service >/dev/null 2>&1 \
    || fail "ben-publisher.service n'est pas connu de systemd après daemon-reload"

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

# Informatif seulement — jamais bloquant.
if systemctl is-active --quiet ben-publisher.service; then
    log "✓ publisher actif"
else
    warn "publisher pas encore actif — normal s'il n'y a pas de réseau ou pas de certificat"
fi

log "✓ le boîtier pousse désormais ses mesures vers ben-api"
log "  à surveiller (le rattrapage part du plus ANCIEN, il durera plusieurs jours) :"
log "    journalctl -u ben-publisher -f"
log "    → « envoyé 500 · inséré N · reste ~M », un lot toutes les 60 s"
log "    → « échec n°K … » suivi de reprises = serveur injoignable, c'est prévu et sans perte"
log "✓ update OK"
