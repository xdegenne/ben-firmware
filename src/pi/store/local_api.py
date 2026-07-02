#!/usr/bin/env python3
"""
local_api.py — API HTTP locale BEN (lecture seule) lue par l'app Flutter sur le LAN.

Sert la conso stockée par les readers dans `measurements.db` (cf. db.py).
L'app récupère l'IP du device au provisioning (statut `connected:<ip>`) puis
tape cette API sur le port 8087.

Endpoints :
  GET /ping
      → {"ben":true} — sonde de présence ultra-light, AUCUNE lecture
        (ni fichier ni base) ; pensée pour du polling régulier
  GET /health
      → {"deviceId","model","softwareVersion","db":true,
         "last_tic_ts":<ts dernière trame TIC ou null>,"now":...}
  GET /pdls
      → [{"pdl_index":0,"last_ts":...}, ...]
  GET /live[?pdl_index=N]
      → dernière mesure (≤ ~30 s) par PDL (ou pour un PDL donné)
  GET /measurements?pdl_index=N[&since=ts&until=ts&limit=N]
      → points de conso ordonnés par ts croissant (since défaut = -24 h).
        Degrade-safe : si > limit points fins → agrégé à ~limit buckets
        (min/max/avg) au lieu d'être tronqué. `downsampled` indique le cas.
  GET /curve?pdl_index=N[&since=ts&until=ts&buckets=K]
      → courbe agrégée par bucket (min/max/avg, pics préservés). `buckets` =
        résolution voulue par l'app (défaut 500, max 2000). Endpoint riche.
  GET /consumption?pdl_index=N[&since=ts&until=ts]
      → conso PAR REGISTRE (Wh) sur la plage : {by_register:[{src_standard,
        index_id,wh}],total_wh}. Carry-forward server-side (MAX-MIN par registre,
        bi-mode). Contrat commun Pi/cloud ; l'app applique le prix (Σ wh×prix).
  GET /lora-link?pdl_index=N[&since=ts&limit=N]
      → qualité de réception LoRa (rssi/snr) — modèles pi0-lora

Stdlib only (zéro dépendance — idéal Pi Zero W). Read-only sur la base
(WAL → lectures concurrentes pendant que le reader écrit). LAN-only, read-only ;
durcissement (cert device / token) prévu plus tard.
"""

import json
import os
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import db
import levels
import settings

HOST = "0.0.0.0"
PORT = 8087
DEVICE_JSON = "/etc/ben-firmware/device.json"
DEFAULT_WINDOW_SEC = 24 * 3600
MAX_LIMIT = 10000
DEFAULT_CURVE_BUCKETS = 500   # points servis par défaut (≈ largeur écran)
MAX_CURVE_BUCKETS = 2000      # plafond : au-delà, inutile (densité > pixels)


def _device_info() -> dict:
    try:
        with open(DEVICE_JSON) as f:
            d = json.load(f)
        info = {k: d.get(k) for k in ("deviceId", "model", "softwareVersion")}
    except (FileNotFoundError, json.JSONDecodeError):
        info = {"deviceId": None, "model": None, "softwareVersion": None}
    # Date de la dernière MAJ firmware (epoch s) = mtime de device.json : ce
    # fichier n'est réécrit qu'à un changement de version (OTA) ou au
    # provisioning. Pas de champ stocké, on lit la métadonnée filesystem.
    try:
        info["lastUpdateTs"] = int(os.path.getmtime(DEVICE_JSON))
    except OSError:
        info["lastUpdateTs"] = None
    return info


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
            if path == "/ping":
                return self._ping()
            if path == "/health":
                return self._health()
            if path == "/pdls":
                return self._pdls()
            if path == "/live":
                return self._live(qs)
            if path == "/measurements":
                return self._measurements(qs)
            if path == "/curve":
                return self._curve(qs)
            if path == "/consumption":
                return self._consumption(qs)
            if path == "/registers":
                return self._registers(qs)
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
        if path == "/unprovision":
            return self._unprovision(parse_qs(url.query))
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
    def _ping(self):
        # Présence ultra-light : AUCUNE lecture (ni fichier ni base). Pensé pour
        # du polling régulier (voyant de joignabilité, heartbeat de l'app).
        self._send({"ben": True})

    def _health(self):
        info = _device_info()
        db_ok = True
        last_tic = None
        try:
            with db.connect(read_only=True) as conn:
                row = conn.execute("SELECT MAX(ts) FROM measurements").fetchone()
                last_tic = row[0] if row else None
        except sqlite3.OperationalError:
            db_ok = False
        self._send({**info, "db": db_ok, "last_tic_ts": last_tic,
                    "now": int(time.time())})

    def _unprovision(self, qs):
        """Désappaire le boîtier : oublie le WiFi (→ provisioning BLE au prochain
        allumage), efface optionnellement les données (`?wipe=1`), puis S'ÉTEINT.

        Le boîtier s'éteint TOUJOURS (poweroff), avec ou sans wipe : il part hors
        tension ; quand l'utilisateur le rallume, l'absence de WiFi le fait
        démarrer en mode configuration (BLE).

        ORDRE CRITIQUE : on répond AVANT de couper le réseau, puis on fait le
        désappairage + reboot en ASYNCHRONE. Sinon supprimer la connexion WiFi
        tue le lien TCP et l'app ne reçoit jamais la réponse.
        Raccourci assumé (pas en prod) : AUCUNE auth. Garde l'identité (certs)."""
        wipe = (qs.get("wipe", ["0"])[0]).lower() in ("1", "true", "yes")
        print(f"[unprovision] requête reçue (wipe={wipe})", flush=True)
        # 1. Réponse immédiate — le réseau est encore là, l'app reçoit l'ack.
        self._send({"ok": True, "wipe": wipe, "rebooting": True})

        # 2. Désappairage + reboot DIFFÉRÉS (laisse la réponse HTTP partir).
        def _teardown():
            # Oublie TOUTES les connexions WiFi (ben-provisioned + éventuel profil
            # opérateur du golden) → repart en provisioning BLE au prochain boot.
            listing = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                capture_output=True, text=True)
            for line in listing.stdout.splitlines():
                name, _, ctype = line.partition(":")
                if ctype == "802-11-wireless" and name:
                    r = subprocess.run(
                        ["sudo", "nmcli", "connection", "delete", name],
                        capture_output=True, text=True)
                    print(f"[unprovision] delete '{name}' rc={r.returncode} "
                          f"{r.stderr.strip()}", flush=True)
            if wipe:
                # Stop le(s) reader(s) AVANT le rm : la base n'est alors plus ouverte (WAL)
                # → wipe propre (pas d'écriture dans un inode supprimé, pas de -wal recréé).
                # On NE touche PAS ben-local-api : c'est lui qui exécute _teardown.
                subprocess.run(
                    ["sudo", "systemctl", "stop", "ben-tic-reader", "ben-lora-receiver"],
                    stderr=subprocess.DEVNULL)  # le service absent selon le modèle → ignoré
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.remove(db.DB_PATH + suffix)
                        print(f"[unprovision] wipe {db.DB_PATH}{suffix}",
                              flush=True)
                    except OSError:
                        pass
            # Désappairage → le boîtier S'ÉTEINT TOUJOURS (poweroff), avec ou sans
            # wipe : il part hors tension. Au prochain allumage, plus de WiFi →
            # démarrage en mode configuration (BLE). (Avant : reboot quand pas de
            # wipe ; on éteint désormais dans tous les cas pour un signal clair.)
            print(f"[unprovision] poweroff (wipe={wipe})", flush=True)
            subprocess.Popen(["sudo", "systemctl", "poweroff"])

        threading.Timer(2.0, _teardown).start()

    def _pdls(self):
        with db.connect(read_only=True) as conn:
            rows = _rows(
                conn,
                # first_ts = 1re mesure du PDL → l'app en déduit l'âge
                # d'apprentissage (now - first_ts) pour dimensionner sa fenêtre de
                # lissage anti-Hawthorne (volet D, présentation côté app).
                "SELECT pdl_index, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
                "COUNT(*) AS points "
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
                    "SELECT ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, "
                    "index_id, index_value, src_standard, inject_total "
                    "FROM measurements WHERE pdl_index=? ORDER BY ts DESC LIMIT 1",
                    (pdl,),
                )
            else:
                # Dernière mesure par PDL.
                rows = _rows(
                    conn,
                    "SELECT m.ts, m.pdl_index, m.base, m.hchc, m.hchp, m.papp, "
                    "m.iinst, m.tariff, m.index_id, m.index_value, m.src_standard, "
                    "m.inject_total "
                    "FROM measurements m "
                    "JOIN (SELECT pdl_index, MAX(ts) mx FROM measurements "
                    "      GROUP BY pdl_index) g "
                    "  ON m.pdl_index=g.pdl_index AND m.ts=g.mx "
                    "ORDER BY m.pdl_index",
                    (),
                )
            # Niveau de conso 1..4 (visuel app), seuils pré-calculés par
            # ben-level-profiler ; ici lecture seule (cf. levels.py).
            now = int(time.time())
            for row in rows:
                row["level"] = levels.level_for(
                    conn, row["pdl_index"], row.get("papp"), now)
                # Mode TIC du PDL ('standard'/'historique'/null) — affiché par l'app.
                # En standard, `papp` est le NET SIGNÉ (>0 soutiré, <0 surplus injecté).
                mode = db.tic_mode(conn, row["pdl_index"])
                row["tic_mode"] = mode
                # Producteur = injection CONSTATÉE (pas juste EAIT présent) → l'app
                # affiche la jauge bidir soutirage⇄injection (Lot C). inject_total
                # est déjà dans la ligne (index monotone → dernière valeur = MAX).
                row["producer"] = db.producer(
                    conn, row["pdl_index"], row.get("inject_total"))
                # Abonnement souscrit (réglages app + étalonnage jauge), exposé brut
                # dans son unité d'origine : ISOUSC (A) en histo, PREF (kVA) en standard.
                # Étalonnage maxVa : on prend la source du MODE COURANT (standard→PREF×1000,
                # histo→ISOUSC×230), avec repli sur l'autre si la valeur du mode manque encore
                # (transition / cold-start). None tant que rien n'est reçu.
                isousc = db.get_isousc(conn, row["pdl_index"])
                pref = db.get_pref(conn, row["pdl_index"])
                row["isousc"] = isousc
                row["pref"] = pref
                from_isousc = isousc * 230 if isousc else None
                from_pref = pref * 1000 if pref else None
                if mode == "standard":
                    row["maxVa"] = from_pref or from_isousc
                else:
                    row["maxVa"] = from_isousc or from_pref
                # Libellé tarifaire EN COURS (jauge HP/HC) — résolu côté serveur :
                # standard→LTARF autoritatif, histo→convention PTEC. None → l'app garde
                # sa propre convention (rétro-compat). Cf. chantier unification labels.
                row["tariff_label"] = db.resolve_label(
                    conn, row["pdl_index"], row.get("src_standard"), row.get("index_id"))
                # Contrat (NGTF, quasi-statique) — distinct du tarif en cours ci-dessus.
                row["contract"] = db.get_ngtf(conn, row["pdl_index"])
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
            # Degrade-safe : la lecture au fil de l'eau densifie la courbe (~7×).
            # Si la plage contient plus de `limit` points fins, on AGRÈGE à ~limit
            # buckets (min/max/avg) au lieu de tronquer aux plus VIEUX via
            # `ORDER BY ts ASC LIMIT` — qui affichait un bord périmé. Une app pas
            # à jour reçoit ainsi une courbe complète et allégée, sans rien changer
            # côté app (cf. chantier-courbe-temps-reel.md, compat).
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM measurements "
                "WHERE pdl_index=? AND ts>=? AND ts<=? AND papp IS NOT NULL",
                (pdl, since, until),
            ).fetchone()["c"]
            if total > limit and until > since:
                bucket_sec = max(1, (until - since) // limit)
                rows = db.curve_buckets(conn, pdl, since, until, bucket_sec)
                downsampled = True
            else:
                rows = _rows(
                    conn,
                    "SELECT ts, base, hchc, hchp, papp, iinst, tariff, "
                    "index_id, index_value, src_standard, inject_total FROM measurements "
                    "WHERE pdl_index=? AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT ?",
                    (pdl, since, until, limit),
                )
                downsampled = False
        self._send({"pdl_index": pdl, "since": since, "until": until,
                    "count": len(rows), "downsampled": downsampled, "points": rows})

    def _curve(self, qs):
        """Courbe agrégée par bucket — endpoint riche pour l'app à jour. L'app
        pilote la résolution (`buckets` = largeur de son viewport) ; le firmware
        agrège (min/max/avg, pics préservés). Le lissage anti-Hawthorne est
        appliqué PAR L'APP par-dessus (présentation pure, volet D)."""
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        now = int(time.time())
        since = _int(qs, "since", now - DEFAULT_WINDOW_SEC)
        until = _int(qs, "until", now)
        if until <= since:
            return self._send({"error": "bad_range"}, 400)
        buckets = _int(qs, "buckets", DEFAULT_CURVE_BUCKETS) or DEFAULT_CURVE_BUCKETS
        buckets = max(1, min(buckets, MAX_CURVE_BUCKETS))
        bucket_sec = max(1, (until - since) // buckets)
        with db.connect(read_only=True) as conn:
            pts = db.curve_buckets(conn, pdl, since, until, bucket_sec)
        self._send({"pdl_index": pdl, "since": since, "until": until,
                    "bucket_sec": bucket_sec, "count": len(pts), "points": pts})

    def _consumption(self, qs):
        """Conso par registre sur [since, until] — carry-forward server-side
        (cf. db.consumption). Contrat commun Pi/cloud ; l'app appelle en débounce
        au repos du pan/zoom et applique le prix (Σ wh × prix)."""
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        now = int(time.time())
        since = _int(qs, "since", now - DEFAULT_WINDOW_SEC)
        until = _int(qs, "until", now)
        if until <= since:
            return self._send({"error": "bad_range"}, 400)
        with db.connect(read_only=True) as conn:
            res = db.consumption(conn, pdl, since, until)
        self._send({"pdl_index": pdl, "since": since, "until": until, **res})

    def _registers(self, qs):
        """Registres tarifaires d'un PDL (libellé résolu server-side + dernier index).
        Sert la carte réglages de l'app — un registre par tarif (Base / HC / HP…)."""
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        with db.connect(read_only=True) as conn:
            regs = db.registers(conn, pdl)
            contract = db.get_ngtf(conn, pdl)   # NGTF = le contrat (calendrier fournisseur)
        self._send({"pdl_index": pdl, "contract": contract, "registers": regs})

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
