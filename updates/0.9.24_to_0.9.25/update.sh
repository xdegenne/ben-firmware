#!/usr/bin/env bash
# update.sh — 0.9.24 → pi-0.9.25 : ON RÉPARE LA BASE CORROMPUE EN LA RECOPIANT.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   `pi-0.9.24` a livré le hello d'escalade, et il a répondu VINGT SECONDES après l'OTA :
#
#       [15:41:07][WARNING] échec n°1 (database disk image is malformed) …
#       [15:41:27][ERROR]   échec n°5 (database disk image is malformed) …
#
#   Ce n'était ni le réseau, ni la taille des lots, ni le serveur : une zone de la table
#   `measurements` est ILLISIBLE, et `fetch_batch` lève AVANT d'atteindre `cli.post`. Le
#   serveur n'a jamais rien eu à refuser. 61 lignes `malformed` sur deux instantanés, échec
#   n°57 du processus précédent. Neuf jours.
#
#   ⭐ CE QUI MARCHE ENCORE DIT OÙ EST LE DOMMAGE :
#
#       l'écriture — 8 322 lignes en 3 h 12, à 0,2 % du débit nominal   pages NEUVES
#       le hello                                       lit `pdl`, `contract_epoch` — intactes
#       `pending`, `unsent`, `pdl.last_ts`             servis par des INDEX, arbres séparés
#       `radio` (lit rssi/snr, non couverts)           pages de table RÉCENTES — et réussit
#       `fetch_batch`                                  ÉCHOUE : 11 colonnes, et il part du
#                                                      rowid NON ENVOYÉ LE PLUS ANCIEN
#
#     Les écritures réussissant, la page corrompue n'est pas sur le chemin menant au bout de
#     l'arbre : le dommage est dans la partie ANCIENNE. Le publisher marche droit dessus à
#     chaque tentative depuis le 22/09.
#
#   ⓘ Cause probable : l'usure de la carte SD — ~64 000 lignes écrites par jour, 24 h sur 24,
#     depuis le 1er août. AUCUN LOGICIEL NE RÉPARE ÇA ; le changement de carte reste
#     nécessaire. Cette update récupère les données et remet la base dans un état sain.
#
#   ── ① LA GARDE : une erreur locale n'est plus une panne du serveur (TOUT LE PARC) ────────
#
#   `fetch_batch` levait `sqlite3.DatabaseError`, c'était attrapé par le `except Exception` de
#   la boucle, compté dans `echecs`, et le backoff SERVEUR poussé à 300 s. Pendant neuf jours,
#   ce boîtier a accusé le serveur d'un défaut de son disque.
#
#   ⭐ Le dépôt avait déjà le précédent et ne l'appliquait pas là : `cadence_sure()` garde
#     `pending_approx` avec exactement ce commentaire — « une base locale qui bronche n'est pas
#     un serveur en panne ». `fetch_batch` n'avait aucune garde.
#
#   ── ② LA SONDE CANARI : deux lignes complètes, sur tout le parc (TOUT LE PARC) ───────────
#
#   Rien ne signalait la panne parce que TOUS les champs de l'instantané qui touchent
#   `measurements` sont servis par un INDEX. Les index étaient intacts.
#
#       0,56 ms   la plus vieille non envoyée — celle sur laquelle le publisher bute
#       0,31 ms   la plus récente — celle que le lecteur vient d'écrire
#
#   ⚠️ Et ce n'est PAS un détecteur complet, il faut le dire : si le dommage est au milieu des
#      lignes déjà envoyées, personne ne le lit et la sonde ne le voit pas. C'est une alerte
#      précoce sur le chemin de lecture du PUBLISHER, pas un `integrity_check` déguisé.
#
#   ── ③ LA RECONSTRUCTION, CIBLÉE (UN SEUL BOÎTIER) ────────────────────────────────────────
#
#   On ne répare pas le fichier : on en construit un SAIN à côté, en recopiant ce qui est
#   lisible, par tranches de `rowid` avec dichotomie sur les tranches qui lèvent. On ne perd
#   ainsi que ce qui est RÉELLEMENT détruit — quelques centaines de lignes, soit des minutes —
#   au lieu des neuf jours.
#
#   ⚠️ CE QU'ON N'A PAS PU UTILISER, ET POURQUOI :
#       VACUUM INTO           relit TOUTES les pages → avorte sur le dommage
#       Connection.backup()   copie page par page SOUS les arbres : recopierait la corruption
#                             à l'identique. Le piège est qu'il RÉUSSIRAIT
#       sqlite3 .recover      l'outil fait pour ça — mais le binaire est ABSENT des boîtiers
#                             (vérifié sur les deux modèles), et installer un paquet par OTA
#                             sur une machine injoignable est hors de question
#       DELETE / UPDATE       récrit les pages corrompues : on aggrave ce qu'on contourne
#
# ═══ LES QUATRE GARDE-FOUS DE LA RÉPARATION ═══════════════════════════════════════════════════
#
# 🚨 ① CIBLÉE PAR `device.json`, ET C'EST UNE EXCEPTION ASSUMÉE. Le mécanisme d'OTA n'a pas de
#      ciblage : les sept boîtiers sont sur la même version, donc la transition part à tous. Un
#      garde-fou d'IDENTITÉ est le seul qui ne puisse pas se tromper — toute détection par
#      symptôme a un taux de faux positifs, et un faux positif ici veut dire ARRÊTER LES
#      SERVICES D'UN BOÎTIER DE TERRAIN SAIN. On cible par l'identité ce qui est irréversible
#      ou perturbant, par le symptôme ce qui est sûr et auto-limité.
#      ⚠️ Et il échoue DU BON CÔTÉ : `device.json` illisible ⇒ on ne fait rien. Jamais « dans
#         le doute, c'est lui ».
#
# 🚨 ② IDEMPOTENTE, parce que l'OTA REJOUE toutes les 10 min quand `device.json` n'est pas
#      bumpé. La garde de symptôme la donne presque entièrement : après une réparation
#      réussie, `--refus` lit une base saine et sort sans arrêter quoi que ce soit. Et
#      `rebuild()` efface tout `.rebuild` laissé par une tentative tuée. Il manquait un FREIN :
#      un compteur de tentatives, incrémenté AVANT le travail pour qu'un SIGKILL compte aussi.
#
# 🚨 ③ UN FILET QUI SURVIT À SIGKILL. Le `trap` de bash ne peut RIEN contre SIGKILL : un
#      boîtier de terrain resterait avec ses services arrêtés jusqu'à la prochaine OTA, qui
#      serait tuée pareil — panne permanente. On arme donc un timer systemd TRANSIENT avant
#      d'arrêter quoi que ce soit, et on l'annule au succès.
#      ⓘ Il n'y a PAS de risque de timeout par ailleurs, c'est mesuré : `check_update.py`
#        appelle `subprocess.run(["bash", script])` SANS `timeout=`, et `ben-update.service`
#        étant `Type=oneshot`, systemd lui donne `TimeoutStartUSec=infinity` (le défaut global
#        est 1 min 30 — une unité ordinaire aurait été tuée à 90 s).
#
# 🚨 ④ AUCUN ÉTAT DE LA DONNÉE NE FAIT ÉCHOUER CETTE UPDATE (règle posée en 0.9.21). Toute
#      situation inattendue ⇒ l'original reste INTACT, on sort en 0, et on rapporte. Ne font
#      échouer que les défauts de CODE.
#
# ⭐ ET UNE PROPRIÉTÉ QUI REND TOUT CECI TENABLE : jusqu'au `os.replace` final, RIEN n'est en
#   jeu. On n'écrit que dans un fichier neuf. Une coupure de courant à n'importe quel moment de
#   la recopie laisse la base d'origine telle quelle.
set -uo pipefail

REPO=${REPO_PATH:-/opt/ben/repo}
STORE="$REPO/src/pi/store"
PUB="$REPO/src/pi/publisher"
VAR=/var/lib/ben-firmware
LISTE="$VAR/db-rebuild.services"
RAPPORT="$VAR/db-rebuild.json"

# Le boîtier visé. Surchargeable pour les essais, jamais en production.
CIBLE=${BEN_REBUILD_CIBLE:-ben-0012}

# La base à réparer. Surchargeable POUR LES ESSAIS UNIQUEMENT — ça permet de répéter la
# séquence complète (arrêt → reconstruction → bascule → redémarrage → hello) sur une base
# jetable, au lieu de n'avoir jamais déroulé le geste le plus risqué qu'on livre.
BASE=${BEN_REBUILD_DB:-}

# Au-delà, on ne fait plus que rapporter. Une opération de ~20 min retentée toutes les 10 min
# sur un boîtier qui n'y arrive pas est une boucle, pas une réparation.
MAX_TENTATIVES=${BEN_REBUILD_MAX:-3}

# ⚠️ LE FILET DOIT ÊTRE PLUS LONG QUE LE PIRE CAS DE LA RECONSTRUCTION. S'il se déclenchait
#    PENDANT, les écrivains réouvriraient la base et la bascule leur ferait écrire dans
#    l'ancien inode — leurs mesures partiraient dans un fichier que plus personne ne lit.
#
# ⚠️ ET LA DURÉE RÉELLE N'EST PAS MESURÉE, il faut le dire. L'essai lancé sur une copie de la
#    base de 492 Mo d'un boîtier de banc a coïncidé avec un REDÉMARRAGE de ce boîtier (sans
#    sous-tension enregistrée, sans erreur noyau — et ce boîtier redémarre de lui-même tous les
#    1 à 3 jours, donc la causalité n'est pas établie). On n'a retenu que la copie brute :
#    86 s pour 492 Mo. La base visée fait 306 Mo pour ~4,3 M lignes.
#
# ⭐ Deux heures est donc VOLONTAIREMENT LARGE, et un filet trop court serait le vrai danger :
#   il réveillerait les écrivains PENDANT l'opération, et la bascule leur ferait écrire dans
#   l'ancien inode. Le surcoût d'un filet long est borné, parce que l'autre mode de panne —
#   un REDÉMARRAGE en cours de route — se répare tout seul : systemd relance les services au
#   boot, et rien n'a été basculé.
FILET_S=${BEN_REBUILD_FILET_S:-7200}
FILET=ben-db-rebuild-filet

log()  { echo "[update → pi-0.9.25] $*"; }
warn() { echo "[update → pi-0.9.25] ⚠ $*" >&2; }
fail() { echo "[update → pi-0.9.25] ✗ $*" >&2; exit 1; }

# ═══ PRÉFLIGHT — TOUT LE PARC ═════════════════════════════════════════════════════════════════

for f in "$PUB/health.py" "$PUB/ben_publisher.py" "$STORE/db_rebuild.py" "$STORE/db.py"; do
    [ -f "$f" ] || fail "fichier absent : $f"
done

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$PUB/health.py" "$PUB/ben_publisher.py" "$STORE/db_rebuild.py" <<'PYEOF' \
        || fail "le code ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# 🚨 UN CORRECTIF LIVRÉ MAIS NON BRANCHÉ EST UNE UPDATE QUI NE CHANGE RIEN, EN SILENCE. C'est
#    le défaut qui a coûté pi-0.9.23 : la décision était juste, elle n'avait aucun effet.
python3 - "$PUB/ben_publisher.py" "$PUB/health.py" <<'PYEOF' || fail "le correctif n'est pas branché"
import pathlib, sys


def sans_commentaires(txt):
    """🚨 ON RETIRE LES COMMENTAIRES AVANT D'ANALYSER, et ce n'est pas un détail : la garde
    qu'on vérifie porte le commentaire « se rendort sur PERIOD, JAMAIS sur PERIOD_RETARD ».
    Sans ce filtrage, le contrôle se déclenche sur sa propre mise en garde et fait échouer
    l'update. Constaté sur un boîtier du parc — et c'est la DEUXIÈME fois dans ce chantier
    qu'un contrôle structurel tombe sur un contre-exemple cité en commentaire."""
    return "\n".join(l for l in txt.splitlines() if not l.lstrip().startswith("#"))


pub = sans_commentaires(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
hea = sans_commentaires(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
ko = []

# ── ① la garde : une erreur SQLite a sa PROPRE branche, AVANT le `except Exception` ──
try:
    corps = pub[pub.index("    while not _stop:"):]
except ValueError:
    corps = ""
    ko.append("boucle principale introuvable — le fichier a changé de forme")
if corps:
    i_sql = corps.find("        except sqlite3.Error as e:")
    i_gen = corps.find("        except Exception as e:")
    if i_sql < 0:
        ko.append("aucune branche `except sqlite3.Error` dans la boucle : une erreur de la "
                  "base LOCALE serait encore comptée comme une panne du SERVEUR, et le "
                  "backoff serveur monterait à 300 s pour un disque abîmé")
    elif i_gen >= 0 and i_sql > i_gen:
        ko.append("`except sqlite3.Error` est APRÈS `except Exception` : il ne sera jamais "
                  "atteint, Python prenant la première branche qui correspond")
    bloc = corps[i_sql:i_gen] if 0 <= i_sql < i_gen else ""
    if bloc and "echecs +=" in bloc:
        ko.append("la branche de la base locale incrémente `echecs`, le compteur du SERVEUR")
    if bloc and "PERIOD_RETARD" in bloc:
        ko.append("la branche de la base locale se rendort sur PERIOD_RETARD : ne pas savoir "
                  "LIRE ne doit pas faire accélérer")
    if bloc and "signaler_echec(" not in bloc:
        ko.append("la branche de la base locale ne SIGNALE pas : c'est le hello d'escalade qui "
                  "a livré la cause vingt secondes après l'OTA")

# ── ② la sonde canari ──
if 'out["read"]' not in hea:
    ko.append("`store()` ne rend pas `read` : aucune lecture de LIGNE COMPLÈTE dans "
              "l'instantané, donc une corruption de table resterait invisible — tous les "
              "autres champs sont servis par des index")

if ko:
    print("\n".join("  ✗ " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "préflight : code compilable, garde branchée et prioritaire, sonde canari en place"

# 🚨 ON EXÉCUTE LA COLLECTE SUR CE BOÎTIER-CI, sur sa VRAIE base, en LECTURE SEULE. Vérifier
#    que le code compile ne dit rien de ce qu'il fera ici.
python3 - "$PUB" "$REPO/src/pi" <<'PYEOF' || fail "la collecte de santé ne se comporte pas comme attendu"
import json, sqlite3, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[2], sys.argv[2] + "/store"]
import db, health
import capabilities as caps

ko, avert = [], []
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
try:
    brut = json.dumps(snap)
except Exception as e:                      # noqa: BLE001
    ko.append(f"l'instantané n'est PAS sérialisable en JSON : {e}")
    brut = ""
if "dev" not in snap:
    ko.append("`dev` absent : `versions()` est cassée (fonction pure de device.json)")

# ⭐ LA SONDE CANARI, ÉPROUVÉE SUR LA VRAIE BASE. C'est ici qu'on ferme le trou de la règle
#   structurelle du banc : elle fait confiance à `LIMIT 1` pour borner le travail, ce qui n'est
#   vrai que si le prédicat est servi par un index. Seule une mesure sur la cible le prouve.
lect = snap.get("read")
if not isinstance(lect, dict) or set(lect) != {"old", "new"}:
    ko.append(f"la sonde canari ne rend pas ses deux lectures : {lect!r}")
else:
    print(f"  canari : old={lect['old'][:48]} · new={lect['new'][:48]}", file=sys.stderr)
    if lect["old"] != "ok" or lect["new"] != "ok":
        avert.append("UNE LECTURE DE LIGNE COMPLÈTE ÉCHOUE SUR CE BOÎTIER — la table est "
                     "abîmée ; c'est exactement ce que la sonde doit signaler")

if duree > 10:
    avert.append(f"collecte lente : {duree:.1f} s pour un budget de {health.BUDGET_S:.0f} s")
if len(brut) > 16 * 1024:
    avert.append(f"au-delà de la borne serveur : {len(brut)} o > 16384 — champ ÉCARTÉ")

print(f"  champs : {', '.join(sorted(k for k in snap if k != 'collect_ms'))}", file=sys.stderr)
print(f"  {len(brut)} o bruts · collecte {snap.get('collect_ms')} ms", file=sys.stderr)
for a in avert:
    print(f"  ⚠ {a}", file=sys.stderr)
if ko:
    print("\n".join("  ✗ " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "✓ collecte éprouvée sur CE boîtier, sonde canari comprise"

# ═══ LA RÉPARATION — UN SEUL BOÎTIER ══════════════════════════════════════════════════════════

BOITIER=$(python3 -c 'import json,sys
try: print(json.load(open("/etc/ben-firmware/device.json")).get("deviceId",""))
except Exception: print("")' 2>/dev/null)

if [ -z "$BOITIER" ]; then
    # 🚨 ÉCHOUER DU BON CÔTÉ. Sans identité certaine, on ne touche à rien.
    warn "device.json illisible — aucune réparation tentée"
    log "✓ update OK (partie parc uniquement)"
    exit 0
fi
if [ "$BOITIER" != "$CIBLE" ]; then
    log "boîtier $BOITIER — la réparation ne concerne que $CIBLE, rien à faire"
    log "✓ update OK"
    exit 0
fi

# ── Le frein ──
TENT=$(python3 -c "import json
try: print(int(json.load(open('$RAPPORT')).get('tentatives', 0)))
except Exception: print(0)" 2>/dev/null || echo 0)
if [ "$TENT" -ge "$MAX_TENTATIVES" ]; then
    warn "$TENT tentatives déjà faites (plafond $MAX_TENTATIVES) — on ne retente plus"
    warn "la base reste abîmée ; intervention physique nécessaire (carte SD)"
    log "✓ update OK"
    exit 0
fi

# ── Le symptôme, SANS EFFET ──
# ⭐ On n'arrête pas les services d'un boîtier pour constater qu'il va bien.
REFUS=$(python3 "$STORE/db_rebuild.py" --refus $BASE 2>/dev/null || echo "sonde indisponible")
if [ -n "$REFUS" ]; then
    log "rien à reconstruire : $REFUS"
    log "✓ update OK"
    exit 0
fi
log "🚨 corruption confirmée sur $BOITIER — reconstruction (tentative $((TENT + 1)))"

# ── On compte la tentative AVANT le travail : un SIGKILL doit compter ──
python3 -c "import json,os
d = {}
try:
    with open('$RAPPORT') as f: d = json.load(f)
except Exception: pass
d['tentatives'] = int(d.get('tentatives', 0)) + 1
tmp = '$RAPPORT.tmp'
with open(tmp, 'w') as f: json.dump(d, f, ensure_ascii=False)
os.replace(tmp, '$RAPPORT')" || warn "compteur de tentatives non écrit"

# ── La liste des services : DÉRIVÉE DES CAPABILITIES, puis filtrée sur ceux qui TOURNENT ──
#
# 🚨 Pas une liste en dur : sur un boîtier Radio, `ben-tic-reader` (capability `tic-uart`) n'a
#    rien à faire là — il y est `dead` avec 172 redémarrages, précisément parce qu'il n'est pas
#    de ce modèle.
# 🚨 Et on ne REDÉMARRE que ce qui tournait : relancer une unité délibérément arrêtée serait
#    changer un état qu'on ne nous a pas demandé de changer.
: > "$LISTE"
while read -r u; do
    [ -n "$u" ] || continue
    if systemctl is-active --quiet "$u"; then echo "$u" >> "$LISTE"; fi
done < <(python3 "$STORE/db_rebuild.py" --services)
tac "$LISTE" > "$LISTE.start"
log "services concernés : $(tr '\n' ' ' < "$LISTE")"

# ── Le filet, ARMÉ AVANT TOUT ARRÊT ──
sudo systemctl stop "$FILET.timer" >/dev/null 2>&1 || true
if sudo systemd-run --unit="$FILET" --on-active="$FILET_S" --collect \
        /bin/sh -c "xargs -r systemctl start < $LISTE.start" >/dev/null 2>&1; then
    log "filet armé : les services seront relevés dans ${FILET_S} s même si ce script est tué"
else
    warn "filet NON armé — un SIGKILL laisserait les services arrêtés jusqu'à la prochaine OTA"
fi

relever() {
    xargs -r sudo systemctl start < "$LISTE.start" >/dev/null 2>&1 || true
    sudo systemctl stop "$FILET.timer" >/dev/null 2>&1 || true
}
trap relever EXIT INT TERM

# ── L'opération ──
log "arrêt des services qui tiennent la base (ben-certd et wifi-watchdog restent debout)"
xargs -r sudo systemctl stop < "$LISTE" || warn "un arrêt a échoué — on continue"
python3 "$STORE/db_rebuild.py" --rebuild $BASE || warn "la reconstruction a rendu une erreur"

trap - EXIT INT TERM
relever
log "services relevés, filet désarmé"

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════

for u in $(cat "$LISTE.start"); do
    systemctl is-active --quiet "$u" || warn "$u n'est PAS revenu — à regarder d'urgence"
done

# La base doit désormais se LIRE. C'est le seul contrôle qui compte.
python3 - "$STORE" "${BASE:-}" <<'PYEOF' || warn "la base ne se lit toujours pas — voir le rapport"
import sqlite3, sys
sys.path[:0] = [sys.argv[1]]
import db
chemin = sys.argv[2] or db.DB_PATH
c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
COLS = ("ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, src_standard, "
        "index_id, index_value, inject_total, meter_ts")
for sql, nom in ((f"SELECT {COLS} FROM measurements WHERE sent = 0 ORDER BY rowid LIMIT 1",
                  "plus vieille non envoyée"),
                 (f"SELECT {COLS} FROM measurements ORDER BY rowid DESC LIMIT 1",
                  "plus récente")):
    c.execute(sql).fetchone()
    print(f"  ✓ {nom} : lue", file=sys.stderr)
n = c.execute("SELECT count(*) FROM (SELECT 1 FROM measurements WHERE sent = 0 LIMIT 1000)")
print(f"  lot prêt à partir : {n.fetchone()[0]} ligne(s)", file=sys.stderr)
c.close()
PYEOF

# L'API locale ouvre en LECTURE SEULE. On contrôle sur /health, qui prouve en plus que la base
# est lisible (`db: true`) — jamais /info, route inexistante dont le 404 a brûlé pi-0.9.12.
if systemctl is-active --quiet ben-local-api.service; then
    # ⚠️ ON LAISSE LE TEMPS D'ÉCOUTER. Le contrôle tombait juste après le redémarrage du
    #    service et rendait un faux négatif — constaté sur la cible, « /health ne confirme pas
    #    db:true » alors que la base venait d'être lue avec succès deux lignes plus haut.
    vu=0
    for _ in 1 2 3 4 5 6; do
        if curl -fsS --max-time 10 http://127.0.0.1:8087/health 2>/dev/null \
                | grep -q '"db":[[:space:]]*true'; then vu=1; break; fi
        sleep 5
    done
    [ "$vu" = 1 ] || warn "/health ne confirme pas db:true après 30 s — à regarder"
fi

# ═══ LE HELLO, DÉCLENCHÉ EXPLICITEMENT ════════════════════════════════════════════════════════
#
# ⭐ On ne se contente PAS du hello que `check_update.py` provoque par effet de bord en
#   redémarrant le publisher à son étape 10 : ici on veut que le rapport de reconstruction
#   parte TOUT DE SUITE, et qu'on le voie dans `device_health` sans attendre.
# ⚠️ Ne fait jamais échouer l'update : c'est un envoi informatif.
python3 - "$PUB" "$REPO/src/pi" <<'PYEOF' || warn "hello non envoyé — il partira au redémarrage du publisher"
import sqlite3, sys
sys.path[:0] = [sys.argv[1], sys.argv[2], sys.argv[2] + "/store"]
import db
import capabilities as caps
import ben_publisher as pub
dev = caps.load_device()
if not dev.get("deviceId"):
    raise SystemExit("device.json sans deviceId")
cli = pub.Client(dev["deviceId"])
conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
try:
    pub.send_hello(cli, conn, dev)
    print("  ✓ hello envoyé avec le rapport de reconstruction", file=sys.stderr)
finally:
    cli.close()
    conn.close()
PYEOF

[ -f "$RAPPORT" ] && sed 's/^/  rapport : /' < <(head -c 600 "$RAPPORT"; echo)

log "✓ update OK"
# ⚠️ GUILLEMETS SIMPLES : avec des doubles, les apostrophes inverses de « `rebuild` » sont
#    interprétées comme une SUBSTITUTION DE COMMANDE par bash. Constaté sur la cible —
#    « update.sh: line 424: rebuild: command not found », et le message arrivait amputé.
log '⚠ contrôle final CÔTÉ SERVEUR : device_health doit porter rebuild et read: ok/ok'
