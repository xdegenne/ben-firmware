// tic-debug — instrumente la DISCOVERY de l'émetteur BEN (comprendre le "rouge rouge" histo).
// Reproduit les 2 probes de discoverMode() sur le VRAI signal : STANDARD (9600 7E1) puis HISTO
// (1200 7E1), et dit ce que discoverMode déciderait. Dump à 1200 (FTDI TX débranché, RX à 1200 8N1).
//   - probe standard voit "SINSTS"/"ADSC" sur le signal histo -> FAUX POSITIF -> locke standard -> bug,
//   - probe histo voit "PTEC"/"ADCO" -> devrait locker histo.
#include <avr/wdt.h>
#include <EEPROM.h>

static char     buf[300];
static uint16_t blen;

static bool contains(const char *needle) {
  uint8_t nl = 0; while (needle[nl]) nl++;
  for (uint16_t i = 0; i + nl <= blen; i++) {
    bool m = true;
    for (uint8_t j = 0; j < nl; j++) if (buf[i + j] != needle[j]) { m = false; break; }
    if (m) return true;
  }
  return false;
}
static uint8_t countSTX() { uint8_t n = 0; for (uint16_t i = 0; i < blen; i++) if (buf[i] == 0x02) n++; return n; }

static void readFrame(long baud) {                 // lit STX..ETX au débit demandé (7E1, masque 0x7F)
  Serial.begin(baud, SERIAL_7E1);
  blen = 0;
  char c = 0;
  unsigned long t0 = millis();
  while (c != 0x02) { if (Serial.available()) c = Serial.read() & 0x7F; else if (millis() - t0 > 2500) { Serial.end(); return; } }
  buf[blen++] = c; t0 = millis();
  while (blen < sizeof(buf) - 1) {
    if (Serial.available()) { c = Serial.read() & 0x7F; buf[blen++] = c; if (c == 0x03) break; }
    else if (millis() - t0 > 2500) break;
  }
  Serial.end();
}

void setup() { wdt_disable(); }

void loop() {
  uint8_t persisted = EEPROM.read(0x20);           // MODE_ADDR : 1=STANDARD 0=HISTO 0xFF=blank

  readFrame(9600);                                 // PROBE STANDARD (comme discoverMode 1er si persisté=std)
  uint16_t sLen = blen; uint8_t sStx = countSTX();
  bool sSINSTS = contains("SINSTS"), sADSC = contains("ADSC");

  readFrame(1200);                                 // PROBE HISTO
  uint16_t hLen = blen; uint8_t hStx = countSTX();
  bool hPTEC = contains("PTEC"), hADCO = contains("ADCO");

  Serial.begin(1200);                              // DUMP
  Serial.println(); Serial.println(F("===== DISCOVERY ====="));
  Serial.print(F("persisted=")); Serial.print(persisted);
  Serial.println(persisted == 1 ? F(" STANDARD") : persisted == 0 ? F(" HISTO") : F(" blank"));
  Serial.print(F("[STD @9600 ] len=")); Serial.print(sLen); Serial.print(F(" STX=")); Serial.print(sStx);
  Serial.print(F(" SINSTS=")); Serial.print(sSINSTS); Serial.print(F(" ADSC=")); Serial.print(sADSC);
  Serial.println(sSINSTS ? F("  -> valide (FAUX POSITIF!)") : F("  -> non"));
  Serial.print(F("[HISTO@1200] len=")); Serial.print(hLen); Serial.print(F(" STX=")); Serial.print(hStx);
  Serial.print(F(" PTEC=")); Serial.print(hPTEC); Serial.print(F(" ADCO=")); Serial.print(hADCO);
  Serial.println(hPTEC ? F("  -> valide") : F("  -> non"));
  Serial.print(F(">>> discoverMode -> "));
  if (persisted == 1 && sSINSTS) Serial.println(F("STANDARD (faux positif = BUG)"));
  else if (hPTEC)                Serial.println(F("HISTO (ok) -> lit ensuite"));
  else if (sSINSTS)             Serial.println(F("STANDARD"));
  else                          Serial.println(F("0xFF -> blinkErr(2) rouge rouge"));
  Serial.flush(); Serial.end();
  delay(200);
}
