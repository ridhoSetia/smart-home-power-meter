"""
website/recommender_engine.py  --  Penasihat LLM (Tier 3) untuk Smart Power Meter.

Modul ini HANYA menambah lapisan saran berbasis LLM. Aturan deterministik
(evaluate_dss di main.py: Safety Gate MCB 85%, Runway Extender, Top-Up) tetap
menjadi sumber kebenaran dan tidak diubah. LLM menerima hasil DSS tersebut
sebagai konteks dan hanya menghasilkan saran tindakan yang lebih konkret.

Tanpa dependensi baru (hanya pustaka standar Python).

Environment variable:
  LLM_API_KEY        kunci API (alias: GEMINI_API_KEY). Kosong -> mode aturan saja.
  LLM_PROVIDER       "gemini" (default) | "openai"  (openai = API kompatibel OpenAI,
                     mis. Groq / OpenRouter)
  LLM_MODEL          default "gemini-3.1-flash-lite"
  LLM_BASE_URL       wajib bila LLM_PROVIDER=openai, mis.
                     https://api.groq.com/openai/v1  atau  https://openrouter.ai/api/v1
  LLM_PERIODIC_S     refresh berkala bila status sama (default 600 detik)
  LLM_MIN_INTERVAL_S jeda minimum antar panggilan otomatis (default 30 detik)
"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional

# Muat file .env otomatis jika ada
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
    except Exception:
        pass

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.1-flash-lite")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
PERIODIC_S = float(os.getenv("LLM_PERIODIC_S", 600))
MIN_INTERVAL_S = float(os.getenv("LLM_MIN_INTERVAL_S", 30))
MANUAL_COOLDOWN_S = 15.0     # jeda minimum untuk tombol manual
ERROR_BACKOFF_S = 120.0      # jeda panggilan otomatis setelah error/limit
HTTP_TIMEOUT_S = 25

REQUIRED_KEYS = ("ringkasan_kondisi", "urgensi", "tindakan_rekomendasi",
                 "dampak_tindakan", "saran_token")
# Urgensi minimum yang dipaksakan oleh aturan deterministik (LLM tak boleh menurunkan)
URGENCY_FLOOR = {"DANGER": "BAHAYA", "WARNING": "WASPADA"}
URGENCY_RANK = {"AMAN": 0, "WASPADA": 1, "BAHAYA": 2}


def _f(d: Dict[str, Any], key: str) -> float:
    try:
        return float(d.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


class LLMAdvisor:
    def __init__(self, contract_va: float, tarif_per_kwh: float):
        self.contract_va = contract_va
        self.tarif = tarif_per_kwh
        self.enabled = bool(LLM_API_KEY) and (LLM_PROVIDER != "openai" or bool(LLM_BASE_URL))
        self._lock = asyncio.Lock()
        self._cache: Optional[Dict[str, Any]] = None
        self._last_state: Optional[str] = None
        self._last_call = 0.0          # time.monotonic()
        self._blocked_until = 0.0

    # ------------------------------------------------------------------
    # API publik
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """Saran terakhir yang tersimpan (tanpa memanggil LLM)."""
        return {"enabled": self.enabled, "model": LLM_MODEL if self.enabled else None,
                "advice": self._cache}

    async def on_telemetry(self, telemetry: Dict[str, Any], dss: Dict[str, Any]) -> None:
        """Dipanggil tiap paket MQTT (2 dtk). Hanya memanggil LLM bila syarat terpenuhi:
        transisi status DSS, atau cache kosong, atau sudah >= PERIODIC_S."""
        if not self.enabled or self._lock.locked():
            return
        now = time.monotonic()
        state = dss.get("status")
        wanted = (self._cache is None or state != self._last_state
                  or now - self._last_call >= PERIODIC_S)
        allowed = now >= self._blocked_until and now - self._last_call >= MIN_INTERVAL_S
        if wanted and allowed:
            await self._refresh(telemetry, dss)

    async def advise(self, telemetry: Dict[str, Any], dss: Dict[str, Any]) -> Dict[str, Any]:
        """Permintaan manual (tombol 'Minta Saran AI')."""
        if not self.enabled:
            if self._cache is None:
                self._cache = self._fallback(dss, reason="API key LLM belum diatur")
            return self.snapshot()
        idle = not self._lock.locked()
        if idle and time.monotonic() - self._last_call >= MANUAL_COOLDOWN_S:
            await self._refresh(telemetry, dss)
        return self.snapshot()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    async def _refresh(self, telemetry: Dict[str, Any], dss: Dict[str, Any]) -> None:
        async with self._lock:
            now = time.monotonic()
            self._last_call = now
            self._last_state = dss.get("status")
            try:
                text = await asyncio.to_thread(self._call_llm, self._build_prompt(telemetry, dss))
                advice = self._parse(text)
                advice["source"] = "llm"
                advice["error"] = None
            except Exception as e:  # jaringan, kuota, JSON rusak, dll.
                print(f"[LLM] Gagal: {type(e).__name__}: {e}")
                self._blocked_until = now + ERROR_BACKOFF_S
                advice = self._fallback(dss, reason=str(e))
            advice = self._apply_floor(advice, dss)
            advice["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            advice["dss_status"] = dss.get("status")
            advice["dss_title"] = dss.get("title")
            self._cache = advice

    def _build_prompt(self, t: Dict[str, Any], dss: Dict[str, Any]) -> str:
        s = _f(t, "apparent_power")
        qs = [_f(t, k) for k in ("q1", "q2", "q3", "q4")]
        now = datetime.now()
        pct = (s / self.contract_va * 100) if self.contract_va > 0 else 0.0
        return f"""Anda adalah penasihat energi rumah tangga di Indonesia (kontrak {self.contract_va:.0f} VA, listrik prabayar).
Gunakan Bahasa Indonesia sehari-hari yang santun, sederhana, dan mudah dipahami orang rumah.
PENTING: Jangan gunakan istilah asing seperti runway atau load shifting. Gunakan istilah bahasa Indonesia seperti perkiraan sisa waktu pemakaian listrik, daya tahan saldo token, atau memindahkan jam pemakaian alat listrik.
Taksiran daya alat listrik harus ditandai perkiraan, jangan mengarang data pengukuran.

DATA KELISTRIKAN RUMAH:
- Waktu: {now:%H:%M} WIB
- Tegangan: {_f(t,'voltage'):.1f} V, Arus: {_f(t,'current'):.3f} A
- Daya Aktif: {_f(t,'power'):.1f} W, Daya Semu: {s:.1f} VA ({pct:.1f}% dari kapasitas kontrak {self.contract_va:.0f} VA)
- Saldo Token: {_f(t,'saldo_kwh'):.2f} kWh
- Perkiraan Sisa Waktu Pemakaian Listrik: {_f(t,'runway_hours'):.1f} jam
- Estimasi Pemakaian 24 Jam (TinyML): Kuadran 1 (00-06): {qs[0]:.2f} kWh, Kuadran 2 (06-12): {qs[1]:.2f} kWh, Kuadran 3 (12-18): {qs[2]:.2f} kWh, Kuadran 4 (18-24): {qs[3]:.2f} kWh (Total: {sum(qs):.2f} kWh)
- Tarif Listrik PLN: Rp {self.tarif:.2f}/kWh
- Status Sistem: {dss.get('status')} ({dss.get('title')}) - {dss.get('message')}
- Target Pengurangan Beban: {dss.get('excess_watt', 0)} W

Keluarkan output HANYA dalam format JSON valid dengan skema berikut:
{{
  "ringkasan_kondisi": "penjelasan kondisi pemakaian listrik rumah secara singkat dan mudah dimengerti orang rumah",
  "urgensi": "AMAN",
  "tindakan_rekomendasi": ["langkah praktis alat 1", "langkah praktis alat 2"],
  "dampak_tindakan": "manfaat penghematan rupiah atau perkiraan tambahan waktu listrik bertahan",
  "saran_token": "saran isi token atau Saldo token masih mencukupi"
}}"""

    def _call_llm(self, prompt: str) -> str:
        """Panggilan HTTP sinkron (dijalankan di thread agar event loop tidak terblokir)."""
        if LLM_PROVIDER == "openai":
            url = f"{LLM_BASE_URL}/chat/completions"
            body = {"model": LLM_MODEL, "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}]}
            headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{LLM_MODEL}:generateContent"
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3,
                                         "responseMimeType": "application/json"}}
            headers = {"x-goog-api-key": LLM_API_KEY}
        headers["Content-Type"] = "application/json"
        headers["User-Agent"] = "SmartPowerMeter/1.0 (Linux; x86_64)"
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            raise RuntimeError(f"HTTP {e.code} {detail}") from None
        if LLM_PROVIDER == "openai":
            return payload["choices"][0]["message"]["content"]
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)

    @staticmethod
    def _parse(text: str) -> Dict[str, Any]:
        text = text.strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("respons bukan JSON")
        data = json.loads(text[start:end + 1])
        missing = [k for k in REQUIRED_KEYS if k not in data]
        if missing:
            raise ValueError(f"kunci hilang: {missing}")
        acts = data["tindakan_rekomendasi"]
        data["tindakan_rekomendasi"] = [str(a) for a in acts] if isinstance(acts, list) else [str(acts)]
        data["urgensi"] = str(data["urgensi"]).upper()
        if data["urgensi"] not in URGENCY_RANK:
            data["urgensi"] = "WASPADA"
        for k in ("ringkasan_kondisi", "dampak_tindakan", "saran_token"):
            data[k] = str(data[k])
        return {k: data[k] for k in REQUIRED_KEYS}

    def _fallback(self, dss: Dict[str, Any], reason: str) -> Dict[str, Any]:
        """Saran berbasis aturan bila LLM tidak tersedia."""
        return {
            "ringkasan_kondisi": str(dss.get("message", "")),
            "urgensi": URGENCY_FLOOR.get(dss.get("status"), "AMAN"),
            "tindakan_rekomendasi": [
                f"Jaga total beban di bawah {0.75 * self.contract_va:.0f} VA.",
                "Hindari menyalakan beberapa beban besar (AC, pompa air, setrika) bersamaan.",
            ],
            "dampak_tindakan": "Mencegah MCB trip dan menjaga saldo token.",
            "saran_token": "Periksa saldo secara berkala.",
            "source": "rule",
            "error": reason,
        }

    @staticmethod
    def _apply_floor(advice: Dict[str, Any], dss: Dict[str, Any]) -> Dict[str, Any]:
        """Urgensi LLM tidak boleh lebih rendah dari yang ditetapkan aturan deterministik."""
        floor = URGENCY_FLOOR.get(dss.get("status"))
        if floor and URGENCY_RANK[advice["urgensi"]] < URGENCY_RANK[floor]:
            advice["urgensi"] = floor
        return advice
