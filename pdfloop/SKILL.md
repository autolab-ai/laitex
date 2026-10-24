---
name: pdfloop
description: Collect the user's feedback on a PDF with the pdfloop viewer. The user highlights text and comments in the browser; you apply the comments to the source, rebuild the PDF and reply to each one. Use when the user wants to review, read through, proofread or give feedback on a PDF (a paper, thesis, report, slides, LaTeX or Typst output), or says "open it in pdfloop".
---

# pdfloop: PDF review loop

pdfloop is a local PDF viewer. The user selects text and leaves comments; when they press
"Send", a request reaches you through `pdfloop wait`. You change the source, rebuild the
PDF (the viewer reloads it in place), and answer each comment with `pdfloop reply`.
The viewer never edits files or runs git. Everything that changes the project is your job.

If `pdfloop` is not on PATH, install it with
`uv tool install git+https://github.com/autolab-ai/pdfloop` (or `pipx install ...` / `pip install ...`).

## Loop

1. Find the PDF and how it is built (a Makefile, `latexmk`, `typst compile`, a build script).
   Build it once if it is missing or stale.
2. Open the viewer: `pdfloop open path/to/doc.pdf`. It prints the URL. Tell the user it is open
   and that they can select text to comment and press Cmd+Enter (Ctrl+Enter) to send.
3. Wait for a request: `pdfloop wait path/to/doc.pdf`. It blocks until the user presses a
   button, then prints JSON and exits. In Claude Code run it with `run_in_background: true`
   so you are woken when it exits and stay free to talk to the user meanwhile. Without
   background commands, run it in the foreground with `--timeout 600` and start it again
   on exit code 2 (timeout).
4. Handle the request (below), then run `pdfloop done path/to/doc.pdf`.
5. Go back to step 3. Stop when the user says the review is over, then
   `pdfloop stop path/to/doc.pdf` if they want the viewer closed.

While `wait` is running the viewer shows "Claude is listening". Requests sent while you are
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
  `.pdfloop/` folder (it ignores itself).

## Handling comments

- Locate each comment in the source with `quote` and `context`. Text extracted from a PDF
  differs from the source: ligatures, hyphenation at line ends, math and LaTeX macros. Search
  for a distinctive few words, not the whole quote.
- Make the change the comment asks for. If a comment is a question, answer it in the reply.
  If it is unclear or you disagree, reply without changing anything.
- Rebuild the PDF after the edits so the user sees them. One rebuild for all comments is enough.
- Reply to every comment, after the rebuild:

  ```
  pdfloop reply doc.pdf 49025908 "Rewrote the sentence: 'Each section derives ...'"
  pdfloop reply doc.pdf 7f3e2a10 "Which figure do you mean, 3 or 4?" --status answered
  ```

  `--status resolved` (the default) means you made the change. `--status answered` means you
  only replied, for example with a question; the user can edit the comment, which reopens it.
  Pass `-` as the text to read a long reply from stdin.
- Keep replies short: what changed, quoting the new wording when it is a rewrite.
- `pdfloop comments doc.pdf --status all` lists every comment, including resolved ones, if you
  need earlier context.

Comments are stored in `.pdfloop/<name>/comments.json` next to the PDF. Use the commands
above instead of editing that file.
