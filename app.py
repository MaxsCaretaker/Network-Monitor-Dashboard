"""
Network Monitor — Flask + SocketIO server
Run:  python app.py
Then open http://localhost:5000
"""

import os
import json
import csv
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit

from monitor import NetworkMonitor
from alerts import AlertManager

app = Flask(__name__, static_folder="static", template_folder="static")
app.config["SECRET_KEY"] = "netmon-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

monitor = NetworkMonitor()
alert_mgr = AlertManager()

DEFAULT_DEVICES = [
    {"host": "8.8.8.8",       "label": "Google DNS",      "interval": 5},
    {"host": "1.1.1.1",       "label": "Cloudflare DNS",  "interval": 5},
    {"host": "8.8.4.4",       "label": "Google DNS 2",    "interval": 8},
    {"host": "9.9.9.9",       "label": "Quad9 DNS",       "interval": 8},
    {"host": "208.67.222.222","label": "OpenDNS",          "interval": 10},
    {"host": "192.168.1.1",   "label": "Default Gateway", "interval": 5},
]

_previously_down: set = set()

def push_update(device_dict: dict):
    socketio.emit("device_update", device_dict)
    host  = device_dict["host"]
    label = device_dict["label"]

    if device_dict["current_status"] == "up" and host in _previously_down:
        _previously_down.discard(host)
        alert_mgr.handle_recovery(host, label)

    if device_dict["current_status"] == "down":
        _previously_down.add(host)

    anomalies = device_dict.get("anomalies", [])
    if anomalies:
        alert_mgr.handle_anomaly(device_dict, anomalies[-1])

monitor.on_update(push_update)

def sound_cb(kind: str):
    socketio.emit("play_sound", {"kind": kind})

alert_mgr.set_sound_callback(sound_cb)

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/devices", methods=["GET"])
def get_devices():
    return jsonify(monitor.get_all())

@app.route("/api/summary", methods=["GET"])
def get_summary():
    return jsonify(monitor.get_summary())

@app.route("/api/devices", methods=["POST"])
def add_device():
    data     = request.get_json(force=True)
    host     = data.get("host", "").strip()
    label    = data.get("label", "").strip() or host
    interval = float(data.get("interval", 5))
    if not host:
        return jsonify({"error": "host required"}), 400
    monitor.add_device(host, label, interval)
    return jsonify({"status": "added", "host": host}), 201

@app.route("/api/devices/<path:host>", methods=["DELETE"])
def remove_device(host):
    monitor.remove_device(host)
    socketio.emit("device_removed", {"host": host})
    return jsonify({"status": "removed", "host": host})

@app.route("/api/alerts/config", methods=["GET"])
def get_alert_config():
    return jsonify(alert_mgr.get_config_safe())

@app.route("/api/alerts/config", methods=["POST"])
def save_alert_config():
    data = request.get_json(force=True)
    alert_mgr.save_config(data)
    return jsonify({"status": "saved"})

@app.route("/api/alerts/test-email", methods=["POST"])
def test_email():
    ok, msg = alert_mgr.test_email()
    return jsonify({"success": ok, "message": msg}), (200 if ok else 400)

@app.route("/api/logs/anomalies", methods=["GET"])
def get_anomalies():
    path = os.path.join(os.path.dirname(__file__), "logs", "anomalies.csv")
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    return jsonify(rows[-100:])

@app.route("/api/logs/<path:host_csv>", methods=["GET"])
def get_host_log(host_csv):
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    return send_from_directory(log_dir, host_csv)

@socketio.on("connect")
def on_connect():
    emit("initial_state", {
        "devices": monitor.get_all(),
        "summary": monitor.get_summary(),
        "alert_config": alert_mgr.get_config_safe(),
    })

@socketio.on("add_device")
def on_add_device(data):
    host     = data.get("host", "").strip()
    label    = data.get("label", "").strip() or host
    interval = float(data.get("interval", 5))
    if host:
        monitor.add_device(host, label, interval)
        emit("device_added", {"host": host, "label": label}, broadcast=True)

@socketio.on("remove_device")
def on_remove_device(data):
    host = data.get("host", "")
    monitor.remove_device(host)
    emit("device_removed", {"host": host}, broadcast=True)

@socketio.on("save_alert_config")
def on_save_alert_config(data):
    alert_mgr.save_config(data)
    emit("alert_config_saved", {"status": "ok"}, broadcast=True)

def start_monitor():
    for d in DEFAULT_DEVICES:
        monitor.add_device(d["host"], d["label"], d["interval"])
    monitor.start()

if __name__ == "__main__":
    print("=" * 55)
    print("  Network Monitor  —  http://localhost:5000")
    print("=" * 55)
    start_monitor()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)