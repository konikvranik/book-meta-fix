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

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LIBRARY = Path("/mnt/share_nfs/Shared eBooks")
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

	# API rate limit / timeout
	api_rate_sec: float = DEFAULT_API_RATE_SEC
	api_timeout: float = DEFAULT_API_TIMEOUT

	# LLM (Z.AI)
	# Coding plan users must use /api/coding/paas/v4/ (draws from subscription
	# quota). PaaS / pay-as-you-go users use /api/paas/v4/ (per-token billing).
	# Override via ZAI_BASE_URL if you have a PaaS key.
	zai_api_key: str | None = field(default=None)
	zai_base_url: str = "https://api.z.ai/api/coding/paas/v4/"
	# Default fallback model. GLM-5.2 with reasoning_effort=low was the most
	# consistent on CZ/SK metadata extraction in the model experiment (see
	# scripts/llm_experiment.py + README). Non-reasoning alternatives
	# (glm-4.6/glm-4.5-air/glm-4.5-flash/glm-4.7-flash with thinking disabled)
	# use ~3-4x fewer output tokens but hallucinate more on CZ/SK series and
	# diacritics. Override via ZAI_MODEL.
	zai_model: str = "glm-5.2"
	# Reasoning effort for GLM-5.x models (low|medium|max). Ignored by GLM-4.x
	# (use zai_thinking for those). 'low' keeps quality while cutting ~60% of
	# reasoning tokens vs the default. Override via ZAI_REASONING_EFFORT.
	zai_reasoning_effort: str = "low"
	# Thinking toggle for GLM-4.6/4.5-Air/4.5-Flash/4.7-Flash (enabled|disabled).
	# Only applied when the model is NOT a GLM-5.x (which uses reasoning_effort).
	# 'disabled' turns off chain-of-thought, drastically cutting output tokens.
	# Override via ZAI_THINKING.
	zai_thinking: str = "disabled"
	# Self-correction loop: try the free Flash model first (with verify feedback),
	# then the paid final model as fallback. Override via BMF_LLM_LOOP=0.
	llm_loop: bool = True
	# Flash (first-attempt, free) and final (paid fallback) models for the loop.
	# Override via ZAI_FLASH_MODEL / ZAI_FINAL_MODEL. The single-call path uses
	# zai_model when the loop is disabled.
	zai_flash_model: str = "glm-4.7-flash"
	zai_final_model: str = "glm-5.2"
	# Leaky-bucket burst capacity: how many LLM calls may fire in a short burst
	# before the smoother engages. Default 5; lower (e.g. 1) for stricter rate
	# matching Z.AI's free-tier RPM. Override via BMF_LLM_BURST.
	llm_burst: float = 5.0
	# Minimum seconds between LLM requests (RPM throttle). Z.AI's coding plan
	# applies a dynamic requests-per-minute limit; 429 'Rate limit reached for
	# requests' (code 1302) trips when too many calls land inside a rolling
	# window. A 2.0s floor caps us at ~30 RPM regardless of worker count or API
	# response speed. Lower (e.g. 1.0) on a higher tier; raise (e.g. 4.0) if you
	# still hit 429.
	llm_min_interval: float = 2.0

	# Verification thresholds
	verify_fuzzy_strong: float = 0.8  # >= -> VERIFIED
	verify_fuzzy_weak: float = 0.5  # >= -> NEEDS_REVIEW (uncertain)

	@classmethod
	def from_env(cls) -> "Config":
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
		if (v := os.environ.get("BMF_OPENLIBRARY")) is not None:
			cfg.openlibrary_enabled = v.strip().lower() in ("1", "true", "yes", "on")
		if (v := os.environ.get("BMF_GOOGLE_BOOKS")) is not None:
			cfg.google_books_enabled = v.strip().lower() in ("1", "true", "yes", "on")
		# LLM
		if v := os.environ.get("ZAI_API_KEY"):
			cfg.zai_api_key = v
		if v := os.environ.get("ZAI_BASE_URL"):
			cfg.zai_base_url = v
		if v := os.environ.get("ZAI_MODEL"):
			cfg.zai_model = v
		if v := os.environ.get("ZAI_REASONING_EFFORT"):
			cfg.zai_reasoning_effort = v.strip().lower()
		if v := os.environ.get("ZAI_THINKING"):
			cfg.zai_thinking = v.strip().lower()
		if v := os.environ.get("ZAI_FLASH_MODEL"):
			cfg.zai_flash_model = v.strip()
		if v := os.environ.get("ZAI_FINAL_MODEL"):
			cfg.zai_final_model = v.strip()
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
		candidate = cwd.parents[depth - 1] if depth > 0 else cwd
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
	for lineno, raw in enumerate(text.splitlines(), start=1):
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
