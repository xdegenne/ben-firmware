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
    c.execute("INSERT INTO pdl VALUES(0,'031864467282',1,9)")
    c.execute("INSERT INTO emitter VALUES(31,'031864467282',0,1790786260)")
    for i in range(50):
        c.execute("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,1)",
                  (1790840000 + i, 200 + i))
    for i in range(30):
        c.execute("INSERT INTO lora_link(ts,pdl_index,rssi,snr,sent) VALUES(?,0,?,?,1)",
                  (1790840000 + i * 40, -60 - i, 10.0))
    c.commit()
    return c


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
    assert out["radio"]["recent"][0]["n"] == 20, "les 20 dernières trames, pas toutes"
    assert out["radio"]["recent"][0]["ts_max"] == 1790841160


@cas
def les_trames_rendues_sont_les_PLUS_RECENTES_et_datees():
    """⭐⭐ Le champ qui porte tout le diagnostic rétroactif. Sur un boîtier muet depuis des
    jours, ces trames datent du JOUR DE SA MORT — chute brutale à pleine puissance ⇒
    alimentation, dégradation progressive ⇒ antenne. L'ordre `DESC` n'est donc pas un détail :
    rendre les 20 PREMIÈRES trames donnerait l'état du lien à sa naissance."""
    out = health.snapshot(base_radio(), None)
    r = out["radio"]["recent"][0]
    assert r["ts_min"] == 1790840400 and r["ts_max"] == 1790841160
    # Les 20 dernières ont les rssi les plus BAS (la base les fait décroître) : si on avait
    # pris les 20 premières, la moyenne serait autour de -69, pas de -79.
    assert r["rssi_min"] == -89 and r["rssi_max"] == -70, r


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
            assert "rowid)" in autour, (
                f"requête sur `{table}` sans WHERE pdl_index ni encadrement par rowid :"
                f"\n    …{autour[250:420]}…")


@cas
def le_resume_radio_est_PAR_COMPTEUR():
    """⭐ Conséquence utile du correctif : on sait LEQUEL des compteurs a perdu son lien, au
    lieu d'un agrégat qui les mélange."""
    out = health.snapshot(base_radio(), None)
    r = out["radio"]["recent"]
    assert isinstance(r, list) and r[0]["i"] == 0, r
    assert r[0]["n"] == 20 and r[0]["ts_max"] == 1790841160


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
    c.execute("INSERT INTO pdl VALUES(0,'021861862663',1,9)")
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
    `git checkout` et le bump de `device.json` — un état qu'on ne pouvait voir qu'en SSH."""
    out = health.snapshot(None, {"model": "Radio", "softwareVersion": "0.9.21",
                                 "arduinoFirmwareVersion": "0.0.6",
                                 "capabilities": ["lora", "lora-tic-receiver"]})
    assert out["dev"] == {"model": "Radio", "sw": "0.9.21", "arduino": "0.0.6",
                          "caps": ["lora", "lora-tic-receiver"]}


@cas
def capabilities_est_un_DICT_sur_les_vrais_boitiers():
    """🚨 Défaut trouvé en lançant la collecte sur un vrai boîtier : `device.json` porte
    `capabilities` sous forme de DICT, pas de liste. Ne garder que les listes faisait
    disparaître le champ EN SILENCE — or il contient la version de firmware de l'émetteur
    (`lora-tic-receiver.fw`), celle-là même qu'on avait jugée incohérente sur ce boîtier.

    ⚖️ Les deux formes doivent passer : le blob n'est pas typé, on transmet tel quel."""
    reel = {"rgb-led-indicator": {"hw": "rev01"}, "lora": {"hw": "rev01"},
            "lora-tic-receiver": {"hw": "rev01", "fw": "0.1.2"}}
    assert health.versions({"capabilities": reel})["caps"] == reel
    assert health.versions({"capabilities": ["lora"]})["caps"] == ["lora"]
    assert "caps" not in (health.versions({"model": "Radio", "capabilities": {}}) or {})


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
