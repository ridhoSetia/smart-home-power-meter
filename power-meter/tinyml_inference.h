#ifndef TINYML_INFERENCE_H
#define TINYML_INFERENCE_H

#include <math.h>
#include "tinyml_model.h"

#ifndef PI
#define PI 3.14159265358979323846f
#endif

/**
 * @brief Struktur data penampung hasil prediksi 4 horizon (Q1 - Q4) dalam satuan kWh
 */
struct TinyMLPrediction {
    float q1_0_6h;    // Horizon 0–6 jam ke depan
    float q2_6_12h;   // Horizon 6–12 jam ke depan
    float q3_12_18h;  // Horizon 12–18 jam ke depan
    float q4_18_24h;  // Horizon 18–24 jam ke depan
    float total_24h;  // Akumulasi konsumsi 24 jam ke depan
};

/**
 * @brief Ekstraksi 8 fitur masukan dari Circular Buffer energi 96 slot
 */
inline void tinyml_extract_features(const float buffer[96], int headIndex, int hour, int minute, float features_out[8]) {
    auto get_lag = [&](int lag) -> float {
        int idx = (headIndex - lag) % 96;
        if (idx < 0) idx += 96;
        return buffer[idx];
    };

    // Fitur 0 - 3: 4 Lag konsumsi 1 jam terakhir
    features_out[0] = get_lag(0); // x0: t0 (15 menit terakhir)
    features_out[1] = get_lag(1); // x1: t-1 (15 menit lalu)
    features_out[2] = get_lag(2); // x2: t-2 (30 menit lalu)
    features_out[3] = get_lag(3); // x3: t-3 (45 menit lalu)

    // Fitur 4: Baseline 6 jam terakhir (rata-rata 24 blok terakhir dari t-1 s.d. t-24)
    float sum_6h = 0.0f;
    for (int i = 1; i <= 24; i++) {
        sum_6h += get_lag(i);
    }
    features_out[4] = sum_6h / 24.0f;

    // Fitur 5: Konsumsi jam yang sama kemarin (lag 96 blok / 24 jam lalu)
    features_out[5] = get_lag(95);

    // Fitur 6 - 7: Siklus sirkadian jam (sin & cos 24 jam)
    float hour_decimal = (float)hour + ((float)minute / 60.0f);
    features_out[6] = sinf(2.0f * PI * hour_decimal / 24.0f);
    features_out[7] = cosf(2.0f * PI * hour_decimal / 24.0f);
}

/**
 * @brief Eksekusi inferensi On-Device AR-MLP (8 -> 24 -> 12 -> 4) - Return Struct
 */
inline TinyMLPrediction tinyml_predict(const float x_raw[8]) {
    // 1. Normalisasi Z-Score Masukan: x_norm = (x - mean) / std
    float x_norm[N_INPUT];
    for (int i = 0; i < N_INPUT; i++) {
        x_norm[i] = (x_raw[i] - FEATURE_MEAN[i]) / FEATURE_STD[i];
    }

    // 2. Hidden Layer 1 (Dense 8 -> 24, aktivasi LeakyReLU)
    float h1[N_HIDDEN1];
    for (int j = 0; j < N_HIDDEN1; j++) {
        float sum = B_LAYER1[j];
        for (int i = 0; i < N_INPUT; i++) {
            sum += x_norm[i] * W_LAYER1[i * N_HIDDEN1 + j];
        }
        h1[j] = (sum > 0.0f) ? sum : (LEAKY_SLOPE * sum);
    }

    // 3. Hidden Layer 2 (Dense 24 -> 12, aktivasi LeakyReLU)
    float h2[N_HIDDEN2];
    for (int j = 0; j < N_HIDDEN2; j++) {
        float sum = B_LAYER2[j];
        for (int i = 0; i < N_HIDDEN1; i++) {
            sum += h1[i] * W_LAYER2[i * N_HIDDEN2 + j];
        }
        h2[j] = (sum > 0.0f) ? sum : (LEAKY_SLOPE * sum);
    }

    // 4. Output Layer (Dense 12 -> 4, aktivasi Linear)
    float out_norm[N_OUTPUT];
    for (int j = 0; j < N_OUTPUT; j++) {
        float sum = B_LAYER3[j];
        for (int i = 0; i < N_HIDDEN2; i++) {
            sum += h2[i] * W_LAYER3[i * N_OUTPUT + j];
        }
        out_norm[j] = sum;
    }

    // 5. Denormalisasi Luaran ke kWh: y_kwh = y_norm * std + mean (clamping >= 0)
    float q_kwh[N_OUTPUT];
    for (int j = 0; j < N_OUTPUT; j++) {
        float val = out_norm[j] * TARGET_STD[j] + TARGET_MEAN[j];
        q_kwh[j] = (val > 0.0f) ? val : 0.0f;
    }

    TinyMLPrediction pred;
    pred.q1_0_6h   = q_kwh[0];
    pred.q2_6_12h  = q_kwh[1];
    pred.q3_12_18h = q_kwh[2];
    pred.q4_18_24h = q_kwh[3];
    pred.total_24h = q_kwh[0] + q_kwh[1] + q_kwh[2] + q_kwh[3];

    return pred;
}

/**
 * @brief Overload fungsi tinyml_predict untuk kompatibilitas pemanggilan array output
 */
inline void tinyml_predict(const float x_raw[8], float out_q[4]) {
    TinyMLPrediction pred = tinyml_predict(x_raw);
    out_q[0] = pred.q1_0_6h;
    out_q[1] = pred.q2_6_12h;
    out_q[2] = pred.q3_12_18h;
    out_q[3] = pred.q4_18_24h;
}

#endif // TINYML_INFERENCE_H