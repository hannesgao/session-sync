---
description: Hand this Claude Code session off to another machine. Uploads the transcript (plus subagent transcripts and tool results) to the session-relay rclone remote so it can be continued with /pickup on the other machine.
disable-model-invocation: true
argument-hint: "[--dry-run]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/relay.py *) Bash(git status *)
---

# Hand off this session

Follow these steps in order. Keep your messages short.

1. Check the code state. Run:

   ```
   git status --short --branch
   ```

   If it fails because this is not a git repository, stop and tell the user that session-relay
   matches sessions to repositories by their git remote URL, so it only works inside a git repo
   that has a remote.

   If there are uncommitted changes, untracked files, or the branch is ahead of its upstream,
   tell the user in one or two lines: this version of session-relay moves only the conversation,
   not code, so they should commit and push before continuing on the other machine. Do not
   commit, stash, or push anything yourself unless the user asks. Then continue with step 2.

2. Upload the session. Run exactly this command (append `--dry-run` only if the user passed it:
   `$ARGUMENTS`):

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/relay.py push --session "${CLAUDE_SESSION_ID}" --remote "${user_config.rclone_remote}" --base-path "${user_config.base_path}" --data-dir "${CLAUDE_PLUGIN_DATA}"
   ```

3. Report the result.
   - On success, tell the user: the session `${CLAUDE_SESSION_ID}` is uploaded; they should exit
     this session now (anything said after the handoff is not uploaded); on the other machine they
     open the same repository in Claude Code and run `/pickup`.
   - On failure, show the `relay: error:` line and the fix it suggests. If the error says no rclone
     remote is configured, tell the user to set the `rclone_remote` option of the session-relay
     plugin (`/plugin` → session-relay → configure) after creating the remote with `rclone config`.
