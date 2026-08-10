"""Unit tests for ISBN extraction and validation."""
from __future__ import annotations

from book_meta_fix.isbn import canonicalize, extract_isbn, extract_isbns


class TestCanonicalize:
	def test_isbn13_plain(self):
		assert canonicalize("9788090131354") == "9788090131354"

	def test_isbn10_plain(self):
		# 8071741302 is a valid ISBN-10
		assert canonicalize("8071741302") == "9788071741305"

	def test_isbn10_with_hyphens(self):
		assert canonicalize("80-85906-45-7") == "9788085906455"

	def test_isbn13_with_hyphens(self):
		assert canonicalize("978-80-903850-5-4") == "9788090385054"

	def test_isbn_with_label(self):
		assert canonicalize("ISBN:9788090131354") == "9788090131354"

	def test_isbn10_with_X_checkdigit(self):
		# 080442957X is a valid ISBN-10 with X check digit
		assert canonicalize("080442957X") is not None
		assert len(canonicalize("080442957X")) == 13

	def test_invalid_too_short(self):
		assert canonicalize("12345") is None

	def test_invalid_bad_checksum(self):
		# Same digits as valid but last digit changed
		assert canonicalize("9788090131355") is None

	def test_empty(self):
		assert canonicalize("") is None
		assert canonicalize(None) is None


class TestExtract:
	def test_extract_single(self):
		text = "ISBN 80-85906-45-7"
		result = extract_isbn(text)
		assert result == "9788085906455"

	def test_extract_with_label(self):
		assert extract_isbn("ISBN: 9788090131354") == "9788090131354"

	def test_extract_isbn13_label(self):
		assert extract_isbn("ISBN-13: 978-80-903850-5-4") == "9788090385054"

	def test_extract_in_sentence(self):
		text = "Vydalo nakladatelství X, ISBN 9780440158677, 2005"
		assert extract_isbn(text) == "9780440158677"

	def test_extract_plain_isbn10(self):
		assert extract_isbn("8071741302") == "9788071741305"

	def test_extract_multiple_dedup(self):
		text = "ISBN 9788090131354 a taky ISBN 9788090131354"
		isbns = extract_isbns(text)
		assert isbns == ["9788090131354"]

	def test_extract_none(self):
		assert extract_isbn("no isbn here") is None
		assert extract_isbn("") is None

	def test_extract_prefers_first(self):
		# When two different ISBNs are present, the first wins
		text = "ISBN 9788090131354 also ISBN 9780440158677"
		isbns = extract_isbns(text)
		assert isbns[0] == "9788090131354"
