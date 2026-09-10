// ============================================================
//  Sonic-Expert | sonic_sensor.ino
//
//  역할: 센서 수집 + 릴레이 제어 + LED 상태 표시
//  모든 모드/릴레이 제어는 PC(Python)가 시리얼 명령으로 전달
//  (버튼 없음 — 웹앱에서 전부 제어)
//
//  [핀 배치]
//  A0        : 마이크 MAX4466 OUT
//  A4 (SDA)  : MPU-6050 SDA
//  A5 (SCL)  : MPU-6050 SCL
//  D7        : 릴레이 IN       (활성 LOW: LOW=ON, HIGH=OFF)
//  D10       : LED 파란색      (DETECT 모드 표시)
//  D11       : LED 노란색      (TRAIN 모드 표시)
//
//  [PC→아두이노 명령]
//  RELAY:OFF    릴레이 수동 차단
//  RELAY:ON     릴레이 수동 복구
//  MODE:TRAIN   학습 모드 LED 표시
//  MODE:DETECT  탐지 모드 LED 표시
//
//  [아두이노→PC 데이터]
//  헤더: timestamp_ms,mic,accel_x,accel_y,accel_z,relay
//  상태: STATUS:RELAY_OFF / RELAY_ON / MODE_TRAIN / MODE_DETECT
// ============================================================

#include <Wire.h>

// ── MPU-6050 레지스터 ────────────────────────────────────────
#define MPU_ADDR     0x68
#define PWR_MGMT_1   0x6B
#define ACCEL_XOUT_H 0x3B

// ── 핀 정의 ─────────────────────────────────────────────────
#define RELAY_PIN    7
#define LED_DETECT   10
#define LED_TRAIN    11
#define MIC_PIN      A0

// ── 설정 ────────────────────────────────────────────────────
#define SAMPLE_RATE_MS 10   // 100Hz

// ── 상태 변수 ───────────────────────────────────────────────
bool relayOn = true;
String currentMode = "DETECT";   // "TRAIN" | "DETECT"  (표시용)

unsigned long lastSample = 0;

// ── 릴레이 제어 (수동 명령에만 반응) ─────────────────────────
void relayON() {
  digitalWrite(RELAY_PIN, LOW);
  relayOn = true;
}

void relayOFF() {
  digitalWrite(RELAY_PIN, HIGH);
  relayOn = false;
}

// ── LED 업데이트 (현재 모드 표시) ─────────────────────────────
void updateLED() {
  digitalWrite(LED_DETECT, currentMode == "DETECT" ? HIGH : LOW);
  digitalWrite(LED_TRAIN,  currentMode == "TRAIN"  ? HIGH : LOW);
}

// ────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);

  // 릴레이: 안전하게 OFF 초기화 후 ON
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, HIGH);
  delay(100);
  relayON();

  // LED
  pinMode(LED_DETECT, OUTPUT);
  pinMode(LED_TRAIN,  OUTPUT);
  updateLED();

  // MPU-6050 초기화
  Wire.begin();
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(PWR_MGMT_1);
  Wire.write(0);
  Wire.endTransmission(true);

  // 헤더 전송
  Serial.println("timestamp_ms,mic,accel_x,accel_y,accel_z,relay");
  delay(500);
}

// ────────────────────────────────────────────────────────────
void loop() {

  // ── 1. PC 명령 수신 ──────────────────────────────────────
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();

    if (cmd == "RELAY:OFF") {
      relayOFF();
      Serial.println("STATUS:RELAY_OFF");

    } else if (cmd == "RELAY:ON") {
      relayON();
      Serial.println("STATUS:RELAY_ON");

    } else if (cmd == "MODE:TRAIN") {
      currentMode = "TRAIN";
      updateLED();
      Serial.println("STATUS:MODE_TRAIN");

    } else if (cmd == "MODE:DETECT") {
      currentMode = "DETECT";
      updateLED();
      Serial.println("STATUS:MODE_DETECT");
    }
  }

  // ── 2. LED 점멸 (릴레이 차단 시 현재 모드 LED 점멸) ─────
  if (!relayOn) {
    bool blink = (millis() / 300) % 2 == 0;
    digitalWrite(LED_DETECT, currentMode == "DETECT" ? (blink ? HIGH : LOW) : LOW);
    digitalWrite(LED_TRAIN,  currentMode == "TRAIN"  ? (blink ? HIGH : LOW) : LOW);
  } else {
    updateLED();
  }

  // ── 3. 센서 수집 및 시리얼 전송 (100Hz) ─────────────────
  if (millis() - lastSample >= SAMPLE_RATE_MS) {
    lastSample = millis();

    int     mic = analogRead(MIC_PIN);
    int16_t ax, ay, az;
    readAccel(&ax, &ay, &az);

    Serial.print(millis());         Serial.print(',');
    Serial.print(mic);              Serial.print(',');
    Serial.print(ax);               Serial.print(',');
    Serial.print(ay);               Serial.print(',');
    Serial.print(az);               Serial.print(',');
    Serial.println(relayOn ? 1 : 0);
  }
}

// ── MPU-6050 가속도 읽기 ────────────────────────────────────
void readAccel(int16_t* ax, int16_t* ay, int16_t* az) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(ACCEL_XOUT_H);
  Wire.endTransmission(false);
  Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)6, (uint8_t)true);
  *ax = (Wire.read() << 8) | Wire.read();
  *ay = (Wire.read() << 8) | Wire.read();
  *az = (Wire.read() << 8) | Wire.read();
}
