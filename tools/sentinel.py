#!/usr/bin/env python3
"""
SENTINEL — Silent theft-recovery daemon for the harness.

Activation: relay command, CLI flag, or API trigger.
Capabilities:
  - Webcam capture (every N seconds)
  - Screenshot capture
  - Audio recording (configurable duration)
  - Wifi geolocation (BSSID scan → approximate coordinates)
  - Evidence upload to relay + local archive
  - Deterrent display (optional — shows "you are being watched" message)
  - All operations silent by default

Usage:
  python3 sentinel.py --activate                # start monitoring
  python3 sentinel.py --status                  # check if running
  python3 sentinel.py --deactivate              # stop monitoring
  python3 sentinel.py --test                    # single capture cycle, no upload

Relay activation:
  Send to #sentinel channel: "ACTIVATE <password>"
  Send to #sentinel channel: "DEACTIVATE <password>"
  Send to #sentinel channel: "STATUS"
  Send to #sentinel channel: "DETER" (show deterrent message)
"""

import os
import sys
import json
import time
import hashlib
import signal
import subprocess
import threading
import base64
import uuid
from pathlib import Path
from datetime import datetime, timezone

# --- Configuration ---
SENTINEL_DIR = Path(__file__).parent.parent / "data" / "sentinel"
EVIDENCE_DIR = SENTINEL_DIR / "evidence"
CONFIG_FILE = SENTINEL_DIR / "config.json"
PID_FILE = SENTINEL_DIR / "sentinel.pid"
LOG_FILE = SENTINEL_DIR / "sentinel.log"

# Relay config
RELAY_URL = os.environ.get("RELAY_URL", "")
RELAY_KEY = os.environ.get("RELAY_KEY", "")
RELAY_IDENTITY = "sentinel"
SENTINEL_CHANNEL = "sentinel"

# Capture intervals (seconds)
DEFAULT_CONFIG = {
    "webcam_interval": 30,
    "screenshot_interval": 60,
    "audio_duration": 10,        # seconds per audio clip
    "audio_interval": 120,       # how often to record
    "wifi_scan_interval": 60,
    "upload_interval": 30,       # how often to push evidence to relay
    "activation_password_hash": None,  # set on first activation
    "deterrent_shown": False,
    "active": False,
    "webcam_device": "/dev/video0",
    "max_local_evidence_mb": 500,  # auto-prune old evidence if exceeded
}


def log(msg):
    """Silent log to file only."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}\n"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(entry)
    except:
        pass


def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
            cfg = {**DEFAULT_CONFIG, **stored}
            return cfg
        except:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def evidence_path(category, ext="jpg"):
    """Generate timestamped evidence filename."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:6]
    subdir = EVIDENCE_DIR / category / datetime.now().strftime("%Y-%m-%d")
    subdir.mkdir(parents=True, exist_ok=True)
    return subdir / f"{category}_{ts}_{uid}.{ext}"


# --- Capture Functions ---

def capture_webcam(device="/dev/video0"):
    """Silent webcam capture via ffmpeg."""
    outpath = evidence_path("webcam", "jpg")
    try:
        result = subprocess.run(
            ["ffmpeg", "-f", "v4l2", "-i", device,
             "-frames:v", "1", "-y", "-loglevel", "quiet",
             str(outpath)],
            capture_output=True, timeout=10
        )
        if outpath.exists() and outpath.stat().st_size > 1000:
            log(f"Webcam capture: {outpath} ({outpath.stat().st_size} bytes)")
            return outpath
        else:
            log(f"Webcam capture failed: empty or missing file")
            return None
    except Exception as e:
        log(f"Webcam capture error: {e}")
        return None


def capture_screenshot():
    """Silent screenshot via import (ImageMagick)."""
    outpath = evidence_path("screenshot", "png")
    try:
        # Try import (ImageMagick) first — works headless
        result = subprocess.run(
            ["import", "-window", "root", str(outpath)],
            capture_output=True, timeout=10,
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")}
        )
        if outpath.exists() and outpath.stat().st_size > 1000:
            log(f"Screenshot: {outpath} ({outpath.stat().st_size} bytes)")
            return outpath
        # Fallback: gnome-screenshot
        outpath2 = outpath.with_suffix(".png")
        subprocess.run(
            ["gnome-screenshot", "-f", str(outpath2)],
            capture_output=True, timeout=10
        )
        if outpath2.exists():
            log(f"Screenshot (gnome): {outpath2}")
            return outpath2
        log("Screenshot failed: no capture method worked")
        return None
    except Exception as e:
        log(f"Screenshot error: {e}")
        return None


def capture_audio(duration=10):
    """Record audio via arecord."""
    outpath = evidence_path("audio", "wav")
    try:
        result = subprocess.run(
            ["arecord", "-d", str(duration), "-f", "cd", "-q", str(outpath)],
            capture_output=True, timeout=duration + 5
        )
        if outpath.exists() and outpath.stat().st_size > 1000:
            log(f"Audio capture: {outpath} ({outpath.stat().st_size} bytes)")
            return outpath
        log("Audio capture failed: empty file")
        return None
    except Exception as e:
        log(f"Audio capture error: {e}")
        return None


def scan_wifi():
    """Scan nearby wifi networks for geolocation."""
    outpath = evidence_path("wifi", "json")
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "BSSID,SSID,SIGNAL,CHAN,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=15
        )
        networks = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(":")
            if len(parts) >= 5:
                # BSSID has colons, so rejoin first 6 parts
                bssid = ":".join(parts[:6]).replace("\\", "")
                rest = parts[6:]
                if len(rest) >= 4:
                    networks.append({
                        "bssid": bssid,
                        "ssid": rest[0],
                        "signal": rest[1],
                        "channel": rest[2],
                        "security": rest[3]
                    })

        scan_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "networks": networks,
            "network_count": len(networks)
        }

        with open(outpath, "w") as f:
            json.dump(scan_data, f, indent=2)

        log(f"Wifi scan: {len(networks)} networks found")
        return outpath, scan_data
    except Exception as e:
        log(f"Wifi scan error: {e}")
        return None, None


def get_system_info():
    """Collect system state — logged in users, network connections, running processes."""
    info = {}
    try:
        # Who is logged in
        r = subprocess.run(["who"], capture_output=True, text=True, timeout=5)
        info["logged_in_users"] = r.stdout.strip()

        # Active network connections
        r = subprocess.run(["ss", "-tuln"], capture_output=True, text=True, timeout=5)
        info["network_connections"] = r.stdout.strip()[:2000]

        # Top processes by CPU
        r = subprocess.run(["ps", "aux", "--sort=-%cpu"], capture_output=True, text=True, timeout=5)
        info["top_processes"] = "\n".join(r.stdout.strip().split("\n")[:15])

        # Uptime
        r = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        info["uptime"] = r.stdout.strip()

        # External IP (if connected)
        try:
            import requests
            r = requests.get("https://api.ipify.org?format=json", timeout=5)
            info["external_ip"] = r.json().get("ip", "unknown")
        except:
            info["external_ip"] = "unreachable"

    except Exception as e:
        info["error"] = str(e)

    return info


# --- Upload Functions ---

def upload_to_relay(filepath, category="evidence", metadata=None):
    """Upload evidence file to relay as base64."""
    try:
        import requests

        if not RELAY_KEY:
            log("No relay key configured — skipping upload")
            return False

        with open(filepath, "rb") as f:
            data = f.read()

        b64 = base64.b64encode(data).decode()

        msg = {
            "text": json.dumps({
                "type": "sentinel_evidence",
                "category": category,
                "filename": filepath.name,
                "size_bytes": len(data),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata or {},
                "data_b64": b64[:50000]  # truncate large files for relay
            }),
            "channel": SENTINEL_CHANNEL
        }

        r = requests.post(
            f"{RELAY_URL}/send",
            json=msg,
            headers={
                "X-Relay-Key": RELAY_KEY,
                "Content-Type": "application/json"
            },
            timeout=15
        )

        if r.status_code == 200:
            log(f"Uploaded {filepath.name} to relay ({len(data)} bytes)")
            return True
        else:
            log(f"Upload failed: {r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        log(f"Upload error: {e}")
        return False


def send_alert(message):
    """Send text alert to relay."""
    try:
        import requests

        if not RELAY_KEY:
            return

        requests.post(
            f"{RELAY_URL}/send",
            json={"text": f"🚨 SENTINEL: {message}", "channel": SENTINEL_CHANNEL},
            headers={"X-Relay-Key": RELAY_KEY, "Content-Type": "application/json"},
            timeout=10
        )
        log(f"Alert sent: {message}")
    except Exception as e:
        log(f"Alert send error: {e}")


# --- Deterrent ---

def show_deterrent():
    """Display a deterrent message on screen."""
    try:
        # Create a fullscreen deterrent window using zenity
        msg = (
            "⚠️ THIS DEVICE IS MONITORED ⚠️\n\n"
            "Your photo has been captured.\n"
            "Your location has been recorded.\n"
            "Your network identity has been logged.\n\n"
            "All evidence has been uploaded to a remote server.\n\n"
            "The owner of this device has been notified.\n\n"
            "Return this device to avoid legal action."
        )
        subprocess.Popen(
            ["zenity", "--warning", "--text", msg,
             "--title", "SECURITY ALERT", "--width", "600", "--height", "400"],
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")}
        )
        log("Deterrent message displayed")
    except Exception as e:
        log(f"Deterrent display error: {e}")


# --- Main Monitoring Loop ---

class SentinelDaemon:
    def __init__(self):
        self.config = load_config()
        self.running = False
        self._threads = []
        self._evidence_queue = []

    def activate(self, password=None):
        """Start monitoring."""
        if password and not self.config.get("activation_password_hash"):
            self.config["activation_password_hash"] = hash_password(password)

        self.config["active"] = True
        self.config["activated_at"] = datetime.now(timezone.utc).isoformat()
        save_config(self.config)

        # Write PID file
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

        self.running = True
        log("SENTINEL ACTIVATED")
        send_alert("Device monitoring ACTIVATED. Capturing evidence.")

        # Initial burst — capture everything immediately
        self._initial_burst()

        # Start monitoring threads
        self._start_threads()

    def _initial_burst(self):
        """Immediate capture on activation — get as much as possible fast."""
        log("Initial burst capture...")

        # System info
        sysinfo = get_system_info()
        infopath = evidence_path("system", "json")
        with open(infopath, "w") as f:
            json.dump(sysinfo, f, indent=2)
        upload_to_relay(infopath, "system", sysinfo)

        # Webcam
        img = capture_webcam(self.config.get("webcam_device", "/dev/video0"))
        if img:
            upload_to_relay(img, "webcam")

        # Screenshot
        scr = capture_screenshot()
        if scr:
            upload_to_relay(scr, "screenshot")

        # Wifi
        wpath, wdata = scan_wifi()
        if wpath:
            upload_to_relay(wpath, "wifi", wdata)

        log("Initial burst complete")

    def _start_threads(self):
        """Start background capture threads."""
        threads = [
            ("webcam", self._webcam_loop),
            ("screenshot", self._screenshot_loop),
            ("audio", self._audio_loop),
            ("wifi", self._wifi_loop),
            ("uploader", self._upload_loop),
            ("relay_listener", self._relay_listener),
            ("pruner", self._prune_loop),
        ]

        for name, target in threads:
            t = threading.Thread(target=target, name=f"sentinel_{name}", daemon=True)
            t.start()
            self._threads.append(t)
            log(f"Thread started: {name}")

    def _webcam_loop(self):
        interval = self.config.get("webcam_interval", 30)
        while self.running:
            try:
                img = capture_webcam(self.config.get("webcam_device", "/dev/video0"))
                if img:
                    self._evidence_queue.append(("webcam", img))
            except Exception as e:
                log(f"Webcam loop error: {e}")
            time.sleep(interval)

    def _screenshot_loop(self):
        interval = self.config.get("screenshot_interval", 60)
        while self.running:
            try:
                scr = capture_screenshot()
                if scr:
                    self._evidence_queue.append(("screenshot", scr))
            except Exception as e:
                log(f"Screenshot loop error: {e}")
            time.sleep(interval)

    def _audio_loop(self):
        interval = self.config.get("audio_interval", 120)
        duration = self.config.get("audio_duration", 10)
        while self.running:
            try:
                aud = capture_audio(duration)
                if aud:
                    self._evidence_queue.append(("audio", aud))
            except Exception as e:
                log(f"Audio loop error: {e}")
            time.sleep(interval)

    def _wifi_loop(self):
        interval = self.config.get("wifi_scan_interval", 60)
        while self.running:
            try:
                wpath, wdata = scan_wifi()
                if wpath:
                    self._evidence_queue.append(("wifi", wpath))
            except Exception as e:
                log(f"Wifi loop error: {e}")
            time.sleep(interval)

    def _upload_loop(self):
        """Batch upload evidence to relay."""
        interval = self.config.get("upload_interval", 30)
        while self.running:
            try:
                while self._evidence_queue:
                    category, filepath = self._evidence_queue.pop(0)
                    upload_to_relay(filepath, category)
                    time.sleep(1)  # don't spam relay
            except Exception as e:
                log(f"Upload loop error: {e}")
            time.sleep(interval)

    def _relay_listener(self):
        """Listen for commands on the sentinel relay channel."""
        try:
            import requests
        except ImportError:
            log("requests not available — relay listener disabled")
            return

        last_id = 0
        while self.running:
            try:
                r = requests.get(
                    f"{RELAY_URL}/messages",
                    params={"channel": SENTINEL_CHANNEL, "since_id": last_id},
                    headers={"X-Relay-Key": RELAY_KEY},
                    timeout=10
                )
                if r.status_code == 200:
                    data = r.json()
                    messages = data if isinstance(data, list) else data.get("messages", [])
                    for msg in messages:
                        msg_id = msg.get("id", 0)
                        if msg_id > last_id:
                            last_id = msg_id
                        text = msg.get("text", "").strip()
                        sender = msg.get("sender", "")
                        if sender == RELAY_IDENTITY:
                            continue  # skip own messages
                        self._handle_relay_command(text)
            except Exception as e:
                log(f"Relay listener error: {e}")
            time.sleep(10)

    def _handle_relay_command(self, text):
        """Process relay commands."""
        text = text.strip().upper()

        if text == "STATUS":
            evidence_count = sum(1 for _ in EVIDENCE_DIR.rglob("*") if _.is_file()) if EVIDENCE_DIR.exists() else 0
            evidence_size = sum(f.stat().st_size for f in EVIDENCE_DIR.rglob("*") if f.is_file()) if EVIDENCE_DIR.exists() else 0
            send_alert(
                f"ACTIVE since {self.config.get('activated_at', 'unknown')}. "
                f"{evidence_count} evidence files ({evidence_size // 1024}KB). "
                f"Queue: {len(self._evidence_queue)} pending uploads."
            )

        elif text == "DETER":
            show_deterrent()
            send_alert("Deterrent message displayed on screen.")

        elif text.startswith("DEACTIVATE"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                pw_hash = hash_password(parts[1].lower())
                if pw_hash == self.config.get("activation_password_hash"):
                    self.deactivate()
                    send_alert("DEACTIVATED by relay command.")
                else:
                    send_alert("Deactivation DENIED — wrong password.")
            else:
                send_alert("Deactivation requires password.")

        elif text == "BURST":
            send_alert("Manual burst capture triggered.")
            self._initial_burst()

        elif text == "SYSINFO":
            sysinfo = get_system_info()
            send_alert(json.dumps(sysinfo, indent=2)[:3000])

    def _prune_loop(self):
        """Prune old evidence if exceeding size limit."""
        max_bytes = self.config.get("max_local_evidence_mb", 500) * 1024 * 1024
        while self.running:
            try:
                if EVIDENCE_DIR.exists():
                    files = sorted(EVIDENCE_DIR.rglob("*"), key=lambda f: f.stat().st_mtime if f.is_file() else 0)
                    total = sum(f.stat().st_size for f in files if f.is_file())
                    while total > max_bytes and files:
                        oldest = files.pop(0)
                        if oldest.is_file():
                            size = oldest.stat().st_size
                            oldest.unlink()
                            total -= size
                            log(f"Pruned old evidence: {oldest.name} ({size} bytes)")
            except Exception as e:
                log(f"Prune error: {e}")
            time.sleep(300)  # check every 5 minutes

    def deactivate(self):
        """Stop monitoring."""
        self.running = False
        self.config["active"] = False
        self.config["deactivated_at"] = datetime.now(timezone.utc).isoformat()
        save_config(self.config)
        if PID_FILE.exists():
            PID_FILE.unlink()
        log("SENTINEL DEACTIVATED")

    def run(self):
        """Main run loop — keep alive until deactivated."""
        log("Sentinel daemon starting main loop")
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            log("Sentinel interrupted by keyboard")
            self.deactivate()


# --- API Integration (for harness server) ---

def get_status():
    """Return current sentinel status for API."""
    cfg = load_config()
    evidence_count = 0
    evidence_size = 0
    if EVIDENCE_DIR.exists():
        for f in EVIDENCE_DIR.rglob("*"):
            if f.is_file():
                evidence_count += 1
                evidence_size += f.stat().st_size

    pid_alive = False
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # check if process exists
            pid_alive = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    return {
        "active": cfg.get("active", False) and pid_alive,
        "activated_at": cfg.get("activated_at"),
        "deactivated_at": cfg.get("deactivated_at"),
        "evidence_files": evidence_count,
        "evidence_size_mb": round(evidence_size / (1024 * 1024), 2),
        "config": {
            "webcam_interval": cfg.get("webcam_interval", 30),
            "screenshot_interval": cfg.get("screenshot_interval", 60),
            "audio_interval": cfg.get("audio_interval", 120),
            "audio_duration": cfg.get("audio_duration", 10),
            "wifi_scan_interval": cfg.get("wifi_scan_interval", 60),
        },
        "pid": int(PID_FILE.read_text().strip()) if PID_FILE.exists() else None,
        "pid_alive": pid_alive
    }


def activate_sentinel(password):
    """Launch sentinel as background process."""
    SENTINEL_DIR.mkdir(parents=True, exist_ok=True)

    # Check if already running
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return {"status": "already_active", "pid": pid}
        except (ProcessLookupError, ValueError):
            PID_FILE.unlink()

    # Save password hash
    cfg = load_config()
    cfg["activation_password_hash"] = hash_password(password)
    save_config(cfg)

    # Launch as detached background process
    sentinel_script = Path(__file__).resolve()
    env = {**os.environ, "RELAY_KEY": RELAY_KEY}

    proc = subprocess.Popen(
        [sys.executable, str(sentinel_script), "--activate", "--password", password],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env
    )

    return {"status": "activated", "pid": proc.pid}


def deactivate_sentinel(password):
    """Stop sentinel daemon."""
    cfg = load_config()

    if cfg.get("activation_password_hash") and hash_password(password) != cfg["activation_password_hash"]:
        return {"status": "denied", "error": "wrong password"}

    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            PID_FILE.unlink()
        except (ProcessLookupError, ValueError):
            pass

    cfg["active"] = False
    cfg["deactivated_at"] = datetime.now(timezone.utc).isoformat()
    save_config(cfg)

    return {"status": "deactivated"}


# --- CLI ---

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel theft-recovery daemon")
    parser.add_argument("--activate", action="store_true", help="Start monitoring")
    parser.add_argument("--deactivate", action="store_true", help="Stop monitoring")
    parser.add_argument("--status", action="store_true", help="Show status")
    parser.add_argument("--test", action="store_true", help="Single capture cycle (no upload)")
    parser.add_argument("--password", type=str, default="sentinel", help="Activation password")
    parser.add_argument("--deter", action="store_true", help="Show deterrent message")

    args = parser.parse_args()

    if args.status:
        status = get_status()
        print(json.dumps(status, indent=2))

    elif args.test:
        print("Running test capture cycle...")
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

        print("Webcam...", end=" ")
        img = capture_webcam()
        print(f"{'OK' if img else 'FAILED'}: {img}")

        print("Screenshot...", end=" ")
        scr = capture_screenshot()
        print(f"{'OK' if scr else 'FAILED'}: {scr}")

        print("Wifi scan...", end=" ")
        wpath, wdata = scan_wifi()
        print(f"{'OK' if wpath else 'FAILED'}: {wdata.get('network_count', 0) if wdata else 0} networks")

        print("System info...", end=" ")
        sysinfo = get_system_info()
        print(f"IP: {sysinfo.get('external_ip', 'unknown')}")

        print("Audio (3s test)...", end=" ")
        aud = capture_audio(3)
        print(f"{'OK' if aud else 'FAILED'}: {aud}")

        print(f"\nEvidence saved to: {EVIDENCE_DIR}")

    elif args.deter:
        show_deterrent()

    elif args.deactivate:
        result = deactivate_sentinel(args.password)
        print(json.dumps(result, indent=2))

    elif args.activate:
        print(f"Sentinel activating (PID {os.getpid()})...")
        daemon = SentinelDaemon()

        # Handle signals
        def shutdown(sig, frame):
            daemon.deactivate()
            sys.exit(0)
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        daemon.activate(args.password)
        daemon.run()

    else:
        parser.print_help()
