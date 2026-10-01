#!/usr/bin/env python3
"""health.py — l'instantané que le boîtier joint à son hello (issue #16).

🚨 POURQUOI CE MODULE EXISTE
───────────────────────────
Un boîtier qui **cesse de mesurer** continue de dire bonjour. `last_seen` reste frais côté
cloud, l'OTA passe, et **rien ne le signale**. Constaté sur un boîtier du parc le 2026-10-01 :
NEUF JOURS sans une seule mesure, découverts par hasard — et il est injoignable, ni SSH ni VPN.

⭐ `last_seen` confond trois états :

    mesure et publie          → last_seen frais
    mesure mais ne publie pas → last_seen ancien   (celui-là se voit déjà)
    NE MESURE PLUS            → last_seen FRAIS    🚨 invisible

Ce module ne sert à rien d'autre qu'à séparer le premier état du troisième, et à dire POURQUOI.

🚨 LA RÈGLE QUI PRIME SUR TOUT LE RESTE
───────────────────────────────────────
**Le hello est purement informatif, et cet instantané l'est encore plus.** Une commande qui
pend, un fichier absent, une base verrouillée, un `/proc` qui n'existe pas : RIEN ne doit
empêcher le hello de partir, ni a fortiori la publication des mesures. Chaque sonde est isolée
et se replie sur l'ABSENCE de son champ — jamais sur une exception, jamais sur une valeur
inventée. Même discipline que `_meta()` dans le publisher : on dégrade champ par champ.

⚠️ CE QUI A ÉTÉ ÉCARTÉ, ET POURQUOI — tout vérifié sur un Pi Zero du parc
────────────────────────────────────────────────────────────────────────
· 🚨 `PRAGMA quick_check` : pas « coûteux », **NUISIBLE**. Sur une base de 490 Mo il a poussé la
  charge d'un Pi Zero mono-cœur de 1,15 à **4,50**, rendu `sshd` muet plus d'une minute, et ne
  s'est pas terminé en 180 s. On a abîmé un boîtier de production pour le mesurer.
· 🚨 `journalctl --since` : **9 SECONDES** de balayage, et il a rendu **0 octet** là où `-n 8`
  seul en rendait 8 392 — les dernières erreurs existaient, mais dataient de plus de 24 h.
  Filtrer par date côté boîtier coûtait neuf secondes POUR JETER L'INFORMATION UTILE. On prend
  les dernières avec leur horodatage, et c'est le lecteur qui juge si elles sont vieilles.
· `journalctl --list-boots` : même balayage, même verdict.
· `vcgencmd get_throttled` : échoue en utilisateur `ben` (`/dev/vcio` inaccessible).
· `systemctl status` : prose localisée, et il **pagine** — un script sans `--no-pager` pendrait.

🚨 ON NE TOUCHE À AUCUN MATÉRIEL
────────────────────────────────
`ben-radio` possède le SPI en exclusivité. Tout ce qui concerne la radio se lit dans
`radio-state.json` et la table `lora_link` — JAMAIS par un accès direct. La course au GPIO
perdue par le lecteur contre `ben-radio` a coûté 2 538 plantages en neuf heures (0.9.17).

    python3 health.py [chemin/vers/measurements.db]
"""
import json
import os
import sqlite3
import subprocess
import time

# Les unités qu'on interroge. On ne décide PAS ce qui devrait tourner — c'est une décision de
# `check_network` à partir des capabilities. On rapporte ce qu'on trouve, et une unité absente
# se signale d'elle-même par son absence du rapport.
UNITS = ("ben-radio", "ben-telemetry", "ben-tic-reader", "ben-publisher",
         "ben-local-api", "ben-certd", "ben-recognizer")

VAR = "/var/lib/ben-firmware"
DB_PATH = f"{VAR}/measurements.db"

# ⚠️ Un délai PAR SONDE, et pas seulement un budget global : le coût de `journalctl` VARIE
#    beaucoup — 1 151 ms au premier appel (journal froid), 117 ms ensuite. Sans garde
#    individuelle, une seule commande lente mangerait le budget de toutes les autres.
PROBE_TIMEOUT_S = 4.0
BUDGET_S = 12.0

N_ERRORS = 8        # les N dernières lignes de priorité <= 3
N_FRAMES = 20       # les N dernières trames LoRa : l'état du lien AU MOMENT où il meurt


def _sh(*cmd: str) -> str:
    """Une commande externe, bornée. Rend '' sur n'importe quel échec."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
        return r.stdout
    except Exception:  # noqa: BLE001 — y compris TimeoutExpired et FileNotFoundError
        return ""


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return ""


def _json(path: str) -> dict:
    try:
        d = json.loads(_read(path) or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ── Les sondes. Chacune rend son morceau, ou rien si elle n'a rien à dire ────

def host() -> dict:
    """Le socle : le boîtier redémarre-t-il en boucle, manque-t-il de place, chauffe-t-il."""
    out: dict = {}
    up = _read("/proc/uptime").split()
    if up:
        try:
            out["up"] = int(float(up[0]))
        except ValueError:
            pass
    for line in _read("/proc/stat").splitlines():
        if line.startswith("btime"):
            p = line.split()
            if len(p) > 1 and p[1].isdigit():
                out["boot"] = int(p[1])
    t = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    if t.lstrip("-").isdigit():
        out["temp"] = int(t) // 1000
    la = _read("/proc/loadavg").split()
    if len(la) >= 3:
        # ⭐ `load` est DANS le paquet, et ce n'est pas décoratif : un « 2,4 s » de collecte
        #    mesuré sur un boîtier au repos ne dit rien d'un boîtier en difficulté — et un
        #    boîtier en difficulté est précisément celui dont on lira l'instantané.
        try:
            out["load"] = [float(x) for x in la[:3]]
        except ValueError:
            pass
    mem, cles = {}, {"MemTotal": "total", "MemAvailable": "free", "SwapFree": "swap"}
    for line in _read("/proc/meminfo").splitlines():
        p = line.split()
        if len(p) >= 2 and p[0][:-1] in cles and p[1].isdigit():
            mem[cles[p[0][:-1]]] = int(p[1]) // 1024
    if mem:
        out["mem"] = mem
    # 🚨 Le noyau teinté est un DRAPEAU PERMANENT. C'est lui qui pilotait le test périodique
    #    de 0.9.12 : 208 redémarrages de `ben-radio`, 46 % des mesures perdues. Il vaut 1024
    #    (TAINT_WARN) sur un boîtier du parc au moment où on écrit ces lignes. 1,5 ms.
    tainted = _read("/proc/sys/kernel/tainted").strip()
    if tainted.isdigit():
        out["tainted"] = int(tainted)
    try:
        s = os.statvfs(VAR)
        out["disk_mb"] = int(s.f_bavail * s.f_frsize / 1048576)
    except Exception:  # noqa: BLE001
        pass
    return out


def wifi() -> dict | None:
    """⭐ Jamais regardé jusqu'ici, et c'est DIRECTEMENT la question des « fenêtres de
    connectivité » : un lien WiFi faible explique des coupures qu'on attribuait au fournisseur.
    7,9 ms via `/proc/net/wireless`, sans aucune commande externe."""
    lines = _read("/proc/net/wireless").splitlines()
    if len(lines) < 3:
        return None
    p = lines[-1].split()
    if len(p) < 4:
        return None
    try:
        return {"iface": p[0].rstrip(":"),
                "qual": float(p[2].rstrip(".")),
                "rssi": float(p[3].rstrip("."))}
    except ValueError:
        return None


def repo(path: str = "/opt/ben/repo") -> dict | None:
    """🚨 Un dépôt SALE bloque le `git checkout` NU de l'OTA — piège documenté dans CLAUDE.md,
    qu'on ne pouvait jusqu'ici constater qu'en SSH. Et `tag` peut différer de la version
    déclarée dans `device.json` si une update a échoué entre le checkout et le bump.

    ⚠️ C'est la sonde la plus chère de tout l'instantané : ~1,15 s pour les deux appels git.
    Elle les vaut, une fois par jour, parce qu'elle est le SEUL moyen de voir à distance un
    boîtier dont l'OTA ne passera plus jamais."""
    tag = _sh("git", "-C", path, "describe", "--tags", "--always").strip()
    if not tag:
        return None
    return {"tag": tag, "dirty": bool(_sh("git", "-C", path, "status", "--porcelain").strip())}


def store(conn: sqlite3.Connection, db_path: str = DB_PATH) -> dict:
    """Ce que le boîtier a mesuré, et ce qu'il lui reste à envoyer.

    ⚠️ `max(ts)` se lit PAR PDL (`WHERE pdl_index = ?`) : 100 à 600× d'écart mesuré sur Pi Zero
       entre cette forme et un balayage global.
    ⚠️ Le retard s'encadre par les `rowid` — un `count(*) WHERE sent=0` prend **37 secondes**.
    """
    out: dict = {}
    size = {}
    for key, suffix in (("mb", ""), ("wal_mb", "-wal")):
        try:
            size[key] = round(os.path.getsize(f"{db_path}{suffix}") / 1048576, 1)
        except Exception:  # noqa: BLE001
            pass
    if size:
        out["db"] = size

    try:
        indexes = [i for i, in conn.execute("SELECT pdl_index FROM pdl ORDER BY pdl_index")]
    except sqlite3.Error:
        indexes = []
    meters = []
    for i in indexes:
        entry = {"i": i}
        try:
            entry["last_ts"] = conn.execute(
                "SELECT max(ts) FROM measurements WHERE pdl_index = ?", (i,)).fetchone()[0]
        except sqlite3.Error:
            pass
        meters.append(entry)
    if meters:
        out["pdl"] = meters

    try:
        top, low = conn.execute(
            "SELECT (SELECT max(rowid) FROM measurements), "
            "       (SELECT min(rowid) FROM measurements WHERE sent = 0)").fetchone()
        out["pending"] = 0 if top is None or low is None else max(0, top - low + 1)
    except sqlite3.Error:
        pass

    # `emitter` était au cœur des PDL fantômes : quel compteur au bout de quel émetteur, et
    # depuis quand. Vide sur un boîtier filaire — l'absence est normale.
    try:
        emitters = [{"addr": a, "adco": c or "", "pdl": p, "ts": t} for a, c, p, t in
                    conn.execute("SELECT lora_addr, adco, pdl_index, updated_ts FROM emitter")]
        if emitters:
            out["emitter"] = emitters
    except sqlite3.Error:
        pass

    try:
        out["events_pending"] = conn.execute(
            "SELECT count(*) FROM event WHERE sent = 0").fetchone()[0]
    except sqlite3.Error:
        pass
    return out


def radio(conn: sqlite3.Connection) -> dict | None:
    """⭐ LE CŒUR DU DIAGNOSTIC RADIO, et la seule façon de séparer deux pannes qui donnent
    sinon la MÊME signature — « l'émetteur est muet » et « le récepteur est sourd ».

    · `silence_restarts` est le discriminant : `ben-radio` le remet à 0 à chaque trame reçue et
      l'incrémente quand son détecteur de silence le relance. Après des jours sans trame il est
      donc GRAND si le récepteur écoute vraiment, et NUL si le détecteur n'a jamais joué — donc
      si c'est le récepteur qui est en cause.

    · ⭐⭐ `recent` est le diagnostic RÉTROACTIF, et c'est le vrai gain : `lora_link` garde 180
      jours de rssi/snr par trame. Les 20 dernières AVEC LEURS HORODATAGES coûtent 3,5 ms — et
      sur un boîtier muet depuis des jours, elles datent du jour de sa mort. Chute brutale à
      pleine puissance ⇒ alimentation (cf. la diode BAT85 de ben01). Dégradation progressive
      ⇒ antenne ou portée. **Aucun autre champ ne permet de trancher ça**, et l'information
      s'effacera à 180 jours.

    ⚠️ Rend None sur un boîtier filaire : pas de `radio-state.json`, pas de `lora_link`. C'est
       un état normal, pas une anomalie.
    """
    out: dict = {}
    state = _json(f"{VAR}/radio-state.json")
    if "last_frame_time" in state:
        try:
            out["last_frame"] = int(float(state["last_frame_time"] or 0))
        except (TypeError, ValueError):
            pass
    if "silence_restart_count" in state:
        try:
            out["silence_restarts"] = int(state["silence_restart_count"] or 0)
        except (TypeError, ValueError):
            pass

    lora = _json(f"{VAR}/lora-state.json")
    if lora:
        out["state"] = {k: lora.get(k) for k in
                        ("last_batch_seq", "last_boot_seq", "last_active_id")}
    tx = _json(f"{VAR}/radio-tx.json")
    if tx:
        out["tx"] = tx

    try:
        n, tmin, tmax, avg, lo, hi, snr = conn.execute(
            "SELECT count(*), min(ts), max(ts), round(avg(rssi),1), min(rssi), max(rssi), "
            "       round(avg(snr),1) FROM (SELECT ts, rssi, snr FROM lora_link "
            "                               ORDER BY ts DESC LIMIT ?)", (N_FRAMES,)).fetchone()
        if n:
            out["recent"] = {"n": n, "ts_min": tmin, "ts_max": tmax, "rssi": avg,
                             "rssi_min": lo, "rssi_max": hi, "snr": snr}
    except sqlite3.Error:
        pass
    return out or None


def units() -> list | None:
    """⚠️ `systemctl show --property=`, JAMAIS `systemctl status` : celui-ci rend de la prose
    localisée et PAGINE par défaut — sans `--no-pager` un script pendrait.
    ⚠️ Et `--timestamp=unix` est obligatoire, sinon `ExecMainStartTimestamp` sort en
       « Mon 2026-09-28 17:23:48 CEST », du texte localisé inutilisable."""
    raw = _sh("systemctl", "show", "--timestamp=unix", "--no-pager",
              "--property=Id,ActiveState,SubState,NRestarts,ExecMainStartTimestamp",
              *[u + ".service" for u in UNITS])
    if not raw:
        return None
    blocks, cur = [], {}
    for line in raw.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        blocks.append(cur)

    out = []
    for b in blocks:
        name = b.get("Id", "").removesuffix(".service")
        if not name:
            continue
        entry = {"n": name, "a": b.get("ActiveState"), "s": b.get("SubState")}
        r = (b.get("NRestarts") or "").strip()
        if r.isdigit():
            # 🚨 `NRestarts` est le champ qui aurait crié en 0.9.12 : 208 redémarrages.
            entry["r"] = int(r)
        since = (b.get("ExecMainStartTimestamp") or "").lstrip("@").strip()
        if since.isdigit():
            entry["since"] = int(since)
        out.append(entry)
    return out or None


def errors() -> list | None:
    """Les dernières lignes de priorité <= 3, services ET NOYAU.

    ⭐ Le noyau n'est pas un bonus : c'est là qu'apparaissent les blocages SPI et les
    sous-tensions, soit exactement les pannes d'un boîtier radio devenu muet.

    🚨 PAS de `--since` : voir l'en-tête du module. On rend l'horodatage de chaque ligne,
    c'est le LECTEUR qui juge si elle est vieille."""
    out = []
    for source, extra in (("units", sum([["-u", u] for u in UNITS], [])),
                          ("kernel", ["-k"])):
        raw = _sh("journalctl", "-p", "3", "-n", str(N_ERRORS), "--no-pager",
                  "-o", "json", *extra)
        for line in raw.splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            msg = d.get("MESSAGE")
            if not isinstance(msg, str):
                continue        # un MESSAGE binaire existe ; il n'a rien à faire dans du JSON
            try:
                ts = int(int(d.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000)
            except (TypeError, ValueError):
                ts = 0
            out.append({"u": (d.get("_SYSTEMD_UNIT") or source).removesuffix(".service"),
                        "t": ts, "m": msg[:200]})
    return out or None


def versions(dev: dict) -> dict | None:
    """Ce que le boîtier CROIT être. À recouper avec `repo.tag` : les deux divergent si une
    update a échoué entre le checkout et le bump de `device.json`."""
    if not dev:
        return None
    out = {k: v for k, v in (("model", dev.get("model")),
                             ("sw", dev.get("softwareVersion")),
                             ("arduino", dev.get("arduinoFirmwareVersion"))) if v}
    # ⚠️ `capabilities` est un DICT dans `device.json` — `{"lora": {"hw": "rev01"},
    #    "lora-tic-receiver": {"hw": "rev01", "fw": "0.1.2"}}` — et pas une liste. Ne garder
    #    que les listes faisait disparaître le champ en silence, alors qu'il porte la version
    #    de firmware de l'émetteur : précisément celle qu'on avait jugée incohérente sur un
    #    boîtier du parc. On accepte les deux formes et on transmet tel quel.
    caps = dev.get("capabilities")
    if isinstance(caps, (dict, list)) and caps:
        out["caps"] = caps
    return out or None


# ── L'assemblage ─────────────────────────────────────────────────────────────

# Les sondes versées À PLAT dans le résultat : leur contenu est le socle, pas un sous-objet.
FLAT = ("host", "store")


def snapshot(conn: sqlite3.Connection | None, dev: dict | None = None,
             db_path: str = DB_PATH) -> dict:
    """L'instantané complet. **NE LÈVE JAMAIS.**

    ⭐ `collect_ms` est dans le résultat au même titre que le reste : sans lui, on ne sait pas
    interpréter ce qu'on lit. Un instantané pris en 2,4 s sur un boîtier au repos et un pris en
    30 s sur un boîtier en détresse se ressemblent, et ne disent pas la même chose.

    ⚠️ Un champ ABSENT veut dire « je n'ai pas pu », jamais « ça vaut zéro ». C'est pour ça
    qu'aucune sonde n'invente de valeur de repli.
    """
    start = time.monotonic()
    out: dict = {}

    probes = (("host", host),
              ("wifi", wifi),
              ("dev", lambda: versions(dev or {})),
              ("store", lambda: store(conn, db_path) if conn is not None else None),
              ("radio", lambda: radio(conn) if conn is not None else None),
              ("repo", repo),
              ("units", units),
              ("errors", errors))

    for name, probe in probes:
        if time.monotonic() - start >= BUDGET_S:
            # On le DIT plutôt que de rendre un instantané amputé en silence.
            out["truncated_at"] = name
            break
        try:
            value = probe()
        except Exception:  # noqa: BLE001 — une sonde ne peut pas coûter le hello
            continue
        if not value:
            continue
        if name in FLAT:
            out.update(value)
        else:
            out[name] = value

    out["collect_ms"] = int((time.monotonic() - start) * 1000)
    return out


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        conn = None
    device = {}
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import capabilities as caps
        device = caps.load_device() or {}
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(snapshot(conn, device, path), indent=1, ensure_ascii=False))
