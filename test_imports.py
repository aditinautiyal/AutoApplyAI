"""
test_imports.py
Smoke-tests all AutoApplyAI modules to catch import errors before running.
Run: python test_imports.py
"""

import sys
import traceback

results = []


def test(name, fn):
    try:
        fn()
        results.append((True, name, ""))
        print(f"  ✅ {name}")
    except Exception as e:
        results.append((False, name, str(e)))
        print(f"  ❌ {name}: {e}")


print("\n─── AutoApplyAI Import Tests ───\n")

print("Core:")
test("settings_store",    lambda: __import__("core.settings_store", fromlist=["SettingsStore"]))
test("api_router",        lambda: __import__("core.api_router", fromlist=["APIRouter"]))
test("success_tracker",   lambda: __import__("core.success_tracker", fromlist=["record_positive_response"]))

print("\nOnboarding:")
test("resume_parser",     lambda: __import__("onboarding.resume_parser", fromlist=["parse_resume"]))
test("setup_wizard",      lambda: __import__("onboarding.setup_wizard", fromlist=["SetupWizard"]))

print("\nDiscovery:")
test("job_pool",          lambda: __import__("discovery.job_pool", fromlist=["JobPool", "Job"]))
test("google_search",     lambda: __import__("discovery.google_search", fromlist=["run_google_discovery"]))
test("rss_feeds",         lambda: __import__("discovery.rss_feeds", fromlist=["run_rss_discovery"]))
test("reddit_scanner",    lambda: __import__("discovery.reddit_scanner", fromlist=["run_reddit_discovery"]))
test("deep_web_scanner",  lambda: __import__("discovery.deep_web_scanner", fromlist=["run_deep_web_discovery"]))
test("discovery_manager", lambda: __import__("discovery.discovery_manager", fromlist=["DiscoveryManager"]))

print("\nResearch:")
test("company_researcher",  lambda: __import__("research.company_researcher", fromlist=["research_company"]))
test("insight_synthesizer", lambda: __import__("research.insight_synthesizer", fromlist=["synthesize"]))
test("advice_scraper",      lambda: __import__("research.advice_scraper", fromlist=["run_advice_scraping"]))

print("\nTracks:")
test("cover_letter_gen",  lambda: __import__("tracks.cover_letter_gen", fromlist=["generate_cover_letter"]))
test("humanizer_check",   lambda: __import__("tracks.humanizer_check", fromlist=["ensure_humanized"]))
test("track_worker",      lambda: __import__("tracks.track_worker", fromlist=["TrackWorker"]))
test("track_manager",     lambda: __import__("tracks.track_manager", fromlist=["TrackManager"]))

print("\nSlow Lane:")
test("linkedin_easy_apply", lambda: __import__("slow_lane.linkedin_easy_apply", fromlist=["LinkedInSlowLane"]))
test("indeed_easy_apply",   lambda: __import__("slow_lane.indeed_easy_apply", fromlist=["IndeedSlowLane"]))
test("slow_lane_manager",   lambda: __import__("slow_lane.slow_lane_manager", fromlist=["SlowLaneManager"]))

print("\nEmail:")
test("gmail_sender",      lambda: __import__("email_handler.gmail_sender", fromlist=["GmailClient"]))
test("response_handler",  lambda: __import__("email_handler.response_handler", fromlist=["handle_response"]))

print("\nNotifications:")
test("inbox_monitor_loop", lambda: __import__("notifications.inbox_monitor_loop", fromlist=["InboxMonitorLoop"]))

print("\nExtra Effort:")
test("people_finder",     lambda: __import__("extra_effort.people_finder", fromlist=["find_contacts_for_application"]))

print("\nUI:")
test("extra_effort_tab",      lambda: __import__("ui.extra_effort_tab", fromlist=["ExtraEffortTab"]))
test("research_library_tab",  lambda: __import__("ui.research_library_tab", fromlist=["ResearchLibraryTab"]))
test("settings_tab",          lambda: __import__("ui.settings_tab", fromlist=["SettingsTab"]))

# Summary
passed = sum(1 for ok, _, _ in results if ok)
failed = sum(1 for ok, _, _ in results if not ok)
total  = len(results)

print(f"\n─── Results: {passed}/{total} passed ───")

if failed > 0:
    print(f"\nFailed ({failed}):")
    for ok, name, err in results:
        if not ok:
            print(f"  • {name}: {err}")
    print("\nRun: python setup_env.py  to fix missing packages")
    sys.exit(1)
else:
    print("✅ All modules import successfully. Run: python main.py")
