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

	def test_default_burst_is_one(self):
		"""Default burst=1 = pure even drip (no bunching). A burst >1 is what
		trips Z.AI's dynamic RPM limit, so the default must stay 1 unless the
		user opts in."""
		assert ZaiProvider("k")._bucket.capacity == 1.0
		assert LeakyBucket(interval=2.0).capacity == 1.0


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

	def test_default_no_initial_burst(self):
		"""With the DEFAULT settings (burst=1), concurrent workers do NOT bunch
		into the first second — the gap between the first two call starts is
		>= interval. This is the 'no bunching' guarantee the default gives."""
		p = ZaiProvider("k", min_interval=0.2)  # burst defaults to 1
		ts: list[float] = []
		lock = threading.Lock()

		def fake_create(**kwargs):
			with lock:
				ts.append(time.monotonic())
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
		threads = [threading.Thread(target=p.reconcile, args=({"current": {"title": "x"}},)) for _ in range(3)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		assert len(ts) == 3
		# The first two starts are spaced >= interval apart (no bunching).
		assert ts[1] - ts[0] >= 0.2 - 0.05, f"first two calls bunched (gap {ts[1] - ts[0]:.3f}s)"

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


class TestGlobalCooldown:
	"""Global 429 circuit-breaker.

	Z.AI's free tier cascade-throttles every model when one gets a 429, so
	per-worker throttling alone cannot stop the storm. When ANY call observes a
	429, a shared cooldown deadline is set that _all_ threads wait on. These
	tests cover escalation, the Retry-After override, the cap, the success
	reset, the wait, and the _call integration.
	"""

	def test_on_rate_limited_escalates(self):
		p = ZaiProvider("k", min_interval=0.0, rate_limit_base=5.0, rate_limit_max=60.0)
		assert p._on_rate_limited(None) == 5.0
		assert p._on_rate_limited(None) == 10.0
		assert p._on_rate_limited(None) == 20.0
		assert p._consecutive_429 == 3

	def test_on_rate_limited_caps_at_max(self):
		p = ZaiProvider("k", min_interval=0.0, rate_limit_base=5.0, rate_limit_max=12.0)
		assert p._on_rate_limited(None) == 5.0
		assert p._on_rate_limited(None) == 10.0
		# Would escalate to 20, but capped at rate_limit_max.
		assert p._on_rate_limited(None) == 12.0

	def test_on_rate_limited_honours_retry_after_when_longer(self):
		p = ZaiProvider("k", min_interval=0.0, rate_limit_base=5.0, rate_limit_max=60.0)
		# First 429: escalated base is 5; server Retry-After of 8 wins.
		assert p._on_rate_limited(8.0) == 8.0
		# Second 429: escalated base is 10; server Retry-After of 7 loses.
		assert p._on_rate_limited(7.0) == 10.0

	def test_on_success_resets_counter(self):
		p = ZaiProvider("k", min_interval=0.0, rate_limit_base=5.0, rate_limit_max=60.0)
		p._on_rate_limited(None)  # 5
		p._on_rate_limited(None)  # 10
		p._on_success()
		assert p._consecutive_429 == 0
		# Next 429 starts over from the base.
		assert p._on_rate_limited(None) == 5.0

	def test_wait_cooldown_blocks_until_deadline(self):
		p = ZaiProvider("k", min_interval=0.0, rate_limit_base=0.05, rate_limit_max=1.0)
		assert p._on_rate_limited(None) == 0.05
		t0 = time.monotonic()
		p._wait_cooldown()
		elapsed = time.monotonic() - t0
		assert elapsed >= 0.05 - 0.02, f"wait_cooldown returned too early ({elapsed:.3f}s)"

	def test_wait_cooldown_noop_when_idle(self):
		p = ZaiProvider("k", min_interval=0.0)
		t0 = time.monotonic()
		p._wait_cooldown()
		assert time.monotonic() - t0 < 0.05, "wait_cooldown blocked despite no active cooldown"

	def test_is_rate_limit_detects_real_429_strings(self):
		# The exact Z.AI error string observed in the run log.
		e = RuntimeError("Error code: 429 - {'error': {'code': '1302', 'message': 'Rate limit reached for requests'}}")
		assert ZaiProvider._is_rate_limit(e)
		assert ZaiProvider._is_rate_limit(RuntimeError("Rate limit reached for requests"))

	def test_is_rate_limit_false_for_other_errors(self):
		assert not ZaiProvider._is_rate_limit(RuntimeError("connection reset by peer"))
		assert not ZaiProvider._is_rate_limit(ValueError("bad value"))

	def test_extract_retry_after_seconds(self):
		class _Resp:
			headers = {"Retry-After": "12"}
		class _Exc(Exception):
			response = _Resp()
		assert ZaiProvider._extract_retry_after(_Exc()) == 12.0

	def test_extract_retry_after_milliseconds(self):
		class _Resp:
			headers = {"retry-after-ms": "2500"}
		class _Exc(Exception):
			response = _Resp()
		assert ZaiProvider._extract_retry_after(_Exc()) == 2.5

	def test_extract_retry_after_none_when_absent(self):
		class _Exc(Exception):
			pass
		assert ZaiProvider._extract_retry_after(_Exc()) is None

	def test_call_sets_cooldown_then_recovers(self):
		"""A mocked 429 makes _call set a global cooldown and retry; on the
		successful retry _on_success resets the counter."""
		p = ZaiProvider("k", min_interval=0.0, burst=10.0, rate_limit_base=0.01, rate_limit_max=0.05)
		attempts = {"n": 0}

		def fake_create(**kwargs):
			attempts["n"] += 1
			if attempts["n"] < 2:
				raise RuntimeError("Error code: 429 - Rate limit reached for requests")
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
		# reconcile() returns the ReconciledMeta (not a tuple — that's reconcile_loop).
		result = p.reconcile({"current": {"title": "x"}})
		assert result is not None  # recovered on retry
		assert attempts["n"] == 2
		# The successful retry reset the escalation counter.
		assert p._consecutive_429 == 0

	def test_call_cooldown_pauses_a_second_thread(self):
		"""When one thread trips a 429 cooldown, a concurrent _call on another
		thread waits for it before firing its request."""
		p = ZaiProvider("k", min_interval=0.0, burst=10.0, rate_limit_base=0.15, rate_limit_max=1.0)
		fired: list[float] = []
		lock = threading.Lock()
		state = {"first_done": False}

		def fake_create(**kwargs):
			with lock:
				fired.append(time.monotonic())
				if not state["first_done"]:
					state["first_done"] = True
					raise RuntimeError("Error code: 429 - Rate limit reached for requests")
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
		# Two threads: the first to fire 429s (setting a ~0.15s cooldown); the
		# second must wait for that cooldown before its own request lands.
		threads = [threading.Thread(target=p.reconcile, args=({"current": {"title": "x"}},)) for _ in range(2)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
		# Three firings: thread A (429), thread A retry (ok), thread B (ok).
		assert len(fired) == 3
		# The cooldown (0.15s) separates the 429 from the next firing.
		gap = fired[1] - fired[0]
		assert gap >= 0.15 - 0.05, f"cooldown not honoured (gap {gap:.3f}s)"
