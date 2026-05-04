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

PROTONVPN_PATHS = [
    r"C:\Program Files\Proton\VPN\v3\ProtonVPN.exe",
    r"C:\Program Files (x86)\Proton\VPN\ProtonVPN.exe",
    r"C:\Program Files\ProtonVPN\ProtonVPN.exe",
    "protonvpn-cli",
    "protonvpn",
]

MONITORED_SITES = [
    ("https://www.google.com/search?q=software+engineer+intern+greenhouse.io", "Google"),
    ("https://www.simplyhired.com/search?q=software+engineer+intern",          "SimplyHired"),
    ("https://www.indeed.com/jobs?q=software+engineer+intern",                 "Indeed"),
]

VPN_LOCATIONS = ["US-NY","US-CA","US-TX","US-IL","US-FL","US-WA","US-GA","US-OH","US-MA"]

BLOCK_SIGNATURES = [
    "captcha","unusual traffic","robot","rate limit",
    "access denied","blocked","403 forbidden","cloudflare",
    "please verify","are you human",
]


def _find_vpn_cli() -> tuple[str, str]:
    for path in PROTONVPN_PATHS:
        try:
            check_path = Path(path)
            if not check_path.exists() and path not in ("protonvpn-cli","protonvpn"):
                continue
            result = subprocess.run([path, "--version"], capture_output=True, timeout=5)
            out = (result.stdout + result.stderr).decode(errors="ignore").lower()
            if result.returncode == 0 or "proton" in out or "version" in out:
                return "protonvpn", path
        except Exception:
            continue
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
    if status_code in (403, 429, 503):
        return True
    cl = content.lower()
    return any(s in cl for s in BLOCK_SIGNATURES)


def _try_switch(cli_path: str, location: str = "") -> bool:
    cmds = [
        [cli_path, "connect", "--random"],
        [cli_path, "c", "--random"],
        [cli_path, "connect"],
    ]
    if location:
        cmds.insert(0, [cli_path, "connect", location])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            out = (r.stdout + r.stderr).decode(errors="ignore").lower()
            if r.returncode == 0 or "connected" in out or "success" in out:
                return True
        except Exception:
            continue
    return False


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
            print(f"║  ✅ VPN CLI found: {self.vpn_type:<40}║")
            print(f"║  ✅ Path: {self.cli_path:<48}║")
            print(f"║  ✅ AUTO-SWITCHING: ENABLED                              ║")
            print(f"║     Will automatically rotate IP when RSS blocks hit     ║")
        else:
            print(f"║  ⚠️  VPN CLI: NOT FOUND — auto-switching DISABLED         ║")
            print(f"║  ⚠️  Run: winget install ProtonTechnologies.ProtonVPN     ║")
            print(f"║     Until then: switch servers manually in ProtonVPN app  ║")
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
            print(f"[VPNController] 🔄 Switch attempt {attempt}/{MAX_SWITCH_ATTEMPTS}: {loc}...")
            success = _try_switch(self.cli_path, loc)
            if success:
                time.sleep(5)
                new_ip = _get_current_ip()
                self.current_ip = new_ip
                self.switch_count += 1
                print(f"[VPNController] ✅ IP ROTATED SUCCESSFULLY!")
                print(f"[VPNController] ✅ Old IP: {old_ip}")
                print(f"[VPNController] ✅ New IP: {new_ip} (server: {loc})")
                print(f"[VPNController] ✅ RSS blocks bypassed — resuming discovery")
                self.status_cb("vpn", f"✅ IP rotated: {old_ip} → {new_ip}")
                return
            print(f"[VPNController] ✗ Attempt {attempt} failed ({loc})")
            time.sleep(2)

        print(f"[VPNController] ✗ All {MAX_SWITCH_ATTEMPTS} switch attempts failed")
        print(f"[VPNController] ⚠️  Please switch VPN server manually in ProtonVPN app")
        self.status_cb("vpn", "⚠️ Auto-switch failed — switch manually")

    def _popup_alert(self, blocked_sites: str):
        try:
            from PyQt6.QtWidgets import QMessageBox, QApplication
            if QApplication.instance():
                msg = QMessageBox()
                msg.setWindowTitle("⚠️ VPN — RSS Block Detected")
                msg.setText(
                    f"RSS/Cloudflare block detected!\n\n"
                    f"Blocked: {blocked_sites}\n"
                    f"Current IP: {self.current_ip}\n\n"
                    f"Open ProtonVPN → switch to a different server."
                )
                msg.setIcon(QMessageBox.Icon.Warning)
                msg.exec()
        except Exception:
            pass


_controller: VPNController | None = None

def get_vpn_controller(status_cb=None) -> VPNController:
    global _controller
    if _controller is None:
        _controller = VPNController(status_cb=status_cb)
    return _controller
