#!/usr/bin/env python3
"""banc_listen — LISTENER + ASSERT du banc de test BEN (tourne sur ben-0001).

Décode les trames LoRa du DUT (réutilise frame_codec, incl. 2e courbe IINST histo), N'ÉCRIT RIEN en base,
et COMPARE chaque batch aux scénarios (banc_scenarios) → coche PASS. Rapport via `kill -USR1 <pid>`.

À lancer quand ben-lora-receiver est ARRÊTÉ (conflit radio). Ne pollue JAMAIS la DB de prod.
"""
import signal
import time

import RPi.GPIO as GPIO
from raspi_lora import LoRa, ModemConfig

import frame_codec
from banc_scenarios import SCENARIOS

RF95_FREQ          = 868.0
RF95_POW           = 5
SERVER_ADDRESS     = 32
LORA_INTERRUPT_PIN = 22
RFM95_RST_PIN      = 17
HMAC_KEY_PATH      = "/etc/ben-firmware/hmac.key"

with open(HMAC_KEY_PATH) as f:
    HMAC_KEY = bytes.fromhex(f.read().strip())

RESULTS = {sc["name"]: {"status": "⏳ jamais reçu", "detail": ""} for sc in SCENARIOS}
STATS = {"frames_ok": 0, "mac_err": 0, "frame_err": 0}


def _u8(v):
    if isinstance(v, (bytes, bytearray)):
        return v[0] if v else None
    return v


def batch_fields(d):
    papp = d["papp"]
    iinst = d.get("iinst")
    tlvs = d.get("tlvs", {})
    return {
        "src_standard": d["src_standard"],
        "index_id": d["index_id"],
        "index_value": d["index_value"],
        "papp_sign": "-" if any(p < 0 for p in papp) else "+",
        "has_iinst": iinst is not None,
        "iinst_last": iinst[-1] if iinst else None,
        "njourf1": _u8(tlvs.get(frame_codec.T_NJOURF1)),
        "demain": _u8(tlvs.get(frame_codec.T_DEMAIN)),
    }


def satisfies(bf, expect):
    """True si le batch (bf) satisfait TOUTES les assertions du scénario (expect)."""
    for k, want in expect.items():
        if k == "iinst_val":
            if bf["iinst_last"] is None or abs(bf["iinst_last"] - want) > 20:  # tolérance (IINST varie)
                return False, k
        elif k in bf:
            if bf[k] != want:
                return False, k
        # clés inconnues (ex: index_ids_seen) ignorées ici
    return True, None


def on_recv(payload):
    raw = bytes(payload.message)
    try:
        d = frame_codec.decode(raw, HMAC_KEY)
    except frame_codec.FrameError as e:
        STATS["frame_err"] += 1
        if "MAC" in str(e):
            STATS["mac_err"] += 1
        print(f"[FrameError] {e}  (len={len(raw)})", flush=True)
        return
    if d["type"] != frame_codec.TYPE_CURVE:
        tl = ", ".join(f"{n}={v}" for _t, n, v, _k, _s in frame_codec.interpret_tlvs(d.get("tlvs", {})))
        print(f"BOOT  {tl}", flush=True)
        return

    STATS["frames_ok"] += 1
    bf = batch_fields(d)
    line = (f"COURBE [{'std' if bf['src_standard'] else 'histo'}] "
            f"idx_id={bf['index_id']} idx_val={bf['index_value']} n={d['n']} "
            f"papp[{bf['papp_sign']}]={d['papp'][0]}→{d['papp'][-1]}")
    if bf["has_iinst"]:
        line += f" IINST={d['iinst'][0]}→{d['iinst'][-1]}"
    if bf["njourf1"] is not None:
        line += f" NJOURF+1={bf['njourf1']}"
    if bf["demain"] is not None:
        line += f" DEMAIN={bf['demain']}"

    validated = []
    for sc in SCENARIOS:
        ok, _ = satisfies(bf, sc["expect"])
        if ok:
            if RESULTS[sc["name"]]["status"] != "✅ PASS":
                RESULTS[sc["name"]] = {"status": "✅ PASS", "detail": line}
            validated.append(sc["name"])
    tag = ("→ valide: " + ", ".join(validated)) if validated else "→ (aucun scénario matché)"
    print(f"{line}  {tag}", flush=True)


def report(*_):
    print("\n=========== RAPPORT BANC ===========", flush=True)
    for name, r in RESULTS.items():
        print(f"  {name:24s} {r['status']}", flush=True)
    print(f"  --- santé : frames_ok={STATS['frames_ok']} "
          f"frame_err={STATS['frame_err']} MAC_err={STATS['mac_err']} ---", flush=True)
    npass = sum(1 for r in RESULTS.values() if r["status"] == "✅ PASS")
    print(f"  === {npass}/{len(RESULTS)} scénarios validés ===\n", flush=True)


signal.signal(signal.SIGUSR1, report)

GPIO.setmode(GPIO.BCM)
GPIO.setup(RFM95_RST_PIN, GPIO.OUT)
GPIO.output(RFM95_RST_PIN, GPIO.HIGH); time.sleep(0.01)
GPIO.output(RFM95_RST_PIN, GPIO.LOW);  time.sleep(0.01)
GPIO.output(RFM95_RST_PIN, GPIO.HIGH); time.sleep(0.05)

lora = LoRa(0, LORA_INTERRUPT_PIN, SERVER_ADDRESS,
            modem_config=ModemConfig.Bw125Cr45Sf128,
            tx_power=RF95_POW, acks=True, freq=RF95_FREQ)
lora._spi_write(0x1D, 0x72)   # SF9 BW125 CR4/5 — ISO émetteur
lora._spi_write(0x1E, 0x94)
lora._spi_write(0x26, 0x00)
lora.on_recv = on_recv
lora.set_mode_rx()
print(f"[banc-listen] écoute addr={SERVER_ADDRESS} freq={RF95_FREQ}MHz SF9 — ASSERT, pas de DB.", flush=True)
print(f"[banc-listen] {len(SCENARIOS)} scénarios. Rapport : kill -USR1 <pid>. Ctrl-C pour arrêter.", flush=True)
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    report()
    try:
        lora.close()
    except Exception:
        pass
    GPIO.cleanup()
    print("[banc-listen] arrêt.", flush=True)
