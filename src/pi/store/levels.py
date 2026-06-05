#!/usr/bin/env python3
"""
levels.py — Niveau de consommation 1..4 du foyer, déduit de la distribution PAPP.

Idée : le niveau n'est pas un seuil absolu mais la **position de la PAPP courante
dans la distribution PAPP du foyer** (relatif à CE foyer). Le boîtier a tout
l'historique SQLite → vrai plancher de nuit, contrairement à l'app.

Séparation des responsabilités (SoC) :
  - L'API (`local_api.py`, **read-only**) appelle `level_for()` : lit les seuils
    pré-calculés, lisse la PAPP, classe → 1..4. AUCUNE écriture.
  - Un **service planifié** (`ben-level-profiler.timer`, ~1×/jour) exécute ce
    fichier (`python3 levels.py` → `refresh_all()`) : (re)calcule les percentiles
    et écrit la table `level_profile`. Seul lui écrit.

Paramètres (calés avec le produit) :
  - Fenêtre des percentiles : 7 jours glissants.
  - Seuils P30 / P70 / P90 → niveaux 1 / 2 / 3 / 4 :
        ≤ P30      → 1  plancher (frigo/veilles, la nuit)   ~30 % du temps
        P30..P70   → 2  activité normale                    ~40 %
        P70..P90   → 3  grosse conso mais normale           ~20 %
        > P90      → 4  très grosse, exceptionnelle         ~10 %
  - Lissage : on classe la PAPP MOYENNE sur ~2 min + hystérésis (sinon le badge
    clignote, les appareils cyclent).
  - Cold-start : tant que < 2 j d'historique / pas assez d'échantillons → on
    renvoie 2 (« normal »). Au début surtout 2, parfois 3, puis ça affine.
"""

import time

import db

WINDOW_SEC = 7 * 24 * 3600     # fenêtre des percentiles
SMOOTH_SEC = 120              # lissage PAPP (2 min)
MIN_SAMPLES = 200            # cold-start : en dessous → niveau 2
MIN_SPAN_SEC = 48 * 3600     # cold-start : < 2 j d'historique → niveau 2
HYST = 0.08                  # hystérésis : 8 % de marge pour changer de palier

# Dernier niveau émis par PDL (hystérésis anti-flicker). État volatil, propre au
# process API : perdu au redémarrage → simplement recalculé, sans conséquence.
_last_level: dict = {}


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[idx]


# ───────────────────────── côté API (read-only) ─────────────────────────

def _read_profile(conn, pdl):
    """Seuils (p_low, p_mid, p_high) si un profil représentatif existe, sinon None."""
    row = conn.execute(
        "SELECT p_low, p_mid, p_high, n_samples "
        "FROM level_profile WHERE pdl_index=?",
        (pdl,),
    ).fetchone()
    if row is not None and row["p_low"] is not None and row["n_samples"] >= MIN_SAMPLES:
        return (row["p_low"], row["p_mid"], row["p_high"])
    return None


def _smoothed_papp(conn, pdl, now, fallback):
    """PAPP moyenne sur les ~2 dernières minutes (lissage), sinon la valeur live."""
    row = conn.execute(
        "SELECT AVG(papp) AS a FROM measurements "
        "WHERE pdl_index=? AND ts>=? AND papp IS NOT NULL",
        (pdl, now - SMOOTH_SEC),
    ).fetchone()
    return row["a"] if row and row["a"] is not None else fallback


def _classify(papp, thr):
    lvl = 1
    for bound in thr:
        if papp > bound:
            lvl += 1
    return lvl


def _with_hysteresis(pdl, lvl, papp, thr):
    """Empêche de basculer de palier dès qu'on est pile sur une borne : il faut
    franchir la borne d'au moins HYST pour changer de niveau."""
    prev = _last_level.get(pdl)
    if prev is not None and abs(lvl - prev) == 1:
        bound = thr[min(lvl, prev) - 1]
        if bound and abs(papp - bound) < HYST * bound:
            lvl = prev
    _last_level[pdl] = lvl
    return lvl


def level_for(conn, pdl_index, current_papp, now=None):
    """Niveau 1..4 pour ce PDL. `conn` = connexion read-only de /live.
    Défaut 2 (« normal ») tant que le profil n'est pas représentatif."""
    now = now or int(time.time())
    thr = _read_profile(conn, pdl_index)
    if thr is None:
        return 2  # cold-start / pas encore de profil
    papp = _smoothed_papp(conn, pdl_index, now, current_papp)
    if papp is None:
        return 2
    return _with_hysteresis(pdl_index, _classify(papp, thr), papp, thr)


# ──────────────────── côté job planifié (écriture seule) ────────────────────

def refresh(conn, pdl, now):
    """Recalcule les percentiles 7 j d'un PDL et upsert son profil. Renvoie True
    si le profil est représentatif (assez de données), False sinon (cold-start)."""
    since = now - WINDOW_SEC
    vals = [r["papp"] for r in conn.execute(
        "SELECT papp FROM measurements "
        "WHERE pdl_index=? AND ts>=? AND papp IS NOT NULL ORDER BY papp",
        (pdl, since),
    )]
    span_row = conn.execute(
        "SELECT MAX(ts) - MIN(ts) AS span FROM measurements "
        "WHERE pdl_index=? AND ts>=?",
        (pdl, since),
    ).fetchone()
    span = span_row["span"] if span_row and span_row["span"] is not None else 0

    enough = len(vals) >= MIN_SAMPLES and span >= MIN_SPAN_SEC
    p_low = p_mid = p_high = None
    if enough:
        p_low = _percentile(vals, 0.30)
        p_mid = _percentile(vals, 0.70)
        p_high = _percentile(vals, 0.90)

    conn.execute(
        "INSERT INTO level_profile"
        "(pdl_index, computed_ts, p_low, p_mid, p_high, n_samples, span_sec) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(pdl_index) DO UPDATE SET "
        "computed_ts=excluded.computed_ts, p_low=excluded.p_low, "
        "p_mid=excluded.p_mid, p_high=excluded.p_high, "
        "n_samples=excluded.n_samples, span_sec=excluded.span_sec",
        (pdl, now, p_low, p_mid, p_high, len(vals), span),
    )
    conn.commit()
    return enough


def refresh_all(now=None):
    """(Re)calcule le profil de tous les PDL présents en base. Point d'entrée du
    service planifié. Renvoie (n_pdls, n_representatifs)."""
    now = now or int(time.time())
    conn = db.connect()  # écriture (user `ben`, WAL → concurrence avec le reader)
    try:
        pdls = [r["pdl_index"] for r in conn.execute(
            "SELECT DISTINCT pdl_index FROM measurements")]
        ok = sum(1 for pdl in pdls if refresh(conn, pdl, now))
    finally:
        conn.close()
    return len(pdls), ok


if __name__ == "__main__":
    total, representative = refresh_all()
    print(f"level_profiler: {representative}/{total} PDL(s) avec profil représentatif")
