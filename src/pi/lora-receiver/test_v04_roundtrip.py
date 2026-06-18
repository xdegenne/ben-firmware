"""
test_v04_roundtrip.py — Round-trip de la trame LoRa v0x04 (codec courbe PAPP).

Encodeur de référence (miroir EXACT de l'Arduino curveStart/curveAdd/curveFlush) →
decode_v04 → on vérifie la reconstruction. Couvre les pièges : delta franchissant la
frontière du varint 1 octet (+2000 / −2018 → 2 o), petits deltas (1 o), cas 3 octets,
HMAC falsifié, et l'ancrage temps historique.

Pur Python — pas de dépendance pytest ni hardware. Lancer :
    python3 src/pi/lora-receiver/test_v04_roundtrip.py
Sortie « OK » + code retour 0 si tout passe.
"""

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


def encode_v04(papp, *, index_id, index_value, batch_seq, period_ds=20,
               flags=0, ts_season=0, ts_raw=b"\x00" * 6, key=KEY):
    n = len(papp)
    body = bytearray(struct.pack(
        cc._KEYFRAME_FMT, cc.PROTOCOL_VERSION_CURVE, flags, batch_seq,
        index_id, index_value, papp[0], n, period_ds, ts_season, ts_raw))
    prev = papp[0]
    for p in papp[1:]:
        body += _varint(_zigzag(p - prev))
        prev = p
    mac = hmac.new(key, bytes(body), hashlib.sha256).digest()[:cc.HMAC_LEN]
    return bytes(body) + mac


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_roundtrip_curve():
    # deltas : +5 (1o), +5 (1o), +2000 (2o, franchit la frontière 1o), −2 (1o), −2018 (2o)
    samples = [300, 305, 310, 2310, 2308, 290]
    payload = encode_v04(samples, index_id=0x01, index_value=123456, batch_seq=7)

    # Vérifie qu'on a bien des varints multi-octets là où attendu (anti-régression
    # d'un test qui ne testerait que des deltas 1 octet) : deltas = 1+1+2+1+2 = 7 o.
    assert len(payload) == cc.KEYFRAME_LEN + 7 + cc.HMAC_LEN, len(payload)

    out = cc.decode_v04(payload, KEY)
    assert out["papp"] == samples, out["papp"]
    assert out["index_id"] == 0x01
    assert out["index_value"] == 123456
    assert out["batch_seq"] == 7
    assert out["period_ds"] == 20
    assert out["n"] == len(samples)
    assert out["ts_valid"] is False
    assert out["src_standard"] is False
    assert out["has_ext"] is False


def test_single_sample_keyframe_only():
    # N=1 : keyframe seul, 0 delta → 28 octets pile.
    payload = encode_v04([1234], index_id=0x00, index_value=42, batch_seq=0)
    assert len(payload) == cc.MIN_FRAME_LEN
    out = cc.decode_v04(payload, KEY)
    assert out["papp"] == [1234]
    assert out["n"] == 1


def test_varint_3_bytes():
    # Saut maximal int16 : 0 → 65535 → 0. |Δ| = 65535 → zz ≈ 131070 → varint 3 octets.
    samples = [0, 65535, 0]
    out = cc.decode_v04(encode_v04(samples, index_id=2, index_value=9, batch_seq=1), KEY)
    assert out["papp"] == samples


def test_hmac_tamper_rejected():
    payload = bytearray(encode_v04([100, 110, 120], index_id=0, index_value=1, batch_seq=3))
    payload[-1] ^= 0xFF  # corrompt le dernier octet du HMAC
    try:
        cc.decode_v04(bytes(payload), KEY)
    except cc.CurveDecodeError:
        return
    raise AssertionError("HMAC falsifié non rejeté")


def test_body_tamper_rejected():
    payload = bytearray(encode_v04([100, 110, 120], index_id=0, index_value=1, batch_seq=3))
    payload[9] ^= 0x01  # modifie papp_ref → HMAC ne couvre plus → doit échouer
    try:
        cc.decode_v04(bytes(payload), KEY)
    except cc.CurveDecodeError:
        return
    raise AssertionError("corps falsifié non rejeté")


def test_wrong_key_rejected():
    payload = encode_v04([100, 110], index_id=0, index_value=1, batch_seq=1)
    try:
        cc.decode_v04(payload, bytes(32))  # mauvaise clé
    except cc.CurveDecodeError:
        return
    raise AssertionError("mauvaise clé non rejetée")


def test_too_short_rejected():
    try:
        cc.decode_v04(b"\x04" * 10, KEY)
    except cc.CurveDecodeError:
        return
    raise AssertionError("trame trop courte non rejetée")


def test_anchor_timestamps_historique():
    samples = [300, 305, 310, 2310]  # n=4, period_ds=20 → 2,0 s
    out = cc.decode_v04(encode_v04(samples, index_id=1, index_value=5, batch_seq=2), KEY)
    t_rx = 1_000_000
    ts = cc.anchor_timestamps(out, t_rx)
    assert len(ts) == 4
    assert ts[-1] == t_rx                     # dernier échantillon ancré à la réception
    assert ts[-2] == t_rx - 2                 # espacement = period_ds/10 = 2 s
    assert ts[0] == t_rx - 2 * 3              # premier = t_rx - (n-1)*period
    assert ts == sorted(ts)                   # strictement croissant


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"OK — {len(tests)} tests passés")


if __name__ == "__main__":
    main()
