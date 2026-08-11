"""Tests for LLM JSON parsing tolerance.

GLM models (trained on Python) frequently emit Python literals (None/True/
False) instead of JSON (null/true/false), trailing commas, and truncated
JSON when they hit the token limit. _parse_llm_json should salvage these
rather than wasting 3 retry API calls and giving up.
"""
from __future__ import annotations

from book_meta_fix.llm import _parse_llm_json, _repair_truncated_json, _sanitize_json


class TestSanitizeJson:
	"""Python-literal and trailing-comma fixes."""

	def test_none_becomes_null(self) -> None:
		assert _sanitize_json('{"publisher": None}') == '{"publisher": null}'

	def test_true_false_become_lowercase(self) -> None:
		assert _sanitize_json('{"x": True, "y": False}') == '{"x": true, "y": false}'

	def test_none_inside_string_is_preserved(self) -> None:
		# "None" inside a string value must NOT be rewritten to null.
		out = _sanitize_json('{"title": "None But the Brave"}')
		assert "None But the Brave" in out

	def test_trailing_comma_in_object(self) -> None:
		assert _sanitize_json('{"a": 1,}') == '{"a": 1}'

	def test_trailing_comma_in_array(self) -> None:
		assert _sanitize_json('{"genres": ["x", "y",]}') == '{"genres": ["x", "y"]}'

	def test_multiple_trailing_commas(self) -> None:
		out = _sanitize_json('{"a": [1, 2,], "b": {"c": 3,},}')
		# Both trailing commas removed.
		assert ",}" not in out and ",]" not in out

	def test_real_world_failure_case(self) -> None:
		"""The exact content from the user's log that caused a parse failure."""
		content = '''{
  "title": "GÉOMÉTRIE DES TRAINS - 12",
  "authors": ["TONIO"],
  "translators": [],
  "publisher": None,
  "year": 1998,
  "language": "ces",
  "genres": ["technická dokumentace"]
}'''
		out = _sanitize_json(content)
		# None should be gone, replaced by null.
		assert "None" not in out
		assert "null" in out


class TestRepairTruncatedJson:
	"""Best-effort repair of JSON cut off at the token limit."""

	def test_object_closed_after_value(self) -> None:
		repaired = _repair_truncated_json('{"title": "X"')
		assert repaired is not None
		assert repaired.endswith("}")

	def test_array_closed_mid_string(self) -> None:
		# Truncated inside a string value inside an array. The array should be
		# closed (]) before the outer object (}).
		repaired = _repair_truncated_json('{"genres": ["sc')
		assert repaired is not None
		# Closing order: close the unterminated string, then [, then }.
		assert repaired.endswith(']}')
		# And it must be valid JSON.
		import json
		data = json.loads(repaired)
		assert "genres" in data

	def test_non_object_returns_none(self) -> None:
		# If it doesn't start with {, no repair attempt.
		assert _repair_truncated_json("just some text") is None

	def test_already_complete_unchanged(self) -> None:
		complete = '{"title": "X"}'
		repaired = _repair_truncated_json(complete)
		# No open containers, so suffix is empty — content returned as-is.
		assert repaired == complete


class TestParseLlmJsonTolerance:
	"""End-to-end: _parse_llm_json should salvage LLM mistakes."""

	def test_python_none_parsed_as_null(self) -> None:
		content = '{"title": "X", "publisher": None, "year": 2000}'
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "X"
		assert result.publisher is None
		assert result.year == 2000

	def test_python_true_false_parsed(self) -> None:
		# (Not real fields, but parser should not crash on True/False values.)
		content = '{"title": "X", "confidence": True}'
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "X"

	def test_trailing_comma_parsed(self) -> None:
		content = '{"title": "X", "authors": ["A", "B",]}'
		result = _parse_llm_json(content)
		assert result is not None
		assert result.authors == ["A", "B"]

	def test_truncated_json_repaired(self) -> None:
		# Truncated after the year value — no closing brace.
		content = '{"title": "X", "year": 2000,'
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "X"
		assert result.year == 2000

	def test_truncated_inside_array_repaired(self) -> None:
		# Truncated mid-string inside the genres array.
		content = '{"title": "X", "genres": ["sc'
		result = _parse_llm_json(content)
		# The genres array was cut mid-string; the repaired JSON should parse
		# and the incomplete trailing value is dropped.
		assert result is not None
		assert result.title == "X"

	def test_real_world_failure_case_parses(self) -> None:
		"""The exact content from the user's log that caused a parse failure."""
		content = '''{
  "title": "GÉOMÉTRIE DES TRAINS - 12",
  "authors": ["TONIO"],
  "translators": [],
  "publisher": None,
  "year": 1998,
  "language": "ces",
  "genres": ["technická dokumentace"]
}'''
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "GÉOMÉTRIE DES TRAINS - 12"
		assert result.authors == ["TONIO"]
		assert result.publisher is None
		assert result.year == 1998
		assert result.language == "ces"
		assert result.genres == ["technická dokumentace"]

	def test_markdown_fences_stripped(self) -> None:
		content = '```json\n{"title": "X"}\n```'
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "X"

	def test_completely_garbage_returns_none(self) -> None:
		result = _parse_llm_json("not json at all, just rambling text")
		assert result is None
