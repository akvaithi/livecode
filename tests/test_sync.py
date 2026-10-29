"""Two clones of one bare repo, standing in for you and your cofounder, driven tick by tick."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import livecode  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def clone(bare, dest, name):
    git(bare.parent, "clone", "-q", str(bare), str(dest))
    git(dest, "config", "user.name", name)
    git(dest, "config", "user.email", f"{name}@example.com")
    git(dest, "config", "commit.gpgsign", "false")
    return dest


@pytest.fixture
def pair(tmp_path):
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = clone(bare, tmp_path / "seed", "seed")
    (seed / "shared.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)))
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "-u", "origin", "main")
    a = clone(bare, tmp_path / "arun", "arun")
    b = clone(bare, tmp_path / "cofounder", "cofounder")
    mk = lambda root: livecode.Syncer(root, quiet=0, fetch_every=0, notify_on=False)  # noqa: E731
    return mk(a), mk(b)


def text(s, name="shared.txt"):
    return (s.root / name).read_text()


def edit_line(s, n, new):
    lines = text(s).splitlines(keepends=True)
    lines[n - 1] = new + "\n"
    (s.root / "shared.txt").write_text("".join(lines))


def test_different_files_both_arrive(pair):
    a, b = pair
    (a.root / "a.txt").write_text("from a\n")
    (b.root / "b.txt").write_text("from b\n")
    assert a.tick() == "synced"
    assert b.tick() == "synced"  # rebases onto a's commit, then pushes
    assert a.tick() == "synced"
    for s in pair:
        assert (s.root / "a.txt").read_text() == "from a\n"
        assert (s.root / "b.txt").read_text() == "from b\n"
    log = git(a.root, "log", "--format=%s")
    assert "livecode(arun): a.txt" in log and "livecode(cofounder): b.txt" in log
    assert not git(a.root, "rev-list", "--merges", "HEAD").strip()  # linear history


def test_same_file_different_lines_merge_cleanly(pair):
    a, b = pair
    edit_line(a, 1, "A was here")
    edit_line(b, 10, "B was here")
    a.tick(), b.tick(), a.tick()
    for s in pair:
        assert text(s).startswith("A was here\n") and text(s).endswith("B was here\n")


def test_same_line_conflict_markers_then_resume(pair):
    a, b = pair
    edit_line(a, 5, "A's version")
    edit_line(b, 5, "B's version")
    assert a.tick() == "synced"
    assert b.tick() == "conflict"
    assert "<<<<<<<" in text(b) and b.in_conflict()
    assert b.tick() == "conflict"  # stays paused while markers remain

    # Meanwhile a keeps working elsewhere, so b's merge will need another pull.
    (a.root / "other.txt").write_text("meanwhile\n")
    assert a.tick() == "synced"

    resolved = text(b)
    resolved = resolved.split("<<<<<<<")[0] + "A and B agreed\n" + resolved.split(">>>>>>>")[1].split("\n", 1)[1]
    (b.root / "shared.txt").write_text(resolved)
    assert b.tick() == "synced"
    assert not b.in_conflict()
    assert a.tick() == "synced"
    for s in pair:
        assert "A and B agreed\n" in text(s) and "<<<<<<<" not in text(s)
        assert (s.root / "other.txt").exists()


def test_debounce_waits_for_quiet(pair):
    a, _ = pair
    a.quiet = 3
    (a.root / "draft.txt").write_text("half")
    assert a.tick(now=100) == "waiting"
    assert a.tick(now=102) == "waiting"
    (a.root / "draft.txt").write_text("half written, still going")
    assert a.tick(now=104) == "waiting"  # change resets the timer
    assert a.tick(now=106.5) == "waiting"
    assert a.tick(now=107.5) == "synced"
    assert "draft.txt" in git(a.root, "log", "-1", "--format=%s")


def test_pauses_on_detached_head(pair):
    a, _ = pair
    git(a.root, "checkout", "-q", "--detach")
    (a.root / "x.txt").write_text("x")
    assert a.tick() == "paused"
    assert "x.txt" not in git(a.root, "ls-files")


def test_pauses_during_manual_rebase_and_other_branch(pair):
    a, _ = pair
    git(a.root, "checkout", "-q", "-b", "experiment")
    assert a.tick() == "paused"
    git(a.root, "checkout", "-q", "main")
    (a.git_dir / "rebase-merge").mkdir()
    assert a.tick() == "paused"


def test_push_rejected_then_recovers(pair, monkeypatch):
    a, b = pair
    (a.root / "a.txt").write_text("a\n")
    a.tick()
    (b.root / "b.txt").write_text("b\n")
    monkeypatch.setattr(b, "fetch", lambda remote: None)  # b doesn't see a's push yet
    assert b.tick() == "push-rejected"
    monkeypatch.undo()
    assert b.tick() == "synced"
    a.tick()
    assert (a.root / "b.txt").exists() and (b.root / "a.txt").exists()


def test_ignored_files_are_not_committed(pair):
    a, b = pair
    (a.root / ".gitignore").write_text("secret.env\n")
    (a.root / "secret.env").write_text("TOKEN=x\n")
    a.tick(), b.tick()
    assert (b.root / ".gitignore").exists()
    assert not (b.root / "secret.env").exists()


def test_init_writes_claude_notes_and_hook(pair):
    a, _ = pair
    livecode.main(["init", "--repo", str(a.root)])
    assert livecode.CLAUDE_SECTION_TAG in (a.root / "CLAUDE.md").read_text()
    assert livecode.HOOK_COMMAND in (a.root / ".claude" / "settings.json").read_text()
    livecode.main(["init", "--repo", str(a.root)])  # idempotent
    assert (a.root / "CLAUDE.md").read_text().count(livecode.CLAUDE_SECTION_TAG) == 1
    assert (a.root / ".claude" / "settings.json").read_text().count("livecode start") == 1
