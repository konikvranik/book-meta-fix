"""Configuration: paths, API keys, tunables.

Resolution order for every setting:
	1. CLI flag (--library, ...)
	2. Environment variable (BMF_LIBRARY, ZAI_API_KEY, ...)
	3. config.toml in the current directory or ~/.config/bmf/config.toml
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
	zai_api_key: str | None = field(default=None)
	zai_base_url: str = "https://api.z.ai/api/paas/v4/"
	zai_model: str = "glm-5.2"

	# Verification thresholds
	verify_fuzzy_strong: float = 0.8  # >= -> VERIFIED
	verify_fuzzy_weak: float = 0.5  # >= -> NEEDS_REVIEW (uncertain)

	@classmethod
	def from_env(cls) -> "Config":
		cfg = cls()
		# Library / paths
		if v := os.environ.get("BMF_LIBRARY"):
			cfg.library = Path(v)
		if v := os.environ.get("BMF_CACHE"):
			cfg.cache_db = Path(v)
		if v := os.environ.get("BMF_REVIEW"):
			cfg.review_file = Path(v)
		# LLM
		if v := os.environ.get("ZAI_API_KEY"):
			cfg.zai_api_key = v
		if v := os.environ.get("ZAI_BASE_URL"):
			cfg.zai_base_url = v
		if v := os.environ.get("ZAI_MODEL"):
			cfg.zai_model = v
		return cfg
