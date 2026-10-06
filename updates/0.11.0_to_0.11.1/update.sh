#!/usr/bin/env bash
# update.sh — 0.11.0 → pi-0.11.1 : UNE RELEASE QUI NE CHANGE RIEN, EXPRÈS.
#
# ═══ POURQUOI UNE RELEASE SANS EFFET EXISTE ═══════════════════════════════════════════════════
#
#   Chantier `ben-docs#16`, sous-tâche #44. Le mécanisme de la ref par boîtier est livré des deux
#   côtés — `pi-0.11.0` sur le parc, `GET /api/devices/{id}/update` déployé — mais il n'a jamais
#   fait passer UNE VRAIE RELEASE À UN SEUL BOÎTIER. C'est ce que cette version éprouve.
#
#   ⭐ Et elle l'éprouve sans rien risquer : si quelque chose casse, ça casse sur un `update.sh`
#      qui n'a rien à casser. Le chemin complet est exercé — ref demandée au cloud, plan lu sur
#      `canary`, signature GPG du tag, SHA256 du script, exécution, bump, redémarrage du
#      publisher, déclaration au cloud — et le script lui-même est inerte.
#
# ═══ CE QU'ELLE NE FAIT PAS, ET C'EST EXHAUSTIF ═══════════════════════════════════════════════
#
#   Aucun fichier livré. Aucune migration, aucune table, aucune colonne. Aucun service redémarré.
#   Aucune écriture, nulle part — ni dans la base, ni dans `/var/lib`, ni dans `/etc`.
#
#   ⇒ Le retour arrière vers 0.11.0 ne demande de restaurer RIEN : il n'y a rien à restaurer.
#   ⓘ Le seul effet observable est `device.json.softwareVersion = 0.11.1`, écrit par l'AGENT à
#     l'étape ⑨ — pas par ce script.
#
# ═══ CE QUI SE PASSE QUAND MÊME, ET QU'IL FAUT REGARDER ═══════════════════════════════════════
#
#   L'agent redémarre `ben-publisher` à l'étape ⑩, systématiquement, après CHAQUE update. Le
#   publisher constate alors que la version installée (0.11.1) n'est pas celle qu'il a déclarée
#   (0.11.0) et DÉCLARE — c'est la condition de `pi-0.10.0`, et elle fait que `devices.sw_version`
#   suit sans qu'on y pense. Attendu au journal du publisher, dans la minute :
#
#       déclaration : version installée 0.11.1, déclarée 0.11.0
#       déclaration OK — N compteur(s) déclaré(s)
#
# ⚠️ ET LE TÉMOIN DE TOUT CE CHANTIER EST AILLEURS : les boîtiers dont `ota_ref` est NULL ne
#    doivent voir AUCUNE transition. Si le parc entier passe en 0.11.1, le mécanisme n'a pas
#    fonctionné — il a seulement eu l'air de fonctionner.

set -euo pipefail
TR="→ pi-0.11.1"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
API="http://127.0.0.1:8087/health"

log "release SANS EFFET — elle n'existe que pour éprouver le canary de bout en bout (#44)"
log "  rien n'est livré, rien n'est migré, aucun service n'est redémarré par ce script"

# ═══ CONTRÔLE D'EFFET — le seul contrôle qui ait un sens ici ═══════════════════════════════════
#
# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas et l'avoir interrogée a brûlé pi-0.9.12.
#    /health prouve EN PLUS que la base est LISIBLE (`db: true`), là où un simple code 200
#    masquerait la panne.
#
# ⭐ CE QU'IL PROUVE SUR UNE RELEASE INERTE : que le chemin d'OTA lui-même n'a rien cassé. C'est
#    précisément la question qu'une release no-op pose — le script n'ayant aucun effet, tout ce
#    qu'on observe ensuite est imputable au MÉCANISME.
# ⚠️ On n'exige RIEN de la lecture TIC : aucun lecteur n'est touché, et un câble débranché ferait
#    échouer puis rejouer l'update toutes les 10 minutes — la mécanique qui a brûlé pi-0.9.12.
if systemctl is-active --quiet ben-local-api.service; then
    python3 - "$API" <<'PYEOF' || fail "/health ne répond pas, ou la base n'est plus lisible"
import json, sys, time, urllib.request
api, fin, dernier = sys.argv[1], time.time() + 60, None
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(api, timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    print(f"  /health OK — db lisible, version locale encore {d.get('softwareVersion')} "
          f"(le bump est à l'étape ⑨, après ce script)")
    sys.exit(0)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ /health répond et la base est lisible"
else
    warn "ben-local-api ne tourne pas — contrôle d'effet sauté, et ce script ne l'a pas touchée"
fi

# ═══ CE QU'IL RESTE À REGARDER ════════════════════════════════════════════════════════════════
log "── à vérifier APRÈS cette update (hors de portée de ce script) ──"
log "   journalctl -u ben-update -n 20 --no-pager    → la chaîne du canary :"
log "     « ref OTA : canary (dite par le cloud) » · « plan lu depuis origin/canary »"
log "     « Update available: 0.11.0 → 0.11.1 (tag pi-0.11.1-rc1) »"
log "   journalctl -u ben-publisher -n 20 --no-pager → la déclaration, dans la minute :"
log "     « déclaration : version installée 0.11.1, déclarée 0.11.0 »"
log "   🚨 ET LE TÉMOIN, QUI EST AILLEURS : un boîtier dont ota_ref est NULL ne doit voir AUCUNE"
log "      transition. Si le parc entier passe en 0.11.1, le mécanisme n'a pas fonctionné."

log "✓ update OK"
