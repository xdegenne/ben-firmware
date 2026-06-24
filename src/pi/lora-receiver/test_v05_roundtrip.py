"""
test_v05_roundtrip.py — Round-trip de la trame LoRa v0x05 (codec courbe PAPP, dt par point).

Encodeur de référence (miroir EXACT de l'Arduino curveStart/curveAdd/curveFlush) →
decode_v04 → on vérifie la reconstruction. Couvre : delta franchissant la frontière du
varint 1 octet, cadence NON uniforme (dt par point, l'apport du v0x05), horodate compteur
standard (meter_timestamps), PAPP net SIGNÉ (injection), HMAC falsifié.

Pur Python — pas de dépendance pytest ni hardware. Lancer :
    python3 src/pi/lora-receiver/test_v05_roundtrip.py
Sortie « OK » + code retour 0 si tout passe.
"""

import calendar
import hashlib
import hmac
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import curve_codec as cc  # noqa: E402

KEY = bytes(range(32))  # clé HMAC de test (32 octets)


# --------------------------------------------------------------------------- #
# Encodeur de référence — miroir du firmware Arduino (zig-zag + varint LEB128) #
# --------------------------------------------------------------------------- #
def _zigzag(d: int) -> int:
    """zz = (d << 1) ^ (d >> 31), émulé sur 32 bits (comme l'AVR int32_t)."""
    return ((d << 1) ^ (d >> 31)) & 0xFFFFFFFF


def _varint(u: int) -> bytes:
    out = bytearray()
    while u >= 0x80:
        out.append((u & 0x7F) | 0x80)
        u >>= 7
    out.append(u)
    return bytes(out)


def encode_v05(papp, *, index_id, index_value, batch_seq, dt_s=None, period_ds=20,
               flags=0, ts_season=0, ts_raw=b"\x00" * 6, key=KEY):
    """Encode une trame v0x05. `dt_s` = liste des dt PAR POINT en secondes (dt_s[0]=0 ignoré) ;
    None → 2 s uniformes. PAPP packé en 16 bits (signé géré par le masque & 0xFFFF)."""
    n = len(papp)
    if dt_s is None:
        dt_s = [0] + [2] * (n - 1)
    assert len(dt_s) == n, (len(dt_s), n)
    body = bytearray(struct.pack(
        cc._KEYFRAME_FMT, cc.PROTOCOL_VERSION_CURVE, flags, batch_seq,
        index_id, index_value, papp[0] & 0xFFFF, n, period_ds, ts_season, ts_raw))
    prev = papp[0]
    for i in range(1, n):
        body += _varint(dt_s[i])                      # dt depuis le point précédent, en secondes
        body += _varint(_zigzag(papp[i] - prev))      # delta PAPP zig-zag signé
        prev = papp[i]
    mac = hmac.new(key, bytes(body), hashlib.sha256).digest()[:cc.HMAC_LEN]
    return bytes(body) + mac


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_roundtrip_curve():
    # deltas : +5(1o), +5(1o), +2000(2o, franchit la frontière 1o), −2(1o), −2018(2o) = 7 o
    # dt : 5 points × 2 s → 5 × 1 o = 5 o.  points = 5 + 7 = 12 o.
    samples = [300, 305, 310, 2310, 2308, 290]
    payload = encode_v05(samples, index_id=0x01, index_value=123456, batch_seq=7)
    assert len(payload) == cc.KEYFRAME_LEN + 12 + cc.HMAC_LEN, len(payload)

    out = cc.decode_v04(payload, KEY)
    assert out["papp"] == samples, out["papp"]
    assert out["index_id"] == 0x01
    assert out["index_value"] == 123456
    assert out["batch_seq"] == 7
    assert out["n"] == len(samples)
    assert out["sample_dt_s"] == [0, 2, 2, 2, 2, 2], out["sample_dt_s"]
    assert out["version"] == 0x05
    assert out["src_standard"] is False


def test_single_sample_keyframe_only():
    # N=1 : keyframe seul, 0 point → 28 octets pile.
    payload = encode_v05([1234], index_id=0x00, index_value=42, batch_seq=0)
    assert len(payload) == cc.MIN_FRAME_LEN
    out = cc.decode_v04(payload, KEY)
    assert out["papp"] == [1234]
    assert out["n"] == 1
    assert out["sample_dt_s"] == [0]


def test_varint_3_bytes():
    # Saut maximal int16 : 0 → 65535 → 0. |Δ| = 65535 → zz ≈ 131070 → varint 3 octets.
    samples = [0, 65535, 0]
    out = cc.decode_v04(encode_v05(samples, index_id=2, index_value=9, batch_seq=1), KEY)
    assert out["papp"] == samples


def test_nonuniform_dt_anchor():
    # L'APPORT du v0x05 : cadence NON uniforme (trame ratée → gros écart). dt = [_,1,5,1].
    samples = [100, 110, 130, 125]
    dt_s = [0, 1, 5, 1]                                  # offsets cumulés : 0, 1, 6, 7
    out = cc.decode_v04(encode_v05(samples, index_id=1, index_value=5, batch_seq=2,
                                   dt_s=dt_s), KEY)
    assert out["sample_dt_s"] == [0, 1, 5, 1]
    t_rx = 1_000_000
    ts = cc.anchor_timestamps(out, t_rx)
    assert ts[-1] == t_rx                                # dernier ancré à la réception
    assert ts == [t_rx - 7, t_rx - 6, t_rx - 1, t_rx]    # espacement = dt réels (1,5,1)
    assert ts == sorted(ts)


def test_meter_timestamps_standard():
    # Standard : horodate compteur + dt → meter_ts = t0 + dt cumulés (immunisé délai LoRa).
    samples = [200, 210, 205]
    dt_s = [0, 1, 3]
    season = ord("E")                                   # été, UTC+2
    ts_raw = bytes([24, 6, 23, 14, 30, 0])              # 2024-06-23 14:30:00 (heure locale été)
    out = cc.decode_v04(encode_v05(
        samples, index_id=1, index_value=777, batch_seq=4,
        dt_s=dt_s, flags=cc.FLAG_TS_VALID | cc.FLAG_SRC_STANDARD,
        ts_season=season, ts_raw=ts_raw), KEY)
    t0 = calendar.timegm((2024, 6, 23, 14, 30, 0, 0, 0, 0)) - 2 * 3600
    mts = cc.meter_timestamps(out)
    assert mts == [t0, t0 + 1, t0 + 4], mts             # offsets cumulés 0,1,4


def test_standard_signed_injection():
    # Standard producteur : PAPP net SIGNÉ, transition soutiré → injection (négatif).
    samples = [500, 100, -300, -50]                     # zig-zag gère le passage du signe
    out = cc.decode_v04(encode_v05(
        samples, index_id=1, index_value=10, batch_seq=5,
        flags=cc.FLAG_SRC_STANDARD), KEY)
    assert out["papp"] == samples, out["papp"]
    assert out["src_standard"] is True


def test_hmac_tamper_rejected():
    payload = bytearray(encode_v05([100, 110, 120], index_id=0, index_value=1, batch_seq=3))
    payload[-1] ^= 0xFF
    try:
        cc.decode_v04(bytes(payload), KEY)
    except cc.CurveDecodeError:
        return
    raise AssertionError("HMAC falsifié non rejeté")


def test_body_tamper_rejected():
    payload = bytearray(encode_v05([100, 110, 120], index_id=0, index_value=1, batch_seq=3))
    payload[9] ^= 0x01  # modifie papp_ref → HMAC ne couvre plus
    try:
        cc.decode_v04(bytes(payload), KEY)
    except cc.CurveDecodeError:
        return
    raise AssertionError("corps falsifié non rejeté")


def test_wrong_key_rejected():
    payload = encode_v05([100, 110], index_id=0, index_value=1, batch_seq=1)
    try:
        cc.decode_v04(payload, bytes(32))
    except cc.CurveDecodeError:
        return
    raise AssertionError("mauvaise clé non rejetée")


def test_too_short_rejected():
    try:
        cc.decode_v04(b"\x05" * 10, KEY)
    except cc.CurveDecodeError:
        return
    raise AssertionError("trame trop courte non rejetée")


def test_old_v04_version_rejected():
    # Plus de compat v0x04 : une trame version 0x04 doit être rejetée.
    payload = bytearray(encode_v05([100, 110], index_id=0, index_value=1, batch_seq=1))
    payload[0] = 0x04
    # re-signe pour que seul le numéro de version fasse échouer (pas le HMAC)
    mac = hmac.new(KEY, bytes(payload[:-cc.HMAC_LEN]), hashlib.sha256).digest()[:cc.HMAC_LEN]
    payload[-cc.HMAC_LEN:] = mac
    try:
        cc.decode_v04(bytes(payload), KEY)
    except cc.CurveDecodeError:
        return
    raise AssertionError("version 0x04 non rejetée")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"OK — {len(tests)} tests passés")


if __name__ == "__main__":
    main()
