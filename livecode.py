#!/usr/bin/env python3
"""livecode: keep every working copy of a repo continuously in sync with its upstream branch.

Two (or more) people edit the same branch at the same time. Each runs `livecode start` in the
repo; a background loop autosaves quiet edits as commits, rebases them onto whatever the others
pushed, and pushes. Same-line conflicts become ordinary conflict markers in the files; once the
markers are gone the loop commits the merge and carries on. It never force-pushes.

Standard library only, so it runs anywhere Python 3.8+ and git do.
"""

import argparse
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

__version__ = "0.1.0"

# Never let a background git block on a password prompt.
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_MERGE_AUTOEDIT="no")
MARKERS = re.compile(rb"^(<{7}|>{7})( |$)", re.M)
MAX_NAMES_IN_MESSAGE = 4

CLAUDE_SECTION_TAG = "<!-- livecode -->"
CLAUDE_SECTION = f"""
{CLAUDE_SECTION_TAG}
## Live sync (livecode)

This repo is kept in sync live with teammates by `livecode`, which autosaves, commits,
pulls and pushes in the background every few seconds.

- Files can change underneath you as a teammate's edits arrive. Re-read a file right
  before editing it rather than relying on an earlier read.
- Do not run `git commit`, `git pull`, `git push`, `git rebase` or `git stash` yourself;
  livecode does it and a manual one can collide with it. Read-only git is fine.
- If a file contains `<<<<<<<` / `>>>>>>>` conflict markers, both people edited the same
  lines. Resolve them by editing the file (keep the right combination of both sides) and
  remove every marker. livecode commits the result on its own once no markers remain.
- `livecode status` shows whether sync is running, paused, or waiting on a conflict.
"""
HOOK_COMMAND = "command -v livecode >/dev/null 2>&1 && livecode start --quiet || true"


class GitError(RuntimeError):
    pass


def run_git(cwd, *args, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=ENV)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p


def repo_root(path="."):
    try:
        return Path(run_git(path, "rev-parse", "--show-toplevel").stdout.strip())
    except (GitError, FileNotFoundError):
        sys.exit(f"livecode: {Path(path).resolve()} is not inside a git repository")


def state_dir(root):
    git_dir = Path(run_git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
    d = git_dir / "livecode"
    d.mkdir(exist_ok=True)
    return d


def notify(message):
    title = "livecode"
    try:
        if sys.platform == "darwin":
            esc = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(["osascript", "-e", f'display notification "{esc}" with title "{title}"'],
                           capture_output=True, timeout=5)
        elif os.name == "nt":
            esc = message.replace("'", "''")
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$n=New-Object System.Windows.Forms.NotifyIcon;"
                  "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                  f"$n.ShowBalloonTip(8000,'{title}','{esc}',"
                  "[System.Windows.Forms.ToolTipIcon]::Warning);Start-Sleep 9;$n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["notify-send", title, message], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill on Windows terminates the process, so ask the kernel instead.
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Syncer:
    """One sync step at a time via tick(); run() loops it. Tests drive tick() directly."""

    def __init__(self, root, quiet=3.0, fetch_every=5.0, notify_on=True, echo=False):
        self.root = Path(root)
        self.dir = state_dir(self.root)
        self.git_dir = self.dir.parent
        self.quiet = quiet
        self.fetch_every = fetch_every
        self.notify_on = notify_on
        self.echo = echo
        self.branch = self.current_branch()
        self._sig = None
        self._sig_since = 0.0
        self._last_fetch = float("-inf")
        self._last_status = None
        self.name = run_git(root, "config", "user.name", check=False).stdout.strip() or getpass.getuser()

    # -- helpers ---------------------------------------------------------------

    def git(self, *args, check=True):
        return run_git(self.root, *args, check=check)

    def log(self, msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        with open(self.dir / "log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.echo:
            print(line, flush=True)

    def alert(self, msg):
        self.log(msg)
        if self.notify_on:
            notify(f"{self.root.name}: {msg}")

    def current_branch(self):
        p = self.git("symbolic-ref", "-q", "--short", "HEAD", check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    def upstream(self):
        """('origin', 'main', 'origin/main') for the current branch, or None."""
        b = self.current_branch()
        if not b:
            return None
        remote = self.git("config", f"branch.{b}.remote", check=False).stdout.strip()
        merge = self.git("config", f"branch.{b}.merge", check=False).stdout.strip()
        if not remote or not merge or remote == ".":
            return None
        rbranch = merge[len("refs/heads/"):] if merge.startswith("refs/heads/") else merge
        return remote, rbranch, f"refs/remotes/{remote}/{rbranch}"

    def count(self, rng):
        p = self.git("rev-list", "--count", rng, check=False)
        return int(p.stdout.strip()) if p.returncode == 0 else 0

    def porcelain(self):
        return self.git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout

    def signature(self, porcelain):
        """Status plus size/mtime of every changed path, so any further write resets the timer."""
        parts = [porcelain]
        for entry in porcelain.split("\0"):
            path = entry[3:] if len(entry) > 3 else entry
            try:
                st = (self.root / path).stat()
                parts.append(f"{path}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                parts.append(f"{path}:gone")
        return "\n".join(parts)

    def quiet_enough(self, porcelain, now):
        sig = self.signature(porcelain)
        if sig != self._sig:
            self._sig, self._sig_since = sig, now
        return now - self._sig_since >= self.quiet

    def save_state(self, status, **extra):
        state = {"status": status, "pid": os.getpid(), "branch": self.branch,
                 "updated": time.time(), **extra}
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.dir / "state.json")
        if status != self._last_status:
            if status.startswith("paused"):
                self.log(status)
            self._last_status = status

    # -- conflict mode ---------------------------------------------------------

    @property
    def conflict_file(self):
        return self.dir / "CONFLICT"

    def in_conflict(self):
        return self.conflict_file.exists()

    def unmerged(self):
        out = self.git("diff", "--name-only", "--diff-filter=U", "-z").stdout
        return [p for p in out.split("\0") if p]

    def has_markers(self, path):
        try:
            return bool(MARKERS.search((self.root / path).read_bytes()))
        except OSError:
            return False

    def enter_conflict(self, files):
        self.conflict_file.write_text("\n".join(files) + "\n")
        self.alert(f"conflict in {', '.join(files)} — edit out the <<<<<<< markers and sync resumes")

    def finish_conflict(self, now):
        """Commit the merge once every conflicted file is free of markers and edits have settled."""
        if not (self.git_dir / "MERGE_HEAD").exists():
            # Someone finished or aborted the merge by hand. Fine; just resume.
            self.conflict_file.unlink()
            self.log("merge was completed or aborted outside livecode; resuming")
            return True
        files = self.unmerged()
        still = [f for f in files if self.has_markers(f)]
        if still or not self.quiet_enough(self.porcelain(), now):
            self.save_state("conflict", files=still or files)
            return False
        self.git("add", "-A")
        self.git("commit", "--no-verify", "--no-edit")
        self.conflict_file.unlink()
        self.alert("conflict resolved, syncing again")
        return True

    # -- the loop body ---------------------------------------------------------

    def pause_reason(self):
        b = self.current_branch()
        if b is None:
            return "paused: detached HEAD"
        if b != self.branch:
            return f"paused: on branch {b}, livecode started on {self.branch}"
        for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            if (self.git_dir / name).exists():
                return f"paused: a git operation is in progress ({name})"
        if self.upstream() is None:
            return f"paused: branch {b} has no upstream (git push -u origin {b})"
        return None

    def commit_changes(self):
        self.git("add", "-A")
        names = [n for n in self.git("diff", "--cached", "--name-only", "-z").stdout.split("\0") if n]
        if not names:
            return False
        shown = ", ".join(names[:MAX_NAMES_IN_MESSAGE])
        if len(names) > MAX_NAMES_IN_MESSAGE:
            shown += f" (+{len(names) - MAX_NAMES_IN_MESSAGE} more)"
        self.git("commit", "--no-verify", "-q", "-m", f"livecode({self.name}): {shown}")
        self.log(f"committed {shown}")
        return True

    def fetch(self, remote):
        self.git("fetch", "--quiet", remote)

    def integrate(self, upref):
        """Bring upstream in. Rebase for a linear history; merge when that is impossible or conflicts."""
        has_merges = bool(self.git("rev-list", "--merges", f"{upref}..HEAD").stdout.strip())
        if not has_merges:
            if self.git("rebase", "-q", upref, check=False).returncode == 0:
                return True
            self.git("rebase", "--abort", check=False)
        if self.git("merge", "--no-edit", "-q", upref, check=False).returncode == 0:
            return True
        files = self.unmerged()
        if not files:
            self.git("merge", "--abort", check=False)
            raise GitError("merge failed without conflicts (untracked files in the way?)")
        self.enter_conflict(files)
        return False

    def tick(self, now=None):
        """Do one step of sync. Returns a short status word."""
        now = time.monotonic() if now is None else now

        if self.in_conflict():
            if not self.finish_conflict(now):
                return "conflict"

        reason = self.pause_reason()
        if reason:
            self.save_state(reason)
            return "paused"

        porcelain = self.porcelain()
        if porcelain:
            if not self.quiet_enough(porcelain, now):
                self.save_state("editing")
                return "waiting"
            self.commit_changes()

        remote, rbranch, upref = self.upstream()
        ahead = self.count(f"{upref}..HEAD")
        if ahead or now - self._last_fetch >= self.fetch_every:
            self.fetch(remote)
            self._last_fetch = now

        behind = self.count(f"HEAD..{upref}")
        if behind:
            if not self.integrate(upref):
                self.save_state("conflict", files=self.unmerged())
                return "conflict"
            self.log(f"pulled {behind} commit(s) from {upref.split('/', 2)[2]}")

        ahead = self.count(f"{upref}..HEAD")
        if ahead:
            p = self.git("push", "--quiet", remote, f"HEAD:refs/heads/{rbranch}", check=False)
            if p.returncode != 0:
                # Usually the other person pushed first. Fetch and integrate next tick.
                self._last_fetch = float("-inf")
                self.save_state("push-rejected", error=p.stderr.strip()[-300:])
                return "push-rejected"
            self.log(f"pushed {ahead} commit(s)")

        self.save_state("synced", last_sync=time.time())
        return "synced"

    def run(self, interval=1.0):
        stop_file = self.dir / "stop"
        stop_file.unlink(missing_ok=True)
        (self.dir / "pid").write_text(str(os.getpid()))
        running = [True]

        def _stop(*_):
            running[0] = False
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self.log(f"started on {self.branch} (pid {os.getpid()})")
        last_error = None
        try:
            while running[0] and not stop_file.exists():
                try:
                    self.tick()
                    last_error = None
                except GitError as e:
                    if str(e) != last_error:  # don't flood the log with a repeating failure
                        self.log(f"error: {e}")
                        last_error = str(e)
                    self.save_state("error", error=str(e)[-300:])
                time.sleep(interval)
        finally:
            self.log("stopped")
            for name in ("pid", "stop"):
                (self.dir / name).unlink(missing_ok=True)
            self.save_state("stopped")


# -- CLI ---------------------------------------------------------------------

def read_pid(d):
    try:
        return int((d / "pid").read_text().strip())
    except (OSError, ValueError):
        return 0


def cmd_start(args):
    root = repo_root(args.repo)
    d = state_dir(root)
    pid = read_pid(d)
    if pid_alive(pid):
        if not args.quiet:
            print(f"livecode already running in {root} (pid {pid})")
        return
    cmd = [sys.executable, os.path.abspath(__file__), "run", "--repo", str(root),
           "--quiet-seconds", str(args.quiet_seconds), "--fetch-every", str(args.fetch_every)]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(d / "daemon.out", "ab") as out:
        p = subprocess.Popen(cmd, cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=out, **kwargs)
    for _ in range(30):
        if read_pid(d) == p.pid:
            break
        if p.poll() is not None:
            sys.exit(f"livecode failed to start; see {d / 'daemon.out'}")
        time.sleep(0.1)
    if not args.quiet:
        print(f"livecode syncing {root} (pid {p.pid}). `livecode status` to check, `livecode stop` to stop.")


def cmd_stop(args):
    root = repo_root(args.repo)
    d = state_dir(root)
    pid = read_pid(d)
    if not pid_alive(pid):
        print("livecode is not running here")
        return
    (d / "stop").touch()
    for _ in range(50):
        if not pid_alive(pid):
            print("stopped")
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGTERM)
    print("stopped (terminated)")


def cmd_run(args):
    root = repo_root(args.repo)
    s = Syncer(root, quiet=args.quiet_seconds, fetch_every=args.fetch_every, echo=True)
    if s.branch is None:
        sys.exit("livecode: HEAD is detached; check out a branch first")
    s.run()


def cmd_status(args):
    root = repo_root(args.repo)
    d = state_dir(root)
    pid = read_pid(d)
    try:
        state = json.loads((d / "state.json").read_text())
    except (OSError, ValueError):
        state = {}
    if pid_alive(pid):
        age = time.time() - state.get("updated", 0)
        print(f"running (pid {pid}) on {state.get('branch')}: {state.get('status', '?')}"
              f"  [updated {age:.0f}s ago]")
    else:
        print("not running  (livecode start)")
    if state.get("files"):
        print("conflicted files: " + ", ".join(state["files"]))
    if state.get("error"):
        print("last error: " + state["error"])
    p = run_git(root, "log", "-6", "--format=  %h %<(14,trunc)%an %<(14)%ar %s", "@{u}", check=False)
    if p.returncode == 0 and p.stdout.strip():
        print("recent upstream commits:\n" + p.stdout.rstrip())


def cmd_log(args):
    root = repo_root(args.repo)
    f = state_dir(root) / "log"
    if not f.exists():
        print("no log yet")
        return
    lines = f.read_text(encoding="utf-8").splitlines()
    print("\n".join(lines[-args.lines:]))


def add_claude_hook(root):
    """SessionStart hook so opening the repo in any Claude Code surface starts sync."""
    path = root / ".claude" / "settings.json"
    try:
        settings = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        print(f"  skipped hook: {path} is not valid JSON")
        return
    groups = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    if any(h.get("command") == HOOK_COMMAND for g in groups for h in g.get("hooks", [])):
        return
    groups.append({"hooks": [{"type": "command", "command": HOOK_COMMAND}]})
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"  added SessionStart hook to {path.relative_to(root)}")


def cmd_init(args):
    root = repo_root(args.repo)
    s = Syncer(root, notify_on=False)
    if s.branch is None:
        sys.exit("livecode: HEAD is detached; check out a branch first")
    if s.upstream() is None:
        sys.exit(f"livecode: {s.branch} has no upstream. Push it once with: git push -u origin {s.branch}")
    print(f"livecode init in {root} (branch {s.branch} -> {s.upstream()[0]}/{s.upstream()[1]})")
    targets = [root / "CLAUDE.md"] + ([root / "AGENTS.md"] if (root / "AGENTS.md").exists() else [])
    for t in targets:
        text = t.read_text(encoding="utf-8") if t.exists() else ""
        if CLAUDE_SECTION_TAG not in text:
            t.write_text(text.rstrip("\n") + ("\n" if text else "") + CLAUDE_SECTION, encoding="utf-8")
            print(f"  added live-sync notes to {t.name}")
    if not args.no_claude_hook:
        add_claude_hook(root)
    print("done. Run `livecode start` (your cofounder runs it too, in their clone).")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="livecode", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--repo", default=".", help="path inside the repo (default: current dir)")
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "one-time setup: Claude notes + auto-start hook")
    p.add_argument("--no-claude-hook", action="store_true", help="don't add the SessionStart hook")
    for name, fn, h in (("start", cmd_start, "start syncing in the background"),
                        ("run", cmd_run, "sync in the foreground")):
        p = add(name, fn, h)
        p.add_argument("--quiet-seconds", type=float, default=3.0,
                       help="commit only after files stop changing for this long (default 3)")
        p.add_argument("--fetch-every", type=float, default=5.0,
                       help="seconds between fetches when idle (default 5)")
        if name == "start":
            p.add_argument("--quiet", action="store_true", help="no output if already running")
    add("stop", cmd_stop, "stop syncing")
    add("status", cmd_status, "show sync state and recent upstream commits")
    p = add("log", cmd_log, "show the sync log")
    p.add_argument("-n", "--lines", type=int, default=40)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
