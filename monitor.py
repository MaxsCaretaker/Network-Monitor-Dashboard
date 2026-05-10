"""
Network Monitor Engine
Continuously pings devices, tracks latency/uptime, detects anomalies.
"""

import threading
import time
import json
import csv
import os
import subprocess
import platform
from datetime import datetime, timedelta
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

ANOMALY_THRESHOLD_MS = 200      # latency spike considered anomalous
PACKET_LOSS_WINDOW   = 10       # last N pings to compute packet-loss %
HISTORY_POINTS       = 120      # data points kept in memory per device


@dataclass
class PingResult:
    timestamp: str
    latency_ms: Optional[float]   # None = timeout/unreachable
    status: str                   # "up" | "down" | "timeout"


@dataclass
class DeviceState:
    host: str
    label: str
    ping_interval: float = 5.0

    # rolling history (capped at HISTORY_POINTS)
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_POINTS))

    # cumulative stats
    total_pings: int = 0
    successful_pings: int = 0
    total_latency: float = 0.0
    min_latency: Optional[float] = None
    max_latency: Optional[float] = None

    # anomaly log
    anomalies: list = field(default_factory=list)

    # current state
    current_status: str = "unknown"
    last_seen: Optional[str] = None
    consecutive_down: int = 0

    def record(self, result: PingResult):
        self.history.append(asdict(result))
        self.total_pings += 1
        self.current_status = result.status

        if result.latency_ms is not None:
            self.successful_pings += 1
            self.total_latency += result.latency_ms
            self.last_seen = result.timestamp
            self.consecutive_down = 0

            if self.min_latency is None or result.latency_ms < self.min_latency:
                self.min_latency = result.latency_ms
            if self.max_latency is None or result.latency_ms > self.max_latency:
                self.max_latency = result.latency_ms

            # anomaly: latency spike
            if result.latency_ms > ANOMALY_THRESHOLD_MS:
                self._log_anomaly("latency_spike", result.latency_ms, result.timestamp)
        else:
            self.consecutive_down += 1
            if self.consecutive_down == 3:
                self._log_anomaly("host_down", None, result.timestamp)

    def _log_anomaly(self, kind: str, value, ts: str):
        entry = {"type": kind, "timestamp": ts, "value": value, "host": self.host}
        self.anomalies.append(entry)
        # keep last 50 anomalies in memory
        if len(self.anomalies) > 50:
            self.anomalies.pop(0)
        # persist to CSV
        path = os.path.join(LOG_DIR, "anomalies.csv")
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["timestamp", "host", "type", "value"])
            if write_header:
                w.writeheader()
            w.writerow(entry)

    @property
    def uptime_pct(self) -> float:
        if self.total_pings == 0:
            return 0.0
        return round(self.successful_pings / self.total_pings * 100, 1)

    @property
    def avg_latency(self) -> Optional[float]:
        if self.successful_pings == 0:
            return None
        return round(self.total_latency / self.successful_pings, 2)

    @property
    def packet_loss_pct(self) -> float:
        recent = list(self.history)[-PACKET_LOSS_WINDOW:]
        if not recent:
            return 0.0
        lost = sum(1 for r in recent if r["latency_ms"] is None)
        return round(lost / len(recent) * 100, 1)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "label": self.label,
            "current_status": self.current_status,
            "last_seen": self.last_seen,
            "uptime_pct": self.uptime_pct,
            "avg_latency": self.avg_latency,
            "min_latency": self.min_latency,
            "max_latency": self.max_latency,
            "packet_loss_pct": self.packet_loss_pct,
            "total_pings": self.total_pings,
            "consecutive_down": self.consecutive_down,
            "history": list(self.history)[-60:],   # last 60 points for chart
            "anomalies": self.anomalies[-10:],      # last 10 anomalies
        }


def ping_host(host: str) -> Optional[float]:
    """Returns latency in ms or None on failure. Uses system ping for reliability."""
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", "1000", host]
    else:
        cmd = ["ping", "-c", "1", "-W", "2", host]

    try:
        start = time.perf_counter()
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3
        )
        elapsed = (time.perf_counter() - start) * 1000

        if result.returncode == 0:
            # Try to parse actual time= value from output
            import re
            match = re.search(r"time[=<](\d+\.?\d*)", result.stdout)
            if match:
                return float(match.group(1))
            return round(elapsed, 2)
        return None
    except Exception:
        return None


class NetworkMonitor:
    def __init__(self):
        self.devices: dict[str, DeviceState] = {}
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._running = False
        self._callbacks = []   # called with updated device dict on each ping

    def add_device(self, host: str, label: str = "", interval: float = 5.0):
        label = label or host
        with self._lock:
            if host not in self.devices:
                self.devices[host] = DeviceState(
                    host=host, label=label, ping_interval=interval
                )
        if self._running:
            self._start_device_thread(host)

    def remove_device(self, host: str):
        with self._lock:
            self.devices.pop(host, None)
            # thread will exit naturally on next loop check

    def on_update(self, callback):
        self._callbacks.append(callback)

    def start(self):
        self._running = True
        for host in list(self.devices.keys()):
            self._start_device_thread(host)

    def stop(self):
        self._running = False

    def _start_device_thread(self, host: str):
        if host in self._threads and self._threads[host].is_alive():
            return
        t = threading.Thread(target=self._ping_loop, args=(host,), daemon=True)
        self._threads[host] = t
        t.start()

    def _ping_loop(self, host: str):
        while self._running:
            with self._lock:
                device = self.devices.get(host)
            if device is None:
                break

            latency = ping_host(host)
            ts = datetime.now().isoformat(timespec="seconds")

            if latency is not None:
                result = PingResult(ts, round(latency, 2), "up")
            else:
                result = PingResult(ts, None, "down")

            with self._lock:
                device.record(result)
                snapshot = device.to_dict()

            for cb in self._callbacks:
                try:
                    cb(snapshot)
                except Exception:
                    pass

            # write per-device CSV log
            self._write_log(host, result)

            time.sleep(device.ping_interval)

    def _write_log(self, host: str, result: PingResult):
        safe = host.replace(".", "_").replace(":", "_")
        path = os.path.join(LOG_DIR, f"{safe}.csv")
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["timestamp", "latency_ms", "status"])
            if write_header:
                w.writeheader()
            w.writerow(asdict(result))

    def get_all(self) -> list[dict]:
        with self._lock:
            return [d.to_dict() for d in self.devices.values()]

    def get_summary(self) -> dict:
        with self._lock:
            devices = list(self.devices.values())
        up   = sum(1 for d in devices if d.current_status == "up")
        down = sum(1 for d in devices if d.current_status == "down")
        total_anomalies = sum(len(d.anomalies) for d in devices)
        avg_lat = None
        lats = [d.avg_latency for d in devices if d.avg_latency is not None]
        if lats:
            avg_lat = round(sum(lats) / len(lats), 2)
        return {
            "total": len(devices),
            "up": up,
            "down": down,
            "unknown": len(devices) - up - down,
            "avg_latency": avg_lat,
            "total_anomalies": total_anomalies,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }