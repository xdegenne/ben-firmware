"""
curve_codec.py — Décodage de la trame LoRa v0x04 (courbe PAPP batchée, delta + keyframe).

Module **pur** : aucune dépendance GPIO / raspi_lora → importable et testable hors device
(le récepteur main.py importe RPi.GPIO + raspi_lora, qui échouent à l'import sur une machine
de dev). Toute la logique de décodage v0x04 vit ici pour être couverte par un test round-trip.

Spec complète : rasperry/LORA_PROTOCOL.md §17. Layout (longueur variable) :

    Offset  Taille  Champ        Rôle
    0       1       version      uint8 = 0x04
    1       1       flags        bits 0-3 DEMAIN/ADPS/PEJP (idem v0x02 §12.5)
                                 bit4 ts_valid · bit5 src_standard · bit6 has_ext
    2-3     2       batch_seq    uint16 LE — +1 par batch (détection de trous)
    4       1       index_id     uint8 — index actif (BASE=0x00 … BBRHPJR=0x0A)
    5-8     4       index_value  uint32 LE — index absolu au 1er échantillon (Wh)
    9-10    2       papp_ref     uint16 LE — PAPP du 1er échantillon (VA)
    11      1       N            uint8 — nb total d'échantillons (≥ 1)
    12      1       period_ds    uint8 — période nominale, en dixièmes de s (20 = 2,0 s)
    13      1       ts_season    'E'=été · 'H'=hiver · 0x00 = absent (historique)
    14-19   6       ts           YY MM DD hh mm ss (uint8) — historique : tout à 0
    20..    var     deltas       (N-1) deltas PAPP, varint zig-zag (1-3 o chacun)
    [ext]   var     extension    bloc optionnel (has_ext=1) — différé (mode standard)
    len-8   8       HMAC         HMAC-SHA256(clé, octets 0..len-9) tronqué 8 octets

Le keyframe porte UN seul index actif (l'émetteur coupe le batch au changement d'index,
cf. §17.3 / décision projet) → un batch est homogène en index_id par construction.
"""

import hashlib
import hmac as _hmac
import struct

PROTOCOL_VERSION_CURVE = 0x04
KEYFRAME_LEN = 20
HMAC_LEN = 8
MIN_FRAME_LEN = KEYFRAME_LEN + HMAC_LEN  # 28 octets : keyframe (N=1, 0 delta) + HMAC

# Bits de flags spécifiques v0x04 (les bits 0-3 sont communs avec v0x02, cf. main.py).
FLAG_TS_VALID = 0x10
FLAG_SRC_STANDARD = 0x20
FLAG_HAS_EXT = 0x40

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
        raise CurveDecodeError(f"version {version:#04x} ≠ 0x04")
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

    # 3) Reconstruire la courbe PAPP par intégration des deltas (varint zig-zag SIGNÉ).
    papp = [papp_ref]
    pos = KEYFRAME_LEN
    for _ in range(n - 1):
        if pos >= len(signed):
            raise CurveDecodeError("deltas tronqués (N incohérent avec la longueur)")
        u, pos = _read_varint(signed, pos)
        v = papp[-1] + _unzigzag(u)
        if not (lo <= v <= hi):   # standard : v peut être négatif (injection) — pas de clamp à 0
            raise CurveDecodeError(f"PAPP reconstruite hors bornes : {v}")
        papp.append(v)

    # 4) Bloc extension (has_ext=1) en signed[pos:] : énergie injectée totale EAIT (uint32 LE).
    inject_total = None
    if flags & FLAG_HAS_EXT:
        if pos + 4 > len(signed):
            raise CurveDecodeError("bloc extension tronqué (has_ext mais < 4 octets)")
        inject_total = struct.unpack_from("<I", signed, pos)[0]
        pos += 4

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
        "papp": papp,
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


def anchor_timestamps(decoded: dict, t_rx: float) -> list[int]:
    """Horodate SYSTÈME des N échantillons (timeline horloge du Pi/NTP).

    Le DERNIER échantillon est ancré à l'instant de réception `t_rx`, les précédents
    espacés en arrière de `period_ds`. `period_ds` reste nominal (jitter de la cadence
    TIC) — bon pour l'ordre/espacement. ⚠ Approximatif en LoRa : le batch est transmis
    APRÈS accumulation (+ délai duty-cycle) → `t_rx` ≠ instant réel du dernier échantillon.
    En standard, préférer `meter_timestamps()` (horodate compteur, immunisée au délai).
    """
    n = decoded["n"]
    period_s = decoded["period_ds"] / 10.0
    return [int(t_rx - (n - 1 - i) * period_s) for i in range(n)]


def meter_timestamps(decoded: dict) -> list[int] | None:
    """Horodate COMPTEUR par échantillon (epoch UTC), ou None si indispo/dégradé.

    L'horodate de la trame est celle du 1er échantillon (keyframe = champ DATE de la TIC
    standard) → ancrage AVANT : t[i] = t0 + i*period_s. Contrairement à l'ancrage
    réception, c'est **immunisé au délai de transmission LoRa** (l'heure voyage avec la
    mesure) → c'est le `meter_ts` fiable à stocker pour le standard."""
    t0 = meter_epoch(decoded)
    if t0 is None:
        return None
    period_s = decoded["period_ds"] / 10.0
    n = decoded["n"]
    return [int(t0 + i * period_s) for i in range(n)]
