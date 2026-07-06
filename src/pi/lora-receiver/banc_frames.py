"""banc_frames — trames TIC RÉELLES capturées sur de vrais compteurs, rejouées VERBATIM par le banc.

Capturées via pump_frame.py (lecture 8N1 + masque 0x7F d'une trame STX..ETX). `b64` = les octets
7 bits exacts (STX inclus, ETX inclus, checksums RÉELS du compteur). Le banc les renvoie tels quels
au débit du mode (l'UART 7E1 rajoute la parité). C'est la SEULE façon de tester des cas réels
(contrats HC/EJP/Tempo, PAPP=0/injection, checksums-espace…) sans réinventer des trames jouet.

Pour en ajouter une : lancer pump_frame.py sur le compteur, coller le FRAME_B64 ici.
"""
import base64

REAL_FRAMES = {
    # ben-0003 — Linky HISTORIQUE, contrat HC (Heures Creuses), EN PRODUCTION :
    #   PAPP=00000 + IINST=010 = INJECTION (cas solaire). PTEC=HP.. (checksum = 0x20 ESPACE, cas limite).
    #   ADCO 021861862663. C'est LA trame sur laquelle l'émetteur LoRa v0.5.1 fait rouge rouge.
    "ben0003_histo_hc_prod": {
        "mode": "histo",
        "b64": ("AgpBRENPIDAyMTg2MTg2MjY2MyBIDQpPUFRBUklGIEhDLi4gPA0KSVNPVVNDIDQ1ID8NCkhDSEMgMDMw"
                "MjA1NDc0IF8NCkhDSFAgMDM1NjQxNzA1IDINClBURUMgSFAuLiAgDQpJSU5TVCAwMTAgWA0KSU1BWCAw"
                "OTAgSA0KUEFQUCAwMDAwMCAhDQpISFBIQyBBICwNCk1PVERFVEFUIDAwMDAwMCBCDQM="),
    },
}


def frame_bytes(name):
    return base64.b64decode(REAL_FRAMES[name]["b64"])


def frame_mode(name):
    return REAL_FRAMES[name]["mode"]
