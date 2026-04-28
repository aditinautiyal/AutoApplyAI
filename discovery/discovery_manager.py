"""
discovery/discovery_manager.py
Runs all discovery sources in parallel background coroutines.
Uses API-based discovery (no scraping, no rate limits) as primary source.
Google ATS search kept as secondary, rate-limited to 30 min cycles.
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
            target=self._run_loop,
            daemon=True,
            name="DiscoveryManager"
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
            print(f"[Discovery] Error in discovery loop: {e}")
        finally:
            loop.close()

    async def _async_main(self):
        import json

        enabled_platforms = self.store.get("platforms", [])
        if isinstance(enabled_platforms, str):
            try:
                enabled_platforms = json.loads(enabled_platforms)
            except Exception:
                enabled_platforms = []

        tasks = []

        # ── PRIMARY: API-based discovery (no scraping, never rate-limited) ──
        # Replaces RSS feeds, Reddit, and Google as the main source.
        # The Muse + Remotive + Jobicy + Arbeitnow + USAJobs.
        try:
            from discovery.api_discovery import run_api_discovery
            tasks.append(asyncio.create_task(
                run_api_discovery(continuous=True, stop_event=self._stop_event),
                name="APIDiscovery"
            ))
            print("[Discovery] API discovery started (The Muse, Remotive, Jobicy, Arbeitnow)")
        except Exception as e:
            print(f"[Discovery] Could not start API discovery: {e}")

        # ── SECONDARY: Advice scraping (always runs once at startup) ──
        try:
            from research.advice_scraper import run_advice_scraping
            tasks.append(asyncio.create_task(
                run_advice_scraping(stop_event=self._stop_event),
                name="AdviceScraper"
            ))
        except Exception as e:
            print(f"[Discovery] Could not start advice scraper: {e}")

        # ── OPTIONAL: Google ATS search (only if enabled, rate-limited) ──
        # Only runs if explicitly enabled. Hits once per 30 min max.
        if enabled_platforms and "Google ATS Deep Search" in enabled_platforms:
            try:
                from discovery.google_search import run_google_discovery
                tasks.append(asyncio.create_task(
                    run_google_discovery(continuous=True, stop_event=self._stop_event),
                    name="GoogleSearch"
                ))
                print("[Discovery] Google ATS search enabled (30 min cycles)")
            except Exception as e:
                print(f"[Discovery] Could not start Google search: {e}")

        # ── OPTIONAL: Deep web scan (YC, HN — only if enabled) ──
        if enabled_platforms and ("Deep Web Scan" in enabled_platforms or
                                   "Startup Boards" in enabled_platforms):
            try:
                from discovery.deep_web_scanner import run_deep_web_discovery
                tasks.append(asyncio.create_task(
                    run_deep_web_discovery(continuous=True, stop_event=self._stop_event),
                    name="DeepWebScan"
                ))
            except ImportError as e:
                print(f"[Discovery] Deep web scanner unavailable: {e} — skipping")
            except Exception as e:
                print(f"[Discovery] Could not start deep web scanner: {e}")

        # ── LEGACY: RSS feeds (kept for USAJobs which RSS works for) ──
        if not enabled_platforms or "USAJobs Feed" in enabled_platforms:
            try:
                from discovery.rss_feeds import run_rss_discovery
                tasks.append(asyncio.create_task(
                    run_rss_discovery(continuous=True, stop_event=self._stop_event),
                    name="RSSFeeds"
                ))
            except Exception as e:
                print(f"[Discovery] Could not start RSS feeds: {e}")

        if not tasks:
            print("[Discovery] Warning: no discovery sources started")
            return

        self.status_cb("all", f"Running {len(tasks)} discovery sources")
        await asyncio.gather(*tasks, return_exceptions=True)
