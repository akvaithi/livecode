# livecode

Work on the same repo, on the same branch, at the same time, without pulling and pushing by hand.

Each person runs `livecode start` in their clone. In the background it:

1. **autosaves** your edits as a commit once files stop changing for ~3 seconds,
2. **rebases** them onto whatever your teammate pushed,
3. **pushes**, and keeps pulling their changes in every few seconds.

It only uses git and the filesystem, so it works the same whether you're editing in the Claude
app, Claude in VS Code, Cursor, vim, or anything else. It never force-pushes.

## Install (each person, once)

Needs Python 3.8+ and git. No other dependencies.

```bash
uv tool install git+https://github.com/akvaithi/livecode
```

(`pipx install git+https://github.com/akvaithi/livecode` works too.)

## Use

```bash
cd your-repo
livecode init     # once per repo, by one person: adds Claude notes + auto-start hook
livecode start    # each person, in their own clone
livecode status   # running? synced? any conflict?
livecode stop
livecode log      # what it has been doing
```

`livecode init` adds a short section to `CLAUDE.md` (and `AGENTS.md` if you have one) telling
Claude that files sync live, not to run git commit/pull/push itself, and how to resolve conflicts.
It also adds a `SessionStart` hook in `.claude/settings.json`, so opening the repo in any Claude
Code surface starts sync automatically (skip with `--no-claude-hook`).

## When you both edit the same lines

Edits to different files, or different parts of the same file, merge on their own. If you both
change the same lines, the file gets normal conflict markers:

```
<<<<<<< HEAD
your version
=======
their version
>>>>>>> refs/remotes/origin/main
```

You get a desktop notification and sync pauses for you. Edit the file into what it should be
(or tell Claude "resolve the conflict markers in X"). As soon as no markers remain, livecode
commits the merge and resumes. Nothing to run.

## When it pauses on its own

It steps aside, rather than fighting you, if you check out another branch, detach HEAD, or start
your own rebase/merge/cherry-pick. `livecode status` says why; it picks up again when you're back.

## History

Autosave commits look like `livecode(arun): src/app.py, README.md`. To read history without them:

```bash
git log --invert-grep --grep='^livecode('
```

## Tests

```bash
uv run --no-project --with pytest pytest -q tests
```
