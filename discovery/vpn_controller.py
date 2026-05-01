"""
discovery/vpn_controller.py
Automatic VPN management — detects blocks and switches VPN servers.

Works with ProtonVPN on Windows via its CLI.
Also works with any VPN that has a CLI (NordVPN, ExpressVPN, etc.)

What it does:
- Monitors Google, Indeed, SimplyHired every 10 minutes
- When a site gets blocked, automatically switches to a new VPN server
- Logs VPN status to the dashboard activity log
- Tries up to 5 different servers before giving up

Setup:
- ProtonVPN: install the app, it includes protonvpn-cli automatically
- The app must be installed at default location or on PATH
"""

import asyncio
import subprocess
import threading
import time
import random
import httpx
from pathlib import Path
from core.settings_store import get_store

CHECK_INTERVAL = 10 * 60   # Check every 10 minutes
MAX_SWITCH_ATTEMPTS = 5

# ProtonVPN CLI locations on Windows
PROTONVPN_PATHS = [
    r"C:\Program Files\Proton\VPN\v3\ProtonVPN.exe",
    r"C:\Program Files (x86)\Proton\VPN\ProtonVPN.exe",
    r"C:\Program Files\ProtonVPN\ProtonVPN.exe",
    "protonvpn-cli",           # If on PATH
    "protonvpn",               # Alternative
]

# NordVPN (alternative)
NORDVPN_PATHS = [
    r"C:\Program Files\NordVPN\NordVPN.exe",
    "nordvpn",
]

# ExpressVPN (alternative)
EXPRESSVPN_PATHS = [
    r"C:\Program Files (x86)\ExpressVPN\services\ExpressVPNService.exe",
    "expressvpn",
]

# Sites to monitor — if any return blocked, trigger VPN switch
MONITORED_SITES = [
    ("https://www.google.com/search?q=software+engineer+intern+greenhouse.io", "Google"),
    ("https://www.simplyhired.com/search?q=software+engineer+intern", "SimplyHired"),
    ("https://www.indeed.com/jobs?q=software+engineer+intern", "Indeed"),
]

# US server locations to cycle through
VPN_LOCATIONS = [
    "US-NY", "US-CA", "US-TX", "US-IL", "US-FL",
    "US-WA", "US-GA", "US-VA", "US-OH", "US-MA",
]


def _find_vpn_cli() -> tuple[str, str]:
    """
    Find available VPN CLI on this machine.
    Returns (vpn_type, cli_path) or ("none", "")
    """
    for path in PROTONVPN_PATHS:
        try:
            if Path(path).exists() or path in ("protonvpn-cli", "protonvpn"):
                result = subprocess.run(
                    [path, "--version"],
                    capture_output=True, timeout=5
                )
                if result.returncode == 0 or "proton" in result.stdout.decode().lower():
                    return "protonvpn", path
        except Exception:
            continue

    for path in NORDVPN_PATHS:
        try:
            result = subprocess.run([path, "--version"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return "nordvpn", path
        except Exception:
            continue

    for path in EXPRESSVPN_PATHS:
        try:
            result = subprocess.run([path, "--version"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return "expressvpn", path
        except Exception:
            continue

    return "none", ""


def _get_current_ip() -> str:
    """Get current public IP address."""
    try:
        resp = httpx.get("https://api.ipify.org", timeout=8)
        return resp.text.strip()
    except Exception:
        return "unknown"


def _switch_vpn_server(vpn_type: str, cli_path: str,
                        location: str = "") -> bool:
    """
    Switch to a different VPN server.
    Returns True if switch succeeded.
    """
    try:
        if vpn_type == "protonvpn":
            # ProtonVPN CLI commands
            # First disconnect
            subprocess.run(
                [cli_path, "disconnect"],
                capture_output=True, timeout=15
            )
            time.sleep(3)

            # Connect to specific server or fastest
            if location:
                cmd = [cli_path, "connect", "--cc", location.split("-")[1]]
            else:
                cmd = [cli_path, "connect", "--fastest"]

            result = subprocess.run(cmd, capture_output=True, timeout=30)
            success = result.returncode == 0
            if success:
                print(f"[VPNController] Switched to {location or 'fastest'} server")
            return success

        elif vpn_type == "nordvpn":
            subprocess.run([cli_path, "disconnect"], capture_output=True, timeout=15)
            time.sleep(3)
            if location:
                country = location.split("-")[1].lower()
                result = subprocess.run(
                    [cli_path, "connect", country],
                    capture_output=True, timeout=30
                )
            else:
                result = subprocess.run(
                    [cli_path, "connect"],
                    capture_output=True, timeout=30
                )
            return result.returncode == 0

        elif vpn_type == "expressvpn":
            subprocess.run([cli_path, "disconnect"], capture_output=True, timeout=15)
            time.sleep(3)
            result = subprocess.run(
                [cli_path, "connect", "smart"],
                capture_output=True, timeout=30
            )
            return result.returncode == 0

    except Exception as e:
        print(f"[VPNController] Switch error: {e}")

    return False


async def _check_site(url: str, name: str) -> tuple[bool, str]:
    """
    Check if a site is accessible or blocked.
    Returns (is_accessible, reason).
    """
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
                    )
                }
            )

            # Check for block signals
            text_lower = resp.text.lower()
            url_lower  = str(resp.url).lower()

            if resp.status_code == 429:
                return False, "429 rate limited"
            if "captcha" in text_lower or "sorry/index" in url_lower:
                return False, "CAPTCHA"
            if "unusual traffic" in text_lower:
                return False, "unusual traffic"
            if "cloudflare" in text_lower and "additional verification" in text_lower:
                return False, "Cloudflare block"
            if "access denied" in text_lower:
                return False, "access denied"
            if resp.status_code == 403:
                return False, "403 forbidden"
            if resp.status_code == 200:
                return True, "ok"

            return False, f"status {resp.status_code}"

    except Exception as e:
        return False, str(e)[:50]


class VPNController:
    """
    Monitors site accessibility and automatically switches VPN servers
    when blocks are detected.
    """

    def __init__(self):
        self.store = get_store()
        self._thread = None
        self._stop_event = threading.Event()
        self.running = False
        self.vpn_type = "none"
        self.cli_path  = ""
        self.current_location_idx = 0
        self.blocked_sites: dict[str, str] = {}
        self.switch_count = 0
        self.last_ip = ""

    def start(self):
        # Detect VPN
        self.vpn_type, self.cli_path = _find_vpn_cli()
        if self.vpn_type == "none":
            print("[VPNController] No VPN CLI found — manual switching only")
            print("[VPNController] VPN monitoring still active (will alert on blocks)")
        else:
            print(f"[VPNController] Found {self.vpn_type} at {self.cli_path}")

        self.last_ip = _get_current_ip()
        print(f"[VPNController] Current IP: {self.last_ip}")

        self._stop_event.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="VPNController"
        )
        self._thread.start()
        print("[VPNController] Started — monitoring sites every 10 min")

    def stop(self):
        self._stop_event.set()
        self.running = False

    def _loop(self):
        # First check after 3 minutes (let everything start)
        for _ in range(18):
            if self._stop_event.is_set():
                return
            time.sleep(10)

        while not self._stop_event.is_set():
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self._check_and_act())
            finally:
                loop.close()

            # Sleep in 30s chunks
            for _ in range(CHECK_INTERVAL // 30):
                if self._stop_event.is_set():
                    break
                time.sleep(30)

    async def _check_and_act(self):
        """Check all monitored sites and switch VPN if needed."""
        newly_blocked = []
        newly_unblocked = []

        for url, name in MONITORED_SITES:
            accessible, reason = await _check_site(url, name)

            if not accessible:
                if name not in self.blocked_sites:
                    newly_blocked.append((name, reason))
                self.blocked_sites[name] = reason
            else:
                if name in self.blocked_sites:
                    newly_unblocked.append(name)
                    del self.blocked_sites[name]

        # Log newly unblocked
        for name in newly_unblocked:
            msg = f"✅ {name} accessible again"
            print(f"[VPNController] {msg}")
            self._log_to_dashboard(msg)

        # Handle newly blocked sites
        if newly_blocked:
            blocked_names = [n for n, _ in newly_blocked]
            reasons = [f"{n}: {r}" for n, r in newly_blocked]
            msg = f"❌ Blocked: {', '.join(blocked_names)}"
            print(f"[VPNController] {msg}")
            self._log_to_dashboard(msg)

            # Auto-switch VPN if available
            if self.vpn_type != "none":
                await self._auto_switch()
            else:
                # Show popup asking user to switch manually
                self._show_manual_switch_alert(blocked_names)

    async def _auto_switch(self):
        """Automatically switch to next VPN server."""
        for attempt in range(MAX_SWITCH_ATTEMPTS):
            location = VPN_LOCATIONS[self.current_location_idx % len(VPN_LOCATIONS)]
            self.current_location_idx += 1

            print(f"[VPNController] Auto-switching to {location} (attempt {attempt+1})...")
            self._log_to_dashboard(f"🔄 Auto-switching VPN to {location}...")

            success = _switch_vpn_server(self.vpn_type, self.cli_path, location)

            if success:
                time.sleep(5)  # Wait for connection
                new_ip = _get_current_ip()

                if new_ip != self.last_ip:
                    self.last_ip = new_ip
                    self.switch_count += 1

                    # Verify the blocked sites are now accessible
                    all_clear = True
                    for url, name in MONITORED_SITES:
                        if name in self.blocked_sites:
                            accessible, _ = await _check_site(url, name)
                            if not accessible:
                                all_clear = False
                                break

                    if all_clear:
                        msg = f"✅ VPN switched to {location} — all sites accessible (IP: {new_ip})"
                        print(f"[VPNController] {msg}")
                        self._log_to_dashboard(msg)
                        self.blocked_sites.clear()
                        return
                    else:
                        print(f"[VPNController] {location} still blocked — trying next server...")
                else:
                    print(f"[VPNController] IP unchanged — switch may have failed")
            else:
                print(f"[VPNController] Switch to {location} failed")

            await asyncio.sleep(5)

        # All attempts failed
        msg = "⚠️ VPN auto-switch failed after 5 attempts — please switch manually"
        print(f"[VPNController] {msg}")
        self._log_to_dashboard(msg)
        self._show_manual_switch_alert(list(self.blocked_sites.keys()))

    def _show_manual_switch_alert(self, blocked_sites: list[str]):
        """Show a popup telling the user to switch VPN manually."""
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox
            from PyQt6.QtCore import Qt
            app = QApplication.instance()
            if app:
                def show():
                    msg = QMessageBox()
                    msg.setWindowTitle("⚠️ Switch VPN Server")
                    msg.setText(
                        f"These sites are blocked: {', '.join(blocked_sites)}\n\n"
                        f"Please switch to a different VPN server location.\n"
                        f"Discovery will resume automatically after switching."
                    )
                    msg.setIcon(QMessageBox.Icon.Warning)
                    msg.setWindowFlags(
                        msg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
                    )
                    msg.exec()
                # Run on main thread
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(0, show)
        except Exception:
            pass

    def _log_to_dashboard(self, message: str):
        """Log message to the dashboard activity log."""
        try:
            from main import signals
            signals.log_message.emit(f"[VPN] {message}")
        except Exception:
            pass

    def get_status(self) -> dict:
        return {
            "vpn_type":     self.vpn_type,
            "running":      self.running,
            "blocked_sites": self.blocked_sites,
            "switch_count": self.switch_count,
            "current_ip":   self.last_ip,
        }


# Singleton
_controller_instance = None

def get_vpn_controller() -> VPNController:
    global _controller_instance
    if _controller_instance is None:
        _controller_instance = VPNController()
    return _controller_instance
