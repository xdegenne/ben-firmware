"""test_secure_link.py — valide la couche secure-link HORS radio (zéro risque ben-0001).

Lancer : python3 test_secure_link.py   (aucune dépendance ; frame_codec est pur-Python)
Prouve : round-trip, séparation de direction, isolation par-device, anti-rejeu, altération.
"""
import secure_link as sl
import frame_codec as fc

ROOT = bytes(range(32))                 # K_racine factice (déterministe)
DEV_A = "volet-salon-0x2a"
DEV_B = "volet-cuisine-0x2b"
K_A = sl.derive_device_key(ROOT, DEV_A)
K_B = sl.derive_device_key(ROOT, DEV_B)

_ok = 0

def check(cond, label):
    global _ok
    assert cond, f"ÉCHEC : {label}"
    _ok += 1
    print(f"  ✓ {label}")


def expect_fail(fn, label):
    """Le round-trip DOIT lever FrameError."""
    try:
        fn()
    except fc.FrameError:
        global _ok; _ok += 1
        print(f"  ✓ {label}")
    else:
        raise AssertionError(f"ÉCHEC (aurait dû lever) : {label}")


print("== 1. Dérivation par-device ==")
check(len(K_A) == 32 and len(K_B) == 32, "K_device = 32 octets")
check(K_A != K_B, "deux devices → clés distinctes")
check(sl.derive_device_key(ROOT, DEV_A) == K_A, "dérivation déterministe")
check(sl.derive_device_key(bytes(32), DEV_A) != K_A, "racine différente → clé différente")

print("== 2. Round-trip commande descendante (chiffrée + MACée) ==")
body = bytes([fc.T_ADCO]) + b"OPEN"          # corps applicatif factice
frame = sl.seal_command(K_A, counter=42, body=body)
opened = sl.open_command(frame, K_A)
check(opened["body"] == body, "corps restitué à l'identique")
check(opened["counter"] == 42, "compteur restitué")
check(opened["type"] == sl.TYPE_CMD, "type = TYPE_CMD")
check(frame[0] & fc.FLAG_ENC, "bit chiffré posé")
check(body not in frame, "le corps n'apparaît PAS en clair dans la trame")

print("== 3. Isolation par-device ==")
expect_fail(lambda: sl.open_command(frame, K_B),
            "commande scellée pour A → REJETÉE avec la clé de B (MAC)")

print("== 4. Séparation de direction ==")
# une commande DESCENDANTE ne doit pas se vérifier comme une trame MONTANTE
expect_fail(lambda: fc.decode(frame, K_A),
            "commande descendante → REJETÉE par le décodeur montant (labels ≠)")
# et une trame MONTANTE (boot) ne doit pas s'ouvrir comme une commande descendante
up_boot = fc.encode_boot(K_A, boot_count=1, msg_count=1,
                         tlvs=[(fc.T_ADCO, b"012345678901")], encrypt=True)
expect_fail(lambda: sl.open_command(up_boot, K_A),
            "trame montante → REJETÉE par open_command (labels ≠)")

print("== 5. Détection d'altération ==")
tampered = bytearray(frame)
tampered[fc.HEADER_LEN] ^= 0x01              # flip 1 bit du corps chiffré
expect_fail(lambda: sl.open_command(bytes(tampered), K_A),
            "1 bit du corps flippé → MAC invalide")
tampered2 = bytearray(frame)
tampered2[-1] ^= 0x01                         # flip le MAC
expect_fail(lambda: sl.open_command(bytes(tampered2), K_A),
            "1 bit du MAC flippé → MAC invalide")

print("== 6. Anti-rejeu (ReplayGuard) ==")
guard = sl.ReplayGuard()
guard.check(DEV_A, 42)                        # 1er passage OK
check(True, "counter 42 accepté (1er)")
expect_fail(lambda: guard.check(DEV_A, 42), "counter 42 rejoué → REJETÉ")
expect_fail(lambda: guard.check(DEV_A, 10), "counter plus ancien → REJETÉ")
guard.check(DEV_A, 43)                        # strictement croissant OK
check(True, "counter 43 accepté (croissant)")
guard.check(DEV_B, 1)                         # autre pair, compteur indépendant
check(True, "pair B indépendant (counter 1 accepté)")
check(guard.snapshot() == {DEV_A: 43, DEV_B: 1}, "snapshot persistable correct")

print(f"\n✅ {_ok} assertions passées.")
