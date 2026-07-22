"""secure_link.py — couche de sécurité de lien LoRa (dérivation + direction + anti-rejeu).

S'appuie sur les primitives de `frame_codec` (ChaCha20 / HMAC-8 / nonce IETF, format
d'enveloppe `[ver][cnt_hi:3][cnt_lo:3][corps][MAC:8]`). Ajoute trois choses au schéma
existant, SANS casser le montant déjà déployé :

  1. Dérivation PAR DEVICE   : K_device = HMAC(K_racine, device_id)
     → un EEPROM dumpé ne donne que SA clé, ni la racine ni celles des autres.

  2. Séparation PAR DIRECTION : deux jeux de sous-clés selon le sens.
     - MONTANT  (device→centrale) : labels EXISTANTS "ben-lora-enc"/"ben-lora-mac"
       → compat binaire avec l'émetteur TIC déployé (aucun reflash).
     - DESCENDANT (centrale→device) : labels "…-dn".
     Une trame montante ne se vérifie PAS avec les clés descendantes (labels ≠) → un
     device qui ne détient que sa clé de direction ne peut pas usurper l'autre sens.

  3. Anti-rejeu : compteur 48 bits monotone (réutilise cnt_hi‖cnt_lo du header/nonce).

Le montant reste géré par `frame_codec.encode_*/decode`. Ce module ajoute le DESCENDANT
(commandes actionneur, ex. volets) : `seal_command` / `open_command`.
"""
import hmac
import hashlib

from frame_codec import (
    _seal, _mac, _chacha20, _nonce,
    HEADER_LEN, MAC_LEN, FLAG_ENC, FrameError,
)

# --- Directions ---
UP = "up"   # device → centrale (télémétrie)  — labels historiques (compat déployé)
DN = "dn"   # centrale → device (commandes)   — labels nouveaux

_LABELS = {
    UP: (b"ben-lora-enc",    b"ben-lora-mac"),      # NE PAS changer : compat émetteur déployé
    DN: (b"ben-lora-enc-dn", b"ben-lora-mac-dn"),   # séparation de direction "dans la clé"
}

# --- Types de trame descendante ---
TYPE_CMD = 0x10   # commande actionneur (corps applicatif libre, TLV recommandé)


# --------------------------------------------------------------------------- #
# Dérivation                                                                   #
# --------------------------------------------------------------------------- #
def derive_device_key(root: bytes, device_id) -> bytes:
    """K_device = HMAC-SHA256(K_racine, device_id) → 32 octets.

    Sens unique : connaître K_device ne révèle NI K_racine NI les clés des autres
    devices (même propriété que derive-device-password.sh pour les mots de passe)."""
    if isinstance(device_id, str):
        device_id = device_id.encode("ascii")
    return hmac.new(root, device_id, hashlib.sha256).digest()


def derive_dir_keys(key: bytes, direction: str):
    """(K_enc, K_mac) pour une direction. `key` = clé du device (partagée ou dérivée)."""
    try:
        enc_label, mac_label = _LABELS[direction]
    except KeyError:
        raise FrameError(f"direction inconnue : {direction!r}")
    k_enc = hmac.new(key, enc_label, hashlib.sha256).digest()
    k_mac = hmac.new(key, mac_label, hashlib.sha256).digest()
    return k_enc, k_mac


# --------------------------------------------------------------------------- #
# Descendant : sceau / ouverture d'une commande (chiffrée + MACée)             #
# --------------------------------------------------------------------------- #
def seal_command(key: bytes, counter: int, body: bytes) -> bytes:
    """Scelle une COMMANDE descendante (centrale→device). Corps ChaCha20 + MAC-8, clés
    de direction DN. `counter` = compteur 48 bits monotone (nonce + anti-rejeu)."""
    k_enc, k_mac = derive_dir_keys(key, DN)
    hi = (counter >> 24) & 0xFFFFFF
    lo = counter & 0xFFFFFF
    return _seal(TYPE_CMD, hi, lo, body, k_enc, k_mac, encrypt=True)


def open_command(frame: bytes, key: bytes) -> dict:
    """Vérifie (clés DN) + déchiffre une commande descendante. Lève FrameError si invalide.
    Renvoie {type, counter, body}. NB : l'anti-rejeu se fait AU-DESSUS (ReplayGuard)."""
    if len(frame) < HEADER_LEN + MAC_LEN:
        raise FrameError(f"trame trop courte ({len(frame)} o)")
    k_enc, k_mac = derive_dir_keys(key, DN)
    signed, mac = frame[:-MAC_LEN], frame[-MAC_LEN:]
    if not hmac.compare_digest(_mac(k_mac, signed), mac):
        raise FrameError("MAC invalide (descendant)")

    ver = frame[0]
    ftype = ver & 0x7F
    if ftype != TYPE_CMD:
        raise FrameError(f"type descendant inattendu : 0x{ftype:02x}")
    hi = int.from_bytes(frame[1:4], "little")
    lo = int.from_bytes(frame[4:7], "little")
    counter = (hi << 24) | lo

    body = frame[HEADER_LEN:-MAC_LEN]
    if ver & FLAG_ENC:                              # MAC déjà vérifié (encrypt-then-MAC)
        body = _chacha20(k_enc, _nonce(hi, lo), body)
    return {"type": ftype, "counter": counter, "body": body}


# --------------------------------------------------------------------------- #
# Anti-rejeu (compteur monotone par pair)                                      #
# --------------------------------------------------------------------------- #
class ReplayGuard:
    """Rejette toute commande dont le compteur n'est pas STRICTEMENT croissant pour ce pair.
    Persister `snapshot()` (ex. sur disque) pour survivre à un redémarrage du device."""

    def __init__(self, state: dict | None = None):
        self._last = dict(state or {})

    def check(self, peer, counter: int) -> None:
        last = self._last.get(peer, -1)
        if counter <= last:
            raise FrameError(f"rejeu détecté (pair={peer}) : counter {counter} <= {last}")
        self._last[peer] = counter

    def snapshot(self) -> dict:
        return dict(self._last)
