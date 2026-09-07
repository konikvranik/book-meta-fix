#!/usr/bin/env python3
"""A minimal scripted Agent Client Protocol (v1) agent for tests.

Speaks the real wire protocol — JSON-RPC 2.0, one message per line over
stdio — so test_acp.py exercises bmf's client against an actual subprocess,
not a mock of its own code. No dependencies, no network.

The behaviour is selected with MOCK_ACP_MODE:
	basic          initialize → session/new → prompt; answers with a JSON
	               object whose title echoes the "PROBE:<token>" marker from
	               the prompt (proves the prompt text arrived intact)
	auth           advertises an agent-type authMethod; session/new answers
	               -32000 auth_required until `authenticate` is called
	auth_terminal  advertises ONLY a terminal-type login method (headless
	               clients cannot complete it)
	permission     during the prompt sends session/request_permission and
	               echoes the client's outcome into the answer
	fs             during the prompt sends fs/read_text_file and echoes the
	               client's error code into the answer
	model          answers with the model value set via
	               session/set_config_option ("default" when never set)
	cwd            answers with the cwd the client sent in session/new
	               (proves sessions are created against a neutral scratch dir)
	pid            answers with the agent process's pid (proves connection
	               recycling: a retired process is replaced by a new pid)
	version2       answers initialize with protocolVersion 2 (unsupported)
	slow           never answers the prompt (hangs past any timeout)
	crash          exits right after initialize
"""
from __future__ import annotations

import json
import os
import sys

MODE = os.environ.get("MOCK_ACP_MODE", "basic")

CONFIG_OPTIONS = [
	{
		"id": "model",
		"name": "Model",
		"category": "model",
		"type": "select",
		"currentValue": "gemini-3-pro",
		"options": [
			{"value": "gemini-3-pro", "name": "Gemini 3 Pro"},
			{"value": "gemini-3-flash", "name": "Gemini 3 Flash"},
		],
	}
]


def send(msg: dict) -> None:
	sys.stdout.write(json.dumps(msg) + "\n")
	sys.stdout.flush()


def reply_result(rpc_id, res: dict) -> None:
	send({"jsonrpc": "2.0", "id": rpc_id, "result": res})


def reply_error(rpc_id, code: int, message: str) -> None:
	send({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})


def read_msg() -> dict:
	line = sys.stdin.readline()
	if not line:
		sys.exit(0)
	return json.loads(line)


def request_to_client(method: str, params: dict, rpc_id: int) -> dict:
	"""Send an agent→client request and block until ITS response arrives,
	swallowing unrelated notifications (e.g. session/cancel) meanwhile."""
	send({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
	while True:
		msg = read_msg()
		if msg.get("id") == rpc_id and "method" not in msg:
			return msg


def main() -> None:
	authenticated = False
	model_set = None
	session_cwd = None
	next_id = 100

	while True:
		msg = read_msg()
		method = msg.get("method")
		rpc_id = msg.get("id")
		params = msg.get("params") or {}

		if method == "initialize":
			res: dict = {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}
			if MODE == "auth":
				res["authMethods"] = [{"id": "agent-login", "name": "Agent login"}]
			elif MODE == "auth_terminal":
				res["authMethods"] = [
					{"id": "terminal-login", "name": "Terminal login", "type": "terminal", "args": ["--login"]}
				]
			if MODE == "version2":
				res["protocolVersion"] = 2
			reply_result(rpc_id, res)
			if MODE == "crash":
				# Die AFTER answering initialize — the "agent died between
				# books" shape (init succeeds, the very next request EOFs).
				sys.exit(3)
		elif method == "authenticate":
			authenticated = True
			reply_result(rpc_id, {})
		elif method == "session/new":
			if MODE in ("auth", "auth_terminal") and not authenticated:
				reply_error(rpc_id, -32000, "Authentication required")
			else:
				session_cwd = params.get("cwd")
				reply_result(rpc_id, {"sessionId": "sess-mock-1", "configOptions": CONFIG_OPTIONS})
		elif method == "session/set_config_option":
			model_set = params.get("value")
			reply_result(rpc_id, {"configOptions": CONFIG_OPTIONS})
		elif method == "session/prompt":
			sid = params.get("sessionId")
			prompt_text = "".join(
				b.get("text", "") for b in params.get("prompt", []) if isinstance(b, dict)
			)
			echo = ""
			if MODE == "permission":
				next_id += 1
				resp = request_to_client(
					"session/request_permission",
					{
						"sessionId": sid,
						"toolCall": {"toolCallId": "t1", "title": "read the library"},
						"options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
					},
					next_id,
				)
				outcome = (resp.get("result") or {}).get("outcome") or {}
				echo = "outcome:" + str(outcome.get("outcome", "?"))
			elif MODE == "fs":
				next_id += 1
				resp = request_to_client(
					"fs/read_text_file", {"sessionId": sid, "path": "/etc/passwd"}, next_id
				)
				echo = "fs_error:" + str((resp.get("error") or {}).get("code"))
			if MODE == "slow":
				# Never answers — the client must cancel, give up, and kill us.
				while True:
					read_msg()
			probe = "none"
			if "PROBE:" in prompt_text:
				probe = prompt_text.split("PROBE:", 1)[1].split()[0].strip('"')
			title = probe
			if MODE == "model":
				title = model_set or "default"
			elif MODE == "cwd":
				title = session_cwd or "no-cwd-sent"
			elif MODE == "pid":
				title = str(os.getpid())
			payload = json.dumps({"title": title, "authors": ["Test Author"], "confidence": "high", "reasoning": echo})
			half = len(payload) // 2
			# Two streamed chunks — the client must concatenate them in order.
			send({
				"jsonrpc": "2.0",
				"method": "session/update",
				"params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": payload[:half]}}},
			})
			send({
				"jsonrpc": "2.0",
				"method": "session/update",
				"params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": payload[half:]}}},
			})
			reply_result(rpc_id, {"stopReason": "end_turn"})
		elif method == "session/cancel":
			pass  # notification — nothing pending in this scripted agent
		else:
			if rpc_id is not None:
				reply_error(rpc_id, -32601, f"mock agent does not implement {method}")


if __name__ == "__main__":
	main()
