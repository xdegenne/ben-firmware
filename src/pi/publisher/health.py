#!/usr/bin/env python3
"""health.py — l'instantané que le boîtier joint à son hello (issue #16).

🚨 POURQUOI CE MODULE EXISTE
───────────────────────────
Un boîtier qui **cesse de mesurer** continue de dire bonjour. `last_seen` reste frais côté
cloud, l'OTA passe, et **rien ne le signale**. Constaté sur un boîtier du parc le 2026-10-01 :
NEUF JOURS sans une seule mesure, découverts par hasard — et il est injoignable, ni SSH ni VPN.

⭐ `last_seen` confond trois états :

    mesure et publie          → last_seen frais
    mesure mais ne publie pas → last_seen ancien   (celui-là se voit déjà)
    NE MESURE PLUS            → last_seen FRAIS    🚨 invisible

Ce module ne sert à rien d'autre qu'à séparer le premier état du troisième, et à dire POURQUOI.

🚨 LA RÈGLE QUI PRIME SUR TOUT LE RESTE
───────────────────────────────────────
**Le hello est purement informatif, et cet instantané l'est encore plus.** Une commande qui
pend, un fichier absent, une base verrouillée, un `/proc` qui n'existe pas : RIEN ne doit
empêcher le hello de partir, ni a fortiori la publication des mesures. Chaque sonde est isolée
et se replie sur l'ABSENCE de son champ — jamais sur une exception, jamais sur une valeur
inventée. Même discipline que `_meta()` dans le publisher : on dégrade champ par champ.

⚠️ CE QUI A ÉTÉ ÉCARTÉ, ET POURQUOI — tout vérifié sur un Pi Zero du parc
────────────────────────────────────────────────────────────────────────
· 🚨 `PRAGMA quick_check` : pas « coûteux », **NUISIBLE**. Sur une base de 490 Mo il a poussé la
  charge d'un Pi Zero mono-cœur de 1,15 à **4,50**, rendu `sshd` muet plus d'une minute, et ne
  s'est pas terminé en 180 s. On a abîmé un boîtier de production pour le mesurer.
· 🚨 `journalctl --since` : **9 SECONDES** de balayage, et il a rendu **0 octet** là où `-n 8`
  seul en rendait 8 392 — les dernières erreurs existaient, mais dataient de plus de 24 h.
  Filtrer par date côté boîtier coûtait neuf secondes POUR JETER L'INFORMATION UTILE. On prend
  les dernières avec leur horodatage, et c'est le lecteur qui juge si elles sont vieilles.
· `journalctl --list-boots` : même balayage, même verdict.
· `vcgencmd get_throttled` : échoue en utilisateur `ben` (`/dev/vcio` inaccessible).
· `systemctl status` : prose localisée, et il **pagine** — un script sans `--no-pager` pendrait.

🚨 ON NE TOUCHE À AUCUN MATÉRIEL
────────────────────────────────
`ben-radio` possède le SPI en exclusivité. Tout ce qui concerne la radio se lit dans
`radio-state.json` et la table `lora_link` — JAMAIS par un accès direct. La course au GPIO
perdue par le lecteur contre `ben-radio` a coûté 2 538 plantages en neuf heures (0.9.17).

    python3 health.py [chemin/vers/measurements.db]
"""
import json
import os
import sqlite3
import subprocess
import time

# Les unités qu'on interroge. On ne décide PAS ce qui devrait tourner — c'est une décision de
# `check_network` à partir des capabilities. On rapporte ce qu'on trouve.
#
# 🚨 UNIQUEMENT DES UNITÉS QUE LE DÉPÔT LIVRE (`config/systemd/`). `systemctl show` répond
#    pour n'importe quel nom, même inventé, en le rendant `inactive/dead` : interroger une
#    unité qui n'est pas du produit produit donc un faux service mort dans chaque instantané
#    du parc. `ben-recognizer` y figurait — c'est une EXPÉRIMENTATION, présente sur un seul
#    boîtier, absente du dépôt. Un banc structurel vérifie désormais cette liste contre
#    `config/systemd/`, pour qu'elle ne redérive pas.
#
# ⚠️ Et seulement les services DURABLES : les oneshots (`ben-network-check`,
#    `ben-network-recovery`, `ben-update`, `ben-ble-provisioner`) sont `inactive/dead` entre
#    deux exécutions par construction — leur état n'apprend rien.
UNITS = ("ben-radio", "ben-telemetry", "ben-tic-reader", "ben-publisher",
         "ben-local-api", "ben-certd", "wifi-watchdog")

VAR = "/var/lib/ben-firmware"
DB_PATH = f"{VAR}/measurements.db"

# ⚠️ Un délai PAR SONDE, et pas seulement un budget global : le coût de `journalctl` VARIE
#    beaucoup — 1 151 ms au premier appel (journal froid), 117 ms ensuite. Sans garde
#    individuelle, une seule commande lente mangerait le budget de toutes les autres.
PROBE_TIMEOUT_S = 4.0

# 🚨 20 s, ET CE N'EST PAS DE LA GÉNÉROSITÉ : le coût VARIE de 2 à 10 s sur LE MÊME boîtier,
#    et rien ne permet de prédire lequel. Trois passages consécutifs ont donné 2 029, 1 998 et
#    2 088 ms, quand le même code venait d'en mettre 9 655 quelques minutes plus tôt — carte SD
#    occupée, charge à 1,9. Le coût de `journalctl` dépend de l'état du cache du journal, et
#    l'instantané ne tourne QU'UNE FOIS PAR JOUR : il est donc toujours du mauvais côté.
#
# ⚠️ Avec 12 s, la marge était d'une seconde et demie sur le pire cas observé — et c'est
#    `errors`, la DERNIÈRE sonde, qui aurait été sacrifiée la première. Soit précisément le
#    champ qui porte les erreurs de service et de noyau.
#
# ⓘ Ce que coûte un budget plus large : le hello part AVANT la boucle de publication, donc la
#   collecte retarde d'autant le premier lot après un redémarrage du publisher. 20 s une fois
#   par jour, contre une cadence de croisière de 60 s, est dans le bruit.
BUDGET_S = 20.0

N_ERRORS = 8        # les N dernières lignes de priorité <= 3
N_FRAMES = 20       # les N dernières trames LoRa : l'état du lien AU MOMENT où il meurt
# 🚨 40, ET PAS 5. Mesuré sur ben-0012 le 2026-10-01 : un simple redémarrage systemd émet
#    à lui seul CINQ lignes — `Stopped`, `Consumed … CPU time`, `Started`, puis les deux
#    `INFO` de démarrage du publisher. Or la sonde ne tourne QUE là, au hello qui suit le
#    redémarrage de l'OTA. Avec 5, elle ne remontait donc jamais rien d'autre que la bannière
#    du redémarrage — aveugle au seul moment où elle s'exécute, et aveugle précisément aux
#    lignes de l'ANCIEN processus, qui sont sa raison d'être.
#    40 lignes couvrent ~40 min d'un publisher en échec (une ligne par tentative) pour ~3,5 Ko,
#    contre un plafond serveur de 16 Ko.
N_PUB = 40          # les N dernières lignes du journal de ben-publisher

# Borne du comptage des non envoyés — voir `store()`.
#
# 🚨 ON LIT LA MÊME VARIABLE D'ENVIRONNEMENT QUE `ben_publisher.BATCH`, au lieu d'importer le
#    module : `health` est importé PAR `ben_publisher`, l'importer en retour serait circulaire.
#    Et la valeur DOIT être celle du lot, pas une constante à part — la question posée est
#    « le prochain lot serait-il PLEIN ? », pas « combien y en a-t-il au total ».
N_UNSENT = int(os.environ.get("BEN_PUB_BATCH", "1000"))

# Le compte rendu laissé par `db_rebuild.py`. Absent partout sauf sur un boîtier réparé.
RAPPORT_REBUILD = f"{VAR}/db-rebuild.json"

# Budget d'OCTETS de la sonde `pub`, en plus de sa borne en lignes.
#
# 🚨 UNE BORNE EN LIGNES EST LA MAUVAISE UNITÉ. Les messages du publisher vont de 40 caractères
#    (« rien à envoyer · reste ~43 ») à 200, la troncature de `msg[:200]`. Le pire cas de 40
#    lignes est donc 40 × ~228 o = 9,1 Ko, auxquels s'ajoutent ~5 Ko pour le reste de
#    l'instantané : on franchit les 16 Ko que le serveur accepte.
#
# ⚠️ ET CE N'EST PAS `pub` QU'ON PERDRAIT, C'EST TOUT. `retenirSante` (ben-api) écarte le champ
#    `health` ENTIER au-delà de la borne — « trop volumineuse ». L'échec est donc asymétrique :
#    une sonde trop bavarde emporte les dix-neuf autres.
#
# ⭐ On garde donc les lignes les PLUS RÉCENTES jusqu'à épuisement du budget. 6 Ko laissent
#    ~10 Ko au reste, qui en consomme aujourd'hui 3,1 (ben-0001) et au pire ~5.
PUB_BUDGET_O = 6000


# 🚨 L'ÉCHÉANCE GLOBALE, et c'est elle qui rend le budget RÉEL.
#
#    Première version : on testait le budget AVANT chaque sonde. Insuffisant — une sonde peut
#    ensuite courir `PROBE_TIMEOUT_S` par SOUS-APPEL, et `errors()` en fait deux. Le pire cas
#    était donc 12 + 8 = 20 s, pas 12. Mesuré sur un boîtier du parc : une collecte à 12,59 s
#    alors que le budget était de 12 — et le préflight l'a refusée, à juste titre.
#
# ⭐ Chaque commande externe borne maintenant son délai au MINIMUM de son propre plafond et de
#    ce qui reste. La durée totale est donc réellement majorée par BUDGET_S.
_deadline = 0.0


def _left() -> float:
    """Ce qui reste du budget. `inf` quand aucune échéance n'est posée (appel direct d'une
    sonde, par exemple depuis un banc)."""
    return float("inf") if _deadline == 0.0 else _deadline - time.monotonic()


def _sh(*cmd: str) -> str:
    """Une commande externe, bornée par son plafond ET par l'échéance globale.
    Rend '' sur n'importe quel échec."""
    timeout = min(PROBE_TIMEOUT_S, _left())
    if timeout <= 0:
        return ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:  # noqa: BLE001 — y compris TimeoutExpired et FileNotFoundError
        return ""


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return ""


def _json(path: str) -> dict:
    try:
        d = json.loads(_read(path) or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ── Les sondes. Chacune rend son morceau, ou rien si elle n'a rien à dire ────

def host() -> dict:
    """Le socle : le boîtier redémarre-t-il en boucle, manque-t-il de place, chauffe-t-il."""
    out: dict = {}
    up = _read("/proc/uptime").split()
    if up:
        try:
            out["up"] = int(float(up[0]))
        except ValueError:
            pass
    for line in _read("/proc/stat").splitlines():
        if line.startswith("btime"):
            p = line.split()
            if len(p) > 1 and p[1].isdigit():
                out["boot"] = int(p[1])
    t = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    if t.lstrip("-").isdigit():
        out["temp"] = int(t) // 1000
    la = _read("/proc/loadavg").split()
    if len(la) >= 3:
        # ⭐ `load` est DANS le paquet, et ce n'est pas décoratif : un « 2,4 s » de collecte
        #    mesuré sur un boîtier au repos ne dit rien d'un boîtier en difficulté — et un
        #    boîtier en difficulté est précisément celui dont on lira l'instantané.
        try:
            out["load"] = [float(x) for x in la[:3]]
        except ValueError:
            pass
    mem, cles = {}, {"MemTotal": "total", "MemAvailable": "free", "SwapFree": "swap"}
    for line in _read("/proc/meminfo").splitlines():
        p = line.split()
        if len(p) >= 2 and p[0][:-1] in cles and p[1].isdigit():
            mem[cles[p[0][:-1]]] = int(p[1]) // 1024
    if mem:
        out["mem"] = mem
    # Le noyau teinté, en masque de bits brut — on stocke le FAIT, on décode à la lecture.
    #
    # ⚠️ LE PLANCHER D'UN RASPBERRY PI EST 1024, ET CE N'EST PAS UN INCIDENT. Vérifié sur un
    #    boîtier du parc : bit 10 = `C`, « un pilote staging a été chargé », et le noyau nomme
    #    les coupables — `snd_bcm2835`, `vc_sm_cma`, `bcm2835_mmal_vchiq`, `bcm2835_isp`.
    #    C'est l'état normal de Raspberry Pi OS, donc 1024 sur les sept boîtiers.
    #
    # ⭐ CE QUI COMPTE EST DONC TOUT BIT AU-DELÀ DE CE PLANCHER :
    #
    #        128 (bit  7) D  le noyau est MORT récemment (OOPS/BUG) — vécu sur un boîtier
    #        512 (bit  9) W  le noyau a émis un WARNING
    #      16384 (bit 14) L  SOFT LOCKUP — la signature même d'un SPI figé
    #         16 (bit  4) M  machine check exception
    #
    #    Donc 1152 = Pi normal + oops noyau. ⚠️ Une version antérieure de ce commentaire
    #    annonçait « 1024 = TAINT_WARN » : c'est FAUX, TAINT_WARN est le bit 9 (512). La
    #    valeur lue n'a jamais changé, seule l'interprétation était erronée.
    #
    # ⓘ Le drapeau est PERMANENT : c'est ce qui a fait de lui un mauvais signal de santé en
    #   0.9.12, où il pilotait un test périodique — 208 redémarrages de `ben-radio`, 46 % des
    #   mesures perdues. On le RAPPORTE, on ne décide rien avec. 1,5 ms.
    tainted = _read("/proc/sys/kernel/tainted").strip()
    if tainted.isdigit():
        out["tainted"] = int(tainted)
    try:
        s = os.statvfs(VAR)
        out["disk_mb"] = int(s.f_bavail * s.f_frsize / 1048576)
    except Exception:  # noqa: BLE001
        pass
    return out


def wifi() -> dict | None:
    """⭐ Jamais regardé jusqu'ici, et c'est DIRECTEMENT la question des « fenêtres de
    connectivité » : un lien WiFi faible explique des coupures qu'on attribuait au fournisseur.
    7,9 ms via `/proc/net/wireless`, sans aucune commande externe."""
    lines = _read("/proc/net/wireless").splitlines()
    if len(lines) < 3:
        return None
    p = lines[-1].split()
    if len(p) < 4:
        return None
    try:
        return {"iface": p[0].rstrip(":"),
                "qual": float(p[2].rstrip(".")),
                "rssi": float(p[3].rstrip("."))}
    except ValueError:
        return None


def repo(path: str = "/opt/ben/repo") -> dict | None:
    """🚨 Un dépôt SALE bloque le `git checkout` NU de l'OTA — piège documenté dans CLAUDE.md,
    qu'on ne pouvait jusqu'ici constater qu'en SSH. Et `tag` peut différer de la version
    déclarée dans `device.json` si une update a échoué entre le checkout et le bump.

    ⚠️ C'est la sonde la plus chère de tout l'instantané : ~1,15 s pour les deux appels git.
    Elle les vaut, une fois par jour, parce qu'elle est le SEUL moyen de voir à distance un
    boîtier dont l'OTA ne passera plus jamais.

    🚨 `--no-optional-locks` N'EST PAS UNE OPTION DE CONFORT : SANS LUI, CETTE SONDE PEUT
       BLOQUER L'OTA DU BOÎTIER DÉFINITIVEMENT.

       `git status` prend `.git/index.lock` pour rafraîchir l'index au passage. Or `_sh` tue
       la commande par SIGKILL quand le délai expire — et `repo` passe après `host`, `store`
       et `radio`, donc avec moins d'une seconde devant elle sur un boîtier chargé. Un git
       tué n'a aucun gestionnaire de nettoyage : **le fichier de verrou reste**.

       Après quoi, vérifié sur un boîtier du parc (git 2.47.3) :

           git checkout t1         → fatal: Unable to create '.git/index.lock': File exists.
           git status --porcelain  → (vide, SUCCÈS)

       ⇒ `update_lib.checkout_tag` est en `check=True`, donc `check_update.py` sort en 1,
         `device.json` n'est pas bumpé, et l'update REJOUE toutes les 10 min POUR TOUJOURS.
         Plus aucune version n'atteint ce boîtier jusqu'à une intervention en SSH.

    🚨 ET LE PIRE : la sonde SURVIT au verrou qu'elle a posé. `git status` réussit quand
       même, donc elle continuerait à rapporter `dirty: false` et le bon `tag` pendant que
       toutes les OTA échouent — elle MASQUERAIT précisément le blocage qu'elle existe pour
       révéler. Un garde-fou plus destructeur que la panne qu'il traite est un défaut, pas
       une protection (la leçon du verrou taint de 0.9.12, sous une autre forme).
    """
    tag = _sh("git", "--no-optional-locks", "-C", path,
              "describe", "--tags", "--always").strip()
    if not tag:
        return None
    dirty = _sh("git", "--no-optional-locks", "-C", path, "status", "--porcelain")
    return {"tag": tag, "dirty": bool(dirty.strip())}


def store(conn: sqlite3.Connection, db_path: str = DB_PATH) -> dict:
    """Ce que le boîtier a mesuré, et ce qu'il lui reste à envoyer.

    ⚠️ `max(ts)` se lit PAR PDL (`WHERE pdl_index = ?`) : 100 à 600× d'écart mesuré sur Pi Zero
       entre cette forme et un balayage global.
    ⚠️ Le retard s'encadre par les `rowid` — un `count(*) WHERE sent=0` prend **37 secondes**.
    """
    out: dict = {}
    size = {}
    for key, suffix in (("mb", ""), ("wal_mb", "-wal")):
        try:
            size[key] = round(os.path.getsize(f"{db_path}{suffix}") / 1048576, 1)
        except Exception:  # noqa: BLE001
            pass
    if size:
        out["db"] = size

    try:
        indexes = [i for i, in conn.execute("SELECT pdl_index FROM pdl ORDER BY pdl_index")]
    except sqlite3.Error:
        indexes = []
    meters = []
    for i in indexes:
        entry = {"i": i}
        try:
            entry["last_ts"] = conn.execute(
                "SELECT max(ts) FROM measurements WHERE pdl_index = ?", (i,)).fetchone()[0]
        except sqlite3.Error:
            pass
        meters.append(entry)
    if meters:
        out["pdl"] = meters

    try:
        top, low = conn.execute(
            "SELECT (SELECT max(rowid) FROM measurements), "
            "       (SELECT min(rowid) FROM measurements WHERE sent = 0)").fetchone()
        out["pending"] = 0 if top is None or low is None else max(0, top - low + 1)
    except sqlite3.Error:
        pass

    # ⭐ LE CHIFFRE QUE `pending` NE PEUT PAS DONNER, et c'est ce qui a coûté neuf jours de
    #    diagnostic faux sur un boîtier du parc.
    #
    #    `pending` ENCADRE par les rowid ; il ne COMPTE pas. Il prouve OÙ se trouve la plus
    #    vieille ligne non envoyée — rien de plus. Une seule ligne restée à `sent = 0` sur un
    #    vieux rowid suffit à annoncer un demi-million de points en attente alors qu'il n'y en
    #    a qu'un. ⇒ On a lu « 566 248 en attente » et on en a déduit « un lot plein part à
    #    chaque tour, donc il échoue », sans qu'aucune mesure ne l'étaye.
    #
    # ⇒ `unsent` répond à la SEULE question qui sépare les deux cas :
    #
    #      = N_UNSENT   un lot PLEIN existe, donc un POST est tenté, donc il ÉCHOUE
    #                   → la raison est dans `pub`
    #      = 0          il n'a RIEN à envoyer, donc aucun POST, donc aucun échec
    #                   → `pending` est un artefact, et la question devient : pourquoi des
    #                     lignes sont-elles `sent = 1` alors que le cloud ne les a pas ?
    #      entre        il envoie des lots PARTIELS — un troisième cas, qu'on ne voyait pas
    #
    # ⚠️ BORNÉ, et c'est tout ce qui le rend possible : un `count(*) WHERE sent = 0` nu balaie
    #    les millions d'entrées de l'index et prend 37 s sur un Pi Zero. Avec `LIMIT`, SQLite
    #    s'arrête à la borne : mesuré 1,8 ms sur ben-0001 et 2,2 ms sur ben-0003, borne
    #    RÉELLEMENT atteinte, par index COUVRANT (`idx_meas_sent` suffit, aucun accès table).
    try:
        out["unsent"] = conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM measurements WHERE sent = 0 LIMIT ?)",
            (N_UNSENT,)).fetchone()[0]
    except sqlite3.Error:
        pass

    # `emitter` était au cœur des PDL fantômes : quel compteur au bout de quel émetteur, et
    # depuis quand. Vide sur un boîtier filaire — l'absence est normale.
    #
    # ⭐ `fw` = LA VERSION DU FIRMWARE DE L'ÉMETTEUR, et c'est la seule façon de la connaître
    #    à distance (#28) : le satellite se livre par reflash PHYSIQUE, sa version n'était
    #    donc lisible qu'au banner série, un FTDI en main, devant le boîtier.
    #
    # 🚨 UNE LIGNE PAR ÉMETTEUR, PAS UN CHAMP DE BOÎTIER. La version appartient au satellite,
    #    donc au compteur qu'il lit — et un boîtier peut en écouter PLUSIEURS. Un champ unique
    #    à côté de `sw` aurait été faux dès le second émetteur, et faux en silence.
    #
    # ⚠️ Trois états, et le troisième n'est pas « inconnu » : une version · la constante
    #    `anterieur-campagne` (trame de boot reçue SANS le TLV ⇒ émetteur < 0.1.10, il reste à
    #    reflasher) · `null` (aucune trame de boot vue depuis l'OTA — l'émetteur ne redémarre
    #    pas quand le Pi redémarre). Cf. le commentaire de la colonne dans `db.py`.
    #
    # ⓘ `SELECT` nommé et non `*` : la colonne est arrivée après les quatre autres, et un
    #   `*` ferait dépendre l'ordre du tuple de l'âge de la base.
    try:
        emitters = [{"addr": a, "adco": c or "", "pdl": p, "ts": t, "fw": f}
                    for a, c, p, t, f in conn.execute(
                        "SELECT lora_addr, adco, pdl_index, updated_ts, fw_version "
                        "FROM emitter")]
        if emitters:
            out["emitter"] = emitters
    except sqlite3.Error:
        pass

    try:
        out["events_pending"] = conn.execute(
            "SELECT count(*) FROM event WHERE sent = 0").fetchone()[0]
    except sqlite3.Error:
        pass

    # ⭐ LA SONDE CANARI — celle qui aurait trouvé ben-0012 LE PREMIER JOUR.
    #
    #    Le 2026-10-01, un boîtier du parc a passé neuf jours sans publier, avec une zone de
    #    `measurements` ILLISIBLE (`database disk image is malformed`). Rien ne le signalait,
    #    parce que TOUS les champs de cet instantané qui touchent `measurements` sont servis
    #    par un INDEX : `pending`, `unsent`, `last_ts`. Les index étaient intacts. Seule la
    #    lecture d'une LIGNE COMPLÈTE échouait — et personne ne la demandait.
    #
    # 🚨 J'avais écarté `PRAGMA quick_check` parce qu'il est RUINEUX (charge 4,50 sur un
    #    boîtier du parc, `sshd` muet, jamais fini en 180 s). La décision restait juste — mais
    #    j'ai écarté la CATÉGORIE au lieu de chercher la forme BORNÉE. Elle existe, et elle
    #    coûte des microsecondes : demander DEUX lignes.
    #
    #      0,56 ms   la plus vieille non envoyée — celle sur laquelle le publisher bute
    #      0,31 ms   la plus récente — celle que le lecteur vient d'écrire
    #
    #    ⓘ Le plan de la seconde annonce `SCAN measurements`, ce qui serait normalement
    #      alarmant : `ORDER BY rowid DESC LIMIT 1` descend directement à la feuille la plus à
    #      DROITE. Les 0,31 ms mesurés le confirment.
    #
    # ⭐ Et les deux ensemble disent OÙ est le dommage : la vieille lève et la récente passe
    #   ⇒ dégât CONFINÉ dans les pages anciennes, réparable par recopie. Les deux lèvent
    #   ⇒ tout le fichier est touché.
    #
    # ⚠️ ÉMIS MÊME QUAND TOUT VA BIEN (`{"old": "ok", "new": "ok"}`, ~30 o). Un champ qui
    #    n'apparaît qu'en cas de panne est un champ qu'on oublie, et dont l'absence devient
    #    indiscernable du succès — exactement le défaut qui a rendu `errors` mort-né.
    # 🚨 `old` LIT LE LOT ENTIER, PAS UNE LIGNE — défaut trouvé en revue, et il rendait la
    #    sonde borgne là où elle doit voir. `fetch_batch` lit MILLE lignes, étalées sur une
    #    dizaine de pages : si la page détruite n'est pas celle de la plus vieille ligne non
    #    envoyée mais la 437ᵉ du lot, une lecture d'UNE ligne passe et la sonde annonce « ok »
    #    pendant que le publisher échoue à chaque tentative.
    #
    # ⚠️ Et c'est le cas LE PLUS PROBABLE : le dernier lot parti s'est arrêté juste avant la
    #    page abîmée, donc la frontière `sent = 0` tombe AVANT elle, pas dessus.
    #
    # ⭐ On exécute donc la requête du publisher à l'identique, `LIMIT` comprise, et on ITÈRE
    #   sans accumuler : mêmes pages touchées, aucune mémoire retenue. Mesuré 131 ms pour
    #   1 000 lignes sur un Pi Zero — une fois par hello.
    COLS = ("ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, src_standard, "
            "index_id, index_value, inject_total, meter_ts")
    lecture = {}
    for cle, sql in (
            ("old", f"SELECT {COLS} FROM measurements WHERE sent = 0 "
                    f"ORDER BY rowid LIMIT {N_UNSENT}"),
            ("new", f"SELECT {COLS} FROM measurements ORDER BY rowid DESC LIMIT 1")):
        try:
            for _ in conn.execute(sql):
                pass
            lecture[cle] = "ok"
        except Exception as e:  # noqa: BLE001
            lecture[cle] = str(e)[:120]
    out["read"] = lecture

    # Le compte rendu de la dernière reconstruction, s'il y en a eu une. Absent partout
    # ailleurs — et c'est le seul moyen d'apprendre, depuis le cloud, ce qu'elle a perdu.
    try:
        with open(RAPPORT_REBUILD, encoding="utf-8") as f:
            out["rebuild"] = json.load(f)
    except (OSError, ValueError):
        pass
    return out


def radio(conn: sqlite3.Connection, pdl_indexes: list | None = None) -> dict | None:
    """⭐ LE CŒUR DU DIAGNOSTIC RADIO, et la seule façon de séparer deux pannes qui donnent
    sinon la MÊME signature — « l'émetteur est muet » et « le récepteur est sourd ».

    · `silence_restarts` est le discriminant : `ben-radio` le remet à 0 à chaque trame reçue et
      l'incrémente quand son détecteur de silence le relance. Après des jours sans trame il est
      donc GRAND si le récepteur écoute vraiment, et NUL si le détecteur n'a jamais joué — donc
      si c'est le récepteur qui est en cause.

    · ⭐⭐ `recent` est le diagnostic RÉTROACTIF, et c'est le vrai gain : `lora_link` garde 180
      jours de rssi/snr par trame. Les 20 dernières AVEC LEURS HORODATAGES coûtent 3,5 ms — et
      sur un boîtier muet depuis des jours, elles datent du jour de sa mort. Chute brutale à
      pleine puissance ⇒ alimentation (cf. la diode BAT85 de ben01). Dégradation progressive
      ⇒ antenne ou portée. **Aucun autre champ ne permet de trancher ça**, et l'information
      s'effacera à 180 jours.

    ⚠️ Rend None sur un boîtier filaire : pas de `radio-state.json`, pas de `lora_link`. C'est
       un état normal, pas une anomalie.
    """
    out: dict = {}
    state = _json(f"{VAR}/radio-state.json")
    if "last_frame_time" in state:
        try:
            out["last_frame"] = int(float(state["last_frame_time"] or 0))
        except (TypeError, ValueError):
            pass
    if "silence_restart_count" in state:
        try:
            out["silence_restarts"] = int(state["silence_restart_count"] or 0)
        except (TypeError, ValueError):
            pass

    lora = _json(f"{VAR}/lora-state.json")
    if lora:
        out["state"] = {k: lora.get(k) for k in
                        ("last_batch_seq", "last_boot_seq", "last_active_id")}
    tx = _json(f"{VAR}/radio-tx.json")
    if tx:
        out["tx"] = tx

    # 🚨 `WHERE pdl_index = ?`, TOUJOURS, et ce n'est pas une précaution de style.
    #
    #    Première version sans la clause : l'index couvrant `(pdl_index, ts)` devenait
    #    inutilisable et SQLite balayait puis triait les 145 000 lignes de `lora_link`.
    #    Mesuré sur un boîtier du parc : **6 249 ms contre 3,5 ms**, soit 1 800×. La sonde
    #    mangeait à elle seule la moitié du budget — et c'est la règle que le docstring de
    #    `store()` énonce vingt lignes plus haut.
    #
    # ⭐ Au passage, c'est AUSSI plus utile : par compteur, on sait LEQUEL a perdu son lien.
    # 🚨 LA SÉRIE, PAS UN AGRÉGAT — sinon ce champ ne répond pas à la question qui le
    #    justifie. Tout l'intérêt est de distinguer une CHUTE BRUTALE (alimentation, cf. la
    #    diode BAT85 de ben01) d'une DÉGRADATION PROGRESSIVE (antenne, portée). Or
    #    min/max/moyenne sur 20 trames rendent ces deux cas presque identiques :
    #
    #        chute   : -65 ×19 puis -95   → min -95  max -65  moy -66,5
    #        déclin  : -65 … -95 linéaire → min -95  max -65  moy -80,0
    #
    #    Mêmes bornes, et seule la moyenne diffère — il faudrait savoir à quoi s'attendre
    #    pour la lire. L'ORDRE DANS LE TEMPS tranche sans ambiguïté, et il ne coûte rien.
    #
    # ⭐ En tableaux de tableaux, pas en objets : `[[ts, rssi, snr], …]` pèse trois fois
    #    moins que la même chose avec des clés répétées vingt fois. Les agrégats
    #    disparaissent — ils se recalculent depuis la série, l'inverse est faux.
    recent = []
    for i in (pdl_indexes or []):
        try:
            frames = [[ts, rssi, snr] for ts, rssi, snr in conn.execute(
                "SELECT ts, rssi, snr FROM lora_link WHERE pdl_index = ? "
                "ORDER BY ts DESC LIMIT ?", (i, N_FRAMES))]
        except sqlite3.Error:
            continue
        if frames:
            recent.append({"i": i, "frames": frames})
    if recent:
        out["recent"] = recent
    return out or None


def units() -> list | None:
    """⚠️ `systemctl show --property=`, JAMAIS `systemctl status` : celui-ci rend de la prose
    localisée et PAGINE par défaut — sans `--no-pager` un script pendrait.
    ⚠️ Et `--timestamp=unix` est obligatoire, sinon `ExecMainStartTimestamp` sort en
       « Mon 2026-09-28 17:23:48 CEST », du texte localisé inutilisable."""
    raw = _sh("systemctl", "show", "--timestamp=unix", "--no-pager",
              "--property=Id,LoadState,ActiveState,SubState,NRestarts,"
              "ExecMainStartTimestamp",
              *[u + ".service" for u in UNITS])
    if not raw:
        return None
    blocks, cur = [], {}
    for line in raw.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        blocks.append(cur)

    out = []
    for b in blocks:
        name = b.get("Id", "").removesuffix(".service")
        if not name:
            continue
        entry = {"n": name, "a": b.get("ActiveState"), "s": b.get("SubState")}
        # ⭐ `LoadState` seulement quand il n'est PAS « loaded », et c'est volontaire :
        #    `systemctl show` répond pour une unité qui n'existe pas, en la rendant
        #    `inactive/dead` — indistinguable d'un service arrêté. Or l'écart est une
        #    information : `ben-recognizer` est `loaded` sur un boîtier du parc et
        #    `not-found` sur un autre, alors qu'il n'est dans AUCUN tag du dépôt. C'est
        #    de la DÉRIVE DE PARC, et la masquer (en retirant l'unité de la liste ou en
        #    filtrant les not-found) nous priverait du seul moyen de la voir.
        load = b.get("LoadState")
        if load and load != "loaded":
            entry["load"] = load
        r = (b.get("NRestarts") or "").strip()
        if r.isdigit():
            # 🚨 `NRestarts` est le champ qui aurait crié en 0.9.12 : 208 redémarrages.
            entry["r"] = int(r)
        since = (b.get("ExecMainStartTimestamp") or "").lstrip("@").strip()
        if since.isdigit():
            entry["since"] = int(since)
        out.append(entry)
    return out or None


def errors() -> list | None:
    """Les dernières lignes de priorité <= 3, services ET NOYAU.

    ⭐ Le noyau n'est pas un bonus : c'est là qu'apparaissent les blocages SPI et les
    sous-tensions, soit exactement les pannes d'un boîtier radio devenu muet.

    🚨 PAS de `--since` : voir l'en-tête du module. On rend l'horodatage de chaque ligne,
    c'est le LECTEUR qui juge si elle est vieille.

    🚨 ET PAS DE `-u` NON PLUS, ce qui est contre-intuitif. Mesuré sur un boîtier du parc :

        journalctl -p 3 -n 8 -o json                  0,36 s
        journalctl -p 3 -n 8 -o json -u ben-radio     7,93 s   ← vingt fois plus

    Filtrer par unité force journald à BALAYER tout le journal quand cette unité n'a aucune
    entrée de priorité 3 — le cas normal d'un boîtier sain. Sans `-u` on lit la queue, et
    `_SYSTEMD_UNIT` est de toute façon dans le JSON : on sait toujours d'où vient chaque ligne,
    y compris des unités qu'on n'aurait pas pensé à lister. Moins cher ET plus large."""
    out = []
    # ⚠️ Le PREMIER appel est sans `-k`, mais il inclut QUAND MÊME les lignes noyau : sans
    #    filtre, chaque erreur noyau apparaissait DEUX FOIS (une sous `journal`, une sous
    #    `kernel`) et consommait les créneaux du premier appel. Observé sur un boîtier du
    #    parc : les trois dernières lignes de priorité 3 étaient TOUTES du WiFi noyau, donc
    #    aucune erreur de service ne pouvait remonter. On en demande donc PLUS au premier
    #    appel et on écarte le noyau à la lecture — journalctl n'a pas de négation de champ.
    for source, extra, limite in (("journal", [], N_ERRORS * 2), ("kernel", ["-k"], 5)):
        raw = _sh("journalctl", "-p", "3", "-n", str(limite), "--no-pager",
                  "-o", "json", *extra)
        garde = 0
        for line in raw.splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            msg = d.get("MESSAGE")
            if not isinstance(msg, str):
                continue        # un MESSAGE binaire existe ; il n'a rien à faire dans du JSON
            if source == "journal":
                if d.get("_TRANSPORT") == "kernel":
                    continue    # le second appel s'en occupe, et mieux
                if garde >= N_ERRORS:
                    continue
                garde += 1
            try:
                ts = int(int(d.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000)
            except (TypeError, ValueError):
                ts = 0
            out.append({"u": (d.get("_SYSTEMD_UNIT") or source).removesuffix(".service"),
                        "t": ts, "m": msg[:200]})
    return out or None


def publisher() -> list | None:
    """Les dernières lignes du journal de `ben-publisher`, SANS filtre de priorité.

    🚨 POURQUOI, ALORS QUE `errors()` REMONTE DÉJÀ LA PRIORITÉ 3 — c'est une question de
       MOMENT, et elle a été tranchée par le terrain.

       Le hello part JUSTE APRÈS le redémarrage du publisher par l'OTA (`check_update.py`,
       étape 10). À cet instant le nouveau processus a `echecs = 0` : aucune ligne en
       priorité 3 n'existe encore, et il faudra ~1 minute d'échecs pour qu'elle apparaisse.
       Quant aux lignes de l'ANCIEN processus, elles sont en `PRIORITY=6` sur tout boîtier
       antérieur à 0.9.23.

       ⇒ Sans cette sonde, la cause d'une panne de publication n'arrive qu'au hello SUIVANT,
         donc sous 24 h. Avec, elle arrive au PREMIER — quelques minutes après l'OTA.

    ⭐ Et sur un boîtier SAIN ses lignes INFO sont elles-mêmes le diagnostic :

        [INFO] envoyé 86 · inséré 79 · reste ~0

       `inséré` y dit ce que `pending` ne dit pas : si le serveur a vraiment retenu les points
       ou s'il les absorbe comme doublons.

    ⚠️ SANS FILTRE DE PRIORITÉ, ET C'EST CE QUI LA REND SÛRE. Mesuré sur un boîtier du parc :

        -u ben-publisher -n 5                  2,94 s   ← retenu
        -u ben-publisher -p 4 -n 10            0,57 s
        -u ben-radio     -p 3 -n 8             7,93 s   🚨
        -p 3 -n 8        (sans -u)             0,36 s

       🚨 `-u` ne dégénère en balayage complet que s'il n'y a AUCUNE correspondance : journald
          parcourt alors tout le journal pour n'en trouver aucune. C'est de là que venaient les
          7,93 s — `-u ben-radio -p 3` sur une unité sans erreur. Sans filtre de priorité, les
          lignes INFO du publisher garantissent toujours une correspondance, donc la lecture
          reste une lecture de QUEUE. ⇒ Ajouter `-p 4` serait plus rapide sur un boîtier bavard
          et ruineux sur un boîtier silencieux ; ne rien filtrer est le choix sûr.

    ⚠️ C'est la sonde la plus lente de l'instantané ; elle passe donc EN DERNIER, et l'échéance
       globale la sacrifie en premier sur un boîtier en difficulté.

    🚨 ET IL FAUT BEAUCOUP DE LIGNES — c'est le défaut de sa première version. `N_PUB = 5` a
       été démenti par ben-0012 le jour même de sa livraison : le redémarrage systemd émet cinq
       lignes à lui seul, qui ont évincé tout le reste. Une sonde qui ne tourne QU'APRÈS un
       redémarrage doit donc lire bien au-delà de la bannière de ce redémarrage. Voir `N_PUB`.
    """
    raw = _sh("journalctl", "-u", "ben-publisher", "-n", str(N_PUB), "--no-pager", "-o", "json")
    out = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        msg = d.get("MESSAGE")
        if not isinstance(msg, str):
            continue
        try:
            ts = int(int(d.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000)
        except (TypeError, ValueError):
            ts = 0
        out.append({"t": ts, "m": msg[:200]})

    # ⭐ On rogne par la TÊTE : les lignes les plus récentes sont celles qui expliquent la panne
    #    en cours. `journalctl -n` rend déjà du plus ancien au plus récent, donc on retire du
    #    début. Le coût d'une ligne JSON est sa longueur plus ~28 o d'habillage
    #    (`{"t":1790855252,"m":""},`) — on mesure l'habillage réel plutôt que de le deviner.
    budget = PUB_BUDGET_O
    garde = []
    for e in reversed(out):
        cout = len(e["m"].encode("utf-8", "replace")) + 28
        if garde and budget - cout < 0:
            break
        budget -= cout
        garde.append(e)
    garde.reverse()
    return garde or None


# Les clés de `device.json` qui PRÉTENDENT dire la version du firmware de l'émetteur. Elles
# ne la disent pas, et ne peuvent pas la dire. Cf. `_sans_fw_emetteur`.
FW_EMETTEUR_MENTEUR = ("fw",)


def _sans_fw_emetteur(caps):
    """Les capabilities, DÉBARRASSÉES du champ `fw` qui prétendait dire la version du
    firmware de l'émetteur.

    🚨 POURQUOI ON COUPE À L'ÉMISSION PLUTÔT QU'À LA SOURCE. `caps_for_model` n'écrit plus
    ce champ (#28), mais ça ne vaut que pour un provisioning NEUF : les `device.json` déjà
    posés sur le parc portent toujours `"fw": "0.1.3"` — une valeur fausse depuis des mois,
    les émetteurs étant en 0.1.8 et plus. Tant qu'on la transmet, le cloud lit un chiffre
    inventé à côté du chiffre mesuré, et c'est le faux qui a l'air officiel.

    ⭐ ET ON NE RÉÉCRIT PAS `device.json` POUR AUTANT. Ce fichier est réécrit par l'agent
    d'OTA (il y bumpe la version), le toucher depuis un `update.sh` est la manœuvre qui a
    déjà coûté des tours de boucle, et le champ n'est lu par PERSONNE d'autre que cette
    fonction : aucun accesseur `fw()` n'existe dans `capabilities.py`. Le supprimer là où il
    est consommé le supprime donc partout où il avait un effet, sans toucher au disque.

    ⓘ La vraie valeur voyage dans `health.emitter[].fw`, une ligne par émetteur, mesurée.
    """
    if isinstance(caps, dict):
        return {c: ({k: v for k, v in a.items() if k not in FW_EMETTEUR_MENTEUR}
                    if isinstance(a, dict) else a)
                for c, a in caps.items()}
    return caps


def versions(dev: dict) -> dict | None:
    """Ce que le boîtier CROIT être. À recouper avec `repo.tag` : les deux divergent si une
    update a échoué entre le checkout et le bump de `device.json`.

    🚨 `arduinoFirmwareVersion` A DISPARU D'ICI, pour la raison exacte qui a fait disparaître
    `caps[*].fw` : c'était la MÊME affirmation, tenue à la main, sur la version d'un firmware
    qui se livre par reflash PHYSIQUE. `install.sh` ne l'écrit plus depuis la bascule vers les
    capabilities (« fini le champ arduinoFirmwareVersion »), mais la sonde le lisait encore —
    donc un vieux `device.json` continuait de faire monter une version d'émetteur inventée.
    En garder une seule des deux n'aurait rien réglé : il suffit d'UNE source qui prétend pour
    que le chiffre mesuré soit noyé.
    """
    if not dev:
        return None
    out = {k: v for k, v in (("model", dev.get("model")),
                             ("sw", dev.get("softwareVersion"))) if v}
    # ⚠️ `capabilities` est un DICT dans `device.json` — `{"lora": {"hw": "rev01"},
    #    "lora-tic-receiver": {"hw": "rev01"}}` — et pas une liste. Ne garder que les listes
    #    faisait disparaître le champ en silence. On accepte les deux formes.
    caps = dev.get("capabilities")
    if isinstance(caps, (dict, list)) and caps:
        out["caps"] = _sans_fw_emetteur(caps)
    return out or None


# ── L'assemblage ─────────────────────────────────────────────────────────────

# Les sondes versées À PLAT dans le résultat : leur contenu est le socle, pas un sous-objet.
FLAT = ("host", "store")


def snapshot(conn: sqlite3.Connection | None, dev: dict | None = None,
             db_path: str = DB_PATH) -> dict:
    """L'instantané complet. **NE LÈVE JAMAIS.**

    ⭐ `collect_ms` est dans le résultat au même titre que le reste : sans lui, on ne sait pas
    interpréter ce qu'on lit. Un instantané pris en 2,4 s sur un boîtier au repos et un pris en
    30 s sur un boîtier en détresse se ressemblent, et ne disent pas la même chose.

    ⚠️ Un champ ABSENT veut dire « je n'ai pas pu », jamais « ça vaut zéro ». C'est pour ça
    qu'aucune sonde n'invente de valeur de repli.
    """
    global _deadline
    start = time.monotonic()
    _deadline = start + BUDGET_S
    out: dict = {}

    # ⚠️ La liste des PDL se lit depuis la table `pdl` — JAMAIS par un
    #    `SELECT DISTINCT pdl_index FROM lora_link`, qui prend 16,7 s sur un Pi Zero.
    pdl_indexes: list = []
    if conn is not None:
        try:
            pdl_indexes = [i for i, in conn.execute(
                "SELECT pdl_index FROM pdl ORDER BY pdl_index")]
        except sqlite3.Error:
            pass

    probes = (("host", host),
              ("wifi", wifi),
              ("dev", lambda: versions(dev or {})),
              ("store", lambda: store(conn, db_path) if conn is not None else None),
              ("radio", lambda: radio(conn, pdl_indexes) if conn is not None else None),
              ("repo", repo),
              ("units", units),
              ("errors", errors),
              # ⚠️ EN DERNIER : la plus lente (~2,9 s), et celle dont l'absence coûte le
              #    moins — les autres champs disent déjà l'essentiel.
              ("pub", publisher))

    for name, probe in probes:
        if time.monotonic() - start >= BUDGET_S:
            # On le DIT plutôt que de rendre un instantané amputé en silence.
            out["truncated_at"] = name
            break
        try:
            value = probe()
        except Exception:  # noqa: BLE001 — une sonde ne peut pas coûter le hello
            continue
        if not value:
            continue
        if name in FLAT:
            out.update(value)
        else:
            out[name] = value

    _deadline = 0.0
    out["collect_ms"] = int((time.monotonic() - start) * 1000)
    return out


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        conn = None
    device = {}
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import capabilities as caps
        device = caps.load_device() or {}
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(snapshot(conn, device, path), indent=1, ensure_ascii=False))
