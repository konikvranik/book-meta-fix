"""Tests for LLM JSON parsing tolerance.

GLM models (trained on Python) frequently emit Python literals (None/True/
False) instead of JSON (null/true/false), trailing commas, and truncated
JSON when they hit the token limit. _parse_llm_json should salvage these
rather than wasting 3 retry API calls and giving up.

The hardest failures — unescaped double-quotes inside string values and raw
control characters (newlines) inside strings — are salvaged by the optional
``json-repair`` dependency (the [llm] extra). These tests cover the exact
real-world responses that previously caused parse failures.
"""
from __future__ import annotations

import pytest

from book_meta_fix import llm as llm_mod
from book_meta_fix.llm import _first_json_object, _parse_llm_json, _repair_truncated_json, _sanitize_json


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


# Salvage of unescaped quotes / control chars needs the optional json-repair
# dependency (the [llm] extra). Skip the salvage class gracefully without it so
# the rest of the suite still runs in a minimal install.
_json_repair_required = pytest.mark.skipif(
	llm_mod.json_repair is None, reason="json-repair not installed (install the [llm] extra)"
)


@_json_repair_required
class TestJsonRepairSalvage:
	"""The real-world GLM responses that _sanitize_json cannot fix but
	json-repair salvages: unescaped double-quotes inside string values and raw
	control characters (newlines) inside strings. Each case below is taken from
	an actual run log where it caused a JSONDecodeError and 3 wasted retries."""

	def test_unescaped_quotes_in_reasoning_pán_prstenů(self) -> None:
		# Exact content from the log: "Expecting ',' delimiter" because the
		# reasoning value contains unescaped "PROLOG", "Tři prsteny...", etc.
		content = (
			'{\n'
			'  "title": "Pán prstenů",\n'
			'  "authors": [\n'
			'    "J. R. R. Tolkien"\n'
			'  ],\n'
			'  "language": "eng",\n'
			'  "genres": [\n'
			'    "fantasy"\n'
			'  ],\n'
			'  "confidence": "high",\n'
			'  "reasoning": "First-page text explicitly contains "PROLOG" and '
			'"Tři prsteny pro krále elfů", confirming the title is "Pán prstenů". '
			'The corrupted title "Dvě věže" is clearly incorrect."\n'
			'}'
		)
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "Pán prstenů"
		assert result.authors == ["J. R. R. Tolkien"]
		# reasoning survives as a (non-empty) string despite the inner quotes
		assert isinstance(result.reasoning, str) and result.reasoning

	def test_unescaped_quotes_in_reasoning_mark_stone(self) -> None:
		# Log case: "Expecting ',' delimiter: line 16 column 35" — reasoning
		# quotes the title inline with straight double-quotes.
		content = (
			'{\n'
			'  "title": "Mark Stone 39 - O blo",\n'
			'  "authors": ["J. P. Garen", "Mark Stone"],\n'
			'  "series": "Mark Stone 39",\n'
			'  "series_index": "39",\n'
			'  "year": 2012,\n'
			'  "language": "ces",\n'
			'  "genres": ["sci-fi"],\n'
			'  "confidence": "high",\n'
			'  "reasoning": "First page title "Mark Stone 39 - O blo" matches '
			'filename and file, confirming the correct title and series name."\n'
			'}'
		)
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "Mark Stone 39 - O blo"
		assert result.year == 2012

	def test_unescaped_quotes_in_reasoning_mojibake(self) -> None:
		# Log case: "Expecting ',' delimiter: line 11 column 170" — reasoning
		# quotes the (mojibake) title inline.
		content = (
			'{\n'
			'  "title": "Vytváříme domovskou stránku",\n'
			'  "authors": ["Jiří Kosek"],\n'
			'  "language": "ces",\n'
			'  "genres": ["technická dokumentace"],\n'
			'  "confidence": "medium",\n'
			'  "reasoning": "The title is visible at the top of the first page '
			'as "Vytváříme domovskou stránku", and the author is "Jiří Kosek" '
			'on the same page."\n'
			'}'
		)
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "Vytváříme domovskou stránku"
		assert result.authors == ["Jiří Kosek"]

	def test_control_char_newline_inside_string(self) -> None:
		# Log case: "Invalid control character at: line 13 column 272" — the
		# reasoning value contains a raw newline (literal \n in the bytes)
		# instead of an escaped \\n.
		content = (
			'{\n'
			'  "title": "Okruhliak na obzore",\n'
			'  "authors": ["Isaac Asimov"],\n'
			'  "series": "Robotická séria",\n'
			'  "language": "slk",\n'
			'  "genres": ["sci-fi"],\n'
			'  "confidence": "high",\n'
			'  "reasoning": "First page text contains the exact title '
			'Okruhliak na obzore.\nThe book is clearly the first in the series."\n'
			'}'
		)
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "Okruhliak na obzore"
		assert result.authors == ["Isaac Asimov"]

	def test_combination_unescaped_quotes_and_control_char(self) -> None:
		# Both failure modes at once: inner quotes AND a raw newline.
		content = (
			'{\n'
			'  "title": "X",\n'
			'  "reasoning": "Title "X" found.\nAlso a newline here."\n'
			'}'
		)
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "X"


class TestFirstJsonObject:
	"""Unit checks of the commentary-carving helper."""

	def test_returns_first_balanced_object(self) -> None:
		assert _first_json_object('prose before {"a": 1} prose after') == '{"a": 1}'

	def test_braces_inside_strings_are_ignored(self) -> None:
		content = '{"reasoning": "nested {braces} and } too"} trailing'
		assert _first_json_object(content) == '{"reasoning": "nested {braces} and } too"}'

	def test_escaped_quote_does_not_end_string(self) -> None:
		content = '{"title": "say \\"hi\\""} tail'
		assert _first_json_object(content) == '{"title": "say \\"hi\\""}'

	def test_truncated_object_returns_none(self) -> None:
		# No balanced object — the truncation repair owns this case.
		assert _first_json_object('{"title": "X", "genres": ["sc') is None

	def test_no_object_returns_none(self) -> None:
		assert _first_json_object("just prose, no braces") is None


class TestWrappedJsonExtraction:
	"""Responses that embed the JSON in commentary. Measured 2026-08-29
	(glm-5.3): a valid object, then "Note: ..." prose and a second, fenced
	copy — the whole-content parse died on "Extra data" and the retry budget
	was spent. The first balanced object must be carved out and parsed even
	without the optional json-repair extra (hence the monkeypatch)."""

	def test_object_with_trailing_note_and_fenced_copy(self, monkeypatch, caplog) -> None:
		import logging as _logging

		monkeypatch.setattr(llm_mod, "json_repair", None)
		content = (
			'{"title": "Private Automobile Accident Report", "authors": null, '
			'"genres": null, "confidence": "medium", "reasoning": "This is not a '
			'published book but a blank automobile accident report form; no '
			'author, ISBN, or publisher applies."}\n\n'
			'Note: this appears to be a form/template document rather than a '
			'book. Recommended fields:\n\n'
			'```json\n{\n  "title": "Private Automobile Accident Report",\n'
			'  "language": "eng",\n  "confidence": "medium",\n  "reasoning": '
			'"The first page is a blank insurance accident report form."\n}\n```'
		)
		with caplog.at_level(_logging.WARNING, logger="book_meta_fix.llm"):
			result = _parse_llm_json(content, model="glm-5.3")
		assert result is not None
		assert result.title == "Private Automobile Accident Report"
		# The FIRST object won, not the fenced restatement (which carries
		# language "eng" and would have set it).
		assert result.language is None
		assert "not a published book" in (result.reasoning or "")
		warnings = [r.getMessage() for r in caplog.records if r.levelno >= _logging.WARNING]
		assert any("commentary-wrapped" in w and "model=glm-5.3" in w for w in warnings)

	def test_prose_before_fenced_json(self, monkeypatch) -> None:
		monkeypatch.setattr(llm_mod, "json_repair", None)
		content = 'Here is the corrected metadata:\n```json\n{"title": "X", "year": 2000}\n```'
		result = _parse_llm_json(content)
		assert result is not None
		assert result.title == "X"
		assert result.year == 2000

	def test_clean_single_object_does_not_log_extraction(self, monkeypatch, caplog) -> None:
		# The common case must go through the plain parse, no salvage noise.
		import logging as _logging

		monkeypatch.setattr(llm_mod, "json_repair", None)
		with caplog.at_level(_logging.WARNING, logger="book_meta_fix.llm"):
			result = _parse_llm_json('{"title": "X", "year": 2000}')
		assert result is not None
		assert result.title == "X"
		assert not [r for r in caplog.records if r.levelno >= _logging.WARNING]


@_json_repair_required
class TestJsonRepairListSalvage:
	"""json-repair on "object + prose + object" content returns BOTH objects
	as a list: the first is the model's answer, the second its restated
	"recommended fields" copy. The old isinstance(dict) guard rejected the
	list and threw the salvage away (log 2026-08-29, glm-5.3) — the
	take-first-dict fix must recover it."""

	def test_list_result_yields_first_dict(self, caplog) -> None:
		import logging as _logging

		content = (
			'{"title": "Private Automobile Accident Report", "authors": null}\n\n'
			'Note: this appears to be a form/template document. Recommended fields:\n\n'
			'```json\n{"title": "Private Automobile Accident Report", "language": "eng"}\n```'
		)
		with caplog.at_level(_logging.WARNING, logger="book_meta_fix.llm"):
			result = _parse_llm_json(content, model="glm-5.3")
		assert result is not None
		assert result.title == "Private Automobile Accident Report"
		# The FIRST object won, not the fenced restatement (which carries
		# language "eng" and would have set it).
		assert result.language is None
		warnings = [r.getMessage() for r in caplog.records if r.levelno >= _logging.WARNING]
		assert any("json-repair" in w and "leading object" in w for w in warnings)


class TestRepairWarningNamesModel:
	"""The repair warnings carry the calling model: flash and the paid
	reasoning model damage JSON at different rates, and the log must show
	WHO truncated so the right knob gets turned (max_tokens vs prompt)."""

	def test_truncation_warning_names_model(self, caplog) -> None:
		import logging as _logging

		content = '{"title": "X", "genres": ["sc'  # cut mid-string at the token limit
		with caplog.at_level(_logging.WARNING, logger="book_meta_fix.llm"):
			result = _parse_llm_json(content, model="glm-4.7-flash")
		assert result is not None  # repaired to a parseable object
		warnings = [r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING]
		assert any("truncated" in w and "model=glm-4.7-flash" in w for w in warnings)

	def test_no_model_context_gets_explicit_placeholder(self, caplog) -> None:
		"""Direct callers without model context still yield a parseable
		message (an explicit '?', not a Python None repr)."""
		import logging as _logging

		with caplog.at_level(_logging.WARNING, logger="book_meta_fix.llm"):
			_parse_llm_json('{"genres": ["sc')
		assert any("model=?" in r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING)
