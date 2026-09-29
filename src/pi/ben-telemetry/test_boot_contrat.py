#!/usr/bin/env python3
"""Régression : une trame BOOT incomplète ne doit PAS ouvrir d'époque tarifaire.

Un émetteur qui lit une trame TIC TRONQUÉE en rend les premières lignes seulement : l'ADCO
est juste (ADSC/ADCO est en tête de trame), le reste ne vaut rien. Observé sur ben-0001 —
`CONTRAT='00'` à chaque ré-enregistrement, puis la vraie valeur 7 s plus tard. Enregistré
tel quel, ce faux contrat ouvre une époque tarifaire bidon, émet DEUX `changement_offre`
(aller puis retour) et vide `/registers`, qui se borne à l'époque courante.

Corrigé des DEUX côtés : l'émetteur n'annonce plus de contrat depuis une trame tronquée
(`tic-reader.ino`, `contractOf()` / `v.complete`), et le récepteur n'en accepte pas d'un boot
dépourvu de contexte tarifaire. Ce test couvre le second — le premier vit dans le firmware
Arduino, hors de portée d'un test Python.

    python3 src/pi/ben-telemetry/test_boot_contrat.py
"""
import pathlib
import sys
import types

R = pathlib.Path(__file__).resolve().parent.parent          # …/src/pi

# ben_telemetry importe paho au chargement : on le neutralise, le test ne touche pas au MQTT.
sys.modules.setdefault("paho", types.ModuleType("paho"))
sys.modules.setdefault("paho.mqtt", types.ModuleType("paho.mqtt"))
_mqtt = types.ModuleType("paho.mqtt.client")
_mqtt.Client = object
sys.modules.setdefault("paho.mqtt.client", _mqtt)

sys.path[:0] = [str(R / "store"), str(R / "lora-receiver"), str(R / "ben-telemetry")]

import db                    # noqa: E402
import frame_codec           # noqa: E402
import ben_telemetry as bt   # noqa: E402


def _frame(**tlvs):
    return {"tlvs": {getattr(frame_codec, tag): val for tag, val in tlvs.items()}}


# (libellé, TLV de la trame boot, époque tarifaire attendue)
CAS = [
    ("boot sur trame tronquee  (ADCO + CONTRAT='00', ni PREF ni ISOUSC)",
     dict(T_ADCO=b"031864000000", T_CONTRAT=b"00", T_PAPP=b"\x00\x00\x00"),
     None),
    ("boot standard complet    (PREF present, CONTRAT='TEMPO')",
     dict(T_ADCO=b"031864000000", T_PREF=b"\x06", T_CONTRAT=b"TEMPO", T_PAPP=b"{\x00\x00"),
     "TEMPO"),
    ("boot historique complet  (ISOUSC present, CONTRAT='HC..')",
     dict(T_ADCO=b"031864000000", T_ISOUSC=b"\x1e", T_CONTRAT=b"HC..", T_PAPP=b"{\x00\x00"),
     "HC.."),
]


def boot_sans_identite_n_ecrit_RIEN() -> bool:
    """🚨 Une trame de boot qui n'identifie pas son compteur est écartée EN ENTIER.

    Le raisonnement diffère du filaire : ici le MAC ChaCha20 a déjà prouvé que ces
    octets sont bien ceux que l'émetteur a émis. Un ADCO difforme ne dit donc pas « la
    radio a abîmé la trame », il dit « L'ÉMETTEUR A MAL LU SA TIC ». Or `ISOUSC`, `PREF`
    et `CONTRAT` sortent de LA MÊME lecture : les garder reviendrait à enregistrer la
    configuration d'un compteur qu'on n'a pas su nommer, sous le pdl_index du PRÉCÉDENT.

    ⚠️ Et le bloc `state` ne passe pas par `resolve_pdl` : il EFFACE `indexes`,
       `last_boot_seq` et `last_active_id` dès que l'ADCO diffère du retenu. `'\\x00\\x00'`
       est « non vide », donc il passait — carry-forward NTARF/EASF perdu, valeur bidon
       mémorisée, et la trame SAINE suivante rejouait l'effacement.

    ⓘ Rien n'est perdu de la MESURE : les trames de COURBE continuent d'arriver et
      `get_pdl_index` se replie sur sources.json. On écarte l'identité, jamais la mesure.
    """
    ok = True
    for libelle, adco in [("ADCO a deux octets NUL", b"\x00\x00"),
                          ("ADCO ampute (checksum aveugle)", b"06194700"),
                          ("ADCO a douze NUL", b"\x00" * 12)]:
        conn = db.connect(":memory:")
        bt.measurements_db = conn
        bt.state = {"adco": "031864000000", "indexes": {"1": 42}}
        bt.save_state = lambda *a, **k: None
        bt.blink_rgb = lambda *a, **k: None
        bt._adco_refuse_par_emetteur = {}

        bt.on_recv_boot(_frame(T_ADCO=adco, T_ISOUSC=b"\x1e", T_CONTRAT=b"HC.."), -60, 10, 0, 31)

        intact = (
            conn.execute("SELECT count(*) FROM pdl").fetchone()[0] == 0
            and conn.execute("SELECT count(*) FROM emitter").fetchone()[0] == 0
            and conn.execute("SELECT count(*) FROM contract_epoch").fetchone()[0] == 0
            and bt.state["adco"] == "031864000000"      # l'etat n'a pas ete ecrase
            and bt.state["indexes"] == {"1": 42}        # ni le carry-forward efface
        )
        ok = ok and intact
        print(f"  [{'OK   ' if intact else 'ECHEC'}] boot ecarte : {libelle}")

    # ⚖️ LE TÉMOIN : un ADCO conforme doit, lui, traverser — sinon un garde qui
    #    refuserait TOUT passerait les trois cas ci-dessus, et aucun boîtier LoRa ne
    #    s'enregistrerait plus jamais.
    conn = db.connect(":memory:")
    bt.measurements_db = conn
    bt.state = {}
    bt.save_state = lambda *a, **k: None
    bt.blink_rgb = lambda *a, **k: None
    bt._pdl_par_emetteur = {}

    bt.on_recv_boot(_frame(T_ADCO=b"031864000000", T_ISOUSC=b"\x1e", T_CONTRAT=b"HC.."), -60, 10, 0, 31)
    lie = (conn.execute("SELECT count(*) FROM pdl").fetchone()[0] == 1
           and db.emitter_pdl(conn, 31) is not None)
    ok = ok and lie
    print(f"  [{'OK   ' if lie else 'ECHEC'}] temoin : un ADCO conforme lie bien l'emetteur")
    return ok


def main() -> int:
    ok = boot_sans_identite_n_ecrit_RIEN()
    for libelle, tlvs, attendu in CAS:
        conn = db.connect(":memory:")
        bt.measurements_db = conn
        bt.state = {}
        bt.save_state = lambda *a, **k: None
        bt.blink_rgb = lambda *a, **k: None

        bt.on_recv_boot(_frame(**tlvs), -60, 10, 0, 31)

        epoques = [r[0] for r in conn.execute("SELECT ngtf FROM contract_epoch")]
        obtenu = epoques[-1] if epoques else None
        reussi = obtenu == attendu
        ok = ok and reussi
        print(f"  [{'OK   ' if reussi else 'ECHEC'}] {libelle}")
        print(f"           epoque = {obtenu!r}  (attendu {attendu!r})")

    print("\n  ==> TOUT PASSE" if ok else "\n  ==> REGRESSION")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
