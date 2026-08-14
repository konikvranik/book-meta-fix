"""Unit tests for mojibake detection and repair."""
from __future__ import annotations

import pytest

from book_meta_fix.encoding import (
	MojibakeKind,
	detect_double_decode,
	detect_mojibake,
	repair,
	repair_double_decode,
)


def _corrupt(text: str, wrong: str) -> str:
	"""Simulate a double-decode: utf-8 bytes mis-decoded as *wrong*."""
	return text.encode("utf-8").decode(wrong)


class TestDetection:
	def test_clean_czech(self):
		assert detect_mojibake("Smrt lorda Edgwarea") == MojibakeKind.NONE

	def test_clean_with_diacritics(self):
		assert detect_mojibake("Kamenické Štěpánov") == MojibakeKind.NONE

	def test_clean_french(self):
		# ç is legitimate in foreign names — not mojibake by itself
		assert detect_mojibake("François Villon") == MojibakeKind.NONE

	def test_clean_polish(self):
		assert detect_mojibake("Pan Wołodyjowski") == MojibakeKind.NONE

	def test_octal_escape(self):
		s = "\\376\\377\\000K\\000u\\000l\\000h\\000\\341\\000n\\000e\\000k"
		assert detect_mojibake(s) == MojibakeKind.OCTAL_ESCAPE

	def test_misdecoded_cyrillic(self):
		# cp1250 -> cp1251 misread produces Cyrillic letters
		assert detect_mojibake("Jiшн Kosek") == MojibakeKind.MISDECODED

	def test_misdecoded_strong_char(self):
		# ¬ (U+00AC) is a strong mojibake signal
		assert detect_mojibake("¬as pý¡livu") == MojibakeKind.MISDECODED

	def test_replacement_char(self):
		assert detect_mojibake("Darko\ufffd je bytost") == MojibakeKind.MISDECODED

	def test_contextual_latin1_high(self):
		# è (U+00E8) mixed with valid CZ diacritics = partial mojibake
		assert detect_mojibake("Kamenáè Bill") == MojibakeKind.MISDECODED

	def test_empty(self):
		assert detect_mojibake("") == MojibakeKind.NONE
		assert detect_mojibake(None) == MojibakeKind.NONE


class TestRepair:
	def test_repair_clean_unchanged(self):
		fixed, kind = repair("Agatha Christie")
		assert fixed == "Agatha Christie"
		assert kind == MojibakeKind.NONE

	def test_repair_octal_utf16(self):
		s = "\\376\\377\\000K\\000u\\000l\\000h\\000\\341\\000n\\000e\\000k"
		fixed, kind = repair(s)
		assert fixed == "Kulhánek"
		assert kind == MojibakeKind.OCTAL_ESCAPE

	def test_repair_cyrillic(self):
		fixed, kind = repair("Jiшн Kosek")
		assert fixed == "Jiří Kosek"
		assert kind == MojibakeKind.MISDECODED

	def test_repair_dynamick(self):
		fixed, kind = repair("Dynamickй HTML: Praktickй ukбzky")
		assert fixed == "Dynamické HTML: Praktické ukázky"
		assert kind == MojibakeKind.MISDECODED

	def test_repair_preserves_french(self):
		# Legitimate foreign text must not be "repaired"
		fixed, kind = repair("François Villon")
		assert fixed == "François Villon"
		assert kind == MojibakeKind.NONE

	def test_repair_preserves_polish(self):
		fixed, kind = repair("Pan Wołodyjowski")
		assert fixed == "Pan Wołodyjowski"

	def test_repair_preserves_japanese_translit(self):
		fixed, _ = repair("Shōgun: A Novel of Japan")
		assert fixed == "Shōgun: A Novel of Japan"

	def test_repair_unrepairable_returns_none(self):
		# Replacement chars can't be repaired (original bytes lost)
		fixed, kind = repair("Darko\ufffd je bytost")
		assert fixed is None
		assert kind == MojibakeKind.MISDECODED

	def test_repair_empty(self):
		assert repair("")[0] == ""
		assert repair(None)[0] is None


class TestDoubleDecodeRepair:
	"""The double-utf8 corruption seen in some book files' extracted text."""

	@pytest.mark.parametrize("wrong", ["cp1250", "iso-8859-2", "cp1252", "latin-1"])
	@pytest.mark.parametrize(
		"clean",
		[
			"Vytváříme si domovskou stránku -- Seznamy & spol.",
			"Další příběh o dědečkovi",
			"Žluťoučký kůň pěl ďábelské ódy",
		],
	)
	def test_detects_and_repairs_each_codec(self, clean, wrong):
		# Some CZ letters can't be encoded/decoded through every codec, so a
		# few (clean, wrong) combos can't be simulated — skip those.
		try:
			mojibake = _corrupt(clean, wrong)
		except (UnicodeEncodeError, UnicodeDecodeError):
			pytest.skip(f"{clean!r} not representable for simulation via {wrong}")
		assert detect_double_decode(mojibake) is True
		assert repair_double_decode(mojibake) == clean

	def test_detects_real_world_sample(self):
		# The exact mojibake the user reported (utf-8 read as cp1250 twice).
		s = "Vytv" + "\u0102\u02c7\u0139\u2122\u0102" + "me si domovskou str" + "\u0102\u02c7" + "nku"
		assert detect_double_decode(s) is True

	def test_clean_czech_not_flagged(self):
		# Legit CZ diacritics in Latin Extended-A (č ď ě ň ř š ť ů ž) must NOT
		# be mistaken for double-decode artefacts.
		assert detect_double_decode("Kamenické Štěpánov") is False
		assert detect_double_decode("Žluťoučký kůň") is False

	def test_clean_inputs_return_none(self):
		assert repair_double_decode("Agatha Christie") is None
		assert repair_double_decode("Vytváříme si domovskou stránku") is None
		assert repair_double_decode("") is None
		assert repair_double_decode(None) is None

	def test_unrepairable_returns_none(self):
		# Replacement chars mean the original UTF-8 bytes are gone for good.
		assert repair_double_decode("Darko\ufffd je bytost") is None
