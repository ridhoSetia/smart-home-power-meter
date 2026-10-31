import time
import paho.mqtt.client as mqtt

# ================= KONFIGURASI MQTT =================
BROKER = "broker.hivemq.com"
PORT = 1883
TOPIC_RELAY = "jairu/powermeter/relay/control"
# Gunakan Client ID yang unik agar tidak bentrok dengan ESP32
CLIENT_ID = "PYTHON_CONTROLLER_JAIRU"

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[SUKSES] Terhubung ke MQTT Broker HiveMQ!")
    else:
        print(f"[GAGAL] Koneksi gagal dengan kode: {rc}")

# Inisialisasi MQTT Client
client = mqtt.Client(client_id=CLIENT_ID)
client.on_connect = on_connect

print(f"Menghubungkan ke {BROKER}:{PORT}...")
client.connect(BROKER, PORT, keepalive=60)
client.loop_start()

# Tunggu sejenak hingga koneksi stabil
time.sleep(1)

def send_command(cmd):
    """Mengirim perintah string ke topik kontrol relay"""
    cmd = cmd.strip().upper()
    if cmd in ["ON", "1", "OFF", "0"]:
        client.publish(TOPIC_RELAY, cmd)
        print(f"--> Perintah '{cmd}' terkirim ke topik: {TOPIC_RELAY}")
    else:
        print("[PERINGATAN] Perintah tidak valid! Gunakan ON, OFF, 1, atau 0.")

try:
    print("\n" + "="*45)
    print(" KONTROL RELAY ESP32 (ELEVATE 2026)")
    print("="*45)
    print("Ketik:")
    print(" - 'ON' atau '1'  : Nyalakan Relay")
    print(" - 'OFF' atau '0' : Matikan Relay")
    print(" - 'EXIT'         : Keluar dari program")
    print("="*45)

    while True:
        user_input = input("\nMasukkan perintah (ON/OFF): ")
        if user_input.strip().upper() == "EXIT":
            print("Keluar dari program...")
            break
        send_command(user_input)

except KeyboardInterrupt:
    print("\nProgram dihentikan oleh pengguna.")

finally:
    client.loop_stop()
    client.disconnect()
    print("Koneksi MQTT ditutup.")