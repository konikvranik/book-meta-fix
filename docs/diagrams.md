# UML Diagrams

**English** | [Čeština](cs/diagrams.md)

Mermaid renderings of the three flows that matter: the `bmf analyze` run
(sequence), the `bmf apply` run (sequence), the per-book decision tree in
analyze (flowchart), and the review-entry lifecycle (state). For the prose
versions see [architecture.md](architecture.md) (module map, data flow) and
[concepts.md](concepts.md) (verification philosophy, fix cascade, review.yaml
format).

## 1. Sequence — `bmf analyze` (the main loop)

One scan, then every non-OK book runs through the fix cascade in a worker
pool; results stream into `review.yaml` as they finish.

```mermaid
sequenceDiagram
	autonumber
	participant CLI as bmf analyze (cli.py)
	participant LIB as scan_library + SQLite cache
	participant POOL as KnownAuthorPool + series set
	participant PB as _process_book (worker)
	participant VER as verifier.py
	participant ENR as enrichers (online)
	participant LLM as LLM provider
	participant RW as ReviewWriter

	CLI->>LIB: scan library tree (NFS, fingerprint cache)
	LIB-->>CLI: all books (BookMeta, uuid ensured)
	CLI->>POOL: build pools from the FULL scan
	note over POOL: verified books included — an author<br/>with all books closed is no less known
	CLI->>CLI: skip verified books, drop detector-OK books

	loop every remaining book (thread pool)
		PB->>PB: detect() C1–C17 (C13 location + C1 pool pattern armed)
		alt verdict OK and --verify-ok
			PB->>VER: verify() against content
			VER-->>PB: MISMATCH / strict UNCERTAIN → reclassify to NEEDS_REVIEW
		end
		PB->>VER: safe_extract() — page text, embedded meta, ISBN
		PB->>VER: _try_deterministic_fix()
		VER->>ENR: _online_fill() (identity-anchored: ISBN or author-filtered)
		alt C1 and title is a known author
			PB->>POOL: _try_known_author_swap()
			PB->>VER: confirm_identity() gates the rebuilt pair
		end
		alt NEEDS_REVIEW, still unfixed, LLM enabled
			PB->>LLM: reconcile_loop (evidence + first page)
			LLM->>VER: verify_proposal() per attempt
			PB->>ENR: author/series existence ladder (else llm:low)
		else prior entry already decided
			PB->>LLM: LLM skipped (llm_skip_ids)
		end
		alt acceptable MISSING_*, nothing recovered
			PB->>VER: acquire_identity() vs content
			alt no content identity
				PB->>POOL: _pool_confirms_author()
				note over POOL: author ≥ 3 books, title not an<br/>author / series / "Neznámý"
			end
		end
		PB->>RW: submit (meta, diagnosis, verification, enriched)
		RW->>RW: carry prior decision by uuid, pre-fill action
		RW->>RW: verified gates (projection clean / identity tiers)
		RW->>RW: stream the entry to review.yaml
	end

	opt --normalize tail (after the writer finishes)
		CLI->>CLI: C15/C16 clusters over the same scan,<br/>merge_normalizations rewrites review.yaml
	end
```

## 2. Sequence — `bmf apply`

Only decided entries are executed. Metadata writes, then placement (the
former `organize`), then pruning. Nothing here re-reads book content — the
expensive verification happened in analyze.

```mermaid
sequenceDiagram
	autonumber
	participant CLI as bmf apply
	participant RV as review.yaml + .bak
	participant AP as apply_review
	participant W as writers.py
	participant COV as covers.py
	participant MV as mover (placement)
	participant FS as library disk

	CLI->>RV: load decided entries (multi-doc + legacy)
	loop every decided entry
		alt action accept or keep
			AP->>W: _apply_action() — proposed fields onto BookMeta
			opt MISSING_COVER / C11 with no replacement yet
				AP->>COV: enricher cover_url, else extract from the book file
			end
			W->>FS: atomic write metadata.json + metadata.opf<br/>(verified flag → json only)
		else action delete (C6)
			AP->>FS: deletion snapshot (tar.gz), then remove folder
		else action delete with proposed.delete_files (C17)
			AP->>FS: re-check each file, delete only still-invalid ones
		end
		opt placement (default, --no-place skips)
			AP->>MV: _placement_target(), recomputed from final metadata
			MV->>FS: move to pattern path / merge same work / "(dup N)"<br/>EMPTY_BOOK → needfix/empty; decided (accept/keep) → always the pattern path
		end
	end
	AP->>RV: prune applied entries (keep retained, paths refreshed)
	note over CLI: finish with bmf abs-rescan to push<br/>the disk writes into Audiobookshelf
```

## 3. Decision — one book through analyze

The tiered logic of `_process_book` + the entry pre-fill in `ReviewWriter`.
Strongest evidence always wins; every failure leaves the book in review with
a hint instead of guessing.

```mermaid
flowchart TD
	book([book]) --> detect{"detect()<br/>C1–C17"}
	detect -->|"EMPTY_BOOK"| empty["pre-fill accept<br/>→ needfix/empty/"]
	detect -->|"verdict OK"| vq{"--verify-ok?"}
	vq -->|no| ok([stays OK, no entry])
	vq -->|yes| verify["verify() against content"]
	verify -->|"MISMATCH / strict UNCERTAIN"| extract
	verify -->|clean| ok
	detect -->|"NEEDS_REVIEW / AUTO_FIXABLE"| extract["safe_extract()<br/>page text + embedded meta"]
	extract --> det["_try_deterministic_fix()<br/>text_meta + online (identity-anchored)"]
	det --> swapq{"C1 and title is<br/>a known author?"}
	swapq -->|yes| swap["_try_known_author_swap()<br/>gated by confirm_identity()"]
	swapq -->|no| llmq
	swap --> llmq{"NEEDS_REVIEW, still unfixed,<br/>LLM enabled?"}
	llmq -->|yes| llm["LLM answer → verify_proposal()<br/>+ author/series existence ladder<br/>(fail → llm:low)"]
	llmq -->|no| amq
	llm --> amq{"acceptable MISSING_*<br/>and nothing recovered?"}
	amq -->|no| review([entry stays for review<br/>with raw hints])
	amq -->|yes| idq{"acquire_identity()<br/>confirms vs content?"}
	idq -->|yes| content["stamp<br/>source = content"]
	idq -->|no| poolq{"author ≥ 3 books in pool,<br/>title not author/series,<br/>not 'Neznámý'?"}
	poolq -->|yes| pool["stamp<br/>source = author-pool"]
	poolq -->|no| review
	content --> entry
	pool --> entry
	review --> entry["ReviewWriter builds the entry"]
	entry --> pc{"projection (apply proposal,<br/>re-detect) detector-clean?"}
	pc -->|yes| born(["born verified: true<br/>→ apply fixes AND closes"])
	pc -->|no| iv{"identity tier? online / llm:high /<br/>content / author-pool + identity_confirmed<br/>+ agrees + benign leftovers only"}
	iv -->|yes| born
	iv -->|no| action(["action pre-filled: accept / keep /<br/>delete / pending → human review (bmf gui)"])
```

## 4. State — the review entry / book lifecycle

Why accepted books keep re-appearing, and what actually closes them.

```mermaid
stateDiagram-v2
	[*] --> Pending: analyze flags the book
	Pending --> Decided: action set (user in GUI / pre-fill)
	Pending --> Pending: re-analyze, undecided (fresh proposal)
	Decided --> Carried: re-analyze before apply (carried verbatim, LLM skipped)
	Carried --> Decided
	Decided --> Applied: bmf apply (fields + cover + placement, pruned)
	Applied --> Pending: detector re-fires (field missing, identity unconfirmed)
	Decided --> Closed: apply of a born-verified entry
	Applied --> Closed
	Closed --> Pending: analyze --recheck-ok clears the flag
	note right of Closed
		verified books are skipped
		by the next analyze
	end note
```

The `Closed` state is the only stable one: a book without it cycles
analyze → accept → apply → analyze forever, because the detector honestly
re-fires while the field is missing. The identity tiers (content,
author-pool, online, llm:high) are what lets analyze pre-fill `verified` —
see [concepts.md](concepts.md), the `verified: true` row.

## What is deliberately not here

- **Class diagram of the data model** (`BookMeta`, `Diagnosis`,
  `EnrichedMeta`, `IdentityResult`, the review-entry dict) — the models are
  small and live in `models.py` / `enrichers.py` docstrings; a diagram would
  duplicate them and drift.
- **Component / deployment diagram** (bmf × NFS library × SQLite cache ×
  online APIs × ABS server × LLM endpoints) — useful if the operational
  picture gets harder to hold in your head; the enricher/ABS chapters of
  [architecture.md](architecture.md) cover it in prose today.
