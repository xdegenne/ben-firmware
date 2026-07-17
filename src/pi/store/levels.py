#!/usr/bin/env python3
"""
levels.py — Niveau de consommation 1..4 du foyer, normalisé sur sa « course ».

Idée : le niveau = **position de la PAPP courante entre le talon (bas) et le
plafond (haut) du foyer**, propre à CE foyer.
    ratio = (PAPP_lissée − talon) / (plafond − talon)   clampé [0, 1]
    bandes : <0,10 →1   0,10–0,40 →2   0,40–0,70 →3   >0,70 →4
  - talon   : ancrage bas = percentile bas (P15) sur la fenêtre (baseload).
  - plafond : `papp_max_alltime`, high-water mark MONOTONE de la PAPP (jamais
              décrémenté, survit au prune 3 mois). « Si c'est arrivé, ça
              arrivera encore » → vrai plafond du foyer. Maintenu par le reader
              dans db.record_measurement (pas ici).
Pas de seuil absolu en watts : les deux bornes sont tirées des données du foyer
→ adaptatif (gros foyer = gros plafond) ET non-dégénéré (contrairement à un P90
qui s'effondre sur une maison à faible base).

Séparation des responsabilités (SoC) :
  - L'API (`local_api.py`, **read-only**) appelle `level_for()` : lit talon +
    plafond, lisse la PAPP, classe → 1..4. AUCUNE écriture.
  - Le **service planifié** (`ben-level-profiler.timer`, ~1×/jour) exécute ce
    fichier (`refresh_all()`) : recalcule le `talon` (SQL) et écrit son profil.
  - Le **reader** (db.record_measurement) maintient `papp_max_alltime` au fil de
    l'eau. Trois écrivains distincts, colonnes disjointes.

Cold-start : tant que < 2 j d'historique / pas assez d'échantillons, ou pas
encore de dynamique (plafond ≤ talon) → niveau 2 (« normal »). En pratique la
1re grosse conso (douche/café à l'install) seede le plafond dès le jour 1.
"""

import time

import db

WINDOW_SEC = 30 * 24 * 3600   # fenêtre pour le talon (percentile bas)
TALON_Q = 0.15               # talon = P15 de la PAPP (ancrage bas, baseload)
SMOOTH_SEC = 120             # lissage PAPP (2 min)
MIN_SAMPLES = 200            # cold-start : en dessous → niveau 2
MIN_SPAN_SEC = 48 * 3600     # cold-start : < 2 j d'historique → niveau 2
HYST = 0.08                  # hystérésis : 8 % de marge pour changer de palier
LEVEL_BANDS = (0.10, 0.40, 0.70)  # ratio → 1/2/3/4

# Dernier niveau émis par PDL (hystérésis anti-flicker). État volatil, propre au
# process API : perdu au redémarrage → simplement recalculé, sans conséquence.
_last_level: dict = {}


# ───────────────────────── côté API (read-only) ─────────────────────────

def _read_profile(conn, pdl):
    """(talon, plafond) si le profil est représentatif et a une dynamique
    (plafond > talon), sinon None (→ cold-start niveau 2)."""
    row = conn.execute(
        "SELECT talon, papp_max_alltime, n_samples, span_sec "
        "FROM level_profile WHERE pdl_index=?",
        (pdl,),
    ).fetchone()
    if (row is None or row["talon"] is None or row["papp_max_alltime"] is None
            or row["n_samples"] < MIN_SAMPLES or row["span_sec"] < MIN_SPAN_SEC):
        return None
    talon, plafond = row["talon"], row["papp_max_alltime"]
    if plafond <= talon:  # pas encore de dynamique (ex. frigo seul) → cold-start
        return None
    return (talon, plafond)


def is_known(conn, pdl_index):
    """True si le foyer est CONNU (profil représentatif : ≥ MIN_SAMPLES, ≥ MIN_SPAN_SEC
    et dynamique plafond>talon) — EXACTEMENT le même gate que le niveau. L'API s'en sert
    pour décider si la jauge peut se caler sur le plafond OBSERVÉ (foyer connu) ou doit
    retomber sur l'abonnement (phase d'apprentissage → pas de faux rouge à l'unboxing)."""
    return _read_profile(conn, pdl_index) is not None


def _boundaries(talon, plafond):
    """Convertit les bandes de ratio en 3 seuils PAPP absolus, pour réutiliser
    _classify / _with_hysteresis tels quels."""
    span = plafond - talon
    return tuple(talon + b * span for b in LEVEL_BANDS)


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
    prof = _read_profile(conn, pdl_index)
    if prof is None:
        return 2  # cold-start / pas encore de dynamique
    talon, plafond = prof
    papp = _smoothed_papp(conn, pdl_index, now, current_papp)
    if papp is None:
        return 2
    thr = _boundaries(talon, plafond)
    return _with_hysteresis(pdl_index, _classify(papp, thr), papp, thr)


# ──────────────────── côté job planifié (écriture seule) ────────────────────

def refresh(conn, pdl, now):
    """Recalcule le `talon` (P15 de la PAPP sur la fenêtre, EN SQL → pas de
    transfert de lignes) et upsert le profil. Ne touche PAS `papp_max_alltime`
    (maintenu par le reader). Renvoie True si représentatif, sinon False."""
    since = now - WINDOW_SEC
    row = conn.execute(
        "WITH d AS ("
        "  SELECT papp, ROW_NUMBER() OVER (ORDER BY papp) AS rn, "
        "         COUNT(*) OVER () AS n "
        "  FROM measurements "
        "  WHERE pdl_index=? AND ts>=? AND papp IS NOT NULL"
        ") "
        "SELECT MAX(CASE WHEN rn = CAST(? * (n - 1) AS INT) + 1 THEN papp END) "
        "         AS talon, "
        "       MAX(n) AS n "
        "FROM d",
        (pdl, since, TALON_Q),
    ).fetchone()
    talon = row["talon"] if row else None
    n = (row["n"] if row else 0) or 0
    span_row = conn.execute(
        "SELECT IFNULL(MAX(ts) - MIN(ts), 0) AS span FROM measurements "
        "WHERE pdl_index=? AND ts>=? AND papp IS NOT NULL",
        (pdl, since),
    ).fetchone()
    span = span_row["span"] if span_row else 0

    # NB : pas de papp_max_alltime ni p_low/p_mid/p_high dans le DO UPDATE →
    # le high-water mark (reader) et les colonnes legacy sont préservés.
    conn.execute(
        "INSERT INTO level_profile "
        "(pdl_index, computed_ts, talon, n_samples, span_sec) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(pdl_index) DO UPDATE SET "
        "computed_ts=excluded.computed_ts, talon=excluded.talon, "
        "n_samples=excluded.n_samples, span_sec=excluded.span_sec",
        (pdl, now, talon, n, span),
    )
    conn.commit()
    return n >= MIN_SAMPLES and span >= MIN_SPAN_SEC


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
