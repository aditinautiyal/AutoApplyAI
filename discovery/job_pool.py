"""
discovery/job_pool.py
Central ranked job queue. All discovery sources feed into this.
Tracks pull from the top. Paused jobs step aside without blocking.
Thread-safe. Jobs scored before entering pool.
"""

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from queue import PriorityQueue
from core.settings_store import get_store
from core.api_router import get_router


@dataclass(order=True)
class Job:
    score:       float           # Higher = better fit. Compared first for priority.
    job_id:      str = field(compare=False)
    title:       str = field(compare=False)
    company:     str = field(compare=False)
    url:         str = field(compare=False)
    ats_url:     str = field(compare=False, default="")
    platform:    str = field(compare=False, default="")
    description: str = field(compare=False, default="")
    location:    str = field(compare=False, default="")
    salary:      str = field(compare=False, default="")
    discovered_at: float = field(compare=False, default_factory=time.time)
    status:      str = field(compare=False, default="queued")
    # queued | assigned | paused | submitted | failed

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "ats_url": self.ats_url,
            "platform": self.platform,
            "description": self.description,
            "location": self.location,
            "salary": self.salary,
            "score": self.score,
            "status": self.status,
        }


class JobPool:
    """
    Thread-safe priority queue of discovered jobs.
    Higher score = picked first.
    Deduplication by URL.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._seen_urls: set[str] = set()
        self._queue: list[Job] = []   # sorted by score descending
        self._paused: dict[str, Job] = {}  # job_id → Job (waiting for user input)
        self._total_added = 0
        self.store = get_store()
        self._load_seen_urls()

    def _load_seen_urls(self):
        """Load already-applied URLs so we never apply twice."""
        apps = self.store.get_applications()
        self._seen_urls = {a["job_url"] for a in apps if a.get("job_url")}

    def add(self, job: Job) -> bool:
        """Add job to pool. Returns False if duplicate."""
        url = job.ats_url or job.url
        with self._lock:
            if url in self._seen_urls:
                return False
            self._seen_urls.add(url)
            # Insert maintaining sorted order (highest score first)
            inserted = False
            for i, existing in enumerate(self._queue):
                if job.score > existing.score:
                    self._queue.insert(i, job)
                    inserted = True
                    break
            if not inserted:
                self._queue.append(job)
            self._total_added += 1
            return True

    def get_next(self) -> Optional[Job]:
        """Get highest-scored queued job. Returns None if pool empty."""
        with self._lock:
            for job in self._queue:
                if job.status == "queued":
                    job.status = "assigned"
                    return job
            return None

    def pause_job(self, job_id: str, reason: str):
        """Move a job to paused state — frees its track slot immediately."""
        with self._lock:
            for job in self._queue:
                if job.job_id == job_id:
                    job.status = "paused"
                    self._paused[job_id] = job
                    break

    def resume_job(self, job_id: str):
        """Put a paused job back into the queue at original priority."""
        with self._lock:
            if job_id in self._paused:
                job = self._paused.pop(job_id)
                job.status = "queued"
                # Re-insert at correct position
                inserted = False
                for i, existing in enumerate(self._queue):
                    if existing.job_id == job_id:
                        existing.status = "queued"
                        inserted = True
                        break
                if not inserted:
                    self._queue.insert(0, job)

    def mark_done(self, job_id: str, status: str = "submitted"):
        """Mark a job as submitted or failed."""
        with self._lock:
            for job in self._queue:
                if job.job_id == job_id:
                    job.status = status
                    break

    def size(self) -> int:
        """Number of queued (not yet assigned) jobs."""
        with self._lock:
            return sum(1 for j in self._queue if j.status == "queued")

    def stats(self) -> dict:
        with self._lock:
            statuses = {}
            for job in self._queue:
                statuses[job.status] = statuses.get(job.status, 0) + 1
            return {
                "total_added": self._total_added,
                "by_status": statuses,
                "paused_count": len(self._paused),
                "queue_size": statuses.get("queued", 0),
            }

    def get_paused(self) -> list[Job]:
        with self._lock:
            return list(self._paused.values())


class JobScorer:
    """
    Scores a job 0-10 for fit before adding to pool.
    Uses Claude Haiku (fast, cheap). Falls back to keyword matching.
    """

    def __init__(self):
        self.router = get_router()
        self.store = get_store()

    def _get_profile_summary(self) -> str:
        profile = self.store.get_profile() or {}
        parts = [
            f"Target roles: {profile.get('target_roles', '')}",
            f"Preferred locations: {profile.get('locations', '')}",
            f"Dream criteria: {profile.get('dream_criteria', '')}",
            f"Skills: {profile.get('strengths_text', '')[:200]}",
            f"Salary range: ${profile.get('salary_min', 0)}-${profile.get('salary_max', 0)}/hr",
        ]
        return "\n".join(parts)

    def score(self, job: Job) -> float:
        """Returns score 0.0-10.0."""
        try:
            profile_summary = self._get_profile_summary()
            prompt = f"""Score this job posting for fit. Return ONLY a number 0-10. Nothing else.

Candidate profile:
{profile_summary}

Job posting:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Description: {job.description[:500]}

Score 0-10 (10=perfect fit, 0=completely wrong):"""
            result = self.router.complete(prompt, max_tokens=5)
            score = float(result.strip().split()[0])
            return max(0.0, min(10.0, score))
        except Exception:
            return self._keyword_score(job)

    def _keyword_score(self, job: Job) -> float:
        """Fast keyword fallback if AI call fails."""
        profile = self.store.get_profile() or {}
        target_roles = profile.get("target_roles", "").lower()
        locations = profile.get("locations", "").lower()

        text = f"{job.title} {job.description}".lower()
        score = 5.0

        # Role match
        good_keywords = ["software", "ai", "ml", "machine learning", "data",
                          "computer science", "backend", "full stack", "python",
                          "intern", "engineer", "research"]
        for kw in good_keywords:
            if kw in text:
                score += 0.3

        # Location match
        for loc in locations.split(","):
            if loc.strip().lower() in (job.location or "").lower():
                score += 1.0

        # Remote bonus
        if "remote" in text or "remote" in (job.location or "").lower():
            score += 0.5

        return min(10.0, score)


# Singleton pool
_pool_instance = None

def get_pool() -> JobPool:
    global _pool_instance
    if _pool_instance is None:
        _pool_instance = JobPool()
    return _pool_instance
