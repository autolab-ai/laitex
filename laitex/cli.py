"""laitex: review a PDF in the browser and hand the comments to a coding agent.

The viewer and the agent share a small state folder next to the PDF
(.laitex-review/<name>/). The viewer writes comments and requests there; the agent reads
them with `laitex wait` and answers with `laitex reply` and `laitex done`.
The viewer itself never edits your files or runs git.
"""
import argparse
import contextlib
import glob
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
    Two builds count as the same when they differ only in timestamps and the file ID."""
    KEEP = 40

    def __init__(self, st):
        self.st = st
        self.key = hashlib.sha1(st.pdf.encode()).hexdigest()[:12]
        self.stem = os.path.splitext(os.path.basename(st.pdf))[0]
        self.dir = os.path.join(cache_root(), self.key + "-" + self.stem)
        self.index = os.path.join(self.dir, "versions.json")
        self.seen = None  # (mtime, size) of the last build looked at

    def list(self):
        return read_json(self.index, [])

    def path(self, n):
        return os.path.join(self.dir, f"v{int(n)}.pdf")

    @staticmethod
    def digest(data):
        data = re.sub(rb"/(CreationDate|ModDate)\s*\(D:[^)]*\)", b"", data)
        data = re.sub(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]", b"", data)
        return hashlib.sha1(data).hexdigest()

    def snapshot(self, settle=True):
        """Keep the current build if it is complete and new. The server calls this about once a
        second (settle: skip a file written in the last second); `checkpoint` calls it directly."""
        try:
            stat = os.stat(self.st.pdf)
        except OSError:
            return
        sig = (stat.st_mtime, stat.st_size)
        if sig == self.seen or (settle and time.time() - stat.st_mtime < 1):
            return  # unchanged, or possibly still being written
        with open(self.st.pdf, "rb") as f:
            data = f.read()
        if b"%%EOF" not in data[-2048:] or len(data) != stat.st_size:
            return  # an unfinished write; look again later
        self.seen = sig
        digest = self.digest(data)
        with self.st.lock():
            items = self.list()
            if items and items[-1]["digest"] == digest:
                return
            os.makedirs(self.dir, exist_ok=True)
            open_ = items and not items[-1].get("sealed", True)
            n = items[-1]["n"] if open_ else (items[-1]["n"] + 1 if items else 1)
            with open(self.path(n) + ".tmp", "wb") as f:
                f.write(data)
            os.replace(self.path(n) + ".tmp", self.path(n))
            item = {"n": n, "time": utc_now(), "built": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(stat.st_mtime)),
                    "size": len(data), "digest": digest, "sealed": not items, "label": "" if items else "the first build kept"}
            if open_:
                items[-1] = item  # the version in progress gets the newer build
            else:
                items.append(item)
            for old in items[:-self.KEEP]:
                with contextlib.suppress(OSError):
                    os.remove(self.path(old["n"]))
            write_json(self.index, items[-self.KEEP:])

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
        v1.0; then v1.1, v1.2, ... until the next commit (v2.0), and so on."""
        major, minor, out = 0, 0, {}
        for i in items if items is not None else self.list():
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
                 "source": bool(i.get("source")), "release": bool(i.get("release")), "stats": i.get("stats")}
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
    pdf = os.path.relpath(versions.st.pdf, root)
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


def thread_of(c):
    """The conversation after a comment: the agent's replies and the user's follow-ups.
    Comments answered before threads existed have only `reply`."""
    if c.get("thread"):
        return list(c["thread"])
    return [{"role": "agent", "text": c["reply"], "time": c.get("replied", "")}] if c.get("reply") else []


def public(c):
    """A comment without the highlight geometry, which agents do not need."""
    return {k: v for k, v in c.items() if k not in ("rects", "more")}


# ---- HTTP server ------------------------------------------------------------

def make_handler(st, agent_name, versions):
    mirror = Mirror(st)

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
                })
            if path == "/api/comments":
                with st.lock():
                    return self.send(200, st.load())
            if path == "/api/versions":
                return self.send(200, versions.public())
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
                with st.lock():
                    write_json(st.chat, read_json(st.chat, []) + [msg])
                return self.send(200, msg)
            if self.path == "/api/request":
                # A request to the agent. The viewer never edits files or runs git;
                # `laitex wait` on the agent side picks this file up.
                data = self.body()
                action = data.get("action", "comments")
                with st.lock():
                    n = sum(1 for c in st.load() if c.get("status") == "open")
                    req = {"id": uuid.uuid4().hex[:8], "time": now(), "action": action, "open": n}
                    if action == "rollback":
                        req["version"] = int(data.get("version") or 0)
                        req["name"] = versions.names().get(req["version"], "")
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
            with st.lock():
                items = st.load()
                items.append(item)
                st.save(items)
            self.send(200, item)

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


def cmd_open(a):
    st = State(a.pdf)
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
                if fresh:
                    req["taken"] = now()
                    write_json(st.request, req)
                    out["request"] = req
                    out["comments"] = [public(c) for c in st.load() if c.get("status") == "open"]
                if chat:
                    msgs = read_json(st.chat, [])
                    for m in msgs:
                        if m["role"] == "user" and not m.get("delivered"):
                            m["delivered"] = now()
                    write_json(st.chat, msgs)
                    out["chat"] = [{k: m[k] for k in ("text", "page", "quote", "context", "version", "time") if m.get(k) is not None}
                                   for m in chat]
                    out["mirror"] = bool(read_json(st.transcript, {}).get("path"))
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
        items = st.load()
    if a.status != "all":
        items = [c for c in items if c.get("status") == a.status]
    json.dump([public(c) for c in items], sys.stdout, indent=2, ensure_ascii=False)
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
            s.add_argument("--app", action="store_true", help="open a chromeless Chrome window")
    pdf_cmd("stop", cmd_stop, "stop the viewer for a PDF")
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
