"""
build.py
Builds AutoApplyAI into a standalone .exe using PyInstaller.
Run: python build.py

Output: dist/AutoApplyAI.exe  (~150-200 MB)
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

APP_NAME    = "AutoApplyAI"
ENTRY_POINT = "main.py"
ICON_PATH   = "assets/icon.ico"   # optional — skip if not present
DIST_DIR    = Path("dist")
BUILD_DIR   = Path("build")


def run(cmd: list[str]):
    print(f"\n>>> {' '.join(cmd)}\n")
    result = subprocess.run(cmd, check=True)
    return result


def clean():
    """Remove previous build artifacts."""
    for d in [DIST_DIR, BUILD_DIR]:
        if d.exists():
            shutil.rmtree(d)
            print(f"Cleaned: {d}")
    spec = Path(f"{APP_NAME}.spec")
    if spec.exists():
        spec.unlink()
        print(f"Cleaned: {spec}")


def check_deps():
    """Make sure all required packages are installed."""
    print("Checking dependencies...")
    run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"])
    run([sys.executable, "-m", "playwright", "install", "chromium"])


def build():
    """Run PyInstaller."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",           # No console window on Windows
        f"--name={APP_NAME}",
        "--noconfirm",
        "--clean",

        # Hidden imports PyInstaller misses
        "--hidden-import=anthropic",
        "--hidden-import=openai",
        "--hidden-import=playwright",
        "--hidden-import=playwright.sync_api",
        "--hidden-import=playwright.async_api",
        "--hidden-import=PyQt6",
        "--hidden-import=PyQt6.QtWebEngineWidgets",
        "--hidden-import=pdfplumber",
        "--hidden-import=feedparser",
        "--hidden-import=cryptography",
        "--hidden-import=google.auth",
        "--hidden-import=google.auth.transport.requests",
        "--hidden-import=google_auth_oauthlib.flow",
        "--hidden-import=googleapiclient.discovery",
        "--hidden-import=aiosqlite",
        "--hidden-import=httpx",
        "--hidden-import=bs4",
        "--hidden-import=lxml",

        # Include all app packages
        "--add-data=core:core",
        "--add-data=onboarding:onboarding",
        "--add-data=discovery:discovery",
        "--add-data=research:research",
        "--add-data=tracks:tracks",
        "--add-data=slow_lane:slow_lane",
        "--add-data=email_handler:email_handler",
        "--add-data=extra_effort:extra_effort",
        "--add-data=notifications:notifications",
        "--add-data=ui:ui",
    ]

    # Add icon if it exists
    if Path(ICON_PATH).exists():
        cmd.append(f"--icon={ICON_PATH}")

    cmd.append(ENTRY_POINT)

    run(cmd)


def verify():
    """Check output exists."""
    exe = DIST_DIR / (APP_NAME + (".exe" if sys.platform == "win32" else ""))
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\n✅ Build successful: {exe}  ({size_mb:.1f} MB)")
    else:
        print(f"\n❌ Build failed — {exe} not found")
        sys.exit(1)


def make_assets_dir():
    """Create assets dir if not present (for icon)."""
    Path("assets").mkdir(exist_ok=True)
    # Write a minimal placeholder icon note
    readme = Path("assets/README.txt")
    if not readme.exists():
        readme.write_text(
            "Place icon.ico here to embed an icon in the .exe\n"
            "Recommended size: 256x256\n"
        )


if __name__ == "__main__":
    print(f"Building {APP_NAME}...\n")
    make_assets_dir()

    if "--no-clean" not in sys.argv:
        clean()

    if "--no-deps" not in sys.argv:
        check_deps()

    build()
    verify()

    print(f"\n📦 Installer ready: dist/{APP_NAME}.exe")
    print("Upload to anautai.com/downloads/ and link from your portfolio page.")
