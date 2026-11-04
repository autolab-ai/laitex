"""laitex: review a PDF in the browser and hand the comments to a coding agent.

The viewer and the agent share a small state folder next to the PDF
(.laitex-review/<name>/). The viewer writes comments and requests there; the agent reads
them with `laitex wait` and answers with `laitex reply` and `laitex done`.
The viewer changes a source file only when the user types in its code editor, runs only the
build command it was given with `laitex open --build`, and never commits, pushes or touches
the branches.
"""
import argparse
import bisect
import contextlib
import difflib
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import fcntl
except ImportError:  # Windows: fall back to the in-process lock only
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8765
STATUSES = ("open", "answered", "resolved")
# The agent counts as listening if `laitex wait` touched its heartbeat this recently.
LISTEN_WINDOW = 10
_THREAD_LOCK = threading.Lock()


# ---- state on disk --------------------------------------------------------

class State:
    """Files for one PDF: comments.json, request.json, chat.json, agent (heartbeat),
    transcript.json (the agent's session log, if known) and server.json."""

    def __init__(self, pdf):
        self.pdf = os.path.abspath(pdf)
        stem = os.path.splitext(os.path.basename(self.pdf))[0]
        self.dir = os.path.join(os.path.dirname(self.pdf), ".laitex-review", stem)
        self.comments = os.path.join(self.dir, "comments.json")
        self.request = os.path.join(self.dir, "request.json")
        self.chat = os.path.join(self.dir, "chat.json")
        self.transcript = os.path.join(self.dir, "transcript.json")
        self.agent = os.path.join(self.dir, "agent")
        self.server = os.path.join(self.dir, "server.json")
        self.log = os.path.join(self.dir, "server.log")
        self.lockfile = os.path.join(self.dir, "lock")
        self.logcfg = os.path.join(self.dir, "log.json")
        self.config = os.path.join(self.dir, "config.json")  # the build command and the source folder
        self.edits = os.path.join(self.dir, "edits.json")  # files the user changed in the viewer
        self.buildinfo = os.path.join(self.dir, "build.json")
        self.buildlock = os.path.join(self.dir, "build.lock")

    def ensure(self):
        os.makedirs(self.dir, exist_ok=True)
        ignore = os.path.join(os.path.dirname(self.dir), ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w") as f:
                f.write("# laitex review state\n*\n")

    @contextlib.contextmanager
    def lock(self):
        """Serialize read-modify-write between the server and CLI calls."""
        self.ensure()
        with _THREAD_LOCK, open(self.lockfile, "w") as fh:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def load(self):
        return read_json(self.comments, [])

    def save(self, items):
        write_json(self.comments, items)

    def get_request(self):
        return read_json(self.request, None)

    def log_path(self):
        """The compile log: the one set with `laitex open --log`, else <name>.log beside the PDF."""
        return read_json(self.logcfg, {}).get("path") or os.path.splitext(self.pdf)[0] + ".log"

    def cfg(self):
        return read_json(self.config, {})

    def src_root(self):
        """The folder of the source files: the one set with `laitex open --src`, else the PDF's."""
        return os.path.realpath(self.cfg().get("src") or os.path.dirname(self.pdf))

    def build_cmd(self):
        return self.cfg().get("build") or None

    def agent_age(self):
        try:
            return time.time() - os.path.getmtime(self.agent)
        except OSError:
            return None


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def utc_now():
    """A timestamp in the format of Claude Code transcripts, so chat and transcript sort together."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def session_log():
    """The transcript of the agent session running this command, if the harness keeps one
    we can read: Claude Code's ~/.claude/projects/<project>/<session id>.jsonl, or any file
    named by LAITEX_TRANSCRIPT."""
    path = os.environ.get("LAITEX_TRANSCRIPT")
    if path:
        return os.path.abspath(os.path.expanduser(path))
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid and re.fullmatch(r"[\w-]+", sid):
        hits = glob.glob(os.path.join(os.path.expanduser("~/.claude/projects"), "*", sid + ".jsonl"))
        if hits:
            return max(hits, key=os.path.getmtime)
    return None


# ---- the chat panel's view of the agent session -------------------------------

TAGS = re.compile(r"<(system-reminder|local-command-stdout|local-command-stderr|bash-stdout|bash-stderr)>.*?</\1>", re.S)


def tool_line(b):
    """One line describing a tool call."""
    i = b.get("input") or {}
    name = b.get("name", "tool")
    if name == "Bash":
        return i.get("description") or (i.get("command") or "").strip().split("\n")[0][:120]
    if "file_path" in i:
        return f"{name} {os.path.basename(i['file_path'])}"
    for key in ("description", "pattern", "query", "url", "prompt"):
        if isinstance(i.get(key), str):
            return f"{name}: {i[key][:100]}"
    return name


def entry_items(d):
    """What the chat panel shows for one line of a Claude Code transcript: the user's
    messages, the agent's text, and one line per tool call. Tool results, thinking,
    reminders and subagent work are left out."""
    if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return []
    kind, msg, ts = d.get("type"), d.get("message") or {}, d.get("timestamp") or ""
    content = msg.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
    out = []
    if kind == "user":
        for b in blocks:
            if b.get("type") == "image":
                out.append({"role": "user", "text": "[image]", "time": ts})
            if b.get("type") != "text":
                continue
            t = TAGS.sub("", b.get("text") or "").strip()
            cmd = re.search(r"<command-name>(.*?)</command-name>", t)
            bash = re.search(r"<bash-input>(.*?)</bash-input>", t, re.S)
            if cmd:
                args = re.search(r"<command-args>(.*?)</command-args>", t, re.S)
                t = (cmd.group(1) + " " + (args.group(1) if args else "")).strip()
            elif bash:
                t = "! " + bash.group(1).strip()
            elif t.startswith(("<", "Caveat:", "This session is being continued")):
                continue
            if t:
                out.append({"role": "user", "text": t, "time": ts, "parent": d.get("parentUuid")})
    elif kind == "assistant":
        for b in blocks:
            if b.get("type") == "text" and (b.get("text") or "").strip():
                out.append({"role": "agent", "text": b["text"].strip(), "time": ts})
            elif b.get("type") == "tool_use":
                out.append({"role": "tool", "text": tool_line(b), "time": ts})
    return out


class Mirror:
    """The chat panel's messages: the tail of the agent's session transcript, followed live,
    merged with the messages typed in the viewer and the agent's `laitex say` answers. Every
    item gets a sequence number so the viewer only fetches what is new."""
    KEEP = 800
    CHUNK = 4 << 20

    def __init__(self, st):
        self.st, self.lock = st, threading.Lock()
        self.path = None
        self.reset()

    def reset(self):
        self.epoch = uuid.uuid4().hex[:8]  # tells the viewer to drop what it has
        self.items, self.seq, self.seen = [], 0, set()
        self.offset, self.chat_mtime = 0, None

    def add(self, item):
        # A prompt sent again (a retry, an edit) starts a new branch from the same parent in
        # the transcript. Drop the old branch so the panel shows the conversation as it is now,
        # and change the epoch so the viewer reloads.
        parent = item.get("parent")
        if parent:
            old = next((k for k, i in enumerate(self.items) if i.get("parent") == parent), None)
            if old is not None:
                del self.items[old:]
                self.epoch = uuid.uuid4().hex[:8]
        self.seq += 1
        self.items.append(dict(item, seq=self.seq))
        del self.items[:-self.KEEP]

    def parse(self, data):
        items = []
        for line in data.split(b"\n"):
            try:
                items += entry_items(json.loads(line))
            except (ValueError, AttributeError, TypeError):
                pass
        return items

    def load_tail(self, path):
        """Read the transcript backwards, a chunk at a time, until there are enough messages
        to fill the panel. Returns them and sets the offset to follow the file from."""
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - self.CHUNK))
            tail = f.read()
            end = size - len(tail) + tail.rfind(b"\n") + 1  # a line still being written waits
            items, carry, pos = [], b"", end
            while pos > 0 and sum(i["role"] != "tool" for i in items) < 120 and end - pos < 48 * self.CHUNK:
                start = max(0, pos - self.CHUNK)
                f.seek(start)
                data = f.read(pos - start) + carry
                carry = b""
                if start > 0:  # the chunk starts inside a line; finish it with the next chunk
                    nl = data.find(b"\n")
                    carry, data = (data, b"") if nl < 0 else (data[:nl + 1], data[nl + 1:])
                items = self.parse(data) + items
                pos = start
        self.offset = end
        return items

    def chat_items(self):
        try:
            self.chat_mtime = os.path.getmtime(self.st.chat)
        except OSError:
            self.chat_mtime = None
        new = [dict(m, viewer=True) for m in read_json(self.st.chat, []) if m["id"] not in self.seen]
        self.seen.update(m["id"] for m in new)
        return new

    def update(self):
        with self.lock:
            path = read_json(self.st.transcript, {}).get("path")
            if path and not os.path.exists(path):
                path = None
            if path != self.path:  # a new session: load it and merge the chat in by time
                self.path = path
                self.reset()
                items = (self.load_tail(path) if path else []) + self.chat_items()
                for item in sorted(items, key=lambda i: i.get("time") or ""):
                    self.add(item)
                return
            if path:
                size = os.path.getsize(path)
                if size < self.offset:
                    self.offset = 0
                if size > self.offset:
                    with open(path, "rb") as f:
                        f.seek(self.offset)
                        data = f.read(size - self.offset)
                    cut = data.rfind(b"\n") + 1
                    self.offset += cut
                    for item in self.parse(data[:cut]):
                        self.add(item)
            try:
                mtime = os.path.getmtime(self.st.chat)
            except OSError:
                mtime = None
            if mtime != self.chat_mtime:
                for item in self.chat_items():
                    self.add(item)

    def since(self, after):
        self.update()
        with self.lock:
            return [i for i in self.items if i["seq"] > after]


# ---- versions: a copy of every build, for the history view --------------------

def cache_root():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/laitex")
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "laitex")


class Versions:
    """The versions of the PDF, kept in the user's cache folder rather than next to the PDF:
    builds can be large, and the PDF often sits in a synced or versioned folder.

    A version is one chunk of the agent's work. A new build opens a version "in progress", and
    later builds replace it, until the agent runs `laitex checkpoint`, which seals it (and, in a
    git repository, records the source; see git_snapshot). The first build kept is sealed at once.
    Two builds count as the same when they differ only in timestamps and the file ID.

    Builds the viewer runs after the user saved code in it are the user's ("by": "you"). They make
    a version of their own, which is sealed when the agent's next build arrives or when the user
    sends the agent a request, so the history tells the user's edits from the agent's work. If the
    agent's version is still in progress then, the user's build joins it instead ("mixed")."""
    KEEP = 40

    def __init__(self, st):
        self.st = st
        self.key = hashlib.sha1(st.pdf.encode()).hexdigest()[:12]
        self.stem = os.path.splitext(os.path.basename(st.pdf))[0]
        self.dir = os.path.join(cache_root(), self.key + "-" + self.stem)
        self.index = os.path.join(self.dir, "versions.json")
        self.seen = None  # (mtime, size) of the last build looked at
        self.hold = False  # the viewer is building: its build is kept by the builder, as the user's

    def list(self):
        return read_json(self.index, [])

    def path(self, n):
        return os.path.join(self.dir, f"v{int(n)}.pdf")

    @staticmethod
    def digest(data):
        data = re.sub(rb"/(CreationDate|ModDate)\s*\(D:[^)]*\)", b"", data)
        data = re.sub(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]", b"", data)
        return hashlib.sha1(data).hexdigest()

    @staticmethod
    def user_label(item):
        files = item.get("files") or []
        return "Your edits in the viewer" + (": " + ", ".join(files[:6]) + (" …" if len(files) > 6 else "") if files else "")

    def snapshot(self, settle=True, by=None, files=()):
        """Keep the current build if it is complete and new, and return its version number. The
        server calls this about once a second (settle: skip a file written in the last second);
        `checkpoint` calls it directly, and the viewer's builder with by="you"."""
        if self.hold and by != "you":
            return None
        try:
            stat = os.stat(self.st.pdf)
        except OSError:
            return None
        sig = (stat.st_mtime, stat.st_size)
        if sig == self.seen or (settle and time.time() - stat.st_mtime < 1):
            return None  # unchanged, or possibly still being written
        with open(self.st.pdf, "rb") as f:
            data = f.read()
        if b"%%EOF" not in data[-2048:] or len(data) != stat.st_size:
            return None  # an unfinished write; look again later
        self.seen = sig
        digest = self.digest(data)
        with self.st.lock():
            items = self.list()
            if items and items[-1]["digest"] == digest:
                return None
            os.makedirs(self.dir, exist_ok=True)
            open_ = items and not items[-1].get("sealed", True)
            if open_ and items[-1].get("by") == "you" and by != "you":
                # the agent builds after the user's edits: those are a version of their own
                items[-1].update(sealed=True, label=self.user_label(items[-1]), sealed_at=utc_now())
                open_ = False
            n = items[-1]["n"] if open_ else (items[-1]["n"] + 1 if items else 1)
            with open(self.path(n) + ".tmp", "wb") as f:
                f.write(data)
            os.replace(self.path(n) + ".tmp", self.path(n))
            item = {"n": n, "time": utc_now(), "built": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(stat.st_mtime)),
                    "size": len(data), "digest": digest, "sealed": not items, "label": "" if items else "the first build kept"}
            prev = items[-1] if open_ else {}
            if by == "you" and (prev.get("by") == "you" or not open_):
                item["by"] = "you"
                item["files"] = sorted(set(prev.get("files") or []) | set(files))
            elif by == "you" or prev.get("mixed"):
                item["mixed"] = True  # the agent's version in progress, with the user's edits in it
            if open_:
                items[-1] = item  # the version in progress gets the newer build
            else:
                items.append(item)
            if len(items) > self.KEEP:
                # the dropped versions still count: the first kept one remembers where the names
                # stood before it, so pruning never renames the versions that remain
                before = self.names(items)[items[-self.KEEP - 1]["n"]]
                items[-self.KEEP]["base"] = [int(x) for x in before[1:].split(".")]
            for old in items[:-self.KEEP]:
                with contextlib.suppress(OSError):
                    os.remove(self.path(old["n"]))
            write_json(self.index, items[-self.KEEP:])
            return n

    def seal_user(self):
        """Close the user's version in progress, if there is one: the user is done editing and
        hands the document to the agent. Returns its number."""
        with self.st.lock():
            items = self.list()
            if not items or items[-1].get("sealed", True) or items[-1].get("by") != "you":
                return None
            items[-1].update(sealed=True, label=self.user_label(items[-1]), sealed_at=utc_now())
            write_json(self.index, items)
            return items[-1]["n"]

    def seal(self, label, source):
        """Close the version in progress. Returns its number, or None if there is none."""
        with self.st.lock():
            items = self.list()
            if not items or items[-1].get("sealed", True):
                return None
            items[-1].update(sealed=True, label=label, sealed_at=utc_now())
            if source:
                items[-1]["source"] = source
            write_json(self.index, items)
            return items[-1]["n"]

    def truncate(self, n):
        """Forget the versions after n, as if they never existed: their PDFs, and their source
        snapshots in git. Used by rollback. Returns the numbers removed."""
        with self.st.lock():
            items = self.list()
            gone = [i for i in items if i["n"] > n]
            write_json(self.index, [i for i in items if i["n"] <= n])
        for i in gone:
            with contextlib.suppress(OSError):
                os.remove(self.path(i["n"]))
            src = i.get("source")
            if src:
                git(src["root"], "update-ref", "-d", src["ref"], check=False)
        return [i["n"] for i in gone]

    def attach(self, n, source):
        with self.st.lock():
            items = self.list()
            for i in items:
                if i["n"] == n:
                    i["source"] = source
            write_json(self.index, items)

    def set_stats(self, n, stats):
        """Store how much text version n changed against the one before. The viewer counts it,
        since it is the side that reads the text; the server only keeps the numbers."""
        keys = ("la", "ld", "ca", "cd", "changes", "moves")
        clean = {k: int(stats.get(k) or 0) for k in keys}
        with self.st.lock():
            items = self.list()
            for i in items:
                if i["n"] == n and i["digest"][:8] == str(stats.get("rev", i["digest"][:8])):
                    i["stats"] = clean
            write_json(self.index, items)

    def names(self, items=None):
        """Display names: v0.1, v0.2, ... until a version is committed to git, which becomes
        v1.0; then v1.1, v1.2, ... until the next commit (v2.0), and so on. Counting starts from
        the "base" of the first version, which the versions pruned before it left behind."""
        items = self.list() if items is None else items
        major, minor = items[0].get("base", (0, 0)) if items else (0, 0)
        out = {}
        for i in items:
            if i.get("release"):
                major, minor = major + 1, 0
            else:
                minor += 1
            out[i["n"]] = f"v{major}.{minor}"
        return out

    def public(self):
        """The list as the viewer sees it."""
        items = self.list()
        names = self.names(items)
        return [{"n": i["n"], "name": names[i["n"]], "time": i["time"], "built": i.get("built"),
                 "rev": i["digest"][:8], "sealed": i.get("sealed", True), "label": i.get("label", ""),
                 "source": bool(i.get("source")), "release": bool(i.get("release")), "stats": i.get("stats"),
                 "by": i.get("by"), "mixed": bool(i.get("mixed"))}
                for i in items]

    def mark_release(self):
        """If a commit made during the review holds the source of a version, mark that version as
        a release (the next major number). Commits from before the review do not count. A version matches when every file of the commit, except the PDF,
        is as the version had it; files the version had but the commit leaves out (untracked
        then) do not count. Returns the version marked, if any."""
        items = self.list()
        srcs = [i for i in reversed(items) if i.get("source")]
        if not srcs:
            return None
        root, pdf = srcs[0]["source"]["root"], srcs[0]["source"]["pdf"]
        head = git(root, "rev-parse", "-q", "--verify", "HEAD", check=False)
        if not head or head == srcs[-1]["source"].get("head"):
            return None  # no commit since the review started: the versions stay v0.x
        for i in srcs:
            r = subprocess.run(["git", "-C", root, "diff", "--quiet", "--diff-filter=d", i["source"]["commit"], "HEAD",
                                "--", ".", f":(exclude){pdf}"], capture_output=True)
            if r.returncode == 0:
                if i.get("release"):
                    return None  # this commit is already marked
                with self.st.lock():
                    fresh = self.list()
                    for f in fresh:
                        if f["n"] == i["n"]:
                            f["release"] = True
                    write_json(self.index, fresh)
                return i["n"]
        return None

    def watch(self):
        while True:
            try:
                self.snapshot()
            except Exception as e:  # never let a bad build stop the server
                print(f"laitex: version snapshot failed: {e}", file=sys.stderr, flush=True)
            time.sleep(1)


# ---- source snapshots in git, for rollback -------------------------------------

def git(root, *args, env=None, check=True):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, env=env)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def git_root(path):
    r = subprocess.run(["git", "-C", os.path.dirname(path), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


GIT_ID = {"GIT_AUTHOR_NAME": "laitex", "GIT_AUTHOR_EMAIL": "laitex@localhost",
          "GIT_COMMITTER_NAME": "laitex", "GIT_COMMITTER_EMAIL": "laitex@localhost"}


def worktree_tree(root, pdf):
    """A git tree of the files as they are now: every file git would see (tracked, or untracked
    and not ignored), except the PDF. Built with a temporary index, so the real index, the
    branch and the working tree are untouched."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
        real = os.path.join(root, git(root, "rev-parse", "--git-path", "index"))
        if os.path.exists(real):
            shutil.copy(real, env["GIT_INDEX_FILE"])  # its file stats make `add` fast
        git(root, "add", "-A", env=env)
        git(root, "rm", "--cached", "-q", "--ignore-unmatch", "--", pdf, env=env)
        return git(root, "write-tree", env=env)


def git_snapshot(versions, n, label):
    """Record the source as it is now, for rolling back to version n. The PDF itself is left
    out, since the cache already keeps it. It goes into a commit under refs/laitex/, which is on
    no branch and which `git push` does not send."""
    root = git_root(versions.st.pdf)
    if not root:
        return None
    pdf = os.path.relpath(os.path.realpath(versions.st.pdf), root)
    tree = worktree_tree(root, pdf)
    head = git(root, "rev-parse", "-q", "--verify", "HEAD", check=False)
    commit = git(root, "commit-tree", tree, *(["-p", head] if head else []),
                 "-m", f"laitex v{n}: {label or 'checkpoint'}", env=dict(os.environ, **GIT_ID))
    ref = f"refs/laitex/{versions.stem}-{versions.key[:6]}/v{n}"
    git(root, "update-ref", ref, commit)
    # Drop the snapshots of versions no longer kept.
    kept = {f"v{i['n']}" for i in versions.list()} | {f"v{n}"}
    for old in git(root, "for-each-ref", "--format=%(refname)", ref.rsplit("/", 1)[0] + "/").split():
        if old.rsplit("/", 1)[1] not in kept:
            git(root, "update-ref", "-d", old, check=False)
    return {"root": root, "commit": commit, "ref": ref, "pdf": pdf, "head": head}


# ---- the source files: listing, reading and saving them for the code editor ---------

SOURCE_EXT = {".tex", ".ltx", ".latex", ".bib", ".sty", ".cls", ".bst", ".bbx", ".cbx", ".dtx", ".ins",
              ".tikz", ".pgf", ".typ", ".md", ".markdown", ".qmd", ".rmd", ".txt", ".yml", ".yaml"}
SOURCE_NAMES = {"Makefile", "makefile", "GNUmakefile", "latexmkrc", ".latexmkrc", "justfile"}
PROSE_EXT = {".tex", ".ltx", ".latex", ".typ", ".md", ".markdown", ".qmd", ".rmd"}
SKIP_DIRS = {".git", ".laitex-review", "node_modules", "__pycache__", ".venv", "venv", ".cache", ".hg", ".svn"}
MAX_EDIT = 4 << 20  # larger files are not opened in the editor
UMASK = os.umask(0o022)
os.umask(UMASK)
_FILE_LOCK = threading.Lock()


def is_source(path):
    base = os.path.basename(path)
    return base in SOURCE_NAMES or os.path.splitext(base)[1].lower() in SOURCE_EXT


def text_hash(data):
    return hashlib.sha1(data).hexdigest()[:16]


def safe_rel(rel):
    """A path that stays inside the folder it is relative to and names a source file."""
    if not isinstance(rel, str) or not rel or "\0" in rel or os.path.isabs(rel):
        return False
    rel = os.path.normpath(rel)
    return not (rel == ".." or rel.startswith(".." + os.sep) or not is_source(rel)
                or any(part in SKIP_DIRS for part in rel.split(os.sep)[:-1]))


def inside(root, path):
    """Whether path, with its symlinks followed, is in root (which is already a real path).
    A file not created yet counts by its folder."""
    real = os.path.realpath(path)
    return real.startswith(root + os.sep)


class Sources:
    """The project's source files, under the source folder (State.src_root). The viewer may read
    and save only these: files with a source extension, at a relative path inside the folder."""

    def __init__(self, st):
        self.st = st
        self._cache = (None, 0, [])  # (root, time, files)

    def root(self):
        return self.st.src_root()

    def resolve(self, rel):
        """The absolute path of a source file given relative to the root, or None if the path
        leaves the root (also through a symlink) or is not a source file."""
        if not safe_rel(rel):
            return None
        root = self.root()
        full = os.path.join(root, os.path.normpath(rel))
        return full if inside(root, full) else None

    def rel(self, path):
        """A path relative to the root, or None for a file outside it."""
        r = os.path.relpath(os.path.realpath(path), self.root())
        return None if r.startswith("..") else r

    def list(self):
        """Relative paths of the source files, cached for a few seconds. In a git repository the
        files are those git sees (tracked, or untracked and not ignored); elsewhere the folder is
        walked, skipping hidden folders and stopping after a few thousand entries."""
        root, at, files = self._cache
        if root == self.root() and time.time() - at < 3:
            return files
        root = self.root()
        files = None
        if git_root(os.path.join(root, "x")):
            r = subprocess.run(["git", "-C", root, "ls-files", "-z", "-co", "--exclude-standard"],
                               capture_output=True)
            if r.returncode == 0:
                files = [f for f in r.stdout.decode("utf-8", "replace").split("\0")
                         if f and is_source(f) and not any(p in SKIP_DIRS for p in f.split("/")[:-1])]
        if files is None:
            files, seen = [], 0
            for d, dirs, names in os.walk(root):
                depth = os.path.relpath(d, root).count(os.sep)
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".") and depth < 5]
                seen += len(names) + len(dirs)
                files += [os.path.relpath(os.path.join(d, n), root) for n in names if is_source(n)]
                if seen > 20000 or len(files) > 3000:
                    break
        files = sorted({f for f in files if inside(root, os.path.join(root, f))},
                       key=lambda f: (f.count("/"), f.lower()))[:3000]
        self._cache = (root, time.time(), files)
        return files

    def main(self, files=None):
        """The file to open first: the one named like the PDF, else the shallowest .tex file."""
        files = self.list() if files is None else files
        stem = os.path.splitext(os.path.basename(self.st.pdf))[0]
        for ext in (".tex", ".typ", ".md", ".qmd", ".rmd", ".ltx"):
            hits = [f for f in files if os.path.basename(f) == stem + ext]
            if hits:
                return hits[0]
        tex = [f for f in files if f.endswith(".tex")]
        return (tex or files or [None])[0]

    def stats(self):
        out, root = [], self.root()
        for f in self.list():
            try:
                s = os.stat(os.path.join(root, f))
            except OSError:
                continue
            out.append({"path": f, "size": s.st_size, "mtime": s.st_mtime_ns})
        return out


def read_source(path):
    """A source file as the editor gets it: its text, the hash of its bytes, and the encoding
    it is saved back in (UTF-8, or Latin-1 for a file that is not valid UTF-8)."""
    with open(path, "rb") as f:
        data = f.read()
    s = os.stat(path)
    out = {"hash": text_hash(data), "mtime": s.st_mtime_ns, "size": s.st_size}
    if len(data) > MAX_EDIT:
        return dict(out, text=None, error=f"too large to edit here ({len(data) >> 20} MB)")
    if b"\0" in data[:8192]:
        return dict(out, text=None, error="a binary file")
    try:
        return dict(out, text=data.decode("utf-8"), encoding="utf-8")
    except UnicodeDecodeError:
        return dict(out, text=data.decode("latin-1"), encoding="latin-1")


def write_source(path, data):
    """Replace the file's bytes in one step, so a build reading it never sees half a file. A
    symlink is followed, so the link stays a link; the file keeps its permissions."""
    target = os.path.realpath(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".laitex-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if os.path.exists(target):
            shutil.copymode(target, tmp)
        else:
            os.chmod(tmp, 0o666 & ~UMASK)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def note_edit(st, rel, before, after):
    """Remember that the user changed a file in the viewer, for the agent's next `laitex wait`:
    the text before the first such change, and the text the user saved last."""
    with st.lock():
        edits = read_json(st.edits, {})
        e = edits.get(rel) or {"base": before, "since": now()}
        e["last"], e["time"] = after, now()
        edits[rel] = e
        write_json(st.edits, edits)


def take_edits(st):
    """The user's changes since the last request, as unified diffs, and forget them."""
    edits = read_json(st.edits, {})
    if not edits:
        return []
    root, out, budget = st.src_root(), [], 40000
    for rel, e in sorted(edits.items()):
        base, last = e.get("base"), e.get("last")
        if base == last:
            continue
        lines = lambda t: [l if l.endswith("\n") else l + "\n" for l in (t or "").splitlines(True)]
        diff = "".join(difflib.unified_diff(lines(base), lines(last), "a/" + rel, "b/" + rel, n=2))
        if len(diff) > budget:
            diff = diff[:max(budget, 2000)] + "\n... (cut; read the file or run git diff for the rest)\n"
        budget = max(0, budget - len(diff))
        out.append({"file": rel, "path": os.path.join(root, rel), "new": base is None, "diff": diff})
    with contextlib.suppress(OSError):
        os.remove(st.edits)
    return out


# ---- the build command: run by the viewer's Recompile, or by `laitex build` ---------------

BUILD_TIMEOUT = 600


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def build_status(st):
    info = read_json(st.buildinfo, {})
    if info.get("running") and not pid_alive(info.get("pid")):
        info["running"] = False  # the process that ran it is gone
    info.pop("pid", None)
    return info


def run_build(st, by, echo=False, ready=None):
    """Run the build command in the source folder and record how it went in build.json. A lock
    file keeps the viewer's builds and `laitex build` from running at the same time, which
    would mix up the auxiliary files (on systems with fcntl, so not on Windows). `ready` is
    called once the lock is held, just before the command starts."""
    cmd, root = st.build_cmd(), st.src_root()
    st.ensure()
    with open(st.buildlock, "w") as lk:
        if fcntl:
            fcntl.flock(lk, fcntl.LOCK_EX)
        if ready:
            ready()
        started = time.time()
        write_json(st.buildinfo, {"running": True, "by": by, "started": started, "pid": os.getpid(), "cmd": cmd})
        out, code = [], None
        try:
            p = subprocess.Popen(cmd, shell=True, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
            timer = threading.Timer(BUILD_TIMEOUT, lambda: stop_group(p))
            timer.start()
            try:
                for raw in p.stdout:
                    line = raw.decode("utf-8", "replace")
                    out.append(line)
                    del out[:-4000]
                    if echo:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                code = p.wait()
            finally:
                timer.cancel()
        except OSError as e:
            out.append(f"laitex: could not run the build command: {e}\n")
            code = 127
        text = "".join(out)
        info = {"running": False, "by": by, "started": started, "finished": time.time(), "code": code,
                "ok": code == 0, "cmd": cmd, "output": text[-20000:]}
        write_json(st.buildinfo, info)
        return info


def stop_group(p):
    with contextlib.suppress(OSError):
        if hasattr(os, "killpg"):
            os.killpg(p.pid, signal.SIGTERM)
        else:
            p.kill()


class Builder:
    """The viewer's builds. One runs at a time; asking again while one runs queues one more, so
    a burst of saves ends in a single build of the newest text. The build is the user's: its
    version in the history is marked as theirs (see Versions.snapshot)."""

    def __init__(self, st, versions, sources):
        self.st, self.versions, self.sources = st, versions, sources
        self.lock = threading.Lock()
        self.running = self.again = False
        self.files = set()  # files saved in the viewer since its last build

    def start(self):
        if not self.st.build_cmd():
            return False
        with self.lock:
            if self.running:
                self.again = True
                return True
            self.running = True
        threading.Thread(target=self.loop, daemon=True).start()
        return True

    def loop(self):
        try:
            while True:
                with self.lock:
                    self.again = False
                    files, self.files = sorted(self.files), set()
                by = "you" if files else None  # a rebuild with nothing saved builds the agent's edits

                def ready():
                    # The lock is ours, so a build of the agent's (`laitex build`) is finished: keep it as
                    # the agent's before muting the watcher, which leaves this build to the code below.
                    self.versions.snapshot(settle=False)
                    self.versions.hold = True

                try:
                    run_build(self.st, "you", ready=ready)
                    n = self.versions.snapshot(settle=False, by=by, files=files)
                    if n and by:
                        with contextlib.suppress(RuntimeError, OSError):
                            self.versions.attach(n, git_snapshot(self.versions, n, "your edits"))
                finally:
                    self.versions.hold = False
                with self.lock:
                    if not self.again:
                        self.running = False
                        return
        except Exception as e:  # never leave the button stuck on "compiling"
            print(f"laitex: build failed: {e}", file=sys.stderr, flush=True)
            with self.lock:
                self.running = False


# ---- SyncTeX: where a spot on the page comes from in the source, and back ------------------

SP_PER_BP = 65781.76


class SyncTeX:
    """A parsed .synctex(.gz) file. Nodes (glue, kerns, math, rules, void boxes) carry the
    file and line they were read from; each sits in the hbox of its typeset line. Paragraph
    lines are boxed when the paragraph ends, so the box's own line is the paragraph's last
    one, while the nodes inside know their own lines."""
    _NUM = re.compile(r"(\d+),(-?\d+)(?:,-?\d+)?:(-?\d+),(-?\d+)(?::(-?\d+)(?:,(-?\d+),(-?\d+))?)?")

    def __init__(self, path):
        self.inputs, self.boxes, self.nodes = {}, [], []  # box: (page, x, y, w, h, d, parent)
        unit, xoff, yoff, page, stack = 1.0, 0.0, 0.0, 0, []
        base = os.path.dirname(path)
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                c = line[:1]
                if c in "[(":
                    m = self._NUM.match(line, 1)
                    if m:
                        t, ln, x, y, w, h, d = (int(v or 0) for v in m.groups())
                        self.boxes.append((page, x, y, w, h, d, stack[-1] if stack else -1, c, t, ln))
                        stack.append(len(self.boxes) - 1)
                elif c in ")]":
                    if stack:
                        stack.pop()
                elif c in "hvxkg$r":
                    m = self._NUM.match(line, 1)
                    if m:
                        t, ln, x, y = (int(v) for v in m.groups()[:4])
                        self.nodes.append((page, t, ln, x, y, stack[-1] if stack else -1))
                elif c == "{":
                    with contextlib.suppress(ValueError):
                        page, stack = int(line[1:]), []
                elif line.startswith("Input:"):
                    tag, _, name = line[6:].rstrip("\n").partition(":")
                    with contextlib.suppress(ValueError):
                        self.inputs[int(tag)] = os.path.normpath(os.path.join(base, name)) if name else ""
                elif line.startswith("Unit:"):
                    unit = float(line[5:] or 1)
                elif line.startswith("X Offset:"):
                    xoff = float(line[9:] or 0)
                elif line.startswith("Y Offset:"):
                    yoff = float(line[9:] or 0)
        self.k, self.xoff, self.yoff = unit / SP_PER_BP, xoff, yoff
        self.by_line = {}
        for i, n in enumerate(self.nodes):
            self.by_line.setdefault((n[1], n[2]), []).append(i)

    def bp(self, x, y):
        return (x + self.xoff) * self.k, (y + self.yoff) * self.k

    def line_box(self, b):
        """The enclosing hbox of a node that is a typeset line with some width."""
        while b >= 0:
            box = self.boxes[b]
            if box[7] == "(" and box[3] > 0:
                return b
            b = box[6]
        return -1

    def view(self, path, line):
        """Boxes on the page (in PDF points from the top-left corner) of the source line, or of
        the nearest line after or before it that left something on the page."""
        path = os.path.realpath(path)
        tags = {t for t, p in self.inputs.items() if p and os.path.realpath(p) == path}
        for dl in [0] + [s * k for k in range(1, 40) for s in (1, -1)]:
            hits = [i for t in tags for i in self.by_line.get((t, line + dl), [])]
            if not hits:
                continue
            page = min(self.nodes[i][0] for i in hits)
            out, seen = [], set()
            for i in hits:
                n = self.nodes[i]
                if n[0] != page:
                    continue
                b = self.line_box(n[5])
                if b in seen:
                    continue
                seen.add(b)
                if b >= 0:
                    _, x, y, w, h, d = self.boxes[b][:6]
                    (x0, y0), (x1, y1) = self.bp(x, y - h), self.bp(x + w, y + d)
                else:
                    (x0, y0), (x1, y1) = self.bp(n[3], n[4] - 650000), self.bp(n[3] + 650000, n[4] + 150000)
                out.append({"page": page, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})
            out.sort(key=lambda r: (r["y"], r["x"]))
            return {"page": page, "line": line + dl, "boxes": out[:40]}
        return None

    def edit(self, page, x, y, keep):
        """The source file and line of the spot (x, y) on a page, in PDF points from the top-left
        corner. Only nodes of files that keep(path) accepts count, so a click lands in the
        project, not in a package. Inside a typeset line, the node nearest left of the spot is
        the space before the word clicked."""
        sx, sy = x / self.k - self.xoff, y / self.k - self.yoff
        ok_tags = {t for t, p in self.inputs.items() if p and keep(p)}
        cands = [n for n in self.nodes if n[0] == page and n[1] in ok_tags]
        if not cands:
            return None
        inside = []
        for b, box in enumerate(self.boxes):
            if box[0] == page and box[7] == "(" and box[3] > 0 and box[1] - 2e5 <= sx <= box[1] + box[3] + 2e5 \
                    and box[2] - box[4] - 1e5 <= sy <= box[2] + box[5] + 1e5:
                inside.append((box[3] * (box[4] + box[5] + 1), b))
        best = None
        for _, b in sorted(inside):
            mine = [n for n in cands if self.line_box(n[5]) == b]
            if mine:
                left = [n for n in mine if n[3] <= sx]
                best = max(left, key=lambda n: n[3]) if left else min(mine, key=lambda n: n[3])
                break
        if best is None:
            best = min(cands, key=lambda n: abs(n[3] - sx) + 3 * abs(n[4] - sy))
        return {"file": self.inputs[best[1]], "line": best[2]}


_SYNCTEX = {}


def synctex_for(st):
    """The SyncTeX data of the current build, parsed once per build, or None without a file."""
    stems = {os.path.splitext(st.pdf)[0], os.path.splitext(st.log_path())[0]}
    for stem in stems:
        for path in (stem + ".synctex.gz", stem + ".synctex"):
            try:
                s = os.stat(path)
            except OSError:
                continue
            key = (path, s.st_mtime_ns, s.st_size)
            hit = _SYNCTEX.get(st.pdf)
            if not hit or hit[0] != key:
                try:
                    hit = (key, SyncTeX(path))
                except (OSError, EOFError, ValueError) as e:
                    print(f"laitex: could not read {path}: {e}", file=sys.stderr, flush=True)
                    return None
                _SYNCTEX[st.pdf] = hit
            return hit[1]
    return None


# Without SyncTeX, a spot on the page is found in the source by its text: the letters around
# it, with comments, command names and the arguments that never reach the page left out.
_HIDDEN_ARGS = {"cite", "citep", "citet", "citealp", "citeauthor", "citeyear", "parencite", "textcite", "autocite",
                "ref", "eqref", "autoref", "cref", "Cref", "pageref", "nameref", "label", "includegraphics",
                "input", "include", "usepackage", "documentclass", "bibliography", "bibliographystyle", "begin",
                "end", "newcommand", "renewcommand", "def", "hspace", "vspace", "setlength", "addbibresource",
                "url", "href", "color", "textcolor", "definecolor"}


def letters(text, latex=True):
    """The letters of a text, lowercased, with each one's offset in the text."""
    out, pos, i, n = [], [], 0, len(text)
    while i < n:
        ch = text[i]
        if latex and ch == "%" and (i == 0 or text[i - 1] != "\\"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if latex and ch == "\\":
            j = i + 1
            while j < n and text[j].isalpha():
                j += 1
            name = text[i + 1:j]
            i = j if j > i + 1 else i + 2
            if name in _HIDDEN_ARGS:
                while i < n and text[i] in " *":
                    i += 1
                depth = 0
                while i < n and (depth or text[i] in "{["):
                    depth += 1 if text[i] in "{[" else -1 if text[i] in "}]" else 0
                    i += 1
            continue
        if ch.isalpha():
            out.append(ch.lower())
            pos.append(i)
        i += 1
    return "".join(out), pos


def locate(sources, text, at):
    """The source file and line of the character at index `at` of a piece of page text: the
    letters around it are looked up in the prose files, in a shrinking window until one place
    matches."""
    q, qpos = letters(text, latex=False)
    k = next((j for j, p in enumerate(qpos) if p >= at), len(q))
    docs = []
    for rel in sources.list():
        if os.path.splitext(rel)[1].lower() not in PROSE_EXT:
            continue
        try:
            with open(os.path.join(sources.root(), rel), encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        docs.append((rel, src) + letters(src, latex=not rel.endswith((".md", ".markdown", ".typ", ".qmd", ".rmd"))))
    for L in (40, 30, 22, 16, 11, 8):
        lo, hi = max(0, k - L), min(len(q), k + L)
        needle = q[lo:hi]
        if len(needle) < 8:
            continue
        hits = []
        for rel, src, sk, pos in docs:
            j = sk.find(needle)
            while j >= 0 and len(hits) < 5:
                hits.append((rel, src, pos[min(j + k - lo, len(pos) - 1)]))
                j = sk.find(needle, j + 1)
        if len(hits) == 1 or (hits and L <= 11):
            rel, src, off = hits[0]
            return {"file": rel, "line": src.count("\n", 0, off) + 1, "ch": off - src.rfind("\n", 0, off) - 1}
    return None


# ---- the source of two versions, compared ---------------------------------------------

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def parse_diff(text):
    """`git diff` output as files with hunks of [sign, text] lines."""
    files, f, h = [], None, None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            f = {"path": None, "old": None, "status": "M", "hunks": [], "add": 0, "del": 0}
            files.append(f)
            h = None
        elif f is None:
            continue
        elif h is None and line.startswith("new file"):
            f["status"] = "A"
        elif h is None and line.startswith("deleted file"):
            f["status"] = "D"
        elif h is None and line.startswith("Binary files"):
            f["binary"] = True
            m = re.match(r"Binary files (?:a/(.*?)|/dev/null) and (?:b/(.*)|/dev/null) differ", line)
            if m:
                f["path"] = m.group(2) or m.group(1)
        elif h is None and line.startswith("--- "):
            p = line[4:].rstrip("\t")
            f["old"] = p[2:] if p.startswith("a/") else None
        elif h is None and line.startswith("+++ "):
            p = line[4:].rstrip("\t")
            f["path"] = p[2:] if p.startswith("b/") else f["old"]
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)", line)
            if m:
                h = {"a": int(m.group(1)), "b": int(m.group(2)), "head": m.group(3), "lines": []}
                f["hunks"].append(h)
        elif h is not None and line[:1] in (" ", "+", "-"):
            h["lines"].append([line[0], line[1:]])
            if line[0] == "+":
                f["add"] += 1
            elif line[0] == "-":
                f["del"] += 1
    return files


def source_diff(st, versions, sources, a, b, ctx=3, only=None):
    """The changes to the source files between versions a and b, from their snapshots in git.
    Version 0 is the empty project; the version in progress, which has no snapshot yet, is
    the files as they are now."""
    root = git_root(st.pdf)
    if not root:
        return {"files": [], "note": "The source of each version is kept in git, and this folder is not a git repository."}
    items, names = versions.list(), versions.names()
    pdf_rel = os.path.relpath(os.path.realpath(st.pdf), root)

    def tree(n):
        if not n:
            return EMPTY_TREE
        v = next((i for i in items if i["n"] == n), None)
        if not v:
            raise LookupError(f"there is no version {n}")
        if v.get("source"):
            return v["source"]["commit"]
        if v is items[-1] and not v.get("sealed", True):
            return worktree_tree(root, pdf_rel)
        raise LookupError(f"{names.get(n, 'v' + str(n))} has no copy of its source "
                          "(it was kept before laitex recorded sources)")

    try:
        ta, tb = tree(a), tree(b)
    except LookupError as e:
        return {"files": [], "note": str(e)[0].upper() + str(e)[1:] + ", so its code cannot be compared."}
    except RuntimeError as e:
        return {"files": [], "note": f"git could not read the source: {e}"}
    src = os.path.relpath(sources.root(), root)
    spec = [src if not only else os.path.join(src, only), f":(exclude){pdf_rel}"]
    # git's own TeX, BibTeX and Markdown rules head each hunk with its section, not the line above it
    attrs = os.path.join(st.dir, "diff-attributes")
    if not os.path.exists(attrs):
        st.ensure()
        with open(attrs, "w") as f:
            f.write("".join(f"*{e} diff=tex\n" for e in (".tex", ".ltx", ".latex", ".sty", ".cls", ".dtx"))
                    + "*.bib diff=bibtex\n*.md diff=markdown\n*.markdown diff=markdown\n*.qmd diff=markdown\n")
    r = subprocess.run(["git", "-C", root, "-c", "core.quotePath=false", "-c", f"core.attributesFile={attrs}", "diff",
                        "--no-color", "--no-renames", "--no-ext-diff", f"-U{int(ctx)}", ta, tb, "--", *spec],
                       capture_output=True)
    if r.returncode not in (0, 1):
        return {"files": [], "note": "git diff failed: " + r.stderr.decode("utf-8", "replace").strip()}
    files, others = [], 0
    for f in parse_diff(r.stdout.decode("utf-8", "replace")):
        if not f["path"] or not is_source(f["path"]):
            others += 1
            continue
        f["path"] = os.path.relpath(os.path.join(root, f["path"]), sources.root())
        f.pop("old", None)
        n = 0
        for i, h in enumerate(f["hunks"]):
            n += len(h["lines"])
            if n > 5000:
                f["hunks"], f["cut"] = f["hunks"][:i + 1], True
                break
        files.append(f)
    return {"files": files, "others": others}


def thread_of(c):
    """The conversation after a comment: the agent's replies and the user's follow-ups.
    Comments answered before threads existed have only `reply`."""
    if c.get("thread"):
        return list(c["thread"])
    return [{"role": "agent", "text": c["reply"], "time": c.get("replied", "")}] if c.get("reply") else []


def public(c, root=None):
    """A comment without the highlight geometry, which agents do not need. A comment on the
    source also gets the file's absolute path."""
    out = {k: v for k, v in c.items() if k not in ("rects", "more", "range")}
    if c.get("file") and root:
        out["path"] = os.path.join(root, c["file"])
    return out


_LINES = {}  # path -> ((mtime, size), text with \n line breaks, offsets of the line starts)


def file_lines(path):
    try:
        s = os.stat(path)
    except OSError:
        return None
    key = (s.st_mtime_ns, s.st_size)
    hit = _LINES.get(path)
    if not hit or hit[0] != key:
        try:
            src = read_source(path)
        except OSError:
            return None
        if src.get("text") is None:
            return None
        norm = src["text"].replace("\r\n", "\n").replace("\r", "\n")
        hit = (key, norm, [0] + [m.end() for m in re.finditer("\n", norm)], any(ord(ch) > 0xFFFF for ch in norm))
        _LINES[path] = hit
    return hit[1:]


def u16(text):
    """The length of a text in UTF-16 code units, which is how the editor counts columns."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def from_u16(line, units):
    """The index in a line of the column the editor calls `units`."""
    n = 0
    for i, ch in enumerate(line):
        if n >= units:
            return i
        n += 2 if ord(ch) > 0xFFFF else 1
    return len(line)


def reanchor_code(st, items):
    """Keep comments on the source on their text when the file changes: if the range no longer
    holds the quoted text, take the place of that text nearest the old one. Lines and columns
    are 0-based, as in the editor. Returns whether a comment moved."""
    root, moved = st.src_root(), False
    for c in items:
        r = c.get("range")
        if not c.get("file") or c.get("status") == "resolved" or not c.get("quote") or not (isinstance(r, list) and len(r) == 4):
            continue
        hit = file_lines(os.path.join(root, c["file"]))
        if not hit:
            continue
        text, starts, astral = hit
        quote = c["quote"].replace("\r\n", "\n").replace("\r", "\n")
        line_of = lambda l: text[starts[l]:starts[l + 1] - 1 if l + 1 < len(starts) else len(text)]
        at = lambda l, ch: (starts[l] + (from_u16(line_of(l), ch) if astral else ch)) if 0 <= l < len(starts) else None
        a, b = at(r[0], r[1]), at(r[2], r[3])
        if a is not None and b is not None and text[a:b] == quote:
            continue
        want, best, i = a if a is not None else len(text), None, text.find(quote)
        while i >= 0:
            if best is None or abs(i - want) < abs(best - want):
                best = i
            i = text.find(quote, i + 1)
        if best is None:
            continue  # the text was rewritten: the comment stays where it was
        def lc(off):
            l = bisect.bisect_right(starts, off) - 1
            return l, (u16(text[starts[l]:off]) if astral else off - starts[l])
        (l0, c0), (l1, c1) = lc(best), lc(best + len(quote))
        c.update(range=[l0, c0, l1, c1], line=l0 + 1, end_line=l1 if c1 == 0 and l1 > l0 else l1 + 1)
        moved = True
    return moved


def load_anchored(st):
    """The comments, with those on the source moved to where their text is now. Call it with
    the state locked."""
    items = st.load()
    if reanchor_code(st, items):
        st.save(items)
    return items


def code_fields(data):
    """The place of a comment on the source, as the viewer sends it: the file, its lines
    (1-based, for the agent) and the exact range (0-based line and column, for the editor)."""
    out = {}
    if safe_rel(data.get("file")):
        out["file"] = os.path.normpath(data["file"])[:500]
        for k in ("line", "end_line"):
            if isinstance(data.get(k), int):
                out[k] = data[k]
        r = data.get("range")
        if isinstance(r, list) and len(r) == 4 and all(isinstance(v, int) for v in r):
            out["range"] = r
    return out


# ---- HTTP server ------------------------------------------------------------

# ---- the compile log: errors and warnings of the last LaTeX build ----------------

LOG_WRAP = 79  # TeX breaks log lines at max_print_line characters (79 in TeX Live and MiKTeX)
LOG_LEVELS = ("error", "warning", "box")
_SOURCE_EXT = (r"tex|sty|cls|clo|cfg|def|fd|ldf|bbl|aux|toc|lof|lot|out|ltx|bib|bst|tikz|dict|lua|"
               r"nav|snm|vrb|ind|idx|gls|glo|ist|mkiv|mkii")
_OPEN_FILE = re.compile(r"\(([^\s()]*\.(?:" + _SOURCE_EXT + r"))(?=[\s()\[]|$)", re.I)
_FILE_LINE_ERROR = re.compile(r"^(\S.*?\.(?:" + _SOURCE_EXT + r")):(\d+): (.*)$", re.I)
_WARNING = re.compile(r"^((?:LaTeX|Package \S+|Class \S+|Module \S+)(?: Font)? Warning): (.*)$")
_PDFTEX_WARNING = re.compile(r"pdfTeX warning(?: \(([^)]*)\))?: (.*)$")
_BOX = re.compile(r"^(Overfull|Underfull) \\[hv]box \(([^)]*)\)(.*?)(?: at lines? (\d+)(?:--\d+)?)?$")
_CONTINUED = re.compile(r"^\([\w@.\-]+\)\s+")


def _log_lines(raw):
    """The log as logical lines: TeX breaks long lines at exactly LOG_WRAP bytes, so a physical
    line of that length continues on the next one."""
    out, buf = [], b""
    for line in raw.replace(b"\r\n", b"\n").split(b"\n"):
        buf += line
        if len(line) == LOG_WRAP:
            continue
        out.append(buf.decode("utf-8", "replace"))
        buf = b""
    if buf:
        out.append(buf.decode("utf-8", "replace"))
    return out


def _source_name(path, base):
    """A file name as the user knows it: relative to the log's folder, or the bare name of a
    file from the TeX distribution."""
    if path.startswith("./"):
        return path[2:]
    if os.path.isabs(path):
        if "/texmf" in path or "/texlive/" in path or "/miktex/" in path.lower():
            return os.path.basename(path)
        rel = os.path.relpath(path, base)
        return path if rel.startswith("..") else rel
    return path


def parse_tex_log(raw, base, main=None):
    """Errors, warnings and over/underfull boxes in a TeX log (bytes). Each has a level, a
    message, the source file and line when the log says them, and the log lines it came from.
    The current file is followed through the '(' and ')' with which TeX marks opening and
    closing files; lines that belong to a message are not scanned for them."""
    lines = _log_lines(raw)
    items, stack = [], []

    def current():
        return next((f for f in reversed(stack) if f), main)

    def add(level, message, file, line, detail):
        items.append({"level": level, "message": message.strip(), "file": file, "line": line,
                      "detail": "\n".join(detail).strip("\n")[:2000], "source": "tex"})

    i = 0
    while i < len(lines):
        text = lines[i]
        fle = _FILE_LINE_ERROR.match(text)
        if text.startswith("! ") or fle:
            message = fle.group(3) if fle else text[2:]
            file, line = (_source_name(fle.group(1), base), int(fle.group(2))) if fle else (current(), None)
            detail, j = [text], i + 1
            while j < len(lines) and j - i < 20 and not lines[j].startswith("! ") and not _FILE_LINE_ERROR.match(lines[j]):
                detail.append(lines[j])
                at = re.match(r"l\.(\d+)", lines[j])
                j += 1
                if at:
                    line = line or int(at.group(1))
                    if j < len(lines) and lines[j].strip():
                        detail.append(lines[j])  # the rest of the source line, after the break
                        j += 1
                    break
            add("error", message, file, line, detail)
            i = j
            continue
        w = _WARNING.match(text)
        if w:
            detail, j = [text], i + 1
            while j < len(lines) and _CONTINUED.match(lines[j]):
                detail.append(lines[j])
                j += 1
            message = " ".join([w.group(2)] + [_CONTINUED.sub("", d) for d in detail[1:]])
            at = re.search(r"on input line (\d+)", message)
            add("warning", f"{w.group(1)}: {message}", current(), int(at.group(1)) if at else None, detail)
            i = j
            continue
        b = _BOX.match(text)
        if b:
            detail, j = [text], i + 1
            while j < len(lines) and lines[j].strip() and j - i < 8:
                detail.append(lines[j])
                j += 1
            add("box", text, current(), int(b.group(4)) if b.group(4) else None, detail)
            i = j
            continue
        p = _PDFTEX_WARNING.search(text)
        if p:
            add("warning", "pdfTeX warning" + (f" ({p.group(1)})" if p.group(1) else "") + ": " + p.group(2),
                current(), None, [text])
            text = text[:p.start()]  # the files opened or closed before it still count
        k = 0
        while True:
            m = re.compile(r"[()]").search(text, k)
            if not m:
                break
            k = m.start()
            if text[k] == ")":
                if stack:
                    stack.pop()
                k += 1
            else:
                f = _OPEN_FILE.match(text, k)
                stack.append(_source_name(f.group(1), base) if f else None)
                k = f.end() if f else k + 1
        i += 1
    return items


def parse_blg(text):
    """Errors and warnings in a BibTeX log (.blg)."""
    items, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.startswith("Warning--"):
            at = re.match(r"^--line (\d+) of file (.+)$", nxt)
            items.append({"level": "warning", "message": "BibTeX: " + line[len("Warning--"):],
                          "file": at.group(2) if at else None, "line": int(at.group(1)) if at else None,
                          "detail": line + ("\n" + nxt if at else ""), "source": "bibtex"})
            continue
        e = re.match(r"^(.*)---line (\d+) of file (.+)$", line)
        if e and e.group(1).strip():
            items.append({"level": "error", "message": "BibTeX: " + e.group(1), "file": e.group(3),
                          "line": int(e.group(2)), "detail": "\n".join(lines[i:i + 4]), "source": "bibtex"})
            continue
        e = re.match(r"^(.*)---while reading file (.+)$", line)
        if e:
            items.append({"level": "error", "message": f"BibTeX: {e.group(1)} (while reading {e.group(2)})",
                          "file": e.group(2), "line": None, "detail": line, "source": "bibtex"})
    return items


_LOG_CACHE = {}


def compile_log(st):
    """The parsed log of the last build, with counts, cached until the log changes. The log is
    the one set with `laitex open --log`, else <name>.log next to the PDF; a .blg beside it is
    read too."""
    path = st.log_path()
    blg = os.path.splitext(path)[0] + ".blg"
    sig = []
    for f in (path, blg):
        try:
            s = os.stat(f)
            sig.append((s.st_mtime_ns, s.st_size))
        except OSError:
            sig.append(None)
    try:
        pdf_mtime = os.path.getmtime(st.pdf)
    except OSError:
        pdf_mtime = 0
    key = (path, tuple(sig))
    hit = _LOG_CACHE.get(st.pdf)
    if not hit or hit[0] != key:
        items = []
        if sig[0]:
            with open(path, "rb") as f:
                items += parse_tex_log(f.read(), os.path.dirname(path),
                                       os.path.splitext(os.path.basename(st.pdf))[0] + ".tex")
        if sig[1]:
            with open(blg, encoding="utf-8", errors="replace") as f:
                items += parse_blg(f.read())
        merged = {}
        for it in items:  # the same message from the same place counts once
            k = (it["level"], it["message"], it["file"], it["line"])
            if k in merged:
                merged[k]["count"] += 1
            else:
                it["id"] = hashlib.sha1(repr(k).encode()).hexdigest()[:8]
                it["count"] = 1
                merged[k] = it
        order = {lv: n for n, lv in enumerate(LOG_LEVELS)}
        items = sorted(merged.values(), key=lambda it: order[it["level"]])
        hit = (key, items)
        _LOG_CACHE[st.pdf] = hit
    items = hit[1]
    mtime = sig[0][0] / 1e9 if sig[0] else None
    return {"path": path, "exists": bool(sig[0]), "mtime": mtime,
            "stale": bool(mtime and pdf_mtime and mtime < pdf_mtime - 120),
            "counts": {lv: sum(1 for it in items if it["level"] == lv) for lv in LOG_LEVELS},
            "items": items}


def make_handler(st, agent_name, versions):
    mirror = Mirror(st)
    sources = Sources(st)
    builder = Builder(st, versions, sources)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)) or body is None:
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(n) or b"{}")

        def query(self):
            q = self.path.partition("?")[2]
            return {urllib.parse.unquote_plus(k): urllib.parse.unquote_plus(v)
                    for k, _, v in (kv.partition("=") for kv in q.split("&") if kv)}

        def allowed(self, write=False):
            """Only the viewer's own page may use the server. The Host must be this server, so
            a page on another domain cannot reach it by DNS rebinding. Writes also need the
            X-Laitex header: another site cannot send it to localhost without a CORS preflight,
            which this server never grants. This matters because chat messages reach an agent
            that can run commands."""
            port = self.server.server_address[1]
            hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
            if self.headers.get("Host") not in hosts:
                return False
            if write:
                origin = self.headers.get("Origin")
                if origin and origin not in {"http://" + h for h in hosts}:
                    return False
                if self.headers.get("X-Laitex") != "1":
                    return False
            return True

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error": "forbidden"})
            path, _, query = self.path.partition("?")
            if path == "/":
                with open(os.path.join(HERE, "viewer.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if path == "/pdf":
                try:
                    with open(st.pdf, "rb") as f:
                        return self.send(200, f.read(), "application/pdf")
                except OSError:
                    return self.send(404, {"error": "PDF not found"})
            if path == "/api/state":
                try:
                    mtime = os.path.getmtime(st.pdf)
                except OSError:
                    mtime = 0
                age = st.agent_age()
                return self.send(200, {
                    "name": os.path.basename(st.pdf),
                    "pdf": st.pdf,
                    "mtime": mtime,
                    "request": st.get_request(),
                    "listening": age is not None and age < LISTEN_WINDOW,
                    "agent_name": agent_name,
                    "log": {k: v for k, v in compile_log(st).items() if k != "items"},
                    "build": self.build_info(output=False),
                })
            if path == "/api/build":
                return self.send(200, self.build_info(output=True))
            if path == "/api/files":
                files = sources.stats()
                return self.send(200, {"root": sources.root(), "main": sources.main([f["path"] for f in files]),
                                       "files": files, "build": bool(st.build_cmd()),
                                       "synctex": synctex_for(st) is not None})
            if path == "/api/file":
                full = sources.resolve(self.query().get("path"))
                if not full:
                    return self.send(403, {"error": "not a source file of this project"})
                try:
                    return self.send(200, dict(read_source(full), path=self.query()["path"]))
                except OSError:
                    return self.send(404, {"error": "no such file"})
            if path == "/api/srcdiff":
                q = self.query()
                try:
                    a, b, ctx = int(q.get("a") or 0), int(q.get("b") or 0), min(int(q.get("ctx") or 3), 100000)
                except ValueError:
                    return self.send(400, {"error": "bad version"})
                only = sources.resolve(q["file"]) and q["file"] if q.get("file") else None
                return self.send(200, source_diff(st, versions, sources, a, b, ctx, only))
            # Sync answers null, not 404, when there is no SyncTeX file or nothing is found there:
            # the viewer then matches the text instead, and the browser logs no error.
            if path == "/api/synctex/edit":
                q, sx = self.query(), synctex_for(st)
                if not sx:
                    return self.send(200, None)
                try:
                    hit = sx.edit(int(q["page"]), float(q["x"]), float(q["y"]), lambda p: sources.rel(p) and is_source(p))
                except (KeyError, ValueError):
                    return self.send(400, {"error": "bad position"})
                return self.send(200, hit and {"file": sources.rel(hit["file"]), "line": hit["line"]})
            if path == "/api/synctex/view":
                q, sx = self.query(), synctex_for(st)
                full = sources.resolve(q.get("file"))
                if not sx or not full:
                    return self.send(200, None)
                try:
                    return self.send(200, sx.view(full, int(q.get("line") or 1)))
                except ValueError:
                    return self.send(400, {"error": "bad line"})
            if path == "/api/comments":
                with st.lock():
                    return self.send(200, load_anchored(st))
            if path == "/api/versions":
                return self.send(200, versions.public())
            if path == "/api/log":
                return self.send(200, compile_log(st))
            m = re.fullmatch(r"/version/(\d+)\.pdf", path)
            if m:
                try:
                    with open(versions.path(m.group(1)), "rb") as f:
                        return self.send(200, f.read(), "application/pdf")
                except OSError:
                    return self.send(404, {"error": "no such version"})
            if path == "/api/chat":
                q = dict(kv.partition("=")[::2] for kv in query.split("&") if kv)
                after = int(q.get("after") or 0) if q.get("epoch") == mirror.epoch else 0
                items = mirror.since(after)
                with st.lock():
                    pending = sum(1 for m in read_json(st.chat, []) if m["role"] == "user" and not m.get("delivered"))
                return self.send(200, {"epoch": mirror.epoch, "mirror": bool(mirror.path),
                                       "items": items, "pending": pending})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.allowed(write=True):
                return self.send(403, {"error": "forbidden"})
            m = re.fullmatch(r"/api/versions/(\d+)/stats", self.path)
            if m:
                versions.set_stats(int(m.group(1)), self.body())
                return self.send(200, {"ok": True})
            if self.path == "/api/file":
                return self.save_file(self.body())
            if self.path == "/api/build":
                if not builder.start():
                    return self.send(409, {"error": "no build command; start the viewer with laitex open --build"})
                return self.send(200, {"ok": True})
            if self.path == "/api/locate":
                data = self.body()
                return self.send(200, locate(sources, str(data.get("text") or "")[:4000], int(data.get("at") or 0)))
            if self.path == "/api/chat":
                data = self.body()
                text = (data.get("text") or "").strip()
                if not text:
                    return self.send(400, {"error": "empty message"})
                msg = {"id": uuid.uuid4().hex[:8], "role": "user", "text": text,
                       "time": utc_now(), "page": data.get("page")}
                for k in ("quote", "context", "version"):  # a question about selected text carries it
                    if isinstance(data.get(k), str) and data[k].strip():
                        msg[k] = data[k][:2000]
                msg.update({k: v for k, v in code_fields(data).items() if k != "range"})
                versions.seal_user()  # the user's edits so far are one version; the agent's work comes next
                with st.lock():
                    write_json(st.chat, read_json(st.chat, []) + [msg])
                return self.send(200, msg)
            if self.path == "/api/request":
                # A request to the agent; `laitex wait` on the agent side picks this file up.
                data = self.body()
                action = data.get("action", "comments")
                versions.seal_user()
                with st.lock():
                    n = sum(1 for c in st.load() if c.get("status") == "open")
                    req = {"id": uuid.uuid4().hex[:8], "time": now(), "action": action, "open": n}
                    if action == "rollback":
                        req["version"] = int(data.get("version") or 0)
                        req["name"] = versions.names().get(req["version"], "")
                    if action == "fix":  # the issues are taken from the log here, by id
                        log, ids = compile_log(st), set(map(str, data.get("ids") or []))
                        req["log"] = log["path"]
                        req["issues"] = [{k: it[k] for k in ("level", "message", "file", "line", "count", "detail")}
                                         for it in log["items"] if it["id"] in ids][:200]
                        if not req["issues"]:
                            return self.send(409, {"error": "those issues are no longer in the log"})
                    write_json(st.request, req)
                return self.send(200, req)
            if self.path != "/api/comments":
                return self.send(404, {"error": "not found"})
            data = self.body()
            item = {
                "id": uuid.uuid4().hex[:8],
                "created": now(),
                "page": data.get("page"),
                "quote": data.get("quote", ""),
                "context": data.get("context", ""),
                "comment": data.get("comment", ""),
                "rects": data.get("rects", []),
                "status": "open",
                "reply": "",
            }
            if isinstance(data.get("more"), list) and data["more"]:  # a selection that runs onto later pages
                item["more"] = [{"page": int(m["page"]), "rects": m["rects"]} for m in data["more"]
                                if isinstance(m, dict) and "page" in m and isinstance(m.get("rects"), list)]
                item["to_page"] = item["more"][-1]["page"] if item["more"] else item["page"]
            if isinstance(data.get("version"), str) and data.get("vn"):  # about an older version
                item["version"], item["vn"] = data["version"][:20], int(data["vn"])
            item.update(code_fields(data))  # a comment on the source rather than the page
            if item.get("file"):
                item.pop("page")
            with st.lock():
                items = st.load()
                items.append(item)
                st.save(items)
            self.send(200, item)

        def build_info(self, output):
            info = build_status(st)
            if not output:
                info.pop("output", None)
            info.update(configured=bool(st.build_cmd()), queued=builder.again,
                        running=builder.running or info.get("running", False))
            return info

        def save_file(self, data):
            """Save what the user typed in the code editor. `base` is the hash of the file as the
            editor last had it; if the file changed on disk since (the agent edited it), nothing
            is written and the editor gets the new text back, to merge and save again."""
            rel = data.get("path")
            full = sources.resolve(rel)
            text = data.get("text")
            if not full or not isinstance(text, str):
                return self.send(403, {"error": "not a source file of this project"})
            with _FILE_LOCK:
                try:
                    cur = read_source(full)
                except FileNotFoundError:
                    cur = None
                except OSError as e:
                    return self.send(500, {"error": str(e)})
                if cur and cur.get("text") is None:
                    return self.send(409, {"error": "this file cannot be edited here: " + cur["error"]})
                if (cur["hash"] if cur else None) != data.get("base"):
                    return self.send(409, dict(cur or {"text": None, "hash": None}, error="changed on disk"))
                try:
                    payload = text.encode((cur or {}).get("encoding") or "utf-8")
                except UnicodeEncodeError:
                    return self.send(400, {"error": "the file is saved in Latin-1, which cannot hold some of the new characters"})
                if len(payload) > MAX_EDIT:
                    return self.send(400, {"error": "too large"})
                try:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    write_source(full, payload)
                except OSError as e:
                    return self.send(500, {"error": str(e)})
                s = os.stat(full)
            note_edit(st, os.path.normpath(rel), cur["text"] if cur else None, text)
            with builder.lock:
                builder.files.add(os.path.normpath(rel))
            return self.send(200, {"hash": text_hash(payload), "mtime": s.st_mtime_ns, "size": s.st_size})

        def do_PATCH(self):
            if not self.allowed(write=True):
                return self.send(403, {"error": "forbidden"})
            if not self.path.startswith("/api/comments/"):
                return self.send(404, {"error": "not found"})
            cid = self.path.rsplit("/", 1)[1]
            data = self.body()
            with st.lock():
                items = st.load()
                for c in items:
                    if c["id"] == cid:
                        for k in ("status", "comment", "reply", "page", "rects", "context"):
                            if k in data:
                                c[k] = data[k]
                        if c.get("file") and "range" in data:  # the code moved under the comment
                            c.update(code_fields(dict(data, file=c["file"])))
                            if isinstance(data.get("quote"), str):
                                c["quote"] = data["quote"]
                        if str(data.get("followup") or "").strip():  # the user answers the agent's reply
                            c["thread"] = thread_of(c) + [{"role": "user", "text": data["followup"].strip(), "time": now()}]
                            c["status"] = "open"
                st.save(items)
            self.send(200, {"ok": True})

        def do_DELETE(self):
            if not self.allowed(write=True):
                return self.send(403, {"error": "forbidden"})
            if not self.path.startswith("/api/comments/"):
                return self.send(404, {"error": "not found"})
            cid = self.path.rsplit("/", 1)[1]
            with st.lock():
                st.save([c for c in st.load() if c["id"] != cid])
            self.send(200, {"ok": True})

    return Handler


def bind(st, port, agent_name, versions):
    """Bind to `port`, or the next free one."""
    for p in range(port, port + 50):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), make_handler(st, agent_name, versions))
        except OSError:
            continue
    sys.exit(f"laitex: no free port in {port}..{port + 49}")


def running_server(st):
    """URL of a live server for this PDF, or None."""
    info = read_json(st.server, None)
    if not info:
        return None
    url = f"http://localhost:{info['port']}"
    try:
        with urllib.request.urlopen(url + "/api/state", timeout=1) as r:
            if json.load(r).get("pdf") == st.pdf:
                return url
    except (OSError, ValueError):
        pass
    return None


# ---- commands -----------------------------------------------------------------

def cmd_serve(a):
    st = State(a.pdf)
    st.ensure()
    versions = Versions(st)
    threading.Thread(target=versions.watch, daemon=True).start()
    httpd = bind(st, a.port, a.agent_name, versions)
    port = httpd.server_address[1]
    write_json(st.server, {"port": port, "pid": os.getpid(), "pdf": st.pdf})
    print(f"laitex: http://localhost:{port}  ({st.pdf})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if read_json(st.server, {}).get("pid") == os.getpid():
            os.remove(st.server)


def set_log(st, path):
    st.ensure()
    write_json(st.logcfg, {"path": os.path.abspath(path)} if path else {})


def cmd_log(a):
    st = State(a.pdf)
    if a.set is not None:
        set_log(st, a.set)
        print(f"laitex: reading the log from {st.log_path()}")
        return
    log = compile_log(st)
    if not log["exists"]:
        sys.exit(f"laitex: no log at {log['path']}; point to it with `laitex log {a.pdf} --set path/to/doc.log`")
    json.dump(log, sys.stdout, indent=2, ensure_ascii=False)
    print()


def set_config(st, **kw):
    """Update config.json: a value of "" clears it, None leaves it as it is."""
    st.ensure()
    cfg = st.cfg()
    for k, v in kw.items():
        if v is None:
            continue
        if v:
            cfg[k] = v
        else:
            cfg.pop(k, None)
    write_json(st.config, cfg)


def cmd_open(a):
    st = State(a.pdf)
    if a.log:
        set_log(st, a.log)
    set_config(st, build=a.build, src=os.path.abspath(a.src) if a.src else a.src)
    if not os.path.exists(st.pdf):
        print(f"laitex: {st.pdf} does not exist yet; the viewer will show it once it is built.",
              file=sys.stderr)
    url = running_server(st)
    if not url:
        st.ensure()
        with open(st.log, "w") as log:
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "serve", st.pdf, "--port", str(a.port),
                 "--agent-name", a.agent_name],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(50):
            time.sleep(0.1)
            url = running_server(st)
            if url:
                break
        else:
            sys.exit(f"laitex: server did not start, see {st.log}")
    # Keep the build the review starts from, with its source, so it can be rolled back to.
    versions = Versions(st)
    versions.snapshot(settle=False)
    items = versions.list()
    if len(items) == 1 and not items[0].get("source"):
        with contextlib.suppress(RuntimeError, OSError):
            versions.attach(1, git_snapshot(versions, 1, "the first build kept"))
    if not a.no_browser:
        open_browser(url, a.app)
    print(url)


def open_browser(url, app):
    if app:
        # A chromeless window, if Chrome or Chromium is installed.
        if sys.platform == "darwin":
            for name in ("Google Chrome", "Chromium", "Microsoft Edge"):
                if subprocess.run(["open", "-na", name, "--args", f"--app={url}"],
                                  capture_output=True).returncode == 0:
                    return
        for exe in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            if shutil.which(exe):
                subprocess.Popen([exe, f"--app={url}"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return
    webbrowser.open(url)


def cmd_stop(a):
    st = State(a.pdf)
    info = read_json(st.server, None)
    if not info or not running_server(st):
        print("laitex: no server running for this PDF")
        return
    os.kill(info["pid"], signal.SIGTERM)
    with contextlib.suppress(OSError):
        os.remove(st.server)
    print("stopped")


def cmd_build(a):
    """Run the build command given with `laitex open --build`, as the viewer's Recompile does.
    The viewer shows the build, and it never runs at the same time as one the viewer started."""
    st = State(a.pdf)
    if not st.build_cmd():
        sys.exit(f"laitex: no build command; set one with `laitex open {a.pdf} --build \"...\"`")
    info = run_build(st, "agent", echo=not a.quiet)
    print(f"laitex: build {'succeeded' if info['ok'] else 'failed'} (exit {info['code']}) in "
          f"{info['finished'] - info['started']:.1f}s", file=sys.stderr)
    sys.exit(0 if info["ok"] else 1)


def cmd_wait(a):
    """Block until the reader sends a request or a chat message, then print it as JSON."""
    st = State(a.pdf)
    st.ensure()
    log = session_log()
    if log and read_json(st.transcript, {}).get("path") != log:
        write_json(st.transcript, {"path": log})  # the chat panel mirrors this session
    deadline = time.time() + a.timeout if a.timeout else None
    while True:
        st.ensure()  # the folder may have been deleted while waiting
        with open(st.agent, "w") as f:  # heartbeat: the viewer shows "agent listening"
            f.write(now())
        req = st.get_request()
        fresh = req and not req.get("taken")
        chat = [m for m in read_json(st.chat, []) if m["role"] == "user" and not m.get("delivered")]
        if fresh or chat:
            with st.lock():
                out = {"pdf": st.pdf, "request": None}
                root = st.src_root()
                if fresh:
                    req["taken"] = now()
                    write_json(st.request, req)
                    out["request"] = req
                    out["comments"] = [public(c, root) for c in load_anchored(st) if c.get("status") == "open"]
                if chat:
                    msgs = read_json(st.chat, [])
                    for m in msgs:
                        if m["role"] == "user" and not m.get("delivered"):
                            m["delivered"] = now()
                    write_json(st.chat, msgs)
                    keys = ("text", "page", "quote", "context", "version", "file", "line", "end_line", "time")
                    out["chat"] = [dict({k: m[k] for k in keys if m.get(k) is not None},
                                        **({"path": os.path.join(root, m["file"])} if m.get("file") else {}))
                                   for m in chat]
                    out["mirror"] = bool(read_json(st.transcript, {}).get("path"))
                edited = take_edits(st)
                if edited:  # what the user changed in the code editor since the last request
                    out["edited"] = edited
            json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
            print()
            return
        if deadline and time.time() > deadline:
            print(json.dumps({"pdf": st.pdf, "request": None, "timeout": True}))
            sys.exit(2)
        time.sleep(0.5)


def cmd_comments(a):
    st = State(a.pdf)
    with st.lock():
        items = load_anchored(st)
    if a.status != "all":
        items = [c for c in items if c.get("status") == a.status]
    json.dump([public(c, st.src_root()) for c in items], sys.stdout, indent=2, ensure_ascii=False)
    print()


def cmd_reply(a):
    st = State(a.pdf)
    text = sys.stdin.read().strip() if a.text == "-" else a.text
    with st.lock():
        items = st.load()
        hit = [c for c in items if c["id"] == a.id]
        if not hit:
            sys.exit(f"laitex: no comment with id {a.id}")
        thread = thread_of(hit[0])
        if thread and thread[-1]["role"] == "agent":
            thread.pop()  # a second reply before the user answers revises the first
        hit[0]["thread"] = thread + [{"role": "agent", "text": text, "time": now()}]
        hit[0]["reply"] = text
        hit[0]["status"] = a.status
        hit[0]["replied"] = now()
        st.save(items)
    print(f"{a.id}: {a.status}")


def cmd_say(a):
    """Post a message to the viewer's chat panel."""
    st = State(a.pdf)
    text = sys.stdin.read().strip() if a.text == "-" else a.text
    msg = {"id": uuid.uuid4().hex[:8], "role": "agent", "text": text, "time": utc_now()}
    with st.lock():
        write_json(st.chat, read_json(st.chat, []) + [msg])
    print("said")


def cmd_done(a):
    st = State(a.pdf)
    with st.lock():
        req = st.get_request()
        if req and req.get("taken"):  # a request sent meanwhile is still waiting for `wait`
            with contextlib.suppress(OSError):
                os.remove(st.request)
    print("done")


def cmd_checkpoint(a):
    """Seal the version in progress, so the history shows this chunk of work as one version,
    and record the source for rolling back to it."""
    st = State(a.pdf)
    versions = Versions(st)
    versions.snapshot(settle=False)  # the build just made, even if the server has not seen it yet
    user = versions.seal_user()  # no build of the agent's since the user's edits: those stay theirs
    if user:
        print(f"laitex: {versions.names()[user]} holds the user's edits from the viewer, kept as theirs", file=sys.stderr)
    items = versions.list()
    if not items or items[-1].get("sealed", True):
        released = release(versions)
        if released:
            print(f"marked {versions.names()[released]} (version {released}) as the committed version")
        else:
            print(f"laitex: no new build since {versions.names()[items[-1]['n']]}; nothing to keep" if items else "laitex: no build yet")
        return
    n = items[-1]["n"]
    try:
        source = git_snapshot(versions, n, a.message)
    except (RuntimeError, OSError) as e:
        print(f"laitex: could not record the source in git ({e}); it is kept without rollback", file=sys.stderr)
        source = None
    versions.seal(a.message, source)
    released = release(versions)
    name = versions.names()[n]
    print(f"kept {name} (version {n})" + (f", source in {source['ref']}" if source else ", no source snapshot (not a git repository)")
          + (f"; it is the committed version" if released == n else ""))


def release(versions):
    try:
        return versions.mark_release()
    except (RuntimeError, OSError):
        return None


def cmd_rollback(a):
    """Restore the source files of version n into the working tree, and forget the versions
    after it. HEAD and the index are left alone, so the result shows up as ordinary uncommitted
    changes."""
    st = State(a.pdf)
    versions = Versions(st)
    v = next((i for i in versions.list() if i["n"] == a.version), None)
    if not v:
        sys.exit(f"laitex: no version {a.version}")
    src = v.get("source")
    if not src:
        sys.exit(f"laitex: v{a.version} has no source snapshot (it was kept outside a git repository, "
                 "or before laitex recorded sources); roll back by other means")
    root, commit = src["root"], src["commit"]
    spec = [".", f":(exclude){src['pdf']}"]
    lines = git(root, "diff", "--name-status", "--no-renames", worktree_tree(root, src["pdf"]), commit).splitlines()
    tracked = set(git(root, "ls-files").splitlines())
    git(root, "restore", f"--source={commit}", "--worktree", "--", *spec)
    print(f"restored the source of v{a.version} ({commit[:10]}) in {root}")
    restored = [l for l in lines if l.split("\t", 1)[1] in tracked or not l.startswith("D")]
    left = [l.split("\t", 1)[1] for l in lines if l.startswith("D") and l.split("\t", 1)[1] not in tracked]
    print("\n".join(restored) if restored else "no files differed")
    if left:
        print("not in that version, untracked, left in place: " + ", ".join(left))
    names = versions.names()
    gone = versions.truncate(a.version)
    if gone:
        print("removed from the history: " + ", ".join(names[n] for n in gone))


def cmd_install_skill(a):
    if a.dir:
        base = os.path.expanduser(a.dir)
    elif a.project:
        base = os.path.join(os.getcwd(), ".claude", "skills")
    else:
        base = os.path.expanduser("~/.claude/skills")
    dest = os.path.join(base, "laitex")
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, "SKILL.md")
    if os.path.lexists(target):
        os.remove(target)
    # A symlink, so `git pull` in the laitex checkout also updates the skill.
    os.symlink(os.path.join(HERE, "SKILL.md"), target)
    print(f"installed {target} -> {os.path.join(HERE, 'SKILL.md')}")


def cmd_guide(a):
    """Print the agent instructions, for harnesses without skill support."""
    with open(os.path.join(HERE, "SKILL.md")) as f:
        sys.stdout.write(f.read().split("---", 2)[2].lstrip())


def main(argv=None):
    p = argparse.ArgumentParser(prog="laitex", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def pdf_cmd(name, fn, help):
        s = sub.add_parser(name, help=help)
        s.add_argument("pdf", help="path to the PDF")
        s.set_defaults(fn=fn)
        return s

    for name, fn, help in (("open", cmd_open, "start the viewer for a PDF and open it in the browser"),
                           ("serve", cmd_serve, "run the viewer in the foreground")):
        s = pdf_cmd(name, fn, help)
        s.add_argument("--port", type=int, default=int(os.environ.get("LAITEX_PORT", DEFAULT_PORT)))
        s.add_argument("--agent-name", default=os.environ.get("LAITEX_AGENT", "the agent"),
                       help='name shown on the buttons, e.g. Claude (default: "the agent")')
        if name == "open":
            s.add_argument("--no-browser", action="store_true", help="only start the server")
            s.add_argument("--log", help="the build's log, if it is not <name>.log next to the PDF")
            s.add_argument("--app", action="store_true", help="open a chromeless Chrome window")
            s.add_argument("--build", metavar="CMD", help="the shell command that builds the PDF, run in the source "
                           "folder by the viewer's Recompile and by `laitex build` (\"\" to remove it)")
            s.add_argument("--src", metavar="DIR", help="the folder of the source files, if it is not the PDF's "
                           "(\"\" for the PDF's)")
    pdf_cmd("stop", cmd_stop, "stop the viewer for a PDF")
    s = pdf_cmd("build", cmd_build, "run the build command set with `open --build`, as the viewer does")
    s.add_argument("-q", "--quiet", action="store_true", help="do not print the build's output")
    s = pdf_cmd("log", cmd_log, "print the errors and warnings of the last build as JSON")
    s.add_argument("--set", metavar="LOG", help="read the log from LOG from now on (\"\" for <name>.log next to the PDF)")
    s = pdf_cmd("wait", cmd_wait, "block until the reader sends a request; print it as JSON")
    s.add_argument("--timeout", type=float, default=0, help="seconds; exit 2 if nothing arrives")
    s = pdf_cmd("comments", cmd_comments, "print comments as JSON")
    s.add_argument("--status", default="open", choices=STATUSES + ("all",))
    s = pdf_cmd("reply", cmd_reply, "reply to a comment")
    s.add_argument("id")
    s.add_argument("text", help="reply text, or - to read it from stdin")
    s.add_argument("--status", default="resolved", choices=STATUSES,
                   help="resolved: change made (default); answered: reply only, e.g. a question")
    s = pdf_cmd("say", cmd_say, "answer in the viewer's chat panel")
    s.add_argument("text", help="message text, or - to read it from stdin")
    pdf_cmd("done", cmd_done, "mark the current request as handled")
    s = pdf_cmd("checkpoint", cmd_checkpoint, "seal the current build as one version, recording the source")
    s.add_argument("-m", "--message", default="", help="a one-line summary of the work, shown in the history")
    s = pdf_cmd("rollback", cmd_rollback, "restore the source of a version into the working tree")
    s.add_argument("version", type=int)
    sub.add_parser("guide", help="print the instructions for the agent").set_defaults(fn=cmd_guide)
    s = sub.add_parser("install-skill", help="install the agent skill (default: ~/.claude/skills)")
    s.add_argument("--project", action="store_true", help="install into ./.claude/skills")
    s.add_argument("--dir", help="install into DIR/laitex instead")
    s.set_defaults(fn=cmd_install_skill)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
