# NetMon — Network Monitor Dashboard

A lightweight Python app that continuously pings devices on your network,
tracks latency and uptime over time, logs anomalies, and displays a live
dashboard in your browser with sound and email alerts.

---

## Quick Start

```bat
pip install flask flask-socketio
python app.py
```

Then open **http://localhost:5000**

---

## File Structure

```
Network_Monitor/
├── app.py            ← Flask server + REST API + WebSocket
├── monitor.py        ← Ping engine, stats, anomaly detection
├── alerts.py         ← Sound + email alert system
├── requirements.txt  ← Python dependencies
├── alert_config.json ← Auto-created when you save alert settings
├── static/
│   └── index.html    ← Live dashboard UI
└── logs/
    ├── anomalies.csv       ← Global anomaly log (all devices)
    ├── 8_8_8_8.csv         ← Per-device ping log
    ├── 1_1_1_1.csv
    └── ...
```

---

## Features

### Live Dashboard
- Real-time latency spark charts per device
- Summary bar: total devices, online, offline, avg latency, anomaly count
- Click any device card to see its full latency history chart
- Add or remove devices at runtime without restarting

### Monitoring Engine
| Metric | Description |
|---|---|
| Avg / Min / Max Latency | Running stats across all pings |
| Uptime % | Successful pings / total pings |
| Packet Loss % | Computed over last 10 pings |
| Consecutive Down | How many pings in a row have failed |

### Anomaly Detection
| Event | Trigger |
|---|---|
| Latency Spike | Single ping exceeds 200ms |
| Host Down | 3 consecutive failed pings |
| Recovery | Host responds after being marked down |

### Sound Alerts (browser)
Generated via Web Audio API — no audio files required.
| Event | Sound |
|---|---|
| Host Down | Urgent descending alarm beeps + red flash |
| Latency Spike | Double warning tone + yellow flash |
| Recovery | Ascending chime + green flash |

### Email Alerts
- Works with Gmail (App Password), Outlook, or any SMTP server
- Sends HTML-formatted emails with device stats
- Configurable cooldown per host to prevent alert storms (default 120s)
- Toggle on/off per event type independently

---

## Configuration

### Default Devices
Edit `DEFAULT_DEVICES` in `app.py`:
```python
DEFAULT_DEVICES = [
    {"host": "8.8.8.8",      "label": "Google DNS",      "interval": 5},
    {"host": "192.168.1.1",  "label": "My Router",       "interval": 5},
    {"host": "192.168.1.100","label": "NAS",              "interval": 10},
]
```

### Anomaly Thresholds
Edit at the top of `monitor.py`:
```python
ANOMALY_THRESHOLD_MS = 200   # ms — latency spike threshold
PACKET_LOSS_WINDOW   = 10    # last N pings for packet loss %
HISTORY_POINTS       = 120   # in-memory data points per device
```

### Alert Cooldown
Default is 120 seconds between alerts for the same host + event type.
Change it in the Alert Settings modal in the UI, or edit `alert_config.json`.

---

## Email Setup (Gmail)

1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Create an App Password for "Mail"
3. In the dashboard, click 🔔 → fill in:
   - SMTP Host: `smtp.gmail.com`
   - SMTP Port: `587`
   - Username: `your.email@gmail.com`
   - Password: the 16-character App Password
   - Recipients: comma-separated email addresses
4. Click **Send Test Email** to verify
5. Click **Save Settings**

Settings are persisted to `alert_config.json` (password is stored in memory only, not on disk).

---

## REST API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/devices` | All device states + stats |
| POST | `/api/devices` | Add device `{"host","label","interval"}` |
| DELETE | `/api/devices/<host>` | Remove device |
| GET | `/api/summary` | Global summary stats |
| GET | `/api/alerts/config` | Current alert configuration |
| POST | `/api/alerts/config` | Update alert configuration |
| POST | `/api/alerts/test-email` | Send a test email |
| GET | `/api/logs/anomalies` | Last 100 anomalies (JSON) |
| GET | `/api/logs/<host>.csv` | Raw ping log for a device |

---

## WebSocket Events

| Event | Direction | Description |
|---|---|---|
| `initial_state` | Server → Client | Full state on connect |
| `device_update` | Server → Client | Live ping result per device |
| `device_removed` | Server → Client | Device was removed |
| `play_sound` | Server → Client | Trigger browser audio |
| `add_device` | Client → Server | Add a device |
| `remove_device` | Client → Server | Remove a device |
| `save_alert_config` | Client → Server | Save alert settings |

---

## Accessing from Other Devices

The server binds to `0.0.0.0` so any device on your network can access it.
Find your machine's IP in the terminal output when you start the app:

```
 * Running on http://192.168.4.35:5000
```

Open that address on your phone, tablet, or another PC.

---

## Requirements

- Python 3.9+
- `flask` — web server
- `flask-socketio` — real-time WebSocket support
- No third-party ping library needed — uses the system `ping` command
