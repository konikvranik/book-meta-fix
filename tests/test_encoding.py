"""Unit tests for mojibake detection and repair."""
from __future__ import annotations

import pytest

from book_meta_fix.encoding import (
	MojibakeKind,
	detect_double_decode,
	detect_mojibake,
	recode,
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

	def test_repairs_partial_mojibake(self):
		# A book repaired once, then partly corrupted again: clean segments
		# (WITH diacritics) and mojibake segments coexist. A whole-string
		# round-trip fails on the clean parts (a clean "á" becomes a lone
		# 0xE1) — _mixed_utf8_decode must fix only the broken segments and
		# leave the clean ones byte-identical.
		text = (
			"Kapitola první\n"
			+ _corrupt("Vytváříme si domovskou stránku -- Seznamy & spol.", "cp1250")
			+ "\nText, který už je v pořádku: příliš žluťoučký kůň.\n"
			+ _corrupt("Další rozbitá část ďábelských ód", "cp1250")
			+ "\n"
		)
		assert detect_double_decode(text) is True
		assert repair_double_decode(text) == (
			"Kapitola první\n"
			"Vytváříme si domovskou stránku -- Seznamy & spol.\n"
			"Text, který už je v pořádku: příliš žluťoučký kůň.\n"
			"Další rozbitá část ďábelských ód\n"
		)

	def test_partial_mojibake_within_one_line(self):
		# The originally reported sample shape: clean words and mojibake words
		# mixed in the SAME line ("Vytv<mojibake> si domovskou str<mojibake>nku").
		s = _corrupt("Vytváříme", "cp1250") + " si domovskou " + _corrupt("stránku", "cp1250")
		assert repair_double_decode(s) == "Vytváříme si domovskou stránku"

	def test_detected_but_clean_text_not_rewritten(self):
		# "™" trips detection, but the text is clean: every byte falls back to
		# the single-byte codec unchanged, fixed == s -> no rewrite.
		assert repair_double_decode("Seznamy & spol. ™ 2025") is None


class TestRecode:
	"""The manual z/do codec experiment used by the GUI content preview."""

	def test_double_decode_pair(self):
		moji = _corrupt("Vytváříme si domovskou stránku", "cp1250")
		assert recode(moji, "cp1250", "utf-8") == "Vytváříme si domovskou stránku"

	def test_single_misdecode_reverse_direction(self):
		# cp1250 bytes mis-read as latin-1: fix = encode latin-1, decode cp1250
		good = "Čas přílivu"
		moji = good.encode("cp1250").decode("latin-1")
		assert recode(moji, "latin-1", "cp1250") == good

	def test_partial_text_via_tolerant_utf8_target(self):
		# dst=utf-8 goes through the tolerant byte-level decoder, so a text
		# with clean and mojibake segments mixed converts too.
		text = "Kapitola\n" + _corrupt("Vytváříme", "cp1250") + "\npříliš žluťoučký kůň"
		assert recode(text, "cp1250", "utf-8") == "Kapitola\nVytváříme\npříliš žluťoučký kůň"

	def test_clean_text_unchanged(self):
		assert recode("příliš žluťoučký kůň", "cp1250", "utf-8") == "příliš žluťoučký kůň"

	def test_unrepresentable_char_returns_none(self):
		# Hiragana cannot be encoded through a Central-European single byte codec.
		assert recode("あ", "cp1250", "utf-8") is None

	def test_undecodable_target_returns_none(self):
		# ä -> 0xE4, which is not decodable as ascii.
		assert recode("ä", "cp1250", "ascii") is None

	def test_empty(self):
		assert recode("", "cp1250", "utf-8") == ""
