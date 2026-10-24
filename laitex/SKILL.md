---
name: laitex
description: Collect the user's feedback on a PDF with the laitex viewer. The user highlights text and comments in the browser; you apply the comments to the source, rebuild the PDF and reply to each one. Use when the user wants to review, read through, proofread or give feedback on a PDF (a paper, thesis, report, slides, LaTeX or Typst output), or says "open it in laitex".
---

# laitex: PDF review loop with your agent

laitex is a local PDF viewer. The user selects text and leaves comments; when they press
"Send", a request reaches you through `laitex wait`. You change the source, rebuild the
PDF (the viewer reloads it in place), and answer each comment with `laitex reply`.
The viewer never edits files or runs git. Everything that changes the project is your job.

## Setup

laitex lives in a git checkout at `~/.laitex`. Get it, or update it:

```sh
git clone https://github.com/autolab-ai/laitex ~/.laitex 2>/dev/null || git -C ~/.laitex pull --ff-only
```

Below, `laitex` means `~/.laitex/bin/laitex` (Python 3.8+, no dependencies), unless a
`laitex` command is already on PATH. If your harness supports skills, install this file as
one so later sessions find it: `~/.laitex/bin/laitex install-skill` (Claude Code), or
`install-skill --dir <your skills folder>` for other agents. It symlinks, so `git pull`
keeps the skill current.

## Loop

1. Find the PDF and how it is built (a Makefile, `latexmk`, `typst compile`, a build script).
   Build it once if it is missing or stale.
2. Open the viewer: `laitex open path/to/doc.pdf`. It prints the URL. Tell the user it is open
   and that they can select text to comment and press Cmd+Enter (Ctrl+Enter) to send.
3. Wait for a request: `laitex wait path/to/doc.pdf`. It blocks until the user presses a
   button, then prints JSON and exits. If your harness can run a command in the background and wake you
   when it exits (Claude Code: `run_in_background: true`), do that, so you stay free to talk
   to the user meanwhile. Otherwise run it in the foreground with `--timeout 600` and start it again
   on exit code 2 (timeout).
4. Handle the request (below), then run `laitex done path/to/doc.pdf`.
5. Go back to step 3. Stop when the user says the review is over, then
   `laitex stop path/to/doc.pdf` if they want the viewer closed.

While `wait` is running the viewer shows "The agent is listening". Requests sent while you are
not listening are kept and picked up by the next `wait`.

## Request format

```json
{
  "pdf": "/abs/path/doc.pdf",
  "request": {"id": "a1b2c3d4", "time": "2026-10-24 19:40:00", "action": "comments", "open": 2},
  "comments": [
    {
      "id": "49025908",
      "page": 4,
      "quote": "the selected text, as extracted from the PDF",
      "context": "about 150 characters before [[the selected text]] and after",
      "comment": "what the user wants",
      "status": "open",
      "reply": "",
      "created": "2026-10-24 19:39:12"
    }
  ]
}
```

`action` is one of:

- `comments`: apply the open comments listed in `comments`.
- `commit`: commit the source changes made in this review and push them, if the project is
  a git repository. Use a message that summarizes the edits. Do not commit the
  `.laitex-review/` folder (it ignores itself).

## Handling comments

- Locate each comment in the source with `quote` and `context`. Text extracted from a PDF
  differs from the source: ligatures, hyphenation at line ends, math and LaTeX macros. Search
  for a distinctive few words, not the whole quote.
- Make the change the comment asks for. If a comment is a question, answer it in the reply.
  If it is unclear or you disagree, reply without changing anything.
- Rebuild the PDF after the edits so the user sees them. One rebuild for all comments is enough.
- Reply to every comment, after the rebuild:

  ```
  laitex reply doc.pdf 49025908 "Rewrote the sentence: 'Each section derives ...'"
  laitex reply doc.pdf 7f3e2a10 "Which figure do you mean, 3 or 4?" --status answered
  ```

  `--status resolved` (the default) means you made the change. `--status answered` means you
  only replied, for example with a question; the user can edit the comment, which reopens it.
  Pass `-` as the text to read a long reply from stdin.
- Keep replies short: what changed, quoting the new wording when it is a rewrite.
- `laitex comments doc.pdf --status all` lists every comment, including resolved ones, if you
  need earlier context.

Comments are stored in `.laitex-review/<name>/comments.json` next to the PDF. Use the commands
above instead of editing that file.

## Improving laitex

`~/.laitex` is an ordinary clone of https://github.com/autolab-ai/laitex, and you may
change it. If you hit a bug or the user wants a feature, fix it there. Keep the viewer's
rule: it records comments and requests, and never edits the user's files or runs git.
If the change would help others, ask the user whether to open a pull request upstream.
If the tool was useful, ask the user whether they would like to star the repository
(`gh api -X PUT user/starred/autolab-ai/laitex`, or the GitHub page). Never star or open a PR without
their OK.
