---
name: laitex
description: Collect the user's feedback on a PDF with the laitex viewer. The user highlights text and comments in the browser; you apply the comments to the source, rebuild the PDF and reply to each one. Use when the user wants to review, read through, proofread or give feedback on a PDF (a paper, thesis, report, slides, LaTeX or Typst output), or says "open it in laitex".
---

# laitex: PDF review loop with your agent

laitex is a local PDF viewer. The user selects text and leaves comments; when they press
"Send", a request reaches you through `laitex wait`. You change the source, rebuild the
PDF (the viewer reloads it in place), and answer each comment with `laitex reply`.
The viewer also has a code editor, so the user can edit the source next to the PDF and comment
on the code. Apart from what the user types there, the viewer changes nothing in the project and
never runs git commands that touch the branches. Acting on comments, chat and requests is your job.

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
2. Open the viewer with the build command: `laitex open path/to/doc.pdf --build "<command>"`.
   It prints the URL. The command runs in the source folder when the user presses Recompile (or
   saves, with auto build on) after editing the code in the viewer, so it must build without
   asking anything and should write SyncTeX data, which lets the user jump between the PDF and
   the code: `latexmk -pdf -synctex=1 -interaction=nonstopmode doc.tex`,
   `tectonic --synctex --keep-logs doc.tex`, `typst compile doc.typ`, or the project's
   `make`. If the source is not in the PDF's folder (the PDF is in `build/`, say), add
   `--src path/to/source`. From then on, build with `laitex build path/to/doc.pdf`: it runs the
   same command, prints its output, exits non-zero on failure, and never runs at the same time
   as a build the viewer started, which would mix up the auxiliary files.
   The Log tab shows the errors and warnings of the last build, read from `doc.log` next to the
   PDF. If the build writes its log elsewhere (a build folder, a temporary copy), add
   `--log path/to/doc.log`; a `doc.blg` next to the log is read too. Tell the user it is open
   and that they can select text to comment and press Cmd+Enter (Ctrl+Enter) to send, and that
   the toggle at the top shows the code.
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
    },
    {
      "id": "7c1d02aa",
      "file": "sections/intro.tex",
      "path": "/abs/path/sections/intro.tex",
      "line": 42,
      "end_line": 43,
      "quote": "the exact source text the user selected",
      "context": "a few lines before [[the selected text]] and after",
      "comment": "what the user wants",
      "status": "open",
      "reply": ""
    }
  ],
  "edited": [
    {"file": "doc.tex", "path": "/abs/path/doc.tex", "new": false,
     "diff": "--- a/doc.tex\n+++ b/doc.tex\n@@ -28,5 +28,5 @@\n ..."}
  ]
}
```

A comment with `file` is on the source, made in the viewer's code editor: `quote` is the text the
user selected, which starts on line `line` and ends on `end_line` of that file (both kept up to
date as the file changes), so find it there directly. `edited`, when
present, lists the files the user changed in the code editor since the last request, with a
diff of their changes (it can come with a chat message too). Their edits are deliberate: read
those files again before you change them, build on the edits, and do not undo them unless asked.

`action` is one of:

- `comments`: apply the open comments listed in `comments`.
- `pull`: pull the latest changes from git (`git pull --rebase --autostash`, so the user's
  uncommitted edits stay). If a conflict stops it, resolve it keeping both sides' intent where
  you can, and ask in the chat where you cannot. Rebuild the PDF if the source changed, then
  `laitex checkpoint doc.pdf -m "Pulled: <what came in>"` and `laitex done`. Tell the user what came
  in, in the conversation or with `laitex say`.
- `commit`: commit the source changes made in this review, if the project is a git repository,
  and do not push. Use a message that summarizes the edits. Do not commit the
  `.laitex-review/` folder (it ignores itself). Then run `laitex checkpoint doc.pdf -m "<the
  commit message>"`: it finds the version the commit holds and numbers it as the next release.
- `push`: push the current branch. Do not commit first: if there are uncommitted changes, push
  what is committed and say what was left out. If the push is rejected because the remote moved
  on, pull with `--rebase`, rebuild if the source changed, and push again. Say what was pushed
  and where.
- `fix`: fix the compile errors and warnings in `request.issues`, which the user picked in the
  Log tab. Each has `level` (`error`, `warning`, or `box` for an overfull or underfull box),
  `message`, `file` and `line` when the log names them (relative to the log's folder), `count`,
  and `detail`, the lines of the log it came from. Fix the cause in the source, rebuild, and
  check the new log with `laitex log doc.pdf`, which prints the same list as JSON. Some
  warnings are better left alone (a font substitution, a box a fraction of a point too wide);
  say so instead of contorting the text. Then tell the user what you fixed and what you left,
  in the conversation, or with `laitex say` if the chat does not mirror your session.
- `build`: the user edited the source in the viewer and pressed Rebuild, and there is no build
  command for the viewer to run. Rebuild the PDF; if you know the command, also give it to the
  viewer with `laitex open doc.pdf --build "<command>"` so it builds by itself next time. Then
  `laitex checkpoint doc.pdf -m "The user's edits"` and `laitex done`.
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
"here" in the message as that passage. A message about code selected in the editor has `file`,
`path`, `line` and `end_line` instead of `page`. If they also pressed a button, `request` and
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
task that changed the document. Builds the viewer makes from the user's own edits are a version
of theirs, labelled as such, which ends when they send you a request or your next build arrives.
If your version is still in progress when the user builds, their edits join it instead, marked
"with your edits", and your checkpoint closes both; checkpoint before you wait again to keep them apart. A rebuild starts a version marked "in progress" and later
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

## The user edits too

The user may type in the viewer's code editor while you work; it saves a moment after they stop.
Read a file again right before you change it, and change it with targeted edits (replace a
passage) rather than by writing out a copy you read earlier, which would erase what they typed
meanwhile. If they had unsaved text when you saved, the viewer merges your change into theirs.
Where you both changed the same words, the viewer saves nothing until the user picks one side, so
the file keeps your version for now and may get theirs back; if that happens, their `edited` diff
says so, and you take it as their decision.

Comments are stored in `.laitex-review/<name>/comments.json` next to the PDF. Use the commands
above instead of editing that file.

## Improving laitex

`~/.laitex` is an ordinary clone of https://github.com/autolab-ai/laitex, and you may
change it. If you hit a bug or the user wants a feature, fix it there. Keep the viewer's
rule: it records comments and requests and saves what the user types in its code editor, and
every other change to the project is yours.
If the change would help others, ask the user whether to open a pull request upstream.
If the tool was useful, ask the user whether they would like to star the repository
(`gh api -X PUT user/starred/autolab-ai/laitex`, or the GitHub page). Never star or open a PR without
their OK.
