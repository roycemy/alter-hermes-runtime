# inbox/ - Instinct's drop box for Hermes

At the end of any real task, Instinct drops one small markdown file here:
`YYYY-MM-DD-short-slug.md`, three lines or so:

```
# <task name>
Did: <what shipped or changed, one line>
Next: <the obvious follow-up, or "none">
```

Hermes reads every `*.md` here (except this README) during its nightly run,
then leaves them in place. Old files can be pruned anytime; they are the
paper trail, not state. State lives in the four root memory files.
