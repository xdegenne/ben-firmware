# ble-provisioner — POC niveau 3

Agent BLE qui permet au Pi de recevoir une configuration WiFi (SSID + password)
sans contact physique. Le Pi advertise sous son `deviceId` et expose un service
GATT minimal. Un central (app Flutter ou simulateur Python sur Mac, cf.
`poc-ble-central/`) lit les métadonnées + le scan WiFi, écrit la configuration ;
le Pi se connecte au WiFi via `nmcli` et notifie l'état au central. Après succès,
le Pi reboot pour repartir propre.

## Service GATT

Tous les UUIDs préfixés `b3e7e511-...`.

| Caractéristique | UUID            | Flags             | Description |
|---|---|---|---|
| `WIFI_CONFIG`   | `…000000000001` | `write`           | JSON `{"ssid": "...", "password": "..."}` |
| `STATUS`        | `…000000000002` | `read` + `notify` | Texte : `idle`, `configuring`, `connecting`, `connected[:<ip>]`, `failed:<raison>`. En cas de succès, l'**IP locale** du device est suffixée (`connected:192.168.1.74`) pour permettre au central de se connecter directement sur le LAN (mode proto). |
| `WIFI_SCAN`     | `…000000000003` | `read`            | JSON `[{"ssid": "...", "signal": 60, "freq": 2462}, ...]` — réseaux 2.4 GHz visibles, cache rafraîchi toutes les 30s |
| `DEVICE_INFO`   | `…000000000004` | `read`            | JSON `{"deviceId": "...", "model": "...", "hardwareRevision": "...", "softwareVersion": "..."}` lu depuis `/etc/ben-firmware/device.json` |

Le nom d'advertising est le `deviceId` lu dans `/etc/ben-firmware/device.json`
(fallback `ben-poc01` si absent).

## Cycle de vie

Le service est démarré on-demand par `ben-network-check.service` (oneshot au boot
qui ping `1.1.1.1`) :

- **Internet OK** → `check_network` ne fait rien, les services normaux (`ben-tic-reader`,
  …) tournent
- **Internet KO** → `check_network` lance `ben-ble-provisioner.service`
  - Stop `ben-tic-reader` (via `Conflicts=`) pour libérer la LED
  - LED clignote violet ↔ jaune (mode provisioning)
  - Attend une connexion BLE
  - Après réception d'un `WIFI_CONFIG` valide → 3 flashs verts + LED verte fixe pendant 5s → **reboot complet** → retour pipeline normal

Indicateurs LED :

| Phase                                 | LED |
|---|---|
| `check_network` (boot)                | Bleu clignotant ~1.2 Hz |
| `check_network` Internet OK           | 5 flashs verts (0.5s) |
| `check_network` Internet KO           | 5 flashs rouges (0.5s) |
| `ble-provisioner` attente             | Violet ↔ jaune (1.2 Hz) |
| `ble-provisioner` succès              | 3 flashs verts + vert fixe 5s puis reboot |
| `ble-provisioner` échec               | 3 flashs rouges puis retour violet/jaune |

## Dépendances

Pi OS Bookworm/Trixie (image golden BEN 1.0.6+). NetworkManager + BlueZ
fournis par l'OS.

```bash
sudo apt-get install -y python3-dbus python3-gi
sudo python3 -m pip install --break-system-packages -r requirements.txt
```

(`--break-system-packages` requis sur Pi OS Trixie ; on installe en root car
les services tournent en root pour piloter nmcli + GPIO + DBus BlueZ.)

## Installation des services

```bash
# units systemd
sudo cp ../../../config/systemd/ben-ble-provisioner.service /etc/systemd/system/
sudo cp ../../../config/systemd/ben-network-check.service /etc/systemd/system/
sudo systemctl daemon-reload

# enable au boot
sudo systemctl enable ben-network-check.service
# ben-ble-provisioner reste `static` (démarré on-demand)
```

## Sécurité — limitations connues

⚠ **Pairing/chiffrement BLE non actifs** dans ce POC.

Raison : nous avions activé le flag `encrypt-write` (qui impose une connexion
BLE chiffrée Just Works côté BlueZ) — ça fonctionne, mais le central
**Bleak / Core Bluetooth sur macOS** ne déclenche pas le pairing implicite et
renvoie une erreur `Insufficient Encryption`. macOS n'expose pas l'API
d'initialisation de pairing aux apps. On a donc retiré le flag pour le POC.

**Conséquence** : le password WiFi transite **en clair** sur l'air BLE pendant
l'écriture de `WIFI_CONFIG`. Un attaquant à portée (~10m intérieur) pourrait
le capturer avec une sonde BLE.

**Chemin prod** : réactiver `encrypt-write` côté Pi quand l'app Flutter
(`flutter_blue_plus` sur iOS/Android, qui gère le pairing nativement) sera
prête. Le code Pi a déjà tout le nécessaire (`Pairable=True` sur l'adapter,
flag prêt à remettre en 1 ligne dans `main.py`).

## Test rapide depuis le Mac

Voir `poc-ble-central/` dans le workspace `ben/` :

```bash
cd ~/work/ben/poc-ble-central && source .venv/bin/activate
python3 central_sim.py --device ben-0003
```

Le script lit `DEVICE_INFO` + `WIFI_SCAN`, présente la liste des SSID, demande
le password, écrit la config, suit le `STATUS` jusqu'à `connected`/`failed`.

## TODO avant prod

- Pairing BLE chiffrement (cf. section sécurité)
- Caractéristiques supplémentaires : `WIFI_FORGET` (effacer connection), `REBOOT` (déclencher reboot à la demande de l'app)
- Timeout global : si pas de central depuis 30 min, exit (laisse systemd décider)
- Fix warning `bluezero` "deprecated disconnect callback"
- Tests automatisés (mock BlueZ via dbus-mock ?)
- Intégration au pipeline OTA + tag release
