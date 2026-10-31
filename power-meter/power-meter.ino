/*
 * ==================================================================================
 * PROYEK: SMART POWER METER & TINYML PREDICTOR (JAIRU)
 * REVISI: FULL CODE LCD 16x2 I2C DENGAN CUSTOM CHAR (ENTER & BACKSPACE),
 *         ANTI-FLICKER CACHE, MULTITASKING FREERTOS KEYPAD, & METROLOGI PRESISI
 *         + FITUR KONTROL RELAY PIN 4 VIA MQTT SUBSCRIBE
 *         + FIX: RELAY TIDAK NYALA SESAAT SAAT BOOT (RELAY_OFF PALING AWAL)
 *
 * CATATAN HARDWARE PENTING:
 *   GPIO 4 pada ESP32 default-nya input + pull-down lemah saat reset/bootloader.
 *   Untuk modul relay ACTIVE-LOW, ini membuat relay nyala sesaat sebelum setup().
 *   Solusi: pasang resistor pull-up 4.7k-10k dari GPIO 4 ke 3V3 (level OFF = HIGH).
 *   Software tidak bisa mengatasi jendela waktu sebelum setup() berjalan.
 * ==================================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <Keypad.h>
#include <time.h>
#include <math.h>
#include "FS.h"
#include "SD.h"
#include "SPI.h"
#include "tinyml_inference.h"

// ================= KONTROL RELAY (PIN 4) =================
#define PIN_RELAY 4
#define RELAY_ON LOW
#define RELAY_OFF HIGH
bool is_relay_on = false;
const char* FILE_RELAY = "/relay_state.txt";

// ================= LCD 16x2 I2C (SDA: GPIO 21, SCL: GPIO 22) =================
#define LCD_I2C_ADDR 0x27
#define LCD_COLS 16
#define LCD_ROWS 2
LiquidCrystal_I2C lcd(LCD_I2C_ADDR, LCD_COLS, LCD_ROWS);

// Definisi Custom Character (5x8 Pixel)
byte charEnter[8] = {
  0b00001,
  0b00001,
  0b00101,
  0b01001,
  0b11111,
  0b01000,
  0b00100,
  0b00000
};

byte charBackspace[8] = {
  0b00000,
  0b00100,
  0b01100,
  0b11111,
  0b01100,
  0b00100,
  0b00000,
  0b00000
};

// Cache isi layar LCD agar tidak berkedip
char lcdCache[LCD_ROWS][LCD_COLS + 1];

// Pengaturan rotasi halaman informasi bergantian
#define LCD_PAGE_COUNT 6
const unsigned long LCD_PAGE_INTERVAL_MS = 6000;  // Rotasi tiap 6 detik
uint8_t lcd_page = 0;
unsigned long prevPageSwitch = 0;

// ================= MICRO SD CARD (VSPI: CS=5, MOSI=23, MISO=19, SCK=18) =================
#define SD_CS_PIN 5
bool sdCardAvailable = false;
const char* FILE_SALDO = "/saldo.txt";
const char* FILE_SALDO_TMP = "/saldo.tmp";
const char* FILE_LOG = "/energy_log.csv";
const char* FILE_BUFFER = "/buffer_history.dat";
const char* FILE_BUFF_TMP = "/buffer_history.tmp";

// ================= KEYPAD MATRIX 4x4 (GPIO 13, 12, 14, 27, 26, 25, 33, 32) =================
const byte ROWS = 4;
const byte COLS = 4;
char keys[ROWS][COLS] = {
  { '1', '2', '3', 'A' },
  { '4', '5', '6', 'B' },
  { '7', '8', '9', 'C' },
  { '*', '0', '#', 'D' }
};

byte rowPins[ROWS] = { 13, 12, 14, 27 };
byte colPins[COLS] = { 26, 25, 33, 32 };
Keypad keypad = Keypad(makeKeymap(keys), rowPins, colPins, ROWS, COLS);

// Antrian keypad FreeRTOS Core 0
QueueHandle_t keyQueue = nullptr;

// ================= KONFIGURASI WIFI & HIVEMQ =================
const char* WIFI_SSID = "Ridho Ruijie";
const char* WIFI_PASSWORD = "ridhosetiawanskom24";
const char* MQTT_BROKER = "broker.hivemq.com";
const int MQTT_PORT = 1883;
const char* MQTT_TOPIC_PUB = "jairu/powermeter/telemetry";
const char* MQTT_TOPIC_SUB = "jairu/powermeter/relay/control";

const char* NTP_SERVER = "pool.ntp.org";
const long GMT_OFFSET_SEC = 8 * 3600;  // WITA (UTC+8)
const int DAYLIGHT_OFFSET_SEC = 0;

String mqttClientId = "ESP32_JAIRU_ELEVATE2026";
WiFiClient espClient;
PubSubClient mqttClient(espClient);

// ================= PIN SENSOR ADC1 =================
#define PIN_VOLTAGE 35
#define PIN_CURRENT 34
#define SAMPLING_PERIOD_MS 200
#define MAX_SAMPLES 400

const float V_CAL = 5581.51f;
const float I_CAL = 111.53f;
const int PHASE_SAMPLE_SHIFT = -1;
const float FINE_PHASE_TUNE = 0.00f;
const float NO_LOAD_CURRENT_THRESHOLD = 0.015f;
const float NO_LOAD_POWER_THRESHOLD = 0.15f;

float v_rms = 0.0f, i_rms = 0.0f, p_active = 0.0f, p_apparent = 0.0f, power_factor = 0.0f;
float v_rms_stable = 0.0f, i_rms_stable = 0.0f, p_active_stable = 0.0f, p_apparent_stable = 0.0f;

int raw_v_arr[MAX_SAMPLES];
int raw_i_arr[MAX_SAMPLES];

// ================= SALDO & BUFFER PREDIKSI =================
double saldo_token_kwh = 20.0;
double last_saved_saldo = 20.0;
double current_interval_kwh = 0.0;
float token_runway_hours = 999.0f;

#define BUFFER_SIZE 96
float energy_buffer_15m[BUFFER_SIZE] = { 0.0f };
int buffer_idx = 0;
float predicted_q[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
bool is_ml_warmed_up = false;

// ================= MODE INPUT & NOTIFIKASI =================
String input_kwh_str = "";
String key_sequence = "";
bool is_input_mode = false;
bool waiting_format_confirm = false;

unsigned long popup_end_time = 0;
String popup_line1 = "";
String popup_line2 = "";

// Timer Non-Blocking
unsigned long prevSecTime = 0;
unsigned long prevMqttSendTime = 0;
unsigned long prevSdLogTime = 0;
unsigned long prevInterval15m = 0;
unsigned long prevWifiRetry = 0;
unsigned long prevMqttRetry = 0;
unsigned long prevLcdRefresh = 0;
unsigned long prevSaldoPeriodicSave = 0;
const unsigned long INTERVAL_15M = 900000;

// ================= PROTOTIPE FUNGSI =================
void relayHardwareOff();
void setRelay(bool state);
void mqttCallback(char* topic, byte* payload, unsigned int length);
void handleKeypad();
void processKey(char key);
void keypadTask(void* param);
bool getTimeNonBlocking(struct tm* ti);
void updateLCD();
void renderInputScreen();
void showPopup(const String& l1, const String& l2, unsigned long duration);
bool popupActive();
void exitInputMode();
void lcdInvalidate();
void lcdPrintLine(uint8_t row, const char* text);
void lcdPrintLine(uint8_t row, const String& text);
void calculateACMetricsPresisi();
void build_features(float* features);
void handleWiFi();
void handleMQTT();
void publishTelemetry();
void warmupTinyMLWithCurrentLoad();
void initSDCard();
bool recoverSDCard();
bool loadRelayStateFromSD();
void saveRelayStateToSD(bool state);
void saveSaldoToSD(double saldo);
void logTelemetryToSD();
void saveBufferToSD();
bool loadBufferFromSD();
void wipeAllSDCardFiles();

// ================= PAKSA RELAY OFF (DIPANGGIL PALING AWAL) =================
// Level OFF ditulis sebelum dan sesudah pinMode agar aman di semua versi
// core ESP32 Arduino. Tidak menyentuh SD sama sekali.
void relayHardwareOff() {
  digitalWrite(PIN_RELAY, RELAY_OFF);
  pinMode(PIN_RELAY, OUTPUT);
  digitalWrite(PIN_RELAY, RELAY_OFF);
  is_relay_on = false;
}

// ================= SETUP =================
void setup() {
  // 1) PALING PERTAMA: pastikan relay OFF, sebelum Serial/LCD/SD apa pun.
  relayHardwareOff();

  Serial.begin(115200);

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  Wire.begin(21, 22);
  lcd.init();
  lcd.backlight();

  // Daftarkan Custom Character ke CGRAM LCD (Slot 1 & Slot 2)
  lcd.createChar(1, charEnter);      // \x01 = Simbol Enter
  lcd.createChar(2, charBackspace);  // \x02 = Simbol Backspace

  lcd.clear();
  lcdInvalidate();
  lcdPrintLine(0, "BOOTING SYSTEM..");
  lcdPrintLine(1, "Mohon tunggu...");

  initSDCard();

  // 2) Baca status relay terakhir dari MicroSD (relay masih OFF sampai baris ini)
  bool lastState = loadRelayStateFromSD();

  // Proteksi: jika saldo 0 kWh, relay harus tetap OFF
  if (saldo_token_kwh <= 0.0) {
    lastState = false;
  }

  // 3) Terapkan status tanpa menulis ulang file di MicroSD saat booting.
  //    Jika terakhir OFF, relay tidak disentuh sama sekali (tetap OFF).
  if (lastState) {
    is_relay_on = true;
    digitalWrite(PIN_RELAY, RELAY_ON);
  } else {
    is_relay_on = false;
    digitalWrite(PIN_RELAY, RELAY_OFF);
  }
  Serial.printf("[RELAY] Inisialisasi Boot: %s\n", lastState ? "ON" : "OFF");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, NTP_SERVER);

  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setCallback(mqttCallback);
  mqttClient.setBufferSize(512);
  mqttClient.setSocketTimeout(2);

  calculateACMetricsPresisi();
  v_rms_stable = v_rms;
  i_rms_stable = i_rms;
  p_active_stable = p_active;
  p_apparent_stable = p_apparent;

  if (loadBufferFromSD()) {
    is_ml_warmed_up = true;
    float features[8];
    build_features(features);
    tinyml_predict(features, predicted_q);
  } else {
    warmupTinyMLWithCurrentLoad();
  }

  prevPageSwitch = millis();

  // Inisialisasi task pemindai keypad di Core 0 (prioritas 2)
  keyQueue = xQueueCreate(16, sizeof(char));
  xTaskCreatePinnedToCore(keypadTask, "keypad", 2048, NULL, 2, NULL, 0);
}

// ================= LOOP UTAMA =================
void loop() {
  unsigned long currentMillis = millis();

  handleKeypad();
  handleWiFi();
  handleMQTT();

  // 1. Metrologi & Riemann Sum Pemotongan Saldo (Tiap 1 Detik)
  if (currentMillis - prevSecTime >= 1000) {
    double dt_s = (double)(currentMillis - prevSecTime) / 1000.0;
    prevSecTime = currentMillis;

    calculateACMetricsPresisi();

    if (v_rms_stable == 0.0f) {
      v_rms_stable = v_rms;
    } else if (v_rms > 30.0f) {
      v_rms_stable = (0.85f * v_rms_stable) + (0.15f * v_rms);
    } else {
      v_rms_stable = 0.0f;
    }

    if (i_rms == 0.0f) {
      i_rms_stable = 0.0f;
      p_active_stable = 0.0f;
      p_apparent_stable = 0.0f;
      power_factor = 0.0f;
    } else {
      if (i_rms_stable == 0.0f) {
        i_rms_stable = i_rms;
        p_active_stable = p_active;
        p_apparent_stable = p_apparent;
      }
      i_rms_stable = (0.75f * i_rms_stable) + (0.25f * i_rms);
      p_active_stable = (0.75f * p_active_stable) + (0.25f * p_active);
      p_apparent_stable = (0.75f * p_apparent_stable) + (0.25f * p_apparent);

      if (p_apparent_stable > 0.5f && i_rms_stable > NO_LOAD_CURRENT_THRESHOLD) {
        float pf_calc = p_active_stable / p_apparent_stable;
        if (pf_calc > 1.0f) pf_calc = 1.0f;
        power_factor = pf_calc;
      } else {
        power_factor = 0.0f;
      }
    }

    if (!is_ml_warmed_up && p_active_stable > 0.1f) {
      warmupTinyMLWithCurrentLoad();
    }

    double delta_kwh = ((double)p_active_stable * dt_s) / 3600000.0;
    saldo_token_kwh -= delta_kwh;
    if (saldo_token_kwh < 0.0) saldo_token_kwh = 0.0;
    current_interval_kwh += delta_kwh;

    // Proteksi Saldo: Putus relay jika token habis
    if (saldo_token_kwh <= 0.0 && is_relay_on) {
      setRelay(false);
    }

    bool delta_threshold_reached = (fabs(last_saved_saldo - saldo_token_kwh) >= 0.01);
    bool periodic_save_due = (currentMillis - prevSaldoPeriodicSave >= 300000 && delta_kwh > 0.0);

    if (delta_threshold_reached || periodic_save_due) {
      prevSaldoPeriodicSave = currentMillis;
      saveSaldoToSD(saldo_token_kwh);
    }

    float avg_kw = (predicted_q[0] > 0.01f) ? (predicted_q[0] / 6.0f) : (p_active_stable / 1000.0f);
    if (avg_kw > 0.005f) {
      token_runway_hours = (float)(saldo_token_kwh / avg_kw);
    } else {
      token_runway_hours = 999.0f;
    }
  }

  // 2. Refresh Layar LCD (Tiap 200 ms)
  if (currentMillis - prevLcdRefresh >= 200) {
    prevLcdRefresh = currentMillis;
    if (is_input_mode) {
      renderInputScreen();
    } else {
      updateLCD();
    }
  }

  // 3. Pencatatan Log MicroSD (30 Detik)
  if (currentMillis - prevSdLogTime >= 30000) {
    prevSdLogTime = currentMillis;
    logTelemetryToSD();
  }

  // 4. Publikasi MQTT (Tiap 2 Detik)
  if (currentMillis - prevMqttSendTime >= 2000) {
    prevMqttSendTime = currentMillis;
    publishTelemetry();
  }

  // 5. Inferensi TinyML 15-Menitan
  if (currentMillis - prevInterval15m >= INTERVAL_15M) {
    prevInterval15m = currentMillis;

    buffer_idx = (buffer_idx + 1) % BUFFER_SIZE;
    energy_buffer_15m[buffer_idx] = (float)current_interval_kwh;
    current_interval_kwh = 0.0;

    saveBufferToSD();

    float features[8];
    build_features(features);
    tinyml_predict(features, predicted_q);

    logTelemetryToSD();
  }
}

// ================= STATUS RELAY TERSIMPAN DI MICROSD =================
bool loadRelayStateFromSD() {
  // FIX: default OFF (aman) jika SD tidak ada / file belum ada / isi tidak valid.
  if (!sdCardAvailable) return false;

  if (SD.exists(FILE_RELAY)) {
    File f = SD.open(FILE_RELAY, FILE_READ);
    if (f) {
      String stateStr = f.readStringUntil('\n');
      stateStr.trim();
      f.close();
      if (stateStr == "1") {
        return true;  // Kondisi terakhir ON
      }
      return false;  // "0" atau isi tidak dikenali -> OFF
    }
  }
  return false;  // Berkas belum ada -> OFF
}

void saveRelayStateToSD(bool state) {
  if (!sdCardAvailable) return;

  if (SD.exists(FILE_RELAY)) {
    SD.remove(FILE_RELAY);  // Hapus dulu agar bersih
  }
  File f = SD.open(FILE_RELAY, FILE_WRITE);
  if (f) {
    f.println(state ? "1" : "0");
    f.flush();
    f.close();
  }
}

// ================= KONTROL STATUS RELAY =================
void setRelay(bool state) {
  is_relay_on = state;
  digitalWrite(PIN_RELAY, state ? RELAY_ON : RELAY_OFF);

  // Simpan ke SD card agar status tidak hilang saat reboot
  saveRelayStateToSD(state);

  Serial.printf("[RELAY] Status: %s (Tersimpan di SD)\n", state ? "ON" : "OFF");
}

// ================= CALLBACK MQTT (SUBSCRIBE) =================
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String message = "";
  for (unsigned int i = 0; i < length; i++) {
    message += (char)payload[i];
  }
  message.trim();
  message.toUpperCase();

  Serial.printf("[MQTT SUB] Topik: %s | Pesan: %s\n", topic, message.c_str());

  if (String(topic) == MQTT_TOPIC_SUB) {
    if (message == "ON" || message == "1") {
      if (saldo_token_kwh > 0.0) {
        setRelay(true);
      }
    } else if (message == "OFF" || message == "0") {
      setRelay(false);
    }
  }
}

// ================= SISTEM PENYIMPANAN MICROSD =================
void initSDCard() {
  SPI.begin(18, 19, 23, SD_CS_PIN);
  if (!SD.begin(SD_CS_PIN, SPI, 4000000)) {
    Serial.println(F("[SD] Gagal mount! Periksa perkabelan SPI"));
    sdCardAvailable = false;
    return;
  }
  if (SD.cardType() == CARD_NONE) {
    Serial.println(F("[SD] Kartu tidak terpasang."));
    sdCardAvailable = false;
    return;
  }

  sdCardAvailable = true;
  Serial.println(F("[SD] MicroSD Card siap 24/7!"));

  if (!SD.exists(FILE_SALDO) && SD.exists(FILE_SALDO_TMP)) {
    SD.rename(FILE_SALDO_TMP, FILE_SALDO);
  }

  if (SD.exists(FILE_SALDO)) {
    File f = SD.open(FILE_SALDO, FILE_READ);
    if (f) {
      String content = f.readStringUntil('\n');
      content.trim();
      if (content.length() > 0) {
        saldo_token_kwh = content.toDouble();
        last_saved_saldo = saldo_token_kwh;
        Serial.printf("[SD] Saldo aktif: %.6f kWh\n", saldo_token_kwh);
      }
      f.close();
    }
  } else {
    saveSaldoToSD(saldo_token_kwh);
  }

  if (!SD.exists(FILE_LOG)) {
    File logFile = SD.open(FILE_LOG, FILE_WRITE);
    if (logFile) {
      logFile.println("uptime_s,voltage,current,p_active,p_apparent,pf,saldo_kwh,runway_h,q1,q2,q3,q4");
      logFile.flush();
      logFile.close();
    }
  }
}

bool recoverSDCard() {
  SD.end();
  delay(50);
  if (SD.begin(SD_CS_PIN, SPI, 4000000)) {
    if (SD.cardType() != CARD_NONE) {
      sdCardAvailable = true;
      return true;
    }
  }
  sdCardAvailable = false;
  return false;
}

void saveSaldoToSD(double saldo) {
  saldo_token_kwh = saldo;
  last_saved_saldo = saldo;

  if (!sdCardAvailable && !recoverSDCard()) return;

  File f = SD.open(FILE_SALDO_TMP, FILE_WRITE);
  if (!f && recoverSDCard()) {
    f = SD.open(FILE_SALDO_TMP, FILE_WRITE);
  }

  if (f) {
    f.printf("%.6f\n", saldo);
    f.flush();
    f.close();

    if (SD.exists(FILE_SALDO)) {
      SD.remove(FILE_SALDO);
    }
    SD.rename(FILE_SALDO_TMP, FILE_SALDO);
  }
}

void saveBufferToSD() {
  if (!sdCardAvailable && !recoverSDCard()) return;

  File f = SD.open(FILE_BUFF_TMP, FILE_WRITE);
  if (!f && recoverSDCard()) {
    f = SD.open(FILE_BUFF_TMP, FILE_WRITE);
  }

  if (f) {
    f.write((uint8_t*)&buffer_idx, sizeof(buffer_idx));
    f.write((uint8_t*)energy_buffer_15m, sizeof(energy_buffer_15m));
    f.flush();
    f.close();

    if (SD.exists(FILE_BUFFER)) {
      SD.remove(FILE_BUFFER);
    }
    SD.rename(FILE_BUFF_TMP, FILE_BUFFER);
  }
}

bool loadBufferFromSD() {
  if (!sdCardAvailable) return false;

  if (!SD.exists(FILE_BUFFER) && SD.exists(FILE_BUFF_TMP)) {
    SD.rename(FILE_BUFF_TMP, FILE_BUFFER);
  }

  if (!SD.exists(FILE_BUFFER)) return false;

  File f = SD.open(FILE_BUFFER, FILE_READ);
  if (f) {
    size_t expectedSize = sizeof(buffer_idx) + sizeof(energy_buffer_15m);
    if (f.size() == expectedSize) {
      f.read((uint8_t*)&buffer_idx, sizeof(buffer_idx));
      f.read((uint8_t*)energy_buffer_15m, sizeof(energy_buffer_15m));
      f.close();
      return true;
    }
    f.close();
  }
  return false;
}

void wipeAllSDCardFiles() {
  if (!sdCardAvailable) return;

  if (SD.exists(FILE_SALDO)) SD.remove(FILE_SALDO);
  if (SD.exists(FILE_SALDO_TMP)) SD.remove(FILE_SALDO_TMP);
  if (SD.exists(FILE_LOG)) SD.remove(FILE_LOG);
  if (SD.exists(FILE_BUFFER)) SD.remove(FILE_BUFFER);
  if (SD.exists(FILE_BUFF_TMP)) SD.remove(FILE_BUFF_TMP);

  saldo_token_kwh = 0.0;
  last_saved_saldo = 0.0;
  for (int i = 0; i < BUFFER_SIZE; i++) {
    energy_buffer_15m[i] = 0.0f;
  }

  saveSaldoToSD(0.0);

  // Saldo 0 -> relay harus OFF dan statusnya ikut tersimpan
  if (is_relay_on) {
    setRelay(false);
  }

  File logFile = SD.open(FILE_LOG, FILE_WRITE);
  if (logFile) {
    logFile.println("uptime_s,voltage,current,p_active,p_apparent,pf,saldo_kwh,runway_h,q1,q2,q3,q4");
    logFile.flush();
    logFile.close();
  }
}

void logTelemetryToSD() {
  if (!sdCardAvailable && !recoverSDCard()) return;

  File logFile = SD.open(FILE_LOG, FILE_APPEND);
  if (!logFile && recoverSDCard()) {
    logFile = SD.open(FILE_LOG, FILE_APPEND);
  }

  if (logFile) {
    logFile.printf("%lu,%.2f,%.3f,%.2f,%.2f,%.2f,%.6f,%.2f,%.3f,%.3f,%.3f,%.3f\n",
                   millis() / 1000,
                   v_rms_stable, i_rms_stable, p_active_stable, p_apparent_stable, power_factor * 100.0f,
                   saldo_token_kwh, token_runway_hours,
                   predicted_q[0], predicted_q[1], predicted_q[2], predicted_q[3]);
    logFile.flush();
    logFile.close();
  }
}

// ================= DRIVER LCD 16x2 (DENGAN CACHE ANTI-KEDIP) =================
void lcdInvalidate() {
  memset(lcdCache, 0, sizeof(lcdCache));
}

void lcdPrintLine(uint8_t row, const char* text) {
  if (row >= LCD_ROWS) return;

  char buf[LCD_COLS + 1];
  snprintf(buf, sizeof(buf), "%-16.16s", text);

  if (strcmp(buf, lcdCache[row]) != 0) {
    lcd.setCursor(0, row);
    lcd.print(buf);
    strcpy(lcdCache[row], buf);
  }
}

void lcdPrintLine(uint8_t row, const String& text) {
  lcdPrintLine(row, text.c_str());
}

bool popupActive() {
  return (long)(popup_end_time - millis()) > 0;
}

void exitInputMode() {
  is_input_mode = false;
  waiting_format_confirm = false;
  key_sequence = "";
  input_kwh_str = "";

  lcd_page = 0;
  prevPageSwitch = millis();
  lcdInvalidate();
}

// ================= PEMINDAI KEYPAD (TASK TERPISAH, CORE 0) =================
void keypadTask(void* param) {
  keypad.setDebounceTime(10);
  for (;;) {
    char k = keypad.getKey();
    if (k) {
      xQueueSend(keyQueue, &k, 0);
    }
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

bool getTimeNonBlocking(struct tm* ti) {
  time_t t = time(nullptr);
  if (t < 1700000000) return false;
  localtime_r(&t, ti);
  return true;
}

// ================= LOGIKA KEYPAD =================
void handleKeypad() {
  char key;
  while (xQueueReceive(keyQueue, &key, 0) == pdTRUE) {
    processKey(key);
  }
}

void processKey(char key) {
  if (waiting_format_confirm) {
    if (key == 'D') {
      wipeAllSDCardFiles();
      showPopup(F("SD BERHASIL"), F("DIRESET TOTAL!"), 1500);
    } else {
      showPopup(F("FORMAT SD"), F("DIBATALKAN"), 1000);
    }
    exitInputMode();
    return;
  }

  if (key == 'A') {
    is_input_mode = true;
    input_kwh_str = "";
    key_sequence = "A";
    renderInputScreen();
    return;
  } else if (key == 'B') {
    is_input_mode = true;
    if (key_sequence == "A") key_sequence = "AB";
    else key_sequence = "B";
    renderInputScreen();
    return;
  } else if (key == 'C') {
    is_input_mode = true;
    if (key_sequence == "AB") {
      key_sequence = "ABC";
      waiting_format_confirm = true;
    } else {
      key_sequence = "C";
    }
    renderInputScreen();
    return;
  }

  if (key == 'D') {
    if (input_kwh_str.length() > 0) {
      double added_kwh = input_kwh_str.toDouble();
      saveSaldoToSD(saldo_token_kwh + added_kwh);
      if (!is_relay_on && saldo_token_kwh > 0.0) {
        setRelay(true);
      }
      showPopup(F("TOP-UP BERHASIL!"), "+" + String((float)added_kwh, 2) + " kWh", 1500);
      exitInputMode();
    } else if (key_sequence == "AB") {
      saveSaldoToSD(0.0);
      if (is_relay_on) {
        setRelay(false);
      }
      showPopup(F("SALDO RESET"), F("MENJADI 0.00 kWh"), 1500);
      exitInputMode();
    } else {
      exitInputMode();
      updateLCD();
    }
    return;
  }

  if (key >= '0' && key <= '9') {
    is_input_mode = true;
    key_sequence = "";
    if (input_kwh_str.length() < 7) {
      input_kwh_str += key;
    }
    renderInputScreen();
    return;
  }

  if (key == '#') {
    is_input_mode = true;
    key_sequence = "";
    if (input_kwh_str.indexOf('.') == -1) {
      if (input_kwh_str.length() == 0) {
        input_kwh_str += "0.";
      } else if (input_kwh_str.length() < 7) {
        input_kwh_str += ".";
      }
    }
    renderInputScreen();
    return;
  }

  if (key == '*') {
    if (input_kwh_str.length() > 0) {
      input_kwh_str.remove(input_kwh_str.length() - 1);
    }
    if (input_kwh_str.length() == 0 && key_sequence.length() == 0) {
      exitInputMode();
      updateLCD();
      return;
    }
    renderInputScreen();
    return;
  }
}

// ================= POPUP NOTIFIKASI =================
void showPopup(const String& l1, const String& l2, unsigned long duration) {
  popup_line1 = l1;
  popup_line2 = l2;
  popup_end_time = millis() + duration;

  lcdPrintLine(0, popup_line1);
  lcdPrintLine(1, popup_line2);
}

// ================= TAMPILAN MODE INPUT =================
void renderInputScreen() {
  if (popupActive()) return;

  if (waiting_format_confirm) {
    lcdPrintLine(0, "HAPUS SEMUA FILE");
    lcdPrintLine(1, "\x01=Ya  Lain=Batal");
    return;
  }

  if (key_sequence.length() > 0) {
    lcdPrintLine(0, "RESET Seq:" + key_sequence);
    if (key_sequence == "AB") {
      lcdPrintLine(1, "\x01=Nol C=HapusSD");
    } else {
      lcdPrintLine(1, "B=Lanjut \x02=Batal");
    }
    return;
  }

  lcdPrintLine(0, "Isi:+" + input_kwh_str + " kWh");
  lcdPrintLine(1, "\x01=Enter \x02=Hapus");
}

// ================= TAMPILAN NORMAL (PAGING) =================
void updateLCD() {
  if (popupActive()) {
    prevPageSwitch = millis();
    return;
  }

  unsigned long now = millis();
  if (now - prevPageSwitch >= LCD_PAGE_INTERVAL_MS) {
    prevPageSwitch = now;
    lcd_page = (lcd_page + 1) % LCD_PAGE_COUNT;
    lcdInvalidate();
  }

  char l1[40], l2[40];

  switch (lcd_page) {
    case 0:
      snprintf(l1, sizeof(l1), "Voltase:%3.1fV", v_rms_stable);
      snprintf(l2, sizeof(l2), "Arus   :%3.3fA", i_rms_stable);
      break;

    case 1:
      snprintf(l1, sizeof(l1), "Daya Aktif:%1.1fW", p_active_stable);
      snprintf(l2, sizeof(l2), "Daya Semu:%1.1fVA", p_apparent_stable);
      break;

    case 2:
      snprintf(l1, sizeof(l1), "Faktor Daya:%3.2f", power_factor);
      snprintf(l2, sizeof(l2), "Beban:%s", (i_rms_stable > 0.0f) ? "AKTIF" : "KOSONG");
      break;

    case 3:
      if (saldo_token_kwh < 1000.0) {
        snprintf(l1, sizeof(l1), "Saldo:%3.2f kWh", (float)saldo_token_kwh);
      } else {
        snprintf(l1, sizeof(l1), "Saldo:%3.0f kWh", (float)saldo_token_kwh);
      }
      snprintf(l2, sizeof(l2), "Sisa:%6.2f Jam", token_runway_hours);
      break;

    case 4:
      snprintf(l1, sizeof(l1), "Q1:%4.2f Q2:%4.2f", predicted_q[0], predicted_q[1]);
      snprintf(l2, sizeof(l2), "Q3:%4.2f Q4:%4.2f", predicted_q[2], predicted_q[3]);
      break;

    default:
      {
        struct tm ti;
        if (getTimeNonBlocking(&ti)) {
          snprintf(l1, sizeof(l1), "%02d/%02d %02d:%02d:%02d",
                   ti.tm_mday, ti.tm_mon + 1, ti.tm_hour, ti.tm_min, ti.tm_sec);
        } else {
          snprintf(l1, sizeof(l1), "Jam: belum sync");
        }
        snprintf(l2, sizeof(l2), "SD:%s W:%s M:%s",
                 sdCardAvailable ? "OK" : "--",
                 (WiFi.status() == WL_CONNECTED) ? "OK" : "--",
                 mqttClient.connected() ? "OK" : "--");
        break;
      }
  }

  lcdPrintLine(0, l1);
  lcdPrintLine(1, l2);
}

// ================= METROLOGI TRUE-RMS DENGAN KOMPENSASI NOISE FLOOR =================
void calculateACMetricsPresisi() {
  unsigned long start_time = millis();
  int n_samples = 0;
  long sum_raw_v = 0, sum_raw_i = 0;

  while ((millis() - start_time) < SAMPLING_PERIOD_MS && n_samples < MAX_SAMPLES) {
    int v_accum = 0;
    for (int k = 0; k < 4; k++) {
      v_accum += analogRead(PIN_VOLTAGE);
      delayMicroseconds(4);
    }
    int v_val = v_accum >> 2;

    int i_accum = 0;
    for (int k = 0; k < 16; k++) {
      i_accum += analogRead(PIN_CURRENT);
      delayMicroseconds(2);
    }
    int i_val = i_accum >> 4;

    raw_v_arr[n_samples] = v_val;
    raw_i_arr[n_samples] = i_val;
    sum_raw_v += v_val;
    sum_raw_i += i_val;
    n_samples++;

    delayMicroseconds(180);
  }

  if (n_samples < 100) {
    v_rms = 0.0f;
    i_rms = 0.0f;
    p_active = 0.0f;
    p_apparent = 0.0f;
    power_factor = 0.0f;
    return;
  }

  float v_offset_approx = (float)sum_raw_v / n_samples;

  const float HYST_THRESHOLD = 40.0f;
  bool armed = false;
  int zc_indices[25];
  int zc_count = 0;

  for (int n = 0; n < n_samples; n++) {
    float v_diff = (float)raw_v_arr[n] - v_offset_approx;
    if (v_diff < -HYST_THRESHOLD) {
      armed = true;
    }
    if (armed && v_diff >= 0.0f) {
      if (zc_count == 0 || (n - zc_indices[zc_count - 1] >= 35)) {
        if (zc_count < 25) {
          zc_indices[zc_count++] = n;
        }
      }
      armed = false;
    }
  }

  if (zc_count < 3) {
    v_rms = 0.0f;
    i_rms = 0.0f;
    p_active = 0.0f;
    p_apparent = 0.0f;
    power_factor = 0.0f;
    return;
  }

  int start_idx = zc_indices[0];
  int end_idx = zc_indices[zc_count - 1];
  int cycle_samples = end_idx - start_idx;

  if (cycle_samples < 70) {
    v_rms = 0.0f;
    i_rms = 0.0f;
    p_active = 0.0f;
    p_apparent = 0.0f;
    power_factor = 0.0f;
    return;
  }

  long sum_v_exact = 0, sum_i_exact = 0;
  for (int n = start_idx; n < end_idx; n++) {
    sum_v_exact += raw_v_arr[n];
    sum_i_exact += raw_i_arr[n];
  }
  float v_offset = (float)sum_v_exact / cycle_samples;
  float i_offset = (float)sum_i_exact / cycle_samples;

  double sum_v_sq = 0.0, sum_i_sq = 0.0;
  for (int n = start_idx; n < end_idx; n++) {
    float v_inst = (float)raw_v_arr[n] - v_offset;
    float i_inst = (float)raw_i_arr[n] - i_offset;
    sum_v_sq += (double)(v_inst * v_inst);
    sum_i_sq += (double)(i_inst * i_inst);
  }

  double mean_v_sq = sum_v_sq / cycle_samples;
  double mean_i_sq = sum_i_sq / cycle_samples;

  if (mean_i_sq < 100.0) {
    const double NOISE_VARIANCE_I = 0.0;
    if (mean_i_sq > NOISE_VARIANCE_I) {
      mean_i_sq -= NOISE_VARIANCE_I;
    } else {
      mean_i_sq = 0.0;
    }
  }

  float v_calc_raw = (sqrt(mean_v_sq) / 4095.0f) * V_CAL;
  i_rms = (sqrt(mean_i_sq) / 4095.0f) * I_CAL;

  static int v_glitch_counter = 0;
  if (v_rms_stable > 180.0f && (v_calc_raw > 265.0f || v_calc_raw < 150.0f)) {
    v_glitch_counter++;
    if (v_glitch_counter > 3) {
      v_rms = v_calc_raw;
      v_glitch_counter = 0;
    } else {
      v_rms = v_rms_stable;
    }
  } else if (v_calc_raw < 50.0f) {
    v_rms = 0.0f;
    v_glitch_counter = 0;
  } else {
    v_rms = v_calc_raw;
    v_glitch_counter = 0;
  }

  double sum_p_inst = 0.0;
  int valid_p = 0;

  for (int n = start_idx; n < end_idx; n++) {
    int v_shifted_idx = n + PHASE_SAMPLE_SHIFT;
    if (v_shifted_idx >= 0 && v_shifted_idx < n_samples - 1) {
      float v0 = (float)raw_v_arr[v_shifted_idx] - v_offset;
      float v1 = (float)raw_v_arr[v_shifted_idx + 1] - v_offset;
      float v_aligned = v0 + (FINE_PHASE_TUNE * (v1 - v0));
      float i_inst = (float)raw_i_arr[n] - i_offset;
      sum_p_inst += (double)(v_aligned * i_inst);
      valid_p++;
    }
  }

  p_active = (valid_p > 0) ? ((sum_p_inst / valid_p) / (4095.0f * 4095.0f)) * (V_CAL * I_CAL) : 0.0f;

  if (p_active < 0.0f) {
    p_active = fabs(p_active);
  }

  if (i_rms < NO_LOAD_CURRENT_THRESHOLD || p_active < NO_LOAD_POWER_THRESHOLD) {
    i_rms = 0.0f;
    p_active = 0.0f;
  }

  p_apparent = v_rms * i_rms;

  if (p_active > p_apparent) {
    p_apparent = p_active;
  }

  if (i_rms == 0.0f || p_apparent < 0.5f) {
    power_factor = 0.0f;
    p_apparent = 0.0f;
  } else {
    power_factor = p_active / p_apparent;
    if (power_factor > 1.0f) power_factor = 1.0f;
  }
}

// ================= ADAPTASI AWAL TINYML =================
void warmupTinyMLWithCurrentLoad() {
  float active_kw = (p_active_stable > 0.05f) ? (p_active_stable / 1000.0f) : 0.060f;
  float initial_energy_15m = active_kw * 0.25f;

  for (int i = 0; i < BUFFER_SIZE; i++) {
    energy_buffer_15m[i] = initial_energy_15m;
  }

  float features[8];
  build_features(features);
  tinyml_predict(features, predicted_q);

  is_ml_warmed_up = true;
}

// ================= EKSTRAKSI FITUR TINYML =================
void build_features(float* features) {
  features[0] = energy_buffer_15m[buffer_idx];
  features[1] = energy_buffer_15m[(buffer_idx - 1 + BUFFER_SIZE) % BUFFER_SIZE];
  features[2] = energy_buffer_15m[(buffer_idx - 2 + BUFFER_SIZE) % BUFFER_SIZE];
  features[3] = energy_buffer_15m[(buffer_idx - 3 + BUFFER_SIZE) % BUFFER_SIZE];

  float sum_6h = 0.0f;
  for (int k = 0; k < 24; k++) {
    int idx = (buffer_idx - 1 - k + BUFFER_SIZE * 2) % BUFFER_SIZE;
    sum_6h += energy_buffer_15m[idx];
  }
  features[4] = sum_6h / 24.0f;

  features[5] = energy_buffer_15m[(buffer_idx + 1) % BUFFER_SIZE];

  struct tm timeinfo;
  float hour_dec = 12.0f;
  if (getTimeNonBlocking(&timeinfo)) {
    hour_dec = timeinfo.tm_hour + (timeinfo.tm_min / 60.0f);
  }
  features[6] = sinf(2.0f * PI * hour_dec / 24.0f);
  features[7] = cosf(2.0f * PI * hour_dec / 24.0f);
}

// ================= JARINGAN WIFI & MQTT =================
void handleWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - prevWifiRetry >= 5000) {
    prevWifiRetry = millis();
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
}

void handleMQTT() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqttClient.connected()) {
    mqttClient.loop();
    return;
  }
  if (millis() - prevMqttRetry >= 5000) {
    prevMqttRetry = millis();
    if (mqttClient.connect(mqttClientId.c_str())) {
      mqttClient.subscribe(MQTT_TOPIC_SUB);
      Serial.printf("[MQTT] Subscribed to: %s\n", MQTT_TOPIC_SUB);

      if (predicted_q[0] == 0.0f && predicted_q[3] == 0.0f) {
        float features[8];
        build_features(features);
        tinyml_predict(features, predicted_q);
      }
    }
  }
}

void publishTelemetry() {
  if (!mqttClient.connected()) return;

  struct tm timeinfo;
  char time_str[25] = "";

  if (getTimeNonBlocking(&timeinfo)) {
    strftime(time_str, sizeof(time_str), "%Y-%m-%d %H:%M:%S", &timeinfo);
  }

  char payload[450];
  snprintf(payload, sizeof(payload),
           "{\"timestamp\":\"%s\","
           "\"voltage\":%.2f,\"current\":%.3f,\"power\":%.2f,\"apparent_power\":%.2f,\"pf\":%.2f,"
           "\"saldo_kwh\":%.6f,\"runway_hours\":%.2f,"
           "\"q1\":%.3f,\"q2\":%.3f,\"q3\":%.3f,\"q4\":%.3f}",
           time_str,
           v_rms_stable, i_rms_stable, p_active_stable, p_apparent_stable, power_factor * 100.0f,
           saldo_token_kwh, token_runway_hours,
           predicted_q[0], predicted_q[1], predicted_q[2], predicted_q[3]);

  mqttClient.publish(MQTT_TOPIC_PUB, payload);
}
