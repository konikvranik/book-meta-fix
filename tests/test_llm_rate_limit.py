"""Tests for the LeakyBucket rate limiter used by ZaiProvider.

The leaky-bucket smoother enforces a constant aggregate request rate across
worker threads: a short burst up to *capacity* calls is allowed, then calls
drip out at one per *interval* seconds. With the old lock-during-sleep
throttle, N threads hitting the LLM phase at once piled up on the lock and
emitted requests back-to-back once each woke; the bucket smooths that into a
steady drip so Z.AI's dynamic RPM limit (429 code 1302) is not tripped.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from book_meta_fix.llm import LeakyBucket, ZaiProvider


def _make_provider_responding(*, interval=0.2, capacity=1.0):
	"""Build a ZaiProvider whose reconcile() returns immediately with a canned
	response. Returns (provider, list_of_call_timestamps).

	capacity=1 reproduces the pre-bucket "one call per interval" behaviour for
	the serial/concurrent spacing tests; larger capacities allow bursts.
	"""
	p = ZaiProvider("k", min_interval=interval, burst=capacity)
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


class TestBucketDefaults:
	def test_default_interval_is_2s(self):
		p = ZaiProvider("k")
		assert p._bucket.interval == 2.0

	def test_custom_interval(self):
		p = ZaiProvider("k", min_interval=0.5)
		assert p._bucket.interval == 0.5

	def test_disabled_when_zero(self):
		p = ZaiProvider("k", min_interval=0.0)
		assert p._bucket.interval == 0.0

	def test_negative_interval_floored_to_zero(self):
		p = ZaiProvider("k", min_interval=-1.0)
		assert p._bucket.interval == 0.0

	def test_burst_capacity_configurable(self):
		p = ZaiProvider("k", burst=8.0)
		assert p._bucket.capacity == 8.0


class TestSerialCalls:
	def test_serial_calls_respect_interval(self):
		"""With capacity=1, three sequential calls are spaced >= interval apart."""
		p, ts = _make_provider_responding(interval=0.2, capacity=1.0)
		for _ in range(3):
			p.reconcile({"current": {"title": "x"}})
		assert len(ts) == 3
		gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
		for g in gaps:
			assert g >= 0.2 - 0.05, f"gap {g:.3f} below interval (allowing jitter)"

	def test_disabled_limiter_does_not_block(self):
		"""interval=0 must let calls fire back-to-back with no throttle."""
		p, ts = _make_provider_responding(interval=0.0, capacity=1.0)
		t0 = time.monotonic()
		for _ in range(5):
			p.reconcile({"current": {"title": "x"}})
		elapsed = time.monotonic() - t0
		assert len(ts) == 5
		assert elapsed < 0.5, f"throttle fired despite interval=0 ({elapsed:.3f}s)"


class TestConcurrentCalls:
	def test_concurrent_threads_rate_is_bounded(self):
		"""With interval=0.2 and capacity=1, 5 concurrent threads must emit
		calls at >= 0.2s spacing (the bucket serialises them into a drip)."""
		p, ts = _make_provider_responding(interval=0.2, capacity=1.0)
		threads = [threading.Thread(target=p.reconcile, args=({"current": {"title": "x"}},)) for _ in range(5)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		assert len(ts) == 5
		gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
		for g in gaps:
			assert g >= 0.2 - 0.05, f"concurrent gap {g:.3f} below interval"

	def test_burst_capacity_allows_short_burst(self):
		"""With capacity=3, the first 3 concurrent calls fire immediately
		(within the burst) and only the 4th onward waits."""
		bucket = LeakyBucket(capacity=3.0, interval=0.2)
		# Pre-fill the bucket so refill doesn't muddy the burst test.
		start = time.monotonic()
		fire_times: list[float] = []

		def grab():
			bucket.acquire()
			fire_times.append(time.monotonic() - start)

		threads = [threading.Thread(target=grab) for _ in range(5)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		# First 3 should be near-immediate (within burst); 4th and 5th wait.
		assert len(fire_times) == 5
		# First three all fire within a small window (burst).
		assert max(fire_times[:3]) < 0.05, f"first 3 not bursty: {fire_times[:3]}"
		# The 4th waited roughly one interval.
		assert fire_times[3] >= 0.2 - 0.05, f"4th call did not wait: {fire_times[3]}"


class TestRetryRateHolding:
	def test_throttle_fires_before_each_retry(self):
		"""A failing call that retries must also acquire a bucket token on each
		retry — the rate floor holds even during backoff."""
		from unittest.mock import MagicMock as _MM

		p = ZaiProvider("k", min_interval=0.1, burst=1.0)
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
