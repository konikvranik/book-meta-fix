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
	zai_model: str = "glm-5.2"

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
