#!/usr/bin/env python3
"""
setup_env.py
First-time setup. Run this once before launching AutoApplyAI.
Installs all dependencies and Playwright browsers.

Usage:
    python setup_env.py
"""

import subprocess
import sys
import os
from pathlib import Path


def run(cmd, description=""):
    if description:
        print(f"\n▶ {description}")
    print(f"  {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"  ⚠ Command exited with code {result.returncode} — continuing anyway")
    return result.returncode == 0


def check_python():
    version = sys.version_info
    print(f"Python {version.major}.{version.minor}.{version.micro}")
    if version.major < 3 or (version.major == 3 and version.minor < 11):
        print("❌ Python 3.11+ required. Download from python.org")
        sys.exit(1)
    print("✅ Python version OK")


def install_requirements():
    ok = run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"],
        "Installing Python packages..."
    )
    if not ok:
        print("  Trying without --quiet flag for more detail...")
        run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])


def install_playwright():
    run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        "Installing Playwright Chromium browser..."
    )


def create_dirs():
    print("\n▶ Creating app directories...")
    app_dir = Path.home() / ".autoapplyai"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "linkedin_profile").mkdir(exist_ok=True)
    (app_dir / "indeed_profile").mkdir(exist_ok=True)
    Path("assets").mkdir(exist_ok=True)
    print(f"  App data directory: {app_dir}")


def verify_imports():
    print("\n▶ Verifying key imports...")
    failures = []
    imports = [
        ("PyQt6.QtWidgets", "PyQt6"),
        ("playwright.async_api", "Playwright"),
        ("anthropic", "Anthropic SDK"),
        ("httpx", "httpx"),
        ("bs4", "BeautifulSoup4"),
        ("feedparser", "feedparser"),
        ("pdfplumber", "pdfplumber"),
        ("cryptography.fernet", "cryptography"),
    ]
    for module, name in imports:
        try:
            __import__(module)
            print(f"  ✅ {name}")
        except ImportError:
            print(f"  ❌ {name} — import failed")
            failures.append(name)

    if failures:
        print(f"\n⚠ Failed imports: {', '.join(failures)}")
        print("  Try: pip install -r requirements.txt")
    else:
        print("\n✅ All imports OK")

    return len(failures) == 0


def main():
    print("=" * 50)
    print("  AutoApplyAI — First-Time Setup")
    print("=" * 50)

    check_python()
    install_requirements()
    install_playwright()
    create_dirs()
    all_ok = verify_imports()

    print("\n" + "=" * 50)
    if all_ok:
        print("✅ Setup complete!")
        print("\nNext steps:")
        print("  1. Get your Claude API key: console.anthropic.com")
        print("  2. Run:  python main.py")
        print("  3. Complete the 5-minute setup wizard")
        print("  4. Click ▶ Start Applying on the Dashboard")
    else:
        print("⚠ Setup completed with some warnings.")
        print("  Fix the failed imports above, then run: python main.py")
    print("=" * 50)


if __name__ == "__main__":
    main()
