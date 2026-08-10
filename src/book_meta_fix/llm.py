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
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


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
		"Return the corrected metadata as JSON.",
	]
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

	def __init__(self, api_key: str, base_url: str = "https://api.z.ai/api/paas/v4/", model: str = "glm-5.2", *, min_interval: float | None = None) -> None:
		self.api_key = api_key
		self.base_url = base_url
		self.model = model
		self._client = None
		# Rate limiter state (shared across worker threads). min_interval < 0
		# disables throttling; floor at a tiny positive value keeps behavior sane.
		interval = self.DEFAULT_MIN_INTERVAL if min_interval is None else min_interval
		self._min_interval = max(0.0, interval)
		self._last_request_at = 0.0
		self._rate_lock = threading.Lock()

	def _throttle(self) -> None:
		"""Block until at least _min_interval has passed since the last request.

		Called before each HTTP call. Thread-safe: the lock serializes the
		bookkeeping so the floor interval holds across concurrent workers.
		"""
		if self._min_interval <= 0:
			return
		import time

		with self._rate_lock:
			now = time.monotonic()
			wait = self._last_request_at + self._min_interval - now
			if wait > 0:
				time.sleep(wait)
				now = time.monotonic()
			self._last_request_at = now

	def _get_client(self):
		"""Lazy-init the OpenAI client (avoids import error if openai not installed)."""
		if self._client is None:
			try:
				from openai import OpenAI
			except ImportError as e:
				raise RuntimeError("openai package not installed; run: pip install openai") from e
			self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
		return self._client

	def reconcile(self, evidence: dict[str, Any]) -> ReconciledMeta | None:
		"""Call the LLM with retry on empty/transient failures.

		GLM-5.x is a reasoning model: it produces a `reasoning_content` (chain
		of thought) BEFORE the final answer. The reasoning can consume many
		tokens, so we set a generous max_tokens (8000) and read both fields.
		Retries on empty/transient failures (rate limit, overload).

		Request rate is throttled to _min_interval seconds between calls (default
		2.0s = ~30 RPM) so that running many worker threads does not flood Z.AI
		and trip its dynamic RPM limit (429 code 1302). The throttle fires before
		each HTTP call; response parsing and retry backoff happen outside it.
		"""
		import time

		max_retries = 3
		prompt = build_user_prompt(evidence)
		last_error = None
		for attempt in range(max_retries):
			# Throttle BEFORE the HTTP call so the RPM floor holds across worker
			# threads. _throttle() blocks (under a lock) until enough time has
			# passed since the previous request, then stamps the timestamp.
			# Retries also pass through here, so a flapping endpoint can't exceed
			# the rate even while backing off.
			self._throttle()
			try:
				client = self._get_client()
				resp = client.chat.completions.create(
					model=self.model,
					messages=[
						{"role": "system", "content": SYSTEM_PROMPT},
						{"role": "user", "content": prompt},
					],
					# GLM-5.x reasoning models ignore response_format and may
					# emit markdown fences; we tolerate both in _parse_llm_json.
					temperature=0.1,
					max_tokens=8000,
				)
			except Exception as e:  # noqa: BLE001
				last_error = str(e)
				# Transient 429s are already retried by the openai client's
				# internal backoff; if we still see one, sleep a bit before the
				# next attempt so the rate window can recover.
				time.sleep(1.0 * (attempt + 1))
				continue

			# --- response processing ---
			choice = resp.choices[0]
			content = choice.message.content or ""
			# Reasoning models sometimes stash the answer only in content
			# (after the thinking). If content is empty but finish_reason
			# is "length", we ran out of tokens during reasoning — retry
			# won't help, but we record it for debugging.
			if not content.strip():
				finish = choice.finish_reason
				reasoning = getattr(choice.message, "reasoning_content", None) or ""
				if finish == "length":
					log.warning(
						"Z.AI ran out of tokens during reasoning (model=%s, max=8000). "
						"Consider a non-reasoning model.",
						self.model,
					)
					last_error = "length (reasoning too long)"
					# Don't retry — same prompt will hit the same limit.
					break
				log.debug("Z.AI returned empty content (attempt %d/%d)", attempt + 1, max_retries)
				last_error = "empty response"
				time.sleep(1.0 * (attempt + 1))
				continue
			result = _parse_llm_json(content)
			if result is not None:
				# Attach the reasoning chain to the result for transparency.
				reasoning = getattr(choice.message, "reasoning_content", None)
				if reasoning and not result.reasoning:
					result.reasoning = reasoning[-300:]  # last 300 chars
				return result
			last_error = "json parse failed"
			if attempt < max_retries - 1:
				time.sleep(0.5)
				continue
		if last_error:
			log.warning("Z.AI reconcile gave up after %d attempts: %s", max_retries, last_error)
		return None


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
		)
	if os.environ.get("BMF_LLM_MOCK"):
		return MockProvider()
	return None


def _parse_llm_json(content: str) -> ReconciledMeta | None:
	"""Parse the LLM's JSON response into a ReconciledMeta."""
	# Strip markdown fences if present (```json ... ```)
	content = content.strip()
	if content.startswith("```"):
		lines = content.splitlines()
		# Remove first line (```json) and last line (```)
		lines = [ln for ln in lines if not ln.strip().startswith("```")]
		content = "\n".join(lines)
	try:
		data = json.loads(content)
	except json.JSONDecodeError as e:
		log.warning("LLM returned invalid JSON: %s; content: %s", e, content[:200])
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
