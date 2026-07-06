"""frame_codec.py — codec de la trame LoRa « format cible ».

Réf : ben-firmware/docs/lora-frame-format.md. Enveloppe unifiée :

    [ver/type:1][boot_count:3 LE][msg_count:3 LE]   ← header CLAIR (nonce)
    [ ... corps (selon type) ... ]                  ← chiffrable (phase 2)
    [MAC:8]                                          ← HMAC(K_mac, clair ‖ corps)[:8]

- `ver` : bits 0-6 = type, bit7 = chiffré (phase 1 = 0, clair).
- Corps COURBE (0x05) : core + points (delta/zigzag varint) + TLV.
- Corps BOOT   (0x01) : TLV d'identité uniquement.
- Champs optionnels = TLV `[tag:1][len:1][value:len]` → forward-compatible.

Encode ET decode ici → sert au round-trip (l'émetteur Arduino mirrore l'encode).
Phase 1 = clair. Phase 2 (ChaCha20) : brancher `_encrypt`/`_decrypt`, poser bit7.
"""
import hmac
import hashlib
import struct

# --- Types de trame (octet version, bits 0-6) ---
TYPE_BOOT = 0x01
TYPE_CURVE = 0x05
FLAG_ENC = 0x80                 # bit7 = corps chiffré

# --- Flags du core courbe (octet flags) ---
CF_SRC_STANDARD = 0x01
CF_TS_VALID = 0x02
CF_HAS_IINST = 0x04   # bit2 : 2e courbe IINST (histo) — restaure IINST perdu au passage delta

# --- Tags TLV (cf. §7 du doc) ---
T_ADCO = 0x01
T_ISOUSC = 0x02
T_PREF = 0x03
T_CONTRAT = 0x04
T_EAIT = 0x10
T_LTARF = 0x11
T_DEMAIN = 0x20
T_NJOURF = 0x21
T_NJOURF1 = 0x22
T_ADPS = 0x30
T_PEJP = 0x31
T_MSG1 = 0x40
T_MSG2 = 0x41

MAC_LEN = 8
HEADER_LEN = 7                  # ver(1) + boot_count(3) + msg_count(3)


class FrameError(Exception):
    """Trame invalide : longueur, MAC, version, corps tronqué."""


# --------------------------------------------------------------------------- #
# Dérivation de clés (K_enc / K_mac) depuis la clé par-device                  #
# --------------------------------------------------------------------------- #
def derive_keys(key: bytes):
    """K_enc / K_mac = HMAC-SHA256(K, label). Dérivées une fois (au boot côté Arduino)."""
    k_enc = hmac.new(key, b"ben-lora-enc", hashlib.sha256).digest()
    k_mac = hmac.new(key, b"ben-lora-mac", hashlib.sha256).digest()
    return k_enc, k_mac


def _mac(k_mac: bytes, data: bytes) -> bytes:
    return hmac.new(k_mac, data, hashlib.sha256).digest()[:MAC_LEN]


# --------------------------------------------------------------------------- #
# ChaCha20 (RFC 8439 IETF : nonce 96 bits, compteur 32 bits départ 0)          #
# Pur-Python (pas de dépendance) ; DOIT matcher rweather ChaCha côté Arduino   #
# (setIV(nonce,12) = variante IETF, setCounter(0)). Flot → chiffre = déchiffre.#
# --------------------------------------------------------------------------- #
def _rotl32(v, c):
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    const = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
    state = list(const) + list(struct.unpack("<8I", key)) + \
        [counter & 0xFFFFFFFF] + list(struct.unpack("<3I", nonce))
    x = state[:]

    def qr(a, b, c, d):
        x[a] = (x[a] + x[b]) & 0xFFFFFFFF; x[d] = _rotl32(x[d] ^ x[a], 16)
        x[c] = (x[c] + x[d]) & 0xFFFFFFFF; x[b] = _rotl32(x[b] ^ x[c], 12)
        x[a] = (x[a] + x[b]) & 0xFFFFFFFF; x[d] = _rotl32(x[d] ^ x[a], 8)
        x[c] = (x[c] + x[d]) & 0xFFFFFFFF; x[b] = _rotl32(x[b] ^ x[c], 7)
    for _ in range(10):                    # 20 rounds = 10 × (colonnes + diagonales)
        qr(0, 4, 8, 12); qr(1, 5, 9, 13); qr(2, 6, 10, 14); qr(3, 7, 11, 15)
        qr(0, 5, 10, 15); qr(1, 6, 11, 12); qr(2, 7, 8, 13); qr(3, 4, 9, 14)
    return struct.pack("<16I", *[(x[i] + state[i]) & 0xFFFFFFFF for i in range(16)])


def _chacha20(key: bytes, nonce: bytes, data: bytes, counter: int = 0) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 64):
        ks = _chacha20_block(key, counter + i // 64, nonce)
        out += bytes(a ^ b for a, b in zip(data[i:i + 64], ks))
    return bytes(out)


def _nonce(boot_count: int, msg_count: int) -> bytes:
    """96 bits IETF = boot_count(3o LE) ‖ msg_count(3o LE) ‖ 0×6. Unique/clé (cf. §6 du doc)."""
    return boot_count.to_bytes(3, "little") + msg_count.to_bytes(3, "little") + b"\x00" * 6


# --------------------------------------------------------------------------- #
# Primitives varint / zigzag / int24                                          #
# --------------------------------------------------------------------------- #
def _varint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _read_varint(buf, pos):
    shift = 0
    result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _zigzag(v: int) -> int:
    return ((v << 1) ^ (v >> 31)) & 0xFFFFFFFF


def _unzigzag(u: int) -> int:
    return (u >> 1) ^ -(u & 1)


def _pack_i24(v: int) -> bytes:
    return (v & 0xFFFFFF).to_bytes(3, "little")


def _unpack_i24(b: bytes, signed: bool) -> int:
    v = int.from_bytes(b, "little")
    if signed and (v & 0x800000):
        v -= 1 << 24
    return v


# --------------------------------------------------------------------------- #
# TLV                                                                         #
# --------------------------------------------------------------------------- #
def _encode_tlvs(tlvs) -> bytes:
    """tlvs = liste de (tag:int, value:bytes)."""
    out = bytearray()
    for tag, value in tlvs:
        if len(value) > 255:
            raise FrameError(f"TLV {tag:#04x} trop long ({len(value)})")
        out.append(tag)
        out.append(len(value))
        out += value
    return bytes(out)


def _decode_tlvs(buf, pos, end) -> dict:
    """Itère les TLV de [pos, end). Renvoie {tag: value_bytes}. Saute les tags inconnus
    via leur longueur (forward-compat)."""
    out = {}
    while pos < end:
        tag = buf[pos]
        length = buf[pos + 1]
        v0 = pos + 2
        if v0 + length > end:
            raise FrameError(f"TLV {tag:#04x} tronqué")
        out[tag] = bytes(buf[v0:v0 + length])
        pos = v0 + length
    return out


# --------------------------------------------------------------------------- #
# Encode (clair, ou chiffré via encrypt=True)                                  #
# --------------------------------------------------------------------------- #
def _seal(ftype, boot_count, msg_count, body, k_enc, k_mac, encrypt):
    """Chiffre le corps (si encrypt) → header (bit7) → MAC sur (clair ‖ corps). Encrypt-then-MAC."""
    ver = ftype | (FLAG_ENC if encrypt else 0)
    if encrypt:
        body = _chacha20(k_enc, _nonce(boot_count, msg_count), body)
    header = struct.pack("<B", ver) + boot_count.to_bytes(3, "little") + msg_count.to_bytes(3, "little")
    frame = header + body
    return frame + _mac(k_mac, frame)


def encode_curve(key: bytes, *, boot_count: int, msg_count: int, batch_seq: int,
                 index_id: int, index_value: int, papp: list, period_ds: int,
                 src_standard: bool = False, ts_valid: bool = False,
                 ts: bytes = b"\x00" * 7, dt_s=None, tlvs=None, encrypt: bool = False) -> bytes:
    """Encode une trame COURBE. `papp` = liste des PAPP (papp[0]=keyframe). `dt_s` = dt par
    point en s (dt_s[0] ignoré) ; None → 2 s uniformes. `tlvs` = liste de (tag, value_bytes).
    `encrypt=True` → corps ChaCha20 + bit7."""
    k_enc, k_mac = derive_keys(key)
    n = len(papp)
    if dt_s is None:
        dt_s = [0] + [2] * (n - 1)
    flags = (CF_SRC_STANDARD if src_standard else 0) | (CF_TS_VALID if ts_valid else 0)

    core = bytearray()
    core.append(flags)
    core += struct.pack("<H", batch_seq & 0xFFFF)
    core.append(index_id & 0xFF)
    core += struct.pack("<I", index_value & 0xFFFFFFFF)
    core += _pack_i24(papp[0])
    core.append(n & 0xFF)
    core.append(period_ds & 0xFF)
    if ts_valid:
        core += ts[:7].ljust(7, b"\x00")

    pts = bytearray()
    prev = papp[0]
    for i in range(1, n):
        pts += _varint(dt_s[i])
        pts += _varint(_zigzag(papp[i] - prev))
        prev = papp[i]

    body = bytes(core) + bytes(pts) + _encode_tlvs(tlvs or [])
    return _seal(TYPE_CURVE, boot_count, msg_count, body, k_enc, k_mac, encrypt)


def encode_boot(key: bytes, *, boot_count: int, msg_count: int, tlvs, encrypt: bool = False) -> bytes:
    """Encode une trame BOOT/IDENTITÉ. `tlvs` = liste de (tag, value). `encrypt=True` → ChaCha20 + bit7."""
    k_enc, k_mac = derive_keys(key)
    return _seal(TYPE_BOOT, boot_count, msg_count, _encode_tlvs(tlvs), k_enc, k_mac, encrypt)


# --------------------------------------------------------------------------- #
# Decode                                                                       #
# --------------------------------------------------------------------------- #
def decode(payload: bytes, key: bytes) -> dict:
    """Vérifie le MAC et décode une trame (BOOT ou COURBE). Lève FrameError si invalide."""
    if len(payload) < HEADER_LEN + MAC_LEN:
        raise FrameError(f"trame trop courte ({len(payload)} o)")
    k_enc, k_mac = derive_keys(key)
    signed_part, mac = payload[:-MAC_LEN], payload[-MAC_LEN:]
    if not hmac.compare_digest(_mac(k_mac, signed_part), mac):
        raise FrameError("MAC invalide")

    ver = payload[0]
    encrypted = bool(ver & FLAG_ENC)
    ftype = ver & 0x7F
    boot_count = int.from_bytes(payload[1:4], "little")
    msg_count = int.from_bytes(payload[4:7], "little")

    body = payload[HEADER_LEN:-MAC_LEN]
    if encrypted:                                    # MAC déjà vérifié (encrypt-then-MAC)
        body = _chacha20(k_enc, _nonce(boot_count, msg_count), body)

    out = {"type": ftype, "encrypted": encrypted,
           "boot_count": boot_count, "msg_count": msg_count}

    if ftype == TYPE_BOOT:
        out["tlvs"] = _decode_tlvs(body, 0, len(body))
        return out

    if ftype == TYPE_CURVE:
        pos = 0
        flags = body[pos]; pos += 1
        src_standard = bool(flags & CF_SRC_STANDARD)
        ts_valid = bool(flags & CF_TS_VALID)
        batch_seq = struct.unpack_from("<H", body, pos)[0]; pos += 2
        index_id = body[pos]; pos += 1
        index_value = struct.unpack_from("<I", body, pos)[0]; pos += 4
        papp_ref = _unpack_i24(body[pos:pos + 3], signed=src_standard); pos += 3
        n = body[pos]; pos += 1
        period_ds = body[pos]; pos += 1
        ts = None
        ts_season = None
        ts_raw = None
        if ts_valid:
            ts = bytes(body[pos:pos + 7]); pos += 7
            ts_season = ts[0]        # octet saison (ord 'E'/'H') — pour meter_epoch()
            ts_raw = ts[1:7]         # 6 o : YY MM DD hh mm ss
        has_iinst = bool(flags & CF_HAS_IINST)   # 2e courbe IINST (histo)
        iinst_ref = None
        if has_iinst:
            iinst_ref = struct.unpack_from("<H", body, pos)[0]; pos += 2
        # points (papp ; + iinst en histo si has_iinst)
        papp = [papp_ref]
        dt_s = [0]
        iinst = [iinst_ref] if has_iinst else None
        for _ in range(n - 1):
            dt, pos = _read_varint(body, pos)
            u, pos = _read_varint(body, pos)
            papp.append(papp[-1] + _unzigzag(u))
            dt_s.append(dt)
            if has_iinst:
                ui, pos = _read_varint(body, pos)
                iinst.append(iinst[-1] + _unzigzag(ui))
        tlvs = _decode_tlvs(body, pos, len(body))
        out.update({
            "flags": flags, "src_standard": src_standard, "ts_valid": ts_valid,
            "batch_seq": batch_seq, "index_id": index_id, "index_value": index_value,
            "papp_ref": papp_ref, "n": n, "period_ds": period_ds, "ts": ts,
            "ts_season": ts_season, "ts_raw": ts_raw,
            "papp": papp, "sample_dt_s": dt_s, "iinst": iinst, "tlvs": tlvs,
        })
        return out

    raise FrameError(f"type de trame inconnu : 0x{ftype:02x}")


# --------------------------------------------------------------------------- #
# Horodatage (porté de curve_codec — frame_codec est le SEUL codec désormais)  #
# --------------------------------------------------------------------------- #
def meter_epoch(decoded: dict):
    """Horodate COMPTEUR (champ DATE, TIC standard) → epoch UTC, ou None si absente/dégradée.
    Saison 'E' (UTC+2) / 'H' (UTC+1) dite par le compteur → local→UTC sans base fuseaux."""
    if not decoded.get("ts_valid"):
        return None
    s = decoded["ts_season"]
    season = chr(s) if isinstance(s, int) else s
    if season not in ("E", "H"):
        return None
    yy, mo, da, hh, mi, se = decoded["ts_raw"][0:6]
    offset = 2 if season == "E" else 1
    import calendar
    try:
        return calendar.timegm((2000 + yy, mo, da, hh, mi, se, 0, 0, 0)) - offset * 3600
    except (ValueError, OverflowError):
        return None


def _cumulative_offsets_s(decoded: dict):
    off, acc = [], 0.0
    for dt in decoded["sample_dt_s"]:
        acc += dt
        off.append(acc)
    return off


def anchor_timestamps(decoded: dict, t_rx: float):
    """Horodate SYSTÈME des N points (horloge Pi/NTP), s entières. Dernier point ancré à t_rx,
    précédents reculés par les dt cumulés. Approx LoRa (airtime) → préférer meter_timestamps() en std."""
    off = _cumulative_offsets_s(decoded)
    total = off[-1] if off else 0.0
    return [round(t_rx - (total - o)) for o in off]


def meter_timestamps(decoded: dict):
    """Horodate COMPTEUR par point (epoch UTC), ou None. Immunisé au délai LoRa → le meter_ts
    fiable à stocker en standard."""
    t0 = meter_epoch(decoded)
    if t0 is None:
        return None
    return [int(round(t0 + o)) for o in _cumulative_offsets_s(decoded)]


# --------------------------------------------------------------------------- #
# Interprétation TLV : nommer, typer, dire STOCKÉ vs juste LOGUÉ                #
# --------------------------------------------------------------------------- #
TAG_NAMES = {
    T_ADCO: "ADCO", T_ISOUSC: "ISOUSC", T_PREF: "PREF", T_CONTRAT: "CONTRAT",
    T_EAIT: "EAIT", T_LTARF: "LTARF", T_DEMAIN: "DEMAIN", T_NJOURF: "NJOURF",
    T_NJOURF1: "NJOURF+1", T_ADPS: "ADPS", T_PEJP: "PEJP", T_MSG1: "MSG1", T_MSG2: "MSG2",
}
# Tags déjà CÂBLÉS au stockage. Les autres tags CONNUS sont décodés + LOGUÉS (pas encore
# stockés) → visibilité avant câblage (DEMAIN/ADPS/PEJP/NJOURF/MSG).
TAG_STORED = {T_ADCO, T_ISOUSC, T_PREF, T_CONTRAT, T_EAIT, T_LTARF}

_TLV_STR = {T_ADCO, T_CONTRAT, T_LTARF, T_MSG1, T_MSG2}
_TLV_U8 = {T_ISOUSC, T_PREF, T_DEMAIN, T_NJOURF, T_NJOURF1}


def interpret_tlv(tag: int, value: bytes):
    """Valeur typée d'un TLV connu (str / int / présence) ; octets bruts sinon."""
    if tag in _TLV_STR:
        return value.decode("ascii", "replace").strip()
    if tag == T_EAIT:
        return int.from_bytes(value, "little")
    if tag in _TLV_U8:
        return value[0] if value else None
    if tag in (T_ADPS, T_PEJP):
        return value[0] if value else True      # présence (len 0) ou valeur
    return value


def interpret_tlvs(tlvs: dict):
    """[(tag, name, value, known, stored)] par TLV décodé. `known`=tag reconnu ;
    `stored`=déjà câblé au stockage (sinon : à LOGUER)."""
    out = []
    for tag, raw in tlvs.items():
        name = TAG_NAMES.get(tag)
        out.append((tag, name or f"0x{tag:02x}", interpret_tlv(tag, raw),
                    name is not None, tag in TAG_STORED))
    return out
