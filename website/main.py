import os
import io
import csv
import json
import math
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

# Muat file .env otomatis jika ada (tanpa memerlukan dependensi eksternal)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#"):
                    continue
                if "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k, _v = _k.strip(), _v.strip().strip("'\"")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
    except Exception as _e:
        print(f"[!] Gagal membaca .env: {_e}")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
import paho.mqtt.client as mqtt

# [LLM] Penasihat LLM (Tier 3) — modul tambahan
from recommender_engine import LLMAdvisor
# [WhatsApp] Gateway WAHA & Engine Alert
from whatsapp_notifier import WhatsAppAlertEngine

# Konfigurasi Template
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

def resolve_db_path() -> str:
    env_path = os.getenv("DB_PATH")
    if env_path:
        path = env_path
    else:
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        data_db = os.path.join(data_dir, "power_meter.db")
        root_db = os.path.join(os.path.dirname(__file__), "power_meter.db")
        if os.path.exists(data_db) or os.path.isdir(data_dir):
            path = data_db
        else:
            path = root_db

    # Proteksi: Jika path berupa direktori (misal Docker me-mount volume ke nama folder), arahkan ke file di dalamnya
    if os.path.exists(path) and os.path.isdir(path):
        path = os.path.join(path, "power_meter.db")
    return path

DB_PATH = resolve_db_path()

# Daya kontrak rumah & tarif (dapat disesuaikan via .env)
KONTRAK_DAYA_VA: float = float(os.getenv("KONTRAK_DAYA_VA", 1300.0))
TARIF_PER_KWH: float = float(os.getenv("TARIF_PER_KWH", 1444.70))

# Jeda antar sampel terbesar (detik) yang masih dianggap pengukuran kontinu
# saat menghitung energi. Jeda lebih besar (alat mati) tidak dihitung.
MAX_GAP_SECONDS = int(os.getenv("MAX_GAP_SECONDS", 300))

TELEMETRY_FIELDS = [
    "voltage", "current", "power", "apparent_power", "pf",
    "saldo_kwh", "runway_hours", "q1", "q2", "q3", "q4",
]

# Event loop utama FastAPI (diisi saat startup, dipakai thread MQTT)
main_loop: Optional[asyncio.AbstractEventLoop] = None
# Menandai apakah sudah ada data nyata dari perangkat
data_received: bool = False


# --------------------------------------------------------------------------
# Basis Data SQLite & Inisialisasi
# --------------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    global DB_PATH
    DB_PATH = resolve_db_path()
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception as _e:
            print(f"[!] Gagal membuat direktori database {db_dir}: {_e}")

    # Migrasi / Seed otomatis: Jika target DB belum ada, tapi power_meter.db lama ada di root
    if not os.path.exists(DB_PATH):
        root_db = os.path.join(os.path.dirname(__file__), "power_meter.db")
        if os.path.exists(root_db) and os.path.isfile(root_db) and os.path.abspath(root_db) != os.path.abspath(DB_PATH):
            try:
                import shutil
                shutil.copy2(root_db, DB_PATH)
                print(f"[*] Berhasil menyalin database awal dari {root_db} ke {DB_PATH}")
            except Exception as _e:
                print(f"[!] Gagal menyalin database awal: {_e}")
    try:
        conn = get_db_connection()
    except sqlite3.OperationalError as _e:
        print(f"[!] Gagal membuka SQLite pada '{DB_PATH}': {_e}")
        fallback_path = os.path.join(os.path.dirname(__file__), "power_meter.db")
        if DB_PATH != fallback_path and not os.path.isdir(fallback_path):
            print(f"[*] Menggunakan fallback database lokal: {fallback_path}")
            DB_PATH = fallback_path
            conn = get_db_connection()
        else:
            raise _e
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            voltage REAL,
            current REAL,
            power REAL,
            apparent_power REAL,
            pf REAL,
            saldo_kwh REAL,
            runway_hours REAL,
            q1 REAL,
            q2 REAL,
            q3 REAL,
            q4 REAL
        )
    """)
    # Index agar query rentang tanggal tetap cepat saat data menumpuk
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry_history(timestamp)")

    # ----------------------------------------------------------------------
    # Tabel-tabel Notifikasi WhatsApp & Audit Trail
    # ----------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_configs (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            recipient_phone VARCHAR(25) NOT NULL DEFAULT '',
            alert_overload_enabled BOOLEAN DEFAULT 1,
            overload_threshold_va REAL DEFAULT 1300.0,
            alert_runway_12h_enabled BOOLEAN DEFAULT 1,
            alert_runway_6h_enabled BOOLEAN DEFAULT 1,
            alert_dawn_risk_enabled BOOLEAN DEFAULT 1,
            alert_cutoff_enabled BOOLEAN DEFAULT 1,
            alert_topup_enabled BOOLEAN DEFAULT 1,
            daily_digest_enabled BOOLEAN DEFAULT 1,
            daily_digest_time VARCHAR(5) DEFAULT '20:00',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migrasi kolom alert_dawn_risk_enabled jika tabel sudah ada sebelumnya
    try:
        cursor.execute("ALTER TABLE notification_configs ADD COLUMN alert_dawn_risk_enabled BOOLEAN DEFAULT 1")
    except Exception:
        pass
    cursor.execute("""
        INSERT OR IGNORE INTO notification_configs
        (id, recipient_phone, alert_overload_enabled, overload_threshold_va, alert_runway_12h_enabled, alert_runway_6h_enabled, alert_dawn_risk_enabled, alert_cutoff_enabled, alert_topup_enabled, daily_digest_enabled, daily_digest_time)
        VALUES (1, '', 1, 1300.0, 1, 1, 1, 1, 1, 1, '20:00')
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alert_runtime_states (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            is_overload_locked BOOLEAN DEFAULT 0,
            runway_12h_locked BOOLEAN DEFAULT 0,
            runway_6h_locked BOOLEAN DEFAULT 0,
            runway_extender_locked BOOLEAN DEFAULT 0,
            is_cutoff_locked BOOLEAN DEFAULT 0,
            last_known_saldo REAL DEFAULT 0.0,
            last_alert_timestamp DATETIME
        )
    """)
    # Migrasi kolom runway_extender_locked jika tabel sudah ada sebelumnya
    try:
        cursor.execute("ALTER TABLE alert_runtime_states ADD COLUMN runway_extender_locked BOOLEAN DEFAULT 0")
    except Exception:
        pass
    cursor.execute("""
        INSERT OR IGNORE INTO alert_runtime_states
        (id, is_overload_locked, runway_12h_locked, runway_6h_locked, runway_extender_locked, is_cutoff_locked, last_known_saldo)
        VALUES (1, 0, 0, 0, 0, 0, 0.0)
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            category VARCHAR(30) NOT NULL,
            recipient VARCHAR(25) NOT NULL,
            message_content TEXT NOT NULL,
            status VARCHAR(15) NOT NULL,
            waha_message_id VARCHAR(100),
            error_message TEXT,
            retry_count INTEGER DEFAULT 0
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notif_logs_ts ON notification_logs(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notif_logs_category ON notification_logs(category)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_energy_digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date DATE UNIQUE NOT NULL,
            total_kwh REAL NOT NULL,
            estimated_cost_rp INTEGER NOT NULL,
            peak_watt REAL NOT NULL,
            peak_va REAL NOT NULL,
            avg_power_factor REAL NOT NULL,
            end_saldo_kwh REAL NOT NULL,
            tinyml_q1_avg REAL,
            tinyml_q2_avg REAL,
            tinyml_q3_avg REAL,
            tinyml_q4_avg REAL,
            llm_narrative TEXT,
            delivery_status VARCHAR(15) DEFAULT 'PENDING',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_digest_date ON daily_energy_digests(report_date)")

    conn.commit()
    conn.close()


init_db()


# --------------------------------------------------------------------------
# Decision Support System (Rule Engine Deterministik)
# --------------------------------------------------------------------------
def evaluate_dss(data: Dict[str, Any]) -> Dict[str, Any]:
    if not data_received:
        return {
            "status": "OPTIMAL",
            "badge_color": "emerald",
            "title": "MENUNGGU DATA PERANGKAT",
            "message": "Belum ada telemetri yang diterima dari smart power meter.",
            "excess_watt": 0.0
        }

    s_va = data.get("apparent_power", 0.0)
    p_watt = data.get("power", 0.0)
    saldo = data.get("saldo_kwh", 0.0)
    runway = data.get("runway_hours", 999.0)
    q1 = data.get("q1", 0.0)
    total_24h = q1 + data.get("q2", 0.0) + data.get("q3", 0.0) + data.get("q4", 0.0)
    current_hour = datetime.now().hour

    # 1. Proteksi Batas MCB PLN (Daya Semu VA)
    if s_va >= (0.85 * KONTRAK_DAYA_VA):
        excess_va = max(0.0, s_va - (0.75 * KONTRAK_DAYA_VA))
        return {
            "status": "DANGER",
            "badge_color": "rose",
            "title": "BAHAYA: BEBAN KRITIS MCB",
            "message": f"Daya semu ({s_va:.0f} VA) mencapai {(s_va/KONTRAK_DAYA_VA)*100:.1f}% batas kontrak ({KONTRAK_DAYA_VA:.0f} VA). Kurangi beban sekunder sekitar {excess_va:.0f} VA untuk mencegah trip.",
            "excess_watt": round(excess_va, 1)
        }

    # 2. Runway Extender (Risiko Padam Dini Hari)
    if (saldo < q1) and (current_hour >= 21 or current_hour <= 5):
        hours_left = (7 - current_hour) if current_hour <= 5 else (7 + 24 - current_hour)
        safe_cap = (saldo * 1000.0) / max(1.0, hours_left)
        return {
            "status": "WARNING",
            "badge_color": "amber",
            "title": "PERINGATAN: RISIKO PADAM SEBELUM FAJAR",
            "message": f"Sisa kuota ({saldo:.2f} kWh) diproyeksikan habis sebelum jam 07:00. Batasi beban di bawah {safe_cap:.0f} W agar listrik bertahan.",
            "excess_watt": max(0.0, p_watt - safe_cap)
        }

    # 3. Top-Up Advisor (Manajemen Anggaran Token)
    if saldo <= 2.0:
        daily_est = max(1.0, total_24h)
        est_cost = daily_est * 7 * TARIF_PER_KWH
        token_saran = "Rp 50.000" if est_cost > 35000 else "Rp 20.000"
        return {
            "status": "INFO",
            "badge_color": "sky",
            "title": "SARAN: SALDO TOKEN MENIPIS",
            "message": f"Saldo tersisa {saldo:.2f} kWh. Berdasarkan pola konsumsi TinyML ({daily_est:.2f} kWh/hari), disarankan isi token {token_saran}.",
            "excess_watt": 0.0
        }

    # 4. Kondisi Normal
    return {
        "status": "OPTIMAL",
        "badge_color": "emerald",
        "title": "SISTEM NORMAL & AMAN",
        "message": f"Beban listrik stabil. Estimasi cadangan kuota mencukupi hingga {runway:.1f} jam ke depan.",
        "excess_watt": 0.0
    }


# --------------------------------------------------------------------------
# WebSocket Connection Manager
# --------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()

# Status relay terakhir yang diketahui (dari MQTT publish atau perintah user)
relay_state: bool = False

# Buffer telemetri real-time terakhir (tanpa field dss)
latest_telemetry: Dict[str, Any] = {
    "voltage": 220.0,
    "current": 0.0,
    "power": 0.0,
    "apparent_power": 0.0,
    "pf": 0.0,
    "saldo_kwh": 0.0,
    "runway_hours": 0.0,
    "q1": 0.0,
    "q2": 0.0,
    "q3": 0.0,
    "q4": 0.0,
    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
}


# [LLM] Instans penasihat LLM. Memakai hasil evaluate_dss() sebagai konteks;
# aturan deterministik tidak diubah.
advisor = LLMAdvisor(KONTRAK_DAYA_VA, TARIF_PER_KWH)

# [WhatsApp] Instans engine alert WhatsApp WAHA
whatsapp_notifier = WhatsAppAlertEngine(resolve_db_path, TARIF_PER_KWH)


# --------------------------------------------------------------------------
# Komunikasi MQTT (HiveMQ Cloud / Broker Lokal)
# --------------------------------------------------------------------------
MQTT_BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "jairu/powermeter/telemetry")
MQTT_RELAY_TOPIC = os.getenv("MQTT_RELAY_TOPIC", "jairu/powermeter/relay/control")


def build_packet(record: Dict[str, Any]) -> Dict[str, Any]:
    """Paket siaran WebSocket = telemetri ESP32 apa adanya + hasil DSS + metadata."""
    packet = dict(record)
    packet["dss"] = evaluate_dss(record)
    packet["kontrak_daya_va"] = KONTRAK_DAYA_VA
    packet["tarif_per_kwh"] = TARIF_PER_KWH
    packet["device_ready"] = data_received
    packet["relay_on"] = relay_state
    try:
        ts = datetime.strptime(record["timestamp"], "%Y-%m-%d %H:%M:%S")
        packet["age_s"] = round(max(0.0, (datetime.now() - ts).total_seconds()), 1)
    except (KeyError, ValueError):
        packet["age_s"] = 0.0
    return packet


def _to_float(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def on_mqtt_connect(client, userdata, flags, rc):
    print(f"[*] Terhubung ke MQTT Broker ({MQTT_BROKER}:{MQTT_PORT}) dengan status kode: {rc}")
    # Subscribe di sini agar otomatis berlangganan ulang setelah reconnect
    client.subscribe(MQTT_TOPIC)


def on_mqtt_message(client, userdata, msg):
    global latest_telemetry, data_received
    try:
        raw = json.loads(msg.payload.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("payload bukan objek JSON")

        # Normalisasi: semua field numerik dijamin ada dan valid
        record = {k: _to_float(raw.get(k), 0.0) for k in TELEMETRY_FIELDS}
        # ESP32 mengirim pf dalam PERSEN (0-100), simpan apa adanya
        record["pf"] = min(100.0, max(0.0, record["pf"]))
        if "apparent_power" not in raw:
            record["apparent_power"] = record["voltage"] * record["current"]

        # Ambil waktu dari ESP32 jika valid, jika kosong baru buat waktu lokal dari server
        raw_timestamp = raw.get("timestamp")
        if raw_timestamp and str(raw_timestamp).strip():
            now_str = str(raw_timestamp).strip()
        else:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        record["timestamp"] = now_str

        # Simpan ke memori lokal (salinan bersih tanpa dss)
        latest_telemetry = dict(record)
        data_received = True

        # Paket siaran = telemetri + hasil DSS
        packet = build_packet(record)

        # [LLM & WhatsApp] Pemicu otomatis (transisi status / siklus berkala). Non-blocking:
        # hanya menjadwalkan coroutine di event loop utama.
        if main_loop is not None and main_loop.is_running():
            asyncio.run_coroutine_threadsafe(advisor.on_telemetry(record, packet["dss"]), main_loop)
            asyncio.run_coroutine_threadsafe(whatsapp_notifier.on_telemetry(record, relay_state), main_loop)

        # Simpan ke database SQLite
        conn = get_db_connection()
        try:
            conn.execute("""
                INSERT INTO telemetry_history
                (timestamp, voltage, current, power, apparent_power, pf, saldo_kwh, runway_hours, q1, q2, q3, q4)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_str,
                record["voltage"], record["current"], record["power"],
                record["apparent_power"], record["pf"], record["saldo_kwh"],
                record["runway_hours"], record["q1"], record["q2"],
                record["q3"], record["q4"],
            ))
            conn.commit()
        finally:
            conn.close()

        # Siarkan via WebSocket ke seluruh browser (event loop utama FastAPI)
        if main_loop is not None and main_loop.is_running():
            asyncio.run_coroutine_threadsafe(manager.broadcast(json.dumps(packet)), main_loop)

    except Exception as e:
        print(f"[!] Gagal memproses payload MQTT: {e}")


# Kompatibel dengan paho-mqtt 1.x maupun 2.x
try:
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
except AttributeError:
    mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_message = on_mqtt_message


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    await whatsapp_notifier.init_state()
    scheduler_task = asyncio.create_task(whatsapp_notifier.scheduler_loop())
    try:
        mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"[!] Peringatan: Tidak dapat memulai koneksi MQTT: {e}")
    yield
    scheduler_task.cancel()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()


app = FastAPI(title="JAIRU Smart Power Meter Dashboard", lifespan=lifespan)


# --------------------------------------------------------------------------
# Endpoint Routes
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "kontrak_daya_va": KONTRAK_DAYA_VA
    })


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    return templates.TemplateResponse(request, "history.html", {
        "kontrak_daya_va": KONTRAK_DAYA_VA
    })


@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    return templates.TemplateResponse(request, "notifications.html", {
        "kontrak_daya_va": KONTRAK_DAYA_VA
    })


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    # Kirimkan data terakhir saat koneksi baru terbuka
    initial_packet = build_packet(latest_telemetry)
    try:
        await websocket.send_text(json.dumps(initial_packet))
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        manager.disconnect(websocket)


def _validate_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Format tanggal harus YYYY-MM-DD")
    return value


def _integrate_kwh(rows: List[Dict[str, Any]]) -> float:
    """Hitung energi (kWh) dengan integrasi trapesium; jeda data besar diabaikan."""
    total_ws = 0.0
    prev_t, prev_p = None, 0.0
    for r in rows:
        t = datetime.fromisoformat(r["timestamp"])
        p = r["power"] or 0.0
        if prev_t is not None:
            dt = (t - prev_t).total_seconds()
            if 0 < dt <= MAX_GAP_SECONDS:
                total_ws += (prev_p + p) / 2.0 * dt
        prev_t, prev_p = t, p
    return total_ws / 3_600_000.0


@app.get("/api/history")
async def get_history_data(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=5000)
):
    start_date, end_date = _validate_date(start_date), _validate_date(end_date)
    if not start_date or not end_date:
        now = datetime.now()
        start = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
    else:
        if start_date > end_date:
            raise HTTPException(status_code=400, detail="Tanggal awal tidak boleh setelah tanggal akhir")
        start = f"{start_date} 00:00:00"
        end = f"{end_date} 23:59:59"

    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT timestamp, voltage, current, power, apparent_power, pf, saldo_kwh,
                   runway_hours, q1, q2, q3, q4
            FROM telemetry_history
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
        """, (start, end)).fetchall()
    finally:
        conn.close()

    data = [dict(row) for row in rows]
    step = max(1, math.ceil(len(data) / limit))
    sampled_data = data[::step]

    if data:
        peak_va = max((d["apparent_power"] or 0.0) for d in data)
        peak_watt = max((d["power"] or 0.0) for d in data)
        # PF (persen) dirata-rata hanya saat ada beban (VA > 0), agar tidak tertarik nol
        pf_rows = [d["pf"] for d in data if (d["apparent_power"] or 0.0) > 0.5 and d["pf"] is not None]
        avg_pf = sum(pf_rows) / len(pf_rows) if pf_rows else 0.0
        total_kwh = round(_integrate_kwh(data), 3)
    else:
        peak_va, peak_watt, avg_pf, total_kwh = 0.0, 0.0, 0.0, 0.0

    return {
        "status": "success",
        "summary": {
            "total_records": len(data),
            "total_kwh": total_kwh,
            "peak_va": round(peak_va, 1),
            "peak_watt": round(peak_watt, 1),
            "avg_pf": round(avg_pf, 1),
            "estimated_cost_rp": int(round(total_kwh * TARIF_PER_KWH)),
            "kontrak_daya_va": KONTRAK_DAYA_VA
        },
        "series": sampled_data,
        "table_data": data[-50:][::-1]
    }


@app.get("/api/history/export")
async def export_history_csv(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None)
):
    start_date, end_date = _validate_date(start_date), _validate_date(end_date)
    start = f"{start_date} 00:00:00" if start_date else (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
    end = f"{end_date} 23:59:59" if end_date else datetime.now().strftime("%Y-%m-%d 23:59:59")

    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT timestamp, voltage, current, power, apparent_power, pf, saldo_kwh,
                   runway_hours, q1, q2, q3, q4
            FROM telemetry_history
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
        """, (start, end)).fetchall()
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Tegangan (V)", "Arus (A)", "Daya Aktif (W)", "Daya Semu (VA)",
                     "Power Factor (%)", "Saldo Token (kWh)", "Runway (Jam)",
                     "Prediksi Q1 0-6j (kWh)", "Prediksi Q2 6-12j (kWh)",
                     "Prediksi Q3 12-18j (kWh)", "Prediksi Q4 18-24j (kWh)"])
    for row in rows:
        writer.writerow([row["timestamp"], row["voltage"], row["current"], row["power"],
                         row["apparent_power"], row["pf"], row["saldo_kwh"], row["runway_hours"],
                         row["q1"], row["q2"], row["q3"], row["q4"]])
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=power_meter_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
    )


# --------------------------------------------------------------------------
# [LLM] Endpoint Saran AI (tambahan)
# --------------------------------------------------------------------------
@app.get("/api/ai-advisory")
async def get_ai_advisory():
    """Saran AI terakhir dari cache (tidak memanggil LLM)."""
    return {"status": "success", "data": advisor.snapshot()}


@app.post("/api/ai-advisory")
async def request_ai_advisory():
    """Permintaan manual (tombol 'Minta Saran AI'). Dibatasi cooldown agar kuota API hemat."""
    if not data_received:
        return {"status": "waiting", "message": "Belum ada telemetri dari perangkat; saran AI belum bisa dibuat."}
    dss = evaluate_dss(latest_telemetry)
    return {"status": "success", "data": await advisor.advise(latest_telemetry, dss)}


# --------------------------------------------------------------------------
# Endpoint Kontrol Relay MQTT
# --------------------------------------------------------------------------
@app.get("/api/relay")
async def get_relay_state():
    """Kembalikan status relay terakhir."""
    return {"status": "success", "relay_on": relay_state}


@app.post("/api/relay/{state}")
async def set_relay(state: str):
    """Publish perintah ON / OFF ke topic relay MQTT.

    state: 'on' atau 'off'
    """
    global relay_state
    state = state.strip().lower()
    if state not in ("on", "off"):
        raise HTTPException(status_code=400, detail="State harus 'on' atau 'off'")

    payload = "ON" if state == "on" else "OFF"
    try:
        mqtt_client.publish(MQTT_RELAY_TOPIC, payload, qos=1)
        relay_state = (state == "on")
        return {"status": "success", "relay_on": relay_state, "published": payload, "topic": MQTT_RELAY_TOPIC}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal publish MQTT: {e}")


# --------------------------------------------------------------------------
# [WhatsApp] Endpoint Gateway WAHA & Notifikasi Otomatis
# --------------------------------------------------------------------------
@app.get("/api/notifications/config")
async def get_notification_config():
    """Ambil konfigurasi notifikasi WhatsApp."""
    return {"status": "success", "data": whatsapp_notifier.get_config()}


@app.put("/api/notifications/config")
async def update_notification_config(request: Request):
    """Simpan perubahan konfigurasi nomor & ambang batas alert."""
    payload = await request.json()
    updated = whatsapp_notifier.update_config(payload)
    return {"status": "success", "data": updated}


@app.post("/api/notifications/test")
async def send_test_notification(request: Request):
    """Kirim pesan uji coba instan ke WhatsApp penerima."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    phone = body.get("recipient_phone") or whatsapp_notifier.get_config().get("recipient_phone")
    if not phone:
        raise HTTPException(status_code=400, detail="Nomor WhatsApp penerima belum diisi")

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S WITA")
    msg = (
        "🔔 *JAIRU SMART POWER METER - TES NOTIFIKASI*\n\n"
        "Halo! Ini adalah pesan uji coba dari sistem IoT JAIRU.\n"
        "Gateway WhatsApp WAHA dan pipeline notifikasi otomatis berhasil terhubung 100%.\n\n"
        f"📅 Waktu: *{now_str}*\n"
        "⚡ Status Pemantauan: *AKTIF 24/7*"
    )
    ok = await whatsapp_notifier._send_and_log("TEST", phone, msg)
    if not ok:
        raise HTTPException(status_code=502, detail="Gagal mengirim pesan melalui gateway WAHA. Pastikan sesi terhubung dan nomor valid.")
    return {"status": "success", "message": f"Pesan uji coba berhasil dikirim ke {phone}"}


@app.post("/api/notifications/simulate/{category}")
async def simulate_notification_rule(category: str, request: Request):
    """Kirim contoh pesan suatu aturan berbasis data telemetri tanpa memengaruhi state alert otomatis."""
    message = await asyncio.to_thread(whatsapp_notifier.build_simulation_message, category.upper())
    if not message:
        raise HTTPException(status_code=404, detail="Kategori simulasi tidak tersedia")

    body = await request.json()
    phone = body.get("recipient_phone") or whatsapp_notifier.get_config().get("recipient_phone")
    if not phone:
        raise HTTPException(status_code=400, detail="Nomor WhatsApp penerima belum diisi")

    ok = await whatsapp_notifier._send_and_log(f"SIMULATION_{category.upper()}", phone, message)
    if not ok:
        raise HTTPException(status_code=502, detail="Gagal mengirim simulasi. Pastikan sesi WhatsApp terhubung dan nomor valid.")
    return {"status": "success", "message": "Pesan simulasi berhasil dikirim ke WhatsApp"}


@app.post("/api/notifications/daily-digest")
async def trigger_manual_daily_digest():
    """Pemicu manual pengiriman laporan ringkasan energi harian AI."""
    result = await whatsapp_notifier.run_daily_digest(force=True)
    return result


@app.get("/api/notifications/logs")
async def get_notification_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    category: Optional[str] = Query(None)
):
    """Ambil riwayat log notifikasi WhatsApp (audit trail)."""
    conn = get_db_connection()
    try:
        query = "SELECT * FROM notification_logs"
        params = []
        if category:
            query += " WHERE category = ?"
            params.append(category)
        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()

        count_q = "SELECT COUNT(*) as total FROM notification_logs"
        count_params = []
        if category:
            count_q += " WHERE category = ?"
            count_params.append(category)
        total_rows = conn.execute(count_q, count_params).fetchone()["total"]

        return {
            "status": "success",
            "total": total_rows,
            "items": [dict(r) for r in rows]
        }
    finally:
        conn.close()


@app.get("/api/waha/session")
async def get_waha_session_status():
    """Periksa status sesi WAHA (WORKING, SCAN_QR_CODE, STOPPED, OFFLINE)."""
    return await whatsapp_notifier.waha.get_session_status()


@app.post("/api/waha/start")
async def start_waha_session():
    """Mulai / jalankan sesi WAHA."""
    return await whatsapp_notifier.waha.start_session()


@app.post("/api/waha/stop")
async def stop_waha_session():
    """Hentikan sesi WAHA."""
    return await whatsapp_notifier.waha.stop_session()


@app.post("/api/waha/logout")
async def logout_waha_session():
    """Putus pairing / logout dari WhatsApp."""
    return await whatsapp_notifier.waha.logout_session()


@app.get("/api/waha/qr")
async def get_waha_qr():
    """Ambil QR Code pairing WAHA langsung dalam format gambar PNG."""
    img_data, mime = await whatsapp_notifier.waha.get_qr_image()
    if img_data:
        return Response(content=img_data, media_type=mime or "image/png")
    raise HTTPException(status_code=404, detail="QR Code belum tersedia atau perangkat sudah terhubung")
