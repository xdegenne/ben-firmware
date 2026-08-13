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
     dict(T_ADCO=b"031864467282", T_CONTRAT=b"00", T_PAPP=b"\x00\x00\x00"),
     None),
    ("boot standard complet    (PREF present, CONTRAT='TEMPO')",
     dict(T_ADCO=b"031864467282", T_PREF=b"\x06", T_CONTRAT=b"TEMPO", T_PAPP=b"{\x00\x00"),
     "TEMPO"),
    ("boot historique complet  (ISOUSC present, CONTRAT='HC..')",
     dict(T_ADCO=b"031864467282", T_ISOUSC=b"\x1e", T_CONTRAT=b"HC..", T_PAPP=b"{\x00\x00"),
     "HC.."),
]


def main() -> int:
    ok = True
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
