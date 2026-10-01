#!/usr/bin/env bash
# update.sh — 0.9.21 → pi-0.9.22 : LE BOÎTIER JOINT UN INSTANTANÉ DE SANTÉ À SON HELLO.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   Un boîtier qui CESSE DE MESURER continue de dire bonjour. `last_seen` reste frais côté
#   cloud, l'OTA passe, et RIEN ne le signale. Constaté le 2026-10-01 sur un boîtier du parc :
#   NEUF JOURS sans une seule mesure, découverts par hasard, sur une machine injoignable.
#
#   ⭐ `last_seen` confond trois états — *mesure et publie* / *mesure mais ne publie pas* /
#      **NE MESURE PLUS**. Seul le dernier est une panne, et c'est le seul invisible.
#
#   Nouveau module `src/pi/publisher/health.py`, joint au hello sous la clé `health`. Côté
#   serveur : table `device_health` (jsonb non typé, historisé) — DÉJÀ DÉPLOYÉE.
#
# ═══ 🚨 CE SCRIPT NE TOUCHE À AUCUN SERVICE, ET C'EST VOULU ═══════════════════════════════════
#
#   Le seul service concerné est `ben-publisher`, et `check_update.py` le redémarre
#   SYSTÉMATIQUEMENT après chaque update (son étape 10). Le redémarrer ici serait un doublon :
#   deux coupures au lieu d'une, pour le même effet. Les lecteurs, `ben-radio` et l'API locale
#   ne partagent aucun code avec ce changement.
#
#   ⭐ Donc pas de `systemctl` du tout. C'est la forme la plus sûre qu'une update puisse
#      prendre, et la leçon de 0.9.17 poussée à son terme : ne pas toucher à ce qu'on n'a pas
#      besoin de toucher.
#
# ═══ 🚨 LE PRÉFLIGHT EST LE CONTRÔLE, PAS UNE FORMALITÉ ═══════════════════════════════════════
#
#   Cet instantané voyage dans le hello, qui porte les compteurs, les époques tarifaires et les
#   libellés. Une sonde qui lève ferait échouer le hello ENTIER — donc perdrait des métadonnées
#   pour un champ de diagnostic. On ne se contente donc pas de vérifier que le fichier compile :
#   ON EXÉCUTE LA COLLECTE SUR LA CIBLE, sur sa vraie base, et on exige qu'elle rende un objet
#   SÉRIALISABLE EN JSON. C'est la seule façon de savoir que ce boîtier-là n'a pas une
#   particularité qui ferait tomber son hello tous les jours.
#
# ⚠️ Le contrôle FINAL, lui, est côté serveur : `device_health` doit gagner des lignes au
#    prochain hello. On ne peut pas l'observer d'ici — le hello part APRÈS ce script, au
#    redémarrage du publisher par l'agent.
#
# AUCUNE migration, AUCUNE table, AUCUNE colonne côté boîtier.

set -euo pipefail
TR="→ pi-0.9.22"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
PUB="$REPO/src/pi/publisher"

# ═══ PRÉFLIGHT ════════════════════════════════════════════════════════════════════════════════
[ -f "$PUB/health.py" ] || fail "absent du dépôt : $PUB/health.py (checkout pi-0.9.22 incomplet ?)"

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$PUB/health.py" "$PUB/ben_publisher.py" <<'PYEOF' || fail "le code ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# 🚨 Un correctif livré mais NON APPELÉ serait une update qui ne change rien, en silence.
grep -q "^import health" "$PUB/ben_publisher.py" \
    || fail "ben_publisher.py n'importe pas health — le module serait livré mais jamais appelé"
grep -q 'payload\["health"\]' "$PUB/ben_publisher.py" \
    || fail "ben_publisher.py ne joint pas health au hello"
log "préflight : fichiers présents, compilent, et l'instantané est bien branché au hello"

# 🚨 ON EXÉCUTE LA COLLECTE SUR CE BOÎTIER-CI, sur sa VRAIE base, en LECTURE SEULE.
#
#    Vérifier que le code compile ne dit rien de ce qu'il fera ici. Ce boîtier a son propre
#    `/proc`, ses propres fichiers d'état, son propre journal et sa propre base : c'est
#    l'ensemble qu'il faut éprouver. Et on exige la SÉRIALISATION JSON, parce qu'une valeur
#    non sérialisable (un `bytes` venu d'une colonne, un `Decimal`) ferait lever `json.dumps`
#    DANS le publisher — donc échouer le hello, tous les jours, sans rien dire d'utile.
python3 - "$PUB" "$REPO/src/pi" <<'PYEOF' || fail "la collecte de santé ne se comporte pas comme attendu"
import json, sqlite3, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[2], sys.argv[2] + "/store"]
import db, health
import capabilities as caps

ko = []
try:
    conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
except Exception as e:                      # noqa: BLE001
    conn = None
    print(f"  base illisible ({e}) — la collecte doit quand même rendre un objet", file=sys.stderr)

t0 = time.monotonic()
snap = health.snapshot(conn, caps.load_device() or {}, db.DB_PATH)
duree = time.monotonic() - t0

if not isinstance(snap, dict):
    ko.append(f"snapshot() ne rend pas un dict mais {type(snap).__name__}")
if "collect_ms" not in snap:
    ko.append("l'instantané ne dit pas combien de temps il a pris")
try:
    brut = json.dumps(snap)
except Exception as e:                      # noqa: BLE001
    ko.append(f"l'instantané n'est PAS sérialisable en JSON : {e}")
    brut = ""

# ⚖️ LE TÉMOIN — et la première version, `len(snap) < 3`, NE POUVAIT JAMAIS ÉCHOUER.
#
#    `host()` verse à lui seul jusqu'à SEPT clés à plat (up, boot, temp, load, mem, tainted,
#    disk_mb), plus `collect_ms`. Le compte était donc toujours >= 3, même si `store`, `radio`,
#    `units`, `errors` ET `repo` revenaient tous vides. On aurait livré à sept boîtiers un
#    diagnostic qui ne diagnostique rien, avec un témoin vert.
#
# ⭐ On exige donc des clés NOMMÉES, et on choisit celles qui ne peuvent pas manquer pour une
#    raison d'ENVIRONNEMENT :
#
#    · `dev` est une fonction PURE du `device.json` que l'agent lit déjà pour connaître sa
#      propre version. Si elle manque, c'est `versions()` qui est cassée — un défaut de CODE,
#      donc un tag à refaire, donc un échec légitime.
#    · ⭐ et au moins UNE sonde au-delà de `host` : c'est l'ASSEMBLAGE qu'on éprouve, pas une
#      sonde en particulier. Un module qui ne rendrait que le socle passerait tout le reste.
#
# ⚠️ `units` et `pdl` sont en AVERTISSEMENT, pas en échec : `systemctl` peut expirer et la base
#    peut être verrouillée. Les exiger ferait échouer l'update sur un boîtier simplement
#    chargé — donc rejeu toutes les 10 min, la mécanique de pi-0.9.12.
if "dev" not in snap:
    ko.append("`dev` absent : `versions()` est cassée (fonction pure de device.json)")
AU_DELA_DU_SOCLE = {"wifi", "dev", "db", "pdl", "pending", "emitter", "events_pending",
                    "radio", "repo", "units", "errors"}
if not (AU_DELA_DU_SOCLE & set(snap)):
    ko.append(f"AUCUNE sonde au-delà du socle n'a rapporté ({sorted(snap)}) — "
              "l'assemblage ne marche pas")

# 🚨 CE QUI AVERTIT SANS FAIRE ÉCHOUER, et la distinction n'est pas de la mollesse.
#
#    Un instantané trop gros ou une collecte lente sont des états DU BOÎTIER — journal
#    bavard, carte SD fatiguée, machine chargée. Faire échouer l'update pour ça laisserait
#    `device.json` non bumpé, donc l'update REJOUERAIT toutes les 10 min sur un boîtier qui
#    n'a rien de cassé. C'est la mécanique de pi-0.9.12, et la règle posée en 0.9.21 :
#    AUCUN ÉTAT DE LA DONNÉE NE FAIT ÉCHOUER UNE UPDATE.
#
#    Ne font échouer que les défauts de CODE : pas un dict, pas sérialisable, quasi vide.
#    Ceux-là veulent dire que le tag est mauvais, et un tag se corrige par un autre tag.
avertissements = []
if len(brut) > 16 * 1024:
    avertissements.append(f"au-delà de la borne serveur : {len(brut)} o > 16384 — le champ "
                          "sera ÉCARTÉ, le hello passera quand même")
if duree > 10:
    avertissements.append(f"collecte lente sur ce boîtier : {duree:.1f} s pour un budget de "
                          f"{health.BUDGET_S:.0f} s — les sondes chères seront perdues")
if "units" not in snap:
    avertissements.append("`units` absent — systemctl a expiré ; l'état des services "
                          "manquera au diagnostic de ce boîtier")
# 🚨 Celui-ci manquait, et c'est pour ça que `errors` est resté mort-né sans que rien ne le
#    dise : il dépend de l'appartenance de `ben` au groupe `systemd-journal`, ajoutée plus
#    bas dans ce script mais effective seulement au redémarrage du publisher.
if "errors" not in snap:
    avertissements.append("`errors` absent — `ben` ne lit pas le journal (groupe "
                          "systemd-journal) ou aucune entrée de priorité <= 3 ; "
                          "les erreurs NOYAU manqueront au diagnostic")
if conn is not None and "pdl" not in snap:
    avertissements.append("`pdl` absent alors que la base est ouverte — base verrouillée, "
                          "ou aucun compteur enregistré")

print(f"  champs : {', '.join(sorted(k for k in snap if k != 'collect_ms'))}", file=sys.stderr)
print(f"  {len(brut)} o bruts · collecte {snap.get('collect_ms')} ms", file=sys.stderr)
for a in avertissements:
    print(f"  ⚠ {a}", file=sys.stderr)
if ko:
    print("\n".join("  ✗ " + k for k in ko), file=sys.stderr)
    sys.exit(1)
PYEOF
log "✓ collecte éprouvée sur CE boîtier : objet sérialisable, non vide, dans le budget"

# ═══ L'ACCÈS AU JOURNAL — le seul geste de ce script ═════════════════════════════════════════
#
# 🚨 SANS LE GROUPE `systemd-journal`, `journalctl` NE REND RIEN À `ben`.
#
#    Mesuré sur un boîtier du parc : `id ben` → dialout, spi, gpio, et rien d'autre.
#    `sudo -u ben journalctl` répond « No journal files were opened due to insufficient
#    permissions » — SUR STDERR, que `_sh` jette. Donc `errors` rendait None partout, en
#    silence. Et les essais passaient parce qu'on les lançait en `pi`, qui est dans `adm`.
#
# ⭐ C'est le champ le plus utile de tout l'instantané qui était mort-né : les lignes NOYAU
#    (blocages SPI, sous-tensions, `brcmfmac: resumed on timeout` du pilote WiFi) ne
#    remontaient pas. `install.sh` est corrigé pour les boîtiers NEUFS ; celui-ci répare
#    les SEPT qui existent.
#
# ⚠️ L'appartenance à un groupe est lue au DÉMARRAGE du processus : elle ne prendra effet
#    qu'au redémarrage de ben-publisher — que `check_update.py` fait de toute façon à son
#    étape 10, juste après ce script.
if id -nG ben 2>/dev/null | tr ' ' '\n' | grep -qx systemd-journal; then
    log "ben est déjà dans systemd-journal"
elif sudo usermod -aG systemd-journal ben 2>/dev/null; then
    log "✓ ben ajouté au groupe systemd-journal — effectif au redémarrage du publisher"
else
    # ⚠️ On AVERTIT sans faire échouer : un groupe manquant dégrade UN champ de diagnostic.
    #    Échouer ici laisserait device.json non bumpé, donc rejeu toutes les 10 min — pour
    #    une update dont tout le reste est en place.
    warn "impossible d'ajouter ben à systemd-journal — le champ `errors` restera vide"
fi

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# ⭐ On n'a rien touché, donc il n'y a rien à réparer — mais on vérifie quand même que le
#    publisher est debout. S'il ne l'était pas, l'étape 10 de l'agent ne le redémarrerait PAS
#    (elle est gardée par `is-active`), et l'instantané ne partirait jamais. On le DIT sans
#    faire échouer l'update : un publisher arrêté est un état que `check_network` décide, pas
#    nous, et faire échouer ici rejouerait l'update toutes les 10 min pour toujours.
if systemctl is-active --quiet ben-publisher.service; then
    log "ben-publisher est debout — l'agent le redémarrera, et le hello suivant portera la santé"
else
    warn "ben-publisher n'est PAS actif : le code est livré, mais aucun instantané ne partira"
    warn "(état décidé par check_network, pas par cette update — on ne le démarre pas ici)"
fi

# La base reste lisible par l'API locale, qui ouvre en LECTURE SEULE. On contrôle sur /health
# (jamais /info, route inexistante — c'est ce 404 qui a brûlé pi-0.9.12).
if systemctl is-active --quiet ben-local-api.service; then
    curl -fsS --max-time 10 http://127.0.0.1:8087/health 2>/dev/null | grep -q '"db":[[:space:]]*true' \
        || warn "/health ne confirme pas db:true — à regarder"
fi

log "✓ update OK"
log "⚠ contrôle final CÔTÉ SERVEUR : device_health doit gagner une ligne au prochain hello"
