# ⚡ AutoApplyAI

Autonomous job application engine. Finds, researches, and applies to jobs 24/7.

---

## Quick Start

```bash
# 1. First-time setup (installs everything)
python setup_env.py

# 2. Verify all modules load
python test_imports.py

# 3. Launch
python main.py
```

First launch opens the **5-minute setup wizard**. Only done once.

---

## What It Does

| System | Description |
|---|---|
| **Fast Tracks** | N parallel isolated application pipelines. Each: discover → research → write → submit |
| **Slow Lane** | LinkedIn + Indeed Easy Apply, human-paced (8–25 min between apps) |
| **Discovery** | Google ATS search, RSS feeds, Reddit, HackerNews, YC, deep web — all parallel |
| **Research** | Deep-scrapes Reddit, Glassdoor, news, TikTok, forums per company before writing |
| **Advice DB** | Collects best application tips from internet. Updates success scores over time |
| **Extra Effort** | Finds/flags contacts at target companies. Auto-messages on LinkedIn/Reddit |
| **Cold Email** | Auto-drafts and sends cold emails via Gmail OAuth after each submission |
| **Inbox Monitor** | Watches Gmail for employer replies every 15 min. Updates DB on positive responses |
| **AI Chat** | Natural language control: "add a track", "focus on ML jobs", "pause slow lane" |

---

## Cost

| Item | Cost |
|---|---|
| Claude API | ~$0.05/app (Haiku for forms, Sonnet 4.6 for cover letters) |
| GPTZero | Free tier: 100 checks/month (cover letters only) |
| Everything else | Free |

**1 track, 24/7 = ~$125 for 14 days (~2,500 tailored applications)**

Set a spending limit at console.anthropic.com before starting.

---

## File Structure

```
AutoApplyAI/
├── main.py                      # Entry point + full dashboard UI
├── setup_env.py                 # First-time install script
├── test_imports.py              # Smoke test all modules
├── build.py                     # PyInstaller .exe builder
│
├── core/
│   ├── settings_store.py        # Encrypted persistent SQLite
│   ├── api_router.py            # Claude/OpenAI switchable layer
│   └── success_tracker.py       # Feedback loop: responses → advice DB
│
├── onboarding/
│   ├── setup_wizard.py          # 9-step first-launch wizard
│   └── resume_parser.py         # PDF → structured data
│
├── discovery/
│   ├── job_pool.py              # Central ranked priority queue
│   ├── google_search.py         # Google → ATS links
│   ├── rss_feeds.py             # Indeed/Handshake/USAJobs feeds
│   ├── reddit_scanner.py        # r/forhire, r/cscareerquestions, etc.
│   ├── deep_web_scanner.py      # HackerNews, YC, niche boards, fellowships
│   └── discovery_manager.py     # Runs all sources in parallel
│
├── research/
│   ├── company_researcher.py    # Deep multi-source company research
│   ├── insight_synthesizer.py   # Claude turns research into strategy
│   └── advice_scraper.py        # Collects/ranks tips from internet
│
├── tracks/
│   ├── track_manager.py         # Manages N parallel fast tracks
│   ├── track_worker.py          # Full pipeline + post-submission actions
│   ├── cover_letter_gen.py      # Sonnet cover letters, Haiku form answers
│   └── humanizer_check.py       # GPTZero gate (≤75% AI threshold)
│
├── slow_lane/
│   ├── linkedin_easy_apply.py   # Human-paced LinkedIn Easy Apply
│   ├── indeed_easy_apply.py     # Human-paced Indeed Easy Apply
│   └── slow_lane_manager.py     # Orchestrates both slow lanes
│
├── email_handler/
│   ├── gmail_sender.py          # OAuth email send + inbox check
│   └── response_handler.py      # Categorizes replies, updates success scores
│
├── extra_effort/
│   └── people_finder.py         # Finds, flags, messages contacts
│
├── notifications/
│   └── inbox_monitor_loop.py    # Background Gmail check every 15 min
│
└── ui/
    ├── extra_effort_tab.py       # Flagged contacts view
    ├── research_library_tab.py   # Company profiles + advice DB browser
    └── settings_tab.py           # API keys, OAuth, profile editing
```

---

## Gmail Setup

1. Go to console.cloud.google.com
2. Create project → Enable Gmail API
3. Create OAuth credentials (Desktop app type)
4. Download → save as `~/.autoapplyai/gmail_creds.json`
5. Restart app → browser opens for one-time auth

Scopes: `gmail.send` + `gmail.readonly` only. Cannot touch contacts or anything else.

---

## Building .exe

```bash
python build.py
# Output: dist/AutoApplyAI.exe
```

Upload to anautai.com/downloads and link from your portfolio.

---

## AI Chat Commands

```
"add another track"          → increases parallel fast tracks
"pause slow lane"            → stops LinkedIn/Indeed Easy Apply
"focus on ML jobs only"      → updates target roles filter
"target Chicago and remote"  → updates location preferences
"show me stats"              → returns current counts
```

---

## Tips

- Run 24/7 — time is free, API cost is per application not per hour
- Fill background text in as much detail as possible
- Check Inbox tab daily for paused applications needing input
- Check Extra Effort tab — ⭐ flagged contacts are highest priority
- Set a Claude API spending limit at console.anthropic.com before starting
