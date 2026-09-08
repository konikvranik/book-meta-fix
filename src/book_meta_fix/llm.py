"""LLM reconciliation — use a language model to repair metadata that
deterministic rules cannot handle.

Currently used for:
	- C1 (author/title swap) — given the jumbled fields + first-page text,
	  decide which is the real author and which is the real title
	- C4 (mojibake) — reconstruct the correct Czech/SK text from corrupted bytes
	- C2 (filename-as-title, fallback only) — extract title/author from the
	  book's first-page text when no other source has them

The provider is pluggable:
	- ZaiProvider            : real Z.AI API (OpenAI-compatible, glm-5.2)
	- AntigravityAcpProvider : a Google Antigravity subscription (or any
	                           Agent Client Protocol agent) as the FAST tier,
	                           Z.AI as the paid fallback — see acp.py
	- MockProvider           : deterministic responses for tests / offline runs

A provider returns a ReconciledMeta dict. Callers (pipeline/review) decide
whether to trust it (always NEEDS_REVIEW verdict — LLM output is a *proposal*,
never auto-applied).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# json-repair salvages LLM JSON that the cheap built-in sanitizer cannot
# recover: unescaped double-quotes inside string values (the model writes
# `"PROLOG"` with straight quotes inside a JSON string), raw control chars
# (newlines) inside strings, and truncation. Optional dependency — if absent we
# fall back to the built-in sanitizer + truncation repair.
try:
	import json_repair  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only without the extra
	json_repair = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


class LeakyBucket:
	"""Thread-safe leaky-bucket rate limiter (token bucket) — a *count per time*
	limiter, not a concurrency limiter.

	It bounds the request START rate regardless of how many calls are in
	flight or when worker threads arrive. With the old lock-during-sleep
	throttle, N threads hitting the LLM phase at once piled up on the lock and
	emitted requests back-to-back once each woke; the bucket smooths that into
	a steady drip.

	*capacity* is the burst size (max tokens that can bank up); *interval*
	(seconds) is the steady-state period between requests, so steady RPM ~=
	60/interval. With the default **capacity=1** the bucket is a pure even
	drip — exactly one call starts every *interval* seconds, no bunching,
	no initial burst. That is the count-per-time semantics we want for Z.AI's
	sliding-window request limit: e.g. interval=2.0 = exactly 30 evenly-spaced
	requests per minute. Raise *capacity* only if you have rate headroom and
	accept the burst risk (a burst of N calls inside one second is exactly
	what trips the dynamic RPM limit).
	"""

	def __init__(self, *, capacity: float = 1.0, interval: float = 2.0) -> None:
		self.capacity = max(0.0, capacity)
		self.interval = max(0.0, interval)
		self._tokens = self.capacity
		self._last = time.monotonic()
		self._lock = threading.Lock()

	def acquire(self) -> None:
		"""Block until a token is available, then consume one.

		The lock is held only for the bookkeeping (computing the wait); the
		actual sleep happens while still holding the lock so that concurrent
		callers queue up rather than all sleeping the same short interval and
		then racing. The total wait is bounded by (backlog * interval).
		"""
		if self.interval <= 0 or self.capacity <= 0:
			return
		while True:
			with self._lock:
				now = time.monotonic()
				# Refill based on elapsed time since last update.
				elapsed = now - self._last
				self._tokens = min(self.capacity, self._tokens + elapsed / self.interval)
				self._last = now
				if self._tokens >= 1.0:
					self._tokens -= 1.0
					return
				# Wait just enough for one token, then re-loop (another thread
				# may grab it first; that's the queueing behaviour we want).
				wait = (1.0 - self._tokens) * self.interval
			# Sleep OUTSIDE the bookkeeping critical section would let another
			# thread refill-check concurrently, but we *want* callers to queue,
			# so we hold the lock through the sleep by re-acquiring on the next
			# loop iteration. The sleep is the queue.
			time.sleep(wait)


class _InflightGate:
	"""Counting gate — a Semaphore whose capacity can shrink/grow at runtime.

	Z.AI's account-level concurrency ceiling (~5, tier-based and dynamic) is
	shared with clients bmf CANNOT see: an external ZCode/chat session on the
	same coding plan draws from the same slots. A fixed cap therefore cannot
	be right in both worlds — 3 is too deep with an external client running,
	too shallow when the account is otherwise idle. The gate adapts instead:
	a 1302 (or a false 1113) means the ceiling is exhausted RIGHT NOW, so the
	effective capacity steps DOWN (yielding a slot to whoever else is
	drawing); a streak of clean 200s earns capacity back up to the configured
	max. This is the depth counterpart of the adaptive drip: the drip adapts
	how fast calls START, the gate adapts how many RUN at once.

	Shrinking below the currently-taken count is fine — new acquirers block
	until the in-flight calls drain under the new capacity.
	"""

	def __init__(self, cap: int, recover_successes: int = 20) -> None:
		self._max = max(1, cap)
		self._cap = self._max
		self._recover = max(1, recover_successes)
		self._successes = 0
		self._taken = 0
		self._cond = threading.Condition()

	@property
	def cap(self) -> int:
		with self._cond:
			return self._cap

	def acquire(self) -> None:
		with self._cond:
			while self._taken >= self._cap:
				self._cond.wait()
			self._taken += 1

	def release(self) -> None:
		with self._cond:
			self._taken = max(0, self._taken - 1)
			self._cond.notify_all()

	def on_pressure(self, why: str) -> None:
		"""The ceiling was hit (429/1302, false 429/1113): yield one slot."""
		with self._cond:
			self._successes = 0
			if self._cap <= 1:
				return
			self._cap -= 1
			log.info("Z.AI: in-flight cap %d -> %d (%s) — making room for external clients on the same plan", self._cap + 1, self._cap, why)
			self._cond.notify_all()

	def on_success(self) -> None:
		"""A clean HTTP 200: after a streak, earn a slot back (up to the max)."""
		with self._cond:
			if self._cap >= self._max:
				return
			self._successes += 1
			if self._successes >= self._recover:
				self._successes = 0
				self._cap += 1
				log.info("Z.AI: in-flight cap recovered -> %d", self._cap)


@dataclass
class ReconciledMeta:
	"""Output of LLM reconciliation. All fields optional — only those the LLM
	could confidently fill are populated."""

	title: str | None = None
	authors: list[str] = field(default_factory=list)
	isbn: str | None = None
	series: str | None = None
	series_index: str | None = None
	publisher: str | None = None
	year: int | None = None
	language: str | None = None
	genres: list[str] = field(default_factory=list)  # literary genre tags (Czech)
	confidence: str = "medium"  # low | medium | high (LLM's self-assessment)
	reasoning: str = ""  # short explanation of how it derived the values


class LLMProvider:
	"""Abstract LLM provider interface."""

	name = "abstract"

	def reconcile(self, evidence: dict[str, Any]) -> ReconciledMeta | None:
		"""Given evidence about a book, return reconciled metadata or None.

		*evidence* keys:
			- category: 'C1' | 'C2' | 'C4' | ...
			- current: dict with the current (broken) metadata
			- first_page_text: str (first ~2000 chars of the book)
			- file_name: str (the book's filename on disk)
			- author_folder: str
			- title_folder: str
		"""
		raise NotImplementedError


# ---------------------------------------------------------------------------
# Z.AI provider (OpenAI-compatible client)
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """\
You are a metadata repair assistant for a Czech/Slovak ebook library. Given
corrupted book metadata and a sample of the book's first-page text, determine
the correct values.

Return ONLY a JSON object (no markdown, no explanation outside JSON) with
these fields (omit any you cannot determine):
  - "title": the real title of the book (with correct Czech/Slovak diacritics)
  - "authors": array of author names (NOT translators — those go in a separate
    "translators" field if you can identify them)
  - "translators": array of translator names (optional)
  - "isbn": canonical ISBN-13 (13 digits, no hyphens) if clearly stated
  - "series": series name (optional)
  - "series_index": position in series (optional, string)
  - "publisher": publisher name (optional)
  - "year": publication year as integer (optional)
  - "language": ISO 639-2 code like "ces", "slk", "eng" (optional)

IMPORTANT JSON rules (GLM models frequently get these wrong):
  - Use JSON null, NOT Python None.
  - Use JSON true/false, NOT Python True/False.
  - No trailing commas before } or ].
  - Omit a field entirely rather than emitting an empty string "".
  - Keep the JSON short. Do not include any key you are not confident about.
  - "genres": array of 1-3 literary genre tags in Czech (e.g. ["sci-fi"],
    ["fantasy","série"], ["detektivka"], ["naučná literatura"],
    ["populárně-naučná"], ["román"], ["povídky"], ["poezie"],
    ["historický román"], ["horor"], ["thriller"], ["dobrodružný"],
    ["romantický"], ["dětská literatura"], ["náboženský text"],
    ["učebnice"], ["skripta"], ["technická dokumentace"]).
    Infer from author's typical genre and first-page content if not explicit.
  - "confidence": one of "low", "medium", "high" — how confident you are
  - "reasoning": one short sentence explaining your reasoning

Rules:
  - The first-page text is the MOST RELIABLE source. Trust it over the
    corrupted metadata fields.
  - For C1 (swap): if the title looks like an author name and vice versa,
    swap them.
  - For C4 (mojibake): reconstruct the correct Czech text. If the bytes are
    unrecoverable, leave the field empty rather than guessing.
  - For C2 (filename-as-title): extract the title from the first-page text.
    Do NOT guess based on the filename alone.
  - Always use proper Czech/Slovak diacritics (á č ď é ě í ň ó ř š ť ú ů ý ž).
  - If you cannot determine a field with reasonable confidence, omit it.
"""


def build_user_prompt(evidence: dict[str, Any]) -> str:
	"""Build the user-turn prompt from the evidence dict."""
	cat = evidence.get("category", "?")
	current = evidence.get("current", {})
	first_page = (evidence.get("first_page_text") or "")[:2000]
	file_name = evidence.get("file_name", "")
	author_folder = evidence.get("author_folder", "")
	title_folder = evidence.get("title_folder", "")

	lines = [
		f"Category: {cat}",
		"",
		"Current (corrupted) metadata:",
		f"  title: {current.get('title')!r}",
		f"  authors: {current.get('authors') or current.get('author')!r}",
		f"  isbn: {current.get('isbn')!r}",
		f"  year: {current.get('year')!r}",
		f"  publisher: {current.get('publisher')!r}",
		"",
		f"File on disk: {file_name!r}",
		f"Author folder: {author_folder!r}",
		f"Title folder: {title_folder!r}",
		"",
		"First-page text from the book (most reliable source):",
		"---",
		first_page,
		"---",
		"",
	]
	# Self-correction feedback from a previous failed attempt (reconcile_loop).
	# Tells the model exactly why its last answer was rejected so it can fix it.
	feedback = evidence.get("feedback")
	if feedback:
		lines += [
			"Your previous answer was REJECTED because:",
			f"  {feedback}",
			"Try again, correcting the problem. Read the first-page text carefully.",
			"",
		]
	lines.append("Return the corrected metadata as JSON.")
	return "\n".join(lines)


class ZaiProvider(LLMProvider):
	"""Z.AI provider via the OpenAI-compatible API."""

	name = "zai"

	# Z.AI sub-codes behind HTTP 429 (see docs.z.ai/api-reference/api-code).
	# They need OPPOSITE reactions, so the raw status alone must never drive
	# the cooldown decision:
	#   1302 'Rate limit reached for requests' — OUR request rate tripped the
	#        RPM window; the escalating global fleet cooldown is the cure.
	#   1305 'The service may be temporarily overloaded' — SERVER-side capacity
	#        (chronically frequent on the free flash models). Slowing down
	#        cannot fix it; a short interval-spaced retry usually recovers.
	#        Must NOT arm the global cooldown: treating 1305 as 1302 turned a
	#        transient overload into permanent 60s fleet lockouts.
	#   1308 'Usage limit reached' — the model's usage quota is exhausted;
	#        retrying is pointless, the model is disabled for the run.
	#   1113 'Insufficient balance or no resource package. Please recharge.'
	#        — arrives (oddly) with HTTP 429. Documented as a hard billing
	#        error, BUT on the coding endpoint it also fires INTERMITTENTLY
	#        under concurrent load with quota clearly left (observed: serial
	#        calls succeed, a 10-worker fallback herd trips it, dashboard
	#        shows plenty free). So it gets the transient treatment — short
	#        retries, then a time-boxed pause — never a run-long disable nor
	#        the global fleet cooldown; a genuine balance outage just costs
	#        one probe per pause window.
	RATE_LIMIT_CODE = "1302"
	OVERLOADED_CODE = "1305"
	USAGE_LIMIT_CODE = "1308"
	BALANCE_CODE = "1113"

	# Short retries a single _call may spend on a transient 429 (1305/1113)
	# before giving up on the model for that call (the loop then falls back
	# to the paid model). Keep SMALL: the streak pause below parks a
	# distressed model fleet-wide within seconds, so a long per-call budget
	# only burns shared drip slots that the other model needs. A sporadic
	# single 1305 recovers on the first retry.
	OVERLOAD_RETRIES = 2

	# Per-model streak circuit breaker for transient 429s (1305 overload,
	# 1113 balance rejection): FOUR consecutive rejections across ALL
	# calls/workers (reset by any HTTP 200 on the model) pause it for
	# OVERLOAD_PAUSE_SEC. This is the throughput keystone: a half-dead free
	# flash pool (2-3 rejections, one 200, repeat) never trips the old
	# "whole-budget burnout" trigger, yet its retries silently eat the shared
	# RPM drip that the paid fallback needs — with the streak, the fleet
	# parks flash within seconds of a wave and every drip slot goes to the
	# model that actually answers. 3 minutes, not 10: re-probing a paused
	# model costs one call, and 1305 waves / 1113 metering bursts last
	# minutes, not tens of them.
	OVERLOAD_STREAK = 4
	OVERLOAD_PAUSE_SEC = 180.0

	# 1113 rejection bursts track flash storms / account-level throttling and
	# pass in tens of seconds. The fallback model it hits is usually the ONLY
	# working capacity at that moment — parking it for minutes would turn a
	# brief burst into minutes of zero LLM output — so its pause is much
	# shorter: books fail fast for ~30 s, then the model is probed again.
	BALANCE_PAUSE_SEC = 30.0

	# Hard cap on CONCURRENT in-flight HTTP requests to Z.AI, across all
	# workers/models/retries. Measured on the coding plan (2026-08-28): the
	# endpoint admits only ~5 simultaneous requests per ACCOUNT — a 12-deep
	# spike drew 7x 429/1302 'Rate limit reached for requests' while serial
	# calls and a 6-deep burst passed cleanly. Z.AI publishes no exact numbers:
	# per docs.z.ai/devpack/usage-policy the (concurrency) limits are TIED TO
	# THE PLAN TIER (Max > Pro > Lite) and adjusted DYNAMICALLY with resource
	# availability (and boosted off-peak), so ~5 is the measured ceiling of one
	# Pro-tier evening, not a contract. The leaky bucket spaces call STARTS but
	# not call DEPTH: with 10 pipeline workers and multi-second reasoning
	# calls, the fallback herd (flash dies -> every worker pivots to the paid
	# model at once) blew past the ceiling, and the resulting 1302 storms +
	# metering chaos also surfaced as FALSE 1113 'insufficient balance' with
	# quota clearly left (docs.z.ai/devpack/faq officially acknowledges 1113
	# firing on a purchased coding package). The semaphore QUEUES workers
	# before Z.AI can reject them; interactive clients (ZCode / chat) draw
	# from the same account ceiling, so the default keeps headroom.
	MAX_INFLIGHT_CALLS = 3

	# Extra, stricter in-flight cap for FLASH-family models (any model whose
	# name contains 'flash'). The free flash pool is chronically capacity-
	# saturated (1305 waves, evenings ~60 % rejections) and community reports
	# put its coding-plan concurrency as low as 1 (undocumented by Z.AI);
	# pushing a deep herd into it only feeds the 1305 retry storm. Flash calls
	# hold BOTH semaphores (this one inside the global one), so a flash wave
	# can never squeeze the paid fallback out of the global slots entirely.
	FLASH_INFLIGHT_CALLS = 2

	# How many clean 200s earn a yielded in-flight slot back (_InflightGate).
	# ~20 successes at the default 2 s drip ≈ 40 s of calm traffic before bmf
	# re-deepens — the external client that caused the yield gets a fair,
	# quiet window, and recovery is still quick when it was a blip.
	INFLIGHT_RECOVER_SUCCESSES = 20

	# Default minimum interval between LLM requests, in seconds. Z.AI's coding
	# plan applies a dynamic RPM (requests-per-minute) limit; 429 'Rate limit
	# reached for requests' (code 1302) fires when too many calls land inside a
	# rolling window. A floor interval of 2.0s caps us at ~30 RPM regardless of
	# how many worker threads are firing calls or how fast the API responds,
	# which is the safest match for the documented dynamic RPM cap.
	DEFAULT_MIN_INTERVAL = 2.0

	# The PaaS (pay-as-you-go) endpoint. Coding-plan keys work here too, and
	# the GLM-4.x flash models are served from the FREE tier on it — separate
	# from the coding plan's concurrency ceiling AND its credit quota.
	PAAS_BASE_URL = "https://api.z.ai/api/paas/v4/"

	def __init__(self, api_key: str, base_url: str = "https://api.z.ai/api/paas/v4/", *, model: str = "glm-4.7-flash", fallback_model: str = "glm-5.3", min_interval: float | None = None, reasoning_effort: str | None = None, thinking: str | None = None, burst: float = 1.0, rate_limit_base: float = 5.0, rate_limit_max: float = 60.0, max_inflight: int | None = None, flash_base_url: str | None = None) -> None:
		self.api_key = api_key
		self.base_url = base_url
		# Endpoint split (measured 2026-08-28): the coding endpoint's ~5-request
		# concurrency ceiling and the PaaS endpoint's are INDEPENDENT, and a
		# coding-plan key may call glm-4.x flash on PaaS for FREE (the paid
		# models 1113 there — no cash balance — and glm-5.x flash is not served,
		# 400/1210 — those stay on the primary). Default AUTO: split when the
		# primary is the coding endpoint; ZAI_FLASH_BASE_URL=<url> forces a
		# URL, =off/0 disables the split (everything on the primary).
		self.flash_base_url = self._resolve_flash_base_url(base_url, flash_base_url)
		self._client = None
		self._flash_client = None
		# Per-model reasoning controls. GLM-5.x exposes reasoning_effort
		# (low|medium|max); GLM-4.x exposes a binary thinking toggle
		# (enabled|disabled). We pick the right one based on the model family
		# and forward it as extra_body to the OpenAI client (Z.AI reads it).
		# See scripts/llm_experiment.py for the token/quality tradeoffs that
		# informed the defaults (glm-5.2 + reasoning_effort=low).
		self._extra_body: dict[str, Any] = {}
		is_glm5 = model.lower().startswith("glm-5")
		if is_glm5 and reasoning_effort:
			self._extra_body["reasoning_effort"] = reasoning_effort
		elif not is_glm5 and thinking:
			self._extra_body["thinking"] = {"type": thinking}
		# Leaky-bucket rate limiter (count-per-time) shared across ALL model
		# calls (Flash + final + retries). With the default burst=1 it is a
		# pure even drip — one call starts every interval seconds, no bunching
		# — which is what Z.AI's sliding-window request limit wants. Raise
		# burst only with rate headroom; a burst of N inside one second is what
		# trips the dynamic RPM limit (429 code 1302).
		# The drip is ADAPTIVE: the configured interval is only the floor.
		# Z.AI's real request ceiling is dynamic — observed: four 1302s in
		# two minutes at a steady 30 RPM one evening, none the next day —
		# so a fixed interval is either wasteful (set below the ceiling) or
		# a 429 storm (set above). 1302 and 1113 stretch the interval (x1.3,
		# capped), every HTTP 200 shrinks it back (x0.97, floored); within a
		# minute or two it settles at whatever the account is actually
		# allowed right now. 1305 does NOT stretch (it is server capacity,
		# not our rate — the streak pause handles it).
		interval = self.DEFAULT_MIN_INTERVAL if min_interval is None else min_interval
		self._interval_floor = max(0.0, interval)
		self._interval_cap = max(self._interval_floor * 4.0, 8.0)
		self._burst = max(0.0, burst)
		self._inflight_max = max(1, self.MAX_INFLIGHT_CALLS if max_inflight is None else max_inflight)
		self._bucket = LeakyBucket(capacity=burst, interval=self._interval_floor)
		# In-flight concurrency caps (see MAX_INFLIGHT_CALLS / FLASH_INFLIGHT_CALLS).
		# The bucket caps how often calls START; these cap how many are RUNNING
		# at once. Held only around the HTTP request itself — cooldown waits and
		# bucket acquire must never sit inside a semaphore, or a paused fleet
		# would deadlock on slots instead of waiting on the clock. Flash calls
		# acquire the flash semaphore INSIDE the global one (fixed order, no
		# deadlock), so the two compose: flash <= FLASH_INFLIGHT_CALLS and
		# total <= the global cap. The user knob scales the global cap and
		# clamps flash with it (--llm-max-inflight 1 pins everything to 1).
		# The gate is adaptive (_InflightGate): it yields slots when
		# Z.AI signals ceiling pressure on ITS endpoint (an external client on
		# the same plan cannot be seen, only its effect) and earns them back
		# on 200 streaks.
		self._call_sem = _InflightGate(self._inflight_max, self.INFLIGHT_RECOVER_SUCCESSES)
		self._flash_sem = threading.Semaphore(max(1, min(self.FLASH_INFLIGHT_CALLS, self._inflight_max)))
		# Global rate-limit cooldown (circuit breaker) shared across ALL worker
		# threads. The leaky bucket caps the steady-state call rate per worker,
		# but Z.AI's free tier has a cascade-cooldown bug: when ONE model gets a
		# 429, the others (including the paid fallback) get throttled too for
		# several seconds. Without coordination, every worker keeps firing and
		# every call 429s. So when ANY call observes a 429, we set a global
		# "cooldown until" timestamp that _all_ threads wait on before their
		# next acquire — one 429 pauses the whole fleet instead of hammering.
		# The cooldown escalates with consecutive 429s (and honours Retry-After
		# when Z.AI sends it), capped at rate_limit_max.
		self._rate_limit_base = max(0.0, rate_limit_base)
		self._rate_limit_max = max(self._rate_limit_base, rate_limit_max)
		self._cooldown_until = 0.0  # monotonic timestamp; callers block until past it
		self._consecutive_429 = 0
		# The whole rate machinery (bucket, cooldown, escalation, in-flight
		# gate) is PER ENDPOINT URL: with the flash split the two endpoints
		# have independent limiters (measured), so pressure on one must never
		# throttle the other. The PRIMARY endpoint's state lives in the
		# classic attributes above; every other URL gets its own set here,
		# created lazily with the same configuration.
		self._ep_bucket: dict[str, LeakyBucket] = {}
		self._ep_gate: dict[str, _InflightGate] = {}
		self._ep_cooldown: dict[str, float] = {}
		self._ep_429: dict[str, int] = {}
		self._cooldown_lock = threading.Lock()
		# Models disabled for the rest of the run (429/1308 usage quota
		# exhausted, 429/1113 no balance): model -> reason. _call
		# short-circuits them without an API hit.
		self._disabled_models: dict[str, str] = {}
		# Per-model sustained-overload circuit breaker (429/1305 + 429/1113):
		# consecutive rejections (reset by any 200) and a skip-until deadline
		# once the streak trips. Guarded by _cooldown_lock (shared small-state
		# lock).
		self._fail_streak: dict[str, int] = {}
		self._overload_until: dict[str, float] = {}
		# Throttle for the "every model is paused" notice (once per minute).
		self._next_idle_log = 0.0
		# Models used by reconcile_loop: self.model is the (free) first-attempt
		# model — glm-4.7-flash, best CZ/SK quality among free models per
		# scripts/llm_experiment.py; fallback_model is the paid high-quality
		# model (glm-5.3 low). Both are resolved by resolve_models() in
		# get_provider, which knows the loop on/off default split. With the
		# loop disabled, self.model IS the single-call model (resolved to the
		# fallback-quality one).
		self.model = model
		self.fallback_model = fallback_model

	@classmethod
	def _resolve_flash_base_url(cls, base_url: str, flash_base_url: str | None) -> str | None:
		"""Resolve the flash-family endpoint (None = use the primary for all)."""
		if flash_base_url:
			if flash_base_url.strip().lower() in ("off", "0", "no"):
				return None
			return flash_base_url.strip()
		# AUTO: a coding-plan primary gets glm-4.x flash from the free PaaS
		# tier — separate concurrency pool, zero coding credits.
		if "/api/coding/" in base_url:
			return cls.PAAS_BASE_URL
		return None

	def _endpoint_for(self, model: str) -> str:
		"""Which base URL serves *model* (see flash_base_url)."""
		if (
			self.flash_base_url
			and "flash" in model.lower()
			# glm-5.x flash is Devpack-native: NOT served on PaaS (400/1210).
			and not model.lower().startswith("glm-5")
		):
			return self.flash_base_url
		return self.base_url

	def _extra_body_for(self, model: str) -> dict[str, Any]:
		"""Pick the right reasoning/thinking knobs for *model*."""
		if model.lower().startswith("glm-5"):
			effort = self._extra_body.get("reasoning_effort", "low")
			return {"reasoning_effort": effort}
		return {"thinking": {"type": "disabled"}}

	# ------------------------------------------------------------------
	# Global rate-limit cooldown (shared across all worker threads)
	# ------------------------------------------------------------------

	def _bucket_for(self, url: str) -> LeakyBucket:
		"""The drip bucket serving *url* (per endpoint — see _ep_bucket)."""
		if url == self.base_url:
			return self._bucket
		return self._ep_bucket.setdefault(url, LeakyBucket(capacity=self._burst, interval=self._interval_floor))

	def _gate_for(self, url: str) -> _InflightGate:
		"""The in-flight gate serving *url* (per endpoint — see _ep_gate)."""
		if url == self.base_url:
			return self._call_sem
		return self._ep_gate.setdefault(url, _InflightGate(self._inflight_max, self.INFLIGHT_RECOVER_SUCCESSES))

	def _cooldown_until_for(self, url: str) -> float:
		if url == self.base_url:
			return self._cooldown_until
		return self._ep_cooldown.get(url, 0.0)

	def _wait_cooldown(self, url: str) -> None:
		"""Block until any active rate-limit cooldown on *url*'s endpoint has
		elapsed.

		Called at the top of every _call attempt, before the bucket acquire, so
		that a 429 observed by one thread pauses every worker on THAT endpoint
		(the cascade-cooldown coordination — scoped per endpoint because the
		endpoints' limiters are independent). Re-checks the deadline
		periodically (a 429 on another thread can extend it while we wait)
		rather than sleeping the full gap in one go.
		"""
		while True:
			with self._cooldown_lock:
				now = time.monotonic()
				until = self._cooldown_until_for(url)
				if now >= until:
					return
				# Sleep at most ~1s, then re-check: another thread may push the
				# deadline out with another 429 while we nap.
				wait = min(1.0, until - now)
			time.sleep(wait)

	def _on_rate_limited(self, url: str, retry_after: float | None) -> float:
		"""Record an observed 429 on *url*'s endpoint; return the cooldown (s).

		Escalates with consecutive 429s (base * 2**(n-1): 5, 10, 20, ...) and
		honours the server's Retry-After when it is longer. Capped at
		``rate_limit_max`` so a sustained outage doesn't park workers forever.
		"""
		with self._cooldown_lock:
			if url == self.base_url:
				self._consecutive_429 += 1
				n = self._consecutive_429
			else:
				n = self._ep_429.get(url, 0) + 1
				self._ep_429[url] = n
			escalated = self._rate_limit_base * (2 ** (n - 1))
			if retry_after and retry_after > escalated:
				cooldown = retry_after
			else:
				cooldown = escalated
			cooldown = min(cooldown, self._rate_limit_max)
			until = time.monotonic() + cooldown
			if url == self.base_url:
				self._cooldown_until = until
			else:
				self._ep_cooldown[url] = until
			return cooldown

	def _on_success(self, url: str) -> None:
		"""Any HTTP 200 on *url*'s endpoint: reset ITS escalation counter.

		Called the moment a response arrives (before content/JSON checks) — a
		200 proves the endpoint is serving us again even when the payload then
		fails to parse, and leaving stale escalation around made one bad-JSON
		streak behave like a sustained rate limit. We deliberately do NOT clear
		an active cooldown deadline — letting it expire on its own keeps
		behaviour predictable and avoids a thundering herd of waiting threads
		all unblocking the instant one call sneaks through.
		"""
		with self._cooldown_lock:
			if url == self.base_url:
				self._consecutive_429 = 0
			else:
				self._ep_429[url] = 0
		# A clean 200 feeds the in-flight recovery (_InflightGate.on_success
		# is a no-op while the gate is at its configured max).
		self._gate_for(url).on_success()

	@staticmethod
	def _is_rate_limit(exc: BaseException) -> bool:
		"""True if *exc* is a rate-limit (429) response from the provider.

		Checks the openai exception type when available, then falls back to a
		message substring match (the observed Z.AI error string is
		``Error code: 429 - {'error': {'code': '1302', 'message': 'Rate limit
		reached for requests'}}``).
		"""
		try:
			from openai import RateLimitError
			if isinstance(exc, RateLimitError):
				return True
		except ImportError:
			pass
		msg = str(exc).lower()
		return "429" in msg or "rate limit" in msg or "rate_limit" in msg

	@staticmethod
	def _extract_retry_after(exc: BaseException) -> float | None:
		"""Best-effort parse of a Retry-After hint from a 429 response.

		The openai client attaches the underlying httpx Response as
		``exc.response``; Z.AI may send ``Retry-After`` (seconds) or
		``retry-after-ms`` (milliseconds). Returns None if unavailable.
		"""
		resp = getattr(exc, "response", None)
		headers = getattr(resp, "headers", None)
		if not headers:
			return None
		try:
			for key in ("retry-after-ms", "Retry-After", "retry-after"):
				if key in headers:
					value = headers[key]
					secs = float(value) / 1000.0 if key.endswith("-ms") else float(value)
					if secs > 0:
						return secs
		except (TypeError, ValueError):
			return None
		return None

	@staticmethod
	def _server_error(exc: BaseException) -> tuple[str | None, str]:
		"""Best-effort extraction of the provider's machine error code + message.

		The openai SDK exposes the parsed error payload as ``exc.body`` (a dict
		like ``{'error': {'code': '1305', 'message': '…'}}``). Plain exceptions
		(tests, non-SDK transports) fall back to a regex over ``str(exc)``,
		which matches the observed ``Error code: 429 - {'error': {'code': …}}``
		text. Returns (code or None, message or '').
		"""
		body = getattr(exc, "body", None)
		err = body.get("error") if isinstance(body, dict) else None
		if isinstance(err, dict):
			code = err.get("code")
			msg = err.get("message")
			if code is not None or msg:
				return (str(code) if code is not None else None, str(msg or ""))
		text = str(exc)
		m = re.search(r"'code':\s*'(\d+)'", text)
		if m:
			m2 = re.search(r"'message':\s*'([^']*)'", text)
			return m.group(1), (m2.group(1) if m2 else "")
		return None, ""

	def _disable_model(self, model: str, reason: str) -> None:
		"""Disable *model* for the rest of the run (reason is returned by
		_call and shown in the log).

		Used for 429/1308 (usage quota exhausted) and 429/1113 (no balance /
		no resource package): both reset on Z.AI's side in hours/days (or a
		recharge), not seconds, so every later _call for the model
		short-circuits without an API hit. Per model, not per provider: on the
		coding plan the free flash model keeps working while a paid fallback
		has no balance (and vice versa on the paas endpoint).
		"""
		with self._cooldown_lock:
			if model in self._disabled_models:
				return
			self._disabled_models[model] = reason
		log.warning("Z.AI: model %s disabled for this run — %s", model, reason)

	def _stretch_interval(self, url: str) -> float:
		"""Widen *url*'s endpoint drip after a rate-shaped 429 (1302, 1113).

		Z.AI's real request ceiling is dynamic — measured: four 1302s in two
		minutes at a steady 30 RPM one evening, none the next day — so a
		fixed interval is either wasteful or a 429 storm. Each stretch is
		x1.3 +0.1s capped at ~4x the floor; every HTTP 200 shrinks it back
		(x0.97, floored), so the drip re-finds the ceiling of the moment
		within a minute or two. 1305 does NOT stretch (server capacity, not
		our rate — the streak pause handles it).
		"""
		b = self._bucket_for(url)
		if self._interval_floor <= 0 or b.interval <= 0:
			return b.interval
		b.interval = min(self._interval_cap, b.interval * 1.3 + 0.1)
		return b.interval

	def _shrink_interval(self, url: str) -> None:
		"""Nudge *url*'s endpoint drip back toward the floor after a success."""
		b = self._bucket_for(url)
		if b.interval > self._interval_floor:
			b.interval = max(self._interval_floor, b.interval * 0.97)

	def _paused_for(self, model: str) -> float:
		"""Seconds left in *model*'s transient-rejection pause (0 = usable)."""
		with self._cooldown_lock:
			return self._overload_until.get(model, 0.0) - time.monotonic()

	def _on_transient_rejection(self, model: str, code: str) -> bool:
		"""Record one transient 429 rejection (1305/1113) on *model*.

		Counts consecutive rejections per model across ALL calls/workers
		(reset by any HTTP 200 on the model); at OVERLOAD_STREAK it arms a
		time-boxed pause during which _call short-circuits the model so the
		fleet goes straight to the fallback instead of bleeding drip slots
		into a model that is not answering. The pause length follows the
		code: 1305 capacity waves last minutes (OVERLOAD_PAUSE_SEC), 1113
		bursts pass in tens of seconds (BALANCE_PAUSE_SEC — and the model it
		hits is usually the only working capacity left). Returns True when
		this rejection tripped or extended the pause (the caller gives up
		the current call too — every further attempt would be rejected
		anyway).

		Rejections arriving while the pause is ALREADY active (straggler
		calls that were inside their retry loop when it armed) still extend
		it — fresh evidence the model is still dead is welcome — but are
		announced only at debug level: the fleet already knows, and a
		straggler herd re-logging "pausing model X" at WARNING seven times
		in as many seconds buried everything else in the run log.
		"""
		pause = self.OVERLOAD_PAUSE_SEC if code == self.OVERLOADED_CODE else self.BALANCE_PAUSE_SEC
		with self._cooldown_lock:
			streak = self._fail_streak.get(model, 0) + 1
			self._fail_streak[model] = streak
			if streak < self.OVERLOAD_STREAK:
				return False
			now = time.monotonic()
			already_paused = now < self._overload_until.get(model, 0.0)
			self._overload_until[model] = now + pause
		if already_paused:
			log.debug(
				"Z.AI: model %s rejected again while paused (streak %d); pause re-armed for another %.0fs",
				model,
				streak,
				pause,
			)
		else:
			log.warning(
				"Z.AI: %d consecutive transient rejections; pausing model %s for %.0fs, falling back until then",
				streak,
				model,
				pause,
			)
		return True

	def _get_client(self, model: str | None = None):
		"""Lazy-init the OpenAI client for *model*'s endpoint (avoids import
		error if openai not installed). One pooled client PER ENDPOINT (the
		flash split means two base URLs can be live at once; each keeps its
		own keep-alive pool).

		Built with max_retries=0: the SDK's own retries fire OUTSIDE our leaky
		bucket (invisible extra requests that break the count-per-time
		guarantee and can trip the very RPM limit the bucket matches), and they
		silently absorb 429/1305 overloads before _call can classify them. All
		retry timing lives in _call, next to the bucket, the cooldown, and the
		429 sub-code dispatch.
		"""
		base = self._endpoint_for(model) if model else self.base_url
		is_flash_ep = base != self.base_url
		if is_flash_ep:
			if self._flash_client is not None:
				return self._flash_client
		elif self._client is not None:
			return self._client
		try:
			from openai import OpenAI
		except ImportError as e:
			raise RuntimeError("openai package not installed; run: pip install openai") from e
		# Explicit pooled httpx client (one per endpoint, shared by every
		# thread — connection reuse = HTTP keep-alive):
		#   * keepalive_expiry=60 s outlives the typical 30 s balance pause
		#     / 5-20 s cooldowns, so a parked fleet does not pay a fresh
		#     TLS handshake for every re-start (httpx's 5 s default would
		#     drop the connection during any pause);
		#   * the pool is sized to the in-flight caps — deeper than that
		#     only invites Z.AI to see overlapping bursts;
		#   * read timeout 180 s is generous for max_tokens=8000 reasoning
		#     calls yet finite: the SDK's 600 s default would let one hung
		#     call squat a scarce in-flight slot for ten minutes.
		import httpx

		pool = self._gate_for(base).cap + 2
		client = OpenAI(
			api_key=self.api_key,
			base_url=base,
			max_retries=0,
			http_client=httpx.Client(
				limits=httpx.Limits(max_connections=pool, max_keepalive_connections=pool, keepalive_expiry=60.0),
				timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
			),
		)
		if is_flash_ep:
			self._flash_client = client
		else:
			self._client = client
		return client

	def _call(self, model: str, evidence: dict[str, Any], *, max_retries: int = 3) -> tuple[ReconciledMeta | None, str | None]:
		"""Single LLM call to *model* with retry/backoff. Returns (result, error).

		Rate limiting is layered, and the 429 SUB-CODE decides which layer
		fires (they mean opposite things — see the class constants):
		  1. ``_wait_cooldown`` blocks the thread if a 1302 elsewhere parked
		     the fleet (cascade-cooldown coordination).
		  2. the leaky-bucket caps the steady-state call rate (also on retries).
		  3. 1302 (real RPM limit) → escalating global cooldown, retry.
		     1305 (server overloaded) and 1113 (intermittent balance
		     rejection) → transient: short interval-spaced retries that do
		     NOT arm the global cooldown; after OVERLOAD_RETRIES give up this
		     call so reconcile_loop can fall back to another model, and
		     OVERLOAD_STREAK consecutive rejections across the fleet pause
		     the model for OVERLOAD_PAUSE_SEC (later books skip straight to
		     the fallback).
		     1308 (usage limit) → the model is disabled for the run.
		On a hard failure (length / exhausted) returns (None, reason) so the
		caller can fall through to the next model.
		"""
		disabled = self._disabled_models.get(model)
		if disabled:
			return None, f"{disabled}; model {model} disabled for this run"
		pause_left = self._paused_for(model)
		if pause_left > 0:
			return None, f"service overloaded (429/1305); model {model} paused for another {pause_left:.0f}s"
		prompt = build_user_prompt(evidence)
		last_error: str | None = None
		extra = self._extra_body_for(model)
		overload_budget = self.OVERLOAD_RETRIES
		url = self._endpoint_for(model)
		bucket = self._bucket_for(url)
		gate = self._gate_for(url)
		# Ceiling-pressure debounce: one _call yields at most ONE in-flight
		# slot (a 1302 retry cascade inside a single call must not shrink the
		# gate several times; the endpoint cooldown already parks the fleet).
		pressured = False
		attempt = 0
		while attempt < max_retries:
			# The pause may have armed while this call was handling an error
			# or waiting on a bucket token (another worker's streak tripped):
			# give up here instead of feeding a model the fleet just parked —
			# a straggler's retries would only re-arm the pause they ignore
			# and burn drip slots the fallback needs (measured: flash drew
			# 1302s while its own 180 s pause was running).
			pause_left = self._paused_for(model)
			if pause_left > 0:
				return None, f"service overloaded (429/1305); model {model} paused for another {pause_left:.0f}s"
			# Wait out any active rate-limit cooldown on THIS endpoint before
			# acquiring a token. The cascade-cooldown fix: a 429 on one thread
			# parks all workers on that endpoint (per endpoint — the two
			# endpoints' limiters are independent).
			self._wait_cooldown(url)
			# Acquire a rate-limit token BEFORE the HTTP call. The bucket
			# smooths concurrent workers; retries also acquire, so a flapping
			# endpoint cannot exceed the configured rate while backing off.
			bucket.acquire()
			try:
				client = self._get_client(model)
				# Flash-family models hold both semaphores (the model-family
				# flash sub-cap inside the endpoint gate — fixed order); other
				# models hold the endpoint gate only. See FLASH_INFLIGHT_CALLS.
				sems = [gate]
				if "flash" in model.lower():
					sems.append(self._flash_sem)
				for s in sems:
					s.acquire()
				try:
					resp = client.chat.completions.create(
						model=model,
						messages=[
							{"role": "system", "content": SYSTEM_PROMPT},
							{"role": "user", "content": prompt},
						],
						temperature=0.1,
						max_tokens=8000,
						extra_body=extra or None,
					)
				finally:
					for s in reversed(sems):
						s.release()
			except Exception as e:  # noqa: BLE001
				if self._is_rate_limit(e):
					code, srv_msg = self._server_error(e)
					if code in (self.OVERLOADED_CODE, self.BALANCE_CODE):
						# Transient per-model failures: 1305 server overload,
						# 1113 intermittent balance rejection (see the class
						# constants). Retry shortly — the next loop pass
						# re-acquires a bucket token, so retries stay
						# interval-spaced, and neither the escalation counter
						# nor the global cooldown is touched. A rejection
						# streak across the fleet parks the model outright.
						what = "service overloaded" if code == self.OVERLOADED_CODE else "insufficient balance"
						if self._on_transient_rejection(model, code):
							if code == self.BALANCE_CODE and not pressured:
								# A false 1113 is ceiling pressure too (see
								# _InflightGate) — yield a slot for external clients.
								pressured = True
								gate.on_pressure(f"429/{code}")
							return None, f"{what} ({code})"
						if code == self.BALANCE_CODE:
							# Rate-shaped rejection: widen the drip a notch.
							self._stretch_interval(url)
						if overload_budget > 0:
							overload_budget -= 1
							log.info(
								"Z.AI %s (429/%s); retrying, %d short retry(ies) left (model=%s)",
								what,
								code,
								overload_budget,
								model,
							)
							continue
						log.warning("Z.AI %s (429/%s); retry budget spent, giving up this call (model=%s)", what, code, model)
						return None, f"{what} ({code})"
					if code == self.USAGE_LIMIT_CODE:
						reason = f"usage limit reached (429/1308: {srv_msg})"
						self._disable_model(model, reason)
						return None, reason
					# 1302, or an unknown 429 code (conservatively read as our
					# fault): set a global cooldown so all workers pause, then
					# retry (the next loop's _wait_cooldown blocks until it
					# elapses). Also widen the drip — a 1302 means the current
					# interval is above the account's real ceiling.
					cooldown = self._on_rate_limited(url, self._extract_retry_after(e))
					drip = self._stretch_interval(url)
					# 1302 = the account ceiling is exhausted right now (possibly
					# by an external client on the same plan) — yield a slot.
					if not pressured:
						pressured = True
						gate.on_pressure(f"429/{code or '1302'}")
					log.info(
						"Z.AI rate-limited (429/%s %s); global cooldown %.1fs across all workers, drip -> %.1fs (model=%s)",
						code or "?",
						srv_msg,
						cooldown,
						drip,
						model,
					)
					last_error = "rate limited"
					attempt += 1
					continue
				last_error = str(e)
				# Exponential backoff for other transient failures.
				time.sleep(1.0 * (2 ** attempt))
				attempt += 1
				continue

			# Any HTTP 200 means the endpoint is serving us again — reset the
			# 429 escalation before looking at the content (a parse failure is
			# not a rate problem) and clear the model's rejection streak, and
			# let the drip ease back toward the configured floor.
			self._on_success(url)
			self._shrink_interval(url)
			with self._cooldown_lock:
				self._fail_streak[model] = 0
			choice = resp.choices[0]
			content = choice.message.content or ""
			if not content.strip():
				finish = choice.finish_reason
				if finish == "length":
					log.warning("Z.AI ran out of tokens during reasoning (model=%s). Consider a non-reasoning model.", model)
					return None, "length (reasoning too long)"
				log.debug("Z.AI returned empty content (model=%s, attempt %d/%d)", model, attempt + 1, max_retries)
				last_error = "empty response"
				time.sleep(1.0 * (attempt + 1))
				attempt += 1
				continue
			result = _parse_llm_json(content, model=model)
			if result is not None:
				reasoning = getattr(choice.message, "reasoning_content", None)
				if reasoning and not result.reasoning:
					result.reasoning = reasoning[-300:]
				return result, None
			last_error = "json parse failed"
			if attempt < max_retries - 1:
				time.sleep(0.5)
			attempt += 1
		return None, last_error

	def reconcile(self, evidence: dict[str, Any]) -> ReconciledMeta | None:
		"""Single LLM call to self.model (back-compat for callers not using the loop).

		With the loop disabled, get_provider resolves self.model to the
		(fallback-quality) model, so this is the loop-off single-call path.
		"""
		result, error = self._call(self.model, evidence)
		if error and result is None:
			log.warning("Z.AI reconcile gave up: %s", error)
		return result

	def reconcile_loop(self, evidence: dict[str, Any], extracted: Any = None, *, max_flash: int = 2, verifier: Any = None) -> tuple[ReconciledMeta | None, str]:
		"""Self-correction loop: cheap loop model first, paid fallback second.

		Flow (each LLM call goes through the shared leaky-bucket, so the
		aggregate request rate stays constant regardless of loop depth):

		  1. The (free flash) loop model up to *max_flash* times.
		     After the first attempt, the verifier's feedback is injected
		     into the evidence so the model can correct itself.
		  2. If the loop model is unusable right now (429/1302 rate-limited,
		     429/1305 overloaded with the retry budget spent, 429/1308 quota
		     exhausted, 429/1113 no balance) or still fails verify after
		     *max_flash* attempts, the paid fallback_model (default glm-5.3
		     low) is tried once.
		  3. If the fallback model also fails verify (or there is no text to
		     verify against), the last non-empty proposal is returned with
		     confidence="low" so the human reviewer still sees something.

		Returns (result, source) where source is one of:
		  'llm:flash'   — loop model passed verify
		  'llm:loop'    — loop model passed verify after feedback
		  'llm:high'    — fallback model passed verify
		  'llm:low'     — nothing passed verify; last proposal returned as-is
		  ''            — every call returned None (nothing to show)
		"""
		verifier_fn = verifier or _default_verifier
		last_result: ReconciledMeta | None = None
		# Try the free loop model up to max_flash times, carrying feedback.
		fb = ""
		for attempt in range(max_flash):
			attempt_ev = dict(evidence)
			if fb:
				attempt_ev["feedback"] = fb
			result, error = self._call(self.model, attempt_ev)
			if result is not None:
				last_result = result
				if extracted is None:
					# Nothing to verify against — accept the loop model's result.
					return result, "llm:flash" if attempt == 0 else "llm:loop"
				passed, new_fb = verifier_fn(result, extracted)
				if passed:
					return result, "llm:flash" if attempt == 0 else "llm:loop"
				fb = new_fb
				log.debug("Loop model attempt %d failed verify: %s", attempt + 1, new_fb[:120])
			elif error and any(k in error.lower() for k in ("rate", "overload", "usage limit", "balance")):
				# The loop model is unusable right now — rate-limited (1302),
				# overloaded (1305), quota-exhausted (1308) or unfundable
				# (1113). Bail to the paid model immediately rather than
				# burning more loop attempts that will also fail. A model
				# that is merely PAUSED short-circuited above: every book
				# hits this while a pause runs, so it stays off the info log
				# (the pausing warning already said it once).
				if "paused for another" in error:
					log.debug("Loop model paused; falling back to %s", self.fallback_model)
				else:
					log.info("Loop model unusable (%s); falling back to %s", error, self.fallback_model)
				break
		# Final fallback: the paid high-quality model, one attempt.
		result, error = self._call(self.fallback_model, evidence)
		if result is None and error and "paused for another" in error:
			# BOTH models are paused — the LLM stage is idle until a pause
			# expires. Say so once a minute, not once per book.
			with self._cooldown_lock:
				now = time.monotonic()
				if now >= self._next_idle_log:
					self._next_idle_log = now + 60.0
					log.info("Z.AI: every model is paused right now — the LLM stage is idle until a pause expires")
		if result is not None:
			if extracted is None:
				return result, "llm:high"
			passed, _ = verifier_fn(result, extracted)
			if passed:
				return result, "llm:high"
			# Did not pass but we have a proposal — return it low-confidence.
			result.confidence = "low"
			return result, "llm:low"
		# Every call returned None. Return the last Flash proposal (if any)
		# low-confidence, else nothing.
		if last_result is not None:
			last_result.confidence = "low"
			return last_result, "llm:low"
		return None, ""


def _default_verifier(proposal: Any, extracted: Any) -> tuple[bool, str]:
	"""Default verify_proposal wrapper used when the caller does not inject one."""
	from .verifier import verify_proposal

	return verify_proposal(proposal, extracted)


# ---------------------------------------------------------------------------
# Mock provider (for tests / offline runs)
# ---------------------------------------------------------------------------


class MockProvider(LLMProvider):
	"""Deterministic mock — returns canned responses based on category.

	Useful for testing the pipeline end-to-end without API calls. Configure
	expected responses via the *responses* dict, or use the default heuristic.
	"""

	name = "mock"

	def __init__(self, responses: dict[str, ReconciledMeta] | None = None) -> None:
		self.responses = responses or {}

	def reconcile(self, evidence: dict[str, Any]) -> ReconciledMeta | None:
		cat = evidence.get("category", "?")
		if cat in self.responses:
			return self.responses[cat]
		current = evidence.get("current") or {}
		# Intentionally dumb echo of current metadata — tests should pass
		# explicit responses via the constructor.
		return ReconciledMeta(
			title=current.get("title"),
			authors=current.get("authors") or [],
			confidence="low",
			reasoning="mock provider — no real LLM call",
		)


# ---------------------------------------------------------------------------
# Factory + JSON parsing
# ---------------------------------------------------------------------------


# Provider-selection aliases for BMF_LLM_PROVIDER / --llm-provider. Unknown
# values are rejected with a warning (not silently ignored) so a typo like
# "atigravity" cannot quietly flip the run back onto the paid endpoint.
PROVIDER_ALIASES = {
	"antigravity": "acp",
	"agy": "acp",
	"acp": "acp",
	"zai": "zai",
	"glm": "zai",
	"mock": "mock",
	"off": "off",
	"none": "off",
}


def resolve_models(config: Any) -> tuple[str, str]:  # noqa: ANN001
	"""Resolve (model, fallback_model) from the config.

	llm_model is the model the loop (or the loop-off single call) tries
	first; None = default, which depends on the loop setting: the free flash
	model when the loop is on, the fallback-quality model when off (a single
	call should not waste money on a second-rate model NOR start cheap).
	"""
	fallback = getattr(config, "llm_fallback_model", None) or "glm-5.3"
	model = getattr(config, "llm_model", None)
	if not model:
		model = "glm-4.7-flash" if getattr(config, "llm_loop", True) else fallback
	return model, fallback


def _build_zai(config: Any, api_key: str) -> ZaiProvider:
	"""Construct the ZaiProvider from the config (shared by the pure-Z.AI
	selection and the Antigravity-ACP-with-Z.AI-fallback composition)."""
	# Minimum seconds between LLM requests (RPM throttle). Falls back to the
	# class default (~30 RPM) if unset. Lower (e.g. 1.0 = 60 RPM) only on a
	# higher Z.AI tier; raise (e.g. 4.0 = 15 RPM) if you still hit 429s.
	min_interval = getattr(config, "llm_min_interval", None)
	model, fallback_model = resolve_models(config)
	return ZaiProvider(
		api_key=api_key,
		base_url=getattr(config, "zai_base_url", "https://api.z.ai/api/paas/v4/"),
		model=model,
		fallback_model=fallback_model,
		min_interval=min_interval,
		reasoning_effort=getattr(config, "zai_reasoning_effort", None),
		thinking=getattr(config, "zai_thinking", None),
		burst=getattr(config, "llm_burst", 1.0),
		rate_limit_base=getattr(config, "llm_rate_limit_base", 5.0),
		rate_limit_max=getattr(config, "llm_rate_limit_max", 60.0),
		max_inflight=getattr(config, "llm_max_inflight", None),
		flash_base_url=getattr(config, "zai_flash_base_url", None),
	)


def _build_acp(config: Any, zai_fallback: ZaiProvider | None, command: list[str] | None = None) -> Any:
	"""Construct the Antigravity ACP provider with its QUALITY stage.

	BMF_ANTIGRAVITY_FALLBACK picks who serves the loop's second stage:
	  'agy' (default) — a SECOND ACP pool on the fallback model
	  (BMF_ANTIGRAVITY_FALLBACK_MODEL, default gemini-pro), so the whole
	  loop stays on the subscription; a configured Z.AI key is NOT used;
	  'glm' — the ZaiProvider's flash+paid loop (needs ZAI_API_KEY, reuses
	  Z.AI's measured rate machinery untouched).

	*command* overrides cfg.acp_command — get_provider passes the RESOLVED
	argv when the agent came from the self-managed cache (ensure_acp_agent),
	which is not expressible in the config string.
	"""
	from .acp import AntigravityAcpProvider

	command = command if command is not None else shlex.split(getattr(config, "acp_command", "") or "")
	# The agent's cwd is informational only (bmf grants no fs access); the
	# library is the natural choice but must not BREAK the provider when it
	# is a stale/unmounted path (Popen with a missing cwd fails outright).
	library = getattr(config, "library", None)
	cwd = library if library is not None and Path(library).is_dir() else "."
	fb_raw = (getattr(config, "acp_fallback_provider", "agy") or "").strip().lower()
	fb = {"agy": "acp", "acp": "acp", "antigravity": "acp", "glm": "zai", "zai": "zai"}.get(fb_raw, "")
	if not fb:
		log.warning("Unknown BMF_ANTIGRAVITY_FALLBACK %r (expected agy or glm) — using agy", fb_raw)
		fb = "acp"
	acp_fallback = None
	if fb == "acp":
		zai_fallback = None  # the agy quality stage replaces Z.AI entirely
		acp_fallback = AntigravityAcpProvider(
			command,
			cwd=cwd,
			model=getattr(config, "acp_fallback_model", None),
			prompt_timeout=getattr(config, "acp_prompt_timeout", 300.0),
			# ONE serialized lane by default (acp_fallback_max_inflight): the
			# quality stage is the exception path, and a single slot keeps the
			# subscription's concurrency for the parallel flash pool. Threads
			# queue on the pool — deliberate, see Config.acp_fallback_max_inflight.
			max_inflight=getattr(config, "acp_fallback_max_inflight", 1),
			min_interval=getattr(config, "acp_min_interval", 0.0),
		)
	elif zai_fallback is None:
		log.warning("BMF_ANTIGRAVITY_FALLBACK=glm but no ZAI_API_KEY — no quality fallback configured")
	return AntigravityAcpProvider(
		# resolve_acp_command already validated executability.
		command,
		cwd=cwd,
		model=getattr(config, "acp_model", None),
		prompt_timeout=getattr(config, "acp_prompt_timeout", 300.0),
		max_inflight=getattr(config, "acp_max_inflight", 2),
		min_interval=getattr(config, "acp_min_interval", 0.0),
		zai_fallback=zai_fallback,
		acp_fallback=acp_fallback,
	)


def get_provider(config: Any, *, progress_cb: Any = None) -> LLMProvider | None:  # noqa: ANN001
	"""Construct the configured LLM provider, or None if disabled/unavailable.

	*progress_cb(done, total)* is forwarded to the ACP agent self-install
	(the implicit ~700 MB download on a cold cache) so the CLI can render it.

	Resolution (BMF_LLM_PROVIDER / --llm-provider picks the branch):
		- 'off'  → None (the LLM stage is disabled)
		- 'mock' → MockProvider (for testing)
		- 'zai'  → ZaiProvider; requires ZAI_API_KEY, ACP never used
		- 'acp' (aliases 'antigravity'/'agy') → the Antigravity ACP agent as
		  the fast tier, with ZaiProvider (if ZAI_API_KEY exists) as the
		  loop's paid fallback; the agent comes from BMF_ANTIGRAVITY_CMD —
		  an explicit path, or empty/'auto' = bmf's SELF-MANAGED cache:
		  the run checks the registry and downloads/upgrades the agent
		  itself (see acp.ensure_acp_agent)
		- '' (auto) → the historical chain, extended one step: the ACP agent
		  when one is configured — an explicit path, the 'auto' sentinel
		  (opt-in to the self-managed cache), or an ALREADY-CACHED install
		  (kept current; a Z.AI-only user who never opted in never triggers
		  a download) — else ZaiProvider when ZAI_API_KEY is set; else
		  BMF_LLM_MOCK=1 → MockProvider; else None
	"""
	pref_raw = (getattr(config, "llm_provider", "") or "").strip().lower()
	pref = PROVIDER_ALIASES.get(pref_raw, pref_raw)
	if pref and pref not in PROVIDER_ALIASES.values():
		log.warning("Unknown BMF_LLM_PROVIDER %r (expected antigravity/acp, zai, mock, off) — treating as auto", pref_raw)
		pref = ""
	api_key = getattr(config, "zai_api_key", None) or os.environ.get("ZAI_API_KEY")
	if pref == "off":
		return None
	if pref == "mock":
		return MockProvider()
	# The Z.AI provider is built lazily-needed: as THE provider, as the ACP
	# fallback, or not at all ('off'/'mock' already returned; acp without a
	# key simply runs ACP-only).
	zai = _build_zai(config, api_key) if (api_key and pref in ("", "zai", "acp")) else None
	if pref == "zai":
		if zai is not None:
			return zai
		log.warning("BMF_LLM_PROVIDER=zai but no ZAI_API_KEY — no LLM provider")
		return None
	# 'acp' (explicit) or auto with an agent. The command resolves in order:
	# an explicit BMF_ANTIGRAVITY_CMD path (validated by resolve_acp_command,
	# which logs WHY it rejected a broken one); else the SELF-MANAGED cache:
	# an explicit agy opt-in (provider 'acp' or the 'auto' sentinel)
	# downloads/updates the agent on demand, while plain auto with an EMPTY
	# command adopts an EXISTING cache (and then keeps it current) but never
	# downloads for someone who never opted into agy.
	from .acp import ensure_acp_agent, installed_acp_release, resolve_acp_command

	command: list[str] | None = None
	if pref in ("", "acp"):
		raw_cmd = (getattr(config, "acp_command", "") or "").strip()
		if raw_cmd and raw_cmd.lower() != "auto":
			command = resolve_acp_command(raw_cmd)
		else:
			opted_in = pref == "acp" or raw_cmd.lower() == "auto"
			if opted_in or installed_acp_release() is not None:
				ensured = ensure_acp_agent(progress_cb=progress_cb)
				if ensured is not None:
					command = ensured[0]
					log.debug("ACP: using the self-managed agent %s (%s)", ensured[1], ensured[0][0])
			else:
				log.info(
					"ACP: no agent configured — set BMF_LLM_PROVIDER=antigravity (or BMF_ANTIGRAVITY_CMD=auto) and bmf will download and keep the official agent itself"
				)
	if pref == "acp":
		if command is None:
			log.warning("BMF_LLM_PROVIDER=antigravity but no usable ACP agent — the self-managed install failed (registry/network?) and no explicit BMF_ANTIGRAVITY_CMD path is set")
			return None
		return _build_acp(config, zai, command)
	# Auto: a configured ACP agent takes the fast tier (Z.AI, when a key
	# exists, drops to the loop's paid fallback); otherwise the historical
	# Z.AI → mock → none chain.
	if command is not None:
		return _build_acp(config, zai, command)
	if zai is not None:
		return zai
	if os.environ.get("BMF_LLM_MOCK"):
		return MockProvider()
	return None


def _sanitize_json(content: str) -> str:
	"""Fix common LLM JSON mistakes before parsing.

	GLM models (trained on Python) frequently emit Python literals where JSON
	is expected: ``None`` instead of ``null``, ``True``/``False`` instead of
	``true``/``false``, and trailing commas before ``}``/``]``. These are
	syntactically tiny errors but they turn an otherwise-perfect response into
	a parse failure that costs 3 retry API calls.

	We split on double-quotes and only rewrite barewords in even-indexed
	segments (outside strings), so ``"None Yet"`` as a title value is left
	alone while ``"publisher": None`` is fixed to ``"publisher": null``.
	"""
	# Split on double-quote: even indices are outside strings, odd are inside.
	# (This correctly handles the common case; escaped quotes \" inside values
	# are extremely rare in book metadata and the worst case is a missed fix.)
	parts = content.split('"')
	for i in range(0, len(parts), 2):  # outside-string segments only
		p = parts[i]
		p = re.sub(r"\bNone\b", "null", p)
		p = re.sub(r"\bTrue\b", "true", p)
		p = re.sub(r"\bFalse\b", "false", p)
		# Trailing comma before a closing brace/bracket: {"a": 1,} -> {"a": 1}
		p = re.sub(r",\s*([}\]])", r"\1", p)
		parts[i] = p
	return '"'.join(parts)


def _repair_truncated_json(content: str) -> str | None:
	"""Best-effort repair of a JSON object truncated mid-value.

	When the LLM hits the token limit mid-response the output is a valid JSON
	prefix that just ends abruptly: ``{"title": "X", "genres": ["sc``. We close
	any open arrays and objects and return something ``json.loads`` can parse.
	Returns None if no sensible repair is possible.
	"""
	# Only attempt repair on content that starts like a JSON object.
	stripped = content.strip()
	if not stripped.startswith("{"):
		return None
	# Track depth of objects/arrays and whether we're inside a string.
	depth: list[str] = []
	in_string = False
	esc = False
	for ch in content:
		if esc:
			esc = False
			continue
		if ch == "\\":
			esc = True
			continue
		if ch == '"':
			in_string = not in_string
			continue
		if in_string:
			continue
		if ch in "{[":
			depth.append(ch)
		elif ch == "}":
			if depth and depth[-1] == "{":
				depth.pop()
		elif ch == "]":
			if depth and depth[-1] == "[":
				depth.pop()
	# Strip trailing punctuation (comma/colon) that would make the closed JSON
	# invalid. Only strip if we're not inside a string.
	result = content
	if not in_string:
		result = result.rstrip()
		while result and result[-1] in ",:":
			result = result[:-1].rstrip()
	# Close an unterminated string, then close open containers innermost-first.
	suffix = ""
	if in_string:
		suffix += '"'
	for opener in reversed(depth):
		suffix += "]" if opener == "[" else "}"
	return result + suffix


def _first_json_object(content: str) -> str | None:
	"""Extract the first balanced {...} object from mixed content.

	Some responses embed the JSON in commentary: prose around it, or a valid
	object followed by a "Note: ..." explanation and a second, fenced copy.
	``json.loads`` on the whole content then dies with "Extra data" and the
	truncation repair has nothing to close — this scans brace depth (string-
	and escape-aware, so braces inside string values do not confuse it) and
	returns the first complete object. Returns None when no balanced object
	exists (plain prose, or truncation mid-object — the truncation repair
	owns that case).
	"""
	start = content.find("{")
	if start < 0:
		return None
	depth = 0
	in_string = False
	esc = False
	for i, ch in enumerate(content[start:]):
		if in_string:
			if esc:
				esc = False
			elif ch == "\\":
				esc = True
			elif ch == '"':
				in_string = False
			continue
		if ch == '"':
			in_string = True
		elif ch == "{":
			depth += 1
		elif ch == "}":
			depth -= 1
			if depth == 0:
				return content[start : start + i + 1]
	return None


def _parse_llm_json(content: str, *, model: str | None = None) -> ReconciledMeta | None:
	"""Parse the LLM's JSON response into a ReconciledMeta.

	Tolerant of three common LLM failure modes (each previously caused a
	3-retry waste of API calls + an eventual give-up):
	  1. Python literals (None/True/False) instead of JSON (null/true/false).
	  2. Truncation at the token limit — closes open braces/arrays.
	  3. JSON wrapped in commentary — the first balanced {...} object is
	     carved out and parsed on its own.

	*model* is diagnostics-only: the repair warnings name the model so the
	log shows WHO truncates (flash and the paid reasoning model fail at
	different rates — and point at different fixes).
	"""
	# Strip markdown fences if present (```json ... ```)
	content = content.strip()
	if content.startswith("```"):
		lines = content.splitlines()
		# Remove first line (```json) and last line (```)
		lines = [ln for ln in lines if not ln.strip().startswith("```")]
		content = "\n".join(lines)
	# Candidates: the whole content first (the common case — one clean
	# object), then the first balanced {...} object carved out of it. The
	# models occasionally wrap their JSON in commentary (measured
	# 2026-08-29: glm-5.3 answered with a valid object, then "Note: ..."
	# prose and a second, fenced copy) — json.loads on the whole thing dies
	# with "Extra data" although the answer sits right at the top.
	candidates = [content]
	extracted = _first_json_object(content)
	if extracted is not None and extracted != content:
		candidates.append(extracted)
	data = None
	used_extracted = False
	for idx, candidate in enumerate(candidates):
		# Fix Python literals and trailing commas (cheap, always safe to apply).
		sanitized = _sanitize_json(candidate)
		# Try parsing directly, then the sanitized version, then a truncation repair.
		for attempt_content, label in (
			(candidate, "raw"),
			(sanitized, "sanitized"),
		):
			try:
				data = json.loads(attempt_content)
				if label == "sanitized":
					log.debug("JSON parsed after sanitization (Python literals fixed)")
				break
			except json.JSONDecodeError:
				continue
		else:
			# Last resort: try repairing truncated JSON (sanitized version).
			repaired = _repair_truncated_json(sanitized)
			if repaired is not None:
				try:
					data = json.loads(repaired)
					log.warning("LLM JSON was truncated; repaired to parseable object (dropping incomplete trailing field) (model=%s)", model or "?")
				except json.JSONDecodeError:
					# Truncation repair didn't yield valid JSON either. Fall through
					# to the json-repair salvage below (handles the same content).
					data = None
			else:
				data = None
			if data is None:
				# Final salvage: json-repair recovers the failure modes the
				# cheap sanitizer cannot — unescaped quotes inside string values
				# ("reasoning": "...contains "PROLOG"...") and raw control chars
				# (newlines) inside strings. These are the most common GLM mistakes
				# and previously cost 3 wasted retry API calls each. Only used when
				# json-repair is installed (optional [llm] extra).
				if json_repair is not None:
					try:
						salvaged = json_repair.loads(candidate)
					except Exception:  # noqa: BLE001 - third-party, never fatal
						salvaged = None
					salvage_reason = "unescaped quotes/control chars fixed"
					if isinstance(salvaged, list):
						# "object + prose + object" content makes json-repair
						# return BOTH objects as a list — the first is the
						# model's answer, the second its restated
						# "recommended fields" copy.
						if salvaged and isinstance(salvaged[0], dict):
							salvaged = salvaged[0]
							salvage_reason = "leading object of wrapped response"
						else:
							salvaged = None
					if isinstance(salvaged, dict):
						log.warning("LLM JSON salvaged via json-repair (%s) (model=%s)", salvage_reason, model or "?")
						data = salvaged
		if data is not None:
			used_extracted = idx > 0
			break
	if data is None:
		log.warning("LLM returned invalid JSON; content: %s", content[:500])
		return None
	if used_extracted:
		# Logged like the truncation/json-repair warnings so this failure
		# mode's frequency is visible per model too.
		log.warning("LLM JSON extracted from commentary-wrapped response (model=%s)", model or "?")
	# Normalize field names
	def _str(k):
		v = data.get(k)
		return v.strip() if isinstance(v, str) and v.strip() else None

	def _int(k):
		v = data.get(k)
		if v is None:
			return None
		try:
			return int(str(v)[:4])
		except (ValueError, TypeError):
			return None

	def _list(k):
		v = data.get(k)
		if v is None:
			return []
		if isinstance(v, str):
			return [v]
		if isinstance(v, list):
			return [str(x).strip() for x in v if str(x).strip()]
		return []

	year = _int("year")
	if year is not None and year < 1000:
		year = None
	return ReconciledMeta(
		title=_str("title"),
		authors=_list("authors"),
		isbn=_str("isbn"),
		series=_str("series"),
		series_index=_str("series_index"),
		publisher=_str("publisher"),
		year=year,
		language=_str("language"),
		genres=_list("genres"),
		confidence=_str("confidence") or "medium",
		reasoning=_str("reasoning") or "",
	)
