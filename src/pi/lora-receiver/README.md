# `lora-receiver/` — les CODECS, plus le récepteur

⚠️ **Le nom du répertoire est historique.** L'agent qui portait ce nom — le monolithe
`main.py` / `ben-lora-receiver.service` — a été **découpé en 0.9.1** (`ben-radio` +
`ben-telemetry`) puis **supprimé en 0.9.12**. Il ne reste ici que du code **partagé et
bien vivant**, importé en production par les deux services :

| Fichier | Rôle | Importé par |
|---|---|---|
| `frame_codec.py` | trames TLV : `open_frame` (MAC + déchiffrement), `parse`, `interpret_tlvs` | `ben-radio`, `ben-telemetry` |
| `curve_codec.py` | courbes v0x04/v0x05 (horodatage par point, carry-forward) | `ben-telemetry` |
| `secure_link.py` | dérivation de clés, ChaCha20-Poly1305 du lien montant | `ben-radio` |
| `banc_*.py`, `run-banc.sh` | bancs de diagnostic radio (à lancer `ben-radio` arrêté) | — |
| `test_*.py` | bancs de non-régression des codecs | — |

Les deux services y accèdent par un `sys.path.insert` (`ben_radio.py`,
`ben_telemetry.py`) : **déplacer ou renommer ce répertoire casse la façade radio sur
tout le parc.** Le renommage est un chantier à part, pas un coup de balai.

Le vrai lecteur LoRa est `src/pi/ben-telemetry/ben_telemetry.py` ; le seul maître du
SX127x est `src/pi/ben-radio/ben_radio.py`.
