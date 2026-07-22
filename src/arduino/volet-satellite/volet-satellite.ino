// bensat.ino — satellite VOLET (1-3). Corps=[volet,action]. Impulsion NON-BLOQUANTE (millis).
// Clé K_DEVICE lue en EEPROM (0x00, 32 o) — provisionnée par avrdude (dérivée par-device).
// LED : boot=bleu bref. Reception valide = VERT -> N blinks (couleur action)=n volet -> ordre.
#include <SPI.h>
#include <RH_RF95.h>
#include <RHReliableDatagram.h>
#include <Crypto.h>
#include <SHA256.h>
#include <EEPROM.h>
#include <string.h>
#define RFM95_CS 10
#define RFM95_INT 2
#define RFM95_RST 9
#define MY_ADDRESS 0x2a          // défaut si EEPROM non provisionnée (0xff/0x00)
#define LORA_ADDR_ADDR 0x21      // adresse LoRa provisionnée en EEPROM (1 seul binaire pour tous les devices)
#define RF95_FREQ 868.0
#define RF95_TXPOWER 20              // alim externe stable → 20 dBm : l'ACK revient jusqu'à la centrale
#define RGB_R 5
#define RGB_G 3
#define RGB_B 6
#define PULSE_MS 500             // brève impulsion (tap) : déclenche le moteur qui s'auto-maintient
#define NVOLETS 3
// index = volet-1.  V1: ouvre D7(orange)/ferme D4(gris) | V2: ouvre D8(blanc)/ferme A2(vert)
//                   V3: ouvre A0(violet)/ferme A1(jaune)
static const uint8_t OUVRE[NVOLETS] = {7, 8, A0};
static const uint8_t FERME[NVOLETS] = {4, A2, A1};
RH_RF95 driver(RFM95_CS, RFM95_INT);
RHReliableDatagram manager(driver, MY_ADDRESS);
SHA256 sha256;
static uint8_t K_DEVICE[32];         // lue depuis l'EEPROM au boot (agnostique à la clé → re-key sans reflash)
uint32_t last_hi=0,last_lo=0; bool have_last=false;
uint8_t active_pin=0; unsigned long pulse_end=0;   // impulsion en cours (non-bloquante)
static inline uint32_t rotl32(uint32_t x,int n){return (x<<n)|(x>>(32-n));}
#define CHACHA_QR(a,b,c,d) a+=b;d^=a;d=rotl32(d,16);c+=d;b^=c;b=rotl32(b,12);a+=b;d^=a;d=rotl32(d,8);c+=d;b^=c;b=rotl32(b,7);
#define CHACHA_LE32(p) ((uint32_t)(p)[0]|((uint32_t)(p)[1]<<8)|((uint32_t)(p)[2]<<16)|((uint32_t)(p)[3]<<24))
static void chacha20_xor(const uint8_t* key,const uint8_t* nonce,uint8_t* buf,uint16_t len){
  uint32_t x[16],counter=0;
  for(uint16_t off=0;off<len;off+=64){
    x[0]=0x61707865UL;x[1]=0x3320646eUL;x[2]=0x79622d32UL;x[3]=0x6b206574UL;
    for(uint8_t i=0;i<8;i++)x[4+i]=CHACHA_LE32(key+4*i);
    x[12]=counter;for(uint8_t i=0;i<3;i++)x[13+i]=CHACHA_LE32(nonce+4*i);
    for(uint8_t r=0;r<10;r++){CHACHA_QR(x[0],x[4],x[8],x[12])CHACHA_QR(x[1],x[5],x[9],x[13])CHACHA_QR(x[2],x[6],x[10],x[14])CHACHA_QR(x[3],x[7],x[11],x[15])CHACHA_QR(x[0],x[5],x[10],x[15])CHACHA_QR(x[1],x[6],x[11],x[12])CHACHA_QR(x[2],x[7],x[8],x[13])CHACHA_QR(x[3],x[4],x[9],x[14])}
    for(uint8_t i=0;i<16;i++){uint32_t si=i<4?(i==0?0x61707865UL:i==1?0x3320646eUL:i==2?0x79622d32UL:0x6b206574UL):i<12?CHACHA_LE32(key+4*(i-4)):i==12?counter:CHACHA_LE32(nonce+4*(i-13));uint32_t v=x[i]+si;for(uint8_t j=0;j<4;j++){uint16_t pp=off+4*i+j;if(pp<len)buf[pp]^=(uint8_t)(v>>(8*j));}}
    counter++;
  }
}
static void setRGB(uint8_t r,uint8_t g,uint8_t b){analogWrite(RGB_R,r);analogWrite(RGB_G,g);analogWrite(RGB_B,b);}
static void blinkN(uint8_t n,uint8_t r,uint8_t g,uint8_t b){for(uint8_t i=0;i<n;i++){setRGB(r,g,b);delay(220);setRGB(0,0,0);delay(220);}}
static void doVolet(uint8_t volet,uint8_t action){
  if(volet<1||volet>NVOLETS||(action!=1&&action!=2)){
    Serial.print(F("  invalide v="));Serial.print(volet);Serial.print(F(" a="));Serial.println(action);
    blinkN(2,80,40,0); return;
  }
  uint8_t pin=(action==1)?OUVRE[volet-1]:FERME[volet-1];
  uint8_t r,g,b; if(action==1){r=255;g=180;b=0;}else{r=0;g=0;b=255;}
  Serial.print(F("  VOLET "));Serial.print(volet);Serial.println(action==1?F(" OUVRE"):F(" FERME"));
  if(active_pin){ digitalWrite(active_pin,LOW); active_pin=0; }   // termine une impulsion en cours
  setRGB(r,g,b);                                                  // couleur d'action FIXE (pas de blinks bloquants)
  digitalWrite(pin,HIGH); active_pin=pin; pulse_end=millis()+PULSE_MS;   // NON-BLOQUANT (fin dans loop)
}
static void handleFrame(uint8_t* frame,uint8_t flen){
  if(flen<17){Serial.println(F("  courte"));return;}
  uint8_t k_mac[32];sha256.resetHMAC(K_DEVICE,32);sha256.update((const uint8_t*)"ben-lora-mac-dn",15);sha256.finalizeHMAC(K_DEVICE,32,k_mac,32);
  uint8_t mac[32];sha256.resetHMAC(k_mac,32);sha256.update(frame,flen-8);sha256.finalizeHMAC(k_mac,32,mac,32);
  if(memcmp(mac,frame+flen-8,8)!=0){Serial.println(F("  MAC INVALIDE"));return;}
  uint32_t chi=(uint32_t)frame[1]|((uint32_t)frame[2]<<8)|((uint32_t)frame[3]<<16);
  uint32_t clo=(uint32_t)frame[4]|((uint32_t)frame[5]<<8)|((uint32_t)frame[6]<<16);
  if(have_last&&!(chi>last_hi||(chi==last_hi&&clo>last_lo))){Serial.println(F("  REJEU"));return;}
  last_hi=chi;last_lo=clo;have_last=true;
  if(frame[0]&0x80){
    uint8_t k_enc[32];sha256.resetHMAC(K_DEVICE,32);sha256.update((const uint8_t*)"ben-lora-enc-dn",15);sha256.finalizeHMAC(K_DEVICE,32,k_enc,32);
    uint8_t nonce[12];nonce[0]=frame[1];nonce[1]=frame[2];nonce[2]=frame[3];nonce[3]=frame[4];nonce[4]=frame[5];nonce[5]=frame[6];for(uint8_t i=6;i<12;i++)nonce[i]=0;
    chacha20_xor(k_enc,nonce,frame+7,flen-7-8);
  }
  Serial.println(F("RX OK"));
  setRGB(0,255,0); delay(80);                               // flash vert bref = reçu (bloque ~80 ms seulement)
  doVolet(frame[7],frame[8]);
}
void setup(){
  pinMode(RGB_R,OUTPUT);pinMode(RGB_G,OUTPUT);pinMode(RGB_B,OUTPUT);
  for(uint8_t i=0;i<NVOLETS;i++){pinMode(OUVRE[i],OUTPUT);digitalWrite(OUVRE[i],LOW);pinMode(FERME[i],OUTPUT);digitalWrite(FERME[i],LOW);}
  setRGB(0,0,0);
  for(uint8_t i=0;i<32;i++) K_DEVICE[i]=EEPROM.read(i);       // clé provisionnée en EEPROM (0x00)
  Serial.begin(9600);Serial.println(F("=== satellite VOLET 0x2a @868 (v1-3, EEPROM-key, non-bloquant) ==="));
  pinMode(RFM95_RST,OUTPUT);digitalWrite(RFM95_RST,HIGH);delay(10);digitalWrite(RFM95_RST,LOW);delay(10);digitalWrite(RFM95_RST,HIGH);delay(10);
  if(manager.init()){
    { uint8_t a=EEPROM.read(LORA_ADDR_ADDR); manager.setThisAddress((a==0xFF||a==0x00)?MY_ADDRESS:a); }  // adresse EEPROM (override défaut)
    driver.setFrequency(RF95_FREQ);driver.setTxPower(RF95_TXPOWER,false);
    static const RH_RF95::ModemConfig SF9={0x72,0x94,0x00};driver.setModemRegisters(&SF9);
    Serial.println(F("LoRa OK @868"));setRGB(0,0,80);delay(300);setRGB(0,0,0);   // boot = bleu bref
  } else {Serial.println(F("LoRa FAIL"));blinkN(3,255,0,0);}
}
void loop(){
  if(manager.available()){
    uint8_t buf[RH_RF95_MAX_MESSAGE_LEN];uint8_t len=sizeof(buf);uint8_t from;
    if(manager.recvfromAck(buf,&len,&from)) handleFrame(buf,len);
  }
  // fin d'impulsion NON-BLOQUANTE : le satellite reste à l'écoute pendant l'impulsion
  if(active_pin && (long)(millis()-pulse_end)>=0){
    digitalWrite(active_pin,LOW); active_pin=0; setRGB(0,0,0);
  }
}
