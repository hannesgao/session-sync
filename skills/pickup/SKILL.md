---
description: Pick up a Claude Code session that was handed off from another machine for this repository. Lists this repo's sessions on the session-relay rclone remote, pulls one into this machine with paths rewritten, and prints the /resume command.
disable-model-invocation: true
argument-hint: "[session-id] [--dry-run]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/relay.py *)
---

# Pick up a handed-off session

User arguments: `$ARGUMENTS` (an optional session id and/or `--dry-run`).

Follow these steps in order. Keep your messages short.

1. Choose the session.
   - If the arguments contain a session id, use it and go to step 2.
   - Otherwise run:

     ```
     python3 ${CLAUDE_PLUGIN_ROOT}/scripts/relay.py list --json --remote "${user_config.rclone_remote}" --base-path "${user_config.base_path}" --data-dir "${CLAUDE_PLUGIN_DATA}"
     ```

     The output is a JSON array, newest first, with `session_id`, `updated_at`, `hostname`, `os`,
     `branch` and `head` for each session.
     - Empty array: tell the user there is nothing to pick up for this repository (run `/handoff`
       on the other machine first, in a clone with the same git remote) and stop.
     - One session: use it.
     - Several: show them as a short table (id, updated, host, OS, branch) and ask which one to
       pick, suggesting the newest. Wait for the answer.

2. Pull it. Run this command with the chosen id in place of `<id>` (append `--dry-run` only if
   the user passed it):

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/relay.py pull <id> --current-session "${CLAUDE_SESSION_ID}" --remote "${user_config.rclone_remote}" --base-path "${user_config.base_path}" --data-dir "${CLAUDE_PLUGIN_DATA}"
   ```

3. Report the result.
   - On success, relay the `rewrite` and `warning` lines briefly, then end your reply with the
     exact command for the user to type next, on its own line:

     `/resume <id>`

     You cannot run `/resume` yourself; the user has to type it. Mention that
     `claude --resume <path>` (printed by the pull) also works from a shell.
   - If a `warning  code differs` line appeared, tell the user the code checkout differs from the
     source machine and they may want to `git pull` / check out that branch before resuming.
   - On failure, show the `relay: error:` line and the fix it suggests. Never delete or move
     transcripts yourself to work around an error.
