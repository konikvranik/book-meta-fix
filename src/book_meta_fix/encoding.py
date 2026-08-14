"""Encoding detection and mojibake repair for metadata strings.

Two kinds of corruption appear in the library's metadata files:

1. **Octal-escape mojibake** — a Python repr() of a byte string leaked into
   the JSON value as a literal backslash-escape sequence:
       "\\376\\377\\000K\\000u\\000l\\000h\\000\\341\\000n\\000e\\000k"
   This is UTF-16BE text (`Kulhánek`) with the BOM (0xFEFF) shown as \\376\\377.

2. **Mis-decoded mojibake** — a cp1250/iso-8859-2 byte stream was decoded as
   Latin-1/MacRoman and re-encoded as UTF-8. Classic Czech giveaway: the
   sequence `\\xc2\\xac` (U+00AC ¬) where `Č` was expected (0x8C in cp1250).
       "Cas p\\xc3\\xbdlivu"  ->  "Čas přílivu"   (the *title* here decoded
       Latin-1 then re-encoded; the repair is encode latin-1 -> decode cp1250).

The repair pipeline:
    detect_mojibake(s)  ->  MojibakeKind | None
    repair(s)           ->  repaired str | None  (None = unfixable)
"""
from __future__ import annotations

import codecs
import logging
import re

log = logging.getLogger(__name__)

# Czech/Slovak encodings, in priority order for voting tie-breaks
CZ_SK_ENCODINGS = ("cp1250", "iso-8859-2", "iso-8859-1", "maccentraleurope", "utf-8")


class MojibakeKind:
	OCTAL_ESCAPE = "octal_escape"  # \\376\\377\\000K...
	MISDECODED = "misdecoded"  # \\xc2\\xac... (latin-1 of cp1250)
	NONE = "none"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

# Literal "\\NNN" octal escapes (Python byte repr style)
_OCTAL_ESCAPE_RE = re.compile(r"\\[0-3][0-7]{2}")

# Strong mojibake signals: characters that almost never appear in legitimate
# book metadata but DO appear when cp1250 bytes are misread as latin-1/cp1252/
# cp1251/macroman and re-encoded as UTF-8.
#
# Empirically observed in this library (frequency in ~3100 fields):
#   ¬ (U+00AC)  2x  cp1250 0x8C=È read as latin-1 -> ¬   ("¬as" = "Èas")
#   ¡ (U+00A1)  4x  cp1250 0xA1 read as latin-1           ("pý¡livu")
#   § (U+00A7)  2x  cp1250 0xA7 in wrong context
#   • (U+2022)  2x  cp1250 0x95 (bullet) read as cp1252
#   ¹ (U+00B9)  1x  cp1250 0xB9
#   Ø (U+00D8)  2x  cp1250 read as MacRoman
#   ш н й б м и щ (Cyrillic!)  cp1250 bytes read as Windows-1251
#       È=0xF8=ш, í=0xED=н, é=0xE9=й, á=0xE1=б, ý=0xFD=м
#       "Jiшн Kosek" = "Jiøí Kosek" via cp1250 -> cp1251
#   è ì ù  (U+00E8/EC/F9)  cp1250 high bytes read as iso-8859-1
#       è=0xE8=è in iso-8859-1 but = è in cp1250; "Kamenáè" = "Kamenáè"
#
# Replacement char (U+FFFD) is always a decode-failure marker.
_STRONG_MOJIBAKE = {
	# Latin-1 Supplement chars that result from cp1250 misreads
	"\u00ac",  # ¬  NOT SIGN
	"\u00a1",  # ¡  INVERTED EXCLAMATION (cp1250 0xA1 in latin-1)
	"\u00b9",  # ¹  SUPERSCRIPT ONE (cp1252 leak)
	"\u00d8",  # Ø  LATIN CAPITAL O WITH STROKE (MacRoman misread)
	"\u00f8",  # ø  lowercase version
	"\u2022",  # •  BULLET (cp1252 0x95)
	# Cyrillic range that appears from cp1250 -> Windows-1251 misreads.
	# Legit Cyrillic in a *Czech* book library is extremely rare; if it
	# co-occurs with Latin letters in the same field, it's almost certainly
	# mojibake.
}
# Cyrillic block (U+0400–U+04FF) — flagged only when mixed with Latin text
_CYRILLIC_RANGE = range(0x0400, 0x0500)

# Replacement char
_REPLACEMENT = "\ufffd"


def detect_mojibake(s: str | None) -> str:
	"""Classify the kind of mojibake in *s*, or MojibakeKind.NONE."""
	if not s:
		return MojibakeKind.NONE
	# Octal-escape form: literally contains backslash + 3 octal digits
	if _OCTAL_ESCAPE_RE.search(s):
		return MojibakeKind.OCTAL_ESCAPE
	# Strong single-character signals
	has_strong = any(c in _STRONG_MOJIBAKE for c in s)
	has_replacement = _REPLACEMENT in s
	# Cyrillic mixed with Latin letters in the same string -> cp1250->cp1251
	has_cyrillic = any(ord(c) in _CYRILLIC_RANGE for c in s)
	has_latin = any(c.isascii() and c.isalpha() for c in s)
	cyrillic_latin_mix = has_cyrillic and has_latin
	# Contextual: Latin-1 high chars (è, ì, ù, ç, ...) appearing together with
	# already-correct Czech diacritics (á, é, í, ...) signal partial mojibake
	# (some chars decoded right, others didn't). E.g. "Kamenáè Bill".
	contextual_mojibake = _has_contextual_mojibake(s)
	if has_strong or has_replacement or cyrillic_latin_mix or contextual_mojibake:
		return MojibakeKind.MISDECODED
	return MojibakeKind.NONE


# Latin-1 Supplement chars that are rare in CZ/SK book metadata on their own
# but, when mixed with valid Czech diacritics, signal partial mojibake.
# These are the chars cp1250 high-bytes get misread AS under iso-8859-1/cp1252.
_LATIN1_HIGH = set("àáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ")
# Czech diacritics that, if present, confirm the text is meant to be Czech.
# This is the COMPLETE set of CZ/SK letters — any of these is legitimate
# and never by itself a mojibake signal.
_CZ_PRESENT = set("áčďéěíňóřšťúůýžôäÁČĎÉĚÍŇÓŘŠŤÚŮÝŽÔÄ")


def _has_contextual_mojibake(s: str) -> bool:
	"""True if *s* mixes Latin-1 high chars with valid Czech diacritics.

	Example: "Kamenáè Bill" has both 'á' (correct CZ) and 'è' (cp1250 0xE8=č
	misread as iso-8859-1 'è'). The co-occurrence is a reliable mojibake
	signal, whereas 'è' alone could be a legitimate French/Italian letter.
	"""
	has_cz = any(c in _CZ_PRESENT for c in s)
	has_latin1_high = any(c in _LATIN1_HIGH for c in s)
	# Exclude: if both chars belong to the *same* Czech letter set, no mojibake.
	# E.g. "Kamenické" has only correct CZ chars.
	if has_cz and has_latin1_high:
		# Confirm at least one Latin-1 high char is NOT a valid CZ char
		for c in s:
			if c in _LATIN1_HIGH and c not in _CZ_PRESENT:
				return True
	return False


# ---------------------------------------------------------------------------
# Repair: octal-escape form
# ---------------------------------------------------------------------------


def _repair_octal_escape(s: str) -> str | None:
	"""Repair a string containing literal \\NNN octal escapes (Python byte repr).

	The underlying bytes are usually UTF-16BE (start with 0xFEFF BOM).
	"""
	# Extract all \NNN sequences into bytes; keep printable ASCII as-is.
	bytes_out = bytearray()
	i = 0
	while i < len(s):
		if s[i] == "\\" and i + 3 < len(s) + 1 and s[i + 1 : i + 4].isdigit():
			# Could be octal \NNN
			octal = s[i + 1 : i + 4]
			try:
				bytes_out.append(int(octal, 8))
				i += 4
				continue
			except ValueError:
				pass
		# Regular char -> encode as UTF-8 (the non-escaped parts are ASCII)
		bytes_out.extend(s[i].encode("utf-8"))
		i += 1
	# Try UTF-16 first (BOM-led), then cp1250/iso-8859-2
	for enc in ("utf-16", "utf-16-le", "utf-16-be", "cp1250", "iso-8859-2"):
		try:
			decoded = bytes_out.decode(enc)
			# Sanity: no replacement chars, plausible Czech
			if "\ufffd" not in decoded:
				return decoded
		except (UnicodeDecodeError, UnicodeError):
			continue
	return None


# ---------------------------------------------------------------------------
# Repair: misdecoded form
# ---------------------------------------------------------------------------


def _repair_misdecoded(s: str) -> str | None:
	"""Reverse a misread of cp1250/iso-8859-2 from the wrong source encoding.

	The string was produced by:  cp1250 bytes -> decoded as <wrong enc> ->
	encoded as utf-8.  To reverse, we must recover the original bytes by
	encoding back through <wrong enc>, then decode as cp1250.

	<wrong enc> is usually one of: cp1252, cp1251 (Cyrillic!), MacRoman,
	iso-8859-1. We try each candidate that can represent the string, and for
	each we decode the recovered bytes as cp1250 / iso-8859-2 directly (in
	that order — CZ/SK preference). The charset-detector voting proved
	unreliable on short strings (it picks Arabic/Chinese); we trust the
	known target encodings instead.
	"""
	# Candidate "wrong" intermediate encodings to reverse through.
	# Order matters: cp1252 is the most common culprit (latin-1 superset);
	# cp1251 catches the Cyrillic case (cp1250 bytes 0x80-0xFF misread as
	# Windows-1251, producing ш н й б м и щ).
	reverse_candidates = ("cp1252", "cp1251", "iso-8859-1", "macroman", "maccentraleurope")
	target_candidates = ("cp1250", "iso-8859-2")  # what the bytes REALLY are
	for reverse_enc in reverse_candidates:
		try:
			raw = s.encode(reverse_enc)
		except (UnicodeEncodeError, LookupError):
			continue
		# Now raw are the original cp1250 (or iso-8859-2) bytes. Try the
		# known CZ/SK target encodings directly.
		for target_enc in target_candidates:
			try:
				decoded = raw.decode(target_enc)
			except (UnicodeDecodeError, LookupError):
				continue
			if _looks_clean(decoded) and _looks_czechish(decoded):
				return decoded
	return None


# Czech letter bigrams that should appear in repaired text if it's really Czech.
# If the repaired string contains at least one of these, it's almost certainly
# a successful repair rather than a coincidental decode.
_CZ_LETTERS = set("áčďéěíňóřšťúůýžôÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")


def _looks_czechish(s: str) -> bool:
	"""Heuristic: does *s* plausibly contain Czech/Slovak diacritics after repair?

	For mojibake repair specifically: if the input had high-byte characters,
	a successful repair to cp1250 should introduce Czech diacritics. If the
	output is pure ASCII, the repair probably didn't help (the original was
	already plain ASCII with no diacritics to recover).
	"""
	return any(c in _CZ_LETTERS for c in s)


def _looks_clean(s: str) -> bool:
	"""Heuristic: does *s* look like a successfully repaired Czech string?

	No replacement chars, no leftover strong mojibake signals, no stray
	Cyrillic mixed with Latin.
	"""
	if not s or _REPLACEMENT in s:
		return False
	if any(c in _STRONG_MOJIBAKE for c in s):
		return False
	has_cyr = any(ord(c) in _CYRILLIC_RANGE for c in s)
	has_lat = any(c.isascii() and c.isalpha() for c in s)
	return not (has_cyr and has_lat)


# ---------------------------------------------------------------------------
# Double-decode mojibake (utf-8 bytes read as cp1250/iso-8859-2, then re-saved
# as utf-8). Distinct from MISDECODED above: there a cp1250 *byte stream* was
# misread as latin-1; here the corruption happened to text already encoded as
# UTF-8 (e.g. an EPUB HTML file whose bytes were decoded with the wrong codec
# and written back). The book FILE on disk now holds the double-corrupted
# bytes, so reading it as UTF-8 yields garbage like "VytvĂˇĹ™Ăme".
# ---------------------------------------------------------------------------


# Legit CZ/SK letters that live in Latin Extended-A (U+0100–U+017E). These are
# NOT double-decode artefacts — exclude them from detection.
_CZ_SK_LATIN_EXT = set("čďěľňŕřšťůžÇĎĚĽŇŔŘŠŤŮŽčďěľňŕřšťůž")
# Standalone chars that almost never appear in clean CZ/SK prose but DO appear
# when a UTF-8 byte pair is mis-decoded as a Central-European single-byte codec:
#   ˇ (U+02C7 caron), ˝ (U+02DD double acute) — cp1250/iso-8859-2 trail bytes
#   ™ (U+2122)        — cp1250 0x99 trail byte
#   ­ (U+00AD shy)     — cp1250 0xAD trail byte
#   Â Ã (U+00C2/C3)   — latin-1/cp1252 lead-byte artefacts
_DOUBLE_DECODE_TELLS = {"\u02c7", "\u02dd", "\u2122", "\u00ad", "\u00c2", "\u00c3"}

def _resolvable(names: tuple[str, ...]) -> tuple[str, ...]:
	"""Drop codec names this Python build cannot look up.

	Codec aliases are a minefield: e.g. the Mac Central-European codec
	resolves as ``maccentraleurope``/``mac_latin2``, but the equally
	plausible ``mac-centraleurope`` raises LookupError (there is an
	``encodings/mac_cyrillic.py`` module, yet no ``mac_centraleurope``
	one). An unresolvable candidate must merely be ABSENT from the search
	(a lost repair opportunity, logged), never crash the repair loop —
	until this guard, one bad alias escaped ``_encode_dropping`` (it only
	caught UnicodeEncodeError), tore through the Tk callback and left the
	GUI content preview wedged on "(načítám…)". Validating the tables once
	at import makes every later encode/decode in the repairs total by
	construction and costs nothing per call.
	"""
	resolved = []
	for name in names:
		try:
			codecs.lookup(name)
		except LookupError:
			log.warning("kodek %r není na této platformě dostupný; vyřazen z kandidátů opravy", name)
			continue
		resolved.append(name)
	return tuple(resolved)


# Candidate single-byte codecs that the UTF-8 bytes may have been misread AS.
# Order: Central-European first (the common CZ/SK case), then the western
# fallbacks. The correct one is self-identifying — see repair_double_decode.
_DOUBLE_DECODE_SRCS = _resolvable(("cp1250", "iso-8859-2", "cp1252", "latin-1"))


def detect_double_decode(s: str | None) -> bool:
	"""Heuristic: does *s* look like UTF-8 text that was mis-decoded twice?

	Flags Latin Extended-A chars that are not legitimate CZ/SK letters, and the
	common double-decode artefact chars (ˇ ˝ ™ ­ Â Ã). Deliberately liberal —
	the actual repair (see :func:`repair_double_decode`) is self-validating, so
	a false positive here just means a no-op repair.
	"""
	if not s:
		return False
	for c in s:
		o = ord(c)
		if 0x0100 <= o <= 0x017F and c not in _CZ_SK_LATIN_EXT:
			return True
		if c in _DOUBLE_DECODE_TELLS:
			return True
	return False


def _utf8_seq_len(b: int) -> int:
	"""Expected UTF-8 sequence length for lead byte *b* (0 = not a lead)."""
	if b < 0x80:
		return 1
	if 0xC0 <= b <= 0xDF:
		return 2
	if 0xE0 <= b <= 0xEF:
		return 3
	if 0xF0 <= b <= 0xF7:
		return 4
	return 0


def _mixed_utf8_decode(raw: bytes, fallback: str) -> str:
	"""Decode *raw* as UTF-8 where valid, as *fallback* per byte where not.

	Real-world corruption is often PARTIAL: a book repaired once and then
	partly corrupted again has clean and mojibake segments side by side. A
	whole-string ``encode(src).decode("utf-8")`` fails on the clean segments
	(their single-byte fallback encodings are not valid UTF-8 — e.g. a clean
	``á`` becomes a lone 0xE1), so the repair would refuse exactly the texts
	that need it most. Greedy sequence validation at the byte level handles
	both: valid UTF-8 runs (the mojibake) decode as UTF-8, and every byte that
	cannot start/extend a valid sequence falls back to the single-byte codec
	(the clean text, decoded back to itself).
	"""
	out = []
	i, n = 0, len(raw)
	while i < n:
		b = raw[i]
		length = _utf8_seq_len(b)
		if length > 1:
			chunk = raw[i : i + length]
			if len(chunk) == length and all(0x80 <= c <= 0xBF for c in chunk[1:]):
				try:
					out.append(chunk.decode("utf-8"))
					i += length
					continue
				except UnicodeDecodeError:  # overlong/surrogate — fall through
					pass
		out.append(bytes((b,)).decode(fallback))
		i += 1
	return "".join(out)


def _for_lost_bytes(s: str) -> str:
	"""Map U+FFFD (a byte lost to an earlier ``errors="replace"`` decode) to SUB.

	No codec can re-encode U+FFFD, but one lost byte must not block
	repairing the rest of the text. SUB (0x1A) is the classic "unknown
	byte" sentinel — it encodes everywhere; the repairs map it back to
	U+FFFD so the lost positions stay VISIBLE in the preview instead of
	silently vanishing. Only U+FFFD is touched — a genuinely unencodable
	char (Hiragana in a Central-European codec) still fails the strict
	encode, preserving the self-validation signal for codec selection.
	"""
	return s.replace("\ufffd", "\x1a")


def _encode_dropping(s: str, codec: str, budget: int) -> bytes | None:
	"""Encode *s* through *codec*, dropping up to *budget* unencodable chars.

	The stragglers this removes are UTF-8 lead bytes orphaned by lost bytes
	(e.g. a "Ń" left standing when the continuation byte after it became
	SUB) — they would block the middle encode of a chained repair (see
	:func:`repair_chain`). Beyond the budget the whole encode fails: a
	genuinely wrong codec must not eat the text. Implemented as
	retry-on-first-failure rather than an ``errors=`` handler because
	builtin ``str.encode`` only accepts REGISTERED handler names, and
	per-call registration would leak the global registry.
	"""
	work = s
	dropped = 0
	while True:
		try:
			return work.encode(codec)
		except UnicodeEncodeError as exc:
			span = exc.end - exc.start
			if dropped + span > budget:
				return None
			dropped += span
			work = work[:exc.start] + work[exc.end:]


def repair_double_decode(s: str | None) -> str | None:
	"""Reverse a double-decode of UTF-8 text through a single-byte codec.

	The corruption chain is:  ``proper text --utf-8--> bytes --mis-decoded as
	<codec>--> mojibake str --utf-8--> bytes on disk``. Reading the file as
	UTF-8 yields the mojibake str. To reverse it we encode that str back
	through <codec> (recovering the original UTF-8 bytes) and decode as UTF-8
	— via :func:`_mixed_utf8_decode`, so PARTIALLY corrupted text (clean and
	mojibake segments mixed) repairs too, not just uniformly broken strings.

	The result is a plain ``str`` (valid UTF-8) — exactly the "mark the result
	as UTF-8" semantics a caller wants for display. Returns ``None`` when the
	string shows no double-decode signals or no candidate yields a clean,
	Czech-looking, *different* string (so a clean input is never mangled: its
	bytes all fall back to the single-byte codec and come out unchanged).
	"""
	if not s or not detect_double_decode(s):
		return None
	lost = "\ufffd" in s
	for src in _DOUBLE_DECODE_SRCS:
		try:
			raw = _for_lost_bytes(s).encode(src)
		except (UnicodeEncodeError, LookupError):
			continue
		fixed = _mixed_utf8_decode(raw, src)
		if lost:
			fixed = fixed.replace("\x1a", "\ufffd")
		if fixed != s and _looks_clean(fixed.replace("\ufffd", "")) and _looks_czechish(fixed):
			return fixed
	return None


# Middle-layer codec candidates for a two-layer chain: the wrong codec the
# text was ONCE mis-read through on top of the usual double-decode. Cyrillic
# first — the wild sample was cp1250 CZ text mis-read as cp1251. Cheap to be
# generous: a candidate that cannot encode the intermediate fails in C.
# NOTE: the Mac Central-European codec must be spelled ``mac_latin2`` (alias
# ``maccentraleurope``) — ``mac-centraleurope`` does NOT resolve and is the
# exact alias that once crashed repair_chain with LookupError.
_CHAIN_MIDDLE = _resolvable((
	"cp1251", "iso-8859-5", "koi8-r", "koi8-u", "mac-cyrillic",
	"cp1250", "cp1252", "cp1257", "cp1254", "cp1255", "cp1256", "cp1258",
	"iso-8859-1", "iso-8859-2", "iso-8859-7", "iso-8859-9", "iso-8859-15",
	"iso-8859-16", "mac_latin2", "cp852", "cp850", "cp866", "cp874",
	"latin-1",
))
# What the recovered bytes of the innermost layer actually are: a CZ/SK book.
_CHAIN_FINALS = _resolvable(("cp1250", "iso-8859-2"))


def repair_chain(s: str | None) -> tuple[str, str] | None:
	"""Repair a TWO-LAYER mojibake chain; returns ``(repaired, chain)`` or None.

	Wild case (an old Czech HTML tutorial in the library): cp1250 Czech text
	was mis-decoded as cp1251 (Czech letters became Cyrillic look-alikes —
	í 0xED → н), re-saved as UTF-8, then mis-decoded AGAIN as cp1250
	(н → D0 BD → "Đ˝") and re-saved as UTF-8. One recode layer only reaches
	the Cyrillic middle layer; the second pair below reaches the original
	cp1250 bytes. Structure, outermost inwards::

		encode(a1) → utf-8 → encode(a2) → decode(a3)

	Only runs when the single-layer :func:`repair_double_decode` refuses
	(its best output still does not look clean), so a plain double-decode
	never gets a chain false-positive. U+FFFD lost bytes pass through as
	SUB and stay visible in the result; the orphaned lead chars they leave
	behind are dropped within a bounded budget (``_encode_dropping``).
	"""
	if not s or (not detect_double_decode(s) and "\ufffd" not in s):
		return None
	if repair_double_decode(s) is not None:
		return None
	lost = "\ufffd" in s
	budget = s.count("\ufffd") * 2 + 4
	for a1 in _DOUBLE_DECODE_SRCS:
		try:
			raw = _for_lost_bytes(s).encode(a1)
		except (UnicodeEncodeError, LookupError):
			continue
		mid = _mixed_utf8_decode(raw, a1)
		if mid == s:
			continue
		for a2 in _CHAIN_MIDDLE:
			inner = _encode_dropping(mid, a2, budget)
			if inner is None:
				continue
			for a3 in _CHAIN_FINALS:
				try:
					out = inner.decode(a3)
				except (UnicodeDecodeError, LookupError):
					continue
				if lost:
					out = out.replace("\x1a", "\ufffd")
				if out != s and _looks_clean(out.replace("\ufffd", "")) and _looks_czechish(out):
					return out, f"{a1}→utf-8→{a2}→{a3}"
	return None


def recode(s: str, src: str, dst: str) -> str | None:
	"""Re-interpret *s*: encode through *src*, decode as *dst*.

	The MANUAL counterpart of :func:`repair_double_decode` (which auto-detects
	the pair): lets a human experiment with codec combinations in the GUI
	until the preview reads right. When *dst* is utf-8 the decode falls back
	to :func:`_mixed_utf8_decode`, so partially corrupted text (clean and
	mojibake segments mixed) converts too, and a fully clean input returns
	unchanged. Returns ``None`` when the combination cannot run (a char not
	representable in *src*, or undecodable as *dst*). Lost bytes (U+FFFD
	from an earlier replace-decode) pass through as SUB and come back
	marked as U+FFFD, so one destroyed byte never blocks the repair.
	"""
	if not s:
		return s
	try:
		raw = _for_lost_bytes(s).encode(src)
	except (UnicodeEncodeError, LookupError):
		return None
	if dst.lower().replace("-", "").replace("_", "") == "utf8":
		out = _mixed_utf8_decode(raw, src)
	else:
		try:
			out = raw.decode(dst)
		except (UnicodeDecodeError, LookupError):
			return None
	# Lost bytes (U+FFFD) travelled as SUB; map them back so the preview
	# shows WHERE the unrecoverable positions are instead of hiding them.
	return out.replace("\x1a", "\ufffd") if "\ufffd" in s else out


def recode_failure_reason(s: str, src: str, dst: str) -> str | None:
	"""Why :func:`recode` returns ``None`` for this pair (``None`` = it runs).

	Mirrors recode's control flow, but keeps the exception instead of
	swallowing it, so the GUI can explain a failed codec experiment
	(char not representable in *src* / byte undefined in *dst*) instead of a
	bare "failed". Messages are Czech — this helper exists for the GUI hint.
	"""
	if not s:
		return None
	try:
		raw = _for_lost_bytes(s).encode(src)
	except UnicodeEncodeError as exc:
		ch = s[exc.start] if 0 <= exc.start < len(s) else "?"
		return f"znak „{ch}“ (U+{ord(ch):04X}) nelze zapsat v {src}"
	except LookupError:
		return f"neznámý kodek {src}"
	if dst.lower().replace("-", "").replace("_", "") == "utf8":
		return None  # _mixed_utf8_decode never fails (per-byte fallback)
	try:
		raw.decode(dst)
	except UnicodeDecodeError as exc:
		byte = exc.object[exc.start] if 0 <= exc.start < len(exc.object) else 0
		return f"bajt 0x{byte:02X} na pozici {exc.start} v {dst} není definován"
	except LookupError:
		return f"neznámý kodek {dst}"
	return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def repair(s: str | None) -> tuple[str | None, str]:
	"""Repair a mojibake'd string.

	Returns (repaired_or_None, kind). If repair fails, returns (None, kind)
	so the caller can add the book to the unfixable list.
	"""
	if not s:
		return s, MojibakeKind.NONE
	kind = detect_mojibake(s)
	if kind == MojibakeKind.NONE:
		return s, MojibakeKind.NONE
	if kind == MojibakeKind.OCTAL_ESCAPE:
		fixed = _repair_octal_escape(s)
		return (fixed, kind) if fixed is not None else (None, kind)
	if kind == MojibakeKind.MISDECODED:
		fixed = _repair_misdecoded(s)
		return (fixed, kind) if fixed is not None else (None, kind)
	return None, kind
