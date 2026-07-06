#!/usr/bin/env python3
"""banc_inject — INJECTEUR du banc de test BEN (tourne sur ben-ops, sur /dev/ttyAMA0).

Lit banc_scenarios.SCENARIOS, construit + injecte les trames TIC (std/histo) de chaque scénario,
cycle, et SWITCH le baud (1200↔9600) sur changement de mode. Écrit le scénario courant dans CURRENT_FILE.

Usage :  ./banc_inject.py [/dev/ttyAMA0] [--loop]
  --loop : rejoue les scénarios en boucle (sinon 1 passe puis quitte).
"""
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    sys.exit("pyserial requis :  sudo apt install python3-serial")

from banc_scenarios import SCENARIOS, CURRENT_FILE

STX, ETX, LF, CR, SP, HT = "\x02", "\x03", "\x0a", "\x0d", "\x20", "\x09"
BAUD = {"histo": 1200, "standard": 9600}
HORO = "E260618120000"


def _cks(body):
    return chr((sum(body.encode("ascii")) & 0x3F) + 0x20)


def _break(c):
    return chr((ord(c) - 0x20 + 1) % 0x40 + 0x20)


def g_histo(label, data, bad=False, s2=False):
    body = label + SP + data
    c = _cks(body + SP) if s2 else _cks(body)   # S2 = checksum incluant le SP avant checksum ; S1 sinon
    return LF + body + SP + (_break(c) if bad else c) + CR


def g_std(label, data, horodate=None, bad=False):
    body = label + HT + (horodate + HT if horodate is not None else "") + data + HT
    c = _cks(body)
    return LF + body + (_break(c) if bad else c) + CR


def frame_std_sc(f, i, bad):
    sinsts = (f["sinsts"] + (i % 10) * 10) if f.get("sinsts") else 0   # varie un peu → courbe
    g = [
        g_std("ADSC", "012345678901"), g_std("VTIC", "02"), g_std("DATE", "", horodate=HORO),
        g_std("NGTF", "BASE"), g_std("LTARF", "BASE"),
        g_std("EAST", f"{f['easf01']:09d}"), g_std("EASF01", f"{f['easf01']:09d}"),
        g_std("IRMS1", f"{f['irms1']:03d}"), g_std("URMS1", "230"),
        g_std("PREF", "06"), g_std("PCOUP", "06"),
        g_std("SINSTS", f"{sinsts:05d}", bad=bad),
    ]
    if f.get("sinsti"):
        g.append(g_std("SINSTI", f"{f['sinsti']:05d}"))
    g += [
        g_std("STGE", "003A0001"), g_std("NTARF", f"{f['ntarf']:02d}"),
        g_std("NJOURF", "00"), g_std("NJOURF+1", f"{f['njourf1']:02d}"),
    ]
    return STX + "".join(g) + ETX


def frame_histo_sc(f, i, vary, bad):
    s2 = (f.get("cks") == "S2")

    def gh(label, data, corrupt=False):
        return g_histo(label, data, corrupt, s2)

    iinst = (f["iinst"] + (i % 15)) if vary else f["iinst"]
    papp = (f["papp"] + (i % 10) * 20) if f["papp"] else 0   # PAPP=0 (production/injection) → reste 0
    g = [
        gh("ADCO", "031234567890"), gh("OPTARIF", "BASE"), gh("ISOUSC", "30"),
        gh("BASE", f"{f['base']:09d}"), gh("PTEC", f["ptec"]),
        gh("IINST", f"{iinst:03d}"), gh("IMAX", "090"),
        gh("PAPP", f"{papp:05d}", bad),
    ]
    if f.get("demain"):
        g.append(gh("DEMAIN", f["demain"]))
    g.append(gh("HHPHC", "A"))
    return STX + "".join(g) + ETX


def build_frame(sc, i, bad):
    if sc["mode"] == "standard":
        return frame_std_sc(sc["fields"], i, bad)
    return frame_histo_sc(sc["fields"], i, sc.get("vary_iinst"), bad)


def open_serial(port, baud):
    return serial.Serial(port, baudrate=baud, bytesize=serial.SEVENBITS,
                         parity=serial.PARITY_EVEN, stopbits=serial.STOPBITS_ONE, timeout=1)


def main():
    args = sys.argv[1:]
    loop = "--loop" in args
    only = next((a.split("=", 1)[1] for a in args if a.startswith("--only=")), None)
    name = next((a.split("=", 1)[1] for a in args if a.startswith("--name=")), None)
    prefix = next((a.split("=", 1)[1] for a in args if a.startswith("--prefix=")), None)
    gap = float(next((a.split("=", 1)[1] for a in args if a.startswith("--gap=")), "0"))
    replay = next((a.split("=", 1)[1] for a in args if a.startswith("--replay=")), None)
    port = next((a for a in args if not a.startswith("--")), "/dev/ttyAMA0")

    if replay:  # rejoue VERBATIM une trame réelle capturée (banc_frames) — la vérité terrain
        from banc_frames import frame_bytes, frame_mode, REAL_FRAMES
        if replay not in REAL_FRAMES:
            sys.exit("trame inconnue : %s (dispo: %s)" % (replay, list(REAL_FRAMES)))
        fb = frame_bytes(replay)
        baud = BAUD[frame_mode(replay)]
        g = gap if gap else 1.3   # cadence Linky réaliste
        ser = open_serial(port, baud)
        print("[banc-inject] REPLAY VERBATIM '%s' (%s %d bd, %d o, gap %.1fs) — boucle" %
              (replay, frame_mode(replay), baud, len(fb), g), flush=True)
        try:
            while True:
                ser.write(fb)
                ser.flush()
                time.sleep(g)
        except KeyboardInterrupt:
            pass
        finally:
            ser.close()
            print("[banc-inject] fin replay.", flush=True)
        return

    scenarios = [s for s in SCENARIOS
                 if (only is None or s["mode"] == only)
                 and (name is None or s["name"] == name)
                 and (prefix is None or s["name"].startswith(prefix))]
    if only is not None or name is not None or prefix is not None:
        loop = True   # filtré (ex: histo seul, 1 scénario, ou un préfixe) → boucle
    ser = None
    cur_baud = None
    print(f"[banc-inject] {len(scenarios)} scénarios sur {port} — loop={loop} only={only} name={name}", flush=True)
    try:
        while True:
            for sc in scenarios:
                baud = BAUD[sc["mode"]]
                if baud != cur_baud:
                    if ser:
                        ser.close()
                    ser = open_serial(port, baud)
                    cur_baud = baud
                    time.sleep(0.5)
                try:
                    with open(CURRENT_FILE, "w") as cf:
                        cf.write(sc["name"])
                except Exception:
                    pass
                print(f"[banc-inject] >>> {sc['name']} ({sc['mode']} {baud} baud) {sc['duration']}s", flush=True)
                t_end = time.time() + sc["duration"]
                i = 0
                while time.time() < t_end:
                    bad = sc.get("corrupt") and (i % 5 == 0)
                    ser.write(build_frame(sc, i, bad).encode("ascii"))
                    ser.flush()
                    i += 1
                    if gap:
                        time.sleep(gap)   # gap inter-trame réaliste (un vrai Linky ne colle pas les trames)
            if not loop:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if ser:
            ser.close()
        print("[banc-inject] fin.", flush=True)


if __name__ == "__main__":
    main()
