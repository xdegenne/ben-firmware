#!/usr/bin/env python3
"""Banc du raccordement TRIPHASÉ côté RÉCEPTEUR (#56).

🚨 LE DÉFAUT QUE CE BANC GARDE : l'émetteur publie depuis 0.1.12 le TOTAL des
   trois phases et lève `CF_TRIPHASE`, mais le récepteur décodait ce bit et le
   JETAIT. Or `ISOUSC` est exprimé PAR PHASE alors que `maxVa` borne un `PAPP`
   qui est un TOTAL : un foyer triphasé tirant 5 000 VA sur un 3×20 A affichait
   109 %, rouge en permanence. Le courant était juste et la jauge fausse.

⭐ C'est la MÊME colonne que le lecteur filaire alimente depuis pi-0.14.0 (#50) :
   `level_profile.phases`. Elle existait, elle n'était nourrie que d'un côté.

🚨 ET LA RÈGLE QUI COMPTE : on n'écrit QUE 3, jamais 1. Le monophasé est
   l'ABSENCE de déclaration, pas une déclaration d'absence — une trame ayant
   perdu une phase au checksum arrive sans le bit et ressemble trait pour trait
   à un monophasé. Les sept boîtiers du parc sont monophasés : les faire
   basculer sur une trame abîmée coûterait un facteur 3 sur leur jauge.

    python3 src/pi/ben-telemetry/test_phases_triphase.py
"""
import pathlib
import sys
import tempfile
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
import ben_telemetry as bt   # noqa: E402

echecs = []


def verifie(nom, obtenu, attendu):
    if obtenu == attendu:
        print(f"  ✓ {nom}")
    else:
        print(f"  ✗ {nom}\n      attendu : {attendu!r}\n      obtenu  : {obtenu!r}")
        echecs.append(nom)


CLE = bytes(range(32))
PDL = 0


_seq = [0]


def courbe(triphase):
    """Une trame COURBE décodée, comme `on_recv_curve` la reçoit.

    ⚠️ `batch_seq` INCRÉMENTÉ À CHAQUE APPEL — relevé en revue de la PR #58.
       Avec un seq constant, `on_recv_curve` marque les trames suivantes comme
       DOUBLONS. Le bloc triphasé s'exécute avant ce contrôle, donc la couverture
       tenait quand même — mais par ACCIDENT D'ORDRE, pas par conception. Si cet
       ordre changeait, ce banc deviendrait vide SANS LE DIRE, ce qui est la
       pire des façons de perdre un banc.
    """
    _seq[0] += 1
    brut = fc.encode_curve(CLE, boot_count=1, msg_count=_seq[0], batch_seq=_seq[0],
                           index_id=1, index_value=1000, papp=[1500, 1520],
                           period_ds=20, triphase=triphase)
    return fc.decode(brut, CLE)


with tempfile.TemporaryDirectory() as rep:
    conn = db.connect(str(pathlib.Path(rep) / "m.db"))
    bt.measurements_db = conn
    bt.db = db
    # ⚠️ `on_recv_curve` persiste un état par `save_state` ; hors cible, le chemin
    #    de production est /var/lib/ben-firmware et n'est pas écrivable. On le
    #    détourne vers le répertoire jetable du banc.
    bt.STATE_PATH = str(pathlib.Path(rep) / "state.json")

    print("── ⚖️ CONTRE-TÉMOIN : le parc est monophasé ────────────────")
    bt._triphase_par_pdl.clear()
    bt.on_recv_curve(courbe(False), -60, 8.0, PDL, 1792000000, 2.0)
    verifie("une trame SANS le bit n'écrit RIEN", db.get_phases(conn, PDL), 1)

    print("── le bit est consommé ────────────────────────────────────")
    bt.on_recv_curve(courbe(True), -60, 8.0, PDL, 1792000002, 2.0)
    verifie("une trame AVEC le bit écrit phases = 3 (⚖️ DOIT TOMBER sans #56)",
            db.get_phases(conn, PDL), 3)
    verifie("   la jauge passe de 4 600 à 13 800 VA (ISOUSC 20)",
            20 * 230 * db.get_phases(conn, PDL), 13800)

    print("── 🚨 une trame abîmée NE DÉFAIT PAS le triphasé ───────────")
    # Une trame ayant perdu une phase au checksum arrive SANS le bit : elle est
    # indiscernable d'un monophasé. Si on la croyait, la jauge repartirait au tiers.
    bt.on_recv_curve(courbe(False), -60, 8.0, PDL, 1792000004, 2.0)
    verifie("après une trame sans bit, phases vaut TOUJOURS 3",
            db.get_phases(conn, PDL), 3)

    print("── écriture SUR CHANGEMENT, pas à chaque trame ────────────")
    ecrits = []
    vrai_record = db.record_phases
    db.record_phases = lambda c, p, n: (ecrits.append((p, n)), vrai_record(c, p, n))[1]
    bt.on_recv_curve(courbe(True), -60, 8.0, PDL, 1792000006, 2.0)
    bt.on_recv_curve(courbe(True), -60, 8.0, PDL, 1792000008, 2.0)
    verifie("deux trames triphasées de plus → AUCUNE écriture", ecrits, [])
    db.record_phases = vrai_record

    print("── ⚖️ un autre PDL n'est pas contaminé ─────────────────────")
    verifie("le PDL 1 reste à 1 phase", db.get_phases(conn, 1), 1)
    conn.close()

print()
if echecs:
    print(f"❌ {len(echecs)} cas en échec : " + ", ".join(echecs))
    sys.exit(1)
print("✅ tous les cas passent")
