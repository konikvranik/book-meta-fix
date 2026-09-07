"""Tests for Config.from_env model-variable resolution (LLM loop models)."""
from __future__ import annotations

import pytest

from book_meta_fix.config import Config
from book_meta_fix.llm import resolve_models


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
	for var in (
		"BMF_LLM_MODEL", "BMF_LLM_FALLBACK_MODEL",
		"ZAI_MODEL", "ZAI_FLASH_MODEL", "ZAI_FINAL_MODEL",
	):
		monkeypatch.delenv(var, raising=False)


class TestResolveLoopModels:
	def test_defaults_loop_on(self):
		cfg = Config(llm_loop=True)
		assert resolve_models(cfg) == ("glm-4.7-flash", "glm-5.3")

	def test_defaults_loop_off(self):
		"""Loop off: the single call goes straight to the fallback model."""
		cfg = Config(llm_loop=False)
		assert resolve_models(cfg) == ("glm-5.3", "glm-5.3")

	def test_explicit_models_win(self):
		cfg = Config(llm_loop=True, llm_model="glm-4.5-flash",
		             llm_fallback_model="glm-4.6")
		assert resolve_models(cfg) == ("glm-4.5-flash", "glm-4.6")

	def test_explicit_loop_model_used_when_loop_off(self):
		cfg = Config(llm_loop=False, llm_model="glm-4.5-flash")
		assert resolve_models(cfg) == ("glm-4.5-flash", "glm-5.3")


class TestLegacyEnvAliases:
	def test_legacy_flash_maps_to_loop_model(self, monkeypatch):
		monkeypatch.setenv("ZAI_FLASH_MODEL", "glm-4.5-flash")
		cfg = Config.from_env()
		assert cfg.llm_model == "glm-4.5-flash"

	def test_legacy_final_maps_to_fallback(self, monkeypatch):
		monkeypatch.setenv("ZAI_FINAL_MODEL", "glm-4.6")
		cfg = Config.from_env()
		assert cfg.llm_fallback_model == "glm-4.6"

	def test_legacy_model_maps_to_fallback(self, monkeypatch):
		monkeypatch.setenv("ZAI_MODEL", "glm-4.6")
		cfg = Config.from_env()
		assert cfg.llm_fallback_model == "glm-4.6"

	def test_new_names_win_over_legacy(self, monkeypatch):
		monkeypatch.setenv("ZAI_FINAL_MODEL", "glm-4.6")
		monkeypatch.setenv("BMF_LLM_FALLBACK_MODEL", "glm-5.3")
		cfg = Config.from_env()
		assert cfg.llm_fallback_model == "glm-5.3"


class TestAbsRescanEnv:
	"""BMF_ABS_* knobs for `bmf abs-rescan`."""

	ABS_VARS = ("BMF_ABS_URL", "BMF_ABS_TOKEN", "BMF_ABS_LIBRARY")

	def test_defaults_when_unset(self, monkeypatch):
		for var in self.ABS_VARS:
			monkeypatch.delenv(var, raising=False)
		cfg = Config.from_env()
		assert cfg.abs_url == ""
		assert cfg.abs_token is None
		assert cfg.abs_library == ""

	def test_env_resolution(self, monkeypatch):
		for var in self.ABS_VARS:
			monkeypatch.delenv(var, raising=False)
		monkeypatch.setenv("BMF_ABS_URL", "http://abs.lan:13378")
		monkeypatch.setenv("BMF_ABS_TOKEN", "s3cret")
		monkeypatch.setenv("BMF_ABS_LIBRARY", "Books")
		cfg = Config.from_env()
		assert cfg.abs_url == "http://abs.lan:13378"
		assert cfg.abs_token == "s3cret"
		assert cfg.abs_library == "Books"

	def test_empty_token_strips_to_none(self, monkeypatch):
		monkeypatch.setenv("BMF_ABS_TOKEN", "")
		assert Config.from_env().abs_token is None
