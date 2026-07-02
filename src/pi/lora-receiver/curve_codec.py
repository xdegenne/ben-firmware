"""
curve_codec.py — Décodage de la trame LoRa v0x05 (courbe PAPP batchée, delta + dt par point).

Module **pur** : aucune dépendance GPIO / raspi_lora → importable et testable hors device
(le récepteur main.py importe RPi.GPIO + raspi_lora, qui échouent à l'import sur une machine
de dev). Toute la logique de décodage v0x05 vit ici pour être couverte par un test round-trip.

Spec complète : rasperry/LORA_PROTOCOL.md §17. Layout (longueur variable) :

    Offset  Taille  Champ        Rôle
    0       1       version      uint8 = 0x05
    1       1       flags        bits 0-3 DEMAIN/ADPS/PEJP (idem v0x02 §12.5)
                                 bit4 ts_valid · bit5 src_standard · bit6 has_ext
    2-3     2       batch_seq    uint16 LE — +1 par batch (détection de trous)
    4       1       index_id     uint8 — index actif (BASE=0x00 … BBRHPJR=0x0A)
    5-8     4       index_value  uint32 LE — index absolu au 1er échantillon (Wh)
    9-10    2       papp_ref     uint16 LE — PAPP du 1er échantillon (VA)
    11      1       N            uint8 — nb total d'échantillons (≥ 1)
    12      1       period_ds    uint8 — moyenne/hint du batch en 1/10 s (le timing vient des dt)
    13      1       ts_season    'E'=été · 'H'=hiver · 0x00 = absent (historique)
    14-19   6       ts           YY MM DD hh mm ss (uint8) — historique : tout à 0
    20..    var     points       (N-1) paires [dt, delta PAPP] : dt = écart au point précédent
                                 en SECONDES (varint) ; delta PAPP varint zig-zag (1-3 o)
    [ext]   var     extension    bloc optionnel (has_ext=1) — EAIT injecté (mode standard)
    len-8   8       HMAC         HMAC-SHA256(clé, octets 0..len-9) tronqué 8 octets

Le keyframe porte UN seul index actif (l'émetteur coupe le batch au changement d'index,
cf. §17.3 / décision projet) → un batch est homogène en index_id par construction.
"""

import hashlib
import hmac as _hmac
import struct

PROTOCOL_VERSION_CURVE = 0x05            # un seul émetteur LoRa en service → pas de compat v0x04
KEYFRAME_LEN = 20                        # period_ds conservé (offsets inchangés) = moyenne/hint du batch
HMAC_LEN = 8
MIN_FRAME_LEN = KEYFRAME_LEN + HMAC_LEN  # 28 octets : keyframe (N=1, 0 delta) + HMAC

# Bits de flags spécifiques v0x04 (les bits 0-3 sont communs avec v0x02, cf. main.py).
FLAG_TS_VALID = 0x10
FLAG_SRC_STANDARD = 0x20
FLAG_HAS_EXT = 0x40
FLAG_EXT_V2 = 0x80   # bit7 : bloc ext v2 (bitmask ext_fields) — EAIT / LTARF / DIAG index=0

PAPP_MAX = 0xFFFF  # PAPP est un uint16 côté émetteur

# `<B B H B I H B B B 6s>` : version, flags, batch_seq, index_id, index_value,
# papp_ref, N, period_ds, ts_season, ts(6 o bruts). Total = 20 octets = KEYFRAME_LEN.
_KEYFRAME_FMT = "<BBHBIHBBB6s"
assert struct.calcsize(_KEYFRAME_FMT) == KEYFRAME_LEN


class CurveDecodeError(ValueError):
    """Trame v0x04 invalide (longueur, HMAC, version, deltas tronqués, PAPP aberrante)."""


def _read_varint(buf, pos):
    """Lit un varint LEB128 non signé à partir de `pos`. Retourne (valeur, pos_suivant)."""
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _unzigzag(u):
    """Décode le zig-zag (mappe non-signé → signé : 0,1,2,3,… → 0,-1,1,-2,…)."""
    return (u >> 1) ^ -(u & 1)


def _read_str(buf, pos):
    """Lit un champ `[len:1][ascii:len]` → (str, new_pos). Lève si tronqué."""
    if pos >= len(buf):
        raise CurveDecodeError("ext : champ chaîne tronqué (longueur absente)")
    n = buf[pos]
    pos += 1
    if pos + n > len(buf):
        raise CurveDecodeError("ext : champ chaîne tronqué (ascii)")
    return buf[pos:pos + n].decode("ascii", errors="replace").strip(), pos + n


def decode_v04(payload: bytes, key: bytes) -> dict:
    """Vérifie le HMAC et décode une trame v0x04. Lève `CurveDecodeError` si invalide.

    Retourne un dict :
      version, flags, batch_seq, index_id, index_value, papp_ref, n, period_ds,
      ts_valid, src_standard, has_ext, ts_season, ts_raw, papp (liste de N PAPP).
    """
    if len(payload) < MIN_FRAME_LEN:
        raise CurveDecodeError(f"trame trop courte : {len(payload)} < {MIN_FRAME_LEN}")

    # 1) Authentifier : signed = tout sauf les 8 derniers octets (HMAC).
    signed, mac_recv = payload[:-HMAC_LEN], payload[-HMAC_LEN:]
    mac_exp = _hmac.new(key, signed, hashlib.sha256).digest()[:HMAC_LEN]
    if not _hmac.compare_digest(mac_recv, mac_exp):
        raise CurveDecodeError("HMAC invalide")

    # 2) Keyframe.
    (version, flags, batch_seq, index_id, index_value, papp_ref,
     n, period_ds, ts_season, ts_raw) = struct.unpack_from(_KEYFRAME_FMT, signed, 0)
    if version != PROTOCOL_VERSION_CURVE:
        raise CurveDecodeError(f"version {version:#04x} ≠ 0x05")
    if n < 1:
        raise CurveDecodeError("N = 0")

    src_standard = bool(flags & FLAG_SRC_STANDARD)
    # En STANDARD, papp est le NET SIGNÉ (int16 : soutiré + / injection −). En historique,
    # uint16 (soutiré seul). L'émetteur stocke les 2 mêmes octets ; on les relit selon le flag.
    if src_standard and papp_ref >= 0x8000:
        papp_ref -= 0x10000
    lo, hi = (-0x8000, 0x7FFF) if src_standard else (0, PAPP_MAX)
    if not (lo <= papp_ref <= hi):
        raise CurveDecodeError(f"papp_ref hors bornes : {papp_ref}")

    # 3) Reconstruire la courbe. Chaque point = (dt en SECONDES, delta PAPP zig-zag signé).
    papp = [papp_ref]
    sample_dt_s = [0.0]              # point 0 (keyframe) : dt nul
    pos = KEYFRAME_LEN
    for _ in range(n - 1):
        if pos >= len(signed):
            raise CurveDecodeError("points tronqués (N incohérent avec la longueur)")
        dt, pos = _read_varint(signed, pos)             # dt depuis le point précédent, en secondes
        sample_dt_s.append(float(dt))
        if pos >= len(signed):
            raise CurveDecodeError("points tronqués (dt sans delta PAPP)")
        u, pos = _read_varint(signed, pos)
        v = papp[-1] + _unzigzag(u)
        if not (lo <= v <= hi):   # standard : v peut être négatif (injection) — pas de clamp à 0
            raise CurveDecodeError(f"PAPP reconstruite hors bornes : {v}")
        papp.append(v)

    # 4) Bloc extension v2 en signed[pos:] (flag bit7) : [ext_fields:1] puis, dans l'ordre
    #    des bits — bit0 EAIT (uint32 LE), bit1 LTARF ([len][ascii]), bit2 DIAG index=0
    #    ([n_ht:1][len][ascii]). Un seul émetteur LoRa (ben-0001), reflashé de façon coordonnée
    #    avec le récepteur → PAS de rétro-compat de format de trame (on ne parse que le v2).
    inject_total = None
    ltarf = None
    diag = None
    if flags & FLAG_EXT_V2:
        if pos >= len(signed):
            raise CurveDecodeError("ext v2 : ext_fields absent")
        ext_fields = signed[pos]
        pos += 1
        if ext_fields & 0x01:                       # EAIT
            if pos + 4 > len(signed):
                raise CurveDecodeError("ext v2 : EAIT tronqué")
            inject_total = struct.unpack_from("<I", signed, pos)[0]
            pos += 4
        if ext_fields & 0x02:                       # LTARF (label tarif courant)
            ltarf, pos = _read_str(signed, pos)
        if ext_fields & 0x04:                       # DIAG index=0 (capture ligne EASF brute)
            if pos >= len(signed):
                raise CurveDecodeError("ext v2 : DIAG tronqué (n_ht absent)")
            n_ht = signed[pos]
            pos += 1
            raw, pos = _read_str(signed, pos)
            diag = {"n_ht": n_ht, "raw": raw}

    return {
        "version": version,
        "flags": flags,
        "batch_seq": batch_seq,
        "index_id": index_id,
        "index_value": index_value,
        "papp_ref": papp_ref,
        "n": n,
        "period_ds": period_ds,
        "ts_valid": bool(flags & FLAG_TS_VALID),
        "src_standard": src_standard,
        "has_ext": bool(flags & FLAG_HAS_EXT),
        "ts_season": ts_season,
        "ts_raw": ts_raw,
        "inject_total": inject_total,   # Wh injectés (standard producteur) ou None
        "ltarf": ltarf,                 # libellé tarif courant (ext v2) ou None
        "diag": diag,                   # {n_ht, raw} capture index=0 (ext v2) ou None
        "papp": papp,
        "sample_dt_s": sample_dt_s,     # dt par point en s (v0x05 réel ; v0x04 = period_ds/10 uniforme)
    }


def meter_epoch(decoded: dict) -> int | None:
    """Convertit l'horodate compteur (champ DATE de la TIC standard, portée par la trame)
    en epoch UTC, ou None si absente/non fiable.

    `ts_raw` = 6 octets binaires YY MM DD hh mm ss ; `ts_season` = 'E' (été, UTC+2) ou
    'H' (hiver, UTC+1). Le compteur DIT la saison → conversion locale→UTC sans base de
    fuseaux. Saison minuscule (mode dégradé) ou ts_valid=0 → None (pas fiable)."""
    if not decoded.get("ts_valid"):
        return None
    season = chr(decoded["ts_season"]) if isinstance(decoded["ts_season"], int) else decoded["ts_season"]
    if season not in ("E", "H"):          # minuscule = horloge compteur dégradée → on n'y fait pas foi
        return None
    yy, mo, da, hh, mi, se = decoded["ts_raw"][0:6]
    offset = 2 if season == "E" else 1    # été UTC+2 / hiver UTC+1
    import calendar
    try:
        return calendar.timegm((2000 + yy, mo, da, hh, mi, se, 0, 0, 0)) - offset * 3600
    except (ValueError, OverflowError):
        return None


def _cumulative_offsets_s(decoded: dict) -> list[float]:
    """Décalage cumulé (s) de chaque point vs le 1er, depuis les dt par point (`sample_dt_s`,
    en secondes ; dt[0]=0). v0x05 : dt RÉELS mesurés (horodate compteur en std, millis en
    histo) → espacement fidèle même cadence non uniforme. v0x04 : period_ds/10 uniforme."""
    off, acc = [], 0.0
    for dt in decoded["sample_dt_s"]:
        acc += dt
        off.append(acc)
    return off


def anchor_timestamps(decoded: dict, t_rx: float) -> list[int]:
    """Horodate SYSTÈME des N points (timeline horloge du Pi/NTP), en secondes entières.

    Le DERNIER point est ancré à l'instant de réception `t_rx`, les précédents reculés par
    les dt cumulés (`sample_dt_s`). ⚠ Approximatif en LoRa : la trame arrive APRÈS l'airtime
    → `t_rx` ≠ instant réel du dernier point (décalé de l'airtime, ~1-3 s). En standard,
    préférer `meter_timestamps()` (horodate compteur, immunisée au délai)."""
    off = _cumulative_offsets_s(decoded)
    total = off[-1] if off else 0.0
    return [round(t_rx - (total - o)) for o in off]


def meter_timestamps(decoded: dict) -> list[int] | None:
    """Horodate COMPTEUR par point (epoch UTC entier), ou None si indispo/dégradé.

    t0 = horodate du keyframe (champ DATE de la TIC standard) ; points espacés par les dt
    cumulés (`sample_dt_s`). En standard les dt VIENNENT de l'horodate compteur → on retombe
    EXACTEMENT sur les horodates de chaque mesure. **Immunisé au délai de transmission LoRa**
    (l'heure voyage avec la mesure) → c'est le `meter_ts` fiable à stocker pour le standard."""
    t0 = meter_epoch(decoded)
    if t0 is None:
        return None
    off = _cumulative_offsets_s(decoded)
    return [int(round(t0 + o)) for o in off]
