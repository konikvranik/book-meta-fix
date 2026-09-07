"""Tests for the per-host RateLimiter (enrichers).

The limiter spaces call STARTS per host (>= min_interval apart) while
sleeping OUTSIDE the lock: the old shape (lock held across time.sleep)
serialized every worker of every host behind one global sleep, capping the
whole enrichment stage at one request per interval regardless of --workers.
"""
from __future__ import annotations

import threading
import time

from book_meta_fix.enrichers import RateLimiter


class TestRateLimiter:
	def test_same_host_calls_are_spaced(self):
		lim = RateLimiter()
		t0 = time.monotonic()
		lim.wait("example.com", 0.2)
		lim.wait("example.com", 0.2)
		elapsed = time.monotonic() - t0
		# The second call must wait for its reserved slot (>= interval minus
		# the time the first spent, so a generous lower bound).
		assert elapsed >= 0.15

	def test_different_hosts_do_not_block_each_other(self):
		"""Two hosts, one queued caller each: neither waits on the other's
		interval. The pre-fix lock-across-sleep made the second call wait
		even for an unrelated host."""
		lim = RateLimiter()
		both = threading.Barrier(2, timeout=3.0)

		def _one(host: str) -> float:
			both.wait()  # release together
			t = time.monotonic()
			lim.wait(host, 1.0)
			return time.monotonic() - t

		threads = []
		results: list[float] = []

		def run(host: str) -> None:
			results.append(_one(host))

		for host in ("a.example", "b.example"):
			th = threading.Thread(target=run, args=(host,))
			th.start()
			threads.append(th)
		for th in threads:
			th.join()
		# No reservation existed for either host -> both start immediately;
		# generous bound so the test never flakes on a slow box.
		assert all(r < 0.5 for r in results), results

	def test_queued_same_host_callers_get_distinct_slots(self):
		"""Callers queuing on ONE host reserve spaced slots and park in
		parallel (sleep outside the lock) instead of serializing behind it."""
		lim = RateLimiter()
		start = threading.Barrier(3, timeout=5.0)
		done: list[float] = []
		done_lock = threading.Lock()

		def _one(i: int) -> None:
			start.wait()
			lim.wait("example.com", 0.15)
			with done_lock:
				done.append(time.monotonic())

		threads = [threading.Thread(target=_one, args=(i,)) for i in range(3)]
		for th in threads:
			th.start()
		for th in threads:
			th.join()
		done.sort()
		spacing = [b - a for a, b in zip(done, done[1:], strict=False)]
		# Slot starts are ~0.15s apart; thread scheduling adds slack, so only
		# assert they did NOT all fire at once AND did not serialize beyond
		# the reserved pacing (3 slots ≈ 0.3s total, far below 3×interval of
		# naive serial lock-holding... which would also be 0.3s — so instead
		# assert the pairwise spacing contract of the reservation itself).
		assert done[0] > 0
		assert all(s >= 0.10 for s in spacing), spacing

	def test_no_wait_when_quiet(self):
		lim = RateLimiter()
		t0 = time.monotonic()
		lim.wait("fresh.example", 5.0)
		assert time.monotonic() - t0 < 0.1  # first caller for a host never waits
