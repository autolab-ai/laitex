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
2. Open the viewer: `laitex open path/to/doc.pdf`. It prints the URL. Its Log tab shows the
   errors and warnings of the last build, read from `doc.log` next to the PDF. If the build
   writes its log elsewhere (a build folder, a temporary copy), add `--log path/to/doc.log`; a
   `doc.blg` next to the log is read too. Tell the user it is open
   and that they can select text to comment and press Cmd+Enter (Ctrl+Enter) to send.
3. Wait for a request: `laitex wait path/to/doc.pdf`. It blocks until the user presses a
   button or writes in the chat panel, then prints JSON and exits. If your harness can run a command in the background and wake you
   when it exits (Claude Code: `run_in_background: true`), do that, so you stay free to talk
   to the user meanwhile. Otherwise run it in the foreground with `--timeout 600` and start it again
   on exit code 2 (timeout).
4. Handle the request (below). If it changed the document, rebuild and run
   `laitex checkpoint path/to/doc.pdf -m "<one-line summary>"` (see Versions). Then run
   `laitex done path/to/doc.pdf`; for a chat message alone (`request` is null), skip `done`.
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
  `.laitex-review/` folder (it ignores itself). Then run `laitex checkpoint doc.pdf -m "<the
  commit message>"`: it finds the version the commit holds and numbers it as the next release.
- `fix`: fix the compile errors and warnings in `request.issues`, which the user picked in the
  Log tab. Each has `level` (`error`, `warning`, or `box` for an overfull or underfull box),
  `message`, `file` and `line` when the log names them (relative to the log's folder), `count`,
  and `detail`, the lines of the log it came from. Fix the cause in the source, rebuild, and
  check the new log with `laitex log doc.pdf`, which prints the same list as JSON. Some
  warnings are better left alone (a font substitution, a box a fraction of a point too wide);
  say so instead of contorting the text. Then tell the user what you fixed and what you left,
  in the conversation, or with `laitex say` if the chat does not mirror your session.
- `rollback`: the user wants the document back as it was at version `request.version`, as if the
  later versions never happened. Run `laitex rollback doc.pdf <version>`, which restores that
  version's source files into the working tree, prints what changed, and removes the later
  versions from the history (with their source snapshots). Then rebuild; the build matches that
  version, so no new version appears (if it differs, checkpoint it). Tell the user what changed
  in the chat (or with `laitex say`). The command leaves
  untracked files created after that version in place and lists them; delete them only if you
  created them in this review. If it reports that the version has no source snapshot, restore it
  another way (for example from git history) or tell the user it cannot be done.

## Chat

The viewer has a chat panel. A message typed there arrives through `laitex wait` as

```json
{"pdf": "/abs/path/doc.pdf", "request": null,
 "chat": [{"text": "is the paper over the page limit?", "page": 9, "time": "2026-10-30T19:01:31.000Z"}],
 "mirror": true}
```

It is the user talking to you, the same as a message in the terminal: answer it, and do the work it
asks for. `page` is the page they were looking at. A message about text they selected also has
`quote` (the selected text) and `context` (the text around it, as in comments): read "this" or
"here" in the message as that passage. If they also pressed a button, `request` and
`comments` are filled in as usual; handle both.

- `mirror: true`: the panel shows your session transcript (Claude Code), so the user reads your
  normal reply there. Answer in the conversation as usual; do not also use `laitex say`.
- `mirror: false`: the user does not see your terminal. Post the answer with
  `laitex say doc.pdf "..."` (or `-` to read it from stdin).

Then wait again.

## Versions

Versions are named v0.1, v0.2, ... until one is committed to git, which becomes v1.0; the next
ones are v1.1, v1.2, ... until the next commit (v2.0). `laitex checkpoint` notices the commit:
run it after committing, even if nothing was rebuilt. `request.version` in a rollback is the
version's number, and `request.name` its name (v0.3), which is what the user sees.

The viewer's History tab shows one version per chunk of your work: each request, or each chat
task that changed the document. A rebuild starts a version marked "in progress" and later
rebuilds update it; `laitex checkpoint doc.pdf -m "Applied 3 comments on the intro"` closes it
with that summary. In a git repository it also records the source files of that moment in a
commit under `refs/laitex/`, which is on no branch, leaves the index and working tree alone, and
is not pushed, so the user can roll back to any version from the viewer.

Checkpoint after every chunk unless the user asks otherwise. If they ask for fewer versions (one
per several requests, only when they say so) or none, do that for the rest of the session. The
first build is kept when you run `laitex open`.

## Handling comments

- A comment or chat message with `version` (for example `"v0.2"`) was made on that older version in
  the History tab: its `quote` and `context` are from that build, which may differ from the current
  one, for example a sentence the user wants back.
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
- The user can answer your reply from the comment card. The comment then comes back open with a
  `thread`: your replies and their follow-ups, oldest first. Act on the last message, read with
  the rest of the thread, and reply as usual; your reply is added to the thread.
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
