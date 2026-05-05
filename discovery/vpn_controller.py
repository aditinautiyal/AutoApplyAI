"""
discovery/vpn_controller.py
VPN monitoring with detailed terminal callouts.
ProtonVPN CLI should now be installed — auto-switching enabled if found.
"""

import subprocess
import threading
import time
import random
import httpx
from pathlib import Path
from core.settings_store import get_store

CHECK_INTERVAL   = 10 * 60
MAX_SWITCH_ATTEMPTS = 5

WINDSCRIBE_CLI = r"C:\Program Files\Windscribe\windscribe-cli.exe"

PROTONVPN_PATHS = [WINDSCRIBE_CLI]  # kept for _find_vpn_cli compatibility

MONITORED_SITES = [
    ("https://www.google.com/search?q=software+engineer+intern+greenhouse.io", "Google"),
    ("https://www.simplyhired.com/search?q=software+engineer+intern",          "SimplyHired"),
]

VPN_LOCATIONS = ["US Central", "US East", "US West", "US Texas", "US Florida",
                 "US New York", "US California", "US Atlanta", "US Chicago"]

BLOCK_SIGNATURES = [
    "captcha","unusual traffic","robot","rate limit",
    "access denied","blocked","403 forbidden","cloudflare",
    "please verify","are you human",
]


def _find_vpn_cli() -> tuple[str, str]:
    cli = WINDSCRIBE_CLI
    try:
        if Path(cli).exists():
            result = subprocess.run([cli, "--version"], capture_output=True, timeout=5)
            out = (result.stdout + result.stderr).decode(errors="ignore").lower()
            if result.returncode == 0 or "windscribe" in out or "version" in out:
                return "windscribe", cli
        # Also try if it's been added to PATH
        result = subprocess.run(["windscribe-cli", "--version"], capture_output=True, timeout=5)
        if result.returncode == 0:
            return "windscribe", "windscribe-cli"
    except Exception:
        pass
    return "none", ""


def _get_current_ip() -> str:
    try:
        return httpx.get("https://api.ipify.org", timeout=8).text.strip()
    except Exception:
        try:
            return httpx.get("https://ifconfig.me", timeout=8).text.strip()
        except Exception:
            return "unknown"


def _is_blocked(content: str, status_code: int) -> bool:
    if status_code in (403, 429, 503, 999):
        return True
    cl = content.lower()
    return any(s in cl for s in BLOCK_SIGNATURES)


def _try_switch(cli_path: str, location: str = "") -> bool:
    """Switch Windscribe server. Disconnect first, then reconnect to new location."""
    try:
        subprocess.run([cli_path, "disconnect"], capture_output=True, timeout=15)
        time.sleep(2)
        loc = location or "US East"
        result = subprocess.run([cli_path, "connect", loc], capture_output=True, timeout=30)
        out = (result.stdout + result.stderr).decode(errors="ignore").lower()
        if "connected" in out or "success" in out:
            return True
        # Fallback: bare connect (picks best server)
        result = subprocess.run([cli_path, "connect"], capture_output=True, timeout=30)
        out = (result.stdout + result.stderr).decode(errors="ignore").lower()
        return "connected" in out
    except Exception as e:
        print(f"[VPNController] Switch error: {e}")
        return False


def _get_windscribe_status(cli_path: str) -> str:
    """Returns the connect state line from windscribe-cli status."""
    try:
        result = subprocess.run([cli_path, "status"], capture_output=True, timeout=10)
        out = (result.stdout + result.stderr).decode(errors="ignore")
        for line in out.splitlines():
            if "connect state:" in line.lower():
                return line.strip()
    except Exception:
        pass
    return ""


class VPNController:
    def __init__(self, status_cb=None):
        self.status_cb    = status_cb or (lambda *a: None)
        self._thread      = None
        self._running     = False
        self.current_ip   = "unknown"
        self.vpn_type     = "none"
        self.cli_path     = ""
        self.auto_capable = False
        self.switch_count = 0
        self.blocked_sites: list[str] = []

    def start(self):
        self.vpn_type, self.cli_path = _find_vpn_cli()
        self.auto_capable = self.vpn_type != "none"
        self.current_ip   = _get_current_ip()

        print()
        print("╔══════════════════════════════════════════════════════════╗")
        print("║              VPN CONTROLLER — STARTUP                   ║")
        print("╠══════════════════════════════════════════════════════════╣")
        if self.auto_capable:
            print(f"║  ✅ Windscribe CLI found at:                              ║")
            print(f"║     {self.cli_path[:52]:<52}║")
            print(f"║  ✅ AUTO-SWITCHING: ENABLED                              ║")
            print(f"║     Will automatically rotate IP when RSS blocks hit     ║")
        else:
            print(f"║  ⚠️  Windscribe CLI not found — auto-switching DISABLED   ║")
            print(f"║  ⚠️  Login first: windscribe-cli.exe login               ║")
            print(f"║     Path: C:\\Program Files\\Windscribe\\windscribe-cli.exe  ║")
        print(f"║  📍 Current IP: {self.current_ip:<42}║")
        print(f"║  🔍 Monitoring {len(MONITORED_SITES)} sites every {CHECK_INTERVAL//60} minutes              ║")
        print("╚══════════════════════════════════════════════════════════╝")
        print()

        self._running = True
        self._thread  = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        time.sleep(60)  # First check after 1 minute
        while self._running:
            self._check_sites()
            time.sleep(CHECK_INTERVAL)

    def _check_sites(self):
        print(f"[VPNController] 🔍 Checking {len(MONITORED_SITES)} monitored sites... (IP: {self.current_ip})")
        blocked = []

        for url, name in MONITORED_SITES:
            try:
                resp = httpx.get(
                    url, timeout=12, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                if _is_blocked(resp.text, resp.status_code):
                    blocked.append(name)
                    print(f"[VPNController] 🚫 RSS BLOCK DETECTED: {name} "
                          f"(HTTP {resp.status_code}) — current IP {self.current_ip} is rate-limited")
                else:
                    print(f"[VPNController] ✅ {name}: accessible")
            except Exception as e:
                blocked.append(name)
                print(f"[VPNController] 🚫 RSS BLOCK DETECTED: {name} unreachable ({type(e).__name__})")

        self.blocked_sites = blocked

        if not blocked:
            print(f"[VPNController] ✅ All sites accessible — IP {self.current_ip} not blocked")
            self.status_cb("vpn", f"✅ VPN OK — IP: {self.current_ip}")
            return

        blocked_str = ", ".join(blocked)
        print(f"[VPNController] ⚠️  BLOCKED: {blocked_str}")

        if self.auto_capable:
            print(f"[VPNController] 🔄 AUTO-SWITCH TRIGGERED — rotating to new IP...")
            self._auto_switch()
        else:
            msg = f"⚠️  RSS block: {blocked_str}. Open ProtonVPN → change server."
            print(f"[VPNController] {msg}")
            print(f"[VPNController] ⚠️  Cannot auto-switch — no CLI. Manual switch required.")
            self.status_cb("vpn", msg)
            self._popup_alert(blocked_str)

    def _auto_switch(self):
        old_ip = self.current_ip
        for attempt in range(1, MAX_SWITCH_ATTEMPTS + 1):
            loc = random.choice(VPN_LOCATIONS)
            print(f"[VPNController] 🔄 Switch attempt {attempt}/{MAX_SWITCH_ATTEMPTS}: connecting to {loc}...")
            success = _try_switch(self.cli_path, loc)
            if success:
                time.sleep(5)
                new_ip = _get_current_ip()
                status  = _get_windscribe_status(self.cli_path)
                self.current_ip = new_ip
                self.switch_count += 1
                print(f"[VPNController] ✅ IP ROTATED SUCCESSFULLY!")
                print(f"[VPNController] ✅ Old IP: {old_ip} → New IP: {new_ip}")
                print(f"[VPNController] ✅ Server: {status}")
                print(f"[VPNController] ✅ RSS blocks bypassed — resuming discovery")
                self.status_cb("vpn", f"✅ IP rotated: {old_ip} → {new_ip}")
                return
            print(f"[VPNController] ✗ Attempt {attempt} failed ({loc})")
            time.sleep(2)

        print(f"[VPNController] ✗ All {MAX_SWITCH_ATTEMPTS} switch attempts failed")
        print(f"[VPNController] ⚠️  Switch server manually in Windscribe app")
        self.status_cb("vpn", "⚠️ Auto-switch failed — switch manually")

    def _popup_alert(self, blocked_sites: str):
        """Non-blocking alert — terminal only to avoid Qt thread freeze."""
        print()
        print("=" * 60)
        print(f"[VPNController] ⚠️  ACTION REQUIRED")
        print(f"[VPNController] RSS block on: {blocked_sites}")
        print(f"[VPNController] Open ProtonVPN app → switch to a different server")
        print(f"[VPNController] No restart needed — new IP takes effect immediately")
        print("=" * 60)
        print()


_controller: VPNController | None = None

def get_vpn_controller(status_cb=None) -> VPNController:
    global _controller
    if _controller is None:
        _controller = VPNController(status_cb=status_cb)
    return _controller
