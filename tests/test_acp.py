"""Tests for the Antigravity ACP provider (acp.py).

The transport is exercised against a REAL subprocess (tests/fixtures/
acp_mock_agent.py speaks actual ACP v1 over stdio — JSON-RPC 2.0, one
message per line), because the interesting failures live in the pipe
plumbing: newline framing, interleaved agent→client requests, process
death. No network anywhere. The provider-level tests monkeypatch the
_send_prompt seam instead, so the loop logic is tested without spawning.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from book_meta_fix import acp as acp_module
from book_meta_fix.acp import AcpAgentConnection, AcpAgentError, AntigravityAcpProvider, match_model_option
from book_meta_fix.extractors import ExtractedMeta
from book_meta_fix.llm import ReconciledMeta, get_provider

FIXTURE = Path(__file__).parent / "fixtures" / "acp_mock_agent.py"


def agent_command(mode: str, **extra_env: str) -> list[str]:
	return [sys.executable, str(FIXTURE)]


def make_conn(mode: str, *, timeout: float = 30.0, cancel_grace: float = 5.0, **extra_env: str) -> AcpAgentConnection:
	return AcpAgentConnection(
		agent_command(mode),
		cwd=Path(__file__).parent,
		prompt_timeout=timeout,
		cancel_grace=cancel_grace,
		agent_env={"MOCK_ACP_MODE": mode},
	)


def make_provider(mode: str = "basic", *, model: str | None = None, **kw) -> AntigravityAcpProvider:
	"""A provider against the mock agent with a small in-flight cap so the
	pool logic runs, but fast timeouts for the failure tests."""
	return AntigravityAcpProvider(
		agent_command(mode),
		cwd=Path(__file__).parent,
		model=model,
		max_inflight=kw.pop("max_inflight", 2),
		prompt_timeout=kw.pop("prompt_timeout", 30.0),
		agent_env={"MOCK_ACP_MODE": mode},
		**kw,
	)


class TestAcpConnection:
	def test_prompt_roundtrip_concatenates_chunks(self):
		"""A full turn: initialize → session/new → prompt; the two streamed
		agent_message_chunk updates must arrive concatenated, in order, and
		the PROBE marker proves the (long, JSON-laden) prompt text made it
		through the pipe intact."""
		conn = make_conn("basic")
		try:
			conn.start()
			sid = conn.new_session()
			assert sid == "sess-mock-1"
			text = conn.prompt("Preamble.\nPROBE:Zlatý-kek\nReturn the JSON.")
			# The agent may JSON-escape non-ASCII on the wire — parse, don't
			# substring-match, the reply.
			data = json.loads(text)
			assert data["title"] == "Zlatý-kek"
			assert data["confidence"] == "high"
		finally:
			conn.close()

	def test_auth_required_then_authenticate(self):
		"""The -32000 auth_required error on session/new triggers the
		protocol-driven authenticate flow, after which sessions work."""
		conn = make_conn("auth")
		try:
			conn.start()
			assert conn.new_session() == "sess-mock-1"
		finally:
			conn.close()

	def test_terminal_only_auth_is_a_clear_error(self):
		"""Only terminal-type login methods advertised → a headless client
		cannot authenticate; the error must say so (guidance to log in
		interactively) instead of hanging or retrying."""
		conn = make_conn("auth_terminal")
		try:
			conn.start()
			with pytest.raises(AcpAgentError, match="terminal-type"):
				conn.new_session()
		finally:
			conn.close()

	def test_permission_request_is_cancelled_and_turn_survives(self):
		"""Agents that ask to run tools get the cancelled outcome; the prompt
		turn itself still completes and the outcome reaches the answer text."""
		conn = make_conn("permission")
		try:
			conn.start()
			conn.new_session()
			text = conn.prompt("anything")
			assert "outcome:cancelled" in text
		finally:
			conn.close()

	def test_fs_request_is_declined(self):
		"""fs/terminal capabilities are not advertised, so a capability
		request gets a JSON-RPC method-not-found error (the mock echoes the
		code back in its answer)."""
		conn = make_conn("fs")
		try:
			conn.start()
			conn.new_session()
			text = conn.prompt("anything")
			assert "fs_error:-32601" in text
		finally:
			conn.close()

	def test_hung_prompt_times_out(self):
		"""A prompt past the timeout sends session/cancel, waits the grace
		window for the mandated final answer, then errors — and close()
		reaps the process (no leak)."""
		conn = make_conn("slow", timeout=0.5, cancel_grace=0.5)
		try:
			conn.start()
			conn.new_session()
			with pytest.raises(AcpAgentError, match="timed out"):
				conn.prompt("anything")
		finally:
			conn.close()

	def test_unsupported_protocol_version_refused(self):
		"""An agent answering protocolVersion 2 speaks a wire format this
		client has not migrated to — refused at initialize, not mis-parsed."""
		conn = make_conn("version2")
		try:
			with pytest.raises(AcpAgentError, match="protocolVersion"):
				conn.start()
		finally:
			conn.close()

	def test_agent_crash_after_init_raises(self):
		"""A process that dies between books (crash mode exits after
		initialize) surfaces as AcpAgentError on the next use, not a hang."""
		conn = make_conn("crash")
		try:
			conn.start()
			with pytest.raises(AcpAgentError):
				conn.new_session()
		finally:
			conn.close()


class TestMatchModelOption:
	"""The family matcher: users (and bmf's defaults) say "gemini-flash",
	agents offer "gemini-3-flash" — and the generation number shifts."""

	OPTS = [
		{"value": "gemini-3-pro", "name": "Gemini 3 Pro"},
		{"value": "gemini-3-flash", "name": "Gemini 3 Flash"},
		{"value": "claude-opus-4", "name": "Claude Opus 4"},
	]

	def test_exact_value(self):
		assert match_model_option("gemini-3-pro", self.OPTS) == "gemini-3-pro"

	def test_exact_name_case_insensitive(self):
		assert match_model_option("Gemini 3 Flash", self.OPTS) == "gemini-3-flash"

	def test_family_match_skips_generation_digits(self):
		assert match_model_option("gemini-flash", self.OPTS) == "gemini-3-flash"
		assert match_model_option("gemini-pro", self.OPTS) == "gemini-3-pro"

	def test_family_prefers_fewest_extra_tokens(self):
		opts = [{"value": "gemini-3-flash"}, {"value": "gemini-3-flash-preview"}]
		assert match_model_option("gemini-flash", opts) == "gemini-3-flash"

	def test_no_match_returns_none(self):
		assert match_model_option("gpt-4", self.OPTS) is None
		assert match_model_option("", self.OPTS) is None

	# The option list the REAL agent served (agy_acp_server 1.1.1, measured
	# 2026-09-07) — locks in how bmf's defaults resolve against it.
	REAL_OPTS = [
		{"value": v, "name": n}
		for v, n in [
			("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
			("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
			("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
			("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)"),
			("gemini-3.7-flash-medium", "Gemini 3.7 Flash (Medium)"),
			("gemini-3.7-flash-low", "Gemini 3.7 Flash (Low)"),
			("gemini-3.6-flash-high", "Gemini 3.6 Flash (High)"),
			("gemini-3.6-flash-medium", "Gemini 3.6 Flash (Medium)"),
			("gemini-3.6-flash-low", "Gemini 3.6 Flash (Low)"),
			("gemini-pro-agent", "Gemini 3.1 Pro (High)"),
			("gemini-3.1-pro-low", "Gemini 3.1 Pro (Low)"),
		]
	]

	def test_bmf_defaults_resolve_on_the_real_agent_list(self):
		# Fast tier default: the newest flash at LOW effort.
		assert match_model_option("gemini-flash-low", self.REAL_OPTS) == "gemini-3.8-flash-low"
		# Quality stage default: the Pro model (its "High" agent variant —
		# fewer extra tokens than gemini-3.1-pro-low).
		assert match_model_option("gemini-pro", self.REAL_OPTS) == "gemini-pro-agent"
		# A bare family name still resolves (to the first listed flash).
		assert match_model_option("gemini-flash", self.REAL_OPTS) == "gemini-3.8-flash-high"


class TestAntigravityAcpProvider:
	def test_reconcile_parses_json_reply(self):
		p = make_provider()
		p._send_prompt = lambda text: '{"title": "Rok 1984", "authors": ["George Orwell"], "confidence": "high"}'
		r = p.reconcile({"current": {}})
		assert r is not None
		assert r.title == "Rok 1984"
		assert r.authors == ["George Orwell"]

	def test_system_prompt_is_in_the_message(self):
		"""ACP has no system role — the metadata-repair contract must travel
		inside the user message (checked via the _send_prompt seam)."""
		seen: list[str] = []

		def fake(text: str) -> str:
			seen.append(text)
			return '{"title": "x", "authors": [], "confidence": "low"}'

		p = make_provider()
		p._send_prompt = fake
		p.reconcile({"current": {"title": "y"}})
		assert seen and "metadata repair assistant" in seen[0] and "PROBE" not in seen[0]

	def test_loop_fast_tier_passes_first_try(self):
		p = make_provider()
		p._send_prompt = lambda text: '{"title": "T", "authors": ["A"], "confidence": "high"}'
		result, src = p.reconcile_loop({"current": {}})
		assert src == "llm:flash"
		assert result.title == "T"

	def test_loop_falls_back_to_zai_provider(self):
		"""Fast tier unusable → the injected ZaiProvider runs its own loop
		(max_flash=1: one flash try, then the paid model) and its source
		label travels through unchanged."""
		calls: list[dict] = []

		class StubZai:
			fallback_model = "glm-5.3"

			def reconcile_loop(self, evidence, extracted=None, *, max_flash=2, verifier=None):
				calls.append({"max_flash": max_flash})
				return ReconciledMeta(title="From Z.AI", authors=["A"], confidence="high"), "llm:high"

		def boom(text: str) -> str:
			raise AcpAgentError("agent died")

		p = make_provider()
		p._send_prompt = boom
		p._zai_fallback = StubZai()
		result, src = p.reconcile_loop({"current": {}})
		assert calls == [{"max_flash": 1}]
		assert src == "llm:high"
		assert result.title == "From Z.AI"

	def test_loop_falls_back_to_acp_fallback(self):
		"""Fast tier dies → the SECOND ACP pool (the agy quality stage,
		gemini-pro) answers — labelled llm:high, not llm:flash (it is the
		quality model, not the quick one)."""
		fb = make_provider()
		fb._send_prompt = lambda text: '{"title": "Pro answer", "authors": ["A"], "confidence": "high"}'

		def boom(text: str) -> str:
			raise AcpAgentError("agent died")

		p = make_provider()
		p._send_prompt = boom
		p._acp_fallback = fb
		result, src = p.reconcile_loop({"current": {}})
		assert src == "llm:high"
		assert result.title == "Pro answer"

	def test_acp_fallback_verify_fail_returns_low(self):
		"""The gemini-pro answer that fails verify still reaches the human —
		low confidence, source llm:low."""
		fb = make_provider()
		fb._send_prompt = lambda text: '{"title": "Pro answer", "authors": ["A"], "confidence": "high"}'

		def boom(text: str) -> str:
			raise AcpAgentError("agent died")

		p = make_provider()
		p._send_prompt = boom
		p._acp_fallback = fb
		ext = ExtractedMeta(first_page_text="Neznámý JINÁ KNIHA")
		result, src = p.reconcile_loop({"current": {}}, ext)
		assert src == "llm:low"
		assert result.title == "Pro answer"
		assert result.confidence == "low"

	def test_transport_failures_disable_the_fast_tier(self):
		"""Three consecutive transport failures park the fast tier for the
		rest of the run — every book must not re-pay the spawn+timeout cost
		of a broken login. Later calls short-circuit without prompting."""
		prompt_calls: list[str] = []

		def boom(text: str) -> str:
			prompt_calls.append(text)
			raise AcpAgentError("agent died")

		p = make_provider()
		p._send_prompt = boom
		for _ in range(AntigravityAcpProvider.MAX_TRANSPORT_FAILURES):
			result, error = p._call({"current": {}})
			assert result is None and error
		assert p._disabled
		result, error = p._call({"current": {}})
		assert result is None
		# The fresh-process retry lives INSIDE _send_prompt, which these tests
		# replaced — so one boom call per _call, three calls → three failures.
		assert len(prompt_calls) == AntigravityAcpProvider.MAX_TRANSPORT_FAILURES

	def test_pool_reuses_one_process_across_calls(self):
		"""Two sequential calls check the same connection back in — only one
		agent process is ever spawned (processes are pooled, not per-call)."""
		p = make_provider(max_inflight=2)
		try:
			for _ in range(2):
				r = p.reconcile({"current": {}})
				assert r is not None
			assert p._created == 1
		finally:
			p.close()

	def test_model_selection_reaches_the_agent(self):
		"""The configured model is matched against the agent's session config
		options and set via session/set_config_option; the mock echoes it as
		the title."""
		p = make_provider("model", model="gemini-3-flash")
		try:
			r = p.reconcile({"current": {}})
			assert r.title == "gemini-3-flash"
		finally:
			p.close()

	def test_family_model_match_reaches_the_agent(self):
		"""The DEFAULT fast-tier model is the family name "gemini-flash" —
		the token matcher must resolve it onto the agent's generation-numbered
		option (gemini-3-flash)."""
		p = make_provider("model", model="gemini-flash")
		try:
			r = p.reconcile({"current": {}})
			assert r.title == "gemini-3-flash"
		finally:
			p.close()

	def test_unknown_model_keeps_agent_default(self):
		"""A model name the agent does not offer must NOT fail the call —
		set_model logs and keeps the agent's default."""
		p = make_provider("model", model="not-offered")
		try:
			r = p.reconcile({"current": {}})
			assert r.title == "default"
		finally:
			p.close()

	def test_close_disables_further_calls(self):
		p = make_provider()
		try:
			r = p.reconcile({"current": {}})
			assert r is not None
		finally:
			p.close()
		result, error = p._call({"current": {}})
		assert result is None and error


class TestGetProviderSelection:
	@pytest.fixture(autouse=True)
	def _clean_env(self, monkeypatch):
		for key in ("ZAI_API_KEY", "BMF_LLM_PROVIDER", "BMF_ANTIGRAVITY_CMD", "BMF_ACP_COMMAND", "BMF_ANTIGRAVITY_MODEL", "BMF_LLM_MOCK", "BMF_ACP_CACHE_DIR"):
			monkeypatch.delenv(key, raising=False)
		# The self-managed-agent seams default to "nothing installed, ensure
		# refuses": selection tests stay offline and deterministic regardless
		# of a real ~/.cache install on the dev machine. Individual tests
		# override these with scripted returns.
		monkeypatch.setattr(acp_module, "installed_acp_release", lambda *a, **k: None)
		monkeypatch.setattr(acp_module, "ensure_acp_agent", lambda *a, **k: None)

	def _cfg(self, **kw):
		from book_meta_fix.config import Config

		cfg = Config.from_env()
		# Config.from_env() honours a real .env (walk-up loader), which BOTH
		# fills cfg.zai_api_key and re-loads it into os.environ — and
		# get_provider falls back to os.environ when cfg has no key. Pop the
		# variable so only cfg decides; tests that want a key pass it as a
		# kwarg (from_env has already copied any setenv'ed key into cfg).
		os.environ.pop("ZAI_API_KEY", None)
		if "zai_api_key" not in kw:
			cfg.zai_api_key = None
		for k, v in kw.items():
			setattr(cfg, k, v)
		return cfg

	def test_glm_fallback_composes_zai_provider(self):
		"""Explicit BMF_ANTIGRAVITY_FALLBACK=glm: the ZaiProvider serves the
		quality stage (its flash+paid loop inside Z.AI's rate machinery)."""
		p = get_provider(self._cfg(zai_api_key="k", llm_provider="antigravity", acp_command=f"{sys.executable} -c pass", acp_fallback_provider="glm"))
		assert isinstance(p, AntigravityAcpProvider)
		assert p.fallback_kind == "glm"
		assert type(p._zai_fallback).__name__ == "ZaiProvider"
		assert p._acp_fallback is None
		assert p.fallback_model == "glm-5.3"

	def test_default_fallback_is_agy_even_with_zai_key(self):
		"""The default quality stage is the SECOND ACP pool (gemini-pro) —
		the whole loop stays on the subscription; a configured Z.AI key is
		not used unless glm is picked explicitly."""
		p = get_provider(self._cfg(zai_api_key="k", llm_provider="antigravity", acp_command=f"{sys.executable} -c pass"))
		assert p.fallback_kind == "agy"
		assert isinstance(p._acp_fallback, AntigravityAcpProvider)
		assert p._zai_fallback is None
		assert p.fallback_model == "gemini-pro"
		assert p.model == "gemini-flash-low"
		# The fallback pool runs the fallback model.
		assert p._acp_fallback.model == "gemini-pro"

	def test_glm_fallback_without_key_has_no_fallback(self):
		p = get_provider(self._cfg(llm_provider="antigravity", acp_command=f"{sys.executable} -c pass", acp_fallback_provider="glm"))
		assert isinstance(p, AntigravityAcpProvider)
		assert p.fallback_kind == ""
		assert p._acp_fallback is None
		assert p._zai_fallback is None

	def test_config_defaults(self):
		"""The requested defaults: quick check = agy gemini flash (low
		effort — the real agent serves flash as -high|medium|low variants
		and the quick tier wants latency), quality stage = agy gemini-pro."""
		from book_meta_fix.config import Config

		cfg = Config()
		assert cfg.acp_model == "gemini-flash-low"
		assert cfg.acp_fallback_model == "gemini-pro"
		assert cfg.acp_fallback_provider == "agy"

	def test_acp_alias_agy(self, monkeypatch):
		p = get_provider(self._cfg(llm_provider="agy", acp_command=f"{sys.executable} -c pass"))
		assert isinstance(p, AntigravityAcpProvider)

	def test_acp_without_zai_key_runs_agent_only(self, monkeypatch):
		p = get_provider(self._cfg(llm_provider="antigravity", acp_command=f"{sys.executable} -c pass"))
		assert isinstance(p, AntigravityAcpProvider)
		assert p._zai_fallback is None
		assert p.fallback_kind == "agy"

	def test_acp_without_command_is_none(self):
		assert get_provider(self._cfg(llm_provider="antigravity", acp_command="")) is None
		assert get_provider(self._cfg(llm_provider="antigravity", acp_command="/nonexistent/agent.par")) is None

	def test_antigravity_uses_the_self_managed_agent(self, monkeypatch, tmp_path):
		"""Explicit agy + empty command: the run installs/updates the agent
		itself (ensure_acp_agent) and launches the RESOLVED argv — including
		the registry's --uid= arg."""
		binary = tmp_path / "agy_acp_server.par"
		ensured: list[dict] = []
		monkeypatch.setattr(
			acp_module,
			"ensure_acp_agent",
			lambda **kw: (ensured.append(kw) or ([str(binary), "--uid="], "9.9.9")),
		)
		p = get_provider(self._cfg(llm_provider="antigravity", acp_command=""))
		assert isinstance(p, AntigravityAcpProvider)
		assert p.command == [str(binary), "--uid="]

	def test_auto_sentinel_opts_into_self_management(self, monkeypatch, tmp_path):
		"""BMF_ANTIGRAVITY_CMD=auto is an explicit opt-in: the agent is
		ensured (downloaded when needed) even in plain auto mode."""
		monkeypatch.setattr(acp_module, "ensure_acp_agent", lambda **kw: ([str(tmp_path / "agy_acp_server.par")], "1.0"))
		p = get_provider(self._cfg(acp_command="auto"))
		assert isinstance(p, AntigravityAcpProvider)

	def test_auto_adopts_and_updates_existing_cache(self, monkeypatch, tmp_path):
		"""Plain auto + empty command + an ALREADY-CACHED agent → the cache is
		adopted (and would be kept current); no opt-in needed because the
		cache itself proves agy usage."""
		calls: list[str] = []
		monkeypatch.setattr(acp_module, "installed_acp_release", lambda *a, **k: {"version": "1.0", "args": ["--uid="], "path": tmp_path / "agy.par"})
		monkeypatch.setattr(acp_module, "ensure_acp_agent", lambda **kw: (calls.append("ensure") or ([str(tmp_path / "agy.par"), "--uid="], "1.0")))
		p = get_provider(self._cfg(acp_command=""))
		assert isinstance(p, AntigravityAcpProvider)
		assert calls == ["ensure"]

	def test_auto_never_downloads_without_optin_or_cache(self, monkeypatch):
		"""A Z.AI-only user (no agy opt-in, no cache) must never trigger the
		~700 MB download: ensure is not even called."""

		def forbidden(**kw):
			raise AssertionError("ensure_acp_agent called without opt-in")

		monkeypatch.setattr(acp_module, "ensure_acp_agent", forbidden)
		p = get_provider(self._cfg(zai_api_key="k", acp_command=""))
		assert type(p).__name__ == "ZaiProvider"

	def test_auto_prefers_acp_fast_tier_when_command_set(self):
		"""Auto + ZAI_API_KEY + configured agent = ACP fast tier; the quality
		stage follows the fallback knob (agy default)."""
		p = get_provider(self._cfg(zai_api_key="k", acp_command=f"{sys.executable} -c pass"))
		assert isinstance(p, AntigravityAcpProvider)
		assert p.fallback_kind == "agy"

	def test_auto_zai_unchanged_without_command(self):
		p = get_provider(self._cfg(zai_api_key="k", acp_command=""))
		assert type(p).__name__ == "ZaiProvider"

	def test_zai_forced_ignores_acp(self):
		p = get_provider(self._cfg(zai_api_key="k", llm_provider="zai", acp_command=f"{sys.executable} -c pass"))
		assert type(p).__name__ == "ZaiProvider"

	def test_off_disables_everything(self):
		assert get_provider(self._cfg(zai_api_key="k", llm_provider="off", acp_command=f"{sys.executable} -c pass")) is None

	def test_mock_forced(self):
		from book_meta_fix.llm import MockProvider

		assert isinstance(get_provider(self._cfg(llm_provider="mock")), MockProvider)

	def test_env_command_canonical_and_alias(self, monkeypatch):
		monkeypatch.setenv("BMF_ANTIGRAVITY_CMD", f"{sys.executable} -c pass")
		assert isinstance(get_provider(self._cfg(llm_provider="acp")), AntigravityAcpProvider)
		monkeypatch.delenv("BMF_ANTIGRAVITY_CMD")
		monkeypatch.setenv("BMF_ACP_COMMAND", f"{sys.executable} -c pass")
		assert isinstance(get_provider(self._cfg(llm_provider="acp")), AntigravityAcpProvider)


class TestEnsureAcpAgent:
	"""The self-managed agent lifecycle (ensure_acp_agent + install_acp_release).

	Every network seam is faked (registry JSON getter, archive getter) and
	the "agent" is a tiny in-memory zip — no real download, ever."""

	@pytest.fixture(autouse=True)
	def _pin_platform(self, monkeypatch):
		monkeypatch.setattr(acp_module, "_registry_platform", lambda: "linux-x86_64")

	def _registry_json(self, version: str) -> dict:
		return {
			"agents": [
				{
					"id": "antigravity-acp",
					"version": version,
					"distribution": {
						"binary": {
							"linux-x86_64": {
								"archive": "https://example.com/dist.zip",
								"cmd": "./agy_acp_server.par",
								"args": ["--uid="],
							}
						}
					},
				}
			]
		}

	@staticmethod
	def _zip_bytes(payload: str) -> bytes:
		import io
		import zipfile

		buf = io.BytesIO()
		with zipfile.ZipFile(buf, "w") as zf:
			zf.writestr("agy_acp_server.par", f"#!/bin/sh\n# {payload}\n")
			zf.writestr("localharness_external", "tool harness bmf never uses")
		return buf.getvalue()

	class _FakeResp:
		def __init__(self, data: bytes):
			self.headers = {"Content-Length": str(len(data))}
			self._data = data

		def iter_content(self, chunk_size: int = 1024):
			for i in range(0, len(self._data), chunk_size):
				yield self._data[i : i + chunk_size]

	def _seed_cache(self, tmp_path, version: str) -> Path:
		"""A pre-installed release older/newer than whatever the registry says."""
		d = tmp_path / "cache"
		d.mkdir(parents=True, exist_ok=True)
		binary = d / "agy_acp_server.par"
		binary.write_text(f"#!/bin/sh\n# installed {version}\n")
		binary.chmod(0o755)
		(d / "version.json").write_text(json.dumps({"version": version, "args": ["--uid="], "path": str(binary)}))
		return d

	def test_cold_install_downloads_and_extracts_only_the_server(self, tmp_path):
		"""Empty cache → the registry release is fetched, the zip streamed,
		ONLY agy_acp_server.par kept (exec bit set), version.json written with
		the registry launch args, argv returned."""
		zip_data = self._zip_bytes("registry 9.9.9")
		downloads: list[str] = []
		progress: list[tuple[int, int]] = []

		def fake_get(url, **kw):
			downloads.append(url)
			return self._FakeResp(zip_data)

		result = acp_module.ensure_acp_agent(
			cache_dir=tmp_path / "cache",
			http_get_json=lambda url: self._registry_json("9.9.9"),
			http_get=fake_get,
			progress_cb=lambda done, total: progress.append((done, total)),
		)
		assert downloads == ["https://example.com/dist.zip"]
		assert result is not None
		argv, version = result
		assert version == "9.9.9"
		assert argv == [str(tmp_path / "cache" / "agy_acp_server.par"), "--uid="]
		binary = tmp_path / "cache" / "agy_acp_server.par"
		assert binary.is_file() and os.access(binary, os.X_OK)
		assert "# registry 9.9.9" in binary.read_text()
		# localharness_external is NOT extracted (bmf denies tools).
		assert not (tmp_path / "cache" / "localharness_external").exists()
		sidecar = json.loads((tmp_path / "cache" / "version.json").read_text())
		assert sidecar["version"] == "9.9.9" and sidecar["args"] == ["--uid="]
		# Progress reported monotonically, with the total.
		assert progress and progress[-1][0] == progress[-1][1] == len(zip_data)

	def test_outdated_cache_is_upgraded_atomically(self, tmp_path):
		"""Installed 1.0.0, registry 2.0.0 → the new binary replaces the old
		one and version.json follows."""
		self._seed_cache(tmp_path, "1.0.0")

		result = acp_module.ensure_acp_agent(
			cache_dir=tmp_path / "cache",
			http_get_json=lambda url: self._registry_json("2.0.0"),
			http_get=lambda url, **kw: self._FakeResp(self._zip_bytes("registry 2.0.0")),
		)
		assert result is not None
		argv, version = result
		assert version == "2.0.0"
		assert "# registry 2.0.0" in Path(argv[0]).read_text()
		assert json.loads((tmp_path / "cache" / "version.json").read_text())["version"] == "2.0.0"

	def test_current_cache_is_not_redownloaded(self, tmp_path):
		"""Installed == registry version → zero archive requests."""
		self._seed_cache(tmp_path, "9.9.9")

		def no_download(url, **kw):
			raise AssertionError("re-downloaded an up-to-date agent")

		result = acp_module.ensure_acp_agent(
			cache_dir=tmp_path / "cache",
			http_get_json=lambda url: self._registry_json("9.9.9"),
			http_get=no_download,
		)
		assert result is not None and result[1] == "9.9.9"

	def test_offline_keeps_the_installed_agent(self, tmp_path):
		"""Registry unreachable → the installed agent is served as-is (a run
		must not lose its LLM tier to a DNS blip)."""

		def offline(url):
			raise OSError("no network")

		self._seed_cache(tmp_path, "1.0.0")
		result = acp_module.ensure_acp_agent(cache_dir=tmp_path / "cache", http_get_json=offline)
		assert result is not None and result[1] == "1.0.0"

	def test_offline_and_cold_returns_none(self, tmp_path):
		def offline(url):
			raise OSError("no network")

		assert acp_module.ensure_acp_agent(cache_dir=tmp_path / "cache", http_get_json=offline) is None

	def test_failed_upgrade_keeps_previous_version(self, tmp_path):
		"""A broken download must not destroy the working install (atomic
		swap) — the previous version keeps serving."""
		self._seed_cache(tmp_path, "1.0.0")

		def broken_zip(url, **kw):
			return self._FakeResp(b"this is not a zip")

		result = acp_module.ensure_acp_agent(
			cache_dir=tmp_path / "cache",
			http_get_json=lambda url: self._registry_json("2.0.0"),
			http_get=broken_zip,
		)
		assert result is not None and result[1] == "1.0.0"
		assert "# installed 1.0.0" in Path(result[0][0]).read_text()
