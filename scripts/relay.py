#!/usr/bin/env python3
"""session-relay: move a Claude Code CLI session between machines via rclone.

Phase 1 commands:

    relay.py push --session <id>          upload this session's transcript
    relay.py list                         list this repo's sessions on the remote
    relay.py pull <id> | --latest         download a session into this machine's
                                          project dir, rewriting path prefixes

Every command accepts --dry-run. Standard library only; shells out to rclone
and git. See SPEC.md and README.md.

Design notes
- Repo identity is the normalized git remote URL, never an absolute path.
- The transcript JSONL is treated as opaque text. The only change made on pull
  is a path-prefix substitution (source project dir, repo root and home ->
  local ones) driven by the sidecar meta.json written at push time.
- Prefixes are logical paths (symlinks kept), because that is what Claude
  Code writes into transcripts (e.g. /tmp/... on macOS, not /private/tmp/...).
  When the symlink-resolved path differs it is recorded as an alias prefix.
- Pull never deletes anything locally. Files it would overwrite are first
  copied to <data-dir>/backups/, which is outside ~/.claude/projects so the
  session id never exists twice where `claude --resume <id>` searches.
"""

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

RELAY_VERSION = "0.1.0"
META_NAME = "meta.json"
META_FORMAT = 1
DEFAULT_BASE_PATH = "session-relay"

# Session ids are UUIDs in practice. Be strict: the id becomes part of local
# and remote paths, so it must never contain separators or "..".
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# Characters that make a path prefix match a *different* path when they follow
# (or precede) the prefix, e.g. /Users/alice/code/X vs /Users/alice/code/X2.
_NAME_CHAR = r"[\w-]"


class RelayError(Exception):
    """A user-facing error: printed without a traceback, exit status 1."""


# --------------------------------------------------------------------------
# small helpers


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def eprint(*args):
    print(*args, file=sys.stderr)


def validate_session_id(sid):
    if not sid or not SESSION_ID_RE.match(sid):
        raise RelayError("invalid session id: %r" % (sid,))
    return sid


def clean_option(value):
    """Return a usable config value or None.

    Skill bodies pass userConfig values as "${user_config.KEY}". If the
    option is unset the placeholder may arrive empty or unsubstituted.
    """
    if value is None:
        return None
    value = value.strip()
    if not value or value.startswith("${"):
        return None
    return value


def atomic_write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.relay-tmp-%d" % (path.name, os.getpid()))
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def atomic_write_json(path, obj):
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------
# configuration


class Config(object):
    def __init__(self, args):
        self.remote = (
            clean_option(getattr(args, "remote", None))
            or clean_option(os.environ.get("CLAUDE_PLUGIN_OPTION_RCLONE_REMOTE"))
            or clean_option(os.environ.get("RELAY_RCLONE_REMOTE"))
        )
        base = clean_option(getattr(args, "base_path", None))
        if base is None:
            base = clean_option(os.environ.get("CLAUDE_PLUGIN_OPTION_BASE_PATH"))
        if base is None:
            base = clean_option(os.environ.get("RELAY_BASE_PATH"))
        self.base_path = base if base is not None else DEFAULT_BASE_PATH

        data_dir = clean_option(getattr(args, "data_dir", None)) or clean_option(
            os.environ.get("CLAUDE_PLUGIN_DATA")
        )
        if data_dir:
            self.data_dir = Path(data_dir).expanduser()
        else:
            state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
            self.data_dir = Path(state_home) / "session-relay"

        claude_dir = clean_option(getattr(args, "claude_dir", None)) or clean_option(
            os.environ.get("CLAUDE_CONFIG_DIR")
        )
        self.claude_dir = Path(claude_dir).expanduser() if claude_dir else Path.home() / ".claude"
        self.dry_run = bool(getattr(args, "dry_run", False))

    def require_remote(self):
        if not self.remote:
            raise RelayError(
                "no rclone remote configured. Set the plugin option 'rclone_remote' "
                "(run /plugin, pick session-relay, configure; or "
                "`claude plugin configure session-relay`), or pass --remote NAME:"
            )
        return remote_root(self.remote, self.base_path)

    @property
    def projects_dir(self):
        return self.claude_dir / "projects"


def remote_root(remote, base_path):
    base = (base_path or "").strip()
    if remote.endswith(":"):
        return remote + base.rstrip("/") if base else remote
    root = remote.rstrip("/")
    return root + "/" + base.strip("/") if base.strip("/") else root


def rjoin(root, *parts):
    sep = "" if root.endswith(":") or root.endswith("/") else "/"
    return root + sep + "/".join(p.strip("/") for p in parts)


# --------------------------------------------------------------------------
# logging and local state (in the plugin data dir)


def log(cfg, message):
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg.data_dir / "relay.log", "a") as fh:
            fh.write("%s %s\n" % (iso(utc_now()), message))
    except OSError:
        pass


def load_state(cfg):
    path = cfg.data_dir / "state.json"
    try:
        with open(path) as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {}


def save_state(cfg, state):
    atomic_write_json(cfg.data_dir / "state.json", state)


def record_project_dir(cfg, cwd, project_dir):
    """Remember dirname(transcript_path) for a cwd (the SPEC's mechanism).

    Phase 2's SessionStart hook will call into the same state file.
    """
    state = load_state(cfg)
    dirs = state.setdefault("project_dirs", {})
    dirs[str(cwd)] = str(project_dir)
    save_state(cfg, state)


# --------------------------------------------------------------------------
# environment facts


def detect_os():
    """Return (os_name, os_detail). os_name is macOS, WSL, Linux or Windows."""
    system = platform.system()
    if system == "Darwin":
        return "macOS", "macOS %s" % (platform.mac_ver()[0] or platform.release())
    if system == "Linux":
        detail = "Linux"
        try:
            with open("/etc/os-release") as fh:
                for line in fh:
                    if line.startswith("PRETTY_NAME="):
                        detail = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        release = platform.release().lower()
        if "microsoft" in release or "wsl" in release or os.environ.get("WSL_DISTRO_NAME"):
            return "WSL", detail
        return "Linux", detail
    if system == "Windows":
        return "Windows", "Windows %s" % platform.release()
    return system or "unknown", system or "unknown"


def claude_code_version():
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\d+\.\d+\.\d+[^\s]*", proc.stdout or "")
    return match.group(0) if match else None


# --------------------------------------------------------------------------
# git and repo identity


def run_git(cwd, *args):
    try:
        proc = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
    except OSError:
        raise RelayError("git is not installed or not on PATH")
    return proc


def normalize_remote_url(url):
    """Normalize a git remote URL so ssh/https/scp variants compare equal.

    git@github.com:Owner/Repo.git, https://github.com/owner/repo and
    ssh://git@github.com:22/owner/repo.git all become github.com/owner/repo.
    User info (and thus any embedded credentials) and ports are dropped.
    """
    u = url.strip()
    host = ""
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", u)
    if m:
        scheme, rest = m.group(1).lower(), m.group(2)
        if scheme == "file":
            path = rest
        else:
            authority, _, path = rest.partition("/")
            authority = authority.rsplit("@", 1)[-1]
            if authority.startswith("["):
                host = authority.split("]", 1)[0] + "]"
            else:
                host = authority.split(":", 1)[0]
    else:
        m = re.match(r"^(?:[^@/]+@)?([^:/]+):(.*)$", u)
        if m and not re.match(r"^[A-Za-z]:[\\/]", u):
            host, path = m.group(1), m.group(2)
        else:
            path = u
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.rstrip("/")
    norm = "%s/%s" % (host, path) if host else path
    return norm.lower()


def repo_key_for(normalized_url):
    slug = re.sub(r"[^a-z0-9._-]+", "_", normalized_url).strip("_.")[:80] or "repo"
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:10]
    return "%s-%s" % (slug, digest)


def same_dir(a, b):
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


def logical_cwd(explicit=None):
    """The working directory as the shell / Claude Code sees it, symlinks kept.

    os.getcwd() always returns the symlink-resolved path, so prefer --cwd,
    then $PWD when it names the same directory (shells keep it logical).
    """
    if explicit:
        return Path(os.path.abspath(os.path.expanduser(explicit)))
    physical = os.getcwd()
    pwd = os.environ.get("PWD")
    if pwd and os.path.isabs(pwd) and same_dir(pwd, physical):
        return Path(os.path.normpath(pwd))
    return Path(physical)


def logical_root(candidates, physical_root):
    """The nearest ancestor of a candidate path that is physical_root.

    git rev-parse --show-toplevel resolves symlinks; walking up a logical
    path (a session cwd, $PWD) gives the same directory spelled the way
    transcripts spell it. Falls back to physical_root.
    """
    for cand in candidates:
        if not cand or not os.path.isabs(str(cand)):
            continue
        path = Path(os.path.normpath(str(cand)))
        for anc in [path] + list(path.parents):
            if same_dir(anc, physical_root):
                return anc
    return Path(physical_root)


def resolved_aliases(path):
    """[symlink-resolved form of path] if it differs from path, else []."""
    real = os.path.realpath(str(path))
    return [real] if real != str(path) else []


def transcript_cwd(path):
    """The first "cwd" recorded in a transcript: the session's logical cwd.

    Read-only; the transcript is not modified or re-serialized.
    """
    try:
        with open(str(path), encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                if n >= 200:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                cwd = rec.get("cwd") if isinstance(rec, dict) else None
                if isinstance(cwd, str) and cwd.startswith("/"):
                    return cwd
    except OSError:
        pass
    return None


def repo_info(cwd, remote_name=None, hints=()):
    """Repo identity and root. root is logical (see logical_root); hints are
    extra logical paths inside the repo to try before cwd."""
    proc = run_git(cwd, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise RelayError("%s is not inside a git repository" % cwd)
    toplevel = proc.stdout.strip()
    root = logical_root(list(hints) + [cwd], toplevel)

    names = [n for n in run_git(root, "remote").stdout.split() if n]
    if remote_name:
        if remote_name not in names:
            raise RelayError("git remote %r not found in %s" % (remote_name, root))
        name = remote_name
    elif "origin" in names:
        name = "origin"
    elif names:
        name = names[0]
    else:
        raise RelayError(
            "%s has no git remote. session-relay identifies a repo by its remote URL; "
            "add one with `git remote add origin <url>`." % root
        )
    url = run_git(root, "remote", "get-url", name).stdout.strip()
    if not url:
        raise RelayError("could not read URL of git remote %r" % name)
    normalized = normalize_remote_url(url)

    branch = run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    head = run_git(root, "rev-parse", "HEAD")
    return {
        "root": root,
        "toplevel": toplevel,
        "remote_name": name,
        "remote_url": normalized,
        "repo_key": repo_key_for(normalized),
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "head": head.stdout.strip() if head.returncode == 0 else None,
    }


# --------------------------------------------------------------------------
# rclone


def rclone(args, ok_codes=(0,)):
    exe = shutil.which("rclone")
    if not exe:
        raise RelayError("rclone is not installed or not on PATH (https://rclone.org/install/)")
    proc = subprocess.run([exe] + list(args), capture_output=True, text=True)
    if proc.returncode not in ok_codes:
        raise RelayError("rclone %s failed (exit %d): %s" % (args[0], proc.returncode, proc.stderr.strip()))
    return proc


# rclone exit code 3 = "directory not found"
RCLONE_DIR_NOT_FOUND = 3


# --------------------------------------------------------------------------
# transcripts


def find_transcripts(cfg, sid):
    """All <projects>/<dir>/<sid>.jsonl on this machine (no encoding logic)."""
    if not cfg.projects_dir.is_dir():
        return []
    return sorted(p for p in cfg.projects_dir.glob("*/%s.jsonl" % sid) if p.is_file())


def find_session_dirs(cfg, sid):
    if not cfg.projects_dir.is_dir():
        return []
    return sorted(p for p in cfg.projects_dir.glob("*/%s" % sid) if p.is_dir())


def session_files(project_dir, sid):
    """Relative paths (posix strings) of the transcript and its sibling dir."""
    files = []
    if (project_dir / ("%s.jsonl" % sid)).is_file():
        files.append("%s.jsonl" % sid)
    sibling = project_dir / sid
    if sibling.is_dir():
        for path in sorted(sibling.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files.append(path.relative_to(project_dir).as_posix())
    return files


# --------------------------------------------------------------------------
# path-prefix rewriting


def path_variants(path, style):
    """How a path prefix can appear in raw transcript text.

    For POSIX paths the raw path is also its JSON-escaped form. A future
    "windows" style would return the JSON-escaped form ("C:\\\\Users\\\\x")
    as well; callers already handle a list of variants per prefix.
    """
    if style == "posix":
        return [path]
    raise RelayError("path style %r is not supported yet (POSIX only in this version)" % style)


def _check_prefix(path, what):
    if not path.startswith("/"):
        raise RelayError("%s is not an absolute POSIX path: %r" % (what, path))
    if any(c in path for c in '"\\') or any(ord(c) < 0x20 for c in path):
        raise RelayError("%s contains characters that would need JSON escaping: %r" % (what, path))


def build_rewrite_pairs(meta, local):
    """(source_prefix, local_prefix, label) for every prefix that differs.

    Each source prefix is the logical path plus optional aliases (its
    symlink-resolved form, meta "<key>_aliases"); all map to the local
    logical prefix.
    """
    pairs = []
    seen = set()
    for label, src_key, dst in (
        ("project dir", "source_project_dir", local["project_dir"]),
        ("repo root", "source_repo_root", local["repo_root"]),
        ("home", "source_home", local["home"]),
    ):
        primary = meta.get(src_key)
        if not primary or not dst:
            continue
        aliases = meta.get(src_key + "_aliases") or []
        if not isinstance(aliases, list):
            aliases = []
        dst = str(dst).rstrip("/") or "/"
        _check_prefix(dst, "local %s" % label)
        for i, src in enumerate([primary] + aliases):
            src_label = label if i == 0 else "%s alias" % label
            src = str(src).rstrip("/") or "/"
            if src == "/" or src in seen:
                continue
            _check_prefix(src, "source %s" % src_label)
            seen.add(src)
            if src != dst:
                pairs.append((src, dst, src_label))
    return pairs


class Rewriter(object):
    """Single-pass, longest-prefix-first, boundary-aware text substitution."""

    def __init__(self, pairs, source_style="posix", local_style="posix"):
        self.mapping = {}
        self.labels = {}
        for src, dst, label in pairs:
            for s, d in zip(path_variants(src, source_style), path_variants(dst, local_style)):
                if s not in self.mapping:
                    self.mapping[s] = d
                    self.labels[s] = label
        self.counts = dict((label, 0) for _, _, label in pairs)
        if self.mapping:
            alternation = "|".join(re.escape(s) for s in sorted(self.mapping, key=len, reverse=True))
            # A prefix starts at a non-name char, or right after a JSON
            # escape (\n, \r, \t): in raw JSONL a path at the start of a
            # line inside a multi-line string follows the letter of "\n".
            self.regex = re.compile(
                r"(?:(?<![\w.-])|(?<=\\[nrt]))(?:%s)(?!%s|\.%s)" % (alternation, _NAME_CHAR, _NAME_CHAR)
            )
        else:
            self.regex = None

    def _sub(self, match):
        found = match.group(0)
        self.counts[self.labels[found]] += 1
        return self.mapping[found]

    def rewrite_text(self, text):
        if self.regex is None:
            return text
        return self.regex.sub(self._sub, text)

    def rewrite_file(self, path):
        """Rewrite a file in place if it is UTF-8 text. Returns True if changed."""
        data = path.read_bytes()
        if b"\0" in data:
            return False
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        new = self.rewrite_text(text)
        if new == text:
            return False
        atomic_write_bytes(path, new.encode("utf-8"))
        return True


# --------------------------------------------------------------------------
# push


def resolve_source_transcript(cfg, sid, explicit, cwd):
    if explicit:
        path = Path(os.path.abspath(os.path.expanduser(explicit)))
        if not path.is_file():
            raise RelayError("transcript not found: %s" % path)
        if path.name != "%s.jsonl" % sid:
            raise RelayError("transcript file name %s does not match session id %s" % (path.name, sid))
        return path
    hits = find_transcripts(cfg, sid)
    if not hits:
        raise RelayError(
            "no transcript for session %s under %s. Has the session written any messages yet?"
            % (sid, cfg.projects_dir)
        )
    if len(hits) > 1:
        recorded = load_state(cfg).get("project_dirs", {}).get(str(cwd))
        chosen = [h for h in hits if recorded and str(h.parent) == recorded]
        if len(chosen) == 1:
            return chosen[0]
        raise RelayError(
            "session %s exists in more than one project dir (%s); pass --transcript to choose"
            % (sid, ", ".join(str(h.parent) for h in hits))
        )
    return hits[0]


def cmd_push(args):
    cfg = Config(args)
    root = cfg.require_remote()
    sid = validate_session_id(args.session)
    cwd = logical_cwd(args.cwd)
    repo = repo_info(cwd, args.git_remote)
    transcript = resolve_source_transcript(cfg, sid, args.transcript, cwd)
    # The transcript's own cwd is how Claude Code spells paths in it.
    repo["root"] = logical_root([transcript_cwd(transcript), cwd], repo["toplevel"])
    project_dir = transcript.parent
    home = Path.home()
    files = session_files(project_dir, sid)
    os_name, os_detail = detect_os()

    meta = {
        "format": META_FORMAT,
        "session_id": sid,
        "remote_url": repo["remote_url"],
        "repo_key": repo["repo_key"],
        "source_repo_root": str(repo["root"]),
        "source_repo_root_aliases": resolved_aliases(repo["root"]),
        "source_cwd": str(cwd),
        "source_home": str(home),
        "source_home_aliases": resolved_aliases(home),
        "source_project_dir": str(project_dir),
        "source_project_dir_aliases": resolved_aliases(project_dir),
        "os": os_name,
        "os_detail": os_detail,
        "path_style": "posix" if os_name != "Windows" else "windows",
        "hostname": socket.gethostname(),
        "branch": repo["branch"],
        "head": repo["head"],
        "updated_at": iso(utc_now()),
        "claude_code_version": claude_code_version(),
        "relay_version": RELAY_VERSION,
        "files": files,
    }
    dest = rjoin(root, repo["repo_key"], sid)

    print("session  %s" % sid)
    print("repo     %s  (%s)" % (repo["remote_url"], repo["root"]))
    print("from     %s" % project_dir)
    print("to       %s" % dest)
    print("files    %d (%s)" % (len(files), ", ".join(files[:5]) + (", ..." if len(files) > 5 else "")))
    if cfg.dry_run:
        print("[dry-run] nothing uploaded")
        return 0

    with tempfile.TemporaryDirectory(prefix="relay-push-") as tmp:
        stage = Path(tmp) / "stage"
        for rel in files:
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(project_dir / rel), str(target))
        rclone(["copy", str(stage), dest])
        # meta.json goes last so `list` never shows a half-uploaded session.
        meta_path = Path(tmp) / META_NAME
        atomic_write_json(meta_path, meta)
        rclone(["copyto", str(meta_path), rjoin(dest, META_NAME)])

    record_project_dir(cfg, cwd, project_dir)
    log(cfg, "push %s -> %s (%d files)" % (sid, dest, len(files)))
    print("pushed. On the other machine, open this repo in Claude Code and run /pickup.")
    print("Note: messages after this point are not uploaded; exit this session now.")
    return 0


# --------------------------------------------------------------------------
# list


def fetch_metas(cfg, root, repo_key):
    src = rjoin(root, repo_key)
    with tempfile.TemporaryDirectory(prefix="relay-list-") as tmp:
        proc = rclone(["copy", src, tmp, "--include", "/*/%s" % META_NAME], ok_codes=(0, RCLONE_DIR_NOT_FOUND))
        if proc.returncode == RCLONE_DIR_NOT_FOUND:
            return []
        metas = []
        for path in sorted(Path(tmp).glob("*/%s" % META_NAME)):
            try:
                with open(path) as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get("session_id") == path.parent.name:
                metas.append(meta)
    metas.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
    return metas


def cmd_list(args):
    cfg = Config(args)
    root = cfg.require_remote()
    cwd = logical_cwd(args.cwd)
    repo = repo_info(cwd, args.git_remote)
    metas = fetch_metas(cfg, root, repo["repo_key"])
    for meta in metas:
        meta["local_copy"] = bool(SESSION_ID_RE.match(meta["session_id"]) and find_transcripts(cfg, meta["session_id"]))
    if args.json:
        print(json.dumps(metas, indent=2, sort_keys=True))
        return 0
    if not metas:
        print("no sessions on %s for %s" % (rjoin(root, repo["repo_key"]), repo["remote_url"]))
        return 0
    print("sessions for %s (newest first):" % repo["remote_url"])
    fmt = "%-36s  %-20s  %-16s  %-6s  %-20s  %s"
    print(fmt % ("SESSION", "UPDATED (UTC)", "HOST", "OS", "BRANCH", "LOCAL"))
    for m in metas:
        print(fmt % (
            m.get("session_id"), m.get("updated_at") or "?", (m.get("hostname") or "?")[:16],
            m.get("os") or "?", (m.get("branch") or "?")[:20], "yes" if m["local_copy"] else "-",
        ))
    return 0


# --------------------------------------------------------------------------
# pull


def resolve_local_project_dir(cfg, args, cwd):
    if args.project_dir:
        return Path(os.path.abspath(os.path.expanduser(args.project_dir))), "--project-dir"
    if args.current_session:
        current = validate_session_id(args.current_session)
        hits = find_transcripts(cfg, current)
        if len(hits) == 1:
            return hits[0].parent, "current session %s" % current
        if len(hits) > 1:
            raise RelayError("current session %s exists in several project dirs" % current)
    dirs = load_state(cfg).get("project_dirs", {})
    recorded = dirs.get(str(cwd)) or dirs.get(os.path.realpath(str(cwd)))
    if recorded:
        return Path(recorded), "recorded for %s" % cwd
    raise RelayError(
        "cannot tell which ~/.claude/projects/<dir> belongs to %s. Run /pickup from a "
        "Claude Code session in this repo (it passes --current-session), or pass "
        "--project-dir. session-relay never re-implements Claude Code's dir encoding." % cwd
    )


def cmd_pull(args):
    cfg = Config(args)
    root = cfg.require_remote()
    cwd = logical_cwd(args.cwd)
    hints = []
    if args.current_session:
        hints = [transcript_cwd(p) for p in find_transcripts(cfg, validate_session_id(args.current_session))]
    repo = repo_info(cwd, args.git_remote, hints)

    if args.session_id:
        sid = validate_session_id(args.session_id)
    elif args.latest:
        metas = fetch_metas(cfg, root, repo["repo_key"])
        if not metas:
            raise RelayError("no sessions on the remote for %s" % repo["remote_url"])
        sid = validate_session_id(metas[0]["session_id"])
    else:
        raise RelayError("give a session id or --latest (see `relay.py list`)")

    if args.current_session and args.current_session == sid:
        raise RelayError("refusing to overwrite the transcript of the session that is running this command")

    target_dir, why = resolve_local_project_dir(cfg, args, cwd)

    # The no-duplicates rule: `claude --resume <id>` fails if another project
    # dir holds the same id, so refuse instead of creating a second copy.
    elsewhere = [p for p in find_transcripts(cfg, sid) if p.parent.resolve() != target_dir.resolve()]
    elsewhere += [p for p in find_session_dirs(cfg, sid) if p.parent.resolve() != target_dir.resolve()]
    if elsewhere:
        raise RelayError(
            "session %s already exists in another project dir: %s. Move or remove that copy "
            "first; session-relay never deletes local transcripts." % (sid, ", ".join(str(p) for p in elsewhere))
        )

    src = rjoin(root, repo["repo_key"], sid)
    with tempfile.TemporaryDirectory(prefix="relay-pull-") as tmp:
        stage = Path(tmp) / "stage"
        proc = rclone(["copy", src, str(stage)], ok_codes=(0, RCLONE_DIR_NOT_FOUND))
        meta_path = stage / META_NAME
        if proc.returncode == RCLONE_DIR_NOT_FOUND or not meta_path.is_file():
            raise RelayError("session %s not found on %s" % (sid, rjoin(root, repo["repo_key"])))
        with open(meta_path) as fh:
            meta = json.load(fh)
        meta_path.unlink()
        if meta.get("session_id") != sid or meta.get("repo_key") != repo["repo_key"]:
            raise RelayError("remote metadata does not match session %s / this repo" % sid)
        if not (stage / ("%s.jsonl" % sid)).is_file():
            raise RelayError("remote copy of %s has no transcript" % sid)

        local = {"project_dir": str(target_dir), "repo_root": str(repo["root"]), "home": str(Path.home())}
        pairs = build_rewrite_pairs(meta, local)
        rewriter = Rewriter(pairs, meta.get("path_style", "posix"), "posix")

        rel_files = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
        for rel in rel_files:
            if not (rel == "%s.jsonl" % sid or rel.startswith(sid + "/")):
                raise RelayError("unexpected file in remote session: %s" % rel)
        changed = [rel for rel in rel_files if rewriter.rewrite_file(stage / rel)]
        existing = [rel for rel in rel_files if (target_dir / rel).exists()]

        print("session  %s  (from %s, %s, pushed %s)" % (
            sid, meta.get("hostname"), meta.get("os_detail") or meta.get("os"), meta.get("updated_at")))
        print("into     %s  [%s]" % (target_dir, why))
        for src_prefix, dst_prefix, label in pairs:
            print("rewrite  %-11s %s -> %s  (%d)" % (label, src_prefix, dst_prefix, rewriter.counts[label]))
        if not pairs:
            print("rewrite  none needed (same paths on both machines)")
        print("files    %d, %d rewritten, %d existing locally (backed up first)" % (
            len(rel_files), len(changed), len(existing)))
        if repo["head"] and meta.get("head") and repo["head"] != meta.get("head"):
            print("warning  code differs: source was on %s @ %s, this checkout is %s @ %s" % (
                meta.get("branch"), (meta.get("head") or "")[:12], repo["branch"], repo["head"][:12]))
        if cfg.dry_run:
            print("[dry-run] nothing written")
            return 0

        backup_dir = None
        if existing:
            backup_dir = cfg.data_dir / "backups" / ("%s-%s" % (utc_now().strftime("%Y%m%dT%H%M%SZ"), sid))
            for rel in existing:
                dst = backup_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(target_dir / rel), str(dst))
        for rel in rel_files:
            atomic_write_bytes(target_dir / rel, (stage / rel).read_bytes())

    state = load_state(cfg)
    state.setdefault("project_dirs", {})[str(cwd)] = str(target_dir)
    state.setdefault("pulled", {})[sid] = {
        "pulled_at": iso(utc_now()), "from_host": meta.get("hostname"), "from_os": meta.get("os"),
        "remote_updated_at": meta.get("updated_at"), "project_dir": str(target_dir),
    }
    save_state(cfg, state)
    log(cfg, "pull %s -> %s (%d files, backup=%s)" % (sid, target_dir, len(rel_files), backup_dir))

    if backup_dir:
        print("backup   %s" % backup_dir)
    print("pulled. Resume with:  /resume %s" % sid)
    print("   or from a shell:   claude --resume %s" % (target_dir / ("%s.jsonl" % sid)))
    return 0


# --------------------------------------------------------------------------
# CLI


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--remote", help="rclone remote, e.g. r2: or mydrive:bucket (plugin option rclone_remote)")
    common.add_argument("--base-path", help="folder on the remote (plugin option base_path, default %s)" % DEFAULT_BASE_PATH)
    common.add_argument("--data-dir", help="state/log dir (default $CLAUDE_PLUGIN_DATA)")
    common.add_argument("--claude-dir", help="Claude config dir (default $CLAUDE_CONFIG_DIR or ~/.claude)")
    common.add_argument("--cwd", help="working directory of the session (default: current dir)")
    common.add_argument("--git-remote", help="git remote name used for repo identity (default origin)")
    common.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")

    parser = argparse.ArgumentParser(prog="relay.py", description="Move Claude Code sessions between machines.")
    parser.add_argument("--version", action="version", version="session-relay %s" % RELAY_VERSION)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("push", parents=[common], help="upload a session")
    p.add_argument("--session", required=True, help="session id (in skills: ${CLAUDE_SESSION_ID})")
    p.add_argument("--transcript", help="explicit path to <session-id>.jsonl")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("list", parents=[common], help="list this repo's sessions on the remote")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("pull", parents=[common], help="download a session into this machine's project dir")
    p.add_argument("session_id", nargs="?", help="session id to pull")
    p.add_argument("--latest", action="store_true", help="pull the most recently pushed session")
    p.add_argument("--current-session", help="id of the session running this command; its transcript's "
                   "directory is used as the target project dir")
    p.add_argument("--project-dir", help="explicit target ~/.claude/projects/<dir>")
    p.set_defaults(func=cmd_pull)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except RelayError as exc:
        eprint("relay: error: %s" % exc)
        try:
            log(Config(args), "error %s: %s" % (args.command, exc))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
