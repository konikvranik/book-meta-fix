"""LLM reconciliation — use a language model to repair metadata that
deterministic rules cannot handle.

Currently used for:
	- C1 (author/title swap) — given the jumbled fields + first-page text,
	  decide which is the real author and which is the real title
	- C4 (mojibake) — reconstruct the correct Czech/SK text from corrupted bytes
	- C2 (filename-as-title, fallback only) — extract title/author from the
	  book's first-page text when no other source has them

The provider is pluggable:
	- ZaiProvider : real Z.AI API (OpenAI-compatible, glm-5.2)
	- MockProvider: deterministic responses for tests / offline runs

A provider returns a ReconciledMeta dict. Callers (pipeline/review) decide
whether to trust it (always NEEDS_REVIEW verdict — LLM output is a *proposal*,
never auto-applied).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
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

	# Default minimum interval between LLM requests, in seconds. Z.AI's coding
	# plan applies a dynamic RPM (requests-per-minute) limit; 429 'Rate limit
	# reached for requests' (code 1302) fires when too many calls land inside a
	# rolling window. A floor interval of 2.0s caps us at ~30 RPM regardless of
	# how many worker threads are firing calls or how fast the API responds,
	# which is the safest match for the documented dynamic RPM cap.
	DEFAULT_MIN_INTERVAL = 2.0

	def __init__(self, api_key: str, base_url: str = "https://api.z.ai/api/paas/v4/", model: str = "glm-5.2", *, min_interval: float | None = None, reasoning_effort: str | None = None, thinking: str | None = None, burst: float = 1.0, rate_limit_base: float = 5.0, rate_limit_max: float = 60.0, flash_model: str | None = None, final_model: str | None = None) -> None:
		self.api_key = api_key
		self.base_url = base_url
		self.model = model
		self._client = None
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
		interval = self.DEFAULT_MIN_INTERVAL if min_interval is None else min_interval
		self._bucket = LeakyBucket(capacity=burst, interval=max(0.0, interval))
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
		self._cooldown_lock = threading.Lock()
		# Models used by reconcile_loop. flash_model is the free first-attempt
		# model (default glm-4.7-flash — best CZ/SK quality among free models
		# per scripts/llm_experiment.py); final_model is the paid high-quality
		# fallback (default: self.model, i.e. glm-5.2 low).
		self.flash_model = flash_model or "glm-4.7-flash"
		self.final_model = final_model or model

	def _extra_body_for(self, model: str) -> dict[str, Any]:
		"""Pick the right reasoning/thinking knobs for *model*."""
		if model.lower().startswith("glm-5"):
			effort = self._extra_body.get("reasoning_effort", "low")
			return {"reasoning_effort": effort}
		return {"thinking": {"type": "disabled"}}

	# ------------------------------------------------------------------
	# Global rate-limit cooldown (shared across all worker threads)
	# ------------------------------------------------------------------

	def _wait_cooldown(self) -> None:
		"""Block until any active global rate-limit cooldown has elapsed.

		Called at the top of every _call attempt, before the bucket acquire, so
		that a 429 observed by one thread pauses the whole fleet. Re-checks the
		deadline periodically (a 429 on another thread can extend it while we
		wait) rather than sleeping the full gap in one go.
		"""
		while True:
			with self._cooldown_lock:
				now = time.monotonic()
				if now >= self._cooldown_until:
					return
				# Sleep at most ~1s, then re-check: another thread may push the
				# deadline out with another 429 while we nap.
				wait = min(1.0, self._cooldown_until - now)
			time.sleep(wait)

	def _on_rate_limited(self, retry_after: float | None) -> float:
		"""Record an observed 429 and return the cooldown applied (seconds).

		Escalates with consecutive 429s (base * 2**(n-1): 5, 10, 20, ...) and
		honours the server's Retry-After when it is longer. Capped at
		``rate_limit_max`` so a sustained outage doesn't park workers forever.
		"""
		with self._cooldown_lock:
			self._consecutive_429 += 1
			escalated = self._rate_limit_base * (2 ** (self._consecutive_429 - 1))
			if retry_after and retry_after > escalated:
				cooldown = retry_after
			else:
				cooldown = escalated
			cooldown = min(cooldown, self._rate_limit_max)
			self._cooldown_until = time.monotonic() + cooldown
			return cooldown

	def _on_success(self) -> None:
		"""A call completed (parseable response): reset the escalation counter.

		We deliberately do NOT clear an active cooldown — letting it expire on
		 its own keeps behaviour predictable and avoids a thundering herd of
		waiting threads all unblocking the instant one call sneaks through.
		"""
		with self._cooldown_lock:
			self._consecutive_429 = 0

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

	def _get_client(self):
		"""Lazy-init the OpenAI client (avoids import error if openai not installed)."""
		if self._client is None:
			try:
				from openai import OpenAI
			except ImportError as e:
				raise RuntimeError("openai package not installed; run: pip install openai") from e
			self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
		return self._client

	def _call(self, model: str, evidence: dict[str, Any], *, max_retries: int = 3) -> tuple[ReconciledMeta | None, str | None]:
		"""Single LLM call to *model* with retry/backoff. Returns (result, error).

		On a transient failure (429 / empty / json-parse) retries with
		exponential backoff. On a hard failure (length / exhausted) returns
		(None, reason) so the caller can fall through to the next model.

		Rate limiting is layered:
		  1. ``_wait_cooldown`` blocks the thread if a 429 elsewhere parked the
		     fleet (cascade-cooldown coordination).
		  2. the leaky-bucket caps the steady-state call rate.
		On a 429 we record a global cooldown (escalating, honouring Retry-After)
		and retry — so the next attempt of every thread waits it out.
		"""
		prompt = build_user_prompt(evidence)
		last_error: str | None = None
		extra = self._extra_body_for(model)
		for attempt in range(max_retries):
			# Wait out any global rate-limit cooldown before acquiring a token.
			# This is the cascade-cooldown fix: a 429 on one thread parks all.
			self._wait_cooldown()
			# Acquire a rate-limit token BEFORE the HTTP call. The bucket
			# smooths concurrent workers; retries also acquire, so a flapping
			# endpoint cannot exceed the configured rate while backing off.
			self._bucket.acquire()
			try:
				client = self._get_client()
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
			except Exception as e:  # noqa: BLE001
				if self._is_rate_limit(e):
					# 429: set a global cooldown so all workers pause, then retry
					# (the next loop's _wait_cooldown blocks until it elapses).
					cooldown = self._on_rate_limited(self._extract_retry_after(e))
					log.info("Z.AI rate-limited (429); global cooldown %.1fs across all workers (model=%s)", cooldown, model)
					last_error = "rate limited"
					continue
				last_error = str(e)
				# Exponential backoff for other transient failures.
				time.sleep(1.0 * (2 ** attempt))
				continue

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
				continue
			result = _parse_llm_json(content)
			if result is not None:
				# Successful parse: the endpoint is healthy — reset escalation.
				self._on_success()
				reasoning = getattr(choice.message, "reasoning_content", None)
				if reasoning and not result.reasoning:
					result.reasoning = reasoning[-300:]
				return result, None
			last_error = "json parse failed"
			if attempt < max_retries - 1:
				time.sleep(0.5)
		return None, last_error

	def reconcile(self, evidence: dict[str, Any]) -> ReconciledMeta | None:
		"""Single LLM call to self.model (back-comat for callers not using the loop)."""
		result, error = self._call(self.model, evidence)
		if error and result is None:
			log.warning("Z.AI reconcile gave up: %s", error)
		return result

	def reconcile_loop(self, evidence: dict[str, Any], extracted: Any = None, *, max_flash: int = 2, verifier: Any = None) -> tuple[ReconciledMeta | None, str]:
		"""Self-correction loop: cheap Flash first, paid GLM-5.2 as the final fallback.

		Flow (each LLM call goes through the shared leaky-bucket, so the
		aggregate request rate stays constant regardless of loop depth):

		  1. Flash (free, thinking off) up to *max_flash* times. After the
		     first attempt, the verifier's feedback is injected into the
		     evidence so the model can correct itself.
		  2. If Flash is rate-limited (429 cascade) or still fails verify after
		     *max_flash* attempts, the paid final_model (default glm-5.2 low)
		     is tried once.
		  3. If the final model also fails verify (or there is no text to
		     verify against), the last non-empty proposal is returned with
		     confidence="low" so the human reviewer still sees something.

		Returns (result, source) where source is one of:
		  'llm:flash'   — Flash passed verify
		  'llm:loop'    — Flash passed verify after feedback
		  'llm:high'    — final model passed verify
		  'llm:low'     — nothing passed verify; last proposal returned as-is
		  ''            — every call returned None (nothing to show)
		"""
		verifier_fn = verifier or _default_verifier
		last_result: ReconciledMeta | None = None
		# Try the free Flash model up to max_flash times, carrying feedback.
		fb = ""
		for attempt in range(max_flash):
			attempt_ev = dict(evidence)
			if fb:
				attempt_ev["feedback"] = fb
			result, error = self._call(self.flash_model, attempt_ev)
			if result is not None:
				last_result = result
				if extracted is None:
					# Nothing to verify against — accept the Flash result.
					return result, "llm:flash" if attempt == 0 else "llm:loop"
				passed, new_fb = verifier_fn(result, extracted)
				if passed:
					return result, "llm:flash" if attempt == 0 else "llm:loop"
				fb = new_fb
				log.debug("Flash attempt %d failed verify: %s", attempt + 1, new_fb[:120])
			elif error and "rate" in (error or "").lower():
				# Free-tier cascade: bail to the paid model immediately rather
				# than burning more Flash attempts that will also 429.
				log.info("Flash rate-limited (%s); falling back to %s", error, self.final_model)
				break
		# Final fallback: the paid high-quality model, one attempt.
		result, error = self._call(self.final_model, evidence)
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
		# Default mock: try to extract title from first-page text
		first_page = evidence.get("first_page_text") or ""
		current = evidence.get("current") or {}
		# Heuristic: look for " - " or known patterns; very crude
		# This is intentionally dumb — tests should pass explicit responses.
		return ReconciledMeta(
			title=current.get("title"),
			authors=current.get("authors") or [],
			confidence="low",
			reasoning="mock provider — no real LLM call",
		)


# ---------------------------------------------------------------------------
# Factory + JSON parsing
# ---------------------------------------------------------------------------


def get_provider(config: Any) -> LLMProvider | None:  # noqa: ANN001
	"""Construct the configured LLM provider, or None if disabled/unavailable.

	Resolution:
		1. If config.zai_api_key is set -> ZaiProvider
		2. Else if env var BMF_LLM_MOCK=1 -> MockProvider (for testing)
		3. Else -> None (LLM disabled)
	"""
	api_key = getattr(config, "zai_api_key", None) or os.environ.get("ZAI_API_KEY")
	if api_key:
		# Minimum seconds between LLM requests (RPM throttle). Falls back to the
		# class default (~30 RPM) if unset. Lower (e.g. 1.0 = 60 RPM) only on a
		# higher Z.AI tier; raise (e.g. 4.0 = 15 RPM) if you still hit 429s.
		min_interval = getattr(config, "llm_min_interval", None)
		return ZaiProvider(
			api_key=api_key,
			base_url=getattr(config, "zai_base_url", "https://api.z.ai/api/paas/v4/"),
			model=getattr(config, "zai_model", "glm-5.2"),
			min_interval=min_interval,
			reasoning_effort=getattr(config, "zai_reasoning_effort", None),
			thinking=getattr(config, "zai_thinking", None),
			burst=getattr(config, "llm_burst", 1.0),
			rate_limit_base=getattr(config, "llm_rate_limit_base", 5.0),
			rate_limit_max=getattr(config, "llm_rate_limit_max", 60.0),
			flash_model=getattr(config, "zai_flash_model", None),
			final_model=getattr(config, "zai_final_model", None),
		)
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


def _parse_llm_json(content: str) -> ReconciledMeta | None:
	"""Parse the LLM's JSON response into a ReconciledMeta.

	Tolerant of two common LLM failure modes (each previously caused a
	3-retry waste of API calls + an eventual give-up):
	  1. Python literals (None/True/False) instead of JSON (null/true/false).
	  2. Truncation at the token limit — closes open braces/arrays.
	"""
	# Strip markdown fences if present (```json ... ```)
	content = content.strip()
	if content.startswith("```"):
		lines = content.splitlines()
		# Remove first line (```json) and last line (```)
		lines = [ln for ln in lines if not ln.strip().startswith("```")]
		content = "\n".join(lines)
	# Fix Python literals and trailing commas (cheap, always safe to apply).
	sanitized = _sanitize_json(content)
	# Try parsing directly, then the sanitized version, then a truncation repair.
	for attempt_content, label in (
		(content, "raw"),
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
				log.warning("LLM JSON was truncated; repaired to parseable object (dropping incomplete trailing field)")
			except json.JSONDecodeError:
				# Truncation repair didn't yield valid JSON either. Fall through
				# to the json-repair salvage below (handles the same content).
				data = None
		else:
			data = None
		if data is None:
			# Final salvage: json-repair recovers the two failure modes the
			# cheap sanitizer cannot — unescaped quotes inside string values
			# ("reasoning": "...contains "PROLOG"...") and raw control chars
			# (newlines) inside strings. These are the most common GLM mistakes
			# and previously cost 3 wasted retry API calls each. Only used when
			# json-repair is installed (optional [llm] extra).
			if json_repair is not None:
				try:
					salvaged = json_repair.loads(content)
				except Exception:  # noqa: BLE001 - third-party, never fatal
					salvaged = None
				if isinstance(salvaged, dict):
					log.warning("LLM JSON salvaged via json-repair (unescaped quotes/control chars fixed)")
					data = salvaged
			if data is None:
				log.warning("LLM returned invalid JSON; content: %s", content[:500])
				return None
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
