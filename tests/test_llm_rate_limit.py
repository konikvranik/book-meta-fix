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


def _ok_response():
	"""A mocked chat completion returning a minimal parseable ReconciledMeta."""
	msg = MagicMock()
	msg.content = '{"title":"x","authors":[],"confidence":"low"}'
	msg.reasoning_content = None
	choice = MagicMock()
	choice.message = msg
	choice.finish_reason = "stop"
	resp = MagicMock()
	resp.choices = [choice]
	return resp


def _err(code: str, message: str) -> RuntimeError:
	"""A fake exception shaped like the observed Z.AI 429 payload."""
	return RuntimeError(f"Error code: 429 - {{'error': {{'code': '{code}', 'message': '{message}'}}}}")


class Test429SubCodes:
	"""HTTP 429 carries three Z.AI sub-codes with OPPOSITE meanings; _call
	must dispatch on the code, not on the status alone.

	1302 = our RPM rate tripped → escalating global fleet cooldown (above).
	1305 = SERVER overloaded → short interval-spaced retries, NO cooldown.
	1308 = usage quota exhausted → model disabled for the rest of the run.
	"""

	def test_server_error_from_exc_body(self):
		"""The openai SDK exposes the parsed payload as exc.body."""
		exc = RuntimeError("Error code: 429")
		exc.body = {"error": {"code": "1305", "message": "The service may be temporarily overloaded"}}
		assert ZaiProvider._server_error(exc) == ("1305", "The service may be temporarily overloaded")

	def test_server_error_from_string(self):
		"""Plain exceptions fall back to a regex over the observed text form."""
		exc = _err("1302", "Rate limit reached for requests")
		assert ZaiProvider._server_error(exc) == ("1302", "Rate limit reached for requests")

	def test_server_error_absent(self):
		assert ZaiProvider._server_error(RuntimeError("connection reset")) == (None, "")

	def test_overloaded_retries_without_global_cooldown(self):
		"""1305 must recover via short retries while leaving the global
		cooldown disarmed — the pre-fix code parked the whole fleet for it."""
		p = ZaiProvider("k", min_interval=0.0, rate_limit_base=5.0, rate_limit_max=60.0)
		attempts = {"n": 0}

		def fake_create(**kwargs):
			attempts["n"] += 1
			if attempts["n"] < 3:
				raise _err("1305", "The service may be temporarily overloaded, please try again later")
			return _ok_response()

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client
		result = p.reconcile({"current": {"title": "x"}})
		assert result is not None
		assert attempts["n"] == 3
		assert p._consecutive_429 == 0
		# _cooldown_until stays at its initial 0.0 — no cooldown was ever armed.
		assert p._cooldown_until == 0.0

	def test_overloaded_budget_spent_falls_back_to_paid_model(self):
		"""When flash keeps 1305ing past the retry budget (but below the
		fleet-wide streak threshold), reconcile_loop gives up on flash and
		answers via the fallback model — without ever arming the global
		cooldown. One call spends at most 1 + OVERLOAD_RETRIES hits."""
		p = ZaiProvider("k", min_interval=0.0, rate_limit_base=5.0, rate_limit_max=60.0)
		calls: list[str] = []

		def fake_create(**kwargs):
			calls.append(kwargs["model"])
			if kwargs["model"] == "glm-4.7-flash":
				raise _err("1305", "The service may be temporarily overloaded, please try again later")
			return _ok_response()

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client
		result, src = p.reconcile_loop({"current": {"title": "x"}}, extracted=None)
		assert result is not None
		assert src == "llm:high"
		# 1 initial hit + exactly the (small) retry budget, then the fallback.
		assert calls.count("glm-4.7-flash") == 1 + ZaiProvider.OVERLOAD_RETRIES
		assert calls.count("glm-5.3") == 1
		# Below the streak threshold no pause was armed, and the global
		# cooldown never fired either.
		assert p._cooldown_until == 0.0
		if 1 + ZaiProvider.OVERLOAD_RETRIES < ZaiProvider.OVERLOAD_STREAK:
			assert "glm-4.7-flash" not in p._overload_until

	def test_real_rate_limit_1302_still_arms_cooldown(self):
		"""Regression guard: the genuine RPM limit keeps the fleet cooldown."""
		p = ZaiProvider("k", min_interval=0.0, burst=10.0, rate_limit_base=0.01, rate_limit_max=0.05)
		attempts = {"n": 0}

		def fake_create(**kwargs):
			attempts["n"] += 1
			if attempts["n"] < 2:
				raise _err("1302", "Rate limit reached for requests")
			return _ok_response()

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client
		result = p.reconcile({"current": {"title": "x"}})
		assert result is not None
		# A cooldown was armed at some point (_cooldown_until is absolute
		# monotonic; it only leaves 0.0 via _on_rate_limited).
		assert p._cooldown_until > 0.0
		# ...and the recovery reset the escalation.
		assert p._consecutive_429 == 0

	def test_usage_limit_disables_model_for_the_run(self):
		"""1308 marks the model exhausted; later _calls short-circuit with no
		HTTP hit, but the other model stays usable."""
		p = ZaiProvider("k", min_interval=0.0)
		calls: list[str] = []

		def fake_create(**kwargs):
			calls.append(kwargs["model"])
			if kwargs["model"] == "glm-4.7-flash":
				raise _err("1308", "Usage limit reached for GLM-4.7-Flash")
			return _ok_response()

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client
		result, src = p.reconcile_loop({"current": {"title": "x"}}, extracted=None)
		assert result is not None
		assert src == "llm:high"
		assert "glm-4.7-flash" in p._disabled_models
		assert "glm-5.3" not in p._disabled_models
		# One flash hit recorded the exhaustion; a direct flash _call now
		# short-circuits without touching the API.
		flash_hits = calls.count("glm-4.7-flash")
		res, err = p._call("glm-4.7-flash", {"current": {"title": "x"}})
		assert res is None
		assert "usage limit" in (err or "")
		assert calls.count("glm-4.7-flash") == flash_hits

	def test_insufficient_balance_is_transient_not_run_long(self):
		"""429/1113 'Insufficient balance' arrives with HTTP 429 but is NOT a
		rate limit — and NOT a hard account state either: on the coding
		endpoint it fires intermittently (short metering bursts) even with
		quota left (dashboard showed 73% free while a 10-worker run tripped
		it). So it must retry shortly, and only a persistent streak time-boxes
		the model via the overload pause — never a run-long disable, never the
		global cooldown."""
		p = ZaiProvider("k", min_interval=0.0)
		calls: list[str] = []
		state = {"mode": "blip"}  # blip = 1113 twice then ok; dead = always 1113

		def fake_create(**kwargs):
			calls.append(kwargs["model"])
			if kwargs["model"] == "glm-5.3":
				if state["mode"] == "dead":
					raise _err("1113", "Insufficient balance or no resource package. Please recharge.")
				if calls.count("glm-5.3") <= 2:
					raise _err("1113", "Insufficient balance or no resource package. Please recharge.")
			return _ok_response()

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client
		ev = {"current": {"title": "x"}}
		p._disabled_models["glm-4.7-flash"] = "test"  # force the fallback path
		# A blip: two 1113s then a 200 — recovers within the retry budget,
		# no pause, no run-long disable, no global cooldown.
		result, src = p.reconcile_loop(dict(ev), extracted=None)
		assert result is not None
		assert src == "llm:high"
		assert "glm-5.3" not in p._disabled_models
		assert "glm-5.3" not in p._overload_until
		assert p._cooldown_until == 0.0
		# Persistent 1113s: the fleet-wide streak hits the threshold mid-way
		# through the SECOND fully-failed call and time-boxes the model.
		state["mode"] = "dead"
		r, e = p._call("glm-5.3", dict(ev))
		assert r is None
		assert "balance" in (e or "")
		assert "glm-5.3" not in p._overload_until  # streak 3 < threshold yet
		r, e = p._call("glm-5.3", dict(ev))
		assert r is None
		assert "balance" in (e or "")
		assert time.monotonic() < p._overload_until["glm-5.3"]
		# The 1113 pause is SHORT (burst-scale), not the minutes-long 1305 one.
		assert p._overload_until["glm-5.3"] - time.monotonic() <= ZaiProvider.BALANCE_PAUSE_SEC + 0.5
		assert "glm-5.3" not in p._disabled_models  # pause, not disable
		# While paused the model short-circuits with zero API hits.
		glm_calls = calls.count("glm-5.3")
		r, e = p._call("glm-5.3", dict(ev))
		assert r is None
		assert calls.count("glm-5.3") == glm_calls

	def test_all_models_paused_is_quiet_and_fast(self, caplog):
		"""With every model paused the LLM stage must be a fast no-op that
		stays OFF the info log — the per-book 'falling back' spam buried the
		real state (books flying through with no LLM content at all)."""
		import logging as _logging

		p = ZaiProvider("k", min_interval=0.0)
		now = time.monotonic()
		p._overload_until["glm-4.7-flash"] = now + 60.0
		p._overload_until["glm-5.3"] = now + 60.0
		client = MagicMock()
		client.chat.completions.create.side_effect = lambda **kw: _ok_response()
		p._client = client
		with caplog.at_level(_logging.INFO, logger="book_meta_fix.llm"):
			for _ in range(3):
				r, s = p.reconcile_loop({"current": {"title": "x"}}, extracted=None)
				assert r is None
		# No API hits while everything is paused.
		assert client.chat.completions.create.call_count == 0
		# The per-book fallback spam stays off the info log...
		assert "falling back" not in caplog.text
		# ...and the idle notice fires at most once per minute.
		assert caplog.text.count("every model is paused") == 1

	def test_rate_limit_stretches_drip_and_success_shrinks_it(self):
		"""The drip is adaptive: a 1302 widens the interval (we are above the
		account's real ceiling), successes ease it back toward the floor."""
		p = ZaiProvider("k", min_interval=0.05, rate_limit_base=0.01, rate_limit_max=0.05)
		attempts = {"n": 0}

		def fake_create(**kwargs):
			attempts["n"] += 1
			if attempts["n"] < 2:
				raise _err("1302", "Rate limit reached for requests")
			return _ok_response()

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client
		assert p.reconcile({"current": {"title": "x"}}) is not None
		assert p._bucket.interval > p._interval_floor
		stretched = p._bucket.interval
		# Successes shrink it back (slowly, floored).
		for _ in range(30):
			assert p.reconcile({"current": {"title": "x"}}) is not None
		assert p._bucket.interval < stretched
		assert p._bucket.interval >= p._interval_floor
		# Stretching is capped.
		p._bucket.interval = p._interval_floor
		for _ in range(50):
			p._stretch_interval()
		assert p._bucket.interval == p._interval_cap

	def test_success_resets_escalation_even_on_bad_json(self):
		"""Any HTTP 200 (not just a parseable one) resets the escalation — a
		bad-JSON streak must not masquerade as a sustained rate limit."""
		p = ZaiProvider("k", min_interval=0.0)
		p._on_rate_limited(None)
		p._on_rate_limited(None)
		assert p._consecutive_429 == 2

		def fake_create(**kwargs):
			msg = MagicMock()
			msg.content = "not json at all"
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
		result = p.reconcile({"current": {"title": "x"}})
		assert result is None  # never parsed
		assert p._consecutive_429 == 0

	def test_overloaded_pauses_model_after_repeated_full_failures(self):
		"""The streak counter is fleet-wide across calls: FOUR consecutive
		1305s (even split across two calls) arm the pause; the next call
		skips flash entirely and answers via the fallback — no more wasted
		requests on a dead model."""
		p = ZaiProvider("k", min_interval=0.0)
		calls: list[str] = []

		def fake_create(**kwargs):
			calls.append(kwargs["model"])
			if kwargs["model"] == "glm-4.7-flash":
				raise _err("1305", "The service may be temporarily overloaded, please try again later")
			return _ok_response()

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client
		ev = {"current": {"title": "x"}}
		# First loop: flash burns 1 + OVERLOAD_RETRIES hits (streak 3, below
		# the threshold), falls through to the fallback. No pause yet.
		result, _ = p.reconcile_loop(dict(ev), extracted=None)
		assert result is not None
		flash_after_1 = calls.count("glm-4.7-flash")
		assert flash_after_1 == 1 + ZaiProvider.OVERLOAD_RETRIES
		assert "glm-4.7-flash" not in p._overload_until
		# Second loop: the FIRST rejection is streak #4 — the pause arms and
		# the call gives up immediately (single hit).
		result, _ = p.reconcile_loop(dict(ev), extracted=None)
		assert result is not None
		assert calls.count("glm-4.7-flash") == flash_after_1 + 1
		assert time.monotonic() < p._overload_until["glm-4.7-flash"]
		# Third loop: flash is short-circuited with ZERO API hits.
		result, src = p.reconcile_loop(dict(ev), extracted=None)
		assert result is not None
		assert src == "llm:high"
		assert calls.count("glm-4.7-flash") == flash_after_1 + 1
		assert calls.count("glm-5.3") == 3

	def test_model_success_clears_overload_streak(self):
		"""A 200 on the model clears its rejection streak, so an isolated bad
		call does not accumulate into a pause across healthy calls."""
		p = ZaiProvider("k", min_interval=0.0)
		calls: list[str] = []
		state = {"flash_ok": False}

		def fake_create(**kwargs):
			calls.append(kwargs["model"])
			if kwargs["model"] == "glm-4.7-flash" and not state["flash_ok"]:
				raise _err("1305", "The service may be temporarily overloaded, please try again later")
			return _ok_response()

		client = MagicMock()
		client.chat.completions.create.side_effect = fake_create
		p._client = client
		ev = {"current": {"title": "x"}}
		# Fully-failed flash call (streak 3), then a healthy flash call
		# (streak resets to 0)...
		p.reconcile_loop(dict(ev), extracted=None)
		state["flash_ok"] = True
		p.reconcile_loop(dict(ev), extracted=None)
		assert p._fail_streak["glm-4.7-flash"] == 0
		# ...so one more fully-failed call (streak back to 3) must NOT pause.
		state["flash_ok"] = False
		calls.clear()
		p.reconcile_loop(dict(ev), extracted=None)
		assert p._fail_streak["glm-4.7-flash"] == 1 + ZaiProvider.OVERLOAD_RETRIES
		assert "glm-4.7-flash" not in p._overload_until

	def test_overload_pause_expires(self):
		"""An expired pause lets the model back in without an API penalty."""
		p = ZaiProvider("k", min_interval=0.0)
		client = MagicMock()
		client.chat.completions.create.side_effect = lambda **kw: _ok_response()
		p._client = client
		p._overload_until["glm-4.7-flash"] = time.monotonic() - 1.0  # already past
		result = p.reconcile({"current": {"title": "x"}})
		assert result is not None
		assert client.chat.completions.create.call_count == 1

	def test_client_built_without_sdk_retries(self):
		"""The openai client must not retry on its own: SDK retries fire
		outside the leaky bucket (breaking the count-per-time guarantee) and
		silently absorb 429s before _call can classify them."""
		p = ZaiProvider("k")
		client = p._get_client()
		assert client.max_retries == 0
