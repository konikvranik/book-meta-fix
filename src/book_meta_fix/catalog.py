"""Corruption-catalog help texts — what each diagnosis code means.

The GUI's "Found problems" section renders the diagnosis codes (C1…C20,
MISSING_*, EMPTY_BOOK, …) clickable: clicking one opens a window with the
title and the full description from :data:`CATEGORY_HELP`. The strings are
English msgids wrapped in ``_()`` at module level — the module is imported
LAZILY (on the first click), long after :func:`i18n.init_language` ran, and
babel can only extract literal ``_("…")`` calls, so lookup-time wrapping
would leave the catalog untranslatable.

Content mirrors docs/corruption-catalog.md (and the README table): keep the
two in sync when a rule changes meaning — the doc page is the long form,
this dict is the in-app short form.
"""
from __future__ import annotations

from .i18n import _

# code → (title, full description), localized at import (see module docstring).
CATEGORY_HELP: dict[str, tuple[str, str]] = {
	"C1": (
		_("Author and title are swapped"),
		_("The author field holds the book's title and the title field holds an "
		  "author's name — Calibre wrote the record the wrong way round during "
		  "import. Often the title slot carries another spelling of the real "
		  "author, or the title of a DIFFERENT book by the author in the author "
		  "slot. The fix swaps the two fields; the swap is only trusted after it "
		  "was confirmed against the book's own text (the mined title must "
		  "agree), otherwise it waits in review."),
	),
	"C2": (
		_("Filename used as title"),
		_("The title is the ebook's file name: diacritics lost, underscores for "
		  "spaces, or an artefact like 'Microsoft Word - …'. Calibre imported "
		  "the file before real metadata existed. The real title has to be "
		  "recovered from the book's content or an online source."),
	),
	"C3": (
		_("Series, library or publisher in the author field"),
		_("The author field carries a collection/series name (e.g. 'abeles'), a "
		  "publisher, or another non-person label instead of the author. These "
		  "records usually come from bulk imports where the field was filled "
		  "with whatever was at hand."),
	),
	"C4": (
		_("Encoding corruption (mojibake)"),
		_("The text was decoded through the wrong code page and saved again, so "
		  "the metadata carry unrepairable mojibake (Czech diacritics turned "
		  "into garbage like 'ZAĚTEK VELIKÝ CESTY' or \\376 escapes). Only some "
		  "cases can be repaired (the GUI's recode tool, or the LLM tier); "
		  "otherwise the correct values must come from an online source."),
	),
	"C5": (
		_("Placeholder record"),
		_("The metadata are literal placeholders ('author', 'title', 'subject') "
		  "— Calibre overwrote the real values during a failed import. The book "
		  "file itself often still holds the real title/author, so this is a "
		  "delete proposal only after the content was checked."),
	),
	"C6": (
		_("MS-Word lock-file duplicate"),
		_("The folder holds a '~$…' lock file that Calibre imported as a book — "
		  "a Word temp file, not a real ebook. The record is a duplicate of "
		  "nothing and the file is junk: proposed for deletion."),
	),
	"C7": (
		_("Glued authors"),
		_("Several author names are glued into one string without a separator "
		  "(e.g. 'byKathy SierraandBert Bates'). Needs the correct split — the "
		  "author list must be reconstructed by hand or from an online record."),
	),
	"C8": (
		_("Translator credited as the author"),
		_("The author field holds the translator (a 'přeložil …' credit) instead "
		  "of, or in front of, the real author. A translator AMONG the authors "
		  "is kept on purpose — the library has no translator field and the "
		  "credit keeps the book searchable by translator."),
	),
	"C9": (
		_("Anonym (mostly fake)"),
		_("The author is an 'unknown' spelling ('Neznamy', 'Unknown', …). Most "
		  "of these are corrupted records, not genuine anonymous works — a "
		  "whitelist (Bible, Koran, …) protects the real ones. Every other "
		  "record needs its real author recovered."),
	),
	"C10": (
		_("Long multi-author list (retired)"),
		_("Retired rule (2026-09): books with many authors were flagged as "
		  "suspicious, but real anthologies and textbooks made the signal too "
		  "noisy. The rule no longer fires; old review entries keep the code."),
	),
	"C11": (
		_("Generated placeholder cover"),
		_("The cover.jpg is a Calibre-generated placeholder (a flat gradient "
		  "from title text), detected by pixel analysis. A real cover is "
		  "proposed from the enrichers when one is available; the GUI can also "
		  "recover an embedded cover from inside the book file."),
	),
	"C12": (
		_("Author field pollution (slug/artefact)"),
		_("The author field carries filename-slug junk: a leading '_'/'*' or "
		  "lost capitalization ('anthony burgess') — clearly not a real name. "
		  "Only the author FIELD is judged here; a garbage FOLDER name next to "
		  "clean authors is C13's business (placement regenerates the folder)."),
	),
	"C13": (
		_("Location mismatch"),
		_("The folder no longer matches the path pattern derived from the book's "
		  "own metadata (author/title changed, or the book sits under needfix/). "
		  "Apply's placement moves the folder to the recomputed target; the move "
		  "is pre-filled as accepted."),
	),
	"C14": (
		_("Series order glued into the series name"),
		_("The volume number is part of the series NAME ('Mark Stone #73') "
		  "instead of its index. The rule splits the name losslessly and "
		  "pre-fills the fix; the book's own index half is kept when present."),
	),
	"C15": (
		_("Author-name variants (library-level)"),
		_("The same author is spelled several ways across the library (initials "
		  "vs full names, diacritics, swapped/comma order, titles). Emitted only "
		  "by 'bmf normalize'; whole-list replacements are proposed per book. "
		  "Deterministic clusters pre-fill accept, fuzzy ones wait in review."),
	),
	"C16": (
		_("Genre/tag name variants (library-level)"),
		_("The same genre/tag exists under several spellings ('SciFi', 'sci-fi', "
		  "…). Emitted only by 'bmf normalize'; genres canonicalize to Czech via "
		  "a curated alias table — misspellings become explicit alias rows, "
		  "never fuzzy guesses."),
	),
	"C17": (
		_("Invalid ebook file"),
		_("The file's CONTENT matches no known book format (probed, not just "
		  "the extension — a valid book under a wrong extension is never "
		  "flagged). Emitted only by 'bmf clean --files'; the delete proposal "
		  "names the exact files and apply re-checks every file before unlink."),
	),
	"C18": (
		_("Series-name variants (library-level)"),
		_("The same series is spelled several ways ('Mark Stone' vs 'Mark Stone "
		  "(edice)'). Emitted only by 'bmf normalize'. Close sub-series stay "
		  "apart on purpose: volume numbering, evidenced suspect pairs and "
		  "online checks decide; the volume index itself is never proposed."),
	),
	"C19": (
		_("Duplicate folders of the same work"),
		_("Two folders hold the same book (double import — folded author+title "
		  "match, or the same valid ISBN). Emitted by 'bmf merge'. One folder "
		  "becomes the survivor; the others carry 'action: merge' and apply "
		  "folds them in (files move, metadata field-merge, covers optional)."),
	),
	"C20": (
		_("Language-code normalization"),
		_("The language field holds a variant spelling of a code the library "
		  "already canonicalizes ('cs' vs 'cze' vs 'Czech'). Emitted only by "
		  "'bmf normalize'; the replacement is deterministic and pre-filled."),
	),
	"EMPTY_BOOK": (
		_("Dead record (the book file is gone)"),
		_("The folder holds only metadata sidecars and their backups — no ebook "
		  "file, nothing to repair. The record moves to needfix/empty/; "
		  "metadata are left untouched."),
	),
	"MISSING_ISBN": (
		_("No ISBN"),
		_("The record (and the book's content) carries no ISBN. Not corruption "
		  "by itself: when author+title are confirmed against the content the "
		  "book is acceptable as-is — the flag only feeds the enrichers, which "
		  "try to recover the field from online sources."),
	),
	"MISSING_YEAR": (
		_("No publication year"),
		_("The record carries no publication year. Same policy as MISSING_ISBN: "
		  "identified content is sufficient, the year is never required for "
		  "identity — enrichers still try to fill it."),
	),
	"MISSING_COVER": (
		_("No cover"),
		_("The folder has no usable cover image. Apply tries the enrichers' "
		  "cover_url and then extracts a cover from inside the book file "
		  "(generated placeholders are rejected)."),
	),
	"CONTENT_MISMATCH": (
		_("Content does not match the record"),
		_("The book's actual text disagrees with the metadata identity (title "
		  "or author not found in the content). The record is what needs "
		  "fixing — or the file is the wrong book entirely. Never auto-fixed."),
	),
	"ERROR": (
		_("Processing error"),
		_("bmf itself failed while processing this book (extraction crashed, "
		  "unexpected exception). Nothing was changed; re-running analyze "
		  "retries the book."),
	),
	"OK": (
		_("No problem found"),
		_("The record passed detection. Listed here only because something else "
		  "on the book is pending (a cover, a location note) or the entry "
		  "carries proposals from another pass (normalize/merge)."),
	),
}


def category_help(code: str) -> tuple[str, str] | None:
	"""(title, description) for a diagnosis code, or None when unknown.

	The dict values are already localized (module import happens after
	:func:`i18n.init_language` — see the module docstring), so this is a
	plain lookup.
	"""
	return CATEGORY_HELP.get(code)
