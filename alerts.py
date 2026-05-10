"""
NetMon Alert System
Handles email and sound alerts for anomaly events.
Includes cooldown per-host to avoid alert storms.
"""

import smtplib
import threading
import time
import json
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Optional

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "alert_config.json")

DEFAULT_CONFIG = {
    "email": {
        "enabled": False,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "",
        "password": "",
        "from_addr": "",
        "to_addrs": [],          # list of recipient addresses
        "on_host_down": True,
        "on_latency_spike": True,
        "on_recovery": True,
    },
    "sound": {
        "enabled": True,
        "on_host_down": True,
        "on_latency_spike": True,
        "on_recovery": True,
    },
    "cooldown_seconds": 120,     # min time between alerts for same host+type
}


class AlertManager:
    def __init__(self):
        self.config = self._load_config()
        self._cooldowns: dict[str, float] = {}   # "host:type" → last alert epoch
        self._lock = threading.Lock()
        self._sound_callback = None   # set by app to emit WS event to browser

    # ── Config ────────────────────────────────────────────────
    def _load_config(self) -> dict:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    saved = json.load(f)
                # merge with defaults so new keys always exist
                merged = DEFAULT_CONFIG.copy()
                merged["email"].update(saved.get("email", {}))
                merged["sound"].update(saved.get("sound", {}))
                merged["cooldown_seconds"] = saved.get(
                    "cooldown_seconds", DEFAULT_CONFIG["cooldown_seconds"]
                )
                return merged
            except Exception:
                pass
        return DEFAULT_CONFIG.copy()

    def save_config(self, new_config: dict):
        # Merge carefully — don't overwrite password with empty string if not provided
        email_cfg = self.config["email"].copy()
        email_cfg.update({k: v for k, v in new_config.get("email", {}).items()
                          if v != "" or k not in ("password",)})
        self.config["email"] = email_cfg
        self.config["sound"].update(new_config.get("sound", {}))
        if "cooldown_seconds" in new_config:
            self.config["cooldown_seconds"] = int(new_config["cooldown_seconds"])

        # Don't persist raw password to disk — store it only in memory
        save_data = {
            "email": {k: v for k, v in self.config["email"].items() if k != "password"},
            "sound": self.config["sound"],
            "cooldown_seconds": self.config["cooldown_seconds"],
        }
        with open(CONFIG_PATH, "w") as f:
            json.dump(save_data, f, indent=2)

    def get_config_safe(self) -> dict:
        """Return config without password for sending to UI."""
        cfg = json.loads(json.dumps(self.config))
        cfg["email"]["password"] = "••••••••" if self.config["email"].get("password") else ""
        return cfg

    # ── Cooldown ──────────────────────────────────────────────
    def _in_cooldown(self, host: str, kind: str) -> bool:
        key = f"{host}:{kind}"
        now = time.time()
        with self._lock:
            last = self._cooldowns.get(key, 0)
            if now - last < self.config["cooldown_seconds"]:
                return True
            self._cooldowns[key] = now
            return False

    # ── Main entry point ──────────────────────────────────────
    def handle_anomaly(self, device: dict, anomaly: dict):
        """Called when a new anomaly is detected. device = device.to_dict()"""
        kind  = anomaly.get("type")        # "latency_spike" | "host_down"
        host  = device.get("host", "")
        label = device.get("label", host)

        if self._in_cooldown(host, kind):
            return

        threading.Thread(
            target=self._dispatch,
            args=(kind, host, label, device, anomaly),
            daemon=True
        ).start()

    def handle_recovery(self, host: str, label: str):
        """Called when a previously-down host comes back up."""
        if self._in_cooldown(host, "recovery"):
            return
        threading.Thread(
            target=self._dispatch,
            args=("recovery", host, label, {}, {}),
            daemon=True
        ).start()

    def _dispatch(self, kind: str, host: str, label: str, device: dict, anomaly: dict):
        if self.config["sound"]["enabled"]:
            self._trigger_sound(kind)

        if self.config["email"]["enabled"]:
            self._send_email(kind, host, label, device, anomaly)

    # ── Sound ─────────────────────────────────────────────────
    def _trigger_sound(self, kind: str):
        cfg = self.config["sound"]
        if kind == "host_down"      and not cfg.get("on_host_down"):      return
        if kind == "latency_spike"  and not cfg.get("on_latency_spike"):   return
        if kind == "recovery"       and not cfg.get("on_recovery"):        return

        # Signal the browser via socketio callback
        if self._sound_callback:
            try:
                self._sound_callback(kind)
            except Exception:
                pass

    def set_sound_callback(self, fn):
        """Register a function that will emit a WS event to trigger browser audio."""
        self._sound_callback = fn

    # ── Email ─────────────────────────────────────────────────
    def _send_email(self, kind: str, host: str, label: str, device: dict, anomaly: dict):
        cfg = self.config["email"]
        if kind == "host_down"     and not cfg.get("on_host_down"):     return
        if kind == "latency_spike" and not cfg.get("on_latency_spike"):  return
        if kind == "recovery"      and not cfg.get("on_recovery"):       return

        if not cfg.get("username") or not cfg.get("password"):
            print("[alerts] Email enabled but credentials missing — skipping.")
            return
        if not cfg.get("to_addrs"):
            print("[alerts] Email enabled but no recipients — skipping.")
            return

        subject, body = self._build_email_content(kind, host, label, device, anomaly)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg.get("from_addr") or cfg["username"]
        msg["To"]      = ", ".join(cfg["to_addrs"])
        msg.attach(MIMEText(body, "html"))

        try:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10) as server:
                server.ehlo()
                server.starttls()
                server.login(cfg["username"], cfg["password"])
                server.sendmail(msg["From"], cfg["to_addrs"], msg.as_string())
            print(f"[alerts] Email sent: {subject}")
        except Exception as e:
            print(f"[alerts] Email failed: {e}")

    def _build_email_content(self, kind, host, label, device, anomaly):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        icons = {
            "host_down":     "🔴",
            "latency_spike": "🟡",
            "recovery":      "🟢",
        }
        titles = {
            "host_down":     f"Host Down — {label}",
            "latency_spike": f"Latency Spike — {label}",
            "recovery":      f"Host Recovered — {label}",
        }

        icon  = icons.get(kind, "⚠️")
        title = titles.get(kind, f"Alert — {label}")
        subject = f"[NetMon] {icon} {title}"

        latency_row = ""
        if kind == "latency_spike" and anomaly.get("value"):
            latency_row = f"<tr><td><b>Spike Latency</b></td><td>{anomaly['value']} ms</td></tr>"

        avg_lat = device.get("avg_latency")
        uptime  = device.get("uptime_pct")
        loss    = device.get("packet_loss_pct")

        body = f"""
        <html><body style="font-family:Arial,sans-serif;background:#f4f4f4;padding:20px">
        <div style="max-width:520px;margin:auto;background:#fff;border-radius:8px;
                    box-shadow:0 2px 8px rgba(0,0,0,.1);overflow:hidden">
          <div style="background:{'#dc2626' if kind=='host_down' else '#d97706' if kind=='latency_spike' else '#16a34a'};
                      padding:20px 24px;color:#fff">
            <h2 style="margin:0;font-size:20px">{icon} {title}</h2>
            <p style="margin:4px 0 0;opacity:.85;font-size:13px">{ts}</p>
          </div>
          <div style="padding:24px">
            <table style="width:100%;border-collapse:collapse;font-size:14px">
              <tr><td style="padding:6px 0;color:#555;width:140px"><b>Host</b></td>
                  <td style="padding:6px 0;font-family:monospace">{host}</td></tr>
              <tr><td><b>Label</b></td><td>{label}</td></tr>
              {latency_row}
              {'<tr><td><b>Avg Latency</b></td><td>' + str(avg_lat) + ' ms</td></tr>' if avg_lat else ''}
              {'<tr><td><b>Uptime</b></td><td>' + str(uptime) + '%</td></tr>' if uptime is not None else ''}
              {'<tr><td><b>Packet Loss</b></td><td>' + str(loss) + '%</td></tr>' if loss is not None else ''}
            </table>
            <hr style="border:none;border-top:1px solid #eee;margin:16px 0">
            <p style="font-size:12px;color:#999;margin:0">
              Sent by NetMon — Network Monitor Dashboard
            </p>
          </div>
        </div>
        </body></html>
        """
        return subject, body

    def test_email(self) -> tuple[bool, str]:
        """Send a test email. Returns (success, message)."""
        cfg = self.config["email"]
        if not cfg.get("username") or not cfg.get("password"):
            return False, "SMTP credentials not configured."
        if not cfg.get("to_addrs"):
            return False, "No recipient addresses configured."
        try:
            msg = MIMEText("<h3>✅ NetMon email alerts are working!</h3>", "html")
            msg["Subject"] = "[NetMon] Test Alert"
            msg["From"]    = cfg.get("from_addr") or cfg["username"]
            msg["To"]      = ", ".join(cfg["to_addrs"])
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10) as s:
                s.ehlo(); s.starttls()
                s.login(cfg["username"], cfg["password"])
                s.sendmail(msg["From"], cfg["to_addrs"], msg.as_string())
            return True, f"Test email sent to {', '.join(cfg['to_addrs'])}"
        except Exception as e:
            return False, str(e)