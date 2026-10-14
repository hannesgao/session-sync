"""Tests for scripts/relay.py.

Two-machine simulation (see SPEC.md, "How to test inside the sandbox"):

  Machine A (macOS-like): HOME=/tmp/mA/Users/alice, repo /tmp/mA/Users/alice/code/X
  Machine B (WSL-like):   HOME=/tmp/mB/home/bob,    repo /tmp/mB/home/bob/src/X

Both repos have the same git remote URL; the shared backend is an rclone
"local" remote pointing at a temp folder. Fixture transcripts contain absolute
paths under A. Run with:

  python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RELAY = REPO / "scripts" / "relay.py"

spec = importlib.util.spec_from_file_location("relay", str(RELAY))
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

A_ROOT = Path("/tmp/mA")
B_ROOT = Path("/tmp/mB")
A_HOME = "/tmp/mA/Users/alice"
A_REPO = A_HOME + "/code/X"
B_HOME = "/tmp/mB/home/bob"
B_REPO = B_HOME + "/src/X"
REMOTE_URL = "git@github.com:hannesgao/X.git"

# Fixture-only: directory names as Claude Code would create them. The plugin
# itself never computes these (it uses dirname(transcript_path)).
A_PROJ = A_HOME + "/.claude/projects/-tmp-mA-Users-alice-code-X"
B_PROJ = B_HOME + "/.claude/projects/-tmp-mB-home-bob-src-X"

# Strings that look like A paths but must NOT be rewritten (different paths).
NEGATIVE_MARKERS = ["/tmp/mA/Users/alice2/elsewhere.txt", "/private/tmp/mA/Users/alice/code/X/p"]

HAVE_RCLONE = shutil.which("rclone") is not None
HAVE_GIT = shutil.which("git") is not None


def fixture_lines(sid):
    """JSONL records with absolute A paths in many positions."""
    tool_result_path = "%s/%s/tool-results/toolu_01.txt" % (A_PROJ, sid)
    return [
        {"type": "user", "sessionId": sid, "cwd": A_REPO, "gitBranch": "main", "version": "2.1.299",
         "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": "Look at %s/src/app.py and my %s/.zshrc. Grüße ✓" % (A_REPO, A_HOME)}},
        {"type": "assistant", "sessionId": sid, "cwd": A_REPO, "uuid": "a1", "parentUuid": "u1",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {"file_path": A_REPO + "/src/app.py"}}]}},
        {"type": "user", "sessionId": sid, "cwd": A_REPO, "uuid": "u2", "parentUuid": "a1",
         "toolUseResult": {"filePath": A_REPO + "/src/app.py"},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_01",
              "content": "Output saved to %s\nSibling repo %s/code/X2/notes.txt\nNot ours: %s and %s"
                         % (tool_result_path, A_HOME, NEGATIVE_MARKERS[0], NEGATIVE_MARKERS[1])}]}},
        {"type": "assistant", "sessionId": sid, "cwd": A_REPO, "uuid": "a2", "parentUuid": "u2",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "The repo is at %s." % A_REPO}]}},
    ]


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Machine(object):
    def __init__(self, root, home, repo, rclone_conf):
        self.root = Path(root)
        self.home = Path(home)
        self.repo = Path(repo)
        self.claude = self.home / ".claude"
        self.data = self.claude / "plugins" / "data" / "session-relay-session-sync"
        self.rclone_conf = rclone_conf

    def env(self):
        env = dict((k, v) for k, v in os.environ.items()
                   if not k.startswith(("CLAUDE_", "RELAY_", "RCLONE_", "GIT_")))
        env.update({
            "HOME": str(self.home),
            "RCLONE_CONFIG": str(self.rclone_conf),
            "CLAUDE_PLUGIN_DATA": str(self.data),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        return env

    def git(self, *args, cwd=None):
        subprocess.run(["git"] + list(args), cwd=str(cwd or self.repo), env=self.env(),
                       check=True, capture_output=True, text=True)

    def make_repo(self, remote_url):
        self.repo.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", remote_url)
        (self.repo / "README").write_text("x\n")
        self.git("add", "README")
        self.git("commit", "-q", "-m", "init")

    def relay(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(RELAY)] + list(args), cwd=str(cwd or self.repo),
                              env=self.env(), capture_output=True, text=True)


@unittest.skipUnless(HAVE_RCLONE and HAVE_GIT, "needs rclone and git")
class TwoMachineTest(unittest.TestCase):
    def setUp(self):
        for root in (A_ROOT, B_ROOT):
            if root.exists():
                shutil.rmtree(str(root))
        self.addCleanup(lambda: [shutil.rmtree(str(r), ignore_errors=True) for r in (A_ROOT, B_ROOT)])
        self.tmp = Path(tempfile.mkdtemp(prefix="relay-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.backend = self.tmp / "backend"
        self.backend.mkdir()
        conf = self.tmp / "rclone.conf"
        conf.write_text("[shared]\ntype = local\n")
        self.remote_args = ["--remote", "shared:", "--base-path", str(self.backend / "relay")]

        self.A = Machine(A_ROOT, A_HOME, A_REPO, conf)
        self.B = Machine(B_ROOT, B_HOME, B_REPO, conf)
        self.A.make_repo(REMOTE_URL)
        self.B.make_repo(REMOTE_URL)

        # Session on A: transcript + sibling dir with subagent transcript,
        # a text tool result and a binary file.
        self.sid = str(uuid.uuid4())
        a_proj = Path(A_PROJ)
        write_jsonl(a_proj / (self.sid + ".jsonl"), fixture_lines(self.sid))
        write_jsonl(a_proj / self.sid / "subagents" / "agent-abc.jsonl", [
            {"type": "user", "isSidechain": True, "sessionId": self.sid, "cwd": A_REPO,
             "message": {"role": "user", "content": "grep in %s/src" % A_REPO}}])
        (a_proj / self.sid / "tool-results").mkdir(parents=True)
        (a_proj / self.sid / "tool-results" / "toolu_01.txt").write_text(
            "listing of %s:\n%s/src/app.py\n" % (A_REPO, A_REPO))
        self.binary = b"\x89PNG\r\n\x1a\n\x00\x00" + A_REPO.encode() + b"\x00\xff"
        (a_proj / self.sid / "tool-results" / "img.bin").write_bytes(self.binary)

        # The /pickup session already running on B (its transcript tells the
        # plugin which project dir belongs to B's repo).
        self.b_current = str(uuid.uuid4())
        write_jsonl(Path(B_PROJ) / (self.b_current + ".jsonl"),
                    [{"type": "user", "sessionId": self.b_current, "cwd": B_REPO,
                      "message": {"role": "user", "content": "/pickup"}}])

    # helpers --------------------------------------------------------------

    def push_a(self, *extra):
        res = self.A.relay("push", "--session", self.sid, *(self.remote_args + list(extra)))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res

    def pull_b(self, *extra, expect=0):
        res = self.B.relay("pull", self.sid, "--current-session", self.b_current,
                           *(self.remote_args + list(extra)))
        self.assertEqual(res.returncode, expect, res.stdout + res.stderr)
        return res

    def backend_files(self):
        return sorted(p.relative_to(self.backend).as_posix() for p in self.backend.rglob("*") if p.is_file())

    def files_named_after_sid(self, root):
        return sorted(str(p) for p in Path(root).rglob("*") if self.sid in p.name)

    # tests ----------------------------------------------------------------

    def test_push_dry_run_uploads_nothing(self):
        res = self.push_a("--dry-run")
        self.assertIn("[dry-run] nothing uploaded", res.stdout)
        self.assertEqual(self.backend_files(), [])

    def test_push_uploads_transcript_sibling_dir_and_meta(self):
        self.push_a()
        key = relay.repo_key_for("github.com/hannesgao/x")
        base = "relay/%s/%s/" % (key, self.sid)
        self.assertEqual(self.backend_files(), sorted([
            base + "meta.json",
            base + self.sid + ".jsonl",
            base + self.sid + "/subagents/agent-abc.jsonl",
            base + self.sid + "/tool-results/img.bin",
            base + self.sid + "/tool-results/toolu_01.txt",
        ]))
        # Uploaded transcript is byte-identical to A's (rewriting happens on pull).
        self.assertEqual((self.backend / base / (self.sid + ".jsonl")).read_bytes(),
                         (Path(A_PROJ) / (self.sid + ".jsonl")).read_bytes())

        meta = json.loads((self.backend / base / "meta.json").read_text())
        os_name, _ = relay.detect_os()
        self.assertEqual(meta["session_id"], self.sid)
        self.assertEqual(meta["remote_url"], "github.com/hannesgao/x")
        self.assertEqual(meta["repo_key"], key)
        self.assertEqual(meta["source_repo_root"], A_REPO)
        self.assertEqual(meta["source_home"], A_HOME)
        self.assertEqual(meta["source_project_dir"], A_PROJ)
        self.assertEqual(meta["os"], os_name)
        self.assertEqual(meta["hostname"], socket.gethostname())
        self.assertEqual(meta["branch"], "main")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=A_REPO, capture_output=True, text=True).stdout.strip()
        self.assertEqual(meta["head"], head)
        self.assertRegex(meta["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("claude_code_version", meta)
        if shutil.which("claude"):
            self.assertRegex(meta["claude_code_version"] or "", r"^\d+\.\d+\.\d+")
        self.assertEqual(meta["path_style"], "posix")
        self.assertEqual(len(meta["files"]), 4)

    def test_list_on_b(self):
        res = self.B.relay("list", *self.remote_args)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no sessions", res.stdout)
        self.push_a()
        res = self.B.relay("list", "--json", *self.remote_args)
        self.assertEqual(res.returncode, 0, res.stderr)
        metas = json.loads(res.stdout)
        self.assertEqual([m["session_id"] for m in metas], [self.sid])
        self.assertFalse(metas[0]["local_copy"])
        res = self.B.relay("list", "--dry-run", *self.remote_args)
        self.assertIn(self.sid, res.stdout)

    def test_pull_dry_run_writes_nothing(self):
        self.push_a()
        before = sorted(str(p) for p in B_ROOT.rglob("*"))
        res = self.pull_b("--dry-run")
        self.assertIn("[dry-run] nothing written", res.stdout)
        self.assertIn("rewrite  repo root", res.stdout)
        self.assertEqual(sorted(str(p) for p in B_ROOT.rglob("*")), before)

    def test_push_a_pull_b_end_to_end(self):
        self.push_a()
        res = self.pull_b()
        self.assertIn("/resume %s" % self.sid, res.stdout)
        b_proj = Path(B_PROJ)

        # 1. Files land in B's project dir.
        jsonl = b_proj / (self.sid + ".jsonl")
        self.assertTrue(jsonl.is_file())
        self.assertTrue((b_proj / self.sid / "subagents" / "agent-abc.jsonl").is_file())
        self.assertTrue((b_proj / self.sid / "tool-results" / "toolu_01.txt").is_file())
        self.assertEqual((b_proj / self.sid / "tool-results" / "img.bin").read_bytes(), self.binary)

        # 2. Every A prefix is rewritten to B's (in every text file).
        for path in [jsonl, b_proj / self.sid / "subagents" / "agent-abc.jsonl",
                     b_proj / self.sid / "tool-results" / "toolu_01.txt"]:
            text = path.read_text(encoding="utf-8")
            for marker in NEGATIVE_MARKERS:
                text = text.replace(marker, "")
            self.assertNotIn("/tmp/mA", text, path)
            self.assertIn(B_REPO, text)

        records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
        originals = fixture_lines(self.sid)
        self.assertEqual(len(records), len(originals))
        # The JSONL schema is untouched: same keys, same structure.
        for got, orig in zip(records, originals):
            self.assertEqual(sorted(got), sorted(orig))
        self.assertEqual(records[0]["cwd"], B_REPO)
        self.assertEqual(records[0]["message"]["content"],
                         "Look at %s/src/app.py and my %s/.zshrc. Grüße ✓" % (B_REPO, B_HOME))
        self.assertEqual(records[1]["message"]["content"][0]["input"]["file_path"], B_REPO + "/src/app.py")
        self.assertEqual(records[2]["toolUseResult"]["filePath"], B_REPO + "/src/app.py")
        tool_text = records[2]["message"]["content"][0]["content"]
        # Project dir prefix is mapped to B's project dir, not just home-rewritten.
        self.assertIn("%s/%s/tool-results/toolu_01.txt" % (B_PROJ, self.sid), tool_text)
        # Home-relative paths outside the repo follow the home prefix.
        self.assertIn(B_HOME + "/code/X2/notes.txt", tool_text)
        # Look-alike paths are left alone.
        for marker in NEGATIVE_MARKERS:
            self.assertIn(marker, tool_text)
        self.assertEqual(records[3]["message"]["content"][0]["text"], "The repo is at %s." % B_REPO)

        # 3. No duplicate copy of the session id anywhere on B.
        self.assertEqual(self.files_named_after_sid(B_ROOT),
                         sorted([str(jsonl), str(b_proj / self.sid)]))
        self.assertEqual(len(list((Path(B_HOME) / ".claude" / "projects").glob("*/%s.jsonl" % self.sid))), 1)

        # 4. Metadata / state on B is correct.
        state = json.loads((self.B.data / "state.json").read_text())
        self.assertEqual(state["project_dirs"][B_REPO], B_PROJ)
        self.assertEqual(state["pulled"][self.sid]["from_host"], socket.gethostname())
        self.assertEqual(state["pulled"][self.sid]["project_dir"], B_PROJ)
        # B's own running session is untouched.
        self.assertTrue((b_proj / (self.b_current + ".jsonl")).is_file())

        # List on B now reports the local copy.
        metas = json.loads(self.B.relay("list", "--json", *self.remote_args).stdout)
        self.assertTrue(metas[0]["local_copy"])

    def test_second_pull_backs_up_outside_projects(self):
        self.push_a()
        self.pull_b()
        jsonl = Path(B_PROJ) / (self.sid + ".jsonl")
        with open(jsonl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "local edit on B"}}) + "\n")
        local_version = jsonl.read_bytes()
        res = self.pull_b()
        self.assertIn("backup", res.stdout)
        backups = list((self.B.data / "backups").rglob(self.sid + ".jsonl"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), local_version)
        # Backups live in the plugin data dir, never under ~/.claude/projects.
        self.assertEqual(len(list((Path(B_HOME) / ".claude" / "projects").rglob(self.sid + ".jsonl"))), 1)
        self.assertNotIn(b"local edit on B", jsonl.read_bytes())

    def test_round_trip_back_to_a(self):
        self.push_a()
        self.pull_b()
        # Continue on B, push from B, pull on A: paths go back to A's.
        res = self.B.relay("push", "--session", self.sid, *self.remote_args)
        self.assertEqual(res.returncode, 0, res.stderr)
        a_current = str(uuid.uuid4())
        write_jsonl(Path(A_PROJ) / (a_current + ".jsonl"), [{"type": "user", "cwd": A_REPO}])
        res = self.A.relay("pull", self.sid, "--current-session", a_current, *self.remote_args)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        text = (Path(A_PROJ) / (self.sid + ".jsonl")).read_text(encoding="utf-8")
        self.assertNotIn("/tmp/mB", text)
        original = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in fixture_lines(self.sid))
        self.assertEqual(text, original)
        self.assertEqual(len(list((Path(A_HOME) / ".claude" / "projects").rglob(self.sid + ".jsonl"))), 1)

    def test_pull_refuses_duplicate_in_other_project(self):
        self.push_a()
        other = Path(B_HOME) / ".claude" / "projects" / "-tmp-mB-home-bob-old-X"
        write_jsonl(other / (self.sid + ".jsonl"), [{"type": "user"}])
        res = self.pull_b(expect=1)
        self.assertIn("already exists in another project dir", res.stderr)
        self.assertFalse((Path(B_PROJ) / (self.sid + ".jsonl")).exists())
        self.assertTrue((other / (self.sid + ".jsonl")).exists())  # never deleted

    def test_pull_refuses_running_session(self):
        self.push_a()
        res = self.B.relay("pull", self.sid, "--current-session", self.sid, *self.remote_args)
        self.assertEqual(res.returncode, 1)
        self.assertIn("refusing to overwrite", res.stderr)

    def test_pull_latest_and_https_variant_matches_ssh(self):
        self.push_a()
        self.B.git("remote", "set-url", "origin", "https://github.com/HannesGao/X")
        res = self.B.relay("pull", "--latest", "--current-session", self.b_current, *self.remote_args)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue((Path(B_PROJ) / (self.sid + ".jsonl")).is_file())

    def test_other_repo_sees_nothing(self):
        self.push_a()
        self.B.git("remote", "set-url", "origin", "git@github.com:hannesgao/other.git")
        res = self.B.relay("list", "--json", *self.remote_args)
        self.assertEqual(json.loads(res.stdout), [])
        res = self.pull_b(expect=1)
        self.assertIn("not found", res.stderr)

    def test_pull_without_known_project_dir_fails_clearly(self):
        self.push_a()
        res = self.B.relay("pull", self.sid, *self.remote_args)
        self.assertEqual(res.returncode, 1)
        self.assertIn("--project-dir", res.stderr)
        self.assertIn("never re-implements", res.stderr)

    def test_pull_uses_recorded_project_dir(self):
        self.push_a()
        self.pull_b()
        # A later pull without --current-session reuses the recorded dir.
        res = self.B.relay("pull", self.sid, *self.remote_args)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("recorded for", res.stdout)

    def test_missing_remote_config(self):
        res = self.A.relay("push", "--session", self.sid, "--remote", "${user_config.rclone_remote}")
        self.assertEqual(res.returncode, 1)
        self.assertIn("no rclone remote configured", res.stderr)
        self.assertTrue((self.A.data / "relay.log").is_file())

    def test_remote_from_plugin_option_env(self):
        env_args = ["--base-path", str(self.backend / "relay")]
        env = self.A.env()
        env["CLAUDE_PLUGIN_OPTION_RCLONE_REMOTE"] = "shared:"
        res = subprocess.run([sys.executable, str(RELAY), "push", "--session", self.sid] + env_args,
                             cwd=A_REPO, env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_no_git_remote(self):
        self.A.git("remote", "remove", "origin")
        res = self.A.relay("push", "--session", self.sid, *self.remote_args)
        self.assertEqual(res.returncode, 1)
        self.assertIn("no git remote", res.stderr)

    def test_unknown_session(self):
        res = self.A.relay("push", "--session", str(uuid.uuid4()), *self.remote_args)
        self.assertEqual(res.returncode, 1)
        self.assertIn("no transcript", res.stderr)

    def test_invalid_session_id_rejected(self):
        res = self.A.relay("push", "--session", "../../etc", *self.remote_args)
        self.assertEqual(res.returncode, 1)
        self.assertIn("invalid session id", res.stderr)


class NormalizeUrlTest(unittest.TestCase):
    def test_variants_are_equal(self):
        variants = [
            "git@github.com:hannesgao/session-sync.git",
            "https://github.com/hannesgao/session-sync.git",
            "https://github.com/hannesgao/session-sync",
            "https://user:token@github.com/HannesGao/Session-Sync/",
            "ssh://git@github.com/hannesgao/session-sync.git",
            "ssh://git@github.com:22/hannesgao/session-sync",
            "git://github.com/hannesgao/session-sync.git",
        ]
        self.assertEqual({relay.normalize_remote_url(v) for v in variants}, {"github.com/hannesgao/session-sync"})

    def test_credentials_dropped_and_hosts_distinct(self):
        self.assertNotIn("token", relay.normalize_remote_url("https://u:token@gitlab.com/a/b.git"))
        self.assertNotEqual(relay.normalize_remote_url("git@gitlab.com:a/b.git"),
                            relay.normalize_remote_url("git@github.com:a/b.git"))

    def test_repo_key_is_path_safe_and_stable(self):
        key = relay.repo_key_for("github.com/hannesgao/session-sync")
        self.assertRegex(key, r"^[a-z0-9._-]+$")
        self.assertEqual(key, relay.repo_key_for("github.com/hannesgao/session-sync"))
        self.assertNotEqual(key, relay.repo_key_for("github.com/hannesgao/session_sync"))


class RewriterTest(unittest.TestCase):
    def rw(self, pairs):
        return relay.Rewriter([(s, d, "p%d" % i) for i, (s, d) in enumerate(pairs)])

    def test_boundaries(self):
        r = self.rw([("/Users/alice/code/X", "/home/bob/src/X"), ("/Users/alice", "/home/bob")])
        cases = {
            "/Users/alice/code/X": "/home/bob/src/X",
            "/Users/alice/code/X/a.py": "/home/bob/src/X/a.py",
            '"cwd":"/Users/alice/code/X"': '"cwd":"/home/bob/src/X"',
            "at /Users/alice/code/X.": "at /home/bob/src/X.",
            "/Users/alice/code/X2/a": "/home/bob/code/X2/a",
            "/Users/alice/code/X.git": "/home/bob/code/X.git",
            "/Users/alice2/a": "/Users/alice2/a",
            "/private/Users/alice/a": "/private/Users/alice/a",
            "file:///Users/alice/a": "file:///home/bob/a",
            "(/Users/alice)": "(/home/bob)",
        }
        for src, expected in cases.items():
            self.assertEqual(r.rewrite_text(src), expected, src)

    def test_single_pass_no_chained_rewrites(self):
        # B's home looks like A's: a second pass would rewrite twice.
        r = self.rw([("/a", "/a/b")])
        self.assertEqual(r.rewrite_text("/a/x"), "/a/b/x")

    def test_counts(self):
        r = relay.Rewriter([("/Users/alice", "/home/bob", "home")])
        r.rewrite_text("/Users/alice/a /Users/alice/b")
        self.assertEqual(r.counts["home"], 2)

    def test_pairs_skip_identical_and_validate(self):
        meta = {"source_project_dir": "/p", "source_repo_root": "/r", "source_home": "/h"}
        pairs = relay.build_rewrite_pairs(meta, {"project_dir": "/p", "repo_root": "/r2", "home": "/h2"})
        self.assertEqual([(s, d) for s, d, _ in pairs], [("/r", "/r2"), ("/h", "/h2")])
        with self.assertRaises(relay.RelayError):
            relay.build_rewrite_pairs(meta, {"project_dir": "/p", "repo_root": '/r"x', "home": "/h"})
        with self.assertRaises(relay.RelayError):
            relay.build_rewrite_pairs({"source_home": "C:\\Users\\a"}, {"project_dir": None, "repo_root": None, "home": "/h"})

    def test_windows_style_not_supported_yet(self):
        with self.assertRaises(relay.RelayError):
            relay.path_variants("C:\\Users\\a", "windows")

    def test_binary_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "b"
            data = b"\x00/Users/alice\xff"
            p.write_bytes(data)
            self.assertFalse(relay.Rewriter([("/Users/alice", "/home/bob", "h")]).rewrite_file(p))
            self.assertEqual(p.read_bytes(), data)


class RemotePathTest(unittest.TestCase):
    def test_remote_root(self):
        self.assertEqual(relay.remote_root("r2:", "session-relay"), "r2:session-relay")
        self.assertEqual(relay.remote_root("r2:bucket", "session-relay"), "r2:bucket/session-relay")
        self.assertEqual(relay.remote_root("local:", "/tmp/x/"), "local:/tmp/x")
        self.assertEqual(relay.remote_root("r2:bucket", ""), "r2:bucket")
        self.assertEqual(relay.rjoin("r2:", "a", "b"), "r2:a/b")
        self.assertEqual(relay.rjoin("r2:s", "a"), "r2:s/a")


@unittest.skipUnless(shutil.which("claude"), "Claude Code CLI not installed")
class PluginValidateTest(unittest.TestCase):
    def test_claude_plugin_validate(self):
        for target in (REPO, REPO / ".claude-plugin" / "plugin.json", REPO / "skills"):
            res = subprocess.run(["claude", "plugin", "validate", "--strict", str(target)],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("Validation passed", res.stdout)

    def test_manifest_name(self):
        manifest = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "session-relay")
        self.assertFalse(manifest["name"].startswith("claude-"))


if __name__ == "__main__":
    unittest.main()
