"""
discovery/discovery_manager.py
Runs all discovery sources in parallel.

Sources:
1. Direct ATS scraper  — Greenhouse/Lever/Ashby APIs, zero login needed
2. API discovery       — SimplyHired, Remotive, USAJobs, Muse
3. Playwright scraper  — Built In, Wellfound, YC (headless browser)
4. Google ATS search   — VPN required, direct ATS links
5. Deep web scan       — HN, YC API
6. Advice scraper      — once at startup
"""

import asyncio
import threading
from typing import Optional
from core.settings_store import get_store


class DiscoveryManager:
    def __init__(self, status_callback=None):
        self.store = get_store()
        self.status_cb = status_callback or (lambda source, msg: None)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False

    def start(self):
        if self.running:
            return
        self._stop_event.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="DiscoveryManager"
        )
        self._thread.start()
        print("[Discovery] All sources starting...")

    def stop(self):
        self._stop_event.set()
        self.running = False
        if self._thread:
            self._thread.join(timeout=8)
        print("[Discovery] Stopped")

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as e:
            print(f"[Discovery] Error: {e}")
        finally:
            loop.close()

    async def _async_main(self):
        import json
        enabled = self.store.get("platforms", [])
        if isinstance(enabled, str):
            try:
                enabled = json.loads(enabled)
            except Exception:
                enabled = []

        tasks = []

        # 1. Direct ATS scraper — Greenhouse/Lever/Ashby (BEST SOURCE)
        try:
            from discovery.greenhouse_lever_scraper import run_ats_scraping
            tasks.append(asyncio.create_task(
                run_ats_scraping(continuous=True, stop_event=self._stop_event),
                name="ATSScraper"
            ))
            print("[Discovery] Direct ATS scraper started (Greenhouse/Lever/Ashby)")
        except Exception as e:
            print(f"[Discovery] ATS scraper error: {e}")

        # 2. API discovery — SimplyHired, Remotive, USAJobs, Muse
        try:
            from discovery.api_discovery import run_api_discovery
            tasks.append(asyncio.create_task(
                run_api_discovery(continuous=True, stop_event=self._stop_event),
                name="APIDiscovery"
            ))
            print("[Discovery] API discovery started")
        except Exception as e:
            print(f"[Discovery] API discovery error: {e}")

        # 3. Playwright scraper — Built In, Wellfound, YC
        try:
            from discovery.playwright_scraper import run_playwright_scraping
            tasks.append(asyncio.create_task(
                run_playwright_scraping(continuous=True, stop_event=self._stop_event),
                name="PlaywrightScraper"
            ))
            print("[Discovery] Playwright scraper started (Built In, Wellfound, YC)")
        except Exception as e:
            print(f"[Discovery] Playwright scraper error: {e}")

        # 4. Advice scraper — once at startup
        try:
            from research.advice_scraper import run_advice_scraping
            tasks.append(asyncio.create_task(
                run_advice_scraping(stop_event=self._stop_event),
                name="AdviceScraper"
            ))
        except Exception as e:
            print(f"[Discovery] Advice scraper error: {e}")

        # 5. Google ATS search — needs VPN
        if not enabled or "Google ATS Deep Search" in enabled:
            try:
                from discovery.google_search import run_google_discovery
                tasks.append(asyncio.create_task(
                    run_google_discovery(continuous=True, stop_event=self._stop_event),
                    name="GoogleSearch"
                ))
                print("[Discovery] Google ATS search enabled (use VPN)")
            except Exception as e:
                print(f"[Discovery] Google search error: {e}")

        # 6. Deep web scan
        if not enabled or "Deep Web Scan" in enabled:
            try:
                from discovery.deep_web_scanner import run_deep_web_discovery
                tasks.append(asyncio.create_task(
                    run_deep_web_discovery(continuous=True, stop_event=self._stop_event),
                    name="DeepWebScan"
                ))
            except Exception as e:
                print(f"[Discovery] Deep web scan error: {e}")

        if not tasks:
            print("[Discovery] Warning: no sources started")
            return

        self.status_cb("all", f"Running {len(tasks)} discovery sources")
        await asyncio.gather(*tasks, return_exceptions=True)
