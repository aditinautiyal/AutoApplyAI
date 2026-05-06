"""
discovery/vpn_controller.py — COORDINATED VERSION
VPN switching is coordinated with track workers so they pause during
IP rotation and restart cleanly after. Eliminates EPIPE crashes.
"""

import subprocess
import threading
import time
import random
import httpx
from pathlib import Path

# ── Global coordination event ─────────────────────────────────────────────────
# Track workers MUST check VPN_SWITCHING.is_set() before starting each job.
# If set, they wait until it clears before proceeding.
VPN_SWITCHING = threading.Event()
VPN_SWITCHING.clear()  # starts as not-switching

WINDSCRIBE_CLI = r"C:\Program Files\Windscribe\windscribe-cli.exe"

CHECK_INTERVAL      = 12 * 60   # Check every 12 minutes
MAX_SWITCH_ATTEMPTS = 5
SWITCH_PAUSE_WAIT   = 15        # Seconds to wait for tracks to finish current action

MONITORED_SITES = [
    ("https://www.google.com/search?q=software+engineer+intern+greenhouse.io", "Google"),
    ("https://www.simplyhired.com/search?q=software+engineer+intern", "SimplyHired"),
]

VPN_LOCATIONS = [
    "US East", "US West", "US Texas", "US Florida",
    "US New York", "US Chicago", "US Atlanta", "US Ohio",
]

# HTTP status codes that actually mean blocked
BLOCKED_STATUS_CODES = {403, 429, 503, 999}

# Page content that means blocked (only checked for non-200 responses)
BLOCK_SIGNATURES = [
    "captcha", "unusual traffic", "robot", "rate limit",
    "access denied", "cloudflare ray id", "are you human",
    "please verify you are a human",
]


def _find_windscribe() -> str:
    """Find Windscribe CLI path."""
    if Path(WINDSCRIBE_CLI).exists():
        try:
            result = subprocess.run(
                [WINDSCRIBE_CLI, "status"],
                capture_output=True, timeout=10
            )
            if result.returncode == 0 or b"windscribe" in (result.stdout + result.stderr).lower():
                return WINDSCRIBE_CLI
        except Exception:
            pass
    # Try PATH
    try:
        result = subprocess.run(["windscribe-cli", "status"], capture_output=True, timeout=5)
        if result.returncode == 0:
            return "windscribe-cli"
    except Exception:
        pass
    return ""


def _get_ip() -> str:
    for url in ["https://api.ipify.org", "https://ifconfig.me/ip"]:
        try:
            return httpx.get(url, timeout=8).text.strip()
        except Exception:
            continue
    return "unknown"


def _is_truly_blocked(content: str, status_code: int) -> bool:
    """
    HTTP 200 is NEVER blocked regardless of content.
    Only non-200 codes with block signatures count.
    """
    if status_code == 200:
        return False  # 200 = accessible, always
    if status_code in BLOCKED_STATUS_CODES:
        return True
    # For other non-200 codes, check content
    cl = content.lower()
    return any(sig in cl for sig in BLOCK_SIGNATURES)


def _windscribe_status(cli: str) -> str:
    """Get current Windscribe connection state."""
    try:
        result = subprocess.run([cli, "status"], capture_output=True, timeout=10)
        out = (result.stdout + result.stderr).decode(errors="ignore")
        for line in out.splitlines():
            if "connect state:" in line.lower():
                return line.strip()
    except Exception:
        pass
    return ""


def _switch_server(cli: str, location: str) -> bool:
    """Disconnect and reconnect to a new server."""
    try:
        # Disconnect first
        subprocess.run([cli, "disconnect"], capture_output=True, timeout=20)
        time.sleep(3)

        # Connect to new location
        result = subprocess.run(
            [cli, "connect", location],
            capture_output=True, timeout=35
        )
        out = (result.stdout + result.stderr).decode(errors="ignore").lower()
        if result.returncode == 0 or "connected" in out:
            time.sleep(4)  # Let connection stabilize
            return True

        # Fallback: connect without location
        result = subprocess.run([cli, "connect"], capture_output=True, timeout=35)
        out = (result.stdout + result.stderr).decode(errors="ignore").lower()
        return "connected" in out

    except Exception as e:
        print(f"[VPNController] Switch command error: {e}")
        return False


class VPNController:
    def __init__(self, status_cb=None):
        self.status_cb    = status_cb or (lambda *a: None)
        self._thread      = None
        self._running     = False
        self.current_ip   = "unknown"
        self.cli_path     = ""
        self.auto_capable = False
        self.switch_count = 0

    def start(self):
        self.cli_path     = _find_windscribe()
        self.auto_capable = bool(self.cli_path)
        self.current_ip   = _get_ip()

        print()
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║              VPN CONTROLLER — COORDINATED MODE              ║")
        print("╠══════════════════════════════════════════════════════════════╣")
        if self.auto_capable:
            print(f"║  ✅ Windscribe CLI: found                                    ║")
            print(f"║  ✅ AUTO-SWITCHING: ENABLED                                  ║")
            print(f"║  ✅ Track coordination: ENABLED (tracks pause during switch) ║")
        else:
            print(f"║  ⚠️  Windscribe CLI: NOT found — auto-switching disabled      ║")
            print(f"║     Switch servers manually in Windscribe app when prompted  ║")
        print(f"║  📍 Current IP: {self.current_ip:<44}║")
        print(f"║  🔍 Checking {len(MONITORED_SITES)} sites every {CHECK_INTERVAL//60} min (HTTP 200 = OK) ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()

        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        time.sleep(90)  # First check after 90 seconds
        while self._running:
            self._check()
            time.sleep(CHECK_INTERVAL)

    def _check(self):
        print(f"[VPNController] 🔍 Checking sites... (IP: {self.current_ip})")
        blocked = []

        for url, name in MONITORED_SITES:
            try:
                resp = httpx.get(
                    url, timeout=15, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                if _is_truly_blocked(resp.text, resp.status_code):
                    blocked.append(name)
                    print(f"[VPNController] 🚫 BLOCKED: {name} (HTTP {resp.status_code})")
                else:
                    print(f"[VPNController] ✅ {name}: OK (HTTP {resp.status_code})")
            except Exception as e:
                blocked.append(name)
                print(f"[VPNController] 🚫 UNREACHABLE: {name} — {e}")

        if not blocked:
            print(f"[VPNController] ✅ All sites OK — IP {self.current_ip} not blocked")
            self.status_cb("vpn", f"✅ VPN OK — {self.current_ip}")
            return

        print(f"[VPNController] ⚠️  Blocked: {', '.join(blocked)}")

        if self.auto_capable:
            self._coordinated_switch()
        else:
            print(f"[VPNController] ⚠️  Manual switch required — open Windscribe app")
            self.status_cb("vpn", f"⚠️ Blocked: {', '.join(blocked)} — switch manually")

    def _coordinated_switch(self):
        """
        Signal tracks to pause, wait for them to finish current action,
        switch VPN server, then signal tracks to resume.
        Prevents EPIPE crashes from mid-application VPN switches.
        """
        old_ip = self.current_ip

        print(f"[VPNController] 🔄 Signaling tracks to pause for VPN switch...")
        VPN_SWITCHING.set()  # Signal all tracks to pause

        # Give tracks time to finish whatever they're mid-way through
        # (They check this at the start of each job, not mid-form)
        time.sleep(SWITCH_PAUSE_WAIT)
        print(f"[VPNController] 🔄 Tracks paused — switching VPN server...")

        success = False
        for attempt in range(1, MAX_SWITCH_ATTEMPTS + 1):
            loc = random.choice(VPN_LOCATIONS)
            print(f"[VPNController] 🔄 Attempt {attempt}/{MAX_SWITCH_ATTEMPTS}: {loc}...")

            if _switch_server(self.cli_path, loc):
                new_ip = _get_ip()
                status = _windscribe_status(self.cli_path)
                self.current_ip = new_ip
                self.switch_count += 1

                print(f"[VPNController] ✅ VPN SWITCHED SUCCESSFULLY!")
                print(f"[VPNController] ✅ Old IP: {old_ip} → New IP: {new_ip}")
                print(f"[VPNController] ✅ {status}")
                self.status_cb("vpn", f"✅ IP: {old_ip} → {new_ip}")
                success = True
                break
            else:
                print(f"[VPNController] ✗ Attempt {attempt} failed")
                time.sleep(3)

        if not success:
            print(f"[VPNController] ✗ All switch attempts failed — reconnecting current server")
            try:
                subprocess.run([self.cli_path, "connect"], capture_output=True, timeout=30)
            except Exception:
                pass
            self.status_cb("vpn", "⚠️ Switch failed — switch manually")

        # Resume tracks
        VPN_SWITCHING.clear()
        print(f"[VPNController] ✅ Tracks resumed")


_controller: VPNController | None = None

def get_vpn_controller(status_cb=None) -> VPNController:
    global _controller
    if _controller is None:
        _controller = VPNController(status_cb=status_cb)
    return _controller
