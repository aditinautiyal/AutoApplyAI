"""
discovery/discovery_manager.py
Runs all discovery sources in parallel background coroutines.
Google ATS search + RSS feeds + Reddit + Deep Web all feeding one pool.
"""

import asyncio
import threading
from typing import Optional
from core.settings_store import get_store


class DiscoveryManager:
    """
    Orchestrates all discovery sources.
    Each source runs as an async coroutine in a shared event loop.
    All feed into the single shared JobPool.
    """

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
        finally:
            loop.close()

    async def _async_main(self):
        """Run all discovery sources as concurrent coroutines."""
        from discovery.google_search import run_google_discovery
        from discovery.rss_feeds import run_rss_discovery
        from discovery.reddit_scanner import run_reddit_discovery
        from discovery.deep_web_scanner import run_deep_web_discovery
        from research.advice_scraper import run_advice_scraping

        enabled_platforms = self.store.get("platforms", [])
        if isinstance(enabled_platforms, str):
            import json
            try:
                enabled_platforms = json.loads(enabled_platforms)
            except Exception:
                enabled_platforms = []

        tasks = []

        # Advice scraping runs once at startup to populate advice DB
        tasks.append(asyncio.create_task(
            run_advice_scraping(stop_event=self._stop_event),
            name="AdviceScraper"
        ))

        # Discovery sources based on user settings
        if not enabled_platforms or "Google ATS Deep Search" in enabled_platforms:
            tasks.append(asyncio.create_task(
                run_google_discovery(continuous=True, stop_event=self._stop_event),
                name="GoogleSearch"
            ))

        if not enabled_platforms or "Indeed RSS Feed" in enabled_platforms or \
           "Handshake Feed" in enabled_platforms or "USAJobs Feed" in enabled_platforms:
            tasks.append(asyncio.create_task(
                run_rss_discovery(continuous=True, stop_event=self._stop_event),
                name="RSSFeeds"
            ))

        if not enabled_platforms or "Reddit Job Posts" in enabled_platforms:
            tasks.append(asyncio.create_task(
                run_reddit_discovery(continuous=True, stop_event=self._stop_event),
                name="RedditScanner"
            ))

        if not enabled_platforms or "Deep Web Scan" in enabled_platforms or \
           "Startup Boards" in enabled_platforms:
            tasks.append(asyncio.create_task(
                run_deep_web_discovery(continuous=True, stop_event=self._stop_event),
                name="DeepWebScan"
            ))

        self.status_cb("all", f"Running {len(tasks)} discovery sources")

        # Run until stopped
        await asyncio.gather(*tasks, return_exceptions=True)
