# Antigravity Project Instructions — book-meta-fix

These instructions govern autonomous agent behavior when working within this workspace.

## 1. Autonomy & Tool Permissions Policy

To operate smoothly and avoid unnecessary approval prompts:

- **Strictly prefer built-in read tools**: Always use `view_file`, `grep_search`, `find_by_name`, and `list_dir` for exploring files and code. **NEVER** use `run_command` with bash utilities like `cat`, `ls`, `grep`, `find`, `sed`, or `head` for reading code or searching the filesystem.
- **Strictly prohibit system-level commands**: **NEVER** execute `sudo`, `mount`, `umount`, `systemctl`, `apt`, `dmesg`, or any OS/infrastructure administration commands.
- **Environmental failures**: If an external resource (e.g. an NFS/SMB network share, network API, or external service) is unavailable or unmounted, immediately inform the user with a clear, localized error explanation. Do NOT attempt to configure or mount network shares.
- **Approved commands for `run_command`**: Use `run_command` exclusively for standard project development workflows:
  - Running tests: `.venv/bin/pytest`, `pytest`
  - Code hygiene: `make lint`, `make test`
  - Localization: `make i18n-extract`, `make i18n-compile`
  - Version control status/diff: `git status`, `git diff`, `git log`

## 2. Core Project Conventions

- **Tab indentation**: Use **TABS** (not spaces) in all Python and test files. Match surrounding indentation strictly.
- **Data models**: Use dataclasses from `models.py` (`BookMeta`, `Diagnosis`, `Book`, etc.) rather than ad-hoc dicts.
- **Source of truth**: `metadata.json` is the source of truth; updates to metadata write to both `metadata.json` and `metadata.opf` atomically.
- **Localization**: User-facing strings must use `_()` from `book_meta_fix.i18n`. Source strings (msgids) are English. Czech translations reside in `src/book_meta_fix/locales/cs/LC_MESSAGES/bmf.po`. Always recompile `.mo` via `make i18n-compile` after updating.
- **Bilingual documentation**: English in `README.md` and `docs/`; Czech mirrors in `README.cs.md` and `docs/cs/`. Update both when modifying docs.
- **External AI consultant**: When requested or for complex second opinions, consult Z.AI GLM-5.3 via `ask_glm`.
- **Domain details**: Refer to [AGENTS.md](AGENTS.md) for full architecture, detector rules, verifier logic, and placement policies.
