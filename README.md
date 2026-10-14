# session-sync

`session-relay` — a Claude Code plugin to stop a CLI session on one machine (macOS) and resume the same conversation on another (WSL2 Ubuntu).

Built phase by phase via pull requests; [SPEC.md](SPEC.md) is the source of truth.

**Status: Phase 1** — manual `/handoff` and `/pickup` through any rclone remote, with path rewriting.
Not yet: encryption, locking, automatic hooks, environment notice (Phase 2); WIP-branch code handoff and
`doctor` (Phase 3). Until Phase 2 lands, use a remote you trust: transcripts are uploaded **unencrypted**.

## How it works

- `/handoff` (on the machine you're leaving) uploads the current session's transcript
  `~/.claude/projects/<dir>/<session-id>.jsonl`, its sibling `<session-id>/` directory (subagent
  transcripts, `tool-results/`), and a sidecar `meta.json` to
  `<rclone_remote><base_path>/<repo-key>/<session-id>/`.
- The **repo key** comes from the normalized git remote URL, so `git@github.com:me/X.git` and
  `https://github.com/me/X` are the same repo, wherever it's cloned.
- `/pickup` (on the other machine, inside a Claude Code session in the same repo) lists the repo's
  sessions, downloads one, and rewrites path prefixes in the raw text — source Claude project dir,
  repo root and home → the local ones. The JSONL schema is never touched. Prefixes are the
  *logical* paths Claude Code writes into transcripts (the session's `cwd`, `$PWD`, `$HOME`), not
  symlink-resolved ones (on macOS `/tmp` is really `/private/tmp`); when the resolved path differs
  it is stored in `meta.json` as an extra alias prefix and rewritten too. The files land in the
  project dir of the session running `/pickup` (session-relay never re-derives Claude Code's
  directory encoding), then you type `/resume <id>`.
- Safety: pull never deletes anything; a file it would overwrite is first copied to
  `${CLAUDE_PLUGIN_DATA}/backups/`. Pull refuses if the session id already exists in another
  project dir (that would break `claude --resume <id>`), and refuses to overwrite the running
  session. Errors are logged to `${CLAUDE_PLUGIN_DATA}/relay.log`.

## Requirements (both machines)

- Claude Code CLI (keep versions close on both machines; the transcript format changes between releases)
- Python 3.8+ (`python3`; macOS: the Xcode Command Line Tools one is fine), git, [rclone](https://rclone.org/install/)
  - macOS: `brew install rclone`
  - WSL Ubuntu: `sudo apt install rclone` (or `curl https://rclone.org/install.sh | sudo bash` for a newer one)
- The same rclone remote configured on both machines (`rclone config`), e.g. Cloudflare R2, B2, S3,
  WebDAV or SFTP. Check with `rclone lsd <remote>:`.

## Install

In Claude Code, on each machine:

```
/plugin marketplace add hannesgao/session-sync
/plugin install session-relay@session-sync
```

(On Claude Code 2.1.275+ the one-step form also works: `/plugin install session-relay --marketplace hannesgao/session-sync`.
For a one-off test from a checkout: `claude --plugin-dir /path/to/session-sync`.)

Claude Code then asks for the plugin options (change them later with `/plugin` → session-relay →
configure, or `claude plugin configure session-relay`):

| Option | Example | Meaning |
| --- | --- | --- |
| `rclone_remote` | `r2:` or `r2:my-bucket` | rclone remote, including the colon |
| `base_path` | `session-relay` (default) | folder under the remote |

## Usage

On the machine you're leaving, in the session you want to move:

```
/session-relay:handoff
```

It warns if you have uncommitted or unpushed code (Phase 1 moves only the conversation — commit and
push yourself), uploads the session, and tells you to exit. Messages sent after the handoff are not
uploaded.

On the other machine, start Claude Code in your clone of the same repo (from the same relative
directory you worked in, usually the repo root) and run:

```
/session-relay:pickup            # or: /session-relay:pickup <session-id>
```

Then type the command it prints:

```
/resume <session-id>
```

(The short forms `/handoff` and `/pickup` also work when no other command uses those names.)

### CLI

The skills call `scripts/relay.py`, which you can also run directly (every command takes `--dry-run`):

```
python3 scripts/relay.py push --session <id> --remote r2: [--base-path session-relay]
python3 scripts/relay.py list [--json] --remote r2:
python3 scripts/relay.py pull <id>|--latest --remote r2: (--current-session <running-id> | --project-dir <dir>)
```

Config can also come from `RELAY_RCLONE_REMOTE` / `RELAY_BASE_PATH`; state lives in
`$CLAUDE_PLUGIN_DATA` (or `--data-dir`, default `~/.local/state/session-relay` outside Claude Code).

## Development

```
python3 -m unittest discover -s tests -v
claude plugin validate --strict .
```

The tests simulate two machines (`/tmp/mA/Users/alice/...` as macOS, `/tmp/mB/home/bob/...` as WSL)
sharing an rclone `local` remote, push as A, pull as B, and check rewriting, placement, metadata and
the no-duplicates rule. The same two-machine tests run again with A's home and repo reached
through a symlink (`/tmp/mA -> /tmp/mA-phys`), and with both machines behind symlinks, so the
macOS `/tmp -> /private/tmp` layout is covered on Linux too. They need `git` and `rclone` on PATH;
the test subprocesses set `PYTHONDONTWRITEBYTECODE=1` so no bytecode caches land in the fake homes.

## Prior art

Studied, no code reused (all MIT or MIT-declared): tawanorg/claude-sync, funkadelic/claude-nomad,
Saturate/clync, junetic/claude-hop, Cha1000000/claude-code-sync.
