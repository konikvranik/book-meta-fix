"""Tests for the LLM concurrency semaphore in ZaiProvider.

The semaphore caps how many LLM HTTP calls are in flight at once, so that a
high --workers count (cheap I/O) doesn't flood Z.AI and trigger 429s.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from book_meta_fix.llm import ZaiProvider


class TestLlmSemaphore:
	def test_semaphore_default_cap_is_3(self):
		p = ZaiProvider("k")
		# threading.Semaphore exposes the count via _value (CPython impl detail;
		# fine for a unit test that just asserts the configured cap).
		assert p._llm_semaphore._value == 3

	def test_semaphore_custom_cap(self):
		p = ZaiProvider("k", max_concurrent_llm=2)
		assert p._llm_semaphore._value == 2

	def test_semaphore_floor_at_1(self):
		p = ZaiProvider("k", max_concurrent_llm=0)
		assert p._llm_semaphore._value == 1

	def test_concurrent_calls_capped(self):
		"""With max_concurrent_llm=2 and many threads calling reconcile, at most
		2 should be inside the HTTP call body simultaneously."""
		p = ZaiProvider("k", max_concurrent_llm=2)
		in_flight = 0
		max_seen = 0
		lock = threading.Lock()

		# Fake the openai client so each call blocks briefly, letting us
		# observe concurrency. We count how many are inside simultaneously.
		def fake_create(**kwargs):
			nonlocal in_flight, max_seen
			with lock:
				in_flight += 1
				max_seen = max(max_seen, in_flight)
			time.sleep(0.05)  # hold the slot briefly
			with lock:
				in_flight -= 1
			# Return a minimal valid-ish response object.
			msg = MagicMock()
			msg.content = '{"title":"x","authors":[],"confidence":"low"}'
			msg.reasoning_content = None
			choice = MagicMock()
			choice.message = msg
			choice.finish_reason = "stop"
			resp = MagicMock()
			resp.choices = [choice]
			return resp

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client

		# Launch 8 threads concurrently — cap is 2.
		threads = [threading.Thread(target=p.reconcile, args=({"current": {"title": "x"}},)) for _ in range(8)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()

		assert max_seen <= 2, f"concurrency exceeded cap: max_seen={max_seen}"

	def test_semaphore_released_on_exception(self):
		"""If the HTTP call raises, the semaphore must still be released so it
		doesn't deadlock subsequent calls."""
		p = ZaiProvider("k", max_concurrent_llm=1)

		call_count = [0]

		def fake_create(**kwargs):
			call_count[0] += 1
			raise RuntimeError("boom")

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client

		# First call: raises, must release the single permit.
		result = p.reconcile({"current": {"title": "x"}})
		assert result is None  # reconcile returns None on exhausted retries
		# Semaphore must be fully available again (cap was 1).
		assert p._llm_semaphore._value == 1, "semaphore leaked after exception"
