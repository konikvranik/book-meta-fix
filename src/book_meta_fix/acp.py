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

The provider implements the full ``reconcile_loop`` contract: fast attempts
go to the agent, and the QUALITY stage is configurable — by default a SECOND
ACP pool on the fallback model (gemini-pro, so the whole loop stays on the
subscription); ``BMF_ANTIGRAVITY_FALLBACK=glm`` swaps in the Z.AI flash+paid
loop instead (ZaiProvider, keeping the long-measured Z.AI rate machinery for
exactly the calls that need it).
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
import threading
from collections import deque
from collections.abc import Callable
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
	) -> None:
		self.command = command
		self.cwd = str(cwd)
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
		result = self._request("session/new", {"cwd": os.path.abspath(self.cwd), "mcpServers": []}, timeout=self.init_timeout)
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
	("gemini-3-flash", "Gemini 2.5 Pro"), while the user (and bmf's defaults)
	say the FAMILY: "gemini-flash", "gemini-pro". Exact value/name match wins;
	otherwise a token-subset family match applies — {gemini, flash} ⊆
	{gemini, 3, flash} with the candidate's digit-only tokens skippable —
	preferring the candidate with the fewest extra tokens (gemini-3-flash
	over gemini-3-flash-preview). Returns the option VALUE or None.
	"""
	if not model:
		return None
	ml = model.lower()
	tokens = _model_tokens(model)
	best: tuple[int, str] | None = None
	for cand in options or []:
		value = str(cand.get("value") or "")
		name = str(cand.get("name") or "")
		if not value:
			continue
		if value.lower() == ml or (name and name.lower() == ml):
			return value
		base = _model_tokens(value) | (_model_tokens(name) if name else set())
		if tokens and tokens <= base:
			extra = len(base - tokens)
			if best is None or extra < best[0]:
				best = (extra, value)
	return best[1] if best is not None else None


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
	fail, they do not cascade-throttle.

	Transport failures are forgiving (one retry on a fresh process — the
	agent may have died between books) but THREE consecutive failures
	disable the fast tier for the rest of the run: every book would
	otherwise pay the spawn/timeout cost before falling back, and a broken
	login or deleted binary does not heal mid-run.
	"""

	name = "antigravity-acp"

	MAX_TRANSPORT_FAILURES = 3

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
	) -> None:
		self.command = list(command)
		self.cwd = cwd
		self._model = model
		self._prompt_timeout = prompt_timeout
		self._max_inflight = max(1, max_inflight)
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
		self._idle: queue.LifoQueue[AcpAgentConnection] = queue.LifoQueue()
		self._created = 0
		self._pool_lock = threading.Lock()
		self._transport_failures = 0
		self._disabled: str | None = None

	# ------------------------------------------------------------------
	# Connection pool (lazy: processes cost memory; spawn on demand)
	# ------------------------------------------------------------------

	def _checkout(self) -> AcpAgentConnection:
		while True:
			try:
				return self._idle.get_nowait()
			except queue.Empty:
				pass
			with self._pool_lock:
				if self._created < self._max_inflight:
					self._created += 1
					create = True
				else:
					create = False
			if create:
				conn = AcpAgentConnection(
					self.command,
					cwd=self.cwd,
					prompt_timeout=self._prompt_timeout,
					agent_env=self._agent_env,
				)
				try:
					conn.start()
				except AcpAgentError:
					with self._pool_lock:
						self._created -= 1
					raise
				return conn
			# Pool exhausted — wait for a return. Bounded so a leaked slot
			# cannot park a worker forever; loop re-checks.
			try:
				return self._idle.get(timeout=120.0)
			except queue.Empty:
				continue

	def _checkin(self, conn: AcpAgentConnection, *, broken: bool = False) -> None:
		if broken:
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
		while True:
			try:
				self._idle.get_nowait().close()
			except queue.Empty:
				break
		with self._pool_lock:
			# Connections currently checked out are closed by their callers'
			# _checkin(broken=True) path after close() — set the counter so
			# nothing new spawns.
			self._created = 0
			self._disabled = self._disabled or "provider closed"

	# ------------------------------------------------------------------
	# LLMProvider contract
	# ------------------------------------------------------------------

	def _send_prompt(self, text: str) -> str:
		"""Prompt the agent and return its message text. The transport seam:
		tests monkeypatch this. Fresh session per prompt (stateless Q&A — see
		the module docstring); a transport error marks the connection broken
		and is retried ONCE on a fresh process before surfacing."""
		last: AcpAgentError | None = None
		for attempt in (1, 2):
			conn = self._checkout()
			broken = False
			try:
				conn.new_session()
				conn.set_model(self._model or "")
				return conn.prompt(text, timeout=self._prompt_timeout)
			except AcpAgentError as e:
				broken = True
				last = e
				log.debug("ACP prompt attempt %d failed: %s", attempt, e)
			finally:
				self._checkin(conn, broken=broken)
		raise last if last else AcpAgentError("ACP prompt failed")

	def _call(self, evidence: dict[str, Any]) -> tuple[Any, str | None]:
		"""One fast-tier attempt → (ReconciledMeta | None, error | None).
		Mirrors ZaiProvider._call's return contract so the loop logic and the
		pipeline's source labels stay provider-agnostic."""
		if self._disabled:
			return None, self._disabled
		from .llm import SYSTEM_PROMPT, _parse_llm_json, build_user_prompt

		self._bucket.acquire()
		# ACP has no system role — the metadata-repair contract travels as one
		# message. Reusing the exact Z.AI system prompt keeps the two tiers'
		# answers interchangeable to the verifier and the JSON salvage.
		text = SYSTEM_PROMPT + "\n\n---\n\n" + build_user_prompt(evidence)
		try:
			reply = self._send_prompt(text)
		except AcpAgentError as e:
			self._transport_failures += 1
			if self._transport_failures >= self.MAX_TRANSPORT_FAILURES:
				self._disabled = f"Antigravity ACP disabled for this run after {self._transport_failures} consecutive failures: {e}"
				log.warning("%s", self._disabled)
			return None, str(e)
		self._transport_failures = 0
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

	def _run_fallback(self, evidence: dict[str, Any], extracted: Any, verifier_fn: Any) -> tuple[Any, str]:
		"""The loop's QUALITY stage, per BMF_ANTIGRAVITY_FALLBACK:

		  agy (default) — ONE attempt on the second ACP pool (gemini-pro): a
		  quality model does not need cheap retries, verify decides;
		  labelled llm:high on pass, llm:low passthrough on verify fail.
		  glm — the ZaiProvider's own loop with max_flash=1 (one free flash
		  try, then the paid model), inside Z.AI's measured rate machinery;
		  its source labels travel through unchanged.
		"""
		if self._acp_fallback is not None:
			result, error = self._acp_fallback._call(evidence)
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
			return self._zai_fallback.reconcile_loop(evidence, extracted, max_flash=1, verifier=verifier_fn)
		return None, ""

	def reconcile_loop(self, evidence: dict[str, Any], extracted: Any = None, *, max_flash: int = 2, verifier: Any = None) -> tuple[Any, str]:
		"""Fast tier first (Antigravity agent), quality stage second.

		Same shape and source labels as ZaiProvider.reconcile_loop — the
		pipeline's `_llm_reconcile_with_loop` treats providers uniformly:

		  1. the agent up to *max_flash* times, injecting verifier feedback
		     between attempts (sources llm:flash / llm:loop);
		  2. on any fast-tier failure (unusable, refused, bad JSON, disabled)
		     the configured quality stage runs — see _run_fallback;
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
			break
		result, src = self._run_fallback(evidence, extracted, verifier_fn)
		if result is not None:
			return result, src
		if last_result is not None:
			last_result.confidence = "low"
			return last_result, "llm:low"
		return None, ""
