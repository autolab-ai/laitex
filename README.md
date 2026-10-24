<div align="center">

# pdfloop

**Review a PDF in your browser. Your coding agent applies the comments.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](pyproject.toml)
[![Claude Code skill](https://img.shields.io/badge/Claude%20Code-skill-D97757)](pdfloop/SKILL.md)
[![Works with LaTeX and Typst](https://img.shields.io/badge/LaTeX%20%7C%20Typst%20%7C%20any%20PDF-supported-555)](#how-the-agent-connects)
[![GitHub stars](https://img.shields.io/github/stars/autolab-ai/pdfloop?style=social)](https://github.com/autolab-ai/pdfloop)

```sh
curl -LsSf https://raw.githubusercontent.com/autolab-ai/pdfloop/main/install.sh | sh
```

[Quick start](#quick-start) · [Using the viewer](#using-the-viewer) · [How the agent connects](#how-the-agent-connects) · [Other agents](#other-agents)

</div>

You read the compiled PDF; the agent edits the LaTeX, Typst or Markdown source. Without a
tool in between, every comment has to be copied out of the PDF, found again in the source
and explained in a chat window. With pdfloop you select a sentence on the page, type what
you want, and press Cmd+Enter. The agent gets each comment with the quoted text and its
surroundings, edits the source, rebuilds, and answers next to your comment. The PDF reloads
in place, at the spot you were reading.

![Writing a comment in pdfloop while the agent's replies show in the sidebar](docs/review.png)

- One Python file and one HTML page. No dependencies beyond Python 3.8+.
- Works with any agent that can run shell commands. Ships with a skill for Claude Code.
- The viewer never edits your files and never runs git. It only records comments and
  requests; the agent does the work, and you can see every change it makes.

## Quick start

Paste this into Claude Code (or any agent that can run shell commands):

```text
Install pdfloop from https://github.com/autolab-ai/pdfloop (run its install.sh),
then open paper.pdf in it so I can review it.
```

Or install it yourself with one command. It installs the `pdfloop` CLI with uv, pipx or pip
and adds the skill to `~/.claude/skills`:

```sh
curl -LsSf https://raw.githubusercontent.com/autolab-ai/pdfloop/main/install.sh | sh
```

After that, one sentence starts a review:

> Open paper.pdf in pdfloop, I want to review it.

The agent opens the viewer, starts listening, and waits. Select text, write a comment,
press Enter to save it. When you have a few, press Cmd+Enter to send them all. The
sidebar shows "Claude is listening" while the agent waits for you, and each comment gets a
reply once it is handled.

To try the viewer without an agent, open the example paper:

```sh
git clone https://github.com/autolab-ai/pdfloop && cd pdfloop
pdfloop open examples/paper/paper.pdf
```

Manual install, if you prefer it:

```sh
uv tool install git+https://github.com/autolab-ai/pdfloop   # or pipx install / pip install
pdfloop install-skill
```

## Using the viewer

![Open comments waiting to be sent](docs/comments.png)

| Keys | Action |
| --- | --- |
| select text | open the comment box |
| Enter | save the comment |
| Shift+Enter | new line in a comment |
| Esc | cancel the comment or clear the selection |
| Cmd+Enter | send all open comments to the agent |
| Cmd+Shift+Enter | ask the agent to commit and push |
| Cmd + / Cmd − / Cmd 0 | zoom in, out, fit (trackpad pinch works too) |
| Alt+← | go back after following a link |

On Linux and Windows use Ctrl instead of Cmd. Both requests are also buttons in the sidebar.

The viewer also:

- reloads the PDF when the file changes and keeps your scroll position and zoom;
- makes citations and section, figure and equation references clickable, with a preview of
  the target on hover;
- jumps to a comment when you click it in the sidebar, where you can also edit, resolve,
  reopen or delete it. An edited comment goes to the agent again with the next send.

## How the agent connects

The viewer and the agent share a folder next to the PDF, `.pdfloop/<name>/`, which ignores
itself in git. The agent does not touch that folder directly. It uses five commands:

| Command | What it does |
| --- | --- |
| `pdfloop open doc.pdf` | start the viewer (once per PDF) and open the browser; prints the URL |
| `pdfloop wait doc.pdf` | block until you press Send or Commit, then print the request as JSON |
| `pdfloop reply doc.pdf ID "text"` | answer a comment and mark it resolved (`--status answered` to only reply) |
| `pdfloop done doc.pdf` | tell the viewer the request is handled |
| `pdfloop comments doc.pdf` | list open comments as JSON (`--status all` for every comment) |

`pdfloop wait` prints:

```json
{
  "pdf": "/home/me/paper/paper.pdf",
  "request": {"id": "a1b2c3d4", "time": "2026-10-24 19:40:00", "action": "comments", "open": 1},
  "comments": [
    {
      "id": "67858717",
      "page": 1,
      "quote": "They cannot see what the reader sees.",
      "context": "...rewrite a paragraph in a different tone. [[They cannot see what the reader sees.]] The review loop closes that gap...",
      "comment": "Too blunt. Say what is missing: the agent edits the source but never sees the rendered page.",
      "status": "open",
      "reply": ""
    }
  ]
}
```

`action` is `comments` (apply the open comments) or `commit` (commit and push the edits from
this review). One round looks like this:

```sh
pdfloop wait paper.pdf            # returns when the reader presses Send
# ...edit paper.tex, rebuild paper.pdf...
pdfloop reply paper.pdf 67858717 "Now: 'They edit the source but never see the rendered page.'"
pdfloop reply paper.pdf 84d5a63c "Which figure, 3 or 4?" --status answered
pdfloop done paper.pdf
pdfloop wait paper.pdf            # next round
```

A comment is `open` until the agent handles it, then `resolved` (the change is made) or
`answered` (the agent only replied, for example with a question). Editing a comment in the
viewer sets it back to `open`.

In Claude Code the skill runs `pdfloop wait` as a background command, so Claude wakes up
when you press Send and you can keep talking to it in the meantime. Agents without
background commands run `pdfloop wait --timeout 600` in a loop.

### Other agents

The skill is a plain [`SKILL.md`](pdfloop/SKILL.md) file. Install it wherever your agent
looks for skills, or paste it into the agent's instructions:

```sh
pdfloop install-skill --project            # ./.claude/skills/pdfloop
pdfloop install-skill --dir ~/.codex/skills
```

## Options

```
pdfloop open doc.pdf [--port 8765] [--app] [--no-browser] [--agent-name NAME]
```

- `--port` is the first port to try. If it is taken, the next free one is used. Also `PDFLOOP_PORT`.
- `--app` opens a separate Chrome window without tabs and address bar.
- `--agent-name` sets the name on the buttons, "Claude" by default. Also `PDFLOOP_AGENT`.

`pdfloop serve doc.pdf` runs the server in the foreground and `pdfloop stop doc.pdf` stops
it. The server listens on 127.0.0.1 only. PDF.js is loaded from cdnjs, so the browser needs
internet access to load the viewer.

## License

MIT. See [LICENSE](LICENSE).
