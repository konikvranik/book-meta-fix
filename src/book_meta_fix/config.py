"""Configuration: paths, API keys, tunables.

Resolution order for every setting (highest precedence first):
	1. CLI flag (--library, ...)
	2. Process environment variable (BMF_LIBRARY, ZAI_API_KEY, ...)
	3. .env file — found by walking up from CWD: ./.env, ../.env, ../../.env, ...
	   (the first existing .env wins; its keys are loaded into os.environ
	   only if not already set there, so real env vars still take precedence)
	4. Built-in default
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _deprecated(old: str, new: str) -> None:
	log.warning("Env var %s is deprecated; use %s instead (read anyway).", old, new)

# Neutral fallback — every real setup overrides this via $BMF_LIBRARY or
# --library; the built-in default only needs to exist, not point anywhere real.
DEFAULT_LIBRARY = Path("~/Books").expanduser()
DEFAULT_CACHE = Path("bmf_cache.db")
DEFAULT_REVIEW = Path("review.yaml")

# Online lookup settings
DEFAULT_API_RATE_SEC = 1.0  # min seconds between API calls
DEFAULT_API_TIMEOUT = 15.0


@dataclass
class Config:
	library: Path = DEFAULT_LIBRARY
	cache_db: Path = DEFAULT_CACHE
	review_file: Path = DEFAULT_REVIEW

	# Online enrichers
	obalkyknih_enabled: bool = True
	google_books_enabled: bool = True
	openlibrary_enabled: bool = True
	databazeknih_enabled: bool = False  # scraping, opt-in
	legie_enabled: bool = False  # legie.info scraping (CZ/SK sci-fi/fantasy), opt-in
	# Self-hosted audiobookshelf_czech_metadata instance (an aggregator over
	# ~17 CZ audiobook storefronts speaking ABS's custom-provider contract;
	# github.com/stecik/audiobookshelf_czech_metadata). Empty URL = disabled;
	# set the BASE url (…:8000), not the /search endpoint. Override via
	# BMF_ABS_CZECH_URL or --abs-czech.
	abs_czech_url: str = ""
	# Bearer token for an instance deployed with AUDIOBOOKSHELF_AUTH_TOKEN.
	# Override via BMF_ABS_CZECH_TOKEN.
	abs_czech_token: str | None = None
	# The user's actual Audiobookshelf server, targeted by `bmf abs-rescan`
	# (ABS keeps its own database, so bmf's disk writes only appear there
	# after a per-item API re-scan). Empty URL = the command is unavailable.
	# Set the BASE url (e.g. http://abs.lan:13378), not an endpoint.
	# Override via BMF_ABS_URL or `bmf abs-rescan --url`.
	abs_url: str = ""
	# Admin API token — the scan endpoints reject non-admin tokens (403).
	# Settings -> Users -> API key in the ABS web UI. Override via
	# BMF_ABS_TOKEN.
	abs_token: str | None = None
	# ABS library name or id when the server hosts several book libraries;
	# empty = auto-detect (a single book library, or a folder-path match).
	# Override via BMF_ABS_LIBRARY or `bmf abs-rescan --abs-library`.
	abs_library: str = ""
	# Parallel per-item scan workers for `bmf abs-rescan --apply`. Each POST
	# waits for ABS to synchronously re-scan one item, so the serial loop
	# crawls at ~1.5 items/s; a small pool raises the aggregate rate about
	# N-fold. 4 is gentle on a small NAS (cover re-picks spawn ffmpeg);
	# 1 = the historical serial behaviour. Override via BMF_ABS_WORKERS or
	# `bmf abs-rescan --abs-workers`.
	abs_workers: int = 4

	# Threads for the library scan itself (tree walk + per-folder metadata
	# reads + uuid minting). The scan is NFS-latency-bound, not CPU-bound —
	# every readdir/stat/open is a network round trip — so a small pool cuts
	# minutes to tens of seconds; 8 measured as a good default. 1 restores
	# the historical serial scan. Override via BMF_SCAN_WORKERS or
	# `bmf analyze --scan-workers`.
	scan_workers: int = 8

	# API rate limit / timeout
	api_rate_sec: float = DEFAULT_API_RATE_SEC
	api_timeout: float = DEFAULT_API_TIMEOUT

	# Enricher cache: how long a cached negative lookup ("__NOT_FOUND__") is
	# trusted before it is re-queried. A book that failed an online lookup once
	# (transient API error, or before its identity was fixed) gets retried after
	# this many seconds. <= 0 disables expiry (negatives kept forever, the old
	# behaviour). Default 7 days. Override via BMF_ENRICH_NEGATIVE_TTL.
	enrich_negative_ttl_sec: float = 7 * 24 * 3600

	# LLM (Z.AI)
	# Coding plan users must use /api/coding/paas/v4/ (draws from subscription
	# quota). PaaS / pay-as-you-go users use /api/paas/v4/ (per-token billing).
	# Override via ZAI_BASE_URL if you have a PaaS key.
	zai_api_key: str | None = field(default=None)
	zai_base_url: str = "https://api.z.ai/api/coding/paas/v4/"
	# Separate endpoint for flash-family models (measured 2026-08-28: the
	# coding endpoint's ~5-request concurrency ceiling and the PaaS endpoint's
	# are independent, and a coding-plan key may call glm-4.x flash on PaaS
	# for FREE — separate pool + zero coding credits; paid models 1113 on
	# PaaS and glm-5.x flash is not served there, so those stay on the
	# primary). None/empty = AUTO (split when the primary is the coding
	# endpoint); a URL forces it; 'off'/'0' disables the split. Override via
	# ZAI_FLASH_BASE_URL.
	zai_flash_base_url: str | None = None
	# Primary LLM model: first attempt of the self-correction loop, and the
	# single-call model when the loop is off. None = resolved at provider
	# construction: glm-4.7-flash (free) when the loop is on, the fallback
	# model when off (a single call should go straight to the quality model).
	# Override via BMF_LLM_MODEL (legacy: ZAI_MODEL for the loop-off case,
	# ZAI_FLASH_MODEL for the loop-on case — deprecated).
	llm_model: str | None = None
	# Reasoning effort for GLM-5.x models (low|medium|max). Ignored by GLM-4.x
	# (use zai_thinking for those). 'low' keeps quality while cutting ~60% of
	# reasoning tokens vs the default. Override via ZAI_REASONING_EFFORT.
	zai_reasoning_effort: str = "low"
	# Thinking toggle for GLM-4.6/4.5-Air/4.5-Flash/4.7-Flash (enabled|disabled).
	# Only applied when the model is NOT a GLM-5.x (which uses reasoning_effort).
	# 'disabled' turns off chain-of-thought, drastically cutting output tokens.
	# Override via ZAI_THINKING.
	zai_thinking: str = "disabled"
	# Self-correction loop: try the (free flash) model first (with verify
	# feedback), then the fallback model. Override via BMF_LLM_LOOP=0.
	llm_loop: bool = True
	# Paid high-quality fallback model (and the loop-off single-call default).
	# Override via BMF_LLM_FALLBACK_MODEL (legacy: ZAI_FINAL_MODEL /
	# ZAI_MODEL — deprecated).
	llm_fallback_model: str = "glm-5.3"
	# Leaky-bucket burst capacity: how many LLM calls may start inside one
	# interval. Default 1 = pure even drip (one call every interval seconds,
	# no bunching) — this is the count-per-time semantics Z.AI's sliding-window
	# request limit wants. A burst > 1 lets N calls fire in the same second,
	# which is exactly what trips the dynamic RPM limit (429 code 1302); raise
	# only with confirmed rate headroom. Override via BMF_LLM_BURST.
	llm_burst: float = 1.0
	# Minimum seconds between LLM requests (RPM throttle). Z.AI's coding plan
	# applies a dynamic requests-per-minute limit; 429 'Rate limit reached for
	# requests' (code 1302) trips when too many calls land inside a rolling
	# window. A 2.0s floor caps us at ~30 RPM regardless of worker count or API
	# response speed. Lower (e.g. 1.0) on a higher tier; raise (e.g. 4.0) if you
	# still hit 429.
	llm_min_interval: float = 2.0
	# Hard cap on LLM requests RUNNING at the same instant (across all
	# workers/models/retries). The Z.AI coding plan admits only ~5 concurrent
	# requests per account (measured: a 12-deep spike -> 7x 429/1302 while a
	# 6-deep burst passed), and interactive clients (ZCode/chat) draw from the
	# same ceiling — so the default keeps headroom. Unlike llm_min_interval
	# (which spaces call STARTS), this caps in-flight DEPTH: with 10 workers
	# and multi-second reasoning calls the fallback herd blows past the limit
	# and the resulting storms also trigger false 1113 'insufficient balance'.
	# Override via BMF_LLM_MAX_INFLIGHT.
	llm_max_inflight: int = 3
	# Global 429 cooldown knobs. When ANY worker sees a 429, ALL workers pause
	# until this many seconds have passed (Z.AI's free tier cascade-throttles
	# every model when one 429s, so per-worker throttling alone can't stop it).
	# The cooldown escalates with consecutive 429s (base * 2**(n-1): 5, 10,
	# 20, ...), honours the server's Retry-After when longer, and is capped at
	# rate_limit_max. Override via BMF_LLM_RATE_LIMIT_BASE / _MAX. Lower base =
	# more aggressive (less waiting, more 429 risk); higher = safer but slower.
	llm_rate_limit_base: float = 5.0
	llm_rate_limit_max: float = 60.0

	# LLM provider selection. Empty = auto (Z.AI when a key exists, then the
	# Antigravity ACP agent when its command is configured, then the mock,
	# then none). Explicit values: 'antigravity' (aliases 'acp', 'agy') force
	# the ACP fast tier (still falling back to Z.AI for the paid stage when a
	# key exists), 'zai' forbids ACP, 'mock' forces the offline mock, 'off'
	# disables the LLM stage. Override via BMF_LLM_PROVIDER.
	llm_provider: str = ""
	# Google Antigravity subscription as the FAST LLM tier: the command that
	# launches an Agent Client Protocol agent process bmf speaks to over
	# stdio (JSON-RPC, ACP v1). The official agent is Google's
	# `agy_acp_server.par` from the ACP Registry ("antigravity-acp"); any ACP
	# agent works, e.g. "gemini --acp". When set (with BMF_LLM_PROVIDER unset
	# or =antigravity), the agent replaces the glm-flash first attempts of
	# the loop and Z.AI (if a key exists) becomes the paid fallback only.
	# Example: /opt/agy/agy_acp_server.par — release zips need chmod +x.
	# Override via BMF_ANTIGRAVITY_CMD (alias: BMF_ACP_COMMAND).
	acp_command: str = ""
	# Model the ACP agent should serve the fast tier with. Matched against
	# the agent's own session config options by exact value/name or by token
	# family; explicitly empty (via BMF_ANTIGRAVITY_MODEL=) = the agent's
	# default pick. Default "gemini-flash-low": the real agent (measured on
	# agy_acp_server 1.1.1) offers flash as gemini-3.8/3.7/3.6-flash-
	# high|medium|low — the family match resolves to the NEWEST flash at LOW
	# effort, which is what a quick-check tier wants (latency, not
	# deliberation). Override via BMF_ANTIGRAVITY_MODEL (alias: BMF_ACP_MODEL).
	acp_model: str | None = "gemini-flash-low"
	# The loop's QUALITY (fallback) stage when the ACP agent is the fast
	# tier: 'agy' (default — a SECOND ACP pool on acp_fallback_model, so the
	# whole loop stays on the subscription) or 'glm' (the Z.AI flash+paid
	# loop; needs ZAI_API_KEY and reuses Z.AI's rate machinery). Override via
	# BMF_ANTIGRAVITY_FALLBACK (alias: BMF_ACP_FALLBACK).
	acp_fallback_provider: str = "agy"
	# Model for the agy quality stage, family-matched like acp_model.
	# Default "gemini-pro". Override via BMF_ANTIGRAVITY_FALLBACK_MODEL
	# (alias: BMF_ACP_FALLBACK_MODEL).
	acp_fallback_model: str | None = "gemini-pro"
	# Seconds before a hung ACP prompt turn is cancelled (session/cancel)
	# and the connection killed. Gemini reasoning calls usually finish in
	# tens of seconds; 300 is generous headroom. Override via BMF_ACP_TIMEOUT.
	acp_prompt_timeout: float = 300.0
	# How many ACP agent processes may run prompts at once (each draws from
	# the subscription's concurrency on the user's account; 2 is polite and
	# still keeps the fast tier ahead of the enrichers). Override via
	# BMF_ACP_MAX_INFLIGHT.
	acp_max_inflight: int = 2
	# Minimum seconds between ACP prompt starts (politeness drip; 0 = as fast
	# as the in-flight cap allows). Override via BMF_ACP_MIN_INTERVAL.
	acp_min_interval: float = 0.0

	# Interface language for CLI/GUI messages ('cs' | 'en'). Empty string =
	# auto-detect from the user's locale (LC_ALL/LC_MESSAGES/LANG; cs* → Czech,
	# anything else → English). Override via BMF_LANGUAGE or --lang.
	language: str = ""

	# Placement pattern for OK books (apply's routing + the C13 location
	# detector). None = mover.DEFAULT_PATH_PATTERN ('{author}/{title} ({id})').
	# Override via BMF_PATTERN or --pattern.
	path_pattern: str | None = None
	# Folder for books apply cannot consider resolved. None =
	# mover.DEFAULT_NEEDFIX_DIR ('needfix'). Override via BMF_NEEDFIX_DIR or
	# --needfix-dir.
	needfix_dir: str | None = None

	# Verification thresholds
	verify_fuzzy_strong: float = 0.8  # >= -> VERIFIED
	verify_fuzzy_weak: float = 0.5  # >= -> NEEDS_REVIEW (uncertain)

	@classmethod
	def from_env(cls) -> Config:
		# Load .env first (walking up from CWD), so its values populate
		# os.environ as defaults. Real env vars still win because load_dotenv
		# is called with override=False.
		load_dotenv_walk_up()
		cfg = cls()
		# Library / paths
		if v := os.environ.get("BMF_LIBRARY"):
			cfg.library = Path(v)
		if v := os.environ.get("BMF_CACHE"):
			cfg.cache_db = Path(v)
		if v := os.environ.get("BMF_REVIEW"):
			cfg.review_file = Path(v)
		# Enrichers (opt-in/out)
		if (v := os.environ.get("BMF_DATABAZEKNIH")) is not None:
			cfg.databazeknih_enabled = v.strip().lower() in ("1", "true", "yes", "on")
		if (v := os.environ.get("BMF_LEGIE")) is not None:
			cfg.legie_enabled = v.strip().lower() in ("1", "true", "yes", "on")
		if (v := os.environ.get("BMF_ABS_CZECH_URL")) is not None:
			cfg.abs_czech_url = v.strip()
		if (v := os.environ.get("BMF_ABS_CZECH_TOKEN")) is not None:
			cfg.abs_czech_token = v.strip() or None
		if (v := os.environ.get("BMF_ABS_URL")) is not None:
			cfg.abs_url = v.strip()
		if (v := os.environ.get("BMF_ABS_TOKEN")) is not None:
			cfg.abs_token = v.strip() or None
		if (v := os.environ.get("BMF_ABS_LIBRARY")) is not None:
			cfg.abs_library = v.strip()
		if (v := os.environ.get("BMF_ABS_WORKERS")) is not None:
			try:
				cfg.abs_workers = max(1, int(v))
			except ValueError:
				pass
		if (v := os.environ.get("BMF_SCAN_WORKERS")) is not None:
			try:
				cfg.scan_workers = max(1, int(v))
			except ValueError:
				pass
		if (v := os.environ.get("BMF_OPENLIBRARY")) is not None:
			cfg.openlibrary_enabled = v.strip().lower() in ("1", "true", "yes", "on")
		if (v := os.environ.get("BMF_GOOGLE_BOOKS")) is not None:
			cfg.google_books_enabled = v.strip().lower() in ("1", "true", "yes", "on")
		# Interface language (cs|en; empty = auto-detect from locale)
		if v := os.environ.get("BMF_LANGUAGE"):
			cfg.language = v.strip().lower()
		# Placement (apply routing + C13 location detector)
		if v := os.environ.get("BMF_PATTERN"):
			cfg.path_pattern = v.strip()
		if v := os.environ.get("BMF_NEEDFIX_DIR"):
			cfg.needfix_dir = v.strip()
		# Enricher negative-cache TTL (seconds)
		if (v := os.environ.get("BMF_ENRICH_NEGATIVE_TTL")) is not None:
			try:
				cfg.enrich_negative_ttl_sec = float(v)
			except ValueError:
				pass
		# LLM
		if v := os.environ.get("ZAI_API_KEY"):
			cfg.zai_api_key = v
		if v := os.environ.get("ZAI_BASE_URL"):
			cfg.zai_base_url = v
		if (v := os.environ.get("ZAI_FLASH_BASE_URL")) is not None:
			cfg.zai_flash_base_url = v.strip() or None
		if v := os.environ.get("BMF_LLM_MODEL"):
			cfg.llm_model = v.strip()
		if v := os.environ.get("BMF_LLM_FALLBACK_MODEL"):
			cfg.llm_fallback_model = v.strip()
		# Legacy aliases (deprecated): ZAI_FLASH_MODEL was the loop's first
		# attempt, ZAI_FINAL_MODEL the loop fallback, ZAI_MODEL the loop-off
		# single-call model (and the loop fallback's default). New names win.
		if v := os.environ.get("ZAI_FLASH_MODEL"):
			cfg.llm_model = cfg.llm_model or v.strip()
			_deprecated("ZAI_FLASH_MODEL", "BMF_LLM_MODEL")
		if v := os.environ.get("ZAI_FINAL_MODEL"):
			if not os.environ.get("BMF_LLM_FALLBACK_MODEL"):
				cfg.llm_fallback_model = v.strip()
			_deprecated("ZAI_FINAL_MODEL", "BMF_LLM_FALLBACK_MODEL")
		if v := os.environ.get("ZAI_MODEL"):
			# Historically the loop-off single-call model AND the implicit
			# default of the loop fallback — map it to the fallback, so both
			# loop-off and the loop's paid stage keep honouring it.
			if not os.environ.get("BMF_LLM_FALLBACK_MODEL") and not os.environ.get("ZAI_FINAL_MODEL"):
				cfg.llm_fallback_model = v.strip()
			_deprecated("ZAI_MODEL", "BMF_LLM_FALLBACK_MODEL")
		if v := os.environ.get("ZAI_REASONING_EFFORT"):
			cfg.zai_reasoning_effort = v.strip().lower()
		if v := os.environ.get("ZAI_THINKING"):
			cfg.zai_thinking = v.strip().lower()
		if (v := os.environ.get("BMF_LLM_LOOP")) is not None:
			cfg.llm_loop = v.strip().lower() in ("1", "true", "yes", "on")
		if (v := os.environ.get("BMF_LLM_BURST")) is not None:
			try:
				cfg.llm_burst = max(0.0, float(v))
			except ValueError:
				pass
		if (v := os.environ.get("BMF_LLM_MIN_INTERVAL")) is not None:
			try:
				cfg.llm_min_interval = max(0.0, float(v))
			except ValueError:
				pass
		if (v := os.environ.get("BMF_LLM_MAX_INFLIGHT")) is not None:
			try:
				cfg.llm_max_inflight = max(1, int(v))
			except ValueError:
				pass
		if (v := os.environ.get("BMF_LLM_RATE_LIMIT_BASE")) is not None:
			try:
				cfg.llm_rate_limit_base = max(0.0, float(v))
			except ValueError:
				pass
		if (v := os.environ.get("BMF_LLM_RATE_LIMIT_MAX")) is not None:
			try:
				cfg.llm_rate_limit_max = max(0.0, float(v))
			except ValueError:
				pass
		# LLM provider selection + the Antigravity ACP fast tier
		if v := os.environ.get("BMF_LLM_PROVIDER"):
			cfg.llm_provider = v.strip().lower()
		if v := os.environ.get("BMF_ANTIGRAVITY_CMD") or os.environ.get("BMF_ACP_COMMAND"):
			cfg.acp_command = v.strip()
		if (v := os.environ.get("BMF_ANTIGRAVITY_MODEL") or os.environ.get("BMF_ACP_MODEL")) is not None:
			cfg.acp_model = v.strip() or None
		if v := os.environ.get("BMF_ANTIGRAVITY_FALLBACK") or os.environ.get("BMF_ACP_FALLBACK"):
			cfg.acp_fallback_provider = v.strip().lower()
		if (v := os.environ.get("BMF_ANTIGRAVITY_FALLBACK_MODEL") or os.environ.get("BMF_ACP_FALLBACK_MODEL")) is not None:
			cfg.acp_fallback_model = v.strip() or None
		if (v := os.environ.get("BMF_ACP_TIMEOUT")) is not None:
			try:
				cfg.acp_prompt_timeout = max(1.0, float(v))
			except ValueError:
				pass
		if (v := os.environ.get("BMF_ACP_MAX_INFLIGHT")) is not None:
			try:
				cfg.acp_max_inflight = max(1, int(v))
			except ValueError:
				pass
		if (v := os.environ.get("BMF_ACP_MIN_INTERVAL")) is not None:
			try:
				cfg.acp_min_interval = max(0.0, float(v))
			except ValueError:
				pass
		return cfg


# ---------------------------------------------------------------------------
# .env loader (walks up from CWD)
# ---------------------------------------------------------------------------


def load_dotenv_walk_up(*, max_depth: int = 20, override: bool = False) -> Path | None:
	"""Load the first .env file found by walking up from CWD.

	Search order: ./.env, ../.env, ../../.env, ... up to *max_depth* parents.
	The first existing file is parsed (simple KEY=VALUE format, # comments and
	blank lines ignored, optional `export ` prefix, single/double-quoted values
	supported). Values are written into os.environ, but only when the key is
	not already set — unless *override* is True.

	Returns the path of the loaded file, or None if no .env was found.
	"""
	cwd = Path.cwd()
	for depth in range(max_depth + 1):
		# Stop once we've walked past the filesystem root (running from a
		# shallow path like /tmp/x made parents[depth-1] raise IndexError).
		try:
			candidate = cwd.parents[depth - 1] if depth > 0 else cwd
		except IndexError:
			break
		# At depth 0 we look at cwd itself; for depth>0 we look at parents[depth-1]
		# (parents[0] is the immediate parent of cwd).
		env_path = candidate / ".env"
		if env_path.is_file():
			_apply_env_file(env_path, override=override)
			return env_path
	return None


def _apply_env_file(path: Path, *, override: bool) -> None:
	"""Parse a .env file and populate os.environ (without overriding real env)."""
	try:
		text = path.read_text(encoding="utf-8")
	except OSError:
		return
	for raw in text.splitlines():
		line = raw.strip()
		if not line or line.startswith("#"):
			continue
		# Optional `export ` prefix
		if line.startswith("export "):
			line = line[len("export ") :].lstrip()
		if "=" not in line:
			continue
		key, _, value = line.partition("=")
		key = key.strip()
		if not key or not key.replace("_", "").isalnum():
			# Reject malformed keys (avoid injecting garbage)
			continue
		value = value.strip()
		# Strip matching surrounding quotes
		if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
			value = value[1:-1]
		# Expand ${VAR} and $VAR references to already-set env values
		value = _expand_vars(value)
		if override or key not in os.environ:
			os.environ[key] = value


def _expand_vars(value: str) -> str:
	"""Expand $VAR and ${VAR} references against the current environment."""
	return os.path.expandvars(value)
