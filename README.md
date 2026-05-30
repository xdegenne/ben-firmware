# BEN Device

Open-source firmware and agent for the BEN Linky data collector.

BEN is a small device based on a Raspberry Pi Zero W that reads electricity consumption data from a French Linky smart meter (via the TIC interface) and publishes it securely to a cloud API.

Two models: `pi0-wired` (Pi Zero only, direct TIC) and `pi0-lora` (Arduino + Pi Zero, LoRa transport).

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full design documentation.

---

## License

MIT
