"""
whatsapp_notifier.py - Modul Notifikasi Otomatis WhatsApp (WAHA Gateway & LLM Digest)
Sistem Smart Power Meter JAIRU.

Mencakup:
1. WAHAClient: REST API wrapper ke container WAHA (manajemen sesi, QR code, kirim teks).
2. WhatsAppAlertEngine: Evaluasi stream MQTT real-time, state-machine anti-spam
   dengan hysteresis (85% alert, 70% reset), milestone runway (12h, 6h),
   deteksi saldo habis / relay cut-off, konfirmasi top-up token.
3. DailyDigestScheduler: Agregasi 24 jam dan pembuatan narasi cerdas via LLM.
"""

import os
import json
import asyncio
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List

# Konfigurasi WAHA
WAHA_BASE_URL = os.getenv("WAHA_BASE_URL", "http://localhost:3000").rstrip("/")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")

# Konfigurasi LLM (mewarisi .env sistem)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")


def clean_phone_number(phone: str) -> str:
    """Normalisasi nomor ponsel ke format internasional tanpa simbol (contoh: 628123456789)."""
    if not phone:
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return digits


# --------------------------------------------------------------------------
# 1. Klien REST API WAHA
# --------------------------------------------------------------------------
class WAHAClient:
    def __init__(self, base_url: str = WAHA_BASE_URL, session: str = WAHA_SESSION):
        self.base_url = base_url
        self.session = session

    def _headers(self, **headers: str) -> Dict[str, str]:
        """Tambahkan API key WAHA bila autentikasi gateway diaktifkan."""
        if WAHA_API_KEY:
            headers["X-Api-Key"] = WAHA_API_KEY
        return headers

    async def get_session_status(self) -> Dict[str, Any]:
        """Periksa status sesi WAHA."""
        url = f"{self.base_url}/api/sessions/{self.session}"
        try:
            req = urllib.request.Request(url, headers=self._headers(Accept="application/json"))
            resp = await asyncio.to_thread(self._fetch_json, req)
            return {
                "status": "success",
                "session": {
                    "name": resp.get("name", self.session),
                    "status": resp.get("status", "UNKNOWN"),
                    "me": resp.get("me") or {},
                }
            }
        except Exception as e:
            # Jika 404, coba cek list sessions
            try:
                list_url = f"{self.base_url}/api/sessions"
                req_list = urllib.request.Request(list_url, headers=self._headers(Accept="application/json"))
                sessions = await asyncio.to_thread(self._fetch_json, req_list)
                for s in sessions:
                    if s.get("name") == self.session:
                        return {
                            "status": "success",
                            "session": {
                                "name": s.get("name", self.session),
                                "status": s.get("status", "UNKNOWN"),
                                "me": s.get("me") or {},
                            }
                        }
                return {
                    "status": "stopped",
                    "session": {"name": self.session, "status": "STOPPED", "me": {}}
                }
            except Exception:
                return {
                    "status": "offline",
                    "session": {"name": self.session, "status": "OFFLINE", "error": str(e), "me": {}}
                }

    async def start_session(self) -> Dict[str, Any]:
        """Mulai / inisiasi sesi WAHA."""
        url = f"{self.base_url}/api/sessions/start"
        payload = json.dumps({"name": self.session}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers=self._headers(**{"Content-Type": "application/json", "Accept": "application/json"}),
            method="POST"
        )
        try:
            resp = await asyncio.to_thread(self._fetch_json, req)
            return {"status": "success", "data": resp}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def stop_session(self) -> Dict[str, Any]:
        """Hentikan sesi WAHA."""
        url = f"{self.base_url}/api/sessions/stop"
        payload = json.dumps({"name": self.session}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers=self._headers(**{"Content-Type": "application/json", "Accept": "application/json"}),
            method="POST"
        )
        try:
            resp = await asyncio.to_thread(self._fetch_json, req)
            return {"status": "success", "data": resp}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def logout_session(self) -> Dict[str, Any]:
        """Putus koneksi / unpair perangkat dari WAHA."""
        url = f"{self.base_url}/api/sessions/logout"
        payload = json.dumps({"name": self.session}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers=self._headers(**{"Content-Type": "application/json", "Accept": "application/json"}),
            method="POST"
        )
        try:
            resp = await asyncio.to_thread(self._fetch_json, req)
            return {"status": "success", "data": resp}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def get_qr_image(self) -> Tuple[Optional[bytes], str]:
        """Ambil stream gambar PNG QR code dari WAHA."""
        # WAHA endpoint untuk QR image: /api/{session}/auth/qr?format=image atau /api/screenshot?session={session}
        urls = [
            f"{self.base_url}/api/{self.session}/auth/qr?format=image",
            f"{self.base_url}/api/{self.session}/auth/qr",
            f"{self.base_url}/api/screenshot?session={self.session}",
        ]
        last_err = ""
        for u in urls:
            try:
                req = urllib.request.Request(u, headers=self._headers(Accept="image/png,image/*,*/*"))
                data, mime = await asyncio.to_thread(self._fetch_raw, req)
                if data and len(data) > 100:
                    return data, mime or "image/png"
            except Exception as e:
                last_err = str(e)
                continue
        return None, last_err

    async def send_text(self, to_phone: str, text: str) -> Tuple[bool, str]:
        """Kirim pesan teks via WAHA."""
        cleaned = clean_phone_number(to_phone)
        if not cleaned:
            return False, "Nomor ponsel penerima kosong atau tidak valid"

        chat_id = f"{cleaned}@c.us"
        url = f"{self.base_url}/api/sendText"
        payload = json.dumps({
            "session": self.session,
            "chatId": chat_id,
            "text": text,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload,
            headers=self._headers(**{"Content-Type": "application/json", "Accept": "application/json"}),
            method="POST"
        )
        try:
            resp = await asyncio.to_thread(self._fetch_json, req)
            msg_id = resp.get("id") or resp.get("messageId") or "SENT"
            return True, str(msg_id)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            return False, f"HTTP {e.code}: {body}"
        except Exception as e:
            return False, str(e)

    def _fetch_json(self, req: urllib.request.Request) -> Any:
        with urllib.request.urlopen(req, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))

    def _fetch_raw(self, req: urllib.request.Request) -> Tuple[bytes, str]:
        with urllib.request.urlopen(req, timeout=12) as response:
            mime = response.headers.get_content_type()
            data = response.read()
            return data, mime


# --------------------------------------------------------------------------
# 2. Engine Alert & State Machine Hysteresis Anti-Spam
# --------------------------------------------------------------------------
class WhatsAppAlertEngine:
    def __init__(self, db_path_resolver, tarif_per_kwh: float = 1444.70):
        self.db_path_resolver = db_path_resolver
        self.tarif_per_kwh = tarif_per_kwh
        self.waha = WAHAClient()

        # In-Memory State Machine
        self._lock = asyncio.Lock()
        self._overload_consecutive_count = 0
        self._is_overload_locked = False
        self._runway_12h_locked = False
        self._runway_6h_locked = False
        self._runway_extender_locked = False
        self._is_cutoff_locked = False
        self._last_known_saldo = 0.0
        self._last_relay_state: Optional[bool] = None
        self._last_digest_date = ""

        # Cache konfigurasi lokal
        self._config_cache: Optional[Dict[str, Any]] = None
        self._last_config_load = 0.0

    def _get_db(self):
        conn = sqlite3.connect(self.db_path_resolver(), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    async def init_state(self):
        """Muat konfigurasi dan state awal dari basis data."""
        await asyncio.to_thread(self._load_state_from_db)

    def _load_state_from_db(self):
        conn = self._get_db()
        try:
            row = conn.execute("SELECT * FROM alert_runtime_states WHERE id = 1").fetchone()
            if row:
                self._is_overload_locked = bool(row["is_overload_locked"])
                self._runway_12h_locked = bool(row["runway_12h_locked"])
                self._runway_6h_locked = bool(row["runway_6h_locked"])
                self._runway_extender_locked = bool(row["runway_extender_locked"]) if "runway_extender_locked" in row.keys() else False
                self._is_cutoff_locked = bool(row["is_cutoff_locked"])
                self._last_known_saldo = float(row["last_known_saldo"] or 0.0)

            # Cek log terakhir digest hari ini
            today_str = datetime.now().strftime("%Y-%m-%d")
            digest_row = conn.execute(
                "SELECT id FROM daily_energy_digests WHERE report_date = ? AND delivery_status = 'DELIVERED'",
                (today_str,)
            ).fetchone()
            if digest_row:
                self._last_digest_date = today_str
        except Exception as e:
            print(f"[WA] Gagal memuat state dari DB: {e}")
        finally:
            conn.close()

    def _save_state_to_db(self):
        conn = self._get_db()
        try:
            conn.execute("""
                UPDATE alert_runtime_states
                SET is_overload_locked = ?,
                    runway_12h_locked = ?,
                    runway_6h_locked = ?,
                    runway_extender_locked = ?,
                    is_cutoff_locked = ?,
                    last_known_saldo = ?,
                    last_alert_timestamp = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (
                int(self._is_overload_locked),
                int(self._runway_12h_locked),
                int(self._runway_6h_locked),
                int(self._runway_extender_locked),
                int(self._is_cutoff_locked),
                float(self._last_known_saldo),
            ))
            conn.commit()
        except Exception as e:
            print(f"[WA] Gagal menyimpan runtime state ke DB: {e}")
        finally:
            conn.close()

    def get_config(self) -> Dict[str, Any]:
        """Ambil konfigurasi notifikasi dari DB."""
        conn = self._get_db()
        try:
            row = conn.execute("SELECT * FROM notification_configs WHERE id = 1").fetchone()
            if row:
                return dict(row)
            return {
                "recipient_phone": "",
                "alert_overload_enabled": 1,
                "overload_threshold_va": 1300.0,
                "alert_runway_12h_enabled": 1,
                "alert_runway_6h_enabled": 1,
                "alert_dawn_risk_enabled": 1,
                "alert_cutoff_enabled": 1,
                "alert_topup_enabled": 1,
                "daily_digest_enabled": 1,
                "daily_digest_time": "20:00",
            }
        finally:
            conn.close()

    def update_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Perbarui konfigurasi notifikasi di DB."""
        conn = self._get_db()
        try:
            phone = clean_phone_number(cfg.get("recipient_phone", ""))
            conn.execute("""
                UPDATE notification_configs
                SET recipient_phone = ?,
                    alert_overload_enabled = ?,
                    overload_threshold_va = ?,
                    alert_runway_12h_enabled = ?,
                    alert_runway_6h_enabled = ?,
                    alert_dawn_risk_enabled = ?,
                    alert_cutoff_enabled = ?,
                    alert_topup_enabled = ?,
                    daily_digest_enabled = ?,
                    daily_digest_time = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            """, (
                phone,
                1 if cfg.get("alert_overload_enabled") else 0,
                float(cfg.get("overload_threshold_va", 1300.0)),
                1 if cfg.get("alert_runway_12h_enabled") else 0,
                1 if cfg.get("alert_runway_6h_enabled") else 0,
                1 if cfg.get("alert_dawn_risk_enabled") else 0,
                1 if cfg.get("alert_cutoff_enabled") else 0,
                1 if cfg.get("alert_topup_enabled") else 0,
                1 if cfg.get("alert_daily_digest_enabled") or cfg.get("daily_digest_enabled") else 0,
                str(cfg.get("daily_digest_time", "20:00"))[:5],
            ))
            conn.commit()
            self._config_cache = None
            return self.get_config()
        finally:
            conn.close()

    def log_notification(self, category: str, recipient: str, content: str, status: str,
                         msg_id: str = "", error: str = ""):
        """Catat riwayat audit trail notifikasi ke DB."""
        conn = self._get_db()
        try:
            conn.execute("""
                INSERT INTO notification_logs
                (category, recipient, message_content, status, waha_message_id, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (category, recipient, content, status, msg_id, error))
            conn.commit()
        except Exception as e:
            print(f"[WA] Gagal menyimpan log notifikasi: {e}")
        finally:
            conn.close()

    async def _send_and_log(self, category: str, recipient: str, text: str) -> bool:
        """Kirim pesan dan catat log audit trail secara asinkron."""
        if not recipient:
            return False

        ok, detail = await self.waha.send_text(recipient, text)
        status = "DELIVERED" if ok else "FAILED"
        msg_id = detail if ok else ""
        err = "" if ok else detail

        await asyncio.to_thread(self.log_notification, category, recipient, text, status, msg_id, err)
        if ok:
            print(f"[WA] Berhasil mengirim alert [{category}] ke {recipient}")
        else:
            print(f"[WA] Gagal mengirim alert [{category}]: {detail}")
        return ok

    # ----------------------------------------------------------------------
    # Pengecekan Telemetri Real-Time (Non-Blocking)
    # ----------------------------------------------------------------------
    async def on_telemetry(self, record: Dict[str, Any], relay_state: bool):
        """Evaluasi stream data sensor MQTT 2 detik."""
        if self._lock.locked():
            return

        async with self._lock:
            config = await asyncio.to_thread(self.get_config)
            recipient = config.get("recipient_phone")
            if not recipient:
                return  # Belum ada nomor penerima yang disetel

            s_va = float(record.get("apparent_power") or 0.0)
            p_watt = float(record.get("power") or 0.0)
            saldo = float(record.get("saldo_kwh") or 0.0)
            runway = float(record.get("runway_hours") or 999.0)
            q1 = float(record.get("q1") or 0.0)
            threshold_va = float(config.get("overload_threshold_va") or 1300.0)

            state_changed = False

            # 1. Evaluasi Beban Kritis (>= 85%) dengan Hysteresis (Reset < 70%)
            if config.get("alert_overload_enabled"):
                high_threshold = 0.85 * threshold_va
                recovery_threshold = 0.70 * threshold_va

                if s_va >= high_threshold:
                    self._overload_consecutive_count += 1
                    # Cegah tarikan sesaat: butuh minimal 2 sampel berturut-turut (>= 4 detik)
                    if self._overload_consecutive_count >= 2 and not self._is_overload_locked:
                        load_pct = (s_va / threshold_va) * 100.0 if threshold_va > 0 else 0.0
                        msg = (
                            "⚠️ *PERINGATAN: BEBAN LISTRIK KRITIS!*\n\n"
                            f"Daya saat ini: *{s_va:.0f} VA* ({load_pct:.1f}% dari batas {threshold_va:.0f} VA)\n"
                            f"Daya Aktif: *{p_watt:.0f} Watt*\n\n"
                            "Segera kurangi beban sekunder (AC, pompa air, setrika) untuk mencegah MCB trip!"
                        )
                        asyncio.create_task(self._send_and_log("OVERLOAD", recipient, msg))
                        self._is_overload_locked = True
                        state_changed = True
                elif s_va < recovery_threshold:
                    self._overload_consecutive_count = 0
                    if self._is_overload_locked:
                        self._is_overload_locked = False
                        state_changed = True
                else:
                    # Di antara 70% dan 85%: pertahankan status terkunci (suppress / hysteresis zone)
                    pass

            # 2. Evaluasi Prediksi Runway Menipis (< 12 Jam & < 6 Jam)
            if saldo > 0.001:
                # Milestone Warning: < 12 Jam
                if config.get("alert_runway_12h_enabled"):
                    if runway < 12.0 and not self._runway_12h_locked:
                        msg = (
                            "⚡ *PERINGATAN: SISA KUOTA LISTRIK MENIPIS*\n\n"
                            f"Sisa waktu pemakaian listrik diproyeksikan tinggal: *{runway:.1f} Jam*\n"
                            f"Sisa Saldo: *{saldo:.2f} kWh*\n\n"
                            "Pertimbangkan untuk melakukan isi ulang token prabayar Anda."
                        )
                        asyncio.create_task(self._send_and_log("RUNWAY_12H", recipient, msg))
                        self._runway_12h_locked = True
                        state_changed = True

                # Milestone Emergency: < 6 Jam
                if config.get("alert_runway_6h_enabled"):
                    if runway < 6.0 and not self._runway_6h_locked:
                        msg = (
                            "🚨 *DARURAT: KUOTA LISTRIK SEGERA HABIS*\n\n"
                            f"Perkiraan listrik bertahan: *{runway:.1f} Jam* (< 6 Jam tersisa)\n"
                            f"Sisa Saldo: *{saldo:.2f} kWh*\n\n"
                            "Segera lakukan pengisian token listrik agar listrik tidak padam tiba-tiba!"
                        )
                        asyncio.create_task(self._send_and_log("RUNWAY_6H", recipient, msg))
                        self._runway_6h_locked = True
                        state_changed = True

                # Buka kunci jika runway membaik (> 14 jam)
                if runway >= 14.0 and (self._runway_12h_locked or self._runway_6h_locked):
                    self._runway_12h_locked = False
                    self._runway_6h_locked = False
                    state_changed = True

            # 2b. Evaluasi Risiko Listrik Padam Sebelum Fajar (Runway Extender)
            # Diadopsi langsung dari DSS Rule Engine (main.py): saldo < q1 pada pukul 21:00 - 05:59
            if config.get("alert_dawn_risk_enabled") and saldo > 0.001:
                current_hour = datetime.now().hour
                if (saldo < q1) and (current_hour >= 21 or current_hour <= 5):
                    if not self._runway_extender_locked:
                        hours_left = (7 - current_hour) if current_hour <= 5 else (7 + 24 - current_hour)
                        safe_cap = (saldo * 1000.0) / max(1.0, hours_left)
                        msg = (
                            "🌙 *PERINGATAN: RISIKO LISTRIK PADAM SEBELUM FAJAR*\n\n"
                            f"Sisa kuota: *{saldo:.2f} kWh* diproyeksikan habis sebelum pukul 07:00 pagi.\n"
                            f"Kebutuhan estimasi kuadran dini hari (Q1): *{q1:.2f} kWh*.\n"
                            f"Daya aktif saat ini: *{p_watt:.0f} Watt*.\n\n"
                            f"💡 *Saran Tindakan:*\n"
                            f"Batasi total pemakaian beban di bawah *{safe_cap:.0f} Watt* (matikan AC/beban berat) agar listrik bertahan hingga pagi!"
                        )
                        asyncio.create_task(self._send_and_log("DAWN_RISK", recipient, msg))
                        self._runway_extender_locked = True
                        state_changed = True
                elif (current_hour >= 7 and current_hour < 21) or (saldo >= q1):
                    # Reset lock di luar rentang malam atau jika saldo telah melampaui q1
                    if self._runway_extender_locked:
                        self._runway_extender_locked = False
                        state_changed = True

            # 3. Deteksi Saldo Habis & Relay Cut-Off
            if config.get("alert_cutoff_enabled"):
                if saldo <= 0.0001 and (relay_state is False):
                    if not self._is_cutoff_locked:
                        msg = (
                            "⛔ *LISTRIK PADAM: SALDO TOKEN HABIS*\n\n"
                            "Saldo token listrik Anda telah *0.00 kWh*.\n"
                            "Relay utama JAIRU telah memutus beban listrik untuk pengamanan.\n\n"
                            "Silakan isi token via keypad fisik ESP32 untuk menyalakan kembali daya listrik."
                        )
                        asyncio.create_task(self._send_and_log("CUTOFF", recipient, msg))
                        self._is_cutoff_locked = True
                        state_changed = True
                elif saldo > 0.05 and self._is_cutoff_locked:
                    self._is_cutoff_locked = False
                    state_changed = True

            # 4. Deteksi Konfirmasi Top-Up Token Berhasil
            if config.get("alert_topup_enabled"):
                if self._last_known_saldo > 0.0:
                    delta_saldo = saldo - self._last_known_saldo
                    if delta_saldo >= 0.5:  # Peningkatan saldo minimal 0.5 kWh
                        msg = (
                            "✅ *TOP-UP TOKEN BERHASIL DITERIMA*\n\n"
                            f"Penambahan Kuota: *+{delta_saldo:.2f} kWh*\n"
                            f"Total Saldo Aktif: *{saldo:.2f} kWh*\n"
                            f"Estimasi Runway Baru: *{runway:.1f} Jam*\n\n"
                            "Terima kasih, sistem kembali memantau pemakaian listrik rumah Anda."
                        )
                        asyncio.create_task(self._send_and_log("TOPUP", recipient, msg))
                        # Reset seluruh milestone lock setelah top-up
                        self._runway_12h_locked = False
                        self._runway_6h_locked = False
                        self._runway_extender_locked = False
                        self._is_cutoff_locked = False
                        state_changed = True

            # Perbarui status memori terakhir
            self._last_known_saldo = saldo
            self._last_relay_state = relay_state

            if state_changed:
                await asyncio.to_thread(self._save_state_to_db)

    # ----------------------------------------------------------------------
    # 3. Daily Digest Scheduler & LLM Generation
    # ----------------------------------------------------------------------
    async def run_daily_digest(self, force: bool = False) -> Dict[str, Any]:
        """Jalankan pembuatan ringkasan harian dan kirimkan via WhatsApp."""
        config = await asyncio.to_thread(self.get_config)
        recipient = config.get("recipient_phone")
        if not recipient:
            return {"status": "error", "message": "Nomor WhatsApp belum dikonfigurasi"}

        today_str = datetime.now().strftime("%Y-%m-%d")
        if not force and self._last_digest_date == today_str:
            return {"status": "skipped", "message": "Laporan harian hari ini sudah terkirim"}

        # 1. Agregasi data 24 jam terakhir dari database
        agg = await asyncio.to_thread(self._aggregate_24h_data)

        # 2. Buat narasi ringkasan cerdas via LLM
        narrative = await self._generate_llm_narrative(agg)

        # 3. Format pesan WhatsApp
        q = agg.get("tinyml_q", [0, 0, 0, 0])
        msg = (
            "📊 *LAPORAN ENERGI HARIAN JAIRU*\n"
            f"📅 Tanggal: *{today_str}*\n\n"
            f"• Total Konsumsi: *{agg['total_kwh']:.2f} kWh* (~Rp {agg['estimated_cost_rp']:,})\n"
            f"• Daya Puncak: *{agg['peak_watt']:.0f} W* ({agg['peak_va']:.0f} VA)\n"
            f"• Rata-rata Power Factor: *{agg['avg_pf']:.1f}%*\n"
            f"• Sisa Saldo: *{agg['end_saldo_kwh']:.2f} kWh*\n\n"
            "🔮 *Proyeksi TinyML 24 Jam:*\n"
            f"• Dini Hari (00-06): {q[0]:.2f} kWh\n"
            f"• Pagi-Siang (06-12): {q[1]:.2f} kWh\n"
            f"• Siang-Sore (12-18): {q[2]:.2f} kWh\n"
            f"• Malam (18-24): {q[3]:.2f} kWh\n\n"
            "💡 *Analisis Efisiensi & Rekomendasi:*\n"
            f"{narrative}"
        )

        # 4. Kirim via WAHA
        ok = await self._send_and_log("DAILY_DIGEST", recipient, msg)
        delivery_status = "DELIVERED" if ok else "FAILED"

        # 5. Simpan record ringkasan ke database
        await asyncio.to_thread(self._save_daily_digest_record, today_str, agg, narrative, delivery_status)
        if ok:
            self._last_digest_date = today_str
            return {"status": "success", "message": "Laporan harian berhasil dikirim"}
        return {"status": "failed", "message": "Gagal mengirim via WAHA"}

    def _aggregate_24h_data(self) -> Dict[str, Any]:
        conn = self._get_db()
        try:
            now = datetime.now()
            start = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            end = now.strftime("%Y-%m-%d %H:%M:%S")

            rows = conn.execute("""
                SELECT timestamp, voltage, current, power, apparent_power, pf, saldo_kwh,
                       runway_hours, q1, q2, q3, q4
                FROM telemetry_history
                WHERE timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
            """, (start, end)).fetchall()

            data = [dict(r) for r in rows]
            if not data:
                return {
                    "total_kwh": 0.0,
                    "estimated_cost_rp": 0,
                    "peak_watt": 0.0,
                    "peak_va": 0.0,
                    "avg_pf": 100.0,
                    "end_saldo_kwh": 0.0,
                    "tinyml_q": [0.0, 0.0, 0.0, 0.0]
                }

            # Integrasi trapesium kWh
            total_ws = 0.0
            prev_t, prev_p = None, 0.0
            for r in data:
                try:
                    t = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                p = float(r["power"] or 0.0)
                if prev_t is not None:
                    dt = (t - prev_t).total_seconds()
                    if 0 < dt <= 300:
                        total_ws += (prev_p + p) / 2.0 * dt
                prev_t, prev_p = t, p

            total_kwh = round(total_ws / 3_600_000.0, 3)
            peak_watt = max((float(d["power"] or 0.0) for d in data), default=0.0)
            peak_va = max((float(d["apparent_power"] or 0.0) for d in data), default=0.0)

            pf_vals = [float(d["pf"]) for d in data if (float(d["apparent_power"] or 0.0) > 0.5 and d["pf"] is not None)]
            avg_pf = round(sum(pf_vals) / len(pf_vals), 1) if pf_vals else 0.0

            last_row = data[-1]
            end_saldo = float(last_row.get("saldo_kwh") or 0.0)
            q_vals = [
                float(last_row.get("q1") or 0.0),
                float(last_row.get("q2") or 0.0),
                float(last_row.get("q3") or 0.0),
                float(last_row.get("q4") or 0.0),
            ]

            return {
                "total_kwh": total_kwh,
                "estimated_cost_rp": int(round(total_kwh * self.tarif_per_kwh)),
                "peak_watt": round(peak_watt, 1),
                "peak_va": round(peak_va, 1),
                "avg_pf": avg_pf,
                "end_saldo_kwh": round(end_saldo, 2),
                "tinyml_q": q_vals
            }
        finally:
            conn.close()

    async def _generate_llm_narrative(self, agg: Dict[str, Any]) -> str:
        """Kirim prompt ke LLM untuk menghasilkan narasi ringkas dalam Bahasa Indonesia."""
        if not LLM_API_KEY:
            return self._fallback_narrative(agg)

        prompt = f"""Anda adalah asisten cerdas pemantau energi listrik rumah tangga JAIRU.
Tuliskan analisis singkat (maksimal 3-4 kalimat padat) untuk pesan WhatsApp harian pengguna:
Data Konsumsi 24 Jam Terakhir:
- Total Pemakaian: {agg['total_kwh']:.2f} kWh (Estimasi Biaya: Rp {agg['estimated_cost_rp']:,})
- Beban Puncak: {agg['peak_watt']:.0f} W (Daya Semu: {agg['peak_va']:.0f} VA)
- Rata-rata Power Factor: {agg['avg_pf']:.1f}%
- Sisa Saldo Token: {agg['end_saldo_kwh']:.2f} kWh
- Proyeksi Kuadran TinyML: Q1={agg['tinyml_q'][0]:.2f} kWh, Q2={agg['tinyml_q'][1]:.2f} kWh, Q3={agg['tinyml_q'][2]:.2f} kWh, Q4={agg['tinyml_q'][3]:.2f} kWh.

Ketentuan:
1. Berikan evaluasi apakah konsumsi stabil atau tinggi.
2. Jika Power Factor di bawah 80%, ingatkan potensi kerugian daya.
3. Berikan saran praktis alat mana yang perlu dijadwalkan ulang (load shifting) mengacu pada jam proyeksi TinyML tertinggi.
4. Gunakan bahasa Indonesia santun, lugas, tanpa tanda bintang tebal berlebihan."""

        try:
            return await asyncio.to_thread(self._call_llm_text, prompt)
        except Exception as e:
            print(f"[WA] LLM gagal menghasilkan narasi: {e}")
            return self._fallback_narrative(agg)

    def _call_llm_text(self, prompt: str) -> str:
        if LLM_PROVIDER == "openai":
            url = f"{LLM_BASE_URL}/chat/completions"
            body = {
                "model": LLM_MODEL,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": "Anda adalah penasihat efisiensi energi listrik rumah tangga di Indonesia."},
                    {"role": "user", "content": prompt}
                ]
            }
            headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{LLM_MODEL}:generateContent"
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
            headers = {"x-goog-api-key": LLM_API_KEY, "Content-Type": "application/json"}

        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=18) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        if LLM_PROVIDER == "openai":
            return payload["choices"][0]["message"]["content"].strip()
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()

    def _fallback_narrative(self, agg: Dict[str, Any]) -> str:
        """Template teks deterministik jika pemanggilan LLM gagal / tidak ada API key."""
        kwh = agg.get("total_kwh", 0.0)
        saldo = agg.get("end_saldo_kwh", 0.0)
        pf = agg.get("avg_pf", 100.0)

        saran = "Pemakaian listrik harian terpantau dalam kondisi wajar. "
        if pf < 80.0:
            saran += "Faktor daya rata-rata tergolong rendah, periksa peralatan induktif seperti pompa atau kompresor. "
        if saldo < 5.0:
            saran += "Saldo token mulai menipis, pertimbangkan pengisian ulang dalam waktu dekat."
        else:
            saran += "Gunakan beban berat seperti setrika atau mesin cuci secara bergantian untuk menghemat daya."
        return saran

    def _save_daily_digest_record(self, report_date: str, agg: Dict[str, Any], narrative: str, status: str):
        conn = self._get_db()
        try:
            q = agg.get("tinyml_q", [0, 0, 0, 0])
            conn.execute("""
                INSERT INTO daily_energy_digests
                (report_date, total_kwh, estimated_cost_rp, peak_watt, peak_va, avg_power_factor,
                 end_saldo_kwh, tinyml_q1_avg, tinyml_q2_avg, tinyml_q3_avg, tinyml_q4_avg,
                 llm_narrative, delivery_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_date) DO UPDATE SET
                    total_kwh = excluded.total_kwh,
                    estimated_cost_rp = excluded.estimated_cost_rp,
                    peak_watt = excluded.peak_watt,
                    peak_va = excluded.peak_va,
                    avg_power_factor = excluded.avg_power_factor,
                    end_saldo_kwh = excluded.end_saldo_kwh,
                    llm_narrative = excluded.llm_narrative,
                    delivery_status = excluded.delivery_status
            """, (
                report_date, agg["total_kwh"], agg["estimated_cost_rp"], agg["peak_watt"], agg["peak_va"],
                agg["avg_pf"], agg["end_saldo_kwh"], q[0], q[1], q[2], q[3], narrative, status
            ))
            conn.commit()
        except Exception as e:
            print(f"[WA] Gagal menyimpan record daily digest: {e}")
        finally:
            conn.close()

    # ----------------------------------------------------------------------
    # 4. Background Scheduler Loop
    # ----------------------------------------------------------------------
    async def scheduler_loop(self):
        """Pemeriksaan berkala tiap 30 detik untuk penjadwalan laporan harian."""
        while True:
            try:
                config = await asyncio.to_thread(self.get_config)
                if config.get("daily_digest_enabled"):
                    target_time = str(config.get("daily_digest_time", "20:00"))[:5]
                    current_time = datetime.now().strftime("%H:%M")
                    today_str = datetime.now().strftime("%Y-%m-%d")

                    if current_time == target_time and self._last_digest_date != today_str:
                        print(f"[WA] Pemicu Laporan Harian otomatis aktif ({current_time})...")
                        await self.run_daily_digest(force=False)
            except Exception as e:
                print(f"[WA] Kesalahan scheduler harian: {e}")

            await asyncio.sleep(30)
