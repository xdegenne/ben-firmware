/*
  tic-reader — BEN Arduino emitter
  =================================
  Lit la TIC du compteur Linky et transmet les données en LoRa
  vers le Pi Zero récepteur (ben-device).

  Protocole binaire v0x02 — 20 octets :
    0       version       uint8  = 0x02
    1       flags         bit0-1 DEMAIN, bit2 ADPS, bit3 PEJP
    2-3     boot_seq      uint16 LE — RAM, reset au boot, +1/trame
    4       index_id      uint8  BASE=0x00 HCHC=0x01 HCHP=0x02 EJP/BBR=0x03..0x0A
    5-8     index_value   uint32 LE — Wh
    9       IINST         uint8  — A, saturé à 255
    10-11   PAPP          uint16 LE — VA
    12-19   HMAC          HMAC-SHA256(key, octets 0..11) tronqué 8 octets

  Clé HMAC : 32 octets stockés en EEPROM (adresses 0x00–0x1F).
  Mode provisioning : si l'EEPROM est vierge (0xFF), l'Arduino attend
  la clé sur Serial 9600 baud. Voir flash-arduino.sh dans ben-ops.

  Hardware — Arduino Pro Mini 5V/16MHz + RFM95 :
    RFM95 NSS  → D10    TIC out  → D4
    RFM95 SCK  → D13    RGB Vert → D3  (100Ω)
    RFM95 MOSI → D11    RGB Rouge→ D5  (150Ω)
    RFM95 MISO → D12    RGB Bleu → D6  (100Ω)
    RFM95 DIO0 → D2     cathode  → GND
    RFM95 RST  → D9
    RFM95 VCC  → RAW (rail supercap)
*/

#include <SPI.h>
#include <RH_RF95.h>
#include <RHReliableDatagram.h>
#include "LowPower.h"
#include <Crypto.h>
#include <SHA256.h>
#include <EEPROM.h>
#include <avr/wdt.h>
#include <string.h>

// ---------------------------------------------------------------------------
// Pins
// ---------------------------------------------------------------------------
#define RFM95_CS   10
#define RFM95_INT   2
#define RFM95_RST   9
#define TIC_OUT     4
#define RGB_G       3
#define RGB_R       5
#define RGB_B       6

// ---------------------------------------------------------------------------
// LoRa
// ---------------------------------------------------------------------------
#define CLIENT_ADDRESS  31
#define SERVER_ADDRESS  32
#define RF95_FREQ      868.0
#define RF95_TXPOWER    20

// ---------------------------------------------------------------------------
// Protocole
// ---------------------------------------------------------------------------
#define PROTOCOL_VERSION 0x02
#define PAYLOAD_LEN      20
#define HMAC_LEN          8
#define HMAC_OFFSET      12

// ---------------------------------------------------------------------------
// Index IDs
// ---------------------------------------------------------------------------
#define IDX_BASE    0x00
#define IDX_HCHC    0x01
#define IDX_HCHP    0x02
#define IDX_EJPHN   0x03
#define IDX_EJPHPM  0x04
#define IDX_BBRHCJB 0x05
#define IDX_BBRHPJB 0x06
#define IDX_BBRHCJW 0x07
#define IDX_BBRHPJW 0x08
#define IDX_BBRHCJR 0x09
#define IDX_BBRHPJR 0x0A
#define IDX_UNKNOWN 0xFF

// ---------------------------------------------------------------------------
// Flags
// ---------------------------------------------------------------------------
#define FLAG_DEMAIN_BLEU 0x00
#define FLAG_DEMAIN_BLAN 0x01
#define FLAG_DEMAIN_ROUG 0x02
#define FLAG_DEMAIN_NA   0x03
#define FLAG_ADPS        0x04
#define FLAG_PEJP        0x08

// ---------------------------------------------------------------------------
// EEPROM
// ---------------------------------------------------------------------------
#define HMAC_KEY_ADDR 0x00
#define HMAC_KEY_LEN  32

// ---------------------------------------------------------------------------
// TIC
// ---------------------------------------------------------------------------
const unsigned long TIC_TIMEOUT_MS = 6000;
const float VCC_MIN = 2.5;

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------
RH_RF95 driver(RFM95_CS, RFM95_INT);
RHReliableDatagram manager(driver, CLIENT_ADDRESS);
SHA256 sha256;
static bool loraOk = false;
static uint16_t boot_seq = 0;
static bool firstFrame = true;
static uint8_t hmac_key[HMAC_KEY_LEN];

// ---------------------------------------------------------------------------
// TIC values
// ---------------------------------------------------------------------------
// ----- fields_seen bitmask — set apres chaque ligne TIC checksum OK -----
#define TIC_SEEN_BASE    (1u << 0)
#define TIC_SEEN_HCHC    (1u << 1)
#define TIC_SEEN_HCHP    (1u << 2)
#define TIC_SEEN_EJPHN   (1u << 3)
#define TIC_SEEN_EJPHPM  (1u << 4)
#define TIC_SEEN_BBR0    (1u << 5)
#define TIC_SEEN_BBR1    (1u << 6)
#define TIC_SEEN_BBR2    (1u << 7)
#define TIC_SEEN_BBR3    (1u << 8)
#define TIC_SEEN_BBR4    (1u << 9)
#define TIC_SEEN_BBR5    (1u << 10)
#define TIC_SEEN_IINST   (1u << 11)
#define TIC_SEEN_PAPP    (1u << 12)

// index_id (0x00-0x0A) -> bit TIC_SEEN correspondant
static const uint16_t INDEX_ID_TO_SEEN[] = {
  TIC_SEEN_BASE, TIC_SEEN_HCHC, TIC_SEEN_HCHP, TIC_SEEN_EJPHN, TIC_SEEN_EJPHPM,
  TIC_SEEN_BBR0, TIC_SEEN_BBR1, TIC_SEEN_BBR2, TIC_SEEN_BBR3, TIC_SEEN_BBR4, TIC_SEEN_BBR5
};

struct TICValues {
  bool valid;
  char adco[13];
  char optarif[5];
  char ptec[5];
  uint32_t base, hchc, hchp, ejphn, ejphpm;
  uint32_t bbr[6];
  char demain[5];
  bool adps_present;
  bool pejp_present;
  uint16_t iinst;
  uint16_t papp;
  uint16_t fields_seen;
};

// ---------------------------------------------------------------------------
// LED RGB
// ---------------------------------------------------------------------------
static void setRGB(uint8_t r, uint8_t g, uint8_t b) {
  analogWrite(RGB_R, r);
  analogWrite(RGB_G, g);
  analogWrite(RGB_B, b);
}

static inline void ledOff() { setRGB(0, 0, 0); }

static void blinkRGB(uint8_t r, uint8_t g, uint8_t b, uint16_t ms = 80) {
  setRGB(r, g, b); delay(ms); ledOff();
}

// 1=Vcc bas  2=TIC KO  3=LoRa KO  4=PTEC inconnu
static void blinkErr(uint8_t code) {
  for (uint8_t i = 0; i < code; i++) {
    setRGB(255, 0, 0); delay(60);
    ledOff();          delay(120);
  }
  delay(400);
}

static void ledBootHello() {
  setRGB(255, 255, 255); delay(1000); ledOff(); delay(300);
}

static void ledLoraOK() {
  for (uint8_t i = 0; i < 3; i++) {
    setRGB(0, 255, 0); delay(2000); ledOff(); delay(300);
  }
}

static void ledLoraKO() {
  for (uint8_t i = 0; i < 3; i++) {
    setRGB(255, 0, 0); delay(2000); ledOff(); delay(300);
  }
}

// ---------------------------------------------------------------------------
// EEPROM
// ---------------------------------------------------------------------------
static bool isKeyBlank(const uint8_t *key) {
  for (uint8_t i = 0; i < HMAC_KEY_LEN; i++) {
    if (key[i] != 0xFF && key[i] != 0x00) return false;
  }
  return true;
}

static void readKeyFromEEPROM(uint8_t *key) {
  for (uint8_t i = 0; i < HMAC_KEY_LEN; i++) {
    key[i] = EEPROM.read(HMAC_KEY_ADDR + i);
  }
}

static void writeKeyToEEPROM(const uint8_t *key) {
  for (uint8_t i = 0; i < HMAC_KEY_LEN; i++) {
    EEPROM.update(HMAC_KEY_ADDR + i, key[i]);
  }
}

static bool hexCharToByte(char hi, char lo, uint8_t &out) {
  auto hexVal = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  int h = hexVal(hi), l = hexVal(lo);
  if (h < 0 || l < 0) return false;
  out = (uint8_t)((h << 4) | l);
  return true;
}

// ---------------------------------------------------------------------------
// Mode provisioning
// ---------------------------------------------------------------------------
// Déclenché si l'EEPROM est vierge.
// Attend sur Serial 9600 la commande : "BEN_KEY:<64 hex chars>\n"
// Répond "OK\n" si succès, "ERR\n" si format invalide.
// Puis reboot.
//
// Appelé depuis le script ben-ops/scripts/flash-arduino.sh.
// ---------------------------------------------------------------------------
static void provisioningMode() {
  Serial.begin(9600);
  Serial.println(F("PROVISIONING — attente BEN_KEY"));
  Serial.flush();

  // LED rouge clignotante = mode provisioning
  while (true) {
    setRGB(255, 0, 0); delay(200);
    ledOff();          delay(200);

    if (Serial.available()) {
      String line = Serial.readStringUntil('\n');
      line.trim();

      // Attend "BEN_KEY:<64 hex>"
      if (!line.startsWith(F("BEN_KEY:"))) {
        Serial.println(F("ERR"));
        Serial.flush();
        continue;
      }

      String hexStr = line.substring(8);
      if (hexStr.length() != 64) {
        Serial.println(F("ERR"));
        Serial.flush();
        continue;
      }

      uint8_t key[HMAC_KEY_LEN];
      bool valid = true;
      for (uint8_t i = 0; i < HMAC_KEY_LEN; i++) {
        if (!hexCharToByte(hexStr[i * 2], hexStr[i * 2 + 1], key[i])) {
          valid = false;
          break;
        }
      }

      if (!valid) {
        Serial.println(F("ERR"));
        Serial.flush();
        continue;
      }

      writeKeyToEEPROM(key);
      Serial.println(F("OK"));
      Serial.flush();

      // Reboot via watchdog
      wdt_enable(WDTO_15MS);
      while (true) {}
    }
  }
}

// ---------------------------------------------------------------------------
// TIC
// ---------------------------------------------------------------------------
bool verifyTICChecksum(const char *line, size_t len) {
  if (len < 4) return false;
  int lastSpace = -1;
  for (int i = (int)len - 2; i >= 0; i--) {
    if (line[i] == ' ') { lastSpace = i; break; }
  }
  if (lastSpace < 1) return false;
  uint8_t sum = 0;
  for (int i = 0; i < lastSpace; i++) sum += (uint8_t)line[i];
  return line[len - 1] == (char)((sum & 0x3F) + 0x20);
}

String readTIC() {
  digitalWrite(TIC_OUT, HIGH);
  Serial.begin(1200, SERIAL_7E1);

  const char STX = 0x02, ETX = 0x03, LF = 0x0A, CR = 0x0D;

  unsigned long t0 = millis();
  char c = 0;
  String frame = "", line = "";
  bool inLine = false;
  uint8_t kept = 0, dropped = 0;

  while (c != STX) {
    wdt_reset();
    if (Serial.available()) {
      c = Serial.read();
    } else if (millis() - t0 > TIC_TIMEOUT_MS) {
      Serial.flush(); Serial.end();
      digitalWrite(TIC_OUT, LOW);
      return "";
    }
  }

  while (c != ETX) {
    wdt_reset();
    if (Serial.available()) {
      c = Serial.read();
      if (c == LF) { line = ""; inLine = true; }
      else if (c == CR) {
        if (inLine && line.length() > 0) {
          if (verifyTICChecksum(line.c_str(), line.length())) {
            frame += line; frame += '\n'; kept++;
          } else { dropped++; }
        }
        inLine = false;
      } else if (inLine) { line += c; }
    } else if (millis() - t0 > TIC_TIMEOUT_MS) { break; }
  }

  Serial.flush(); Serial.end();
  digitalWrite(TIC_OUT, LOW);

  Serial.begin(9600);
  Serial.print(F("TIC ok=")); Serial.print(kept);
  Serial.print(F(" drop=")); Serial.println(dropped);
  Serial.flush(); Serial.end();

  return frame;
}

static void copyField(char *dst, size_t sz, const String& s) {
  size_t n = s.length();
  if (n >= sz) n = sz - 1;
  memcpy(dst, s.c_str(), n);
  dst[n] = 0;
}

TICValues parseTICFrame(const String& frame) {
  TICValues v;
  memset(&v, 0, sizeof(v));
  String rem = frame;
  int pos = rem.indexOf('\n');
  while (pos > -1) {
    String line = rem.substring(0, pos);
    rem = rem.substring(pos + 1);
    pos = rem.indexOf('\n');
    int fs = line.indexOf(' '), ls = line.lastIndexOf(' ');
    if (fs < 1 || ls <= fs) continue;
    String name = line.substring(0, fs);
    String val  = line.substring(fs + 1, ls);
    if      (name == "ADCO")    copyField(v.adco,    sizeof(v.adco),    val);
    else if (name == "OPTARIF") copyField(v.optarif, sizeof(v.optarif), val);
    else if (name == "PTEC")    copyField(v.ptec,    sizeof(v.ptec),    val);
    else if (name == "DEMAIN")  copyField(v.demain,  sizeof(v.demain),  val);
    else if (name == "BASE")    { v.base   = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_BASE;   }
    else if (name == "HCHC")    { v.hchc   = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_HCHC;   }
    else if (name == "HCHP")    { v.hchp   = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_HCHP;   }
    else if (name == "EJPHN")   { v.ejphn  = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_EJPHN;  }
    else if (name == "EJPHPM")  { v.ejphpm = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_EJPHPM; }
    else if (name == "BBRHCJB") { v.bbr[0] = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_BBR0;   }
    else if (name == "BBRHPJB") { v.bbr[1] = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_BBR1;   }
    else if (name == "BBRHCJW") { v.bbr[2] = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_BBR2;   }
    else if (name == "BBRHPJW") { v.bbr[3] = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_BBR3;   }
    else if (name == "BBRHCJR") { v.bbr[4] = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_BBR4;   }
    else if (name == "BBRHPJR") { v.bbr[5] = (uint32_t)val.toInt(); v.fields_seen |= TIC_SEEN_BBR5;   }
    else if (name == "IINST")   { v.iinst  = (uint16_t)val.toInt(); v.fields_seen |= TIC_SEEN_IINST;  }
    else if (name == "PAPP")    { v.papp   = (uint16_t)val.toInt(); v.fields_seen |= TIC_SEEN_PAPP;   }
    else if (name == "ADPS")    v.adps_present = true;
    else if (name == "PEJP")    v.pejp_present = true;
  }
  v.valid = (v.ptec[0] != 0);
  return v;
}

void selectActiveIndex(const TICValues& v, uint8_t& id, uint32_t& value) {
  if      (strncmp(v.ptec, "TH",   2) == 0) { id = IDX_BASE;    value = v.base;   }
  else if (strncmp(v.ptec, "HC..", 4) == 0) { id = IDX_HCHC;    value = v.hchc;   }
  else if (strncmp(v.ptec, "HP..", 4) == 0) { id = IDX_HCHP;    value = v.hchp;   }
  else if (strncmp(v.ptec, "HN",   2) == 0) { id = IDX_EJPHN;   value = v.ejphn;  }
  else if (strncmp(v.ptec, "PM",   2) == 0) { id = IDX_EJPHPM;  value = v.ejphpm; }
  else if (strncmp(v.ptec, "HCJB", 4) == 0) { id = IDX_BBRHCJB; value = v.bbr[0]; }
  else if (strncmp(v.ptec, "HPJB", 4) == 0) { id = IDX_BBRHPJB; value = v.bbr[1]; }
  else if (strncmp(v.ptec, "HCJW", 4) == 0) { id = IDX_BBRHCJW; value = v.bbr[2]; }
  else if (strncmp(v.ptec, "HPJW", 4) == 0) { id = IDX_BBRHPJW; value = v.bbr[3]; }
  else if (strncmp(v.ptec, "HCJR", 4) == 0) { id = IDX_BBRHCJR; value = v.bbr[4]; }
  else if (strncmp(v.ptec, "HPJR", 4) == 0) { id = IDX_BBRHPJR; value = v.bbr[5]; }
  else                                       { id = IDX_UNKNOWN; value = 0; }
}

uint8_t buildFlags(const TICValues& v) {
  uint8_t f;
  if      (strncmp(v.demain, "BLEU", 4) == 0) f = FLAG_DEMAIN_BLEU;
  else if (strncmp(v.demain, "BLAN", 4) == 0) f = FLAG_DEMAIN_BLAN;
  else if (strncmp(v.demain, "ROUG", 4) == 0) f = FLAG_DEMAIN_ROUG;
  else                                         f = FLAG_DEMAIN_NA;
  if (v.adps_present) f |= FLAG_ADPS;
  if (v.pejp_present) f |= FLAG_PEJP;
  return f;
}

// ---------------------------------------------------------------------------
// Tension
// ---------------------------------------------------------------------------
bool isVoltageSufficient() {
  ADMUX = 0x4F; delayMicroseconds(5);
  ADMUX = 0x4E; delayMicroseconds(200);
  ADCSRA |= (1 << ADEN) | (1 << ADSC);
  while (ADCSRA & (1 << ADSC));
  float vcc = (1023.0 * 1.1) / (float)(ADCL | (ADCH << 8));
  Serial.print(F("Vcc: ")); Serial.println(vcc, 2);
  return vcc >= VCC_MIN;
}

// ---------------------------------------------------------------------------
// LoRa
// ---------------------------------------------------------------------------
void buildPayload(uint8_t *buf, const TICValues& v) {
  uint8_t id; uint32_t val;
  selectActiveIndex(v, id, val);
  buf[0] = PROTOCOL_VERSION;
  buf[1] = buildFlags(v);
  memcpy(buf + 2, &boot_seq, 2);
  buf[4] = id;
  memcpy(buf + 5, &val, 4);
  buf[9] = (v.iinst > 255) ? 255 : (uint8_t)v.iinst;
  memcpy(buf + 10, &v.papp, 2);

  uint8_t mac[32];
  sha256.resetHMAC(hmac_key, HMAC_KEY_LEN);
  sha256.update(buf, HMAC_OFFSET);
  sha256.finalizeHMAC(hmac_key, HMAC_KEY_LEN, mac, 32);
  memcpy(buf + HMAC_OFFSET, mac, HMAC_LEN);
}

// Trame boot (version=0x01) : ADCO en clair dans les octets 1-12.
// Chiffrement PDL : sujet separe, a traiter ulterieurement.
void sendBootFrame(const char* adco) {
  uint8_t buf[PAYLOAD_LEN];
  memset(buf, 0, PAYLOAD_LEN);
  buf[0] = 0x01;
  memcpy(buf + 1, adco, 12);

  if (loraOk) {
    driver.setModeIdle();
    manager.sendtoWait(buf, PAYLOAD_LEN, SERVER_ADDRESS);
    blinkRGB(0, 0, 255);  // bleu = boot frame envoyee
    driver.sleep();
  }
}

void sendBinary(const TICValues& v) {
  uint8_t id; uint32_t val;
  selectActiveIndex(v, id, val);
  if (id == IDX_UNKNOWN) {
    Serial.print(F("PTEC inconnu (")); Serial.print(v.ptec); Serial.println(')');
    blinkErr(4);
    return;
  }

  // Guard : index actif + IINST + PAPP doivent avoir ete vus dans la trame.
  // Si une de ces lignes a eu un checksum KO, on skipe plutot que d'emettre 0.
  uint16_t required = TIC_SEEN_IINST | TIC_SEEN_PAPP;
  if (id < sizeof(INDEX_ID_TO_SEEN) / sizeof(INDEX_ID_TO_SEEN[0]))
    required |= INDEX_ID_TO_SEEN[id];
  if ((v.fields_seen & required) != required) {
    Serial.println(F("TIC partielle (champs manquants), trame ignoree"));
    blinkErr(2);
    return;
  }

  uint8_t buf[PAYLOAD_LEN];
  buildPayload(buf, v);

  Serial.print(F("seq=")); Serial.print(boot_seq);
  Serial.print(F(" ptec=")); Serial.print(v.ptec);
  Serial.print(F(" idx=0x")); Serial.print(id, HEX);
  Serial.print(F(" val=")); Serial.print(val);
  Serial.print(F(" IINST=")); Serial.print(v.iinst);
  Serial.print(F(" PAPP=")); Serial.println(v.papp);

  if (loraOk) {
    driver.setModeIdle();
    bool ok = manager.sendtoWait(buf, PAYLOAD_LEN, SERVER_ADDRESS);
    Serial.println(ok ? F("ACK OK") : F("No ACK"));
    blinkRGB(0, 255, 0);
    if (ok) { delay(120); blinkRGB(0, 255, 0); }
    driver.sleep();
  }

  boot_seq++;
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
void setup() {
  pinMode(TIC_OUT, OUTPUT); digitalWrite(TIC_OUT, LOW);
  pinMode(RGB_R,   OUTPUT);
  pinMode(RGB_G,   OUTPUT);
  pinMode(RGB_B,   OUTPUT);
  ledOff();

  ledBootHello();

  // Clé HMAC depuis EEPROM
  readKeyFromEEPROM(hmac_key);
  if (isKeyBlank(hmac_key)) {
    provisioningMode();  // ne retourne pas — reboot à la fin
  }

  Serial.begin(9600);
  Serial.println(F("tic-reader boot"));
  Serial.flush(); Serial.end();

  // Init RFM95
  pinMode(RFM95_RST, OUTPUT);
  digitalWrite(RFM95_RST, HIGH); delay(10);
  digitalWrite(RFM95_RST, LOW);  delay(10);
  digitalWrite(RFM95_RST, HIGH); delay(10);

  if (manager.init()) {
    driver.setFrequency(RF95_FREQ);
    driver.setTxPower(RF95_TXPOWER, false);  // 20 dBm — supercap 0.47F requis (pic ~120 mA)
    // SF9 BW125 — pas de constante RadioHead pour cette combinaison.
    // {REG_1D, REG_1E, REG_26} = {BW125+CR4/5, SF9+CRC, LDROptimize off}.
    // Validé terrain ben01 (RSSI ≈ -97 à -103 dBm, SNR +7/+10 dB).
    // Doit rester ISO avec lora-receiver/main.py côté Pi.
    static const RH_RF95::ModemConfig SF9_BW125 = {0x72, 0x94, 0x00};
    driver.setModemRegisters(&SF9_BW125);
    driver.sleep();
    loraOk = true;
    Serial.begin(9600);
    Serial.println(F("LoRa OK"));
    Serial.flush(); Serial.end();
    ledLoraOK();
  } else {
    ledLoraKO();
  }
  wdt_enable(WDTO_8S);
}

// ---------------------------------------------------------------------------
// Loop
// ---------------------------------------------------------------------------
void loop() {
  // Sleep ~32s
  for (uint8_t i = 0; i < 4; i++) {
    LowPower.powerDown(SLEEP_8S, ADC_OFF, BOD_OFF);
  }
  wdt_enable(WDTO_8S);  // LowPower desactive le WDT pendant le sleep

  Serial.begin(9600);
  blinkRGB(255, 255, 255);  // heartbeat

  if (!isVoltageSufficient()) {
    Serial.flush(); Serial.end();
    blinkErr(1);
    return;
  }

  Serial.flush(); Serial.end();
  String frame = readTIC();
  Serial.begin(9600);

  if (frame == "") {
    blinkErr(2);
  } else {
    TICValues v = parseTICFrame(frame);
    if (firstFrame) {
      blinkRGB(255, 80, 0);  // orange : premiere trame / discovery
      if (v.adco[0] != 0) {
        Serial.print(F("Discovery ADCO="));
        Serial.println(v.adco);
        Serial.flush(); Serial.end();
        sendBootFrame(v.adco);
        Serial.begin(9600);
      }
      firstFrame = false;
    } else if (!v.valid) {
      blinkErr(3);
    } else {
      blinkRGB(0, 0, 255);  // TIC valide
      Serial.flush(); Serial.end();
      sendBinary(v);
      Serial.begin(9600);
    }
  }

  Serial.flush(); Serial.end();
}
