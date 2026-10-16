#!/usr/bin/env bash
# update.sh — 0.12.0 → pi-0.13.0 : L'ACCÈS LOCAL. TLS sur :8088, jetons porteurs de rôle,
#                                  et /claim relayé au cloud PAR TICKET.
#
# ═══ CE QUE ÇA POSE ═══════════════════════════════════════════════════════════════════════════
#
#   ① une écoute CHIFFRÉE sur :8088, qui n'ouvre QUE si le certificat est acceptable par un
#      téléphone (durée ≤ 398 j, SAN couvrant le deviceId) ;
#   ② `access.db` — base SÉPARÉE de measurements.db, créée à la première session ;
#   ③ `/etc/sudoers.d/ben-certd` à jour : UNE LIGNE PAR UNITÉ qui tient le certificat, et
#      `ben-local-api` vient de rejoindre `ben-publisher` ;
#   ④ `ben-local-api.service` revu (l'unité change) ;
#   ⑤ la caractéristique GATT du TICKET, qui rend un boîtier NEUF revendiquable.
#
# ═══ 🚨 CE QUI NE DOIT PAS CASSER, ET C'EST LA RÈGLE CARDINALE ════════════════════════════════
#
#   :8087 EN CLAIR CONTINUE DE SERVIR, QUOI QU'IL ARRIVE. C'est lui que l'app du parc et Home
#   Assistant utilisent, aujourd'hui, en permanence. Le code le garantit — tout chemin d'échec
#   de `_ecoute_tls()` rend None et l'écoute claire vit dans le thread PRINCIPAL — et ce script
#   ne doit pas inventer une exigence que le code refuse d'avoir.
#
# ⇒ D'OÙ LE CONTRÔLE D'EFFET : il EXIGE :8087, et il RAPPORTE :8088 sans l'exiger.
#   ⚠️ Exiger :8088 BRÛLERAIT la version sur tout boîtier dont le certificat n'a pas de SAN —
#      et le SAN ne se lit pas depuis le cloud (`cert_task.cert` est vidée à la livraison).
#      Un garde-fou plus exigeant que le code qu'il garde est un garde-fou faux (pi-0.9.12).

set -euo pipefail
TR="→ pi-0.13.0"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
SRC="$REPO/src/pi"
API="http://127.0.0.1:8087/health"
SUDOERS_SRC="$REPO/config/etc/sudoers.d/ben-certd"
SUDOERS_DST=/etc/sudoers.d/ben-certd
UNITE_SRC="$REPO/config/systemd/ben-local-api.service"
UNITE_DST=/etc/systemd/system/ben-local-api.service

# ═══ PRÉFLIGHT ⓞ — le droit d'écrire hors du dépôt ════════════════════════════════════════════
sudo -n true 2>/dev/null \
    || fail "l'agent (ben) n'a pas de sudo sans mot de passe — /etc/sudoers.d/ben-firmware manque"
log "préflight ⓞ OK (sudo sans mot de passe pour $(id -un))"

# ═══ PRÉFLIGHT ① — les fichiers livrés sont là et se tiennent ═════════════════════════════════
for f in "$SUDOERS_SRC" "$UNITE_SRC" \
         "$SRC/store/local_api.py" "$SRC/store/access.py" "$SRC/store/claim_ticket.py" \
         "$SRC/provisioner/main.py" "$SRC/publisher/ben_publisher.py" \
         "$SRC/store/test_access.py" "$SRC/store/test_claim_ticket.py" \
         "$SRC/store/test_routage_ports.py" "$SRC/store/test_certificat_conforme.py" \
         "$SRC/publisher/test_presenter_ticket.py"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.13.0 incomplet ?)"
done
# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$SRC" <<'PYEOF' || fail 'un fichier Python livré ne compile pas'
import ast, pathlib, sys
base = pathlib.Path(sys.argv[1])
for rel in ("store/local_api.py", "store/access.py", "store/claim_ticket.py",
            "provisioner/main.py", "publisher/ben_publisher.py"):
    p = base / rel
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
print("  5 fichiers Python compilent")
PYEOF
log "préflight ① OK (12 fichiers présents, syntaxe vérifiée)"

# ═══ PRÉFLIGHT ② — 🚨 LE SUDOERS EST VALIDE **AVANT** D'ÊTRE POSÉ ═════════════════════════════
#
# 🚨 UN /etc/sudoers.d/ CASSÉ CASSE SUDO POUR TOUT LE MONDE, root compris, et le boîtier est
#    chez un client. `visudo -c -f` le dit sans rien installer. C'est le seul préflight de ce
#    script dont l'absence serait une panne IRRÉCUPÉRABLE à distance.
sudo visudo -c -f "$SUDOERS_SRC" >/dev/null 2>&1 \
    || fail "le sudoers livré est INVALIDE — on ne le pose pas, sudo resterait cassé"
log "préflight ② OK (sudoers livré validé par visudo, sans être posé)"

# ═══ PRÉFLIGHT ③ — LES CINQ BANCS LIVRÉS, SUR LE PYTHON DU BOÎTIER ════════════════════════════
# ⚠️ `TMPDIR=/var/tmp` et pas /tmp : /tmp peut être un tmpfs étroit sur un Pi Zero.
cd "$SRC/store"
for b in test_access.py test_claim_ticket.py test_routage_ports.py test_certificat_conforme.py; do
    TMPDIR=/var/tmp python3 "$b" >/dev/null 2>&1 || fail "le banc $b échoue — NE PAS déployer"
done
cd "$SRC/publisher"
TMPDIR=/var/tmp python3 test_presenter_ticket.py >/dev/null 2>&1 \
    || fail "le banc test_presenter_ticket.py échoue — NE PAS déployer"
cd "$REPO"
log "préflight ③ OK (5 bancs livrés verts, sur le Python du boîtier)"

# ═══ PRÉFLIGHT ④ — 🚨 LE CHAMP D'ACCÈS EST DANS LE BATTEMENT, PAS DANS LE HELLO ═══════════════
#
# 🚨 C'EST LE PIÈGE QUE LE TICKET ANNONÇAIT, ET QU'AUCUN CONFLIT GIT NE SIGNALE. Le serveur
#    refuse les champs inconnus (`DisallowUnknownFields`) : un `access` resté dans `/hello`
#    partirait en 400 `bad_body` à CHAQUE déclaration, et la fonctionnalité serait morte en
#    SILENCE. Sur l'ARBRE SYNTAXIQUE, jamais un grep — les commentaires nomment les deux.
python3 - "$SRC/publisher/ben_publisher.py" <<'PYEOF' || fail "le champ d'accès n'est pas au bon endroit"
import ast, pathlib, sys
arbre = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
corps = {n.name: ast.unparse(n) for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef)}
ko = []
if "heartbeat" not in corps or "'access'" not in corps["heartbeat"].replace('"', "'"):
    ko.append("`access` ABSENT de heartbeat() — les droits ne monteraient jamais")
if "declare" in corps and "'access'" in corps["declare"].replace('"', "'"):
    ko.append("`access` PRÉSENT dans declare() — 400 bad_body à chaque /hello")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
print("  access dans heartbeat, absent de declare")
PYEOF
log "préflight ④ OK (le champ d'accès voyage dans /ping, pas dans /hello)"

# ═══ LA POSE ══════════════════════════════════════════════════════════════════════════════════
#
# ⭐ LE SUDOERS D'ABORD : `ben-certd` doit pouvoir redémarrer `ben-local-api` dès que celui-ci
#    tient un certificat. Posé après, il y aurait une fenêtre où un renouvellement laisserait
#    l'écoute chiffrée sur l'ancien certificat, sans une erreur au journal.
sudo install -m 440 -o root -g root "$SUDOERS_SRC" "$SUDOERS_DST" \
    || fail "copie de $SUDOERS_DST impossible"
sudo visudo -c -f "$SUDOERS_DST" >/dev/null 2>&1 \
    || fail "sudoers POSÉ mais invalide — à réparer À LA MAIN immédiatement"
sudo -n true 2>/dev/null || fail "sudo ne répond plus après la pose du sudoers"
log "✓ $SUDOERS_DST posé (440 root:root), validé, et sudo répond toujours"

sudo install -m 644 "$UNITE_SRC" "$UNITE_DST" || fail "copie de $UNITE_DST impossible"
sudo systemctl daemon-reload || fail "daemon-reload impossible"
log "✓ ben-local-api.service posée et relue"

# 🚨 LE LECTEUR AVANT L'API LOCALE. Le schéma SQLite se crée à l'ouverture en ÉCRITURE
#    (`db.connect()` rejoue `_SCHEMA`) ; l'API locale ouvre en LECTURE SEULE et ne peut RIEN
#    créer. L'inverse laisserait une API qui échoue sur une table absente.
#    ⓘ `access.db` est une base SÉPARÉE, créée à la première `access.session()` — donc par
#      l'API locale elle-même, en écriture. Rien à créer ici.
for lecteur in ben-tic-reader ben-radio; do
    if systemctl list-unit-files "$lecteur.service" >/dev/null 2>&1 \
       && systemctl is-enabled --quiet "$lecteur.service" 2>/dev/null; then
        sudo systemctl restart "$lecteur.service" || warn "redémarrage de $lecteur refusé"
        log "✓ $lecteur redémarré (schéma rejoué en écriture)"
    fi
done
sudo systemctl restart ben-local-api.service || fail "ben-local-api ne redémarre pas"
log "✓ ben-local-api redémarré"

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# ① :8087 EXIGÉ — c'est la règle cardinale. /health, JAMAIS /info (pi-0.9.12 brûlée), et il
#    prouve en plus que la base est lisible (`db: true`), là où un 200 masque la panne.
python3 - "$API" <<'PYEOF' || fail "/health ne répond pas sur :8087, ou la base n'est plus lisible"
import json, sys, time, urllib.request
api, fin, dernier = sys.argv[1], time.time() + 90, None
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(api, timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    print("  :8087 /health OK — db lisible")
    sys.exit(0)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
log "✓ :8087 sert, et la base est lisible"

# ② :8088 RAPPORTÉ, JAMAIS EXIGÉ — et on dit POURQUOI quand il n'ouvre pas.
sleep 3
JOURNAL="$(sudo journalctl -u ben-local-api -n 40 --no-pager -o cat 2>/dev/null || true)"
if printf '%s' "$JOURNAL" | grep -q "écoute CHIFFRÉE"; then
    log "✓ :8088 OUVERTE — $(printf '%s' "$JOURNAL" | grep -m1 'certificat conforme' || echo 'certificat conforme')"
else
    RAISON="$(printf '%s' "$JOURNAL" | grep -m1 ':8088' || echo 'aucune ligne :8088 au journal')"
    warn ":8088 NON ouverte — $RAISON"
    warn "   ⓘ Ce n'est PAS un échec d'update : :8087 sert, et l'app s'y replie légitimement."
    # 🚨 ET EN ESSAI À BLANC, CETTE LIGNE EST NORMALE — constaté sur ben-0001 le 2026-10-07.
    #    `ben-local-api.service` exécute le code de /opt/ben/repo, PAS de $REPO : avec un
    #    REPO_PATH jetable, le service redémarré est l'ANCIEN code, donc il n'écrit aucune
    #    ligne `:8088`. Pour mesurer le chemin TLS sans installer, appeler la fonction :
    #      sudo python3 -c "import sys; sys.path[:0]=['$SRC','$SRC/store']; \
    #                       import local_api as L; s=L._ecoute_tls(); print(s); s and s.server_close()"
    #    Fait sur ben-0001 ET ben-0003 : « certificat conforme : 180 j, SAN [...] », bind réussi.
    warn "   ⓘ En essai avec un REPO_PATH jetable, c'est ATTENDU (le service lit /opt/ben/repo)."
    warn "   ⇒ Sinon : renouveler le certificat avec ben-certd, puis redémarrer ben-local-api."
fi

# ③ le ticket : le fichier et sa caractéristique GATT sont-ils en place côté provisioner ?
python3 - "$SRC/provisioner/main.py" <<'PYEOF' || fail "la caractéristique du ticket n'est pas branchée"
import ast, pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
arbre = ast.parse(src)
noms = {n.name for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef)}
affectees = {t.id for n in arbre.body if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}
ko = [x for x, ok in (("on_claim_ticket_write", "on_claim_ticket_write" in noms),
                      ("CLAIM_TICKET_UUID", "CLAIM_TICKET_UUID" in affectees)) if not ok]
if ko:
    print("  absents : " + ", ".join(ko), file=sys.stderr); sys.exit(1)
print("  on_claim_ticket_write + CLAIM_TICKET_UUID en place")
PYEOF
log "✓ la caractéristique du ticket est branchée"

log "✓ update OK"
