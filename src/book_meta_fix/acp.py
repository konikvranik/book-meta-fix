"""Antigravity ACP — use a Google Antigravity subscription as the FAST LLM
tier, instead of (or alongside) the Z.AI glm-flash first attempt.

WHY: a coding-plan Z.AI key draws from a tiny, dynamically throttled
concurrency ceiling shared with interactive clients (see llm.py for the whole
429/1302/1305/1113 war), while an Antigravity subscription serves Gemini
models through a separate quota that bmf's metadata prompts barely dent. The
official distribution is Google's ACP server binary
(``agy_acp_server.par``, listed as "antigravity-acp" in the ACP Registry,
https://agentclientprotocol.com/get-started/registry); the same code speaks to
ANY Agent Client Protocol agent — e.g. ``gemini --acp`` — because the command
is user-configured, not hardwired.

The wire protocol (ACP v1, https://agentclientprotocol.com): JSON-RPC 2.0,
one UTF-8 message per line, over the stdio of a subprocess bmf launches.
A turn is: initialize → (authenticate, only on an auth_required error) →
session/new → session/prompt → collect ``session/update`` notifications of
type ``agent_message_chunk`` → the ``session/prompt`` response's ``stopReason``
ends the turn.

bmf uses the agent as a stateless Q&A endpoint: every book gets a FRESH
session (a session keeps its conversation history, so reusing one across
books would bleed one book's evidence into the next and grow every
subsequent prompt), tools are denied (the evidence — first-page text — is
already in the prompt, and a library-repair tool must never run inside the
model's sandbox), and permission requests are answered "cancelled" so an
agent that insists on a tool simply answers without it. JSON in the reply is
salvaged by the same ``_parse_llm_json`` pipeline the Z.AI provider uses.
Google's prompt-level safety filter sometimes answers with boilerplate
INSTEAD of a model turn (the verbatim first-page text of a few books trips
the Prohibited Use classifier); that is detected and answered with ONE
retry on the same evidence WITHOUT the page text — the one prompt part bmf
does not author — and a still-blocked prompt gives up without burning the
quality stage on the same wall (a GLM fallback is a different provider with
a different filter and still runs).

The provider implements the full ``reconcile_loop`` contract: fast attempts
go to the agent, and the QUALITY stage is configurable — by default a SECOND
ACP pool on the fallback model (gemini-flash-high, so the whole loop stays on
the subscription); ``BMF_ANTIGRAVITY_FALLBACK=glm`` swaps in the Z.AI
flash+paid loop instead (ZaiProvider, keeping the long-measured Z.AI rate
machinery for exactly the calls that need it). When the fast tier fails
verify, the verifier's rejection reason travels into the quality stage's
evidence — a stronger model that does not know WHY the previous answer was
rejected tends to repeat it against the same evidence.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ACP v1 protocol version (a single integer = MAJOR version). The client MUST
# send its latest; an agent answering with a HIGHER major version speaks a
# wire format we have not migrated to (v2 exists and breaks fields), so the
# connection is refused loudly rather than mis-parsed.
ACP_PROTOCOL_VERSION = 1

# JSON-RPC / ACP error codes (agentclientprotocol.com/protocol/v1/schema).
ERR_AUTH_REQUIRED = -32000
ERR_METHOD_NOT_FOUND = -32601

# The official agent is an autonomous IDE agent, not a completion endpoint:
# left to itself it runs a full tool cascade around the question (measured on
# 1.1.1: ~30 model round-trips per book prompt — it lists the session cwd,
# "examines the directory content", deliberates about the verifier's rules),
# turning a ~5 s answer into tens of seconds and dragging the localharness
# into every turn. This preamble collapses the cascade to a single turn.
ACP_NO_TOOLS_PREAMBLE = """\
EXECUTION RULES for this request (highest priority):
- You are used as a stateless metadata-reconciliation FUNCTION. This one
  message already contains ALL evidence there is.
- Do NOT use any tools. Do NOT list, read, search or explore files or
  directories; the working directory is UNRELATED to this task.
- No preamble, no explanation, no step-by-step reasoning out loud.
- Reply IMMEDIATELY with the JSON object and nothing else."""

# Google's prompt-level safety filter answers INSTEAD of a model turn with
# this boilerplate (measured 2026-09-08 on a CZ sci-fi/fantasy library: the
# verbatim first-page text of a few books trips the Prohibited Use
# classifier). It is deterministic per prompt — a blocked prompt blocks
# again on every retry and on both Gemini pools — so the reaction is
# EVIDENCE REDUCTION, not retries: drop the page text (the one prompt part
# bmf does not author) and try once; a still-blocked prompt gives up
# without burning the quality stage on the same wall.
PROMPT_BLOCK_MARKERS = (
	"could not be submitted",
	"prohibited use policy",
)
PROMPT_BLOCKED_ERROR = "prompt blocked by Google's safety filter"


def _reply_is_prompt_blocked(reply: str) -> bool:
	"""True when the agent's answer is Google's prompt-block boilerplate
	rather than a model answer. Both boilerplate phrases must match — a
	model quoting one of them alone in prose stays a model answer."""
	low = reply.lower()
	return all(marker in low for marker in PROMPT_BLOCK_MARKERS)


class AcpAgentError(Exception):
	"""The ACP agent is unusable for a call: spawn failure, protocol error,
	process death, refusal, timeout, or unusable auth.

	*stderr_tail* carries the last lines the agent printed on stderr — agents
	announce interactive login URLs and startup failures there, and bmf is a
	headless client that cannot show them any other way.
	"""

	def __init__(self, message: str, *, stderr_tail: str = "") -> None:
		super().__init__(message)
		self.stderr_tail = stderr_tail

	def __str__(self) -> str:
		tail = self.stderr_tail.strip()
		if tail:
			return f"{super().__str__()} — agent stderr (tail): {tail}"
		return super().__str__()


class _TurnState:
	"""Mutable state of one prompt turn (reader thread → prompt caller)."""

	def __init__(self) -> None:
		self.chunks: list[str] = []
		self.lock = threading.Lock()


class AcpAgentConnection:
	"""One launched ACP agent subprocess speaking JSON-RPC over stdio.

	Thread model: a daemon reader thread owns stdout and dispatches messages —
	responses resolve per-id futures, agent→client requests (permission /
	fs / terminal / elicitation) get immediate non-interactive answers, and
	``session/update`` notifications append message chunks to the active turn.
	Prompt turns are serialized per connection (an ACP session is a
	conversation; concurrent prompts on one process would interleave turns),
	which the provider's pool respects by checking connections out.

	Lifecycle: ``start()`` spawns + initializes (and authenticates on demand),
	``new_session()``/``prompt()`` run turns, ``close()`` shuts down. Every
	method may raise ``AcpAgentError`` once the process is known dead.
	"""

	def __init__(
		self,
		command: list[str],
		*,
		cwd: str | os.PathLike[str] = ".",
		prompt_timeout: float = 300.0,
		init_timeout: float = 60.0,
		cancel_grace: float = 15.0,
		agent_env: dict[str, str] | None = None,
		session_cwd: str | os.PathLike[str] | None = None,
	) -> None:
		self.command = command
		self.cwd = str(cwd)
		# The cwd advertised to the AGENT (session/new) — deliberately NOT the
		# process cwd when set: the session cwd is the agent's tool playground,
		# and pointing it at the library invites the very exploration the
		# no-tools preamble forbids (defense in depth; the measured agent
		# listed it). None = fall back to the process cwd.
		self.session_cwd = str(session_cwd) if session_cwd is not None else None
		self.prompt_timeout = prompt_timeout
		self.init_timeout = init_timeout
		# After we send session/cancel the agent MUST still answer the prompt
		# request (stopReason=cancelled); this is how long we wait for that
		# courtesy before declaring the process hung and killing it.
		self.cancel_grace = cancel_grace
		# Extra environment merged OVER os.environ: agents read their
		# configuration (credentials, mode flags) from env vars, and tests
		# select their scripted behaviour this way.
		self.agent_env = agent_env
		self.proc: subprocess.Popen[str] | None = None
		self.session_id: str | None = None
		# Lane warmth: True while this connection holds a FRESH, unused session
		# with the provider's model already selected. Set by the pool's warmer
		# (or the synchronous fallback in _send_prompt) and CLEARED after the
		# first prompt turn — that session now carries the book's context and
		# must never serve another one.
		self.session_hot = False
		self.agent_info: dict[str, Any] = {}
		self.config_options: list[dict[str, Any]] = []
		self._next_id = 0
		self._id_lock = threading.Lock()
		self._write_lock = threading.Lock()
		self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
		self._turn: _TurnState | None = None
		self._stderr_tail: deque[str] = deque(maxlen=30)
		self._closed = False
		self._spawn_error: str | None = None
		# Prompts served by THIS process — the provider recycles connections
		# after a budget (see AntigravityAcpProvider.PROMPTS_PER_PROCESS).
		self.prompts_served = 0

	# ------------------------------------------------------------------
	# Low-level transport
	# ------------------------------------------------------------------

	def _stderr_pump(self) -> None:
		"""Drain the agent's stderr: agents log startup progress and —
		importantly for a headless client — interactive login instructions
		there. Kept in a bounded tail (surfaced with errors) and at DEBUG in
		the run log."""
		assert self.proc is not None and self.proc.stderr is not None
		for line in self.proc.stderr:
			line = line.rstrip()
			if line:
				self._stderr_tail.append(line)
				log.debug("ACP agent stderr: %s", line)

	def _reader(self) -> None:
		"""Own stdout: one JSON object per line, forever (ACP forbids embedded
		newlines, so readline is a full message). Any garbage line is logged
		and skipped — a chatty agent that violates the MUST NOT write non-ACP
		data to stdout rule must not wedge the client."""
		assert self.proc is not None and self.proc.stdout is not None
		for line in self.proc.stdout:
			line = line.strip()
			if not line:
				continue
			try:
				msg = json.loads(line)
			except json.JSONDecodeError:
				log.debug("ACP agent wrote a non-JSON stdout line: %s", line[:200])
				continue
			if not isinstance(msg, dict):
				continue
			try:
				self._dispatch(msg)
			except Exception:  # noqa: BLE001 - a handler bug must not kill the reader
				log.exception("ACP reader dispatch failed for %r", msg)
		# EOF: the process is gone. Wake every pending caller with an error —
		# a silent agent death would otherwise hold the pool slot until the
		# prompt timeout.
		with self._id_lock:
			pending = list(self._pending.values())
			self._pending.clear()
		for q in pending:
			q.put({"jsonrpc": "2.0", "id": 0, "error": {"code": -32603, "message": "ACP agent process exited"}})

	def _dispatch(self, msg: dict[str, Any]) -> None:
		has_method = "method" in msg
		has_id = "id" in msg
		if has_method and has_id:
			self._handle_agent_request(msg)
		elif has_method:
			self._handle_notification(msg)
		elif has_id:
			q = self._pending.get(msg.get("id"))
			if q is not None:
				q.put(msg)
			else:
				log.debug("ACP: response for unknown id %r", msg.get("id"))

	def _handle_agent_request(self, msg: dict[str, Any]) -> None:
		"""Agent→client requests. bmf is a headless, tool-less client: the
		permission request gets the cancelled outcome (the agent SHOULD then
		continue without the tool) and every capability-backed method is
		declined with method-not-found (we advertise no fs/terminal, so a
		well-behaved agent never sends these)."""
		method = msg["method"]
		rpc_id = msg["id"]
		if method == "session/request_permission":
			self._send({"jsonrpc": "2.0", "id": rpc_id, "result": {"outcome": {"outcome": "cancelled"}}})
			log.debug("ACP: permission request for %r cancelled (tool-less client)", (msg.get("params") or {}).get("toolCall"))
			return
		log.debug("ACP: declining agent request %s (capability not enabled)", method)
		self._send({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": ERR_METHOD_NOT_FOUND, "message": f"{method}: capability not enabled"}})

	def _handle_notification(self, msg: dict[str, Any]) -> None:
		if msg.get("method") != "session/update":
			log.debug("ACP notification %s ignored", msg.get("method"))
			return
		params = msg.get("params") or {}
		update = params.get("update") or {}
		kind = update.get("sessionUpdate")
		if kind == "agent_message_chunk":
			content = update.get("content") or {}
			if content.get("type") == "text":
				turn = self._turn
				if turn is not None:
					with turn.lock:
						turn.chunks.append(content.get("text") or "")
		elif kind == "tool_call":
			log.debug("ACP: agent tool call %r (denied client; result ignored)", update.get("title"))
		else:
			log.debug("ACP session update %s ignored", kind)

	def _send(self, msg: dict[str, Any]) -> None:
		assert self.proc is not None and self.proc.stdin is not None
		data = json.dumps(msg, ensure_ascii=False)
		with self._write_lock:
			if self.proc.stdin.closed:
				raise AcpAgentError("ACP agent stdin is closed", stderr_tail=self._tail())
			try:
				self.proc.stdin.write(data + "\n")
				self.proc.stdin.flush()
			except (BrokenPipeError, ValueError, OSError) as e:
				raise AcpAgentError(f"ACP agent stdin write failed: {e}", stderr_tail=self._tail()) from e

	def _tail(self) -> str:
		return "\n".join(self._stderr_tail)

	def _request(self, method: str, params: dict[str, Any], *, timeout: float, on_timeout: Callable[[], None] | None = None) -> dict[str, Any]:
		"""Send a request and wait for its response. On timeout, *on_timeout*
		fires once (the session/cancel path) and the wait is extended by
		``cancel_grace`` for the agent's mandated final answer."""
		with self._id_lock:
			if self._spawn_error:
				raise AcpAgentError(self._spawn_error, stderr_tail=self._tail())
			self._next_id += 1
			rpc_id = self._next_id
			q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
			self._pending[rpc_id] = q
		try:
			self._send({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
		except AcpAgentError:
			with self._id_lock:
				self._pending.pop(rpc_id, None)
			raise
		try:
			msg = q.get(timeout=timeout)
		except queue.Empty:
			with self._id_lock:
				self._pending.pop(rpc_id, None)
			if on_timeout is not None:
				on_timeout()
				try:
					msg = q.get(timeout=self.cancel_grace)
				except queue.Empty:
					raise AcpAgentError(f"{method} timed out after {timeout}s and did not answer the cancel either", stderr_tail=self._tail()) from None
			else:
				raise AcpAgentError(f"{method} timed out after {timeout}s", stderr_tail=self._tail()) from None
		if "error" in msg:
			err = msg["error"] or {}
			raise _AcpRpcError(int(err.get("code") or -32603), str(err.get("message") or "unknown ACP error"))
		return msg.get("result") or {}

	# ------------------------------------------------------------------
	# Lifecycle
	# ------------------------------------------------------------------

	def start(self) -> None:
		"""Spawn the subprocess and run the initialize handshake (with the
		auth_required → authenticate → retry dance).

		One connection object is bound to ONE process: a dead process is NOT
		respawned here (a respawn would hand the new process's stdin/stdout
		to a connection whose reader thread still belongs to the old one and
		race its pending-response queues). Recovery from an agent crash is
		the PROVIDER's job — it marks the connection broken, closes it, and
		checks out a fresh one."""
		if self._closed:
			raise AcpAgentError("connection already closed", stderr_tail=self._tail())
		if self.proc is not None:
			if self.proc.poll() is None:
				return
			raise AcpAgentError(
				f"ACP agent process exited (code {self.proc.returncode})",
				stderr_tail=self._tail(),
			)
		try:
			env = {**os.environ, **(self.agent_env or {})}
			self.proc = subprocess.Popen(  # noqa: S603 - the command is the user's own configured agent
				self.command,
				cwd=self.cwd,
				env=env,
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=subprocess.PIPE,
				text=True,
				encoding="utf-8",
				errors="replace",
				bufsize=1,
			)
		except OSError as e:
			self._spawn_error = f"cannot launch ACP agent {' '.join(self.command)}: {e}"
			raise AcpAgentError(self._spawn_error) from e
		threading.Thread(target=self._stderr_pump, daemon=True, name="acp-stderr").start()
		threading.Thread(target=self._reader, daemon=True, name="acp-reader").start()
		try:
			result = self._request(
				"initialize",
				{
					"protocolVersion": ACP_PROTOCOL_VERSION,
					"clientCapabilities": {
						# No fs, no terminal: the evidence travels inside the
						# prompt, and the model must not touch the library.
						"fs": {"readTextFile": False, "writeTextFile": False},
						"terminal": False,
					},
					"clientInfo": {"name": "book-meta-fix", "version": "1.0"},
				},
				timeout=self.init_timeout,
			)
		except _AcpRpcError as e:
			self.close()
			raise AcpAgentError(f"ACP initialize failed: {e}", stderr_tail=self._tail()) from e
		except AcpAgentError:
			self.close()
			raise
		version = result.get("protocolVersion")
		if not isinstance(version, int) or version > ACP_PROTOCOL_VERSION:
			self.close()
			raise AcpAgentError(
				f"ACP agent speaks protocolVersion {version!r}; bmf supports {ACP_PROTOCOL_VERSION}",
				stderr_tail=self._tail(),
			)
		self.agent_info = result
		log.debug("ACP initialized: agent=%r protocolVersion=%s", result.get("agentInfo") or result.get("serverInfo"), version)

	# ------------------------------------------------------------------
	# Sessions and prompts
	# ------------------------------------------------------------------

	def new_session(self) -> str:
		"""Create a session, transparently authenticating once on the ACP
		``auth_required`` error (-32000). A fresh session per book is the
		point of the provider (stateless Q&A — see the module docstring)."""
		if self.proc is None:
			self.start()
		try:
			return self._new_session_raw()
		except _AcpRpcError as e:
			if e.code != ERR_AUTH_REQUIRED:
				raise AcpAgentError(f"session/new failed: {e}", stderr_tail=self._tail()) from e
			self._authenticate()
			try:
				return self._new_session_raw()
			except _AcpRpcError as e2:
				raise AcpAgentError(f"session/new failed after authenticate: {e2}", stderr_tail=self._tail()) from e2

	def _new_session_raw(self) -> str:
		result = self._request("session/new", {"cwd": os.path.abspath(self.session_cwd or self.cwd), "mcpServers": []}, timeout=self.init_timeout)
		sid = result.get("sessionId")
		if not sid:
			raise AcpAgentError("session/new returned no sessionId", stderr_tail=self._tail())
		self.session_id = str(sid)
		self.config_options = list(result.get("configOptions") or [])
		return self.session_id

	def _authenticate(self) -> None:
		"""Protocol-driven auth: call ``authenticate`` with the first
		agent-type method. Terminal-type methods need an interactive tty
		(Zed / the IDE login), which a headless bmf run cannot offer — the
		error says so, with the agent's stderr tail where login URLs land."""
		methods = list((self.agent_info or {}).get("authMethods") or [])
		if not methods:
			raise AcpAgentError("ACP agent requires authentication but advertises no authMethods", stderr_tail=self._tail())
		for m in methods:
			if m.get("type", "agent") == "agent":
				log.info("ACP: authenticating with agent via %s (%s)", m.get("id"), m.get("name"))
				try:
					self._request("authenticate", {"methodId": m.get("id")}, timeout=self.init_timeout)
				except _AcpRpcError as e:
					raise AcpAgentError(
						f"ACP authenticate ({m.get('id')}) failed: {e} — run the login once interactively (e.g. in Zed or the Antigravity IDE), then retry",
						stderr_tail=self._tail(),
					) from e
				return
		raise AcpAgentError(
			"ACP agent only offers terminal-type login methods ("
			+ ", ".join(str(m.get("id")) for m in methods)
			+ "); log in once interactively (e.g. in Zed or the Antigravity IDE), then retry",
			stderr_tail=self._tail(),
		)

	def set_model(self, model: str) -> bool:
		"""Select the agent's model via the session config option of category
		"id": "model" (ACP session config options; the older modes mechanism is
		ignored). Matched by exact value/name or by token family (see
		match_model_option — "gemini-flash" picks "gemini-3-flash"). Returns
		False (and keeps the agent default) when the agent exposes no such
		option or the name matches nothing — a mismatch must never fail a
		metadata run."""
		if not model or self.session_id is None:
			return False
		opt = None
		for o in self.config_options:
			if o.get("category") == "model" or o.get("id") == "model":
				opt = o
				break
		if not opt:
			log.debug("ACP: agent exposes no model config option; using its default")
			return False
		value = match_model_option(model, opt.get("options") or [])
		if value is None:
			known = ", ".join(str(c.get("value")) for c in (opt.get("options") or []))
			log.info("ACP: model %r not offered by the agent (options: %s); using its default", model, known)
			return False
		try:
			result = self._request(
				"session/set_config_option",
				{"sessionId": self.session_id, "configId": opt.get("id"), "value": value},
				timeout=self.init_timeout,
			)
		except _AcpRpcError as e:
			log.info("ACP: set_config_option(model) refused (%s); using the agent default", e)
			return False
		self.config_options = list(result.get("configOptions") or self.config_options)
		log.debug("ACP: model set to %r", value)
		return True

	def prompt(self, text: str, *, timeout: float | None = None) -> str:
		"""One full prompt turn → the agent's message text. Raises
		AcpAgentError on transport failure, refusal, or (after a cancel) a
		still-hung agent. ``max_tokens`` is an EMPTY-ish answer, not an
		error: whatever streamed before the cap is returned and the JSON
		salvage decides if it is usable (truncation repair exists for
		exactly this)."""
		if self.session_id is None:
			raise AcpAgentError("no active session — call new_session() first", stderr_tail=self._tail())
		budget = timeout if timeout is not None else self.prompt_timeout
		self.prompts_served += 1
		turn = _TurnState()
		self._turn = turn
		try:

			def _cancel() -> None:
				log.warning("ACP: prompt exceeded %.0fs — sending session/cancel", budget)
				try:
					self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})
				except AcpAgentError:
					pass

			result = self._request(
				"session/prompt",
				{"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
				timeout=budget,
				on_timeout=_cancel,
			)
		finally:
			self._turn = None
		stop = (result or {}).get("stopReason", "end_turn")
		with turn.lock:
			out = "".join(turn.chunks)
		if stop == "refusal":
			raise AcpAgentError("the agent refused the request", stderr_tail=self._tail())
		if stop not in ("end_turn", "max_tokens", "cancelled", "max_turn_requests"):
			log.debug("ACP: unusual stopReason %r", stop)
		if stop == "max_tokens":
			log.warning("ACP: agent hit its token cap mid-answer (stopReason=max_tokens); using the truncated text")
		if stop == "cancelled":
			log.warning("ACP: prompt was cancelled (timeout); using whatever streamed")
		return out

	def close(self) -> None:
		"""Shut down: close stdin (the agent's natural exit cue), then wait,
		then kill. Safe to call twice; safe when the process already died."""
		if self._closed:
			return
		self._closed = True
		proc = self.proc
		self.proc = None
		if proc is None:
			return
		try:
			if proc.stdin and not proc.stdin.closed:
				proc.stdin.close()
		except OSError:
			pass
		try:
			proc.wait(timeout=5)
		except subprocess.TimeoutExpired:
			proc.kill()
			try:
				proc.wait(timeout=5)
			except subprocess.TimeoutExpired:
				pass


class _AcpRpcError(Exception):
	"""An error object inside an otherwise healthy JSON-RPC response."""

	def __init__(self, code: int, message: str) -> None:
		super().__init__(f"code {code}: {message}")
		self.code = code
		self.message = message


def _model_tokens(name: str) -> set[str]:
	"""Split a model name into lowercase tokens: "Gemini 3 Flash" → {gemini, 3, flash}."""
	return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t}


def match_model_option(model: str, options: list[dict[str, Any]]) -> str | None:
	"""Match *model* against an agent's model config-option candidates.

	Agents name their models with generation numbers that shift monthly
	("gemini-3.8-flash-low", "Gemini 2.5 Pro"), while the user (and bmf's
	defaults) say the FAMILY: "gemini-flash", "gemini-pro". Exact value/name
	match wins; otherwise a token-subset family match applies — {gemini,
	flash} ⊆ {gemini, 3, 8, flash} with digit-only tokens skippable —
	preferring the candidate with the fewest extra tokens. VALUE tokens are
	scored first (agents repeat the model id there; display names add
	decoration like "(High)" that would skew the distance), names only serve
	as the fallback pass for agents whose values are opaque ids. Returns the
	option VALUE or None.
	"""
	if not model:
		return None
	ml = model.lower()
	tokens = _model_tokens(model)
	for base_of in ("value", "name"):
		best: tuple[int, str] | None = None
		for cand in options or []:
			value = str(cand.get("value") or "")
			name = str(cand.get("name") or "")
			if not value:
				continue
			if value.lower() == ml or (name and name.lower() == ml):
				return value
			base = _model_tokens(value if base_of == "value" else name)
			if tokens and base and tokens <= base:
				extra = len(base - tokens)
				if best is None or extra < best[0]:
					best = (extra, value)
		if best is not None:
			return best[1]
	return None


def resolve_acp_command(command: str | None) -> list[str] | None:
	"""Resolve the configured ACP agent command into argv, or None when unset.

	A bare name is looked up on PATH; anything containing a separator is a
	path and must exist + be executable. Returns None (never raises) so the
	factory can fall through to the next provider; *why* it failed is logged.
	"""
	if not command or not command.strip():
		return None
	try:
		argv = shlex.split(command)
	except ValueError as e:
		log.warning("BMF_ANTIGRAVITY_CMD %r cannot be parsed: %s", command, e)
		return None
	if not argv:
		return None
	exe = argv[0]
	if os.path.basename(exe) == exe:
		if shutil.which(exe) is None:
			log.warning("ACP agent command %r not found on PATH", exe)
			return None
	else:
		if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
			log.warning("ACP agent command %r is not an executable file (unzipped release binaries need chmod +x)", exe)
			return None
	return argv


class AntigravityAcpProvider:
	"""LLM provider whose FAST tier is an Antigravity subscription via ACP.

	Implements the pipeline's full provider contract: ``reconcile`` (single
	call) and ``reconcile_loop`` (fast attempts with verify feedback, then —
	when a ZaiProvider was injected as *zai_fallback* — the measured Z.AI
	paid-fallback machinery, reusing its own rate limiting untouched).

	Rate limiting is deliberately minimal: a semaphore bounds how many agent
	processes run at once (each holds a subscription slot on the user's
	Antigravity account, whose limits Google does not publish — 2 is polite
	and still fast), and an optional leaky-bucket drip spaces call starts.
	All the Z.AI 429 machinery is unnecessary here: ACP agents answer or
	fail, they do not cascade-throttle. A background WARMER keeps lanes hot
	(fresh session + model pre-selected) and replaces recycled processes off
	the per-book path — the in-flight cap bounds the pool, not the latency.

	Transport failures are forgiving (one retry on a fresh process — the
	agent may have died between books) but THREE consecutive failures
	disable the fast tier for the rest of the run: every book would
	otherwise pay the spawn/timeout cost before falling back, and a broken
	login or deleted binary does not heal mid-run.
	"""

	name = "antigravity-acp"

	MAX_TRANSPORT_FAILURES = 3

	# Connections are recycled after this many prompts. The agent exposes NO
	# session/delete (measured 1.1.1: sessionCapabilities list/resume only),
	# and every session pins a localharness child (~150 MB RSS) for the
	# server process's lifetime — a 2 700-book run would pile one per book.
	# Recycling the PROCESS (graceful exit reaps its children — verified:
	# no orphans after probe runs) bounds the leak to this many harnesses
	# per pool slot. The warmer spawns the replacement lane in the
	# background, so retirement costs the next book nothing.
	PROMPTS_PER_PROCESS = 8

	def __init__(
		self,
		command: list[str],
		*,
		cwd: str | os.PathLike[str] = ".",
		model: str | None = None,
		prompt_timeout: float = 300.0,
		max_inflight: int = 2,
		min_interval: float = 0.0,
		zai_fallback: Any = None,  # ZaiProvider | None (typed loosely to dodge the circular import)
		acp_fallback: AntigravityAcpProvider | None = None,
		agent_env: dict[str, str] | None = None,
		recycle_after: int | None = None,
		warm: bool = True,
	) -> None:
		self.command = list(command)
		self.cwd = cwd
		self._model = model
		self._prompt_timeout = prompt_timeout
		self._max_inflight = max(1, max_inflight)
		self._recycle_after = self.PROMPTS_PER_PROCESS if recycle_after is None else max(0, int(recycle_after))
		self._zai_fallback = zai_fallback
		# The QUALITY stage as a second ACP pool (gemini-pro default) —
		# mutually exclusive with zai_fallback; get_provider builds exactly
		# one of them per BMF_ANTIGRAVITY_FALLBACK (agy | glm).
		self._acp_fallback = acp_fallback
		self._agent_env = agent_env
		# model/fallback_model/fallback_kind mirror the ZaiProvider surface
		# the CLI prints (primary → fallback). Empty model = the agent's own
		# default pick; fallback_kind names WHO serves the quality stage.
		self.model = model or ""
		if acp_fallback is not None:
			self.fallback_kind = "agy"
			self.fallback_model = acp_fallback.model
		elif zai_fallback is not None:
			self.fallback_kind = "glm"
			self.fallback_model = getattr(zai_fallback, "fallback_model", "")
		else:
			self.fallback_kind = ""
			self.fallback_model = ""
		from .llm import LeakyBucket

		self._bucket = LeakyBucket(capacity=1.0, interval=max(0.0, min_interval))
		self._sem = threading.Semaphore(self._max_inflight)
		# HOT lanes (fresh unused session + model selected) wait in _idle; a
		# lane that just served a prompt carries that book's context, so it
		# goes to _cold for re-heating before it may serve again. The warmer
		# thread (warm=True, the production default) does that re-heating —
		# and the process spawn + initialize for recycled/new lanes — OFF the
		# per-book path, so a waiting book pays only the prompt turn.
		self._idle: queue.LifoQueue[AcpAgentConnection] = queue.LifoQueue()
		self._cold: list[AcpAgentConnection] = []
		self._created = 0
		self._out = 0
		self._active = False
		self._warm = warm
		self._closing = False
		self._warm_kick = threading.Event()
		self._warmer: threading.Thread | None = None
		self._pool_lock = threading.Lock()
		self._transport_failures = 0
		self._disabled: str | None = None
		# The session cwd advertised to the agent (see AcpAgentConnection:
		# NOT the library). One empty scratch dir per provider lifetime,
		# created lazily and removed on close.
		self._scratch: str | None = None

	# ------------------------------------------------------------------
	# Connection pool: HOT lanes (warmed in the background), cold lanes
	# (re-heated on checkout as the deterministic fallback), lazy spawn.
	# ------------------------------------------------------------------

	def _session_scratch(self) -> str:
		"""The neutral cwd every session is created with (empty scratch dir —
		tools find nothing there even if the agent disobeys the preamble)."""
		with self._pool_lock:
			if self._scratch is None:
				self._scratch = tempfile.mkdtemp(prefix="bmf-acp-")
			return self._scratch

	def _heat(self, conn: AcpAgentConnection) -> None:
		"""Bring a lane to HOT: a FRESH session with the provider's model
		already selected. Book-independent by construction (empty session,
		neutral scratch cwd, provider-wide model) — which is what lets the
		warmer run it any time, so a waiting book pays only the prompt turn.
		Raises AcpAgentError when the agent refuses or died."""
		conn.new_session()
		conn.set_model(self._model or "")
		conn.session_hot = True

	def _pop_cold(self) -> AcpAgentConnection | None:
		with self._pool_lock:
			return self._cold.pop() if self._cold else None

	def _checkout(self) -> AcpAgentConnection:
		"""Take a lane: a HOT one when available, else a cold one (heated by
		the CALLER — the deterministic pre-warmer behaviour), else a fresh
		process; a bounded wait last."""
		while True:
			conn = None
			try:
				conn = self._idle.get_nowait()
			except queue.Empty:
				conn = self._pop_cold()
			if conn is None:
				with self._pool_lock:
					create = self._created < self._max_inflight
					if create:
						self._created += 1
				if create:
					conn = AcpAgentConnection(
						self.command,
						cwd=self.cwd,
						prompt_timeout=self._prompt_timeout,
						agent_env=self._agent_env,
						session_cwd=self._session_scratch(),
					)
					try:
						conn.start()
					except AcpAgentError:
						with self._pool_lock:
							self._created -= 1
						raise
			if conn is not None:
				with self._pool_lock:
					self._out += 1
				self._kick_warmer()
				return conn
			# Pool exhausted — wait for the warmer / a caller to return a hot
			# lane. Bounded so a leaked slot cannot park a worker forever.
			try:
				conn = self._idle.get(timeout=120.0)
			except queue.Empty:
				continue
			with self._pool_lock:
				self._out += 1
			return conn

	def _checkin(self, conn: AcpAgentConnection, *, broken: bool = False) -> None:
		with self._pool_lock:
			self._out = max(0, self._out - 1)
		# Retirement = the same mechanics as a broken connection: the process
		# is closed (its session-pinned harness children go down with it) and
		# the warmer replaces it off the per-book path.
		if broken or self._closing or (self._recycle_after and conn.prompts_served >= self._recycle_after):
			conn.close()
			with self._pool_lock:
				self._created -= 1
		else:
			# The lane's session just carried a book's evidence — re-heating
			# (a FRESH session) is required before it may serve again.
			with self._pool_lock:
				self._cold.append(conn)
		self._kick_warmer()

	# -- Background warmer ------------------------------------------------

	def _kick_warmer(self) -> None:
		"""Wake the warmer for a pass (idempotent; starts it on first demand).
		No-op when the pool runs warm=False — checkout then heats inline and
		spawns synchronously, which keeps pool-level tests deterministic."""
		if not self._warm or self._closing:
			return
		with self._pool_lock:
			if self._warmer is None:
				self._warmer = threading.Thread(target=self._warmer_loop, daemon=True, name="acp-warmer")
				self._warmer.start()
		self._warm_kick.set()

	def _warmer_loop(self) -> None:
		while True:
			self._warm_kick.wait()
			self._warm_kick.clear()
			if self._closing:
				return
			try:
				self._warm_pass()
			except Exception:  # noqa: BLE001 - the warmer must never take the pool down
				log.exception("ACP warmer pass failed")

	def _warm_pass(self) -> None:
		"""One warmer pass: re-heat every cold lane, then (once the run has
		seen LLM traffic) top the pool back up to the in-flight cap — the
		measured ~4 s process spawn and the session+model ceremony then never
		land on a waiting book. Also the test seam: warm=False providers
		invoke it directly, without the thread."""
		while not self._closing:
			conn = self._pop_cold()
			if conn is None:
				break
			try:
				self._heat(conn)
			except AcpAgentError as e:
				# Unheatable lane (agent refused / died mid-heat): retire it
				# and stop this pass — the synchronous checkout path surfaces
				# the error to _call, which owns the disable logic.
				log.debug("ACP warmer: lane re-heat failed: %s", e)
				conn.close()
				with self._pool_lock:
					self._created -= 1
				return
			self._park(conn)
		while not self._closing:
			with self._pool_lock:
				want = self._active and self._created < self._max_inflight
				if want:
					self._created += 1  # count the in-progress spawn so checkout cannot overshoot
			if not want:
				return
			conn = AcpAgentConnection(
				self.command,
				cwd=self.cwd,
				prompt_timeout=self._prompt_timeout,
				agent_env=self._agent_env,
				session_cwd=self._session_scratch(),
			)
			try:
				conn.start()
				self._heat(conn)
			except AcpAgentError as e:
				log.debug("ACP warmer: lane spawn failed: %s", e)
				conn.close()
				with self._pool_lock:
					self._created -= 1
				return
			self._park(conn)

	def _park(self, conn: AcpAgentConnection) -> None:
		"""Offer a heated lane to waiters — unless the provider closed under
		us, in which case the lane dies here (never leak a process)."""
		if self._closing:
			conn.close()
			with self._pool_lock:
				self._created -= 1
			return
		self._idle.put(conn)

	def close(self) -> None:
		"""Shut every pooled agent process (the CLI's finally-block calls
		this; analyze also ends naturally through it)."""
		if self._acp_fallback is not None:
			self._acp_fallback.close()
		self._closing = True
		self._warm_kick.set()
		if self._warmer is not None:
			self._warmer.join(timeout=5.0)
		while True:
			try:
				self._idle.get_nowait().close()
			except queue.Empty:
				break
		with self._pool_lock:
			cold, self._cold = self._cold, []
			# Connections currently checked out are closed by their callers'
			# _checkin path after close() (_closing routes it to retirement)
			# — set the counter so nothing new spawns.
			self._created = 0
			self._disabled = self._disabled or "provider closed"
		for conn in cold:
			conn.close()
		if self._scratch is not None:
			shutil.rmtree(self._scratch, ignore_errors=True)
			self._scratch = None


	# ------------------------------------------------------------------
	# LLMProvider contract
	# ------------------------------------------------------------------

	def _send_prompt(self, text: str) -> str:
		"""Prompt the agent and return its message text. The transport seam:
		tests monkeypatch this. Fresh session per prompt (stateless Q&A — see
		the module docstring): the pool serves it PRE-WARMED when it can (a
		HOT lane already holds the fresh session + model — both are
		book-independent), and heats a cold lane inline as the deterministic
		fallback. A transport error marks the connection broken and is
		retried ONCE on a fresh process before surfacing."""
		last: AcpAgentError | None = None
		self._active = True
		self._kick_warmer()
		for attempt in (1, 2):
			conn = self._checkout()
			broken = False
			try:
				if not conn.session_hot:
					self._heat(conn)
				return conn.prompt(text, timeout=self._prompt_timeout)
			except AcpAgentError as e:
				broken = True
				last = e
				log.debug("ACP prompt attempt %d failed: %s", attempt, e)
			finally:
				# Whatever happened, the lane's session must never serve
				# another book — force re-heating before the next checkout.
				conn.session_hot = False
				self._checkin(conn, broken=broken)
		raise last if last else AcpAgentError("ACP prompt failed")

	def _call(self, evidence: dict[str, Any]) -> tuple[Any, str | None]:
		"""One fast-tier attempt → (ReconciledMeta | None, error | None).
		Mirrors ZaiProvider._call's return contract so the loop logic and the
		pipeline's source labels stay provider-agnostic."""
		if self._disabled:
			return None, self._disabled
		from .llm import SYSTEM_PROMPT, _parse_llm_json, build_user_prompt

		def _send_counted(text: str) -> str:
			"""One prompt with the transport-failure bookkeeping (consecutive
			failures disable the fast tier for the run) — shared by both
			prompt variants below."""
			try:
				reply = self._send_prompt(text)
			except AcpAgentError as e:
				self._transport_failures += 1
				if self._transport_failures >= self.MAX_TRANSPORT_FAILURES:
					self._disabled = f"Antigravity ACP disabled for this run after {self._transport_failures} consecutive failures: {e}"
					log.warning("%s", self._disabled)
				raise
			self._transport_failures = 0
			return reply

		self._bucket.acquire()
		# ACP has no system role — the metadata-repair contract travels as one
		# message. Reusing the exact Z.AI system prompt keeps the two tiers'
		# answers interchangeable to the verifier and the JSON salvage. The
		# preamble rides FIRST (see ACP_NO_TOOLS_PREAMBLE): the addressee is
		# an autonomous agent that would otherwise tool-explore its way
		# through ~30 model round-trips per book.
		def _compose(ev: dict[str, Any]) -> str:
			return ACP_NO_TOOLS_PREAMBLE + "\n\n" + SYSTEM_PROMPT + "\n\n---\n\n" + build_user_prompt(ev)

		try:
			reply = _send_counted(_compose(evidence))
		except AcpAgentError as e:
			return None, str(e)
		if _reply_is_prompt_blocked(reply):
			# The verbatim page text is the one prompt part whose content bmf
			# does not control — drop it and try once more before giving up.
			# A blocked prompt is deterministic, so this is not a retry on the
			# same text but a genuinely different (smaller) prompt.
			label = (evidence.get("current") or {}).get("title")
			log.warning("ACP: prompt blocked by Google's safety filter; retrying without the page text (title: %r)", label)
			sanitized = {k: v for k, v in evidence.items() if k != "first_page_text"}
			try:
				reply = _send_counted(_compose(sanitized))
			except AcpAgentError as e:
				return None, str(e)
			if _reply_is_prompt_blocked(reply):
				log.warning("ACP: prompt still blocked by Google's safety filter without the page text; LLM skipped (title: %r)", label)
				return None, PROMPT_BLOCKED_ERROR
		result = _parse_llm_json(reply, model="antigravity-acp")
		if result is None:
			return None, "invalid JSON in ACP agent reply"
		return result, None

	def reconcile(self, evidence: dict[str, Any]) -> Any:
		"""Single fast-tier call (the loop-off path)."""
		result, error = self._call(evidence)
		if error and result is None:
			log.warning("Antigravity ACP reconcile gave up: %s", error)
		return result

	def _run_fallback(self, evidence: dict[str, Any], extracted: Any, verifier_fn: Any, feedback: str = "") -> tuple[Any, str]:
		"""The loop's QUALITY stage, per BMF_ANTIGRAVITY_FALLBACK:

		  agy (default) — ONE attempt on the second ACP pool (the fallback
		  model, default gemini-flash-high): a quality model does not need
		  cheap retries, verify decides; labelled llm:high on pass, llm:low
		  passthrough on verify fail.
		  glm — the ZaiProvider's own loop with max_flash=1 (one free flash
		  try, then the paid model), inside Z.AI's measured rate machinery;
		  its source labels travel through unchanged.

		*feedback* is the verifier's reason the fast tier was rejected. It
		travels into BOTH branches' evidence: a stronger model that does not
		know why the previous answer failed tends to repeat it against the
		same evidence — the rejection reason is the one thing this retry can
		offer that the fast tier did not have. Empty when the fast tier died
		before producing a verifiable answer (transport, bad JSON): there is
		nothing to report, the fallback runs on plain evidence.
		"""
		fb_ev = dict(evidence)
		if feedback:
			fb_ev["feedback"] = feedback
		if extracted is not None:
			broader = getattr(extracted, "broader_text", None)
			if broader and len(broader) > len(fb_ev.get("first_page_text") or ""):
				fb_ev["first_page_text"] = broader
		fb_ev["max_text_len"] = 6000
		if self._acp_fallback is not None:
			result, error = self._acp_fallback._call(fb_ev)
			if result is None:
				log.debug("ACP fallback attempt failed: %s", error)
				return None, ""
			if extracted is None:
				return result, "llm:high"
			passed, _ = verifier_fn(result, extracted)
			if passed:
				return result, "llm:high"
			result.confidence = "low"
			return result, "llm:low"
		if self._zai_fallback is not None:
			return self._zai_fallback.reconcile_loop(fb_ev, extracted, max_flash=1, verifier=verifier_fn)
		return None, ""

	def reconcile_loop(self, evidence: dict[str, Any], extracted: Any = None, *, max_flash: int = 2, verifier: Any = None) -> tuple[Any, str]:
		"""Fast tier first (Antigravity agent), quality stage second.

		Same shape and source labels as ZaiProvider.reconcile_loop — the
		pipeline's `_llm_reconcile_with_loop` treats providers uniformly:

		  1. the agent up to *max_flash* times, injecting verifier feedback
		     between attempts (sources llm:flash / llm:loop);
		  2. on any fast-tier failure (unusable, refused, bad JSON, disabled)
		     the configured quality stage runs — see _run_fallback, which
		     carries the LAST verifier feedback along so the stronger model
		     knows why the fast answer was rejected;
		  3. with no quality stage configured, the last fast proposal goes
		     out low-confidence (llm:low) or nothing ('').

		Unlike Z.AI, ANY fast-tier error is fatal for the call: an agent has
		no 1305-style "retry me in 30 s" semantics — it answered, died, or
		refused.
		"""
		from .llm import _default_verifier

		verifier_fn = verifier or _default_verifier
		last_result = None
		fb = ""
		blocked = False
		for attempt in range(max_flash):
			attempt_ev = dict(evidence)
			if fb:
				attempt_ev["feedback"] = fb
			result, error = self._call(attempt_ev)
			if result is not None:
				last_result = result
				if extracted is None:
					return result, "llm:flash" if attempt == 0 else "llm:loop"
				passed, new_fb = verifier_fn(result, extracted)
				if passed:
					return result, "llm:flash" if attempt == 0 else "llm:loop"
				fb = new_fb
				log.debug("ACP fast attempt %d failed verify: %s", attempt + 1, new_fb[:120])
				continue
			log.debug("ACP fast attempt %d failed: %s", attempt + 1, error)
			blocked = error == PROMPT_BLOCKED_ERROR
			break
		if blocked and self._acp_fallback is not None:
			# The quality stage is the SAME Gemini API behind the SAME prompt
			# filter and _run_fallback feeds it the SAME evidence (often with
			# an even LONGER page text) — a prompt blocked after the page-text
			# reduction blocks there too. Only the GLM fallback below (a
			# different provider, a different filter) still gets a chance.
			return None, ""
		result, src = self._run_fallback(evidence, extracted, verifier_fn, fb)
		if result is not None:
			return result, src
		if last_result is not None:
			last_result.confidence = "low"
			return last_result, "llm:low"
		return None, ""


# ---------------------------------------------------------------------------
# Auto-install of the official agent (ACP Registry)
# ---------------------------------------------------------------------------

# The registry's machine-readable index — the same source editors use for
# auto-install. It carries the CURRENT versioned archive URL per platform, so
# bmf never needs a hardwired version.
ACP_REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"
ACP_REGISTRY_ID = "antigravity-acp"

# version.json sidecar of an auto-installed release.
ACP_VERSION_FILE = "version.json"

# The tool-harness member of the distribution zip. The agent's own launcher
# (main.py _configure_localharness_path) hunts for localharness_external /
# localharness[.exe] NEXT TO ITS OWN BINARY and exports it as
# ANTIGRAVITY_HARNESS_PATH; a prefix match covers every platform spelling.
ACP_HARNESS_PREFIX = "localharness"

# The download is ~700 MB zipped / ~2.0 GB unpacked (server + harness) — bmf
# refuses to start it with less than this free (zip + unpacked + headroom for
# the atomic replace).
ACP_INSTALL_MIN_FREE_BYTES = 4 * 1024**3


def acp_cache_dir() -> Path:
	"""Where bmf keeps the auto-installed agent: ~/.cache/book-meta-fix/acp
	(XDG_CACHE_HOME respected); BMF_ACP_CACHE_DIR overrides."""
	if override := os.environ.get("BMF_ACP_CACHE_DIR"):
		return Path(override).expanduser()
	base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
	return Path(base).expanduser() / "book-meta-fix" / "acp"


def _registry_platform() -> str | None:
	"""Map this machine onto a registry distribution key (linux-x86_64, …)."""
	import platform

	system = platform.system().lower()
	machine = platform.machine().lower()
	if machine in ("x86_64", "amd64"):
		arch = "x86_64"
	elif machine in ("aarch64", "arm64"):
		arch = "aarch64"
	else:
		return None
	key = f"{system}-{arch}"
	return key if system in ("linux", "darwin", "windows") else None


def fetch_registry_release(*, http_get_json: Callable[..., Any] | None = None) -> dict[str, Any]:
	"""The CURRENT registry release for this platform.

	Returns {"version", "archive", "cmd", "args"} — *args* is the launch
	argument list the registry prescribes (linux: ``--uid=``). *http_get_json*
	is the no-network test seam (called with the registry URL). Raises
	AcpAgentError on network, schema, or platform failure.
	"""
	if http_get_json is None:
		import requests

		def http_get_json(url: str) -> Any:  # noqa: F811 - local seam default
			resp = requests.get(url, timeout=20)
			resp.raise_for_status()
			return resp.json()

	key = _registry_platform()
	if key is None:
		raise AcpAgentError("unsupported platform for the ACP registry (no distribution matches this system/arch)")
	try:
		data = http_get_json(ACP_REGISTRY_URL)
		agent = next(a for a in data.get("agents", []) if a.get("id") == ACP_REGISTRY_ID)
		dist = agent["distribution"]["binary"][key]
	except Exception as e:  # noqa: BLE001 - any shape/network problem is one error
		raise AcpAgentError(f"cannot read the ACP registry ({e})") from e
	return {
		"version": str(agent.get("version") or ""),
		"archive": str(dist.get("archive") or ""),
		"cmd": str(dist.get("cmd") or ""),
		"args": [str(a) for a in dist.get("args") or []],
	}


def _version_tuple(v: str) -> tuple[int, ...]:
	return tuple(int(p) for p in re.findall(r"\d+", v)[:3]) or (0,)


def installed_acp_release(cache_dir: Path | None = None) -> dict[str, Any] | None:
	"""The auto-installed release: {"version", "args", "path"} or None.

	Stale sidecars (binary deleted / exec bit lost / unreadable json /
	localharness sibling missing) degrade to None — the caller then treats
	it as not-installed, so a same-version re-install repairs it."""
	d = (cache_dir or acp_cache_dir())
	sidecar = d / ACP_VERSION_FILE
	binary = d / "agy_acp_server.par"
	try:
		info = json.loads(sidecar.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return None
	if not isinstance(info, dict) or not info.get("version"):
		return None
	bin_path = Path(info.get("path") or binary)
	if not (bin_path.is_file() and os.access(bin_path, os.X_OK)):
		return None
	# The localharness sibling is part of a COMPLETE install (see
	# install_acp_release): a binary without it answers every session/new
	# with -32603 Internal error. Only a sidecar that explicitly recorded
	# harness=null (an archive that carried no harness member) skips the
	# check — enforcing it there would loop a re-download nothing can fix.
	harnessless = "harness" in info and not info.get("harness")
	if not harnessless and not any(p.is_file() and os.access(p, os.X_OK) for p in d.glob(f"{ACP_HARNESS_PREFIX}*")):
		return None
	return {"version": str(info["version"]), "args": [str(a) for a in info.get("args") or []], "path": bin_path}


def install_acp_release(
	release: dict[str, Any] | None = None,
	*,
	cache_dir: Path | None = None,
	force: bool = False,
	progress_cb: Callable[[int, int], None] | None = None,
	http_get_json: Callable[..., Any] | None = None,
	http_get: Callable[..., Any] | None = None,
) -> tuple[Path, str, bool]:
	"""Ensure the CURRENT official agent is installed in the cache.

	Returns (binary_path, version, downloaded) — downloaded=False when the
	installed version already matched and *force* was not set. The zip is
	streamed to a temp file, the ``agy_acp_server.*`` member AND its
	``localharness*`` sibling are extracted (exec bits set, each moved into
	place with os.replace — an atomic same-fs swap, so a failed run never
	destroys the previous version), and version.json records the version +
	registry launch args + the harness member name.

	The harness is extracted even though bmf denies every tool call: the
	SERVER resolves it when CREATING a session (ProxyConnectionStrategy →
	LocalConnection), so a harness-less install answers every session/new
	with -32603 Internal error (measured 2026-09-07 on 1.1.1 — the agent's
	main.py looks for localharness_external / localharness[.exe] next to its
	own binary and only then sets ANTIGRAVITY_HARNESS_PATH).

	*http_get_json* / *http_get* are the no-network test seams.
	"""
	if release is None:
		release = fetch_registry_release(http_get_json=http_get_json)
	version = release.get("version") or ""
	archive = release.get("archive") or ""
	cmd = release.get("cmd") or "./agy_acp_server.par"
	args = list(release.get("args") or [])
	if not archive:
		raise AcpAgentError("the ACP registry entry carries no archive URL")
	# The member to extract = the registry cmd's basename (agy_acp_server.par
	# on posix, agy_acp_server.exe on windows).
	member_name = os.path.basename(cmd.replace("\\", "/"))

	d = (cache_dir or acp_cache_dir())
	d.mkdir(parents=True, exist_ok=True)
	installed = installed_acp_release(d)
	if installed and not force and _version_tuple(installed["version"]) >= _version_tuple(version):
		return installed["path"], installed["version"], False

	free = shutil.disk_usage(d).free
	if free < ACP_INSTALL_MIN_FREE_BYTES:
		log.warning(
			"ACP install: only %.1f GB free under %s (the download needs ~2.7 GB transiently) — proceeding anyway",
			free / 1024**3,
			d,
		)

	if http_get is None:
		import requests

		def http_get(url: str) -> Any:  # noqa: F811 - local seam default
			return requests.get(url, stream=True, timeout=(10, 600))

	zip_path = d / f".{member_name}.download.zip"
	try:
		resp = http_get(archive)
		total = 0
		try:
			total = int(resp.headers.get("Content-Length") or 0)
		except (TypeError, ValueError):
			total = 0
		done = 0
		with open(zip_path, "wb") as fh:
			for chunk in resp.iter_content(chunk_size=1024 * 1024):
				if not chunk:
					continue
				fh.write(chunk)
				done += len(chunk)
				if progress_cb is not None:
					progress_cb(done, total)
		import zipfile

		def _extract_member(zf: zipfile.ZipFile, member: str) -> Path:
			"""Stream one member to a dot-tmp file with the exec bit set (the
			zip stores it, but belt-and-braces — see resolve_acp_command's
			chmod note) and hand it back ready for the atomic swap."""
			tmp = d / f".{os.path.basename(member)}.new"
			with zf.open(member) as src, open(tmp, "wb") as dst:
				shutil.copyfileobj(src, dst, 1024 * 1024)
			os.chmod(tmp, 0o755)
			return tmp

		harness_name: str | None = None
		try:
			with zipfile.ZipFile(zip_path) as zf:
				matches = [n for n in zf.namelist() if os.path.basename(n) == member_name]
				if not matches:
					raise AcpAgentError(f"the downloaded archive carries no {member_name} (members: {', '.join(zf.namelist())})")
				final = d / member_name
				os.replace(_extract_member(zf, matches[0]), final)
				# The harness sits beside the server binary — the layout the
				# agent's own startup hunts for (see the docstring: without it
				# every session/new fails, tools or no tools).
				harness_member = next((n for n in zf.namelist() if os.path.basename(n).startswith(ACP_HARNESS_PREFIX)), None)
				if harness_member is not None:
					harness_name = os.path.basename(harness_member)
					os.replace(_extract_member(zf, harness_member), d / harness_name)
				else:
					log.warning(
						"ACP install: the archive carries no %s* member — the agent's sessions will fail unless ANTIGRAVITY_HARNESS_PATH points at a harness",
						ACP_HARNESS_PREFIX,
					)
		except AcpAgentError:
			raise
		except Exception as e:  # noqa: BLE001 - a truncated/garbled download is one install error
			raise AcpAgentError(f"processing the downloaded archive failed: {e}") from e
		sidecar = {
			"version": version,
			"archive": archive,
			"args": args,
			"path": str(final),
			"harness": harness_name,
		}
		(d / ACP_VERSION_FILE).write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
	finally:
		zip_path.unlink(missing_ok=True)
	log.info("ACP: installed the official agent %s -> %s", version, final)
	return final, version, True


def ensure_acp_agent(
	*,
	progress_cb: Callable[[int, int], None] | None = None,
	http_get_json: Callable[..., Any] | None = None,
	http_get: Callable[..., Any] | None = None,
	cache_dir: Path | None = None,
) -> tuple[list[str], str] | None:
	"""Self-managed agent lifecycle for commands that need agy: ensure the
	cached copy EXISTS and MATCHES the registry's current version,
	downloading/installing/upgrading on demand (there is no separate install
	command — the run that wants the agent fetches it itself).

	Returns (argv, version) — argv includes the registry-prescribed launch
	args (linux: ``--uid=``) — or None when nothing is installed AND the
	registry cannot be reached (a genuinely cold offline machine).

	Offline resilience: an installed agent is used AS-IS when the registry
	check fails (a warning says so) — a run must not lose its LLM tier to a
	DNS blip. The ~700 MB download streams through *progress_cb(done, total)*
	when provided; *http_get_json* / *http_get* are the no-network test seams.
	"""
	installed = installed_acp_release(cache_dir)
	try:
		release = fetch_registry_release(http_get_json=http_get_json)
	except Exception as e:  # noqa: BLE001 - advisory lookup, never fatal here
		if installed is not None:
			log.warning("ACP: cannot check the registry for updates (%s) — using the installed agent %s as-is", e, installed["version"])
			return [str(installed["path"]), *installed["args"]], installed["version"]
		log.warning("ACP: cannot reach the registry and no cached agent is installed (%s)", e)
		return None
	if installed is not None and _version_tuple(installed["version"]) >= _version_tuple(release["version"]):
		return [str(installed["path"]), *installed["args"]], installed["version"]
	try:
		path, version, _downloaded = install_acp_release(release, cache_dir=cache_dir, force=False, progress_cb=progress_cb, http_get_json=http_get_json, http_get=http_get)
	except AcpAgentError as e:
		# The swap is atomic — a failed upgrade leaves the previous version
		# usable, so prefer it over nothing.
		if installed is not None:
			log.error("ACP: agent upgrade failed (%s) — keeping the installed %s", e, installed["version"])
			return [str(installed["path"]), *installed["args"]], installed["version"]
		log.error("ACP: agent install failed (%s)", e)
		return None
	return [str(path), *(release.get("args") or [])], version
