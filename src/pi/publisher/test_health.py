#!/usr/bin/env python3
"""Banc de l'instantané de santé (#16).

🚨 CE QUI EST ÉPROUVÉ ICI N'EST PAS LE CONTENU, C'EST L'INNOCUITÉ.

Cet instantané voyage dans le hello, et le hello est ce qui porte les compteurs, les époques
tarifaires et les libellés des sept boîtiers. **Une sonde qui lève ferait échouer le hello
entier** — donc perdrait des métadonnées pour un champ de diagnostic. Et comme la collecte
touche `/proc`, `systemctl`, `journalctl`, `git` et quatre fichiers d'état, elle a beaucoup
d'occasions de lever sur un boîtier en difficulté : exactement celui dont on veut l'instantané.

⭐ Le banc tourne donc SUR UN MAC, où `/proc` n'existe pas, où `systemctl` est introuvable et où
`/var/lib/ben-firmware` est absent. Ce n'est pas un pis-aller — c'est le cas de panne, gratuit.

    python3 src/pi/publisher/test_health.py
"""
import json
import pathlib
import sqlite3
import sys
import time

R = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(R.parent / "store"), str(R.parent), str(R)]

import db  # noqa: E402
import health  # noqa: E402

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


def base_radio() -> sqlite3.Connection:
    """Un boîtier LoRa qui mesure : un compteur, des mesures, un émetteur, des trames."""
    c = db.connect(":memory:")
    c.execute("INSERT INTO pdl (pdl_index, adco, first_seen, last_seen) VALUES (0,'031864467282',1,9)")
    # ⓘ Colonnes NOMMÉES : `VALUES(...)` positionnel se casse à chaque colonne ajoutée
    #   (`fw_version` est arrivée avec #28), et le banc ne dirait alors rien de son sujet.
    c.execute("INSERT INTO emitter (lora_addr, adco, pdl_index, updated_ts, fw_version) "
              "VALUES (31,'031864467282',0,1790786260,'0.1.11')")
    for i in range(50):
        c.execute("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,1)",
                  (1790840000 + i, 200 + i))
    for i in range(30):
        c.execute("INSERT INTO lora_link(ts,pdl_index,rssi,snr,sent) VALUES(?,0,?,?,1)",
                  (1790840000 + i * 40, -60 - i, 10.0))
    c.commit()
    return c


def base_nue() -> sqlite3.Connection:
    """Une base au schéma réel, SANS aucune mesure — les cas `unsent` les posent eux-mêmes,
    parce que c'est la RÉPARTITION de `sent` qu'ils éprouvent, pas la présence de données."""
    return db.connect(":memory:")


# ── L'innocuité, qui est tout l'enjeu ────────────────────────────────────────

@cas
def un_environnement_entierement_absent_ne_leve_pas():
    """🚨 LE CAS QUI PROTÈGE LE PARC. Sur ce Mac il n'y a ni `/proc`, ni `systemctl`, ni
    `journalctl`, ni `/var/lib/ben-firmware` — et `snapshot()` doit quand même rendre un dict.
    Si elle levait, le hello échouerait et on perdrait les métadonnées des sept boîtiers pour
    un champ de diagnostic."""
    out = health.snapshot(None, None, "/n/existe/pas.db")
    assert isinstance(out, dict), "snapshot() doit TOUJOURS rendre un dict"
    assert "collect_ms" in out, "même vide, l'instantané dit combien de temps il a pris"


@cas
def une_sonde_qui_leve_laisse_son_champ_ABSENT():
    """⚠️ Pas une valeur de repli : l'ABSENCE. Un champ à zéro se confondrait avec une vraie
    mesure à zéro — « 0 trame reçue » et « je n'ai pas pu compter » ne veulent pas dire la
    même chose, et c'est toute la différence entre un diagnostic et une devinette."""
    def explose():
        raise RuntimeError("sonde en vrac")

    vrai = health.units
    health.units = explose
    try:
        out = health.snapshot(None, None, "/n/existe/pas.db")
    finally:
        health.units = vrai
    assert "units" not in out
    assert "collect_ms" in out, "le reste de l'instantané part quand même"


@cas
def le_budget_borne_une_sonde_qui_PEND():
    """⚠️ Le vrai risque n'est pas qu'une sonde lève, c'est qu'elle PENDE : `journalctl` a été
    mesuré à 9 s avec `--since`, et `sshd` lui-même a cessé de répondre pendant qu'on faisait
    un `PRAGMA quick_check`. Un instantané qui bloque bloque le hello, donc la publication."""
    def lente():
        time.sleep(0.4)
        return {"x": 1}

    vrais = health.host, health.wifi, health.units, health.errors, health.repo
    health.host = health.wifi = health.units = health.errors = health.repo = lente
    budget = health.BUDGET_S
    health.BUDGET_S = 0.5
    try:
        t0 = time.monotonic()
        out = health.snapshot(None, None, "/n/existe/pas.db")
        duree = time.monotonic() - t0
    finally:
        health.host, health.wifi, health.units, health.errors, health.repo = vrais
        health.BUDGET_S = budget
    assert duree < 2.0, f"la collecte a duré {duree:.1f} s malgré un budget de 0,5 s"
    assert out.get("truncated_at"), "une collecte écourtée doit le DIRE, pas se taire"


@cas
def le_budget_borne_aussi_les_SOUS_APPELS():
    """🚨 LE DÉFAUT QUE LE PRÉFLIGHT A TROUVÉ SUR LA CIBLE, et que le cas précédent ne voyait
    pas — il détourne les sondes, donc jamais `_sh`.

    Première version : le budget était testé AVANT chaque sonde. Insuffisant — une sonde peut
    ensuite courir `PROBE_TIMEOUT_S` par SOUS-APPEL, et `errors()` en fait deux. Le pire cas
    réel était 12 + 8 = 20 s. Mesuré sur un boîtier du parc : une collecte à **12,59 s** pour
    un budget de 12 — `git` avait expiré à 4 s et les deux `journalctl` à 4 s chacun.

    ⚖️ Ce cas utilise une VRAIE commande externe : c'est le seul moyen de prouver que `_sh`
    borne son délai à ce qui RESTE du budget, et pas seulement à son propre plafond."""
    vrais = health.host, health.wifi, health.units
    health.host = health.wifi = lambda: None
    health.units = lambda: {"x": health._sh("sleep", "10")}   # 10 s de sommeil, deux fois
    budget, plafond = health.BUDGET_S, health.PROBE_TIMEOUT_S
    health.BUDGET_S, health.PROBE_TIMEOUT_S = 1.0, 10.0
    try:
        t0 = time.monotonic()
        health.snapshot(None, None, "/n/existe/pas.db")
        duree = time.monotonic() - t0
    finally:
        health.host, health.wifi, health.units = vrais
        health.BUDGET_S, health.PROBE_TIMEOUT_S = budget, plafond
    assert duree < 3.0, (
        f"la collecte a duré {duree:.1f} s pour un budget de 1 s : `_sh` ignore l'échéance")


@cas
def l_echeance_est_RELACHEE_apres_la_collecte():
    """⚠️ `_deadline` est un état de module. L'oublier posée ferait que le PROCHAIN appel
    direct d'une sonde — un banc, le mode ligne de commande — se croirait déjà hors budget et
    ne ferait plus rien, sans rien dire."""
    health.snapshot(None, None, "/n/existe/pas.db")
    assert health._deadline == 0.0
    assert health._left() == float("inf"), "hors collecte, une sonde n'a pas d'échéance"


@cas
def l_instantane_est_SERIALISABLE_en_JSON():
    """🚨 Il part dans le corps d'une requête JSON. Une valeur non sérialisable — un `Decimal`,
    un `datetime`, un `bytes` venu d'une colonne SQLite — ferait lever `json.dumps` DANS le
    publisher, donc échouer le hello. Le défaut serait invisible à la lecture du code."""
    out = health.snapshot(base_radio(), {"model": "Radio", "softwareVersion": "0.9.21"})
    json.dumps(out)      # lève si quoi que ce soit n'est pas sérialisable


# ── Le contenu, avec ses témoins ─────────────────────────────────────────────

@cas
def TEMOIN_un_boitier_qui_mesure_rend_son_dernier_horodatage():
    """⚖️ LE TÉMOIN, et il n'est pas décoratif : sans lui, des sondes qui rendraient toujours
    `None` passeraient tous les cas d'innocuité ci-dessus — et on aurait livré un instantané
    vide à sept boîtiers, en croyant les avoir instrumentés."""
    out = health.snapshot(base_radio(), None)
    assert out["pdl"] == [{"i": 0, "last_ts": 1790840049}], out.get("pdl")
    assert out["emitter"][0]["addr"] == 31
    # ⭐ La version de l'ÉMETTEUR voyage dans SA ligne, pas dans un champ de boîtier (#28) :
    #   un boîtier peut écouter plusieurs émetteurs, et chacun a sa propre version.
    assert out["emitter"][0]["fw"] == "0.1.11", out["emitter"]
    f = out["radio"]["recent"][0]["frames"]
    assert len(f) == 20, "les 20 dernières trames, pas toutes"
    assert f[0][0] == 1790841160, "la plus RÉCENTE en tête"


@cas
def les_trames_rendues_sont_les_PLUS_RECENTES_et_datees():
    """⭐⭐ Le champ qui porte tout le diagnostic rétroactif. Sur un boîtier muet depuis des
    jours, ces trames datent du JOUR DE SA MORT.

    🚨 ET C'EST LA SÉRIE QUI TRANCHE, PAS UN AGRÉGAT. Première version : on envoyait
    count/min/max/moyenne. Or une chute brutale et un déclin progressif donnent les MÊMES
    bornes — seule la moyenne diffère, et il faudrait savoir à quoi s'attendre pour la lire :

        chute  : -65 ×19 puis -95   → min -95  max -65  moy -66,5
        déclin : -65 … -95 linéaire → min -95  max -65  moy -80,0

    Le champ ne répondait donc pas à la question qui le justifie. Avec l'ordre dans le temps,
    les deux cas sont évidents à l'œil."""
    out = health.snapshot(base_radio(), None)
    f = out["radio"]["recent"][0]["frames"]
    # ⚖️ L'ordre DESC n'est pas un détail : rendre les 20 PREMIÈRES trames donnerait l'état
    #    du lien à sa NAISSANCE. La base fait décroître le rssi, donc la plus récente est la
    #    plus faible — si l'ordre était inversé, f[0][1] vaudrait -60.
    assert f[0][0] > f[-1][0], "la plus récente doit être en tête"
    assert f[0][1] == -89 and f[-1][1] == -70, f[:2]


@cas
def TOUTE_requete_sur_les_grosses_tables_porte_WHERE_pdl_index():
    """🚨 LE DÉFAUT TROUVÉ PAR LE PRÉFLIGHT SUR LA CIBLE, et aucun banc fonctionnel ne
    l'aurait vu : la requête rendait le bon résultat, elle le rendait 1 800× trop lentement.

    Sans `WHERE pdl_index = ?`, l'index couvrant `(pdl_index, ts)` devient inutilisable et
    SQLite balaie puis trie toute la table. Mesuré sur un boîtier du parc : **6 249 ms contre
    3,5 ms** — la sonde mangeait la moitié du budget à elle seule.

    ⭐ Ce cas est donc STRUCTUREL, pas fonctionnel : sur une base de banc à 30 lignes, les deux
    formes sont indiscernables. On vérifie le SQL lui-même, parce que c'est la seule façon
    d'attraper une régression de performance sans un Pi Zero sous la main."""
    import re
    # ⚠️ On retire les COMMENTAIRES avant d'analyser : l'en-tête du module cite des requêtes
    #    en contre-exemple (« JAMAIS `SELECT DISTINCT pdl_index FROM lora_link`, 16,7 s »), et
    #    un test qui se déclenche sur sa propre mise en garde ne vérifie rien.
    src = "\n".join(l for l in pathlib.Path(health.__file__).read_text().splitlines()
                    if not l.lstrip().startswith("#"))
    for table in ("measurements", "lora_link", "curve_rollup"):
        for m in re.finditer(rf"FROM {table}\b", src):
            # ⚠️ Une FENÊTRE autour de l'occurrence, pas seulement ce qui suit : dans
            #    `max(rowid) FROM measurements` la dispense est AVANT le FROM. C'est une
            #    heuristique assumée — son rôle est d'empêcher une régression distraite,
            #    pas d'analyser du SQL.
            autour = src[max(0, m.start() - 300):m.end() + 300]
            if "pdl_index = ?" in autour or "pdl_index=?" in autour:
                continue
            # ⭐ La SEULE autre forme admise : un encadrement par `rowid`. `max(rowid)` et
            #    `min(rowid) WHERE sent = 0` se résolvent en O(1) par index sans toucher au
            #    `pdl_index` — c'est la forme documentée de `pending_approx`, et la raison
            #    pour laquelle elle remplace un `count(*)` qui prend 37 secondes.
            # ⭐ Et la TROISIÈME, ajoutée pour la sonde canari : une lecture bornée à UNE
            #    ligne. `LIMIT 1` arrête SQLite après la première, donc il n'y a ni balayage
            #    complet ni tri. Mesuré sur deux boîtiers du parc :
            #
            #      WHERE sent = 0 ORDER BY rowid LIMIT 1     0,56 ms  (via idx_meas_sent)
            #      ORDER BY rowid DESC LIMIT 1               0,31 ms  (feuille la plus à droite)
            #
            # ⚠️ ET VOICI CE QUE CETTE DISPENSE NE COUVRE PAS, parce qu'un garde-fou dont on
            #    ignore le trou est un garde-fou qui ment : un `LIMIT 1` dont le `WHERE` porte
            #    sur une colonne NON indexée balaierait la table jusqu'à trouver sa ligne, et
            #    passerait ici. L'heuristique fait confiance à `LIMIT 1` pour borner le TRAVAIL,
            #    ce qui n'est vrai que si le prédicat est servi par un index.
            #
            # ⇒ Le trou est fermé AILLEURS, et c'est le bon endroit : le préflight de
            #   `update.sh` CHRONOMÈTRE la collecte sur la vraie base du boîtier. Une règle
            #   structurelle empêche la distraction ; seule une mesure sur la cible prouve la
            #   performance. (Même raisonnement qu'en 0.9.23 : un contrôle qui vérifie une
            #   décision sans vérifier son effet est un contrôle qui ment.)
            # 🚨 ET POUR CETTE DISPENSE, PAS LA FENÊTRE `autour` MAIS CE QUI SUIT LE `FROM`.
            #    Première version de ce cas : elle cherchait `LIMIT 1` dans `autour`, donc dans
            #    ±300 caractères. Les deux lectures canari étant des littéraux ADJACENTS, le
            #    `LIMIT 1` de l'une couvrait l'autre : retirer la borne d'une requête ne faisait
            #    PAS tomber le cas. Vérifié par mutation. On s'arrête donc au `SELECT` suivant.
            # ⚠️ `LIMIT` suivi d'un ENTIER, d'une interpolation ou d'un paramètre — pas
            #    seulement `LIMIT 1`. La sonde canari lit désormais le LOT ENTIER
            #    (`LIMIT {N_UNSENT}`), parce qu'une lecture d'une seule ligne la rendait
            #    borgne : le publisher en lit mille, et la page abîmée peut être la 437ᵉ.
            #    La borne reste donc une CONSTANTE bornée, et son coût réel est mesuré par le
            #    préflight sur la cible — c'est là qu'est fermé le trou de cette heuristique.
            suite = src[m.end():m.end() + 250].split("SELECT")[0]
            borne = re.search(r"LIMIT\s*(\d+|\{|\?)", suite)
            assert "rowid)" in autour or borne, (
                f"requête sur `{table}` sans WHERE pdl_index, sans encadrement par rowid et "
                f"sans borne LIMIT :\n    …{autour[250:420]}…")


@cas
def le_provisioning_donne_a_ben_l_acces_au_JOURNAL():
    """🚨 Sans le groupe `systemd-journal`, `journalctl` ne rend RIEN à `ben` — et le dit sur
    STDERR, que `_sh` jette. Le champ `errors` était donc mort-né sur TOUS les boîtiers, et
    les essais passaient parce qu'on les lançait en `pi`, qui est dans `adm`.

    Mesuré le 2026-10-01 : `id ben` → dialout, spi, gpio, rien d'autre. Et
    `sudo -u ben journalctl` → « No journal files were opened due to insufficient
    permissions ».

    ⭐ C'est le champ le plus utile de l'instantané : les lignes NOYAU (blocages SPI,
    sous-tensions, `brcmfmac: resumed on timeout` du pilote WiFi). Ce cas est structurel —
    il garde le provisioning, parce qu'un boîtier neuf ne doit pas repartir sans cet accès."""
    racine = pathlib.Path(health.__file__).resolve().parents[3]
    inst = racine / "install.sh"
    if not inst.is_file():
        return                      # arbre d'essai hors dépôt
    ligne = [l for l in inst.read_text().splitlines()
             if l.startswith("usermod -aG") and " ben" in l]
    assert ligne, "install.sh ne pose plus de groupes à `ben` ? le cas doit être revu"
    assert "systemd-journal" in ligne[0], (
        "install.sh ne met pas `ben` dans systemd-journal — `errors` sera vide sur tout "
        f"boîtier neuf :\n    {ligne[0]}")


@cas
def on_n_interroge_QUE_des_unites_que_le_depot_LIVRE():
    """🚨 `systemctl show` répond pour N'IMPORTE QUEL nom, même inventé, en le rendant
    `inactive/dead`. Interroger une unité qui n'est pas du produit fabrique donc un faux
    service mort dans chaque instantané du parc.

    `ben-recognizer` y figurait : une EXPÉRIMENTATION, présente sur un seul boîtier et absente
    du dépôt. Elle se voyait `loaded/inactive` sur celui-là et `not-found` sur l'autre — du
    bruit dans les deux cas, pour quelque chose qu'on n'a pas à chercher.

    ⚖️ Ce cas est le garde-fou de la LISTE, pas du code : c'est elle qui dérive."""
    systemd = pathlib.Path(health.__file__).resolve().parents[3] / "config" / "systemd"
    if not systemd.is_dir():
        return                      # arbre d'essai hors dépôt : rien à vérifier
    livrees = {f.stem for f in systemd.glob("*.service")}
    for u in health.UNITS:
        assert u in livrees, (
            f"`{u}` est interrogé mais le dépôt ne le livre pas "
            f"(absent de config/systemd/) — ce serait un faux service mort")


@cas
def AUCUNE_sonde_ne_peut_ECRIRE_sur_le_boitier():
    """🚨 LE DÉFAUT LE PLUS GRAVE DE TOUTE CETTE ISSUE, et il venait du diagnostic lui-même.

    `git status` rafraîchit l'index au passage, donc il prend `.git/index.lock`. Or `_sh` tue
    par SIGKILL au délai, et `repo` est la 6ᵉ sonde sur 8 — sur un boîtier chargé elle hérite
    de moins d'une seconde. Un git tué laisse le verrou. Ensuite, vérifié sur un boîtier du
    parc (git 2.47.3) :

        git checkout t1         → fatal: Unable to create '.git/index.lock': File exists.
        git status --porcelain  → (vide, SUCCÈS)

    `checkout_tag` étant en `check=True`, l'agent sort en 1, `device.json` n'est pas bumpé, et
    l'update REJOUE toutes les 10 min pour toujours. 🚨 Et la sonde SURVIT au verrou : elle
    rapporterait `dirty: false` pendant que toute l'OTA est morte — elle masquerait le blocage
    qu'elle existe pour révéler.

    ⭐ Ce cas est STRUCTUREL : on ne peut pas reproduire une course au verrou dans un banc. On
    vérifie donc que TOUT appel à `git` porte `--no-optional-locks`, et c'est la seule forme
    de garde qui tienne dans le temps."""
    src = "\n".join(l for l in pathlib.Path(health.__file__).read_text().splitlines()
                    if not l.lstrip().startswith("#"))
    appels = [l for l in src.splitlines() if '"git"' in l]
    assert appels, "plus aucun appel à git ? le cas doit être revu, pas supprimé"
    for l in appels:
        assert '"--no-optional-locks"' in l, (
            "appel à git SANS --no-optional-locks — il peut laisser un .git/index.lock "
            f"et bloquer l'OTA du boîtier à vie :\n    {l.strip()}")


@cas
def le_resume_radio_est_PAR_COMPTEUR():
    """⭐ Conséquence utile du correctif : on sait LEQUEL des compteurs a perdu son lien, au
    lieu d'un agrégat qui les mélange."""
    out = health.snapshot(base_radio(), None)
    r = out["radio"]["recent"]
    assert isinstance(r, list) and r[0]["i"] == 0, r
    assert len(r[0]["frames"]) == 20


@cas
def un_boitier_FILAIRE_n_a_ni_radio_ni_emetteur():
    """⚠️ L'absence est un ÉTAT NORMAL, pas une anomalie : un filaire n'a ni
    `radio-state.json` ni une seule ligne de `lora_link`. Signaler ça comme un problème
    noierait les vraies pannes du parc.

    🚨 ET IL FAUT NEUTRALISER `health.VAR`, pas seulement la base. `radio()` lit aux DEUX
    endroits : la table `lora_link` ET les fichiers d'état. Sans ce détournement, ce cas
    passait sur un Mac (où `/var/lib/ben-firmware` n'existe pas) et ÉCHOUAIT sur un vrai
    boîtier radio — il était vert par accident. C'est le boîtier qui l'a dit, pas le Mac."""
    import tempfile
    vrai_var = health.VAR
    health.VAR = tempfile.mkdtemp()      # un répertoire VIDE : aucun fichier d'état radio
    c = db.connect(":memory:")
    c.execute("INSERT INTO pdl (pdl_index, adco, first_seen, last_seen) VALUES (0,'021861862663',1,9)")
    c.execute("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(1790840000,0,300,1)")
    c.commit()
    try:
        out = health.snapshot(c, None)
    finally:
        health.VAR = vrai_var
    assert "radio" not in out, "un filaire ne doit pas prétendre avoir une radio"
    assert "emitter" not in out
    assert out["pdl"] == [{"i": 0, "last_ts": 1790840000}]


@cas
def un_boitier_qui_n_a_JAMAIS_mesure_est_un_etat_valide():
    """Un boîtier fraîchement provisionné : la table `pdl` existe, elle est vide. `last_ts`
    n'a pas de valeur, et ce n'est pas une erreur."""
    c = db.connect(":memory:")
    out = health.snapshot(c, None)
    assert "pdl" not in out
    assert out.get("pending") == 0
    assert "collect_ms" in out


@cas
def le_retard_est_rapporte_et_il_est_en_O_1():
    """⚠️ `pending` encadre par les `rowid`. Un `count(*) WHERE sent=0` prend **37 secondes**
    sur un Pi Zero — appelé ici, il mangerait trois fois le budget à lui seul."""
    c = base_radio()
    c.execute("UPDATE measurements SET sent = 0 WHERE rowid > 40")
    c.commit()
    out = health.snapshot(c, None)
    assert out["pending"] == 10, out.get("pending")


@cas
def les_versions_declarees_sont_reprises():
    """À recouper avec `repo.tag` : les deux divergent si une update a échoué entre le
    `git checkout` et le bump de `device.json` — un état qu'on ne pouvait voir qu'en SSH.

    🚨 ET `arduinoFirmwareVersion` NE REMONTE PLUS (#28). C'était une affirmation tenue À LA
    MAIN sur la version d'un firmware qui se livre par reflash PHYSIQUE : `install.sh` ne
    l'écrit plus depuis la bascule vers les capabilities, mais la sonde la lisait encore, donc
    un `device.json` ancien continuait de faire monter une version d'émetteur inventée — à
    côté de la version MESURÉE que porte désormais `emitter[].fw`. Ce cas le verrouille :
    le `device.json` du test la contient, et elle ne doit PAS ressortir."""
    out = health.snapshot(None, {"model": "Radio", "softwareVersion": "0.9.21",
                                 "arduinoFirmwareVersion": "0.0.6",
                                 "capabilities": ["lora", "lora-tic-receiver"]})
    assert out["dev"] == {"model": "Radio", "sw": "0.9.21",
                          "caps": ["lora", "lora-tic-receiver"]}, out.get("dev")


@cas
def capabilities_est_un_DICT_sur_les_vrais_boitiers():
    """🚨 Défaut trouvé en lançant la collecte sur un vrai boîtier : `device.json` porte
    `capabilities` sous forme de DICT, pas de liste. Ne garder que les listes faisait
    disparaître le champ EN SILENCE.

    ⚖️ Les deux formes doivent passer : le blob n'est pas typé, on transmet tel quel."""
    reel = {"rgb-led-indicator": {"hw": "rev01"}, "lora": {"hw": "rev01"},
            "lora-tic-receiver": {"hw": "rev01"}}
    assert health.versions({"capabilities": reel})["caps"] == reel
    assert health.versions({"capabilities": ["lora"]})["caps"] == ["lora"]
    assert "caps" not in (health.versions({"model": "Radio", "capabilities": {}}) or {})


@cas
def le_fw_des_capabilities_NE_REMONTE_PLUS():
    """🚨 LE CAS QUI FERME LA PORTE À LA VALEUR QUI MENTAIT (#28).

    `capabilities["lora-tic-receiver"]["fw"]` prétendait dire la version du firmware de
    l'ÉMETTEUR. Elle annonçait 0.1.3 pendant que le parc tournait en 0.1.8 — et elle ne
    POUVAIT pas tenir : c'est une constante GLOBALE livrée par OTA, là où l'état de reflash
    est PAR ÉMETTEUR, et où un boîtier peut en écouter plusieurs.

    ⚠️ `caps_for_model` ne l'écrit plus, mais ça ne vaut QUE pour un provisioning neuf : les
    sept `device.json` du parc la portent toujours. C'est donc à l'ÉMISSION qu'on la coupe,
    et c'est ce cas qui le vérifie — sinon le cloud lirait le chiffre inventé à côté du
    chiffre mesuré, et le faux aurait l'air aussi officiel que le vrai.

    ⚖️ Et le témoin INVERSE, dans le même cas : `hw` DOIT survivre. Un filtre qui jetterait
    l'attribut entier passerait la première assertion en faisant disparaître la révision
    matérielle, qui n'a rien à voir avec ce chantier."""
    vieux = {"lora": {"hw": "rev01"},
             "lora-tic-receiver": {"hw": "rev01", "fw": "0.1.3"}}
    caps = health.versions({"capabilities": vieux})["caps"]
    assert "fw" not in caps["lora-tic-receiver"], caps
    assert caps["lora-tic-receiver"]["hw"] == "rev01", caps
    assert caps["lora"] == {"hw": "rev01"}, caps
    # ⓘ Et l'entrée du boîtier n'est pas mutée au passage : `device.json` reste ce qu'il est.
    assert vieux["lora-tic-receiver"]["fw"] == "0.1.3"


@cas
def un_device_json_vide_ne_fabrique_pas_de_champ():
    """⚖️ Le témoin de `versions()` : sans lui, une fonction rendant `{}` au lieu de `None`
    mettrait un `"dev": {}` dans le paquet — du bruit qui ressemble à de l'information."""
    assert health.versions({}) is None
    assert health.versions({"model": ""}) is None


# ── Les pièges de parsing, qui ne se voient qu'ici ────────────────────────────

@cas
def un_NRestarts_absent_n_invente_pas_de_zero():
    """⚠️ `systemctl show` omet des propriétés selon la version et le type d'unité. Mettre 0
    par défaut ferait croire qu'un service n'a jamais redémarré — c'est précisément le champ
    qui aurait crié en 0.9.12 (208 redémarrages de ben-radio)."""
    vrai = health._sh
    health._sh = lambda *a: "Id=ben-radio.service\nActiveState=active\nSubState=running\n"
    try:
        u = health.units()
    finally:
        health._sh = vrai
    assert u == [{"n": "ben-radio", "a": "active", "s": "running"}], u


@cas
def un_horodatage_systemd_NON_unix_est_ecarte():
    """⚠️ Sans `--timestamp=unix`, systemd rend « Mon 2026-09-28 17:23:48 CEST » — du texte
    localisé. Le laisser passer mettrait une chaîne illisible là où le serveur attend un
    entier, et la date changerait de forme selon la locale du boîtier."""
    vrai = health._sh
    health._sh = lambda *a: ("Id=ben-radio.service\nActiveState=active\nSubState=running\n"
                             "ExecMainStartTimestamp=Mon 2026-09-28 17:23:48 CEST\n")
    try:
        u = health.units()
    finally:
        health._sh = vrai
    assert "since" not in u[0], u
    # ⚖️ Et la forme unix, elle, doit bien passer.
    health._sh = lambda *a: ("Id=ben-radio.service\nActiveState=active\nSubState=running\n"
                             "ExecMainStartTimestamp=@1790609028\n")
    try:
        u = health.units()
    finally:
        health._sh = vrai
    assert u[0]["since"] == 1790609028, u


@cas
def un_MESSAGE_de_journal_NON_TEXTE_est_ecarte():
    """⚠️ `journalctl -o json` rend un `MESSAGE` sous forme de LISTE d'octets quand la ligne
    n'est pas de l'UTF-8 valide — la TIC en produit. Le laisser passer mettrait un tableau là
    où le serveur attend une chaîne, et `jsonb` l'accepterait sans broncher : on découvrirait
    le problème en lisant les diagnostics, c'est-à-dire trop tard."""
    vrai = health._sh
    health._sh = lambda *a: (
        '{"MESSAGE":[72,105],"__REALTIME_TIMESTAMP":"1790000000000000"}\n'
        '{"MESSAGE":"erreur lisible","_SYSTEMD_UNIT":"ben-radio.service",'
        '"__REALTIME_TIMESTAMP":"1790000001000000"}\n')
    try:
        e = health.errors()
    finally:
        health._sh = vrai
    assert all(isinstance(x["m"], str) for x in e), e
    assert any(x["m"] == "erreur lisible" for x in e)


@cas
def le_journal_du_PUBLISHER_est_remonte():
    """🚨 LA SONDE QUI DONNE LA RÉPONSE AU PREMIER HELLO, PAS AU SUIVANT.

    Le hello part JUSTE APRÈS le redémarrage du publisher par l'OTA. À cet instant le nouveau
    processus a `echecs = 0` : aucune ligne en priorité 3 n'existe encore, et celles de
    l'ancien processus sont en `PRIORITY=6` sur tout boîtier antérieur à 0.9.23.

    ⇒ Sans cette sonde, la cause d'une panne de publication n'arrive qu'au hello SUIVANT, donc
      sous 24 h. C'est ce qui a motivé de la rétablir après l'avoir retirée.

    ⭐ Et sur un boîtier sain, les lignes INFO sont elles-mêmes le diagnostic : `inséré` y dit
      ce que `pending` ne dit pas — si le serveur a retenu les points ou les absorbe en
      doublons."""
    vrai = health._sh
    health._sh = lambda *a: (
        '{"MESSAGE":"envoyé 86 · inséré 79 · reste ~0",'
        '"__REALTIME_TIMESTAMP":"1790850000000000"}\n'
        '{"MESSAGE":"échec n°3 (timed out) — nouvelle tentative dans 6 s",'
        '"__REALTIME_TIMESTAMP":"1790850060000000"}\n')
    try:
        lignes = health.publisher()
    finally:
        health._sh = vrai
    assert lignes and len(lignes) == 2, lignes
    assert lignes[0]["t"] == 1790850000 and "envoyé 86" in lignes[0]["m"]
    # ⭐ La ligne de backoff : sur un boîtier antérieur à 0.9.23 elle est en PRIORITY=6, donc
    #    invisible à `errors()`. C'est exactement ce que cette sonde va chercher.
    assert "échec n°3" in lignes[1]["m"]


@cas
def la_sonde_du_publisher_n_a_PAS_de_filtre_de_priorite():
    """🚨 CONTRE-INTUITIF, ET C'EST CE QUI LA REND SÛRE. Mesuré sur la cible :

        -u ben-publisher -n 5                  2,94 s   ← retenu
        -u ben-publisher -p 4 -n 10            0,57 s
        -u ben-radio     -p 3 -n 8             7,93 s   🚨

    `-u` ne dégénère en balayage complet que s'il n'y a AUCUNE correspondance : journald
    parcourt alors tout le journal pour n'en trouver aucune. Sans filtre de priorité, les
    lignes INFO du publisher garantissent toujours une correspondance — la lecture reste une
    lecture de queue. Ajouter `-p 4` serait plus rapide sur un boîtier bavard et RUINEUX sur un
    boîtier silencieux, soit exactement le cas qu'on veut diagnostiquer.

    ⚖️ Cas STRUCTUREL : on ne peut pas reproduire un balayage de journal dans un banc."""
    vus = []
    vrai = health._sh
    health._sh = lambda *a: (vus.append(a), "")[1]
    try:
        health.publisher()
    finally:
        health._sh = vrai
    assert vus, "la sonde n'appelle plus journalctl ? le cas doit être revu"
    cmd = vus[0]
    assert "-u" in cmd and "ben-publisher" in cmd, cmd
    assert "-p" not in cmd, (
        "la sonde porte un filtre de priorité — ruineux sur un boîtier silencieux, qui est "
        f"le cas qu'on veut diagnostiquer : {cmd}")


@cas
def unsent_COMPTE_la_ou_pending_ENCADRE():
    """🚨 LE GLISSEMENT QUI A COÛTÉ NEUF JOURS DE DIAGNOSTIC FAUX.

    `pending` vaut `max(rowid) - min(rowid WHERE sent=0) + 1`. Il prouve OÙ se trouve la plus
    vieille ligne non envoyée — rien de plus. On a lu « 566 248 en attente » sur un boîtier du
    parc et on en a déduit « un lot plein part à chaque tour, donc il échoue », alors qu'une
    SEULE ligne restée à `sent = 0` sur un vieux rowid produit exactement le même chiffre.

    ⭐ Ce cas construit précisément cette base : 500 lignes, UNE SEULE non envoyée, et la plus
      vieille. `pending` annonce 500, `unsent` répond 1. Les deux ont raison, et seul le second
      répond à la question « un POST est-il tenté ? »."""
    conn = base_nue()
    for i in range(500):
        conn.execute("INSERT INTO measurements(ts, pdl_index, papp, sent) VALUES (?,?,?,?)",
                     (1790000000 + i, 0, 100, 0 if i == 0 else 1))
    conn.commit()
    out = health.store(conn, "/inexistant")
    assert out["pending"] == 500, f"pending = {out['pending']}, attendu 500 (encadrement)"
    assert out["unsent"] == 1, (
        f"unsent = {out['unsent']}, attendu 1 — c'est tout l'intérêt du champ : il COMPTE, "
        "là où `pending` encadre")


@cas
def unsent_est_BORNE_au_lot():
    """⚠️ SANS LA BORNE, LE CHAMP SERAIT INUTILISABLE : un `count(*) WHERE sent = 0` nu balaie
    les millions d'entrées de l'index et prend 37 SECONDES sur un Pi Zero (mesuré le
    2026-09-19). Il serait collecté à chaque hello.

    ⭐ Et la borne n'est pas un compromis : la question posée est « le prochain lot serait-il
      PLEIN ? », pas « combien y en a-t-il au total ». La réponse utile est donc saturée par
      construction — `unsent == N_UNSENT` veut dire « au moins un lot plein ».

    ⚠️ La borne est lue sur `BEN_PUB_BATCH`, la MÊME variable que `ben_publisher.BATCH`, et
       non sur une constante à part qui divergerait au premier réglage."""
    conn = base_nue()
    n = health.N_UNSENT + 50
    conn.executemany("INSERT INTO measurements(ts, pdl_index, papp, sent) VALUES (?,?,?,0)",
                     [(1790000000 + i, 0, 100) for i in range(n)])
    conn.commit()
    out = health.store(conn, "/inexistant")
    assert out["unsent"] == health.N_UNSENT, (
        f"unsent = {out['unsent']} pour {n} lignes non envoyées : la borne "
        f"{health.N_UNSENT} n'est pas respectée — le champ balaiera toute la table")


@cas
def unsent_vaut_ZERO_quand_tout_est_parti():
    """⚖️ LE TÉMOIN, et il n'est pas décoratif : un champ qui rendrait toujours la borne
    passerait le cas précédent et ne distinguerait plus rien. C'est le cas le plus fréquent du
    parc — six boîtiers sur sept sont à jour."""
    conn = base_nue()
    conn.executemany("INSERT INTO measurements(ts, pdl_index, papp, sent) VALUES (?,?,?,1)",
                     [(1790000000 + i, 0, 100) for i in range(10)])
    conn.commit()
    out = health.store(conn, "/inexistant")
    assert out["unsent"] == 0, f"unsent = {out['unsent']} alors que tout est marqué envoyé"


@cas
def la_sonde_du_publisher_SURVIT_a_la_banniere_de_redemarrage():
    """🚨 LE DÉFAUT DE LA PREMIÈRE VERSION, DÉMENTI PAR LE TERRAIN LE JOUR DE SA LIVRAISON.

    `N_PUB` valait 5. Or un redémarrage systemd émet CINQ lignes à lui seul. Voici ce que
    ben-0012 a réellement remonté à son premier hello en 0.9.23, le 2026-10-01 à 11:47 :

        Stopped ben-publisher.service …
        ben-publisher.service: Consumed 1.308s CPU time.
        Started ben-publisher.service …
        [INFO] démarrage — ben-0012 → … · lots de 1000 toutes les 60 s
        [INFO] ~569526 point(s) en attente

    Cinq lignes, cinq places : AUCUNE ligne de l'ancien processus. Or la sonde ne tourne QUE
    là, au hello qui suit le redémarrage de l'OTA — et ce sont justement les lignes de l'ancien
    processus qui sont sa raison d'être. Elle était aveugle au seul moment où elle existe.

    ⚖️ Cas STRUCTUREL, et il doit l'être : le défaut ne produisait aucune erreur. La sonde
       rendait cinq lignes valides, bien formées, parfaitement inutiles. Rien dans un banc
       fonctionnel ne pouvait s'en plaindre — il faut lire la CONSTANTE."""
    BANNIERE = 5  # Stopped · Consumed CPU · Started · 2 × INFO de démarrage
    assert health.N_PUB > BANNIERE, (
        f"N_PUB = {health.N_PUB} : la bannière de redémarrage ({BANNIERE} lignes) consomme "
        "tout le quota, la sonde ne verra jamais l'ancien processus")
    # Une marge d'une seule ligne ne diagnostique rien : il faut la tendance des échecs, et
    # le publisher écrit une ligne par tentative.
    assert health.N_PUB >= BANNIERE + 15, (
        f"N_PUB = {health.N_PUB} : {health.N_PUB - BANNIERE} ligne(s) utile(s) après la "
        "bannière, trop peu pour voir une suite d'échecs")


@cas
def la_sonde_du_publisher_est_bornee_en_OCTETS_pas_seulement_en_lignes():
    """🚨 L'ÉCHEC EST ASYMÉTRIQUE : `retenirSante` (ben-api) écarte le champ `health` ENTIER
    au-delà de 16 Ko — « trop volumineuse ». Une sonde trop bavarde n'emporte donc pas que
    son propre champ, elle emporte les dix-neuf autres.

    Et une borne en LIGNES est la mauvaise unité : les messages du publisher vont de 40
    caractères (« rien à envoyer · reste ~43 ») à 200, la troncature de `msg[:200]`. 40 lignes
    pleines font 9,1 Ko, et le reste de l'instantané en consomme 3,1 à 5.

    ⭐ On garde les lignes les PLUS RÉCENTES : ce sont elles qui expliquent la panne en cours."""
    vrai = health._sh
    long = "x" * 300          # tronqué à 200 par la sonde
    health._sh = lambda *a: "".join(
        '{"MESSAGE":"%s","__REALTIME_TIMESTAMP":"%d000000"}\n' % (long, 1790850000 + i)
        for i in range(health.N_PUB))
    try:
        lignes = health.publisher()
    finally:
        health._sh = vrai
    import json as _j
    taille = len(_j.dumps(lignes, separators=(",", ":")).encode())
    assert taille <= health.PUB_BUDGET_O + 260, (
        f"la sonde rend {taille} o pour un budget de {health.PUB_BUDGET_O} — avec le reste de "
        "l'instantané on franchit les 16 Ko du serveur, qui écarte alors TOUT le champ")
    assert lignes, "la sonde ne rend plus rien : le budget a tout mangé"
    # ⭐ Les plus RÉCENTES, pas les premières venues.
    assert lignes[-1]["t"] == 1790850000 + health.N_PUB - 1, (
        "ce ne sont pas les lignes les plus récentes qui sont conservées")


@cas
def la_sonde_du_publisher_passe_EN_DERNIER():
    """⚠️ L'ORDRE DES SONDES EST UNE LISTE DE PRIORITÉ : l'échéance globale sacrifie la
    dernière en premier. `pub` est la plus lente (~2,9 s) et celle dont l'absence coûte le
    moins — sur un boîtier en difficulté, mieux vaut perdre son journal de publisher que
    l'état de ses services ou ses compteurs."""
    import inspect
    src = inspect.getsource(health.snapshot)
    noms = [l.split('("')[1].split('"')[0]
            for l in src.splitlines() if l.strip().startswith('("')]
    assert noms and noms[-1] == "pub", f"`pub` n'est pas la dernière sonde : {noms}"


@cas
def un_message_tres_long_est_TRONQUE():
    """Une ligne de journal peut faire des kilo-octets. Huit d'entre elles suffiraient à faire
    sauter la borne de 16 Kio du serveur, et c'est tout l'instantané qui serait alors écarté —
    on perdrait le diagnostic à cause d'un seul message bavard."""
    vrai = health._sh
    health._sh = lambda *a: json.dumps(
        {"MESSAGE": "x" * 5000, "__REALTIME_TIMESTAMP": "1790000000000000"}) + "\n"
    try:
        e = health.errors()
    finally:
        health._sh = vrai
    assert all(len(x["m"]) <= 200 for x in e), [len(x["m"]) for x in e]


if __name__ == "__main__":
    ko = 0
    for fn in CAS:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            ko += 1
            print(f"  KO   {fn.__name__} : {e}")
    print(f"\n{len(CAS) - ko}/{len(CAS)}")
    sys.exit(1 if ko else 0)
