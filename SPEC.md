# session-relay — spec

## About me
I'm Hannes, a developer. I use Claude Code CLI daily on two machines: a macOS
(Apple Silicon) laptop and a Windows PC where Claude Code runs inside WSL2
(Ubuntu). I want to be able to stop a CLI session on one machine and resume
the same conversation on the other.

## What I want the agent to do
An engineering agent that builds and maintains a Claude Code plugin called
`session-relay` in a GitHub repo I own (I'll give you the repo). It works
issue → PR:
- Build the plugin phase by phase from the spec below, one pull request per
  phase.
- Afterwards, take follow-up issues and review comments from me (bugs I hit
  when testing on my real machines, feature requests) and address them in new
  PRs or follow-up commits.
- It runs on demand when I give it a phase or an issue. It doesn't need a
  schedule.

## What every PR must contain
- Working code plus tests, with the tests passing in the sandbox.
- A PR description that says what was built, what was tested in the sandbox
  and how, and a short checklist of manual steps I must run on my Mac and on
  WSL to verify it for real.
- README updates whenever setup or usage changes.

## How to test inside the sandbox
The agent can't reach my machines, so it simulates two of them:
- "Machine A" (macOS-like): HOME=/tmp/mA/Users/alice, repo at
  /tmp/mA/Users/alice/code/X, CLAUDE dir /tmp/mA/Users/alice/.claude
- "Machine B" (WSL-like): HOME=/tmp/mB/home/bob, repo at /tmp/mB/home/bob/src/X
- The same git remote URL in both repo copies.
- An rclone remote pointing at a local folder as the shared backend.
- Fixture transcripts (.jsonl plus a sibling session directory) that contain
  absolute paths under A.
- Push as A, pull as B, then assert: the files land in B's project dir, every
  A-prefix is rewritten to B's, there is no duplicate copy of the session id,
  and the metadata is correct.
- If the Claude Code CLI can be installed in the sandbox, also run
  `claude plugin validate`. If it can't, list that step in the PR as a manual
  check for me.

## Guardrails
- Only push branches and open PRs. Never push to main, and never merge.
- Never commit real credentials. Test keys are generated on the fly.
- Stop and ask me before changing an architecture decision listed in the
  spec.

## Spec

### Background (verify against current Claude Code docs before relying on it)
- Transcripts live at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.
  The encoding replaces non-alphanumerics in the cwd with `-`, and names
  longer than 200 chars are truncated and get a hash appended.
- A sibling `<session-id>/` directory may hold subagent transcripts and
  `tool-results/`. Sync it together with the .jsonl.
- `claude --resume <absolute .jsonl path>` works. `claude --resume <id>`
  searches all projects and fails if more than one other project holds a
  copy, so there must never be duplicates on disk.
- The JSONL format is internal and changes between releases. Don't rewrite
  its schema. Only do text path-prefix substitution.
- Hooks get `session_id`, `transcript_path`, and `cwd` on stdin.
  SessionStart matchers: startup / resume / clear / compact / fork.
  SessionStart can inject `hookSpecificOutput.additionalContext`. Command
  hooks support `async: true`.
- Plugin env: `${CLAUDE_PLUGIN_ROOT}` (code) and `${CLAUDE_PLUGIN_DATA}`
  (persistent state). Use `userConfig` for settings, with `sensitive: true`
  for secrets. Plugin names starting with `claude-` fail validation.

### Architecture decisions (fixed)
- Backend: shell out to `rclone`, so any remote works (R2, B2, S3, WebDAV,
  SFTP, or a local folder). No iCloud or other OS-specific backend.
- Encryption: `age`, encrypting before upload.
- Language: Python 3, standard library only. External binaries: rclone,
  age, git. No nontrivial bash, because macOS ships bash 3.2 and BSD tools.
- Repo identity: the normalized git remote URL (ssh/https variants are
  equal), never the absolute path.
- Local project dir: the SessionStart hook records `dirname(transcript_path)`
  per cwd in `${CLAUDE_PLUGIN_DATA}`. Pulls write there. Don't re-implement
  the encoding.
- Path rewriting: a sidecar metadata JSON stores the source repo root, home,
  OS, and host. On pull, replace those prefixes with the local ones in the
  raw text. Design it so JSON-escaped Windows backslash paths can be added
  later.
- Metadata per session: session_id, remote URL, source repo root, source
  home, OS, hostname, branch, HEAD, updated_at, Claude Code version.
- Locking: a per-session lock (host + timestamp). Refuse a pull or resume
  while the session is active elsewhere, unless `--force` is given.
- Environment notice: on SessionStart(resume) of a session that came from
  another OS or host, inject context such as "migrated from macOS to WSL
  Ubuntu, paths rewritten, macOS-only commands (brew/open/pbcopy) unavailable".
- Uncommitted code: `/handoff` checks git status and offers to commit to a
  `wip/<short-id>` branch and push. `/pickup` checks that branch out.

### Plugin layout
```
session-relay/
  .claude-plugin/plugin.json   # userConfig: rclone_remote, base_path, age_identity_path, age_recipients
  hooks/hooks.json             # SessionStart(startup): record dir + background pull
                               # SessionStart(resume): environment notice
                               # Stop (async): debounced push, at most ~1 per 60s
                               # SessionEnd: final push + release lock
  skills/handoff/SKILL.md
  skills/pickup/SKILL.md       # list this repo's remote sessions, pull, print `/resume <id>`
  scripts/relay.py             # push | pull | list | status | doctor, with --dry-run
  tests/
```

### Safety rules for the plugin itself
Never delete local transcripts. Back up a local file before overwriting it.
Never upload plaintext once encryption is on. Hooks must be fast, log errors
to `${CLAUDE_PLUGIN_DATA}/relay.log`, and always exit 0.

### Phases (one PR each)
1. relay.py push/pull/list with an rclone local remote, path rewriting,
   metadata, the /handoff and /pickup skills, tests. No encryption, no auto
   hooks.
2. age encryption, locking, automatic hooks, environment notice.
3. Git WIP-branch handoff, the `doctor` command, full README for macOS and
   WSL setup.
4. Later: native Windows support.

### Prior art (study it; check licenses before reusing any code)
tawanorg/claude-sync, claude-nomad, clync, claude-hop, Cha1000000/claude-code-sync.
Differentiators: automatic repo matching by git remote (no manual path map),
hook-driven automatic push, the environment notice, and WIP code handoff.

### Final acceptance (I run this myself)
On my Mac: a session in /Users/<me>/code/X, then /handoff. On WSL: the same
repo cloned at /home/<me>/src/X, then /pickup, then /resume <id>. Claude
continues with correct paths.
