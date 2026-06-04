#!/usr/bin/env python3
"""
local_api.py — API HTTP locale BEN (lecture seule) lue par l'app Flutter sur le LAN.

Sert la conso stockée par les readers dans `measurements.db` (cf. db.py).
L'app récupère l'IP du device au provisioning (statut `connected:<ip>`) puis
tape cette API sur le port 8087.

Endpoints :
  GET /health
      → {"deviceId","model","softwareVersion","db":true}
  GET /pdls
      → [{"pdl_index":0,"last_ts":...}, ...]
  GET /live[?pdl_index=N]
      → dernière mesure (≤ ~30 s) par PDL (ou pour un PDL donné)
  GET /measurements?pdl_index=N[&since=ts&until=ts&limit=N]
      → points de conso ordonnés par ts croissant (since défaut = -24 h)
  GET /lora-link?pdl_index=N[&since=ts&limit=N]
      → qualité de réception LoRa (rssi/snr) — modèles pi0-lora

Stdlib only (zéro dépendance — idéal Pi Zero W). Read-only sur la base
(WAL → lectures concurrentes pendant que le reader écrit). LAN-only, read-only ;
durcissement (cert device / token) prévu plus tard.
"""

import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import db
import settings

HOST = "0.0.0.0"
PORT = 8087
DEVICE_JSON = "/etc/ben-firmware/device.json"
DEFAULT_WINDOW_SEC = 24 * 3600
MAX_LIMIT = 10000


def _device_info() -> dict:
    try:
        with open(DEVICE_JSON) as f:
            d = json.load(f)
        return {k: d.get(k) for k in ("deviceId", "model", "softwareVersion")}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"deviceId": None, "model": None, "softwareVersion": None}


def _rows(conn, sql, params) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _int(qs, key, default=None):
    try:
        return int(qs[key][0])
    except (KeyError, ValueError, IndexError):
        return default


class Handler(BaseHTTPRequestHandler):
    # Silence le logging par défaut (sinon une ligne stderr par requête).
    def log_message(self, *args):  # noqa: D401
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")  # app mobile LAN
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = parse_qs(url.query)
        try:
            if path == "/health":
                return self._health()
            if path == "/pdls":
                return self._pdls()
            if path == "/live":
                return self._live(qs)
            if path == "/measurements":
                return self._measurements(qs)
            if path == "/lora-link":
                return self._lora_link(qs)
            if path == "/settings":
                return self._send(settings.load())
            return self._send({"error": "not_found"}, 404)
        except sqlite3.OperationalError:
            # base pas encore créée (aucune trame écrite) → réponse dégradée
            return self._send({"error": "db_unavailable"}, 503)
        except Exception as e:  # noqa: BLE001
            return self._send({"error": "internal", "detail": str(e)}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if path != "/settings":
            return self._send({"error": "not_found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                return self._send({"error": "invalid_body"}, 400)
            # settings.save valide/clamp (ex. led_level 0..5) et persiste.
            return self._send(settings.save(body))
        except (ValueError, json.JSONDecodeError):
            return self._send({"error": "invalid_json"}, 400)
        except Exception as e:  # noqa: BLE001
            return self._send({"error": "internal", "detail": str(e)}, 500)

    # --- handlers -----------------------------------------------------------
    def _health(self):
        info = _device_info()
        db_ok = True
        try:
            db.connect(read_only=True).close()
        except sqlite3.OperationalError:
            db_ok = False
        self._send({**info, "db": db_ok, "now": int(time.time())})

    def _pdls(self):
        with db.connect(read_only=True) as conn:
            rows = _rows(
                conn,
                "SELECT pdl_index, MAX(ts) AS last_ts, COUNT(*) AS points "
                "FROM measurements GROUP BY pdl_index ORDER BY pdl_index",
                (),
            )
        self._send(rows)

    def _live(self, qs):
        pdl = _int(qs, "pdl_index")
        with db.connect(read_only=True) as conn:
            if pdl is not None:
                rows = _rows(
                    conn,
                    "SELECT ts, pdl_index, base, hchc, hchp, papp, iinst "
                    "FROM measurements WHERE pdl_index=? ORDER BY ts DESC LIMIT 1",
                    (pdl,),
                )
            else:
                # Dernière mesure par PDL.
                rows = _rows(
                    conn,
                    "SELECT m.ts, m.pdl_index, m.base, m.hchc, m.hchp, m.papp, m.iinst "
                    "FROM measurements m "
                    "JOIN (SELECT pdl_index, MAX(ts) mx FROM measurements "
                    "      GROUP BY pdl_index) g "
                    "  ON m.pdl_index=g.pdl_index AND m.ts=g.mx "
                    "ORDER BY m.pdl_index",
                    (),
                )
        self._send(rows)

    def _measurements(self, qs):
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        now = int(time.time())
        since = _int(qs, "since", now - DEFAULT_WINDOW_SEC)
        until = _int(qs, "until", now)
        limit = min(_int(qs, "limit", MAX_LIMIT) or MAX_LIMIT, MAX_LIMIT)
        with db.connect(read_only=True) as conn:
            rows = _rows(
                conn,
                "SELECT ts, base, hchc, hchp, papp, iinst FROM measurements "
                "WHERE pdl_index=? AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT ?",
                (pdl, since, until, limit),
            )
        self._send({"pdl_index": pdl, "since": since, "until": until,
                    "count": len(rows), "points": rows})

    def _lora_link(self, qs):
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        now = int(time.time())
        since = _int(qs, "since", now - DEFAULT_WINDOW_SEC)
        limit = min(_int(qs, "limit", MAX_LIMIT) or MAX_LIMIT, MAX_LIMIT)
        with db.connect(read_only=True) as conn:
            rows = _rows(
                conn,
                "SELECT ts, rssi, snr FROM lora_link "
                "WHERE pdl_index=? AND ts>=? ORDER BY ts ASC LIMIT ?",
                (pdl, since, limit),
            )
        self._send({"pdl_index": pdl, "since": since,
                    "count": len(rows), "points": rows})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ben-local-api en écoute sur {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
