# Agent Autonomy and Tool Usage Rules

## Tool Usage Constraints
1. **Never use `run_command` for reading or inspecting code or directory structures**:
   - Use `view_file` to read files.
   - Use `grep_search` to search text across files.
   - Use `find_by_name` to search files and directories by pattern.
   - Use `list_dir` to explore directory contents.
   Running shell commands like `ls`, `cat`, `grep`, `find`, `sed` via `run_command` triggers interactive user approval prompts and degrades autonomy.

2. **System Administration Boundary**:
   - Do NOT attempt to run `sudo`, `mount`, `umount`, `systemctl`, `journalctl`, `dmesg`, `apt`, or any OS-level administration commands.
   - If an external resource (network storage, NFS mount, remote service) is unavailable, report it directly to the user with a localized, human-friendly explanation.

3. **Safe Command Whitelist for `run_command`**:
   - `.venv/bin/pytest`, `pytest`
   - `make test`, `make lint`
   - `make i18n-extract`, `make i18n-compile`
   - `git status`, `git diff`, `git log`
