"""Tests for the LLM rate limiter in ZaiProvider.

The rate limiter enforces a minimum interval between LLM HTTP calls, so that a
high --workers count (cheap I/O) doesn't flood Z.AI and trip its dynamic RPM
limit (429 'Rate limit reached for requests', code 1302). The floor interval
(default 2.0s = ~30 RPM) holds across worker threads regardless of API speed.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from book_meta_fix.llm import ZaiProvider


def _make_provider_responding(min_interval=None):
	"""Build a ZaiProvider whose reconcile() returns immediately with a canned
	response. Returns (provider, list_of_call_timestamps)."""
	p = ZaiProvider("k", min_interval=min_interval) if min_interval is not None else ZaiProvider("k")
	timestamps: list[float] = []
	lock = threading.Lock()

	def fake_create(**kwargs):
		with lock:
			timestamps.append(time.monotonic())
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
	return p, timestamps


class TestRateLimiter:
	def test_default_min_interval_is_2s(self):
		p = ZaiProvider("k")
		assert p._min_interval == 2.0

	def test_custom_min_interval(self):
		p = ZaiProvider("k", min_interval=0.5)
		assert p._min_interval == 0.5

	def test_disabled_when_zero(self):
		p = ZaiProvider("k", min_interval=0.0)
		assert p._min_interval == 0.0

	def test_negative_intervals_floored_to_zero(self):
		p = ZaiProvider("k", min_interval=-1.0)
		assert p._min_interval == 0.0

	def test_serial_calls_respect_min_interval(self):
		"""Three sequential calls must be spaced >= min_interval apart."""
		p, ts = _make_provider_responding(min_interval=0.2)
		for _ in range(3):
			p.reconcile({"current": {"title": "x"}})
		# Gaps between consecutive calls must each be >= 0.2s.
		assert len(ts) == 3
		gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
		for g in gaps:
			assert g >= 0.2 - 0.05, f"gap {g:.3f} below interval (allowing jitter)"

	def test_concurrent_threads_respect_min_interval(self):
		"""With min_interval=0.2 and 5 threads firing simultaneously, all 5
		call timestamps must be spaced >= 0.2s apart (serialized by the lock)."""
		p, ts = _make_provider_responding(min_interval=0.2)
		threads = [threading.Thread(target=p.reconcile, args=({"current": {"title": "x"}},)) for _ in range(5)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		assert len(ts) == 5
		gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
		for g in gaps:
			assert g >= 0.2 - 0.05, f"concurrent gap {g:.3f} below interval"

	def test_disabled_limiter_does_not_block(self):
		"""min_interval=0 must let calls fire back-to-back with no throttle."""
		p, ts = _make_provider_responding(min_interval=0.0)
		t0 = time.monotonic()
		for _ in range(5):
			p.reconcile({"current": {"title": "x"}})
		elapsed = time.monotonic() - t0
		assert len(ts) == 5
		# 5 calls with no throttle should complete well under a second.
		assert elapsed < 0.5, f"throttle fired despite min_interval=0 ({elapsed:.3f}s)"

	def test_throttle_fires_before_each_retry(self):
		"""A failing call that retries must also pass through the throttle on
		each retry — the rate floor holds even during backoff."""
		from unittest.mock import MagicMock as _MM

		p = ZaiProvider("k", min_interval=0.1)
		ts: list[float] = []
		lock = threading.Lock()
		attempts = [0]

		def fake_create(**kwargs):
			with lock:
				ts.append(time.monotonic())
				attempts[0] += 1
			if attempts[0] < 3:
				raise RuntimeError("transient")
			msg = _MM()
			msg.content = '{"title":"x","authors":[],"confidence":"low"}'
			msg.reasoning_content = None
			choice = _MM()
			choice.message = msg
			choice.finish_reason = "stop"
			resp = _MM()
			resp.choices = [choice]
			return resp

		client = _MM()
		client.chat.completions.create.side_effect = fake_create
		p._client = client

		result = p.reconcile({"current": {"title": "x"}})
		assert result is not None  # succeeded on the 3rd attempt
		assert len(ts) == 3
		gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
		for g in gaps:
			assert g >= 0.1 - 0.05, f"retry gap {g:.3f} below interval"
