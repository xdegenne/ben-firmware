/*
  tic-reader — BEN Arduino emitter
  =================================
  Lit la TIC du compteur Linky et transmet la COURBE de charge (PAPP fin)
  en LoRa vers le Pi Zero récepteur (ben-device).

  ── Trame v0x05 — courbe PAPP batchée (delta + dt par point + keyframe), longueur variable ──
  Remplace l'ancienne v0x02 (1 index / 32 s, trop grossier pour le NILM). On lit
  TOUTES les trames TIC (~1,7 s en historique), on n'envoie que les DELTAS de PAPP
  par rapport à un keyframe embarqué, et on flush un batch toutes les ~60 s. Le
  keyframe porte aussi l'index actif → l'index cumulatif passe dans chaque batch
  (plus besoin de v0x02). Spec complète : rasperry/LORA_PROTOCOL.md §17.

    0       version      uint8  = 0x05
    1       flags        bit0-1 DEMAIN, bit2 ADPS, bit3 PEJP, bit4 ts_valid,
                         bit5 src_standard, bit6 has_ext  (bits 0-3 = 0 en standard)
    2-3     batch_seq    uint16 LE — RAM, reset au boot, +1/batch
    4       index_id     uint8  histo: PTEC 0x00..0x0A ; standard: NTARF 1..10
    5-8     index_value  uint32 LE — index absolu au 1er échantillon (Wh ; EASF[NTARF] en std)
    9-10    papp_ref     int16/uint16 LE — PAPP 1er éch. ; SIGNÉ si src_standard (− = injection)
    11      N            uint8  — nb d'échantillons du batch
    12      period_ds    uint8  — moyenne du batch en 1/10 s (SYNTHÈSE/hint ; le timing réel
                         vient des dt par point ci-dessous, pas de ce champ)
    13      ts_season    'E'/'H' si ts_valid, sinon 0x00 (horodate compteur fiable)
    14-19   ts           YY MM DD hh mm ss (binaire ; 0 si !ts_valid ; champ DATE en std)
    20..    points       (N-1) paires [dt, delta PAPP] : dt = écart au point précédent en
                         SECONDES (varint) — standard: écart d'horodate compteur ; histo:
                         écart millis() ; delta PAPP = varint zig-zag signé (1-3 o)
    [ext]   EAIT         uint32 LE (Wh) si has_ext — énergie injectée totale (producteur std)
    len-8   HMAC         HMAC-SHA256(key, octets 0..len-9) tronqué 8 octets

  Un CHANGEMENT D'INDEX (PTEC histo / NTARF std) pendant un batch → flush immédiat +
  nouveau keyframe → batch homogène en index_id (cf. §17.3). MODE STANDARD (9600 bd,
  Enedis-NOI-CPT_54E) : implémenté — auto-détection histo↔standard au boot (+EEPROM,
  +re-discovery), SINSTS/SINSTI → papp signé (net), horodate DATE → ts, EAIT → bloc ext.

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
// Interrupteur chiffrement (format cible). ⚠ 1 → objet ChaCha en RAM → 84 % → débordement pile à
// l'envoi (reboot par trame). Nécessite de libérer ~100 o (trim struct/curveBuf) AVANT de repasser à 1.
#define FRAME_ENCRYPT 1
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
#define PROTOCOL_VERSION_CURVE 0x05   // trame courbe batchée (v0x05 : dt par point)
#define FW_VERSION             "0.1.3"   // 0.1.3 : FIX trame boot dans buffer GLOBAL curveBuf (le buffer pile buf[64] débordait pendant le ChaCha et corrompait l ACK -> gate bloquée). 0.1.2 : découplage émetteur↔récepteur (incident ben-0001 09/07). setTimeout 600ms + setRetries 1. MACHINE À ÉTATS REGISTERING/STREAMING : tant que la trame de boot (petit paquet = probe de vivacité) n'est pas ACK, AUCUNE mesure émise ; retry boot à la cadence batch (v frais) ; mesure non-ACK → retour REGISTERING. Base 0.1.0 : garde histo tolérante + IINST 2e courbe + flush 55s + chiffrement ChaCha20 + logging aligné
#define BOOT_PAYLOAD_LEN       20     // v0x01 : version + ADCO(12) + ISOUSC + PREF, padding jusqu'à 20 (rétro)
#define BOOT_MAX_LEN           64     // format cible : header(7) + TLV (ADCO/ISOUSC/PREF/CONTRAT) + MAC(8)

// --- Format cible (docs/lora-frame-format.md) : types + flags core + tags TLV ---
#define TYPE_CURVE       0x05
#define TYPE_BOOT        0x01
#define CF_SRC_STANDARD  0x01   // octet flags du core courbe : bit0
#define CF_TS_VALID      0x02   // bit1
#define CF_HAS_IINST     0x04   // bit2 : 2e courbe IINST (histo) présente
#define T_ADCO    0x01
#define T_ISOUSC  0x02
#define T_PREF    0x03
#define T_CONTRAT 0x04
#define T_EAIT    0x10
#define T_LTARF   0x11
#define T_DEMAIN  0x20
#define T_NJOURF  0x21
#define T_NJOURF1 0x22
#define T_ADPS    0x30
#define T_PEJP    0x31
#define T_MSG1    0x40
#define T_MSG2    0x41
#define HMAC_LEN                8

// Courbe v0x04
// CURVE_BUF_LEN dimensionné pour l'HISTORIQUE v1 (flush ~60 s ≈ 30 éch. ≈ ~110 o).
// 160 o tient un batch de 60 s pire-cas (deltas 2-3 o) avec marge, et économise ~80 o
// de SRAM sur le 328P. Le mode STANDARD (1 Hz, batch ~3 min, ~175 éch.) demandera ~240.
#define CURVE_BUF_LEN     130         // < RH_RF95_MAX_MESSAGE_LEN (251)
#define CURVE_MAX_SAMPLES 130         // garde secondaire (buffer-full domine en pratique)
#define SAMPLE_PERIOD_DS  20          // période nominale = 2,0 s (cadence TIC historique)
#define CURVE_FLUSH_MS    55000UL     // flush périodique → /live garanti < 60 s (batch toujours dans la minute)

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
// Bits v0x04. Posés en STANDARD (implémenté) ; tous à 0 en historique.
#define FLAG_TS_VALID    0x10   // octets 13-19 (horodate compteur) valides
#define FLAG_SRC_STANDARD 0x20  // 0=historique, 1=standard ; dit aussi de lire papp en int16 signé
#define FLAG_HAS_EXT     0x40   // bloc extension présent (EAIT injecté, 4 o)
#define FLAG_EXT_V2      0x80   // bloc ext v2 (bitmask ext_fields : bit0 EAIT, bit1 LTARF) — récepteur ≥ pi-0.0.54

// ---------------------------------------------------------------------------
// Modes TIC (Enedis-NOI-CPT_54E) + auto-détection
// ---------------------------------------------------------------------------
#define MODE_HISTO     0
#define MODE_STANDARD  1
#define TIC_BAUD_HISTO 1200
#define TIC_BAUD_STD   9600
// Période nominale de l'échantillonnage (1/10 s) — porte le pas de temps au récepteur.
#define SAMPLE_PERIOD_DS_HISTO 20   // ~2,0 s/trame en historique
#define SAMPLE_PERIOD_DS_STD   10   // ~1,0 s/trame en standard
// Bloc extension : énergie active injectée totale EAIT (uint32 Wh).
#define EXT_INJECT_LEN  4
// Re-discovery : si N lectures consécutives échouent (Enedis a peut-être rebasculé
// le mode), on re-sonde les deux débits.
#define REDISCOVER_FAILS 5

// ---------------------------------------------------------------------------
// EEPROM
// ---------------------------------------------------------------------------
#define HMAC_KEY_ADDR 0x00
#define HMAC_KEY_LEN  32
#define MODE_ADDR     0x20   // mode TIC auto-détecté persisté (reboot rapide)
#define BOOT_COUNT_ADDR 0x30 // compteur de boot (3 octets EEPROM) — nonce format cible + reboot

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
// Format cible (variante B) : AUCUNE clé stockée — re-dérivées de la clé EEPROM au flush (-96 o).
static uint32_t boot_count = 0;        // EEPROM, +1/boot (survit brownout) — nonce hi
static uint32_t msg_count  = 0;        // RAM, +1/trame émise, reset au boot — nonce lo

// --- État courbe v0x04 ---
// curveBuf est fichier-scope (PAS pile) pour ne pas entrer en collision avec le heap
// String de la TIC pendant le parsing. Encodage AU FIL DE L'EAU (curveAdd) : on
// n'archive jamais la courbe brute, seulement les deltas dans curveBuf.
static uint8_t  curveBuf[CURVE_BUF_LEN];
static uint16_t curvePos     = 0;     // curseur d'écriture
static int32_t  papp_prev    = 0;     // dernière PAPP encodée (signée : net en standard)
static int32_t  iinst_prev   = 0;     // histo : dernier IINST encodé (2e courbe)
static uint8_t  curveN       = 0;     // échantillons dans le batch courant
static bool     curveActive  = false;
static bool     curveFlushPending = false;  // deferred flush : flush exécuté HORS du bloc v (pile dégagée)
static uint8_t  curveIndexId = IDX_UNKNOWN;  // index_id du batch courant (homogène)
static bool     curveHasInject = false;      // batch standard producteur → bloc ext EAIT
static uint32_t curveEait     = 0;           // EAIT au keyframe (Wh) — écrit dans le bloc ext
static char     curveLtarf[17] = {0};        // LTARF capté au keyframe (ext v2 bit1) si NTARF a changé
static bool     curveSendLtarf = false;      // ce batch doit-il transporter LTARF ?
static uint8_t  curveDemain = 0xFF;          // histo : couleur demain (0=BLEU 1=BLAN 2=ROUG ; 0xFF=absent) → TLV
static bool     curveAdps   = false;         // histo : dépassement puissance → TLV présence
static bool     curvePejp   = false;         // histo : préavis EJP → TLV présence
static uint8_t  curveNjourf  = 0xFF;         // std : n° profil jour (Tempo) → TLV (0xFF = absent)
static uint8_t  curveNjourf1 = 0xFF;         // std : n° profil lendemain → TLV (0xFF = absent)
static uint8_t  lastSentIsousc = 0;          // dernier ISOUSC émis (v0x01) → ré-émet sur changement
static uint8_t  lastSentPref   = 0;          // dernier PREF émis (v0x01, standard) → ré-émet sur changement
static uint16_t lastSentNgtfHash = 0;        // hash du dernier NGTF émis (v0x01) → détecte le changement fournisseur (économe RAM vs stocker la chaîne)
static bool     bootAcked      = false;      // false = REGISTERING (on n'émet AUCUNE mesure tant que la trame de boot n'est pas ACK), true = STREAMING. Une trame boot minuscule est le meilleur probe de vivacité : si elle ne passe/n'ACK pas, la grosse trame courbe non plus.
static uint16_t batch_seq    = 0;     // RAM, reset au boot, +1/batch
static unsigned long curveT0 = 0;     // millis() du début de batch (flush périodique)
static uint32_t curveLastOffSec = 0;  // histo : offset cumulé (s) du dernier point vs curveT0 (dt v0x05)
static int32_t  curveLastTod    = 0;  // standard : time-of-day (s) du dernier point (dt = écart d'horodate)

// Mode TIC courant (auto-détecté) + compteur d'échecs consécutifs pour re-discovery.
static uint8_t  ticMode      = MODE_HISTO;
static uint8_t  consecFail   = 0;
static uint32_t lastStdIndex = 0;     // standard : dernier EASF[NTARF] vu (carry-forward)
static uint8_t  lastNtarf    = 0;     // standard : dernier NTARF vu (carry-forward)
static uint8_t  ltarfSentForNtarf = 0;  // standard : NTARF dont le LTARF a été transmis (dédup ext LTARF)

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
  uint8_t  isousc;        // intensité souscrite (A) — abonnement HISTO (statique)
  uint8_t  pref;          // puiss. de réf. (kVA) — abonnement STANDARD (statique, chantier ISOUSC)
  uint16_t fields_seen;
  // --- Champs MODE STANDARD (Enedis-NOI-CPT_54E §6.2) -----------------------
  int16_t  papp_net;      // net signé : soutiré (SINSTS) + / injection (SINSTI) -
  uint8_t  ntarf;         // n° index tarifaire actif (1..10)
  uint8_t  njourf;        // n° profil jour courant (Tempo standard, 0-9)
  uint8_t  njourf1;       // n° profil lendemain (NJOURF+1)
  uint32_t easf_active;      // TRIM : index fournisseur ACTIF (Wh) — capté au parse via lastNtarf reporté
  bool     easf_active_seen; // true = EASF actif vu (checksum OK) cette trame
  uint32_t eait;          // énergie active injectée totale (Wh) — producteur
  bool     has_inject;    // EAIT présent (foyer producteur)
  char     ts[14];        // horodate DATE "SAAMMJJhhmmss" (saison + 12 chiffres)
  char     ltarf[17];     // libellé tarif EN COURS (standard, LTARF) — label du registre actif
  char     ngtf[17];      // nom du calendrier tarifaire fournisseur (standard, NGTF) — quasi-statique
};

// ===================== Format cible — helpers d'encodage =====================
// docs/lora-frame-format.md. Enveloppe : [ver][boot_count:3][msg_count:3][corps][MAC:8].
static void bumpBootCount() {     // lit 3 o EEPROM, +1, réécrit (au boot)
  boot_count = (uint32_t)EEPROM.read(BOOT_COUNT_ADDR)
             | ((uint32_t)EEPROM.read(BOOT_COUNT_ADDR + 1) << 8)
             | ((uint32_t)EEPROM.read(BOOT_COUNT_ADDR + 2) << 16);
  boot_count++;
  EEPROM.update(BOOT_COUNT_ADDR,     boot_count & 0xFF);
  EEPROM.update(BOOT_COUNT_ADDR + 1, (boot_count >> 8) & 0xFF);
  EEPROM.update(BOOT_COUNT_ADDR + 2, (boot_count >> 16) & 0xFF);
}
static uint16_t writeHeader(uint8_t* buf, uint8_t type) {   // header clair → pos=7
  buf[0] = type;
  buf[1] = boot_count & 0xFF; buf[2] = (boot_count >> 8) & 0xFF; buf[3] = (boot_count >> 16) & 0xFF;
  buf[4] = msg_count & 0xFF;  buf[5] = (msg_count >> 8) & 0xFF;  buf[6] = (msg_count >> 16) & 0xFF;
  return 7;
}
static uint16_t writeI24(uint8_t* buf, uint16_t pos, int32_t v) {  // int24 LE
  buf[pos++] = v & 0xFF; buf[pos++] = (v >> 8) & 0xFF; buf[pos++] = (v >> 16) & 0xFF;
  return pos;
}
static uint16_t writeTLV(uint8_t* buf, uint16_t pos, uint8_t tag, const uint8_t* val, uint8_t len) {
  buf[pos++] = tag; buf[pos++] = len;
  memcpy(buf + pos, val, len); pos += len;
  return pos;
}
#if FRAME_ENCRYPT
// ChaCha20 (RFC 8439 IETF) « maison » : keystream calculé sur la PILE, zéro objet permanent.
// Keystream validé identique à frame_codec._chacha20 (récepteur). ~128 o de pile PENDANT l'appel.
static inline uint32_t rotl32(uint32_t x, int n) { return (x << n) | (x >> (32 - n)); }
#define CHACHA_QR(a,b,c,d) a+=b; d^=a; d=rotl32(d,16); c+=d; b^=c; b=rotl32(b,12); \
                           a+=b; d^=a; d=rotl32(d,8);  c+=d; b^=c; b=rotl32(b,7);
#define CHACHA_LE32(p) ((uint32_t)(p)[0]|((uint32_t)(p)[1]<<8)|((uint32_t)(p)[2]<<16)|((uint32_t)(p)[3]<<24))
static void chacha20_xor(const uint8_t* key, const uint8_t* nonce, uint8_t* buf, uint16_t len) {
  uint32_t x[16], counter = 0;                       // 64 o de pile (un seul tableau)
  for (uint16_t off=0; off<len; off+=64) {
    x[0]=0x61707865UL; x[1]=0x3320646eUL; x[2]=0x79622d32UL; x[3]=0x6b206574UL;
    for (uint8_t i=0;i<8;i++) x[4+i]=CHACHA_LE32(key+4*i);
    x[12]=counter;
    for (uint8_t i=0;i<3;i++) x[13+i]=CHACHA_LE32(nonce+4*i);
    for (uint8_t r=0;r<10;r++) {
      CHACHA_QR(x[0],x[4],x[8],x[12]) CHACHA_QR(x[1],x[5],x[9],x[13]) CHACHA_QR(x[2],x[6],x[10],x[14]) CHACHA_QR(x[3],x[7],x[11],x[15])
      CHACHA_QR(x[0],x[5],x[10],x[15]) CHACHA_QR(x[1],x[6],x[11],x[12]) CHACHA_QR(x[2],x[7],x[8],x[13]) CHACHA_QR(x[3],x[4],x[9],x[14])
    }
    for (uint8_t i=0;i<16;i++) {                       // add de l'état INITIAL recalculé (pas stocké)
      uint32_t si = i<4 ? (i==0?0x61707865UL:i==1?0x3320646eUL:i==2?0x79622d32UL:0x6b206574UL)
                  : i<12 ? CHACHA_LE32(key+4*(i-4)) : i==12 ? counter : CHACHA_LE32(nonce+4*(i-13));
      uint32_t v=x[i]+si;
      for (uint8_t j=0;j<4;j++){ uint16_t pp=off+4*i+j; if (pp<len) buf[pp]^=(uint8_t)(v>>(8*j)); } }
    counter++;
  }
}
#endif

// Variante B : re-dérive les sous-clés à la volée depuis la clé EEPROM (0 clé stockée).
static uint16_t frameSeal(uint8_t* buf, uint16_t pos, bool encrypt) {
  uint8_t dev[HMAC_KEY_LEN], sub[HMAC_KEY_LEN];
  readKeyFromEEPROM(dev);
#if FRAME_ENCRYPT
  if (encrypt) {
    sha256.resetHMAC(dev, HMAC_KEY_LEN);
    sha256.update((const uint8_t*)"ben-lora-enc", 12);
    sha256.finalizeHMAC(dev, HMAC_KEY_LEN, sub, 32);
    buf[0] |= 0x80;
    uint8_t nonce[12];
    nonce[0] = boot_count & 0xFF; nonce[1] = (boot_count >> 8) & 0xFF; nonce[2] = (boot_count >> 16) & 0xFF;
    nonce[3] = msg_count & 0xFF;  nonce[4] = (msg_count >> 8) & 0xFF;  nonce[5] = (msg_count >> 16) & 0xFF;
    for (uint8_t i = 6; i < 12; i++) nonce[i] = 0;
    chacha20_xor(sub, nonce, buf + 7, pos - 7);
  }
#else
  (void)encrypt;
#endif
  sha256.resetHMAC(dev, HMAC_KEY_LEN);
  sha256.update((const uint8_t*)"ben-lora-mac", 12);
  sha256.finalizeHMAC(dev, HMAC_KEY_LEN, sub, 32);
  uint8_t mac[32];
  sha256.resetHMAC(sub, HMAC_KEY_LEN);
  sha256.update(buf, pos);
  sha256.finalizeHMAC(sub, HMAC_KEY_LEN, mac, 32);
  memcpy(buf + pos, mac, HMAC_LEN);
  return pos + HMAC_LEN;
}
// =============================================================================

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

// --- Langage LED (basse intensite = conso supercap). Couleur = quelle trame ; vert = ACK commun. ---
//   magenta = TX trame boot · blanc = TX trame courbe · vert = ACK · bleu = lecture TIC
//   orange = discovery · rouge = erreur
#define LED_ACK_G   16       // intensite vert ACK
#define LED_ERR_R   40       // intensite rouge erreur
// Envoi de trame : couleur de trame (30 ms) puis vert bref si ACK. Absence de vert = pas d'ACK.
static void blinkTx(uint8_t r, uint8_t g, uint8_t b, bool acked) {
  blinkRGB(r, g, b, 30);
  if (acked) blinkRGB(0, LED_ACK_G, 0, 30);
}

// erreur rouge : code x flashs (dim). 2x = pas de TIC / compteur muet.
// (Vcc bas = tick rouge dedie plus leger ; LoRa KO = sequence boot ledLoraKO.)
static void blinkErr(uint8_t code) {
  for (uint8_t i = 0; i < code; i++) {
    setRGB(LED_ERR_R, 0, 0); delay(50);
    ledOff();                delay(120);
  }
}

static void ledBootHello() { blinkRGB(20, 20, 20, 150); }   // blanc bref = hello boot

static void ledLoraOK() {                                    // 2x vert bref = init LoRa OK
  for (uint8_t i = 0; i < 2; i++) { setRGB(0, LED_ACK_G, 0); delay(80); ledOff(); delay(120); }
}

static void ledLoraKO() {                                    // 4x rouge bref = init LoRa KO (fatal)
  for (uint8_t i = 0; i < 4; i++) { setRGB(LED_ERR_R, 0, 0); delay(80); ledOff(); delay(120); }
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
  // Historique : DEUX modes de calcul coexistent selon les compteurs — on accepte les deux.
  //   S1 = somme "label SP data" (hors SP avant checksum).
  //   S2 = S1 + le SP avant checksum inclus.
  // Un vrai Linky peut être en S2 alors que le banc tic-gen émet en S1 → sans ça, blinkErr en boucle.
  uint8_t sum = 0;
  for (int i = 0; i < lastSpace; i++) sum += (uint8_t)line[i];
  const char cks = line[len - 1];
  if (cks == (char)((sum & 0x3F) + 0x20)) return true;          // S1
  sum += (uint8_t)line[lastSpace];                              // + SP avant checksum
  return cks == (char)((sum & 0x3F) + 0x20);                    // S2
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
  else if (!strcmp(name, "ISOUSC"))  v.isousc = (uint8_t)strtoul(val, 0, 10);   // abonnement (A)
  else if (!strcmp(name, "ADPS"))    v.adps_present = true;
  else if (!strcmp(name, "PEJP"))    v.pejp_present = true;
}

// ---- MODE STANDARD (Enedis-NOI-CPT_54E §5.3.6) ----------------------------
// Séparateur HT (0x09, ≠ SP en historique) ; checksum sur étiquette → HT de queue
// INCLUS (≠ historique où le SP de queue est exclu). `line` = contenu entre LF et
// CR : "ETIQ<HT>[HORODATE<HT>]DONNEE<HT>CK".
bool verifyTICChecksumStd(const char *line, size_t len) {
  if (len < 4) return false;
  if (line[len - 2] != '\t') return false;        // dernier séparateur = HT
  uint8_t sum = 0;
  for (size_t i = 0; i < len - 1; i++) sum += (uint8_t)line[i];  // HT de queue inclus
  return line[len - 1] == (char)((sum & 0x3F) + 0x20);
}

// Parse UNE ligne standard validée. Split sur HT : [étiquette, (horodate,) donnée,
// checksum]. La donnée est l'avant-dernier champ ; l'horodate (si 9 parties) le 2e.
// On déduit l'horodatage du nombre de HT — rien à coder en dur. On n'extrait que
// l'utile à BEN (SINSTS/SINSTI/EASF/NTARF/EAIT/ADSC/DATE).
static void parseTICLineStd(char* line, uint8_t len, TICValues& v) {
  uint8_t ht[4]; uint8_t n = 0;
  for (uint8_t i = 0; i < len && n < 4; i++) if (line[i] == '\t') ht[n++] = i;
  if (n < 2) return;                              // besoin d'au moins ETIQ<HT>DON<HT>CK
  line[ht[0]] = 0;                                // termine l'étiquette
  const char* name = line;
  uint8_t dStart = ht[n - 2] + 1;                 // donnée = champ avant le checksum
  line[ht[n - 1]] = 0;                            // termine la donnée
  const char* val = line + dStart;

  if      (!strcmp(name, "ADSC")) strncpy(v.adco, val, sizeof(v.adco) - 1);  // ≈ ADCO
  else if (!strcmp(name, "SINSTS")) {            // puiss. soutirée (VA) → net positif
    v.papp_net = (int16_t)strtol(val, 0, 10);
    v.fields_seen |= TIC_SEEN_PAPP;
  }
  else if (!strcmp(name, "SINSTI")) {            // puiss. injectée (VA) → net négatif
    long s = strtol(val, 0, 10);                 // un seul des deux est non nul à la fois
    if (s > 0) { v.papp_net = (int16_t)(-s); v.fields_seen |= TIC_SEEN_PAPP; }
  }
  else if (!strcmp(name, "NTARF")) v.ntarf = (uint8_t)strtoul(val, 0, 10);  // n° tarif actif
  else if (!strncmp(name, "EASF", 4) && name[4] >= '0' && name[5] != 0) {
    uint8_t idx = (uint8_t)strtoul(name + 4, 0, 10);   // EASF01..10 → 1..10
    if (idx == lastNtarf) { v.easf_active = strtoul(val, 0, 10); v.easf_active_seen = true; }  // TRIM : seul l'index actif (NTARF reporté)
  }
  else if (!strcmp(name, "EAIT")) { v.eait = strtoul(val, 0, 10); v.has_inject = true; }
  else if (!strcmp(name, "PREF")) v.pref = (uint8_t)strtoul(val, 0, 10);  // abonnement std (kVA)
  else if (!strcmp(name, "LTARF")) strncpy(v.ltarf, val, sizeof(v.ltarf) - 1);  // libellé tarif courant
  else if (!strcmp(name, "NGTF"))  strncpy(v.ngtf,  val, sizeof(v.ngtf)  - 1);  // calendrier tarifaire fournisseur
  else if (!strcmp(name, "NJOURF"))   v.njourf  = (uint8_t)strtoul(val, 0, 10);  // n° profil jour (Tempo std)
  else if (!strcmp(name, "NJOURF+1")) v.njourf1 = (uint8_t)strtoul(val, 0, 10);  // n° profil lendemain
  else if (!strcmp(name, "DATE")) {              // horodatée, donnée vide → horodate = champ 2
    if (n >= 3) { line[ht[1]] = 0; strncpy(v.ts, line + ht[0] + 1, sizeof(v.ts) - 1); }
  }
}

// UART TIC ouvert EN CONTINU (begin-once) : plus de Serial.begin()/end() par trame — ça
// créait une fenêtre aveugle où l'on ratait une trame sur deux. On (re)configure le
// débit/format SEULEMENT au changement de mode. serialTicMode = mode courant (0xFF = fermé).
static uint8_t serialTicMode = 0xFF;
static void ticSerialBegin(uint8_t mode) {
  if (serialTicMode == mode) return;
  Serial.end();
  Serial.begin(mode == MODE_STANDARD ? TIC_BAUD_STD : TIC_BAUD_HISTO, SERIAL_7E1);
  serialTicMode = mode;
}

// Lit UNE trame TIC (STX..ETX) et la parse directement dans v. Char-based, SANS
// String → plus de corruption de heap. `mode` = MODE_HISTO (1200, SP) ou
// MODE_STANDARD (9600, HT). Retourne true si ≥1 ligne valide lue.
bool readAndParseTIC(TICValues& v, uint8_t mode) {
  memset(&v, 0, sizeof(v));
  v.njourf = 0xFF; v.njourf1 = 0xFF;    // sentinel : 0xFF = absent (profil n°0 = valide)
  digitalWrite(TIC_OUT, HIGH);
  ticSerialBegin(mode);

  const char STX = 0x02, ETX = 0x03, LF = 0x0A, CR = 0x0D;
  unsigned long t0 = millis();
  char c = 0;
  char line[40]; uint8_t li = 0; bool inLine = false;
  uint8_t kept = 0, dropped = 0;

  while (c != STX) {
    wdt_reset();
    if (Serial.available()) c = Serial.read();
    else if (millis() - t0 > TIC_TIMEOUT_MS) {
      digitalWrite(TIC_OUT, LOW); return false;   // UART laissé ouvert (begin-once)
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
          bool ok = (mode == MODE_STANDARD)
                      ? verifyTICChecksumStd(line, li)
                      : verifyTICChecksum(line, li);
          if (ok) {
            if (mode == MODE_STANDARD) parseTICLineStd(line, li, v);
            else                       parseTICLine(line, li, v);
            kept++;
          } else dropped++;
        }
        inLine = false;
      } else if (inLine && li < sizeof(line) - 1) { line[li++] = c; }
    } else if (millis() - t0 > TIC_TIMEOUT_MS) break;
  }

  digitalWrite(TIC_OUT, LOW);   // UART laissé ouvert (begin-once)
  // Validité : historique = PTEC vu ; standard = PAPP vu (NTARF/horodate gérés dans la garde).
  if (mode == MODE_STANDARD)
    v.valid = (v.fields_seen & TIC_SEEN_PAPP) != 0;   // PAPP requis ; NTARF/horodate gérés dans la garde
  else
    v.valid = (v.ptec[0] != 0);
  (void)dropped;
  return (kept > 0);
}

// Auto-détection du mode TIC : on sonde un débit en lisant une trame ; si elle est
// VALIDE → c'est le bon mode (au mauvais débit, pas de STX propre → timeout/garbage).
// Mode persisté (EEPROM) sondé en premier (reboot rapide). 0xFF = aucun (compteur muet).
uint8_t discoverMode() {
  uint8_t persisted = EEPROM.read(MODE_ADDR);
  uint8_t first  = (persisted == MODE_STANDARD) ? MODE_STANDARD : MODE_HISTO;
  uint8_t second = (first == MODE_HISTO) ? MODE_STANDARD : MODE_HISTO;
  TICValues t;
  if (readAndParseTIC(t, first)  && t.valid) return first;
  if (readAndParseTIC(t, second) && t.valid) return second;
  return 0xFF;
}

static void persistMode(uint8_t mode) {
  if (EEPROM.read(MODE_ADDR) != mode) EEPROM.update(MODE_ADDR, mode);
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
  return vcc >= VCC_MIN;
}

// ---------------------------------------------------------------------------
// LoRa
// ---------------------------------------------------------------------------
// Trame boot (version=0x01) : ADCO octets 1-12, ISOUSC (A, histo) octet 13, PREF (kVA, std) octet 14.
// Chiffrement PDL : sujet separe, a traiter ulterieurement.
// Hash 16 bits (djb2-xor) pour détecter un changement de chaîne (NGTF) sans la stocker.
static uint16_t strhash16(const char* s) {
  uint16_t h = 5381;
  while (*s) h = (uint16_t)(((h << 5) + h) ^ (uint8_t)*s++);
  return h;
}

// Contrat (calendrier tarifaire) mode-agnostique pour la trame boot : NGTF en STANDARD,
// OPTARIF en HISTORIQUE. Le récepteur le stocke comme « contrat » (level_profile.ngtf).
static const char* contractOf(const TICValues& v) {
  return (ticMode == MODE_STANDARD) ? v.ngtf : v.optarif;
}

// Retourne true si la trame de boot a été ACQUITTÉE. L'appelant ne fige les marqueurs
// `lastSent*` QUE sur ACK → une trame de boot perdue (récepteur sourd au démarrage) est
// automatiquement ré-émise par la logique on-change au tour suivant, jusqu'à confirmation
// (même robustesse que le LTARF de courbe). Sans ça, l'identité/config (ADCO/ISOUSC/PREF/
// NGTF) reste inconnue du récepteur jusqu'au prochain reboot → jauge non calibrée, labels faux.
bool sendBootFrame(const char* adco, uint8_t isousc, uint8_t pref, const char* ngtf) {
  uint8_t* buf = curveBuf;   // FIX 0.1.3 : buffer GLOBAL (le buffer pile debordait pendant frameSeal/ChaCha -> corruption ACK)
  msg_count++;                                       // nonce lo (+1 par trame émise)
  uint16_t pos = writeHeader(buf, TYPE_BOOT);        // [0-6] header clair
  pos = writeTLV(buf, pos, T_ADCO, (const uint8_t*)adco, 12);
  if (isousc) { uint8_t b = isousc; pos = writeTLV(buf, pos, T_ISOUSC, &b, 1); }
  if (pref)   { uint8_t b = pref;   pos = writeTLV(buf, pos, T_PREF,   &b, 1); }
  if (ngtf && ngtf[0]) {
    uint8_t l = strlen(ngtf); if (l > 16) l = 16;
    pos = writeTLV(buf, pos, T_CONTRAT, (const uint8_t*)ngtf, l);
  }
  uint16_t len = frameSeal(buf, pos, FRAME_ENCRYPT);   // chiffre (si activé) + MAC → longueur totale

  bool acked = false;
  if (loraOk) {
    blinkRGB(30, 0, 30, 40);   // magenta bref = trame BOOT envoyee
    driver.setModeIdle();
    acked = manager.sendtoWait(buf, len, SERVER_ADDRESS);
    if (acked) blinkRGB(0, 40, 0, 40); else blinkRGB(40, 0, 0, 40);  // vert=ACK / rouge=pas d ACK
    driver.sleep();
  }
  return acked;
}

// ---------------------------------------------------------------------------
// Courbe v0x04 — keyframe + deltas (codec inspiré I-frame/P-frame, cf. §17)
// ---------------------------------------------------------------------------
void curveFlush();  // prototype (curveAdd l'appelle quand le buffer est plein)

// Valeur PAPP à encoder : net SIGNÉ en standard (SINSTS + / SINSTI −), PAPP uint16 en
// historique. Le récepteur la relit signée/non-signée selon FLAG_SRC_STANDARD.
static inline int32_t pappValue(const TICValues& v) {
  return (ticMode == MODE_STANDARD) ? (int32_t)v.papp_net : (int32_t)v.papp;
}

// Démarre un batch : le keyframe EST le 1er échantillon (index actif + PAPP absolue).
void curveStart(const TICValues& v, uint8_t id, uint32_t value) {
  int32_t papp = pappValue(v);
  msg_count++;                                        // nonce lo (+1 par trame émise)
  uint16_t pos = writeHeader(curveBuf, TYPE_CURVE);   // [0-6] header clair → pos = 7

  bool tsValid = (ticMode == MODE_STANDARD) &&
                 (v.ts[0] == 'E' || v.ts[0] == 'H');  // saison MAJUSCULE = horloge fiable
  uint8_t flags = (ticMode == MODE_STANDARD ? CF_SRC_STANDARD : 0)
                | (tsValid ? CF_TS_VALID : 0)
                | (ticMode == MODE_HISTO ? CF_HAS_IINST : 0);
  curveBuf[pos++] = flags;                            // [7]  flags
  memcpy(curveBuf + pos, &batch_seq, 2); pos += 2;    // [8-9] batch_seq
  curveBuf[pos++] = id;                               // [10] index_id
  memcpy(curveBuf + pos, &value, 4); pos += 4;        // [11-14] index_value
  pos = writeI24(curveBuf, pos, papp);                // [15-17] papp_ref (int24 signé)
  pos += 2;                                           // [18]=N, [19]=period_ds : à curveFlush
  if (tsValid) {                                      // [20-26] horodate compteur (7 o)
    curveBuf[pos++] = (uint8_t)v.ts[0];
    for (uint8_t i = 0; i < 6; i++)
      curveBuf[pos++] = (uint8_t)((v.ts[1 + i * 2] - '0') * 10 + (v.ts[2 + i * 2] - '0'));
  }
  if (ticMode == MODE_HISTO) {                        // 2e courbe : IINST de réf (uint16)
    memcpy(curveBuf + pos, &v.iinst, 2); pos += 2;
    iinst_prev = v.iinst;
  }
  curvePos       = pos;
  papp_prev      = papp;
  curveN         = 1;
  curveActive    = true;
  curveIndexId   = id;
  curveHasInject = (ticMode == MODE_STANDARD && v.has_inject);
  curveEait      = v.eait;
  // LTARF (ext v2) : transporté quand le NTARF de ce batch diffère de celui déjà confirmé
  // (dédup — le libellé est quasi-statique par NTARF). Le récepteur cache NTARF→LTARF.
  curveSendLtarf = (ticMode == MODE_STANDARD && v.ltarf[0] && id != ltarfSentForNtarf);
  if (curveSendLtarf) strncpy(curveLtarf, v.ltarf, sizeof(curveLtarf) - 1);
  // histo : DEMAIN (couleur demain) / ADPS / PEJP → capturés ici, émis en TLV au flush.
  if (ticMode == MODE_HISTO) {
    curveDemain = (strncmp(v.demain, "BLEU", 4) == 0) ? 0
                : (strncmp(v.demain, "BLAN", 4) == 0) ? 1
                : (strncmp(v.demain, "ROUG", 4) == 0) ? 2 : 0xFF;
    curveAdps = v.adps_present;
    curvePejp = v.pejp_present;
  } else { curveDemain = 0xFF; curveAdps = false; curvePejp = false; }
  curveNjourf = v.njourf; curveNjourf1 = v.njourf1;   // std Tempo (0xFF = absent) → TLV au flush
  curveT0         = millis();
  curveLastOffSec = 0;                                                 // histo : origine des dt (millis)
  curveLastTod    = (ticMode == MODE_STANDARD) ? todSeconds(v.ts) : 0; // standard : tod du keyframe (point 0)
}

// Time-of-day (s depuis minuit) depuis l'horodate standard v.ts = "SAAMMJJhhmmss" :
// hh@[7][8], mm@[9][10], ss@[11][12]. Base du dt par point en standard (écart d'horodate).
static int32_t todSeconds(const char* ts) {
  int hh = (ts[7]  - '0') * 10 + (ts[8]  - '0');
  int mm = (ts[9]  - '0') * 10 + (ts[10] - '0');
  int ss = (ts[11] - '0') * 10 + (ts[12] - '0');
  return (int32_t)hh * 3600 + mm * 60 + ss;
}

// Append d'un échantillon (v0x05) = paire [dt, delta PAPP], dt en SECONDES (varint) :
//  - standard    : dt = écart d'HORODATE compteur (instant de MESURE, autoritaire, sans dérive).
//                  La garde amont garantit une horodate valide → pas de garbage ici.
//  - historique  : dt = écart millis() arrondi (pas d'horodate dispo), sans dérive cumulative
//                  (différence d'offsets arrondis vs curveT0 → Σdt = durée totale arrondie).
//  - delta PAPP  : varint zig-zag signé vs le précédent (gère l'injection en standard).
void curveAdd(int32_t papp, const TICValues& v) {
  uint32_t dt;
  if (ticMode == MODE_STANDARD) {
    int32_t tod = todSeconds(v.ts);
    int32_t d = tod - curveLastTod;
    if (d < 0) d += 86400L;                                  // passage minuit
    dt = (uint32_t)d;
    curveLastTod = tod;
  } else {
    uint32_t offSec = (uint32_t)((millis() - curveT0 + 500UL) / 1000UL);  // ms → s arrondi
    dt = offSec - curveLastOffSec;
    curveLastOffSec = offSec;
  }
  while (dt >= 0x80) { curveBuf[curvePos++] = (dt & 0x7F) | 0x80; dt >>= 7; }
  curveBuf[curvePos++] = (uint8_t)dt;

  int32_t  d  = papp - papp_prev;
  uint32_t zz = ((uint32_t)d << 1) ^ (uint32_t)(d >> 31);    // zig-zag (gère le signe → injection)
  while (zz >= 0x80) { curveBuf[curvePos++] = (zz & 0x7F) | 0x80; zz >>= 7; }
  curveBuf[curvePos++] = (uint8_t)zz;
  papp_prev = papp;
  if (ticMode == MODE_HISTO) {                              // 2e courbe : delta IINST (zigzag)
    int32_t  di  = (int32_t)v.iinst - iinst_prev;
    uint32_t zzi = ((uint32_t)di << 1) ^ (uint32_t)(di >> 31);
    while (zzi >= 0x80) { curveBuf[curvePos++] = (zzi & 0x7F) | 0x80; zzi >>= 7; }
    curveBuf[curvePos++] = (uint8_t)zzi;
    iinst_prev = v.iinst;
  }
  curveN++;
  // marge worst-case = 2 varints de 3 o (dt+delta) = 6 ; -HMAC_LEN ; -TLV réservés :
  // EAIT (2+4) + LTARF (2+16). (increment 3 : + DEMAIN/ADPS/PEJP/NJOURF.)
  uint8_t extReserve = (curveHasInject ? 6 : 0) + (curveSendLtarf ? 18 : 0) + (ticMode == MODE_HISTO ? 7 : 6);
  if (curvePos >= CURVE_BUF_LEN - HMAC_LEN - extReserve - (ticMode == MODE_HISTO ? 9 : 6) || curveN >= CURVE_MAX_SAMPLES)
    curveFlushPending = true;   // deferred : envoi après fermeture du bloc v
}

// Finalise (N, period, [ext], HMAC) et émet le batch. Auto-suffisant : keyframe par
// trame (§17.3), aucun delta inter-batch.
void curveFlush() {
  if (!curveActive || curveN == 0) { curveActive = false; return; }
  curveBuf[18] = curveN;                              // N (offset fixe du core)
  // period_ds = période RÉELLE mesurée (millis), en 1/10 s : (durée batch)/(N-1). Borné 1..255.
  {
    uint8_t pds = (ticMode == MODE_STANDARD) ? SAMPLE_PERIOD_DS_STD : SAMPLE_PERIOD_DS_HISTO;
    if (curveN >= 2) {
      uint32_t per_ds = (millis() - curveT0) / (uint32_t)(curveN - 1) / 100UL;
      pds = (per_ds < 1) ? 1 : (per_ds > 255 ? 255 : (uint8_t)per_ds);
    }
    curveBuf[19] = pds;                              // period_ds (offset fixe)
  }

  // Champs optionnels en TLV (après les points, avant le MAC). On-change / producteur.
  // increment 3 : + DEMAIN/ADPS/PEJP (histo) + NJOURF/NJOURF+1 (std).
  if (curveHasInject) {
    uint8_t eb[4]; memcpy(eb, &curveEait, 4);
    curvePos = writeTLV(curveBuf, curvePos, T_EAIT, eb, 4);
  }
  if (curveSendLtarf) {
    uint8_t l = strlen(curveLtarf); if (l > 16) l = 16;
    curvePos = writeTLV(curveBuf, curvePos, T_LTARF, (const uint8_t*)curveLtarf, l);
  }
  if (curveDemain != 0xFF) curvePos = writeTLV(curveBuf, curvePos, T_DEMAIN, &curveDemain, 1);
  if (curveAdps)  curvePos = writeTLV(curveBuf, curvePos, T_ADPS, &curveDemain, 0);  // présence (len 0)
  if (curvePejp)  curvePos = writeTLV(curveBuf, curvePos, T_PEJP, &curveDemain, 0);  // présence (len 0)
  if (ticMode == MODE_STANDARD && curveNjourf  != 0xFF) curvePos = writeTLV(curveBuf, curvePos, T_NJOURF,  &curveNjourf,  1);
  if (ticMode == MODE_STANDARD && curveNjourf1 != 0xFF) curvePos = writeTLV(curveBuf, curvePos, T_NJOURF1, &curveNjourf1, 1);

  uint16_t len = frameSeal(curveBuf, curvePos, FRAME_ENCRYPT);  // chiffre (si activé) + MAC → longueur totale

  if (loraOk) {
    driver.setModeIdle();
    blinkRGB(30, 30, 30, 40);   // blanc bref = trame COURBE envoyee
    bool acked = manager.sendtoWait(curveBuf, len, SERVER_ADDRESS);
    if (acked) blinkRGB(0, 40, 0, 40); else blinkRGB(40, 0, 0, 40);  // vert=ACK / rouge=pas d ACK
    // LTARF confirmé livré pour ce NTARF → on ne le re-transmet plus (jusqu'à changement de
    // NTARF). Pas d'ACK → curveSendLtarf reste vrai au prochain batch = re-transmission (robuste).
    if (curveSendLtarf && acked) ltarfSentForNtarf = curveIndexId;
    if (!acked) bootAcked = false;     // mesure non-ACK → récepteur reparti → retour REGISTERING (on cesse d'émettre des mesures, on reprend le retry boot)
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
  { uint8_t dev[HMAC_KEY_LEN]; readKeyFromEEPROM(dev);
    if (isKeyBlank(dev)) provisioningMode(); }
  bumpBootCount();

  Serial.begin(9600);
  Serial.println(F("tic-reader boot v" FW_VERSION));
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
    // ACK/retries : découplage émetteur↔récepteur (cf. incident ben-0001 09/07).
    // Défauts RadioHead = 3 retries (4 TX) + timeout 200 ms → sur un récepteur lent
    // OU sourd, 4 × ~600 ms d'airtime SF9 (~2,4 s de TX cumulée à 120 mA) vident la
    // supercap 0,47 F → brownout/reset. On corrige les DEUX causes :
    //  - setTimeout(600) : l'aller-retour d'ACK réel (airtime ACK ~100 ms + retournement
    //    Pi, pire sous charge type /curve) dépasse souvent 200 ms → on attend assez
    //    longtemps avant de conclure « pas d'ACK » (l'attente se fait en RX ~10 mA,
    //    ce qui espace aussi les bursts TX = récup supercap).
    //  - setRetries(1) : 2 TX max (1+1) au lieu de 4 → borne le pire-cas énergétique.
    // Budget bloquant sendtoWait ≈ 2×(600 TX + 600 ACK) ≈ 2,4 s < WDT 8 s. OK.
    manager.setTimeout(600);
    manager.setRetries(1);
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

  bool vok = isVoltageSufficient();   // ADC seul, plus d'impression (UART TIC laissé ouvert)
  if (!vok) {
    if (bootAcked && curveActive) curveFlush();  // brownout : sauver le batch (STREAMING only ; REGISTERING = simple horloge → on jette, surtout pas de TX supercap basse)
    blinkRGB(30, 0, 0, 15);            // tick rouge fugace = Vcc bas (pas de flash fort : supercap basse)
    return;
  }

  // Discovery au boot (1re itération) : sonde histo↔standard, fixe et persiste le mode.
  if (firstFrame) {
    blinkRGB(30, 12, 0, 60);          // orange att. = discovery histo/std
    uint8_t m = discoverMode();
    if (m == 0xFF) { blinkErr(2); return; }   // compteur muet : on retentera au tour suivant
    ticMode = m;
    persistMode(ticMode);
    TICValues v0;                     // une trame du bon mode pour l'identité (ADCO/ADSC + ISOUSC)
    if (readAndParseTIC(v0, ticMode) && v0.adco[0] != 0) {
      if (sendBootFrame(v0.adco, v0.isousc, v0.pref, contractOf(v0))) {
        bootAcked = true;                    // enregistré → STREAMING
        lastSentIsousc = v0.isousc;
        lastSentPref = v0.pref;
        lastSentNgtfHash = strhash16(contractOf(v0));
      }
    }
    firstFrame = false;
    return;
  }

  {  // --- bloc v : TICValues détruit AVANT le flush différé → ~184 o de pile pour ChaCha ---
  TICValues v;
  if (!readAndParseTIC(v, ticMode)) {
    // Échec lecture : Enedis a peut-être rebasculé le mode → re-discovery après N échecs.
    if (++consecFail >= REDISCOVER_FAILS) {
      if (bootAcked && curveActive) curveFlush();  // STREAMING only (REGISTERING : batch = horloge, jeté)
      uint8_t m = discoverMode();
      if (m != 0xFF) { ticMode = m; persistMode(ticMode); }
      consecFail = 0;
    }
    blinkErr(2);                       // rouge 2× = pas de TIC
    return;
  }
  consecFail = 0;
  blinkRGB(0, 0, 12, 6);               // bleu faible bref = trame TIC lue (heartbeat lecture)

  // Métadonnées contrat (statiques) : ré-émettre la trame d'identité v0x01 si ISOUSC (histo, A)
  // OU PREF (standard, kVA) OU le CONTRAT (NGTF en standard / OPTARIF en histo) change. Un
  // changement de contrat = nouvelle époque tarifaire côté serveur (segmentation des registres).
  // STREAMING uniquement : ré-émettre l'identité v0x01 si ISOUSC/PREF/CONTRAT change en cours de
  // route. En REGISTERING on ne passe PAS par ici (sinon on ré-émettrait à CHAQUE tour puisque
  // lastSent* reste figé tant que pas d'ACK) → le retry boot est géré à la cadence batch plus bas.
  if (bootAcked && v.adco[0] != 0 &&
      ((v.isousc != 0 && v.isousc != lastSentIsousc) ||
       (v.pref   != 0 && v.pref   != lastSentPref) ||
       (contractOf(v)[0] && strhash16(contractOf(v)) != lastSentNgtfHash))) {
    if (sendBootFrame(v.adco, v.isousc, v.pref, contractOf(v))) {
      lastSentIsousc = v.isousc;
      lastSentPref = v.pref;
      lastSentNgtfHash = strhash16(contractOf(v));
    }
  }

  // --- Sélection index + garde, par mode -----------------------------------------------
  uint8_t id; uint32_t value;
  if (ticMode == MODE_STANDARD) {
    // Standard : on capte le point si PAPP (la courbe) ET une horodate valide (pour le dater)
    // sont présents. NTARF/EASF en CARRY-FORWARD (index cumulatif quasi-statique) → un drop
    // de ligne ne jette PAS le point et ne clignote PAS rouge : on saute SILENCIEUSEMENT si
    // PAPP/horodate manquent, ou si NTARF n'a jamais été vu (boot).
    bool tsOk = (v.ts[0] == 'E' || v.ts[0] == 'H' || v.ts[0] == 'e' || v.ts[0] == 'h');
    if (!(v.fields_seen & TIC_SEEN_PAPP) || !tsOk) return;         // skip silencieux (pas de rouge)
    // TRIM : easf_active capté au parse via lastNtarf (reporté). id = ce lastNtarf → paire (id,value) cohérente.
    if (v.easf_active_seen) lastStdIndex = v.easf_active;          // maj carry-forward EASF actif
    uint8_t idNow = lastNtarf;                                    // NTARF ayant choisi easf_active (AVANT maj)
    if (v.ntarf >= 1 && v.ntarf <= 10) lastNtarf = v.ntarf;       // maj carry-forward pour la PROCHAINE trame
    if (idNow < 1 || idNow > 10) return;                          // NTARF pas encore amorcé (1re trame) → skip
    // Cold-start (reboot) : NTARF vu mais EASF actif pas encore capturé (1re trame tronquée /
    // checksum) → lastStdIndex=0. Ne PAS démarrer la courbe sur un index 0 (sinon la keyframe
    // fige 0 pour tout le batch). On saute cette trame → le batch démarrera à la trame suivante
    // avec le vrai index. Coût : au pire 1 point PAPP par reboot (cf. instrumentation INDEX0).
    if (lastStdIndex == 0) return;
    id = idNow;
    value = lastStdIndex;
  } else {
    // TOLÉRANCE front-end marginal : trame histo glitchée (invalide / PTEC inconnu / partielle)
    // → skip SILENCIEUX. Plus de rouge rouge sur 1 glitch : on lit les bonnes trames entre les
    // mauvaises (comme le wired). Perte TOTALE de signal = gérée par consecFail (readAndParseTIC false).
    if (!v.valid) return;                                         // histo : trame invalide
    selectActiveIndex(v, id, value);
    if (id == IDX_UNKNOWN) return;                                // histo : PTEC inconnu (souvent = glitch)
    uint16_t required = TIC_SEEN_PAPP;                            // garde histo : PAPP + index actif
    if (id < sizeof(INDEX_ID_TO_SEEN) / sizeof(INDEX_ID_TO_SEEN[0]))
      required |= INDEX_ID_TO_SEEN[id];
    if ((v.fields_seen & required) != required) return;          // trame partielle
  }

  // Accumulation courbe. Changement d'index actif → coupe le batch et repart sur un keyframe
  // du nouvel index → batch homogène (§17.3). En standard, bascule soutiré↔injection sans
  // flush : le delta zig-zag est signé.
  if (!curveActive) {
    curveStart(v, id, value);          // 1er point d'un batch : pas de flush, aucun risque
  } else if (id != curveIndexId) {
    curveFlushPending = true;          // changement d'index : flush différé + on SAUTE ce point
                                       // (le nouveau batch démarre au tour suivant : perte ~1 pt/chgt tarif, osef)
  } else {
    curveAdd(pappValue(v), v);         // même index → point (peut lever curveFlushPending si plein)
  }
  // Flush périodique → fraîcheur /live ~60 s.
  if (curveActive && curveN > 1 && (millis() - curveT0) >= CURVE_FLUSH_MS)
    curveFlushPending = true;

  // REGISTERING : tant que la trame de boot n'est pas ACQUITTÉE, on n'émet AUCUNE mesure. On
  // retente la boot à la CADENCE BATCH (curveFlushPending = ~40 s / ou périodique 55 s), avec le
  // `v` FRAIS → identité/config toujours à jour, jamais un snapshot périmé. Le batch accumulé n'a
  // servi que d'horloge → on le jette (le récepteur n'est pas confirmé, la courbe serait perdue).
  if (!bootAcked && curveFlushPending) {
    if (sendBootFrame(v.adco, v.isousc, v.pref, contractOf(v))) {
      bootAcked = true;                      // enregistré → STREAMING au prochain point
      lastSentIsousc = v.isousc;
      lastSentPref = v.pref;
      lastSentNgtfHash = strhash16(contractOf(v));
    }
    curveActive = false; curveN = 0;         // jeter le batch-horloge
    curveFlushPending = false;               // → pas de flush courbe différé ci-dessous
  }
  }  // --- fin bloc v : TICValues détruit, pile dégagée ---
  // Flush DIFFÉRÉ (+ flush-on-message) : ici v n'est plus sur la pile → chiffrement au large.
  if (curveFlushPending) { curveFlush(); curveFlushPending = false; }
}
