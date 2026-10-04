#!/usr/bin/env python3
"""Banc de la VERSION DE L'ÉMETTEUR, du TLV jusqu'au hello (#28, parente ben-docs#12).

⭐ CE QUE CE CHANTIER RÉSOUT. L'émetteur Arduino se livre par reflash PHYSIQUE — il n'y a
pas d'OTA sur AVR. Sa version n'était donc lisible qu'à son banner série, un FTDI en main,
DEVANT le boîtier. Depuis `tic-reader` 0.1.10 il l'annonce dans sa trame de boot (TLV
`T_FW`, trois octets) ; ici on la décode, on la range, et on la fait monter.

🚨 LE RISQUE QUE CE BANC EXISTE POUR COUVRIR, et il n'est pas théorique : AUCUN émetteur du
parc n'émet ce TLV aujourd'hui. Un contrôle trop strict — « une trame de boot doit porter sa
version » — ferait donc cesser l'enregistrement de TOUS les boîtiers radio d'un coup. Les
deux témoins sont donc symétriques, et c'est le PREMIER qui protège le parc :

    ①  SANS `0x07`  → la trame est décodée et stockée EXACTEMENT comme avant, et l'absence
                      est rangée comme « émetteur antérieur à la campagne », pas comme un trou
    ②  AVEC `0x07`  → la version est décodée, nommée, rangée, et remonte jusqu'au hello

⚠️ Et un troisième état existe, qu'on ne doit pas confondre avec ① : `NULL`, « aucune trame
de boot vue ». L'émetteur ne redémarre pas quand le Pi redémarre (seul le Pi le fait), donc
après une OTA il reste en STREAMING et ne rejoue pas son boot. Un NULL dit « je n'ai pas
encore regardé » ; ① dit « j'ai regardé, et ce satellite est vieux ». Les confondre, c'est
perdre la seule information qui permet de piloter une campagne de reflash.

    python3 src/pi/ben-telemetry/test_fw_emetteur.py
"""
import pathlib
import sqlite3
import sys
import types

R = pathlib.Path(__file__).resolve().parent.parent          # …/src/pi

# ben_telemetry importe paho au chargement : on le neutralise, le banc ne touche pas au MQTT.
sys.modules.setdefault("paho", types.ModuleType("paho"))
sys.modules.setdefault("paho.mqtt", types.ModuleType("paho.mqtt"))
_mqtt = types.ModuleType("paho.mqtt.client")
_mqtt.Client = object
sys.modules.setdefault("paho.mqtt.client", _mqtt)

sys.path[:0] = [str(R / "store"), str(R / "lora-receiver"), str(R / "ben-telemetry"),
                str(R / "publisher")]

import db                    # noqa: E402
import frame_codec as fc     # noqa: E402
import health                # noqa: E402
import ben_telemetry as bt   # noqa: E402

ADCO = b"031864467282"
ADDR = 31

CAS = []


def cas(f):
    CAS.append(f)
    return f


def _boot(conn, *, fw: bytes | None = None, adco: bytes = ADCO, addr: int = ADDR):
    """Fait traverser à `on_recv_boot` une trame de boot, avec ou sans `T_FW`.

    ⓘ On appelle `on_recv_boot` et pas `decode()` : le sujet n'est pas le codec seul mais la
      CHAÎNE — décodage, garde ADCO, rangement. Un banc qui s'arrêterait au codec laisserait
      passer un rangement au mauvais endroit.
    """
    tlvs = {fc.T_ADCO: adco}
    if fw is not None:
        tlvs[fc.T_FW] = fw
    tlvs[fc.T_ISOUSC] = b"\x1e"
    tlvs[fc.T_CONTRAT] = b"HC.."
    tlvs[fc.T_PAPP] = b"{\x00\x00"

    bt.measurements_db = conn
    bt.state = {}
    bt.save_state = lambda *a, **k: None
    bt.blink_rgb = lambda *a, **k: None
    bt._pdl_par_emetteur = {}
    bt._adco_refuse_par_emetteur = {}
    bt._last_uncabled = {}
    bt.on_recv_boot({"tlvs": tlvs}, -60, 10.0, 0, addr)


def _fw(conn, addr: int = ADDR):
    r = conn.execute("SELECT fw_version FROM emitter WHERE lora_addr=?", (addr,)).fetchone()
    return r[0] if r else "PAS DE LIGNE"


# ── Le codec ─────────────────────────────────────────────────────────────────

@cas
def trois_octets_font_une_version_et_rien_d_autre():
    """⭐ TROIS OCTETS, PAS UNE CHAÎNE — et `0.1.10` n'est donc pas plus long sur le fil que
    `0.1.9`. Côté émetteur la source de vérité est `FW_MAJOR`/`FW_MINOR`/`FW_PATCH` et c'est
    le préprocesseur qui fabrique le banner (banc `test_version.py` du sketch).

    ⚖️ Le cas `0x00 0x01 0x0a` est celui MESURÉ sur l'Arduino de dev le 2026-10-02, reçu par
    pi10jd75 : `b'\\x00\\x01\\n'` → 0.1.10. C'est la trame réelle qui fixe la convention, pas
    une relecture du code."""
    assert fc.fw_version(b"\x00\x01\x0a") == "0.1.10"
    assert fc.fw_version(b"\x00\x01\x0b") == "0.1.11"
    assert fc.fw_version(b"\x01\x00\x00") == "1.0.0"
    # 🚨 Une longueur autre que 3 n'est PAS une version, et on ne la devine pas. Le MAC a
    #    déjà prouvé que ces octets sont ceux qu'a émis l'émetteur : une longueur inattendue
    #    ne dit pas « la radio a abîmé la trame », elle dit « cet émetteur n'écrit pas le tag
    #    que nous croyons lire ». La reconstituer fabriquerait un numéro qui n'existe nulle part.
    assert fc.fw_version(b"\x00\x01") is None
    assert fc.fw_version(b"\x00\x01\x0b\x00") is None
    assert fc.fw_version(b"") is None


@cas
def interpret_tlvs_NOMME_le_tag_et_le_dit_stocke():
    """Le tag doit être NOMMÉ, sinon il ressort en `0x07` dans les journaux — illisible — et
    `log_uncabled` le crierait comme « collecté, pas stocké » à chaque changement.

    ⚖️ `stored=True` est l'autre moitié : c'est ce qui dit que ce chantier a bien CÂBLÉ le
    tag, et pas seulement rendu visible."""
    vus = {name: (val, known, stored)
           for _t, name, val, known, stored in fc.interpret_tlvs({fc.T_FW: b"\x00\x01\x0b"})}
    assert vus == {"FW": ("0.1.11", True, True)}, vus


@cas
def le_tag_0x07_ne_collisionne_avec_aucun_autre():
    """⚠️ Les deux tables de tags se MIRROITENT entre `tic-reader.ino` et `frame_codec.py`.
    Une collision ferait lire un champ pour un autre, des deux côtés de la liaison, sans que
    rien ne le signale — le TLV est forward-compatible, donc il ne lève jamais."""
    tags = [v for k, v in vars(fc).items() if k.startswith("T_") and isinstance(v, int)]
    assert len(tags) == len(set(tags)), sorted(tags)
    assert fc.T_FW == 0x07, hex(fc.T_FW)
    assert fc.T_FW in fc.TAG_NAMES


# ── ① LE TÉMOIN QUI PROTÈGE LE PARC ──────────────────────────────────────────

@cas
def TEMOIN_une_trame_SANS_0x07_est_stockee_exactement_comme_avant():
    """⚖️ LE CAS LE PLUS IMPORTANT DU BANC. Tout le parc est dans cet état aujourd'hui : un
    contrôle trop strict ferait cesser l'enregistrement de TOUS les boîtiers radio.

    On vérifie donc que la trame produit EXACTEMENT ce qu'elle produisait : le PDL créé,
    l'émetteur lié, l'abonnement, l'époque tarifaire, et la mesure instantanée de l'unboxing.
    """
    conn = db.connect(":memory:")
    _boot(conn, fw=None)

    assert conn.execute("SELECT count(*) FROM pdl").fetchone()[0] == 1
    assert db.emitter_pdl(conn, ADDR) == 0
    assert conn.execute("SELECT isousc FROM level_profile").fetchone()[0] == 30
    assert [r[0] for r in conn.execute("SELECT ngtf FROM contract_epoch")] == ["HC.."]
    assert conn.execute("SELECT papp FROM measurements").fetchone()[0] == 123


@cas
def l_absence_du_TLV_est_rangee_comme_ANTERIEUR_jamais_comme_un_trou():
    """🚨 `T_FW` est le second TLV INCONDITIONNEL du boot, écrit juste après `T_ADCO` et AVANT
    tout champ qui sort de la TIC (`tic-reader.ino`, `sendBootFrame`). Son absence ne peut
    donc pas vouloir dire « la TIC n'était pas encore lue », contrairement à celle de
    `CONTRAT`, d'`ISOUSC` ou de `PREF` — et c'est précisément ce qui rend l'inférence sûre.

    ⇒ Elle porte UNE information, nette : cet émetteur est ANTÉRIEUR à la campagne, il reste
      à reflasher. Un `NULL` serait indiscernable de « pas encore vu de trame de boot », qui
      est un état RÉEL et différent."""
    conn = db.connect(":memory:")
    _boot(conn, fw=None)
    assert _fw(conn) == db.EMITTER_FW_ANTERIEUR, _fw(conn)
    assert _fw(conn) is not None, "un trou n'est pas une information"


@cas
def NULL_veut_dire_pas_encore_vu_et_ce_n_est_PAS_anterieur():
    """⚖️ LE TROISIÈME ÉTAT, et le témoin du cas précédent. Sans lui, écrire `anterieur` dès
    la création de la ligne passerait les deux autres cas — et on perdrait la distinction qui
    fait tout l'intérêt du chantier.

    ⚠️ Le cas est RÉEL, pas théorique : l'émetteur ne redémarre pas quand le Pi redémarre.
    Après une OTA il reste en STREAMING et ne rejoue donc pas sa trame de boot — ses courbes
    continuent d'arriver (`get_pdl_index` se replie sur `sources.json`) sans qu'aucune trame
    de boot ne passe. La ligne `emitter` peut donc exister, servie par la courbe, SANS que
    quoi que ce soit n'ait encore été dit de la version."""
    conn = db.connect(":memory:")
    db.bind_emitter(conn, ADDR, ADCO.decode(), graine=0)
    assert _fw(conn) is None, _fw(conn)


# ── ② LE TÉMOIN INVERSE, DE BOUT EN BOUT ─────────────────────────────────────

@cas
def TEMOIN_INVERSE_une_trame_AVEC_0x07_fait_monter_la_version_jusqu_au_hello():
    """⚖️ Sans ce cas, un câblage qui ne rangerait RIEN passerait tous les précédents.

    ⭐ Et il va jusqu'au bout de la chaîne, `health.snapshot` inclus, parce que c'est là
    qu'est le but : la version doit être LISIBLE À DISTANCE. Rangée mais non remontée, elle
    n'aurait rien résolu — il faudrait toujours un FTDI sur place."""
    conn = db.connect(":memory:")
    _boot(conn, fw=b"\x00\x01\x0b")
    assert _fw(conn) == "0.1.11", _fw(conn)

    snap = health.snapshot(conn, None)
    lignes = {e["addr"]: e for e in snap["emitter"]}
    assert lignes[ADDR]["fw"] == "0.1.11", snap["emitter"]
    # ⓘ Le compteur lu est dans LA MÊME ligne : c'est ce qui rend la version actionnable
    #   (« quel boîtier, quel compteur, quelle version ») sans jointure côté cloud.
    assert lignes[ADDR]["pdl"] == 0


@cas
def UN_BOITIER_PEUT_ECOUTER_PLUSIEURS_EMETTEURS_chacun_sa_version():
    """🚨 LA RAISON POUR LAQUELLE CE N'EST PAS UN CHAMP DE BOÎTIER. La version appartient au
    SATELLITE — donc au compteur qu'il lit — et un boîtier peut en écouter plusieurs. Un
    champ unique à côté de `sw` aurait été faux dès le second émetteur, et faux en silence :
    le dernier boot reçu écraserait la version de l'autre.

    ⚖️ Deux émetteurs, deux compteurs, deux versions — dont une antérieure à la campagne,
    qui est exactement l'état d'un parc à moitié reflashé."""
    conn = db.connect(":memory:")
    _boot(conn, fw=b"\x00\x01\x0b", adco=ADCO, addr=31)
    _boot(conn, fw=None, adco=b"031864000001", addr=32)

    assert _fw(conn, 31) == "0.1.11", _fw(conn, 31)
    assert _fw(conn, 32) == db.EMITTER_FW_ANTERIEUR, _fw(conn, 32)

    snap = health.snapshot(conn, None)
    vu = {e["addr"]: e["fw"] for e in snap["emitter"]}
    assert vu == {31: "0.1.11", 32: db.EMITTER_FW_ANTERIEUR}, vu


# ── Ce que le rangement NE doit pas faire ────────────────────────────────────

@cas
def record_emitter_fw_ne_touche_NI_adco_NI_pdl_NI_updated_ts():
    """⚠️ Ces trois-là appartiennent à `bind_emitter`, qui les tient depuis la LECTURE TIC de
    la même trame. `updated_ts` signifie « depuis quand ce compteur est au bout de cet
    émetteur » — une question à laquelle un numéro de version ne répond pas."""
    conn = db.connect(":memory:")
    db.bind_emitter(conn, ADDR, ADCO.decode(), graine=0)
    avant = conn.execute("SELECT adco, pdl_index, updated_ts FROM emitter "
                         "WHERE lora_addr=?", (ADDR,)).fetchone()
    db.record_emitter_fw(conn, ADDR, "0.1.11")
    apres = conn.execute("SELECT adco, pdl_index, updated_ts FROM emitter "
                         "WHERE lora_addr=?", (ADDR,)).fetchone()
    assert avant == apres, (avant, apres)
    assert _fw(conn) == "0.1.11"


@cas
def record_emitter_fw_n_ecrit_que_sur_CHANGEMENT():
    """⭐ Une version ne bouge qu'au reflash. C'est l'appelant qui crie, et il ne doit donc
    crier qu'à ce moment-là — pas à chaque trame de boot, qui est rejouée à la cadence du
    batch tant que l'émetteur n'est pas enregistré.

    ⓘ Un émetteur DOWNGRADÉ écrase bien sa version par la plus ancienne : on range ce qui
      TOURNE, pas le maximum jamais vu."""
    conn = db.connect(":memory:")
    assert db.record_emitter_fw(conn, ADDR, "0.1.11") is True
    assert db.record_emitter_fw(conn, ADDR, "0.1.11") is False
    assert db.record_emitter_fw(conn, ADDR, "0.1.10") is True      # downgrade = changement
    assert db.record_emitter_fw(conn, ADDR, None) is True          # reflash arrière < 0.1.10
    assert _fw(conn) == db.EMITTER_FW_ANTERIEUR


@cas
def un_boot_a_l_ADCO_REFUSE_ne_range_toujours_RIEN():
    """🚨 L'INVARIANT QU'ON NE ROUVRE PAS. « Une trame de boot qui n'identifie pas son
    compteur n'écrit RIEN » est tenu par `test_boot_contrat` et `test_pdl_garde`.

    ⚠️ On pourrait soutenir que la version mériterait de passer quand même — elle ne sort pas
    de la TIC, donc le raisonnement du garde ne la concerne pas. On s'y refuse : y ouvrir une
    exception rouvrirait précisément la porte qu'on a fermée, et le coût est nul (l'émetteur
    rejoue sa trame de boot à la cadence du batch). Un boîtier qui lit mal sa TIC a un
    problème plus pressant que la version de son satellite."""
    conn = db.connect(":memory:")
    _boot(conn, fw=b"\x00\x01\x0b", adco=b"\x00\x00")
    assert conn.execute("SELECT count(*) FROM emitter").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM pdl").fetchone()[0] == 0


@cas
def un_T_FW_DIFFORME_ne_range_rien_et_ne_coute_pas_la_trame():
    """⚠️ Ni « antérieur » — ce serait faux, le tag est là — ni une version inventée. On ne
    range RIEN, on le dit au journal, et SURTOUT la trame continue de s'enregistrer : la
    mesure ne doit jamais payer un champ de diagnostic."""
    conn = db.connect(":memory:")
    _boot(conn, fw=b"\x00\x01")
    assert _fw(conn) is None, _fw(conn)
    assert conn.execute("SELECT count(*) FROM pdl").fetchone()[0] == 1
    assert conn.execute("SELECT papp FROM measurements").fetchone()[0] == 123


# ── La migration ─────────────────────────────────────────────────────────────

@cas
def la_colonne_s_ajoute_a_une_base_ANCIENNE_sans_rien_perdre():
    """⚠️ AJOUT SEUL, comme toute migration de ce fichier : l'ancien code doit pouvoir
    tourner sur cette base, c'est ce qui rend le retour arrière possible sans restaurer.

    ⓘ `CREATE TABLE IF NOT EXISTS` n'ajoute RIEN à une table existante — d'où l'`ALTER`
      conditionnel. Un banc est nécessaire parce que l'oubli ne se verrait qu'au premier
      boîtier du parc : sur une base neuve, le schéma porte déjà la colonne."""
    import tempfile
    chemin = tempfile.mktemp(suffix=".db")
    vieille = sqlite3.connect(chemin)
    vieille.executescript("""
        CREATE TABLE emitter (lora_addr INTEGER PRIMARY KEY, adco TEXT NOT NULL DEFAULT '',
                              pdl_index INTEGER, updated_ts INTEGER NOT NULL DEFAULT 0);
        INSERT INTO emitter VALUES (31, '031864467282', 0, 1790786260);
    """)
    vieille.commit()
    vieille.close()

    conn = db.connect(chemin)        # ← joue le schéma + les migrations
    cols = [r[1] for r in conn.execute("PRAGMA table_info(emitter)")]
    assert "fw_version" in cols, cols
    # ⓘ `tuple(...)` : `db.connect()` pose un `row_factory = sqlite3.Row`, qui ne s'égale
    #   pas à un tuple.
    ligne = tuple(conn.execute("SELECT adco, pdl_index, updated_ts, fw_version FROM emitter "
                               "WHERE lora_addr=31").fetchone())
    assert ligne == ("031864467282", 0, 1790786260, None), ligne
    # ⚖️ Et la migration est IDEMPOTENTE : un second `connect()` ne doit pas lever.
    db.connect(chemin).close()


def main() -> int:
    ok = True
    for f in CAS:
        try:
            f()
            print(f"  [OK   ] {f.__name__}")
        except Exception as e:  # noqa: BLE001
            # ⓘ `Exception` et pas `AssertionError` : une régression se manifeste souvent par
            #   une levée (un `TypeError` sur une version de longueur inattendue, par
            #   exemple), et un banc qui s'arrête au premier cas ne dit plus lequel a cassé.
            ok = False
            print(f"  [ECHEC] {f.__name__}\n           {type(e).__name__}: {e}")
    print(f"\n  ==> {len(CAS)}/{len(CAS)} PASSENT" if ok else "\n  ==> REGRESSION")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
