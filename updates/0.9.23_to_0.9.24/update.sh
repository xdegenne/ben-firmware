#!/usr/bin/env bash
# update.sh — 0.9.23 → pi-0.9.24 : L'ÉCHEC SE SIGNALE LUI-MÊME, ET LA SONDE VOIT ENFIN.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   `pi-0.9.23` a été livrée le 2026-10-01 vers 11 h. Deux heures plus tard, le boîtier qui
#   motive tout ce chantier l'avait prise, avait envoyé son premier instantané — et ne nous
#   avait toujours rien appris. Cette version corrige les DEUX raisons, et les deux sont des
#   défauts de la version précédente, pas du boîtier.
#
#   ── ① LA SONDE `pub` ÉTAIT AVEUGLE AU SEUL MOMENT OÙ ELLE S'EXÉCUTE ──────────────────────
#
#   `N_PUB` valait 5. Voici, mot pour mot, ce que le boîtier a remonté à 11:47 :
#
#       Stopped ben-publisher.service …
#       ben-publisher.service: Consumed 1.308s CPU time.
#       Started ben-publisher.service …
#       [INFO] démarrage — … · lots de 1000 toutes les 60 s
#       [INFO] ~569526 point(s) en attente
#
#   🚨 Cinq lignes, cinq places : AUCUNE ligne de l'ancien processus. Or un redémarrage
#      systemd émet à lui seul CINQ lignes, et la sonde ne tourne QUE là — au hello qui suit
#      le redémarrage de l'OTA. Elle était structurellement incapable de voir ce qui est sa
#      raison d'être. `N_PUB = 40` couvre ~40 min d'un publisher en échec pour ~3,5 Ko,
#      contre un plafond serveur de 16 Ko.
#
#   ⚖️ Le défaut ne produisait AUCUNE erreur : cinq lignes valides, bien formées, inutiles.
#      Rien dans un banc fonctionnel ne pouvait s'en plaindre — c'est la CONSTANTE qu'il faut
#      lire, et c'est ce que fait le nouveau cas de banc.
#
#   ── ② L'INSTANTANÉ N'ARRIVAIT QU'UNE FOIS PAR JOUR ───────────────────────────────────────
#
#   Mesuré le même jour, sur le même boîtier :
#
#       il MESURE              dernier point à l'instant, trame LoRa toutes les 38 s
#       il est EN LIGNE        hello 204 en 17 ms, WiFi -40 dBm
#       le publisher TOURNE    active/running, 0 redémarrage, charge 0,24
#       pending                569 526 points — exactement les 8,8 jours manquants, à 0,35 %
#       POST de mesures        ZÉRO, pendant que six autres boîtiers en font 25 en 25 min
#       ben-api               aucun 4xx, sur aucun boîtier : LE SERVEUR NE REFUSE RIEN,
#                              le boîtier ne DEMANDE pas
#
#   ⇒ Il échoue localement, il l'écrit dans son journal à chaque tentative, et cette ligne
#     reste illisible jusqu'au lendemain parce que l'instantané ne voyage qu'avec le hello,
#     et que le hello ne part qu'une fois par jour (`HELLO_EVERY=86400`). On attendait 24 h
#     pour apprendre ce que le boîtier savait depuis la première minute.
#
#   ⭐ DÉSORMAIS L'ÉCHEC SE SIGNALE. Au franchissement de `ECHECS_ERREUR`, le publisher envoie
#      un hello : l'instantané part PENDANT la panne, avec les lignes de son propre journal.
#
#   ⚠️ Plafonné à UN PAR HEURE (`HELLO_SUR_ECHEC_S`), et jamais plus d'un par panne continue :
#      un boîtier coupé du monde doit signaler, pas se mettre à battre. Sans ce plancher, un
#      boîtier en panne persistante enverrait un instantané toutes les 300 s au plafond du
#      backoff, avec jusqu'à 20 s de collecte à chaque fois.
#
#   ⚠️ Le seuil est celui de l'ERREUR, pas le premier échec : une coupure de quelques secondes
#      arrive tous les jours sur les sept boîtiers. UN SEUL SEUIL, partagé avec
#      `niveau_echec()` — deux seuils seraient deux choses à garder d'accord.
#
#   🚨 ET L'ORDRE EST LE FOND : le hello part APRÈS la ligne de journal, jamais avant.
#      `snapshot()` LIT le journal ; envoyé d'abord, le signalement partirait avec un
#      instantané qui ne contient pas l'échec qui l'a déclenché. Un signalement qui ne
#      signale rien — exactement le défaut de `N_PUB = 5`. Un cas de banc STRUCTUREL lit
#      donc l'ordre dans la source, parce que les deux ordres produisent un hello valide, un
#      code de retour identique, et aucune erreur.
#
#   ── ③ `pending` ENCADRE, IL NE COMPTE PAS — ET J'AI CONFONDU LES DEUX ────────────────────
#
#   `pending_approx()` vaut `max(rowid) - min(rowid WHERE sent=0) + 1`. Il prouve OÙ se trouve
#   la plus vieille ligne non envoyée, rien de plus. On a lu « 566 248 en attente » et on en a
#   déduit « un lot plein part à chaque tour, donc il échoue » — alors qu'UNE SEULE ligne
#   restée à `sent = 0` sur un vieux rowid produit exactement le même chiffre.
#
#   ⭐ `health.unsent` COMPTE, borné au lot :
#
#        SELECT count(*) FROM (SELECT 1 FROM measurements WHERE sent = 0 LIMIT 1000)
#
#      = 1000  un lot PLEIN existe ⇒ un POST est tenté ⇒ il ÉCHOUE, et `pub` dit pourquoi
#      = 0     il n'a RIEN à envoyer ⇒ aucun POST, aucun échec ⇒ `pending` est un artefact,
#              et la question devient : pourquoi des lignes sont-elles `sent = 1` alors que
#              le cloud ne les a pas ?
#      entre   des lots PARTIELS — un troisième cas, qu'on ne voyait pas
#
#   ⚠️ BORNÉ, et c'est tout ce qui le rend possible : un `count(*) WHERE sent = 0` nu balaie
#      les millions d'entrées de l'index et prend 37 s sur un Pi Zero. Mesuré avec la borne
#      RÉELLEMENT atteinte : 1,8 ms sur un boîtier radio, 2,2 ms sur un filaire, par index
#      COUVRANT (`idx_meas_sent` suffit, aucun accès à la table).
#
#   ── ④ « RIEN À ENVOYER » N'ÉCRIVAIT RIEN ─────────────────────────────────────────────────
#
#   La branche était en `log.debug`, et le niveau racine est `INFO`. ⇒ Un publisher qui échoue
#   en boucle et un publisher qui n'a rien à envoyer laissaient EXACTEMENT la même trace :
#   aucune. Impossible de les séparer à distance — et c'est ce qui a coûté neuf jours.
#
#   ⭐ Elle passe en `info`, avec le retard joint, parce que c'est la CONTRADICTION qui
#      informe : « rien à envoyer · reste ~569526 » dit en une ligne que `pending` et la
#      réalité ne s'accordent pas. Les deux chiffres séparés ne disaient rien.
#   ⚠️ Gratuit sur un boîtier sain : à 0,74 point/s et une période de 60 s, chaque tour porte
#      ~44 points, donc cette branche n'y est jamais atteinte.
#
# ═══ CE QUE CE SCRIPT PEUT FAIRE ÉCHOUER ══════════════════════════════════════════════════════
#
# 🚨 AUCUN ÉTAT DE LA DONNÉE NE FAIT ÉCHOUER UNE UPDATE (règle posée en 0.9.21). Un échec
#    laisse `device.json` non bumpé, donc l'update REJOUE toutes les 10 min — c'est la
#    mécanique qui a brûlé pi-0.9.12. Ne font échouer que les défauts de CODE : du code qui
#    ne compile pas, ou un correctif livré mais NON APPELÉ. Tout le reste avertit.
set -uo pipefail

REPO=${REPO_PATH:-/opt/ben/repo}
PUB="$REPO/src/pi/publisher"

log()  { echo "[update → pi-0.9.24] $*"; }
warn() { echo "[update → pi-0.9.24] ⚠ $*" >&2; }
fail() { echo "[update → pi-0.9.24] ✗ $*" >&2; exit 1; }

# ═══ PRÉFLIGHT ════════════════════════════════════════════════════════════════════════════════

for f in "$PUB/health.py" "$PUB/ben_publisher.py"; do
    [ -f "$f" ] || fail "fichier absent : $f"
done

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$PUB/health.py" "$PUB/ben_publisher.py" <<'PYEOF' || fail "le code ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# 🚨 UN CORRECTIF LIVRÉ MAIS NON APPELÉ EST UNE UPDATE QUI NE CHANGE RIEN, EN SILENCE.
#    C'est le seul défaut de code que ce script puisse attraper sans exécuter la boucle, et
#    il est exactement du type qui a coûté pi-0.9.23 : la décision était juste, elle n'avait
#    simplement aucun effet.
python3 - "$PUB/ben_publisher.py" "$PUB/health.py" <<'PYEOF' || fail "le correctif n'est pas branché"
import pathlib, re, sys
pub = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
hea = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
ko = []

# ── ① la sonde doit survivre à la bannière de redémarrage ──
m = re.search(r"^N_PUB\s*=\s*(\d+)", hea, re.M)
if not m:
    ko.append("N_PUB introuvable dans health.py")
elif int(m.group(1)) <= 5:
    ko.append(f"N_PUB = {m.group(1)} : la bannière de redémarrage systemd (Stopped, Consumed "
              "CPU, Started + 2 INFO de démarrage) consomme tout le quota — la sonde ne "
              "verra JAMAIS l'ancien processus, qui est sa raison d'être")
elif int(m.group(1)) < 20:
    ko.append(f"N_PUB = {m.group(1)} : trop peu de lignes utiles après la bannière pour voir "
              "une SUITE d'échecs")

# ── ② le signalement doit être appelé, dans la boucle, et APRÈS la ligne de journal ──
if "def signaler_echec(" not in pub:
    ko.append("`signaler_echec()` absente")
try:
    corps = pub[pub.index("    while not _stop:"):]
except ValueError:
    corps = ""
    ko.append("boucle principale introuvable — le fichier a changé de forme")
if corps:
    if "if signaler_echec(" not in corps:
        ko.append("`signaler_echec()` n'est JAMAIS appelée dans la boucle : la fonction est "
                  "livrée, le comportement non — l'instantané continuerait d'arriver une "
                  "fois par jour")
    else:
        i_log = corps.find("log.log(niveau_echec(echecs)")
        i_sig = corps.index("if signaler_echec(")
        if i_log < 0:
            ko.append("la ligne de journal de l'échec a disparu")
        elif i_log > i_sig:
            ko.append("le hello de signalement part AVANT que l'échec soit journalisé : "
                      "`snapshot()` lit le journal, l'instantané ne contiendrait donc PAS "
                      "la raison qui l'a déclenché")
        bloc = corps[i_sig:]
        bloc = bloc[:bloc.find("cli.close()")] if "cli.close()" in bloc else bloc
        if "prochain_hello = hello()" not in bloc:
            ko.append("le signalement ne réarme pas `prochain_hello` : le battement quotidien "
                      "viendra se superposer, on paiera deux collectes pour une information")

# ── ③ le comptage borné doit exister ET être borné ──
if "N_UNSENT" not in hea:
    ko.append("`N_UNSENT` absente de health.py")
if 'out["unsent"]' not in hea:
    ko.append("`store()` ne rend pas `unsent` : on resterait avec `pending`, qui ENCADRE par "
              "rowid et ne dit donc pas si un lot plein existe")
elif "WHERE sent = 0 LIMIT ?" not in hea:
    ko.append("le comptage des non envoyés n'est PAS borné : un count(*) nu sur `sent = 0` "
              "prend 37 s sur un Pi Zero, et il serait fait à chaque hello")

# ── ① bis : la sonde doit aussi être bornée en OCTETS ──
#    🚨 L'échec est ASYMÉTRIQUE : ben-api écarte le champ `health` ENTIER au-delà de 16 Ko.
#       Une sonde trop bavarde n'emporte pas que son champ, elle emporte les dix-neuf autres.
#       Et une borne en LIGNES est la mauvaise unité : 40 lignes tronquées à 200 caractères
#       font 9,1 Ko à elles seules.
if "PUB_BUDGET_O" not in hea:
    ko.append("`PUB_BUDGET_O` absente : la sonde `pub` n'est bornée qu'en LIGNES, donc son "
              "pire cas (40 × 200 car. = 9,1 Ko) peut faire franchir les 16 Ko du serveur — "
              "qui écarte alors TOUT l'instantané, pas seulement ce champ")

# ── ④ « rien à envoyer » doit s'écrire ──
if corps:
    try:
        i = corps.index('"rien à envoyer')
        appel = corps[corps.rindex("log.", 0, i):corps.index(")", i) + 1]
        if not appel.startswith("log.info("):
            ko.append("« rien à envoyer » est journalisé par %r : avec un niveau racine à "
                      "INFO un `debug` n'écrit RIEN, et un publisher en échec resterait "
                      "indiscernable d'un publisher sans rien à envoyer"
                      % appel.split("(")[0])
        elif "pending_approx" not in appel:
            ko.append("la ligne « rien à envoyer » ne porte pas le retard — sans lui elle ne "
                      "contredit rien et n'informe pas")
    except ValueError:
        ko.append("la branche « rien à envoyer » a disparu de la boucle")

if ko:
    print("\n".join("  ✗ " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "préflight : code compilable, sonde non aveugle, signalement branché, comptage borné"

# ═══ LA DÉCISION ET SON EFFET, ÉPROUVÉS SUR CE BOÎTIER-CI ═════════════════════════════════════
#
# ⭐ `signaler_echec()` est PURE — c'est ce qui permet d'éprouver le plancher horaire sans
#    attendre une heure, sans réseau et sans base. On vérifie les trois bords, TÉMOIN COMPRIS.
python3 - "$PUB" "$REPO/src/pi" <<'PYEOF' || fail "le signalement ne se comporte pas comme attendu"
import sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import ben_publisher as pub
ko = []

if not pub.signaler_echec(pub.ECHECS_ERREUR, float("-inf"), 1000.0):
    ko.append("un échec PERSISTANT ne déclenche pas de signalement — rien n'aura changé")
# ⚖️ LE TÉMOIN, et il n'est pas décoratif : sans lui, une fonction qui rendrait toujours True
#    passerait le cas ci-dessus. C'est le parc entier qui se mettrait à battre.
if pub.signaler_echec(1, float("-inf"), 1000.0):
    ko.append("TÉMOIN : un échec ISOLÉ déclenche un signalement — une coupure de trois "
              "secondes, qui arrive tous les jours, ferait collecter les sept boîtiers")
# ⚠️ Le plancher : une panne CONTINUE ne doit signaler qu'une fois par heure.
t0 = 100_000.0
if pub.signaler_echec(99, t0, t0 + 1):
    ko.append("aucun plancher : une panne continue signalerait à chaque tentative, soit un "
              "instantané toutes les 300 s avec jusqu'à 20 s de collecte")
if not pub.signaler_echec(99, t0, t0 + pub.HELLO_SUR_ECHEC_S):
    ko.append("le plancher ne se relâche jamais : un seul signalement par panne, même si "
              "elle dure des jours")
if not 0 < pub.HELLO_SUR_ECHEC_S <= 86400:
    ko.append(f"plancher aberrant ({pub.HELLO_SUR_ECHEC_S} s) : ni plus fréquent que le "
              "battement quotidien, ni nul")

print(f"  seuil d'erreur {pub.ECHECS_ERREUR} échecs · plancher "
      f"{pub.HELLO_SUR_ECHEC_S:.0f} s · battement {pub.HELLO_EVERY:.0f} s", file=sys.stderr)
if ko:
    print("\n".join("  ✗ " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "✓ signalement éprouvé : persistant oui, isolé non, une fois l'heure"

# ═══ LA COLLECTE, SUR LA VRAIE BASE DE CE BOÎTIER, EN LECTURE SEULE ═══════════════════════════
#
# 🚨 Vérifier que le code compile ne dit rien de ce qu'il fera ICI : ce boîtier a son propre
#    /proc, son propre journal, sa propre base. Et on exige la SÉRIALISATION JSON — une valeur
#    non sérialisable ferait lever `json.dumps` DANS le publisher, donc échouer le hello tous
#    les jours sans rien dire d'utile.
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

# ⚖️ `dev` est une fonction PURE de device.json : son absence est un défaut de CODE. Et on
#    exige au moins une sonde AU-DELÀ du socle, parce que c'est l'ASSEMBLAGE qu'on éprouve —
#    `host()` verse à lui seul sept clés à plat, donc un simple `len(snap) >= 3` ne pouvait
#    jamais échouer (défaut corrigé en 0.9.23).
if "dev" not in snap:
    ko.append("`dev` absent : `versions()` est cassée (fonction pure de device.json)")
AU_DELA_DU_SOCLE = {"wifi", "dev", "db", "pdl", "pending", "emitter", "events_pending",
                    "radio", "repo", "units", "errors", "pub", "unsent"}
if not (AU_DELA_DU_SOCLE & set(snap)):
    ko.append(f"AUCUNE sonde au-delà du socle n'a rapporté ({sorted(snap)}) — "
              "l'assemblage ne marche pas")

# 🚨 CE QUI AVERTIT SANS FAIRE ÉCHOUER. Un instantané gros ou une collecte lente sont des
#    états DU BOÎTIER (journal bavard, carte SD fatiguée, machine chargée) : faire échouer
#    pour ça rejouerait l'update toutes les 10 min sur un boîtier qui n'a rien de cassé.
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
if "errors" not in snap:
    avertissements.append("`errors` absent — `ben` ne lit pas le journal (groupe "
                          "systemd-journal) ou aucune entrée de priorité <= 3")
# ⭐ LE CHAMP DE CETTE VERSION. On dit combien de lignes il porte : c'est le seul moyen de
#    voir, sur la cible, que le quota n'est plus mangé par la bannière de redémarrage.
pub_lignes = snap.get("pub") or []
if not pub_lignes:
    avertissements.append("`pub` absent — le journal de ben-publisher est illisible ; la "
                          "cause d'une panne de publication n'arrivera qu'au hello suivant")
else:
    if len(pub_lignes) <= 5:
        avertissements.append(f"`pub` ne porte que {len(pub_lignes)} ligne(s) — journal court, "
                              "ou journalctl tronque ; à revoir si ça persiste après "
                              "quelques minutes de fonctionnement")
if conn is not None and "pdl" not in snap:
    avertissements.append("`pdl` absent alors que la base est ouverte — base verrouillée, "
                          "ou aucun compteur enregistré")

# ⭐ LE CHIFFRE QU'ON VIENT CHERCHER. On l'affiche sur la cible : c'est le seul moyen de voir,
#    AVANT de faire signer le tag, qu'il répond quelque chose de sensé sur une VRAIE base.
# ⭐ La part de `pub` dans l'instantané : c'est elle qui a doublé la taille entre 0.9.23 et ce
#    tag, et c'est elle qui est bornée en octets.
if snap.get("pub"):
    o_pub = len(json.dumps(snap["pub"], separators=(",", ":")).encode())
    print(f"  pub : {len(snap['pub'])} ligne(s), {o_pub} o "
          f"(borne {health.PUB_BUDGET_O} o / {health.N_PUB} lignes)", file=sys.stderr)
    if o_pub > health.PUB_BUDGET_O + 300:
        ko.append(f"la sonde `pub` rend {o_pub} o pour un budget de {health.PUB_BUDGET_O} : "
                  "la borne en octets ne s'applique pas")

if "unsent" in snap:
    print(f"  pending ~{snap.get('pending')} · unsent {snap['unsent']} "
          f"(borne {health.N_UNSENT})", file=sys.stderr)
    if snap["unsent"] == 0 and (snap.get("pending") or 0) > health.N_UNSENT:
        avertissements.append(f"CONTRADICTION : pending ~{snap['pending']} mais unsent 0 — "
                              "rien à envoyer alors que l'encadrement annonce du retard. "
                              "C'est exactement le cas qu'on cherchait à distinguer")
else:
    avertissements.append("`unsent` absent — base verrouillée ; on ne saura pas si un lot "
                          "plein existe, donc pas si les POST échouent")

print(f"  champs : {', '.join(sorted(k for k in snap if k != 'collect_ms'))}", file=sys.stderr)
print(f"  {len(brut)} o bruts · collecte {snap.get('collect_ms')} ms", file=sys.stderr)
for a in avertissements:
    print(f"  ⚠ {a}", file=sys.stderr)
if ko:
    print("\n".join("  ✗ " + k for k in ko), file=sys.stderr)
    sys.exit(1)
PYEOF
log "✓ collecte éprouvée sur CE boîtier : objet sérialisable, non vide, dans le budget"

# ═══ L'ACCÈS AU JOURNAL — idempotent, et on le garde ══════════════════════════════════════════
#
# ⭐ Livré en 0.9.22, donc normalement déjà fait sur les sept boîtiers. On le laisse parce
#    qu'un boîtier revenu d'un retour arrière, ou provisionné avec un vieil `install.sh`,
#    n'aurait rien dans `errors` ET rien dans `pub` — soit les deux champs sur lesquels
#    repose tout ce chantier.
if id -nG ben 2>/dev/null | tr ' ' '\n' | grep -qx systemd-journal; then
    log "ben est déjà dans systemd-journal"
elif sudo usermod -aG systemd-journal ben 2>/dev/null; then
    log "✓ ben ajouté au groupe systemd-journal — effectif au redémarrage du publisher"
else
    warn "impossible d'ajouter ben à systemd-journal — `errors` et `pub` resteront vides"
fi

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# ⭐ On n'a touché à aucun service : l'étape 10 de l'agent redémarre ben-publisher juste après
#    ce script, et c'est ce redémarrage qui envoie le hello. Mais cette étape est GARDÉE par
#    `is-active` : si le publisher est arrêté, elle ne le démarre pas, et aucun instantané ne
#    partira jamais. On le DIT sans faire échouer — un publisher arrêté est un état que
#    `check_network` décide, pas nous.
if systemctl is-active --quiet ben-publisher.service; then
    log "ben-publisher est debout — l'agent le redémarrera, et son hello portera la santé"
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
log "⚠ contrôle final CÔTÉ SERVEUR : au prochain échec persistant, device_health doit gagner"
log "  une ligne SANS attendre 24 h, et son champ `pub` doit porter plus de 5 lignes"
