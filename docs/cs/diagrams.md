# UML diagramy

[English](../diagrams.md) | **Čeština**

Mermaid podoby tří toků, na kterých záleží: běh `bmf analyze` (sekvenční),
běh `bmf apply` (sekvenční), rozhodovací strom jedné knihy v analyze
(flowchart) a lifecycle review záznamu (stavový). Prózu — modulovou mapu a
datové toky — najdete v [architecture.md](../architecture.md), filozofii
verifikace a fix cascade v [concepts.md](../concepts.md) (obojí anglicky).

## 1. Sekvenční — `bmf analyze` (hlavní smyčka)

Jeden scan, potom každá ne-OK kniha projde fix cascade ve worker pool;
výsledky se streamují do `review.yaml` podle toho, jak dokončují.

```mermaid
sequenceDiagram
	autonumber
	participant CLI as bmf analyze (cli.py)
	participant LIB as scan_library + SQLite cache
	participant POOL as KnownAuthorPool + series set
	participant PB as _process_book (worker)
	participant VER as verifier.py
	participant ENR as enrichery (online)
	participant LLM as LLM provider
	participant RW as ReviewWriter

	CLI->>LIB: scan stromu knihovny (NFS, fingerprint cache)
	LIB-->>CLI: všechny knihy (BookMeta, uuid doplněno)
	CLI->>POOL: postavit pooly z PLNÉHO scanu
	note over POOL: verified knihy zahrnuty — autor,<br/>jehož knihy jsou zavřené, není o nic méně známý
	CLI->>CLI: přeskočit verified knihy, vyřadit detektorově-OK knihy

	loop každá zbývající kniha (thread pool)
		PB->>PB: detect() C1–C17 (C13 umístění + C1 pool pattern aktivní)
		alt verdict OK a --verify-ok
			PB->>VER: verify() proti obsahu
			VER-->>PB: MISMATCH / strict UNCERTAIN → reklasifikace na NEEDS_REVIEW
		end
		PB->>VER: safe_extract() — text stránek, vložená metadata, ISBN
		PB->>VER: _try_deterministic_fix()
		VER->>ENR: _online_fill() (ukotveno identitou: ISBN nebo autor-filtr)
		alt C1 a titul je známý autor
			PB->>POOL: _try_known_author_swap()
			PB->>VER: confirm_identity() hlídá přestavěnou dvojici
		end
		alt NEEDS_REVIEW, stále neopraveno, LLM zapnuto
			PB->>LLM: reconcile_loop (evidence + první strana)
			LLM->>VER: verify_proposal() u každého pokusu
			PB->>ENR: existence ladder autor/série (jinak llm:low)
		else předchozí záznam už je rozhodnut
			PB->>LLM: LLM přeskočeno (llm_skip_ids)
		end
		alt akceptovatelné MISSING_*, nic nedohledáno
			PB->>VER: acquire_identity() proti obsahu
			alt žádná identita v obsahu
				PB->>POOL: _pool_confirms_author()
				note over POOL: autor ≥ 3 knihy, titul není<br/>autor / série / „Neznámý"
			end
		end
		PB->>RW: submit (meta, diagnóza, verification, enriched)
		RW->>RW: přenést předchozí rozhodnutí podle uuid, předvyplnit action
		RW->>RW: verified brány (čistá projekce / identity tier)
		RW->>RW: streamovat záznam do review.yaml
	end

	opt --normalize tail (po doběhu writeru)
		CLI->>CLI: C15/C16 clustery nad týmž scanem,<br/>merge_normalizations přepíše review.yaml
	end
```

## 2. Sekvenční — `bmf apply`

Spouští se jen rozhodnuté záznamy. Zápis metadat, pak placement (bývalý
`organize`), pak prořez. Nic tady znovu nečte obsah knihy — drahá
verifikace proběhla v analyze.

```mermaid
sequenceDiagram
	autonumber
	participant CLI as bmf apply
	participant RV as review.yaml + .bak
	participant AP as apply_review
	participant W as writers.py
	participant COV as covers.py
	participant MV as mover (placement)
	participant FS as disk knihovny

	CLI->>RV: načíst rozhodnuté záznamy (multi-doc + legacy)
	loop každý rozhodnutý záznam
		alt action accept nebo keep
			AP->>W: _apply_action() — proposed pole na BookMeta
			opt MISSING_COVER / C11 bez náhrady
				AP->>COV: cover_url z enricheru, jinak extrakt ze souboru knihy
			end
			W->>FS: atomický zápis metadata.json + metadata.opf<br/>(verified flag jen do json)
		else action delete (C6)
			AP->>FS: smazací snapshot (tar.gz), pak odstranit složku
		else action delete s proposed.delete_files (C17)
			AP->>FS: překontrolovat každý soubor, mazat jen stále-nevalidní
		end
		opt placement (default, --no-place vynechá)
			AP->>MV: _placement_target(), přepočítané z finálních metadat
			MV->>FS: přesun na pattern cestu / merge stejného díla / "(dup N)"<br/>EMPTY_BOOK → needfix/empty; rozhodnuto (accept/keep) → vždy pattern cesta
		end
	end
	AP->>RV: prořezat aplikované záznamy (keep zůstává, cesty přepsány)
	note over CLI: dokonči bmf abs-rescan, aby se<br/>diskové zápisy dostaly do Audiobookshelf
```

## 3. Rozhodování — jedna kniha analyze

Tierovaná logika `_process_book` + předvyplnění záznamu v `ReviewWriter`.
Nejsilnější důkaz vždy vyhrává; každé selhání nechá knihu v review s
nápovědou, místo aby se hádalo.

```mermaid
flowchart TD
	book([kniha]) --> detect{"detect()<br/>C1–C17"}
	detect -->|"EMPTY_BOOK"| empty["předvyplnit accept<br/>→ needfix/empty/"]
	detect -->|"verdict OK"| vq{"--verify-ok?"}
	vq -->|ne| ok([zůstává OK, bez záznamu])
	vq -->|ano| verify["verify() proti obsahu"]
	verify -->|"MISMATCH / strict UNCERTAIN"| extract
	verify -->|čisté| ok
	detect -->|"NEEDS_REVIEW / AUTO_FIXABLE"| extract["safe_extract()<br/>text stránek + vložená metadata"]
	extract --> det["_try_deterministic_fix()<br/>text_meta + online (ukotveno identitou)"]
	det --> swapq{"C1 a titul je<br/>známý autor?"}
	swapq -->|ano| swap["_try_known_author_swap()<br/>hlídá confirm_identity()"]
	swapq -->|ne| llmq
	swap --> llmq{"NEEDS_REVIEW, stále neopraveno,<br/>LLM zapnuto?"}
	llmq -->|ano| llm["odpověď LLM → verify_proposal()<br/>+ existence ladder autor/série<br/>(selhání → llm:low)"]
	llmq -->|ne| amq
	llm --> amq{"akceptovatelné MISSING_*<br/>a nic nedohledáno?"}
	amq -->|ne| review([záznam zůstává v review<br/>se syrovými nápovědami])
	amq -->|ano| idq{"acquire_identity()<br/>potvrzuje proti obsahu?"}
	idq -->|ano| content["stamp<br/>source = content"]
	idq -->|ne| poolq{"autor ≥ 3 knihy v poolu,<br/>titul není autor/série,<br/>není „Neznámý“?"}
	poolq -->|ano| pool["stamp<br/>source = author-pool"]
	poolq -->|ne| review
	content --> entry
	pool --> entry
	review --> entry["ReviewWriter staví záznam"]
	entry --> pc{"projekce (aplikovat návrh,<br/>re-detekovat) je čistá?"}
	pc -->|ano| born(["narodí se verified: true<br/>→ apply opraví A zavře"])
	pc -->|ne| iv{"identity tier? online / llm:high /<br/>content / author-pool + identity_confirmed<br/>+ agrees + jen benigní leftovers"}
	iv -->|ano| born
	iv -->|ne| action(["action předvyplněno: accept / keep /<br/>delete / pending → lidský review (bmf gui)"])
```

## 4. Stavy — lifecycle review záznamu / knihy

Proč akceptované knihy pořád znovu přibývají a co je doopravdy zavírá.

```mermaid
stateDiagram-v2
	[*] --> Pending: analyze označí knihu
	Pending --> Decided: action nastaveno (uživatel v GUI / předvyplnění)
	Pending --> Pending: re-analyze, nerozhodnuto (návrh znovu)
	Decided --> Carried: re-analyze před apply (přeneseno verbatim, LLM vynecháno)
	Carried --> Decided
	Decided --> Applied: bmf apply (pole + obálka + placement, prořezáno)
	Applied --> Pending: detektor znovu firingne (pole chybí, identita nepotvrzena)
	Decided --> Closed: apply born-verified záznamu
	Applied --> Closed
	Closed --> Pending: analyze --recheck-ok smaže flag
	note right of Closed
		verified knihy příští analyze
		přeskakuje
	end note
```

Stav `Closed` je jediný stabilní: kniha bez něj cyklí analyze → accept →
apply → analyze donekonečna, protože detektor poctivě firingne, dokud pole
chybí. Identity tier (content, author-pool, online, llm:high) jsou to, co
umožňuje analyze předvyplnit `verified` — viz [concepts.md](../concepts.md),
řádek `verified: true`.

## Co zde záměrně není

- **Class diagram datového modelu** (`BookMeta`, `Diagnosis`,
  `EnrichedMeta`, `IdentityResult`, dict review záznamu) — modely jsou malé
  a žijí v docstringech `models.py` / `enrichers.py`; diagram by je
  duplikoval a rozjel by se s nimi.
- **Komponentní / deploymen diagram** (bmf × NFS knihovna × SQLite cache ×
  online API × ABS server × LLM endpointy) — hodí se, až bude operací víc;
  kapitoly o enricherech/ABS v [architecture.md](../architecture.md) to
  dnes pokrývají prózou.
