/*
  tic-reader — BEN Arduino emitter
  =================================
  Lit la TIC du compteur Linky et transmet la COURBE de charge (PAPP fin)
  en LoRa vers le Pi Zero récepteur (ben-device).

  ── Trame v0x04 — courbe PAPP batchée (delta + keyframe), longueur variable ──
  Remplace l'ancienne v0x02 (1 index / 32 s, trop grossier pour le NILM). On lit
  TOUTES les trames TIC (~1,7 s en historique), on n'envoie que les DELTAS de PAPP
  par rapport à un keyframe embarqué, et on flush un batch toutes les ~60 s. Le
  keyframe porte aussi l'index actif → l'index cumulatif passe dans chaque batch
  (plus besoin de v0x02). Spec complète : rasperry/LORA_PROTOCOL.md §17.

    0       version      uint8  = 0x04
    1       flags        bit0-1 DEMAIN, bit2 ADPS, bit3 PEJP,
                         bit4 ts_valid, bit5 src_standard, bit6 has_ext (=0 en histo)
    2-3     batch_seq    uint16 LE — RAM, reset au boot, +1/batch
    4       index_id     uint8  BASE=0x00 HCHC=0x01 HCHP=0x02 EJP/BBR=0x03..0x0A
    5-8     index_value  uint32 LE — index absolu au 1er échantillon (Wh)
    9-10    papp_ref     uint16 LE — PAPP du 1er échantillon (VA)
    11      N            uint8  — nb d'échantillons du batch
    12      period_ds    uint8  — période nominale en 1/10 s (20 = 2,0 s, historique)
    13      ts_season    'E'/'H'/0x00 (absent en historique)
    14-19   ts           YY MM DD hh mm ss (0 en historique ; champ DATE en standard)
    20..    deltas       (N-1) deltas PAPP, varint zig-zag (1-3 o)
    len-8   HMAC         HMAC-SHA256(key, octets 0..len-9) tronqué 8 octets

  Un CHANGEMENT D'INDEX (PTEC) pendant un batch → flush immédiat + nouveau keyframe
  → chaque batch est homogène en index_id (cf. §17.3). Mode STANDARD (9600 bd,
  URMS/IRMS/SINSTI, horodate absolue, bloc extension) : différé, bits réservés.

  Trame d'identité v0x01 (boot/discovery, 20 o) : version=0x01 + ADCO(12) — inchangée.

  Énergie : la capture continue (pas de deep-sleep, MCU ~5 mA + TX) reste ~6 mA
  moyen côté ligne, très en deçà de la borne A du Linky (~50-100 mA, cf. §8.3). Un
  futur device SANS borne A (batterie) imposerait une capture fenêtrée — hors v1.

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
#define PROTOCOL_VERSION_BOOT  0x01   // trame d'identité (ADCO)
#define PROTOCOL_VERSION_CURVE 0x04   // trame courbe batchée
#define BOOT_PAYLOAD_LEN       20     // v0x01 : version + ADCO(12), padding
#define HMAC_LEN                8

// Courbe v0x04
// CURVE_BUF_LEN dimensionné pour l'HISTORIQUE v1 (flush ~60 s ≈ 30 éch. ≈ ~110 o).
// 160 o tient un batch de 60 s pire-cas (deltas 2-3 o) avec marge, et économise ~80 o
// de SRAM sur le 328P. Le mode STANDARD (1 Hz, batch ~3 min, ~175 éch.) demandera ~240.
#define CURVE_BUF_LEN     160         // < RH_RF95_MAX_MESSAGE_LEN (251)
#define CURVE_MAX_SAMPLES 130         // garde secondaire (buffer-full domine en pratique)
#define SAMPLE_PERIOD_DS  20          // période nominale = 2,0 s (cadence TIC historique)
#define CURVE_FLUSH_MS    60000UL     // flush périodique → fraîcheur /live ~60 s

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
// Bits v0x04 (réservés ; tous à 0 en historique). Posés en standard (différé).
#define FLAG_TS_VALID    0x10
#define FLAG_SRC_STANDARD 0x20
#define FLAG_HAS_EXT     0x40

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
static bool firstFrame = true;
static uint8_t hmac_key[HMAC_KEY_LEN];

// --- État courbe v0x04 ---
// curveBuf est fichier-scope (PAS pile) pour ne pas entrer en collision avec le heap
// String de la TIC pendant le parsing. Encodage AU FIL DE L'EAU (curveAdd) : on
// n'archive jamais la courbe brute, seulement les deltas dans curveBuf.
static uint8_t  curveBuf[CURVE_BUF_LEN];
static uint16_t curvePos     = 0;     // curseur d'écriture
static uint16_t papp_prev    = 0;     // dernière PAPP encodée
static uint8_t  curveN       = 0;     // échantillons dans le batch courant
static bool     curveActive  = false;
static uint8_t  curveIndexId = IDX_UNKNOWN;  // index_id du batch courant (homogène)
static uint16_t batch_seq    = 0;     // RAM, reset au boot, +1/batch
static unsigned long curveT0 = 0;     // millis() du début de batch (flush périodique)

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
      delay(50);  // laisse le temps au "OK\n" de partir avant le reset

      // Soft reset via saut au vecteur 0 — n'arme PAS le WDT.
      // Le pattern wdt_enable(WDTO_15MS) + while(true) cause un bootloop avec
      // certains Optiboot qui ne désarment pas le WDT en entrée (LED pin 13
      // scintille à ~60 Hz, plus de fenêtre upload, user code jamais atteint).
      asm volatile ("jmp 0");
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

// Parse UNE ligne TIC (déjà validée checksum) directement dans v, EN PLACE,
// sans String (char* only) → pas de heap, robuste sur AVR.
static void parseTICLine(char* line, uint8_t len, TICValues& v) {
  int fs = -1, ls = -1;
  for (uint8_t i = 0; i < len; i++) if (line[i] == ' ') { if (fs < 0) fs = i; ls = i; }
  if (fs < 1 || ls <= fs) return;
  line[fs] = 0;                  // termine le nom
  line[ls] = 0;                  // termine la valeur (au dernier espace)
  const char* name = line;
  const char* val  = line + fs + 1;
  if      (!strcmp(name, "ADCO"))    strncpy(v.adco,    val, sizeof(v.adco)    - 1);
  else if (!strcmp(name, "OPTARIF")) strncpy(v.optarif, val, sizeof(v.optarif) - 1);
  else if (!strcmp(name, "PTEC"))    strncpy(v.ptec,    val, sizeof(v.ptec)    - 1);
  else if (!strcmp(name, "DEMAIN"))  strncpy(v.demain,  val, sizeof(v.demain)  - 1);
  else if (!strcmp(name, "BASE"))    { v.base   = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_BASE;   }
  else if (!strcmp(name, "HCHC"))    { v.hchc   = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_HCHC;   }
  else if (!strcmp(name, "HCHP"))    { v.hchp   = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_HCHP;   }
  else if (!strcmp(name, "EJPHN"))   { v.ejphn  = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_EJPHN;  }
  else if (!strcmp(name, "EJPHPM"))  { v.ejphpm = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_EJPHPM; }
  else if (!strcmp(name, "BBRHCJB")) { v.bbr[0] = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_BBR0;   }
  else if (!strcmp(name, "BBRHPJB")) { v.bbr[1] = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_BBR1;   }
  else if (!strcmp(name, "BBRHCJW")) { v.bbr[2] = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_BBR2;   }
  else if (!strcmp(name, "BBRHPJW")) { v.bbr[3] = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_BBR3;   }
  else if (!strcmp(name, "BBRHCJR")) { v.bbr[4] = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_BBR4;   }
  else if (!strcmp(name, "BBRHPJR")) { v.bbr[5] = strtoul(val, 0, 10);           v.fields_seen |= TIC_SEEN_BBR5;   }
  else if (!strcmp(name, "IINST"))   { v.iinst  = (uint16_t)strtoul(val, 0, 10); v.fields_seen |= TIC_SEEN_IINST;  }
  else if (!strcmp(name, "PAPP"))    { v.papp   = (uint16_t)strtoul(val, 0, 10); v.fields_seen |= TIC_SEEN_PAPP;   }
  else if (!strcmp(name, "ADPS"))    v.adps_present = true;
  else if (!strcmp(name, "PEJP"))    v.pejp_present = true;
}

// Lit UNE trame TIC (STX..ETX) et la parse directement dans v. Char-based, SANS
// String → plus de corruption de heap. Retourne true si ≥1 ligne lue.
bool readAndParseTIC(TICValues& v) {
  memset(&v, 0, sizeof(v));
  digitalWrite(TIC_OUT, HIGH);
  Serial.begin(1200, SERIAL_7E1);

  const char STX = 0x02, ETX = 0x03, LF = 0x0A, CR = 0x0D;
  unsigned long t0 = millis();
  char c = 0;
  char line[40]; uint8_t li = 0; bool inLine = false;
  uint8_t kept = 0, dropped = 0;

  while (c != STX) {
    wdt_reset();
    if (Serial.available()) c = Serial.read();
    else if (millis() - t0 > TIC_TIMEOUT_MS) {
      Serial.flush(); Serial.end(); digitalWrite(TIC_OUT, LOW); return false;
    }
  }

  t0 = millis();  // budget propre pour la lecture STX->ETX
  while (c != ETX) {
    wdt_reset();
    if (Serial.available()) {
      c = Serial.read();
      if (c == LF) { li = 0; inLine = true; }
      else if (c == CR) {
        if (inLine && li > 0) {
          line[li] = 0;
          if (verifyTICChecksum(line, li)) { parseTICLine(line, li, v); kept++; }
          else dropped++;
        }
        inLine = false;
      } else if (inLine && li < sizeof(line) - 1) { line[li++] = c; }
    } else if (millis() - t0 > TIC_TIMEOUT_MS) break;
  }

  Serial.flush(); Serial.end();
  digitalWrite(TIC_OUT, LOW);
  v.valid = (v.ptec[0] != 0);
  (void)dropped;
  return (kept > 0);
}

// (readTIC/parseTICFrame String-based supprimés → readAndParseTIC char-based ci-dessus)

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
// Trame boot (version=0x01) : ADCO en clair dans les octets 1-12.
// Chiffrement PDL : sujet separe, a traiter ulterieurement.
void sendBootFrame(const char* adco) {
  uint8_t buf[BOOT_PAYLOAD_LEN];
  memset(buf, 0, BOOT_PAYLOAD_LEN);
  buf[0] = PROTOCOL_VERSION_BOOT;
  memcpy(buf + 1, adco, 12);

  if (loraOk) {
    driver.setModeIdle();
    manager.sendtoWait(buf, BOOT_PAYLOAD_LEN, SERVER_ADDRESS);
    blinkRGB(0, 0, 255);  // bleu = boot frame envoyee
    driver.sleep();
  }
}

// ---------------------------------------------------------------------------
// Courbe v0x04 — keyframe + deltas (codec inspiré I-frame/P-frame, cf. §17)
// ---------------------------------------------------------------------------
void curveFlush();  // prototype (curveAdd l'appelle quand le buffer est plein)

// Démarre un batch : le keyframe EST le 1er échantillon (index actif + PAPP absolue).
void curveStart(const TICValues& v, uint8_t id, uint32_t value) {
  curveBuf[0] = PROTOCOL_VERSION_CURVE;
  curveBuf[1] = buildFlags(v);              // bits 0-3 ; bits 4/5/6 = 0 en historique
  memcpy(curveBuf + 2, &batch_seq, 2);
  curveBuf[4] = id;
  memcpy(curveBuf + 5, &value, 4);
  memcpy(curveBuf + 9, &v.papp, 2);         // papp_ref = PAPP du 1er échantillon
  // buf[11]=N et buf[12]=period_ds : écrits à l'envoi (curveFlush).
  curveBuf[13] = 0x00;                      // ts_season absent (historique)
  memset(curveBuf + 14, 0, 6);              // ts absent (historique)
  curvePos     = 20;
  papp_prev    = v.papp;
  curveN       = 1;
  curveActive  = true;
  curveIndexId = id;
  curveT0      = millis();
}

// Append d'un échantillon = delta zig-zag varint de PAPP vs le précédent.
void curveAdd(uint16_t papp) {
  int32_t  d  = (int32_t)papp - (int32_t)papp_prev;
  uint32_t zz = ((uint32_t)d << 1) ^ (uint32_t)(d >> 31);   // zig-zag
  while (zz >= 0x80) { curveBuf[curvePos++] = (zz & 0x7F) | 0x80; zz >>= 7; }
  curveBuf[curvePos++] = (uint8_t)zz;
  papp_prev = papp;
  curveN++;
  // -3 = marge d'un varint 3 o au pire ; -HMAC_LEN = réserve la signature finale.
  if (curvePos >= CURVE_BUF_LEN - HMAC_LEN - 3 || curveN >= CURVE_MAX_SAMPLES)
    curveFlush();
}

// Finalise (N, period, HMAC) et émet le batch. Auto-suffisant : keyframe par trame
// (§17.3), aucun delta inter-batch.
void curveFlush() {
  if (!curveActive || curveN == 0) { curveActive = false; return; }
  curveBuf[11] = curveN;
  curveBuf[12] = SAMPLE_PERIOD_DS;

  uint8_t mac[32];
  sha256.resetHMAC(hmac_key, HMAC_KEY_LEN);
  sha256.update(curveBuf, curvePos);                          // signe octets 0..pos-1
  sha256.finalizeHMAC(hmac_key, HMAC_KEY_LEN, mac, 32);
  memcpy(curveBuf + curvePos, mac, HMAC_LEN);
  uint16_t len = curvePos + HMAC_LEN;

  Serial.begin(9600);
  Serial.print(F("v04 seq=")); Serial.print(batch_seq);
  Serial.print(F(" n="));      Serial.print(curveN);
  Serial.print(F(" idx=0x"));  Serial.print(curveIndexId, HEX);
  Serial.print(F(" len="));    Serial.println(len);
  Serial.flush(); Serial.end();

  if (loraOk) {
    driver.setModeIdle();
    manager.sendtoWait(curveBuf, len, SERVER_ADDRESS);
    blinkRGB(0, 255, 255);    // cyan = batch courbe envoyé
    driver.sleep();
  }

  batch_seq++;                // prochain batch = nouveau keyframe
  curveActive = false;
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
// Capture CONTINUE : pas de deep-sleep. readTIC() bloque jusqu'à la prochaine trame
// TIC (~1,7 s en historique) → c'est lui qui cadence la boucle (= period_ds nominal).
// On lit TOUTES les trames et on accumule la courbe ; flush par batch (§17).
void loop() {
  wdt_enable(WDTO_8S);  // ré-armé chaque tour ; readTIC() fait wdt_reset() pendant la lecture

  Serial.begin(9600);
  bool vok = isVoltageSufficient();   // imprime "Vcc: x"
  Serial.flush(); Serial.end();
  if (!vok) {
    if (curveActive) curveFlush();    // brownout imminent : ne pas perdre le batch en cours
    blinkErr(1);
    return;
  }

  TICValues v;
  if (!readAndParseTIC(v)) { blinkErr(2); return; }   // pas de trame (timeout STX)

  if (firstFrame) {
    blinkRGB(255, 80, 0);             // orange : premiere trame / discovery
    if (v.adco[0] != 0) sendBootFrame(v.adco);
    firstFrame = false;
    return;
  }
  if (!v.valid) { blinkErr(3); return; }

  uint8_t id; uint32_t value;
  selectActiveIndex(v, id, value);
  if (id == IDX_UNKNOWN) {
    Serial.begin(9600);
    Serial.print(F("PTEC inconnu (")); Serial.print(v.ptec); Serial.println(')');
    Serial.flush(); Serial.end();
    blinkErr(4);
    return;
  }

  // Garde : l'index actif + PAPP doivent avoir été vus (checksum OK) dans la trame.
  // (IINST n'est plus requis : non porté par v0x04 historique — cf. bloc extension différé.)
  uint16_t required = TIC_SEEN_PAPP;
  if (id < sizeof(INDEX_ID_TO_SEEN) / sizeof(INDEX_ID_TO_SEEN[0]))
    required |= INDEX_ID_TO_SEEN[id];
  if ((v.fields_seen & required) != required) {
    blinkErr(2);                      // trame partielle → on saute (papp_prev inchangé)
    return;
  }

  // Accumulation courbe. Changement d'index actif (PTEC) → coupe le batch courant
  // et repart sur un keyframe du nouvel index → batch homogène (décision §17.3).
  if (!curveActive || id != curveIndexId) {
    if (curveActive && curveN > 1) curveFlush();
    curveStart(v, id, value);
  } else {
    curveAdd(v.papp);                 // peut auto-flush si le buffer est plein
  }

  // Flush périodique → fraîcheur /live ~60 s (sinon un batch traînerait jusqu'à plein).
  if (curveActive && curveN > 1 && (millis() - curveT0) >= CURVE_FLUSH_MS)
    curveFlush();

  blinkRGB(0, 12, 0, 4);              // tick vert discret = échantillon capté
}
