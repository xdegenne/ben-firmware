"""banc_scenarios — SOURCE DE VÉRITÉ du banc de test BEN.

Chaque scénario = valeurs TIC à injecter (`fields`) + assertions sur le batch LoRa décodé (`expect`).
Importé par tic-gen (ben-ops, construit + injecte les trames) ET banc_listen (ben-0001, décode + assert).
Déployé sur les DEUX machines. Aucune dépendance (data pure).

Principe : chaque scénario a des valeurs DISTINGUABLES → le listener retrouve le scénario d'un batch
reçu par sa signature, sans synchro inter-machine. Les BASCULES (tarif, PTEC, mode) sont testées par la
TRANSITION entre deux scénarios consécutifs (l'Arduino flushe + repart sur un nouvel index/mode).

expect :
  src_standard : True (std) / False (histo)
  index_id     : NTARF (std) ou rang PTEC (histo)
  index_value  : index actif attendu (Wh)
  njourf1      : NJOURF+1 attendu (std) — None = pas vérifié
  papp_sign    : "+" (soutiré) / "-" (injection)
  has_iinst    : True → 2e courbe IINST présente (histo)
  iinst_val    : valeur IINST attendue (histo, ±2 de tolérance)
"""

# duration = secondes d'injection (≥ ~90 s pour laisser l'Arduino produire 1-2 batchs propres).
SCENARIOS = [
    # ---------------- STANDARD ----------------
    {
        "name": "std_base",
        "mode": "standard", "duration": 100,
        "fields": {"ntarf": 1, "easf01": 1000000, "njourf1": 0, "sinsts": 500, "sinsti": 0, "irms1": 5},
        "expect": {"src_standard": True, "index_id": 1, "index_value": 1000000, "njourf1": 0, "papp_sign": "+"},
    },
    {   # TRANSITION std_base(NTARF=1) -> ici(NTARF=2) = test BASCULE TARIF (flush + nouvel index)
        "name": "std_ntarf2_switch",
        "mode": "standard", "duration": 100,
        "fields": {"ntarf": 2, "easf01": 2000000, "njourf1": 0, "sinsts": 700, "sinsti": 0, "irms1": 7},
        "expect": {"src_standard": True, "index_id": 2, "index_value": 2000000, "njourf1": 0, "papp_sign": "+"},
    },
    {
        "name": "std_njourf_nonzero",
        "mode": "standard", "duration": 100,
        "fields": {"ntarf": 1, "easf01": 1000000, "njourf1": 3, "sinsts": 550, "sinsti": 0, "irms1": 5},
        "expect": {"src_standard": True, "index_id": 1, "njourf1": 3, "papp_sign": "+"},
    },
    {   # injection : SINSTS=0 + SINSTI>0 -> papp_net NÉGATIF
        "name": "std_injection",
        "mode": "standard", "duration": 100,
        "fields": {"ntarf": 1, "easf01": 1000000, "njourf1": 0, "sinsts": 0, "sinsti": 800, "irms1": 4},
        "expect": {"src_standard": True, "index_id": 1, "papp_sign": "-"},
    },
    {   # corrupt : 1 trame /5 avec checksum faux -> l'Arduino saute, pas de crash, batch continue
        "name": "std_corrupt",
        "mode": "standard", "duration": 100, "corrupt": True,
        "fields": {"ntarf": 1, "easf01": 1000000, "njourf1": 0, "sinsts": 600, "sinsti": 0, "irms1": 6},
        "expect": {"src_standard": True, "index_id": 1, "papp_sign": "+"},
    },
    # ---------------- HISTO (nécessite un rig histo / reboot Arduino en histo) ----------------
    {
        "name": "histo_base",
        "mode": "histo", "duration": 100,
        "fields": {"ptec": "TH..", "index_id": 0, "base": 1500000, "papp": 400, "iinst": 10, "demain": None},
        "expect": {"src_standard": False, "index_id": 0, "index_value": 1500000,
                   "papp_sign": "+", "has_iinst": True, "iinst_val": 10},
    },
    {   # IINST qui VARIE -> vérifie que la 2e courbe suit (pas figée)
        "name": "histo_iinst_var",
        "mode": "histo", "duration": 100, "vary_iinst": True,
        "fields": {"ptec": "TH..", "index_id": 0, "base": 1500000, "papp": 900, "iinst": 40, "demain": None},
        "expect": {"src_standard": False, "has_iinst": True, "iinst_val": 40},
    },
    {   # DEMAIN ROUGE (Tempo histo) -> TLV 0x20 = 2
        "name": "histo_demain_rouge",
        "mode": "histo", "duration": 90,
        "fields": {"ptec": "TH..", "index_id": 0, "base": 1500000, "papp": 400, "iinst": 10, "demain": "ROUG"},
        "expect": {"src_standard": False, "has_iinst": True, "demain": 2},
    },
    {   # BASCULE : DEMAIN BLEU (0) -> teste le changement de couleur au batch suivant
        "name": "histo_demain_bleu",
        "mode": "histo", "duration": 90,
        "fields": {"ptec": "TH..", "index_id": 0, "base": 1500000, "papp": 400, "iinst": 10, "demain": "BLEU"},
        "expect": {"src_standard": False, "has_iinst": True, "demain": 0},
    },
    {   # BASCULE : DEMAIN BLANC (1)
        "name": "histo_demain_blanc",
        "mode": "histo", "duration": 90,
        "fields": {"ptec": "TH..", "index_id": 0, "base": 1500000, "papp": 400, "iinst": 10, "demain": "BLAN"},
        "expect": {"src_standard": False, "has_iinst": True, "demain": 1},
    },
]

# Nom du fichier partagé où le générateur écrit le scénario courant (info/debug ; l'assert ne s'en sert PAS).
CURRENT_FILE = "/tmp/banc_scenario"
