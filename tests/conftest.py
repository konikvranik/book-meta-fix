"""Shared test fixtures.

Output-language pinning: user-facing strings (review.yaml header, CLI
messages) are localized via book_meta_fix.i18n and would follow the test
machine's locale. Tests assert the English msgids, so pin the language to
English for every test; tests/test_i18n.py manages the catalog explicitly
(its own autouse fixture runs after this one and resets the module state).

Virtual display: the Tk-gated GUI smoke tests (tests/test_gui.py) build
REAL widgets — on a desktop session their windows pop up over whatever the
user is doing. The session fixture below starts a private Xvfb (when
installed) and points DISPLAY at it for the whole run, so the suite never
touches the desktop and Tk tests also run on headless machines instead of
skipping. Set BMF_TEST_REAL_DISPLAY=1 to opt out when debugging a GUI test
visually.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from book_meta_fix import i18n


@pytest.fixture(autouse=True)
def _pin_language_en(monkeypatch):
	monkeypatch.setenv("BMF_LANGUAGE", "en")
	i18n.init_language("en")
	yield


_XVFB_READY_TIMEOUT = 5.0


def _display_in_use(num: int) -> bool:
	# Both the socket and the lock file appear while an X server owns :num.
	return (Path(f"/tmp/.X11-unix/X{num}").exists()
	        or Path(f"/tmp/.X{num}-lock").exists())


def _start_xvfb() -> tuple[subprocess.Popen, str] | None:
	"""Start a private Xvfb and return (process, DISPLAY value), or None.

	Returns None (and leaves DISPLAY alone) when Xvfb is missing, opted out,
	or no free display number came up — the Tk tests then degrade to their
	old behaviour (windows on the desktop, skip when headless).
	"""
	if os.environ.get("BMF_TEST_REAL_DISPLAY"):
		return None
	if not shutil.which("Xvfb"):
		return None
	for num in range(99, 145):
		if _display_in_use(num):
			continue
		display = f":{num}"
		try:
			proc = subprocess.Popen(
				["Xvfb", display, "-screen", "0", "1280x1024x24",
				 "-nolisten", "tcp"],
				stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		except OSError:
			return None
		deadline = time.monotonic() + _XVFB_READY_TIMEOUT
		while time.monotonic() < deadline:
			if proc.poll() is not None:
				break  # died instantly: another server raced us for :num
			if _display_in_use(num):
				return proc, display
			time.sleep(0.05)
		proc.terminate()  # not ready or dead — reap and try the next number
		proc.wait()
	return None


@pytest.fixture(scope="session", autouse=True)
def _virtual_display():
	"""Run the suite on a private Xvfb (see module docstring)."""
	started = _start_xvfb()
	if started is None:
		yield
		return
	proc, display = started
	old_display = os.environ.get("DISPLAY")
	os.environ["DISPLAY"] = display
	try:
		yield
	finally:
		if old_display is None:
			del os.environ["DISPLAY"]
		else:
			os.environ["DISPLAY"] = old_display
		proc.terminate()
		try:
			proc.wait(timeout=5)
		except subprocess.TimeoutExpired:  # pragma: no cover - stuck Xvfb
			proc.kill()
