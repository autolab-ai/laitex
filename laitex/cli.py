"""laitex: review a PDF in the browser and hand the comments to a coding agent.

The viewer and the agent share a small state folder next to the PDF
(.laitex-review/<name>/). The viewer writes comments and requests there; the agent reads
them with `laitex wait` and answers with `laitex reply` and `laitex done`.
The viewer itself never edits your files or runs git.
"""
import argparse
import contextlib
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
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


def public(c):
    """A comment without the highlight geometry, which agents do not need."""
    return {k: v for k, v in c.items() if k != "rects"}


# ---- HTTP server ------------------------------------------------------------

def make_handler(st, agent_name):
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
            if self.path == "/api/chat":
                data = self.body()
                text = (data.get("text") or "").strip()
                if not text:
                    return self.send(400, {"error": "empty message"})
                msg = {"id": uuid.uuid4().hex[:8], "role": "user", "text": text,
                       "time": utc_now(), "page": data.get("page")}
                with st.lock():
                    write_json(st.chat, read_json(st.chat, []) + [msg])
                return self.send(200, msg)
            if self.path == "/api/request":
                # A request to the agent. The viewer never edits files or runs git;
                # `laitex wait` on the agent side picks this file up.
                action = self.body().get("action", "comments")
                with st.lock():
                    n = sum(1 for c in st.load() if c.get("status") == "open")
                    req = {"id": uuid.uuid4().hex[:8], "time": now(), "action": action, "open": n}
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


def bind(st, port, agent_name):
    """Bind to `port`, or the next free one."""
    for p in range(port, port + 50):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), make_handler(st, agent_name))
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
    httpd = bind(st, a.port, a.agent_name)
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
                    out["chat"] = [{k: m[k] for k in ("text", "page", "time") if m.get(k) is not None}
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
    with st.lock(), contextlib.suppress(OSError):
        os.remove(st.request)
    print("done")


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
    sub.add_parser("guide", help="print the instructions for the agent").set_defaults(fn=cmd_guide)
    s = sub.add_parser("install-skill", help="install the agent skill (default: ~/.claude/skills)")
    s.add_argument("--project", action="store_true", help="install into ./.claude/skills")
    s.add_argument("--dir", help="install into DIR/laitex instead")
    s.set_defaults(fn=cmd_install_skill)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
