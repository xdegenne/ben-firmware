"""
test_v04_store.py — Intégration v0x04 → measurements → /curve (db.py réel, sqlite seul).

Prouve le point critique du chantier : en estampillant l'index actif (constant sur
le batch) sur CHAQUE échantillon, `curve_buckets()` — qui récupère l'index via un
JOIN sur le ts_max de chaque bucket — ne renvoie JAMAIS d'index/tariff NULL. Reproduit
le chemin de stockage de `on_recv_curve()` sans dépendance hardware.

    python3 src/pi/lora-receiver/test_v04_store.py
"""

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "store"))

import curve_codec as cc        # noqa: E402
import db                       # noqa: E402
from test_v04_roundtrip import KEY, encode_v04  # noqa: E402

# Sous-ensemble du mapping de main.py suffisant pour le test (BASE/HC/HP).
INDEX_NAMES = {0x00: "BASE", 0x01: "HCHC", 0x02: "HCHP"}


def _rows_like_on_recv_curve(decoded, pdl_index, t_rx):
    """Identique à on_recv_curve : un row/échantillon, index estampillé partout."""
    active = INDEX_NAMES[decoded["index_id"]]
    ts = cc.anchor_timestamps(decoded, t_rx)
    return [
        (pdl_index, {active: decoded["index_value"], "PAPP": decoded["papp"][i]}, ts[i])
        for i in range(decoded["n"])
    ]


def main():
    tmp = tempfile.mktemp(suffix=".db")
    conn = db.connect(tmp)
    try:
        # Batch HCHP de 30 échantillons (PAPP variable, dont un gros saut).
        samples = [500 + (i * 37 % 400) for i in range(29)] + [2600]
        decoded = cc.decode_v04(
            encode_v04(samples, index_id=0x02, index_value=9_000_000, batch_seq=1), KEY)

        t_rx = 1_700_000_000
        rows = _rows_like_on_recv_curve(decoded, pdl_index=1, t_rx=t_rx)
        n = db.record_measurements_batch(conn, rows)
        assert n == 30, n

        # /curve : chaque bucket doit porter hchp + tariff (sinon coût-par-diff cassé).
        since, until = t_rx - 30 * 2, t_rx
        pts = db.curve_buckets(conn, 1, since, until, bucket_sec=2)
        assert pts, "aucun bucket"
        for p in pts:
            assert p["hchp"] == 9_000_000, f"index NULL/incorrect dans un bucket: {p}"
            assert p["tariff"] == 2, f"tariff attendu 2 (HCHP), eu {p['tariff']}"
            assert p["papp"] is not None
            assert p["base"] is None and p["hchc"] is None  # registres inactifs

        # PAPP agrégée cohérente avec la courbe source + pic préservé (min/max).
        assert max(p["papp_max"] for p in pts) == max(samples)
        assert min(p["papp_min"] for p in pts) == min(samples)

        # High-water mark PAPP alimenté pour le calcul de niveau (jauge).
        hw = conn.execute(
            "SELECT papp_max_alltime FROM level_profile WHERE pdl_index=1").fetchone()
        assert hw and hw["papp_max_alltime"] == max(samples), hw

        print(f"OK — {len(pts)} buckets, index/tariff non-NULL partout, "
              f"{n} mesures, pic PAPP={max(samples)} préservé")
    finally:
        conn.close()
        for f in (tmp, tmp + "-wal", tmp + "-shm"):
            if os.path.exists(f):
                os.unlink(f)


if __name__ == "__main__":
    main()
