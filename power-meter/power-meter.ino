/*
 * ==================================================================================
 * PROYEK: SMART POWER METER & TINYML PREDICTOR (ELEVATE 2026)
 * REVISI: SAMPLING INTERVAL TETAP (10 SIKLUS PENUH) + FILTER TEGANGAN + ATOMIC WRITE SD
 * ==================================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Keypad.h>
#include <time.h>
#include "FS.h"
#include "SD.h"
#include "SPI.h"
#include "tinyml_inference.h"

// Set ke 1 untuk mencetak diagnosa sampling ke Serial Monitor (cek clipping & jitter)
#define DEBUG_SAMPLING 1

// ================= OLED SSD1306 (I2C: GPIO 21, 22) =================
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// ================= MICRO SD CARD (VSPI: CS=5, MOSI=23, MISO=19, SCK=18) =================
#define SD_CS_PIN 5
bool sdCardAvailable = false;
const char* FILE_SALDO = "/saldo.txt";
const char* FILE_SALDO_TMP = "/saldo.tmp";
const char* FILE_LOG = "/energy_log.csv";
const char* FILE_BUFFER = "/buffer_history.dat";
const char* FILE_BUFF_TMP = "/buffer_history.tmp";

// ================= KEYPAD MATRIX 4x4 (GPIO 13, 12, 14, 26, 27, 25, 33, 32) =================
const byte ROWS = 4;
const byte COLS = 4;
char keys[ROWS][COLS] = {
  { '1', '2', '3', 'A' },
  { '4', '5', '6', 'B' },
  { '7', '8', '9', 'C' },
  { '*', '0', '#', 'D' }
};

byte rowPins[ROWS] = { 13, 12, 14, 26 };
byte colPins[COLS] = { 27, 25, 33, 32 };
Keypad keypad = Keypad(makeKeymap(keys), rowPins, colPins, ROWS, COLS);

// ================= KONFIGURASI WIFI & HIVEMQ =================
const char* WIFI_SSID = "Ridho Ruijie";
const char* WIFI_PASSWORD = "ridhosetiawanskom24";
const char* MQTT_BROKER = "broker.hivemq.com";
const int MQTT_PORT = 1883;
const char* MQTT_TOPIC_PUB = "elevate2026/powermeter/telemetry";

const char* NTP_SERVER = "pool.ntp.org";
const long GMT_OFFSET_SEC = 7 * 3600;
const int DAYLIGHT_OFFSET_SEC = 0;

String mqttClientId = "ESP32_ELEVATE_" + String((uint32_t)ESP.getEfuseMac(), HEX);
WiFiClient espClient;
PubSubClient mqttClient(espClient);

// ================= PIN SENSOR ADC1 =================
#define PIN_VOLTAGE 35
#define PIN_CURRENT 34
#define MAX_SAMPLES 400
// 500 us x 400 sampel = 200 ms = tepat 10 siklus 50 Hz (jika jaringan 60 Hz: 12 siklus, juga bulat)
#define SAMPLE_INTERVAL_US 500
#define OVERSAMPLE_COUNT 4  // jumlah pembacaan ADC per sampel (harus 1, 2, atau 4)

const float V_CAL = 5581.51f;
const float I_CAL = 111.53f;
const int PHASE_SAMPLE_SHIFT = -3;
const float FINE_PHASE_TUNE = 0.45f;
const float NO_LOAD_CURRENT_THRESHOLD = 0.010f;
const float NO_LOAD_POWER_THRESHOLD = 0.10f;

float v_rms = 0.0f, i_rms = 0.0f, p_active = 0.0f, p_apparent = 0.0f, power_factor = 0.0f;
float v_rms_stable = 0.0f;
float i_rms_stable = 0.0f, p_active_stable = 0.0f, p_apparent_stable = 0.0f;

int raw_v_arr[MAX_SAMPLES];
int raw_i_arr[MAX_SAMPLES];

// ================= SALDO & BUFFER PREDIKSI =================
float saldo_token_kwh = 20.0f;
float last_saved_saldo = 20.0f;
float current_interval_kwh = 0.0f;
float token_runway_hours = 999.0f;
unsigned long prevSaldoPeriodicSave = 0;

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
unsigned long prevOledRefresh = 0;
unsigned long prevSdRetryTime = 0;
const unsigned long INTERVAL_15M = 900000;

// ================= PROTOTIPE FUNGSI =================
void handleKeypad();
void updateOLED();
void renderInputScreen();
void showPopup(const String& l1, const String& l2, unsigned long duration);
void calculateACMetricsPresisi();
void build_features(float* features);
void handleWiFi();
void handleMQTT();
void publishTelemetry();
void warmupTinyMLWithCurrentLoad();
void initSDCard();
void saveSaldoToSD(float saldo);
void logTelemetryToSD();
void saveBufferToSD();
bool loadBufferFromSD();
void wipeAllSDCardFiles();

// ================= SETUP =================
void setup() {
  Serial.begin(115200);

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println(F("[OLED] Gagal mendeteksi SSD1306"));
  }
  display.clearDisplay();
  display.setTextColor(WHITE);
  display.setTextSize(1);
  display.setCursor(15, 25);
  display.println(F("BOOTING SYSTEM..."));
  display.display();

  initSDCard();

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  configTime(GMT_OFFSET_SEC, DAYLIGHT_OFFSET_SEC, NTP_SERVER);

  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setBufferSize(512);

  // Inisialisasi awal pembacaan AC
  calculateACMetricsPresisi();
  v_rms_stable = (v_rms >= 20.0f) ? v_rms : 0.0f;
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
}

// ================= LOOP UTAMA =================
void loop() {
  unsigned long currentMillis = millis();

  handleKeypad();
  handleWiFi();
  handleMQTT();

  // 1. Metrologi & Riemann Sum Pemotongan Saldo (Tiap 1 Detik)
  if (currentMillis - prevSecTime >= 1000) {
    prevSecTime = currentMillis;

    calculateACMetricsPresisi();

    // Filter tegangan (EMA)
    if (v_rms < 20.0f) {
      v_rms_stable = 0.0f;
    } else if (v_rms_stable == 0.0f) {
      v_rms_stable = v_rms;
    } else {
      v_rms_stable = (0.8f * v_rms_stable) + (0.2f * v_rms);
    }

    if (i_rms == 0.0f) {
      i_rms_stable = 0.0f;
      p_active_stable = 0.0f;
      p_apparent_stable = 0.0f;
    } else {
      if (i_rms_stable == 0.0f) {
        i_rms_stable = i_rms;
        p_active_stable = p_active;
        p_apparent_stable = p_apparent;
      }
      i_rms_stable = (0.75f * i_rms_stable) + (0.25f * i_rms);
      p_active_stable = (0.75f * p_active_stable) + (0.25f * p_active);
      p_apparent_stable = (0.75f * p_apparent_stable) + (0.25f * p_apparent);
    }

    if (!is_ml_warmed_up && p_active_stable > 0.1f) {
      warmupTinyMLWithCurrentLoad();
    }

    float delta_kwh = p_active_stable / 3600000.0f;
    saldo_token_kwh -= delta_kwh;
    if (saldo_token_kwh < 0.0f) saldo_token_kwh = 0.0f;
    current_interval_kwh += delta_kwh;

    // SIMPAN SALDO AMAN: tiap berkurang >= 0.05 kWh ATAU tiap 5 menit jika beban menyala
    bool delta_threshold = (fabsf(last_saved_saldo - saldo_token_kwh) >= 0.05f);
    bool periodic_due = (currentMillis - prevSaldoPeriodicSave >= 300000 && delta_kwh > 0.0f);

    if (delta_threshold || periodic_due) {
      prevSaldoPeriodicSave = currentMillis;
      saveSaldoToSD(saldo_token_kwh);
    }

    float avg_kw = (predicted_q[0] > 0.01f) ? (predicted_q[0] / 6.0f) : (p_active_stable / 1000.0f);
    if (avg_kw > 0.005f) {
      token_runway_hours = saldo_token_kwh / avg_kw;
    } else {
      token_runway_hours = 999.0f;
    }
  }

  // 2. Refresh Layar OLED (Tiap 200 ms)
  if (currentMillis - prevOledRefresh >= 200) {
    prevOledRefresh = currentMillis;
    if (is_input_mode) {
      renderInputScreen();
    } else {
      updateOLED();
    }
  }

  // 3. Pencatatan log berkala ke MicroSD (tiap 30 detik)
  if (currentMillis - prevSdLogTime >= 30000) {
    prevSdLogTime = currentMillis;
    logTelemetryToSD();
  }

  // 4. Publikasi MQTT HiveMQ (tiap 2 detik)
  if (currentMillis - prevMqttSendTime >= 2000) {
    prevMqttSendTime = currentMillis;
    publishTelemetry();
  }

  // 5. Inferensi TinyML 15-Menitan
  if (currentMillis - prevInterval15m >= INTERVAL_15M) {
    prevInterval15m = currentMillis;

    buffer_idx = (buffer_idx + 1) % BUFFER_SIZE;
    energy_buffer_15m[buffer_idx] = current_interval_kwh;
    current_interval_kwh = 0.0f;

    saveBufferToSD();

    float features[8];
    build_features(features);
    tinyml_predict(features, predicted_q);

    logTelemetryToSD();
  }

  // 6. Percobaan recovery SD Card non-blocking (tiap 30 detik jika kartu offline)
  if (!sdCardAvailable && (currentMillis - prevSdRetryTime >= 30000)) {
    prevSdRetryTime = currentMillis;
    initSDCard();
  }
}

// ================= SISTEM PENYIMPANAN MICROSD BERSIH & AMAN =================
void initSDCard() {
  SPI.begin(18, 19, 23, SD_CS_PIN);
  if (!SD.begin(SD_CS_PIN, SPI, 4000000)) {
    Serial.println(F("[SD] Kartu belum terpasang atau gagal mount. Sistem tetap berjalan tanpa SD."));
    sdCardAvailable = false;
    return;
  }
  if (SD.cardType() == CARD_NONE) {
    sdCardAvailable = false;
    return;
  }

  sdCardAvailable = true;
  Serial.println(F("[SD] MicroSD Card terdeteksi dan siap!"));

  // Pulihkan jika ada berkas sementara yang belum sempat di-rename
  if (!SD.exists(FILE_SALDO) && SD.exists(FILE_SALDO_TMP)) {
    SD.rename(FILE_SALDO_TMP, FILE_SALDO);
  }

  if (SD.exists(FILE_SALDO)) {
    File f = SD.open(FILE_SALDO, FILE_READ);
    if (f) {
      String content = f.readStringUntil('\n');
      content.trim();
      if (content.length() > 0) {
        saldo_token_kwh = content.toFloat();
        last_saved_saldo = saldo_token_kwh;
        Serial.printf("[SD] Saldo berhasil dimuat: %.4f kWh\n", saldo_token_kwh);
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

void saveSaldoToSD(float saldo) {
  saldo_token_kwh = saldo;
  last_saved_saldo = saldo;

  if (!sdCardAvailable) return;

  File f = SD.open(FILE_SALDO_TMP, FILE_WRITE);
  if (f) {
    f.printf("%.4f\n", saldo);
    f.flush();
    f.close();

    if (SD.exists(FILE_SALDO)) {
      SD.remove(FILE_SALDO);
    }
    SD.rename(FILE_SALDO_TMP, FILE_SALDO);
  }
}

void saveBufferToSD() {
  if (!sdCardAvailable) return;

  File f = SD.open(FILE_BUFF_TMP, FILE_WRITE);
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

  saldo_token_kwh = 0.0f;
  last_saved_saldo = 0.0f;
  for (int i = 0; i < BUFFER_SIZE; i++) {
    energy_buffer_15m[i] = 0.0f;
  }

  saveSaldoToSD(0.0f);

  File logFile = SD.open(FILE_LOG, FILE_WRITE);
  if (logFile) {
    logFile.println("uptime_s,voltage,current,p_active,p_apparent,pf,saldo_kwh,runway_h,q1,q2,q3,q4");
    logFile.flush();
    logFile.close();
  }
}

void logTelemetryToSD() {
  if (!sdCardAvailable) return;

  File logFile = SD.open(FILE_LOG, FILE_APPEND);
  if (logFile) {
    logFile.printf("%lu,%.2f,%.3f,%.2f,%.2f,%.2f,%.4f,%.2f,%.3f,%.3f,%.3f,%.3f\n",
                   millis() / 1000,
                   v_rms_stable, i_rms_stable, p_active_stable, p_apparent_stable, power_factor * 100.0f,
                   saldo_token_kwh, token_runway_hours,
                   predicted_q[0], predicted_q[1], predicted_q[2], predicted_q[3]);
    logFile.flush();
    logFile.close();
  }
}

// ================= LOGIKA KEYPAD MATRIKS =================
void handleKeypad() {
  char key = keypad.getKey();
  if (!key) return;

  if (waiting_format_confirm) {
    if (key == 'D') {
      wipeAllSDCardFiles();
      showPopup(F("SD BERHASIL"), F("DIRESET TOTAL!"), 1500);
      waiting_format_confirm = false;
      is_input_mode = false;
      key_sequence = "";
    } else {
      showPopup(F("FORMAT SD"), F("DIBATALKAN"), 1000);
      waiting_format_confirm = false;
      is_input_mode = false;
      key_sequence = "";
    }
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
    if (key_sequence == "AB") {
      saveSaldoToSD(0.0f);
      showPopup(F("SALDO RESET"), F("MENJADI 0.00 kWh"), 1500);
      key_sequence = "";
      is_input_mode = false;
    } else {
      key_sequence = "";
      is_input_mode = false;
      updateOLED();
    }
    return;
  }

  if (key >= '0' && key <= '9') {
    is_input_mode = true;
    key_sequence = "";
    if (input_kwh_str.length() < 6) {
      input_kwh_str += key;
    }
    renderInputScreen();
    return;
  }

  if (key == '*') {
    if (input_kwh_str.length() > 0) {
      input_kwh_str.remove(input_kwh_str.length() - 1);
    }

    if (input_kwh_str.length() == 0 && key_sequence.length() == 0) {
      is_input_mode = false;
      waiting_format_confirm = false;
      updateOLED();
      return;
    }

    renderInputScreen();
    return;
  }

  if (key == '#') {
    if (input_kwh_str.length() > 0) {
      float added_kwh = input_kwh_str.toFloat();
      saveSaldoToSD(saldo_token_kwh + added_kwh);
      showPopup(F("TOP-UP BERHASIL!"), "+" + String(added_kwh, 2) + " kWh", 1500);
      input_kwh_str = "";
      key_sequence = "";
      is_input_mode = false;
    } else {
      is_input_mode = false;
      key_sequence = "";
      updateOLED();
    }
    return;
  }
}

void showPopup(const String& l1, const String& l2, unsigned long duration) {
  popup_line1 = l1;
  popup_line2 = l2;
  popup_end_time = millis() + duration;

  display.clearDisplay();
  display.setTextColor(WHITE);
  display.setTextSize(1);
  display.setCursor(10, 20);
  display.print(popup_line1);
  display.setCursor(10, 36);
  display.print(popup_line2);
  display.display();
}

void renderInputScreen() {
  if (millis() < popup_end_time) return;

  display.clearDisplay();
  display.setTextColor(WHITE);

  if (waiting_format_confirm) {
    display.setTextSize(1);
    display.setCursor(8, 4);
    display.println(F("=== FORMAT SD ==="));
    display.setCursor(0, 24);
    display.println(F("Hapus seluruh file?"));
    display.drawLine(0, 42, 128, 42, WHITE);
    display.setCursor(0, 50);
    display.println(F("[D] Ya    [*] Batal"));
    display.display();
    return;
  }

  if (key_sequence.length() > 0) {
    display.setTextSize(1);
    display.setCursor(15, 4);
    display.println(F("=== MENU RESET ==="));

    display.setTextSize(2);
    display.setCursor(50, 20);
    display.print(key_sequence);

    display.drawLine(0, 42, 128, 42, WHITE);
    display.setTextSize(1);
    display.setCursor(0, 50);
    if (key_sequence == "AB") {
      display.println(F("[D]ClrSaldo [C]HapusSD"));
    } else {
      display.println(F("[B] Lanjut  [*] Batal"));
    }
    display.display();
    return;
  }

  if (input_kwh_str.length() > 0) {
    display.setTextSize(1);
    display.setCursor(15, 4);
    display.println(F("INPUT TOKEN kWh"));

    display.setTextSize(2);
    display.setCursor(10, 20);
    display.printf("+%s", input_kwh_str.c_str());

    display.drawLine(0, 42, 128, 42, WHITE);
    display.setTextSize(1);
    display.setCursor(0, 50);
    display.println(F("[#] Simpan  [*] Hapus"));
    display.display();
    return;
  }
}

void updateOLED() {
  if (millis() < popup_end_time) return;

  display.clearDisplay();
  display.setTextColor(WHITE);
  display.setTextSize(1);

  display.setCursor(0, 0);
  display.printf("V:%3.0f P:%3.0fW S:%3.0fVA", v_rms_stable, p_active_stable, p_apparent_stable);

  display.setCursor(0, 12);
  display.printf("Arus  : %5.3f A", i_rms_stable);

  display.setCursor(0, 24);
  display.printf("Saldo : %5.2f kWh", saldo_token_kwh);

  display.setCursor(0, 36);
  display.printf("Sisa  : %5.2f Jam", token_runway_hours);

  display.drawLine(0, 48, 128, 48, WHITE);
  display.setCursor(0, 52);
  display.print(sdCardAvailable ? "[SD] Ketik Angka/A" : "!SD Ketik Angka/A");

  display.display();
}

// ================= METROLOGI TRUE-RMS PRESISI =================
// Sampling dengan interval tetap (SAMPLE_INTERVAL_US) selama tepat MAX_SAMPLES sampel,
// sehingga jendela pengukuran = jumlah siklus gelombang yang bulat -> RMS stabil.
// handleKeypad() sengaja TIDAK dipanggil di dalam jendela ini (I2C OLED bisa memblokir).
void calculateACMetricsPresisi() {
  unsigned long start_time = millis();
  int n_samples = 0;
  long sum_raw_v = 0, sum_raw_i = 0;
  unsigned long next_us = micros();

  while (n_samples < MAX_SAMPLES) {
    while ((long)(micros() - next_us) < 0) { }  // tunggu waktu sampling tepat
    next_us += SAMPLE_INTERVAL_US;

    int v_accum = 0, i_accum = 0;
    for (int k = 0; k < OVERSAMPLE_COUNT; k++) {
      v_accum += analogRead(PIN_VOLTAGE);
      i_accum += analogRead(PIN_CURRENT);
    }
    int v_val = v_accum / OVERSAMPLE_COUNT;
    int i_val = i_accum / OVERSAMPLE_COUNT;

    raw_v_arr[n_samples] = v_val;
    raw_i_arr[n_samples] = i_val;
    sum_raw_v += v_val;
    sum_raw_i += i_val;
    n_samples++;
  }

  if (n_samples < 50) return;

  float v_offset = (float)sum_raw_v / n_samples;
  float i_offset = (float)sum_raw_i / n_samples;

  double sum_v_sq = 0.0, sum_i_sq = 0.0;
  for (int n = 0; n < n_samples; n++) {
    float v_inst = (float)raw_v_arr[n] - v_offset;
    float i_inst = (float)raw_i_arr[n] - i_offset;
    sum_v_sq += (double)(v_inst * v_inst);
    sum_i_sq += (double)(i_inst * i_inst);
  }

  v_rms = (sqrt(sum_v_sq / n_samples) / 4095.0f) * V_CAL;
  i_rms = (sqrt(sum_i_sq / n_samples) / 4095.0f) * I_CAL;

  double sum_p_inst = 0.0;
  int valid_p = 0;
  int start_idx = (1 > -PHASE_SAMPLE_SHIFT) ? 1 : -PHASE_SAMPLE_SHIFT;
  int end_idx = (n_samples - 1 < n_samples - PHASE_SAMPLE_SHIFT - 1) ? (n_samples - 1) : (n_samples - PHASE_SAMPLE_SHIFT - 1);

  for (int n = start_idx; n < end_idx; n++) {
    float v0 = (float)raw_v_arr[n + PHASE_SAMPLE_SHIFT] - v_offset;
    float v1 = (float)raw_v_arr[n + PHASE_SAMPLE_SHIFT + 1] - v_offset;
    float v_aligned = v0 + (FINE_PHASE_TUNE * (v1 - v0));
    float i_inst = (float)raw_i_arr[n] - i_offset;
    sum_p_inst += (double)(v_aligned * i_inst);
    valid_p++;
  }

  p_active = (valid_p > 0) ? ((sum_p_inst / valid_p) / (4095.0f * 4095.0f)) * (V_CAL * I_CAL) : 0.0f;

  if (i_rms < NO_LOAD_CURRENT_THRESHOLD || p_active < NO_LOAD_POWER_THRESHOLD) {
    i_rms = 0.0f;
    p_active = 0.0f;
  }
  if (p_active < 0.0f) p_active = 0.0f;

  p_apparent = v_rms * i_rms;

  if (i_rms == 0.0f || p_apparent < 0.5f) {
    power_factor = 0.0f;
    p_apparent = 0.0f;
  } else {
    power_factor = p_active / p_apparent;
    if (power_factor > 1.0f) power_factor = 1.0f;
  }

#if DEBUG_SAMPLING
  int vmin = 4095, vmax = 0;
  for (int n = 0; n < n_samples; n++) {
    if (raw_v_arr[n] < vmin) vmin = raw_v_arr[n];
    if (raw_v_arr[n] > vmax) vmax = raw_v_arr[n];
  }
  Serial.printf("[DBG] n=%d t=%lums vmin=%d vmax=%d off=%.1f vrms=%.1f\n",
                n_samples, millis() - start_time, vmin, vmax, v_offset, v_rms);
#endif
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
  Serial.printf("[TinyML] Warm-up selesai: Q1=%.3f, Q2=%.3f, Q3=%.3f, Q4=%.3f\n",
                predicted_q[0], predicted_q[1], predicted_q[2], predicted_q[3]);
}

// ================= EKSTRAKSI FITUR TINYML SINKRON MODEL =================
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
  if (getLocalTime(&timeinfo)) {
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

  char payload[380];
  snprintf(payload, sizeof(payload),
           "{\"voltage\":%.2f,\"current\":%.3f,\"power\":%.2f,\"apparent_power\":%.2f,\"pf\":%.2f,"
           "\"saldo_kwh\":%.4f,\"runway_hours\":%.2f,"
           "\"q1\":%.3f,\"q2\":%.3f,\"q3\":%.3f,\"q4\":%.3f}",
           v_rms_stable, i_rms_stable, p_active_stable, p_apparent_stable, power_factor * 100.0f,
           saldo_token_kwh, token_runway_hours,
           predicted_q[0], predicted_q[1], predicted_q[2], predicted_q[3]);

  mqttClient.publish(MQTT_TOPIC_PUB, payload);
}
