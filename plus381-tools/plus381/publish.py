"""Publishing local library changes straight to the team repository."""

import getpass
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import partsync
from .partsync import BRANCH, Conflict, GitError, Lock, app_dir, git

KINDS = {".kicad_sym": "symbol", ".kicad_mod": "footprint"}


def describe(path):
    """'symbols/Plus381_ICs.kicad_symdir/X.kicad_sym' -> ('Plus381_ICs:X', 'symbol')."""
    p = Path(path)
    if p.suffix in KINDS:
        return f"{p.parent.stem}:{p.stem}", KINDS[p.suffix]
    if p.parts and p.parts[0] == "3dmodels":
        return p.name, "3D model"
    return path, "file"


STATUS_WORDS = {"A": "new", "?": "new", "??": "new", "M": "changed", "D": "deleted", "R": "renamed"}


def pending_changes(cfg):
    """Uncommitted edits plus commits that were not pushed yet (e.g. offline)."""
    items = []
    for st, path in partsync.changed_files(cfg):
        name, kind = describe(path)
        items.append((name, kind, STATUS_WORDS.get(st, STATUS_WORDS.get(st[:1], "changed")), path))
    try:
        ahead, _ = partsync.ahead_behind(cfg)
    except GitError:
        ahead = 0
    if ahead:
        in_review = partsync.load_config().get("publish_mode") == "pr"
        note = ", waiting for review (pull request)" if in_review else ", not uploaded yet"
        out = git(cfg, "diff", "--name-status", f"origin/{BRANCH}", "HEAD").stdout
        for line in out.splitlines():
            st, _, path = line.partition("\t")
            path = path.split("\t")[-1]
            if not any(i[3] == path for i in items):
                name, kind = describe(path)
                items.append((name, kind, STATUS_WORDS.get(st[:1], "changed") + note, path))
    return items


def default_message(items):
    names = sorted({i[0] for i in items if i[1] in ("symbol", "footprint")})
    new = all(i[2].startswith("new") for i in items)
    verb = "Add" if new else "Update"
    if not names:
        return f"{verb} library files"
    if len(names) <= 3:
        return f"{verb} " + ", ".join(names)
    return f"{verb} {len(names)} parts"


def identity_missing(cfg):
    name = git(cfg, "config", "user.name", check=False).stdout.strip()
    email = git(cfg, "config", "user.email", check=False).stdout.strip()
    return not (name and email)


def set_identity(cfg, name, email):
    git(cfg, "config", "user.name", name or getpass.getuser())
    git(cfg, "config", "user.email", email)


def run_check(cfg):
    """Run the repository's own check script. Returns (ok, output)."""
    script = Path(cfg["parts_dir"]) / "scripts" / "check_library.py"
    if not script.exists():
        return True, ""
    r = subprocess.run([partsync.base_python(), str(script)], capture_output=True, text=True,
                       **partsync._hidden())
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def publish(cfg, message, token=None, mode="direct"):
    """Commit everything and put it on top of the newest team version, then
    push it (members) or send it as a pull request (everyone else).
    Raises Conflict when someone else changed the same file meanwhile.
    Returns the pull request URL in "pr" mode, else None."""
    from . import github
    url = None
    with Lock():
        if partsync.changed_files(cfg):
            git(cfg, "add", "-A")
            git(cfg, "commit", "--quiet", "-m", message)
        if mode == "pr":
            _rebase_and_push(cfg, push=False)
            url, _ = github.open_pull_request(cfg, token, message, _pr_body(cfg))
        else:
            _rebase_and_push(cfg)
    partsync.run_sync(cfg, quiet=True)
    return url


def _pr_body(cfg):
    lines = ["Parts published from KiCad with Plus381 Tools.", ""]
    out = git(cfg, "diff", "--name-status", f"origin/{BRANCH}", "HEAD").stdout
    for line in out.splitlines():
        st, _, path = line.partition("\t")
        name, kind = describe(path.split("\t")[-1])
        lines.append(f"- {STATUS_WORDS.get(st[:1], 'changed')} {kind}: `{name}`")
    lines += ["", "Check the 3D model and pins, then merge. Everyone gets the part within a minute."]
    return "\n".join(lines)


def _rebase_and_push(cfg, strategy=None, push=True):
    git(cfg, "fetch", "--quiet", "origin", BRANCH)
    ahead, behind = partsync.ahead_behind(cfg)
    if behind:
        args = ["rebase", "--quiet"] + (["-X", strategy] if strategy else []) + [f"origin/{BRANCH}"]
        r = git(cfg, *args, check=False)
        if r.returncode != 0:
            files = [l[3:] for l in git(cfg, "status", "--porcelain").stdout.splitlines()
                     if l[:2] in ("UU", "AA", "DU", "UD")]
            git(cfg, "rebase", "--abort", check=False)
            raise Conflict(files or ["(unknown)"])
    if push and partsync.ahead_behind(cfg)[0]:
        git(cfg, "push", "--quiet", "origin", f"HEAD:{BRANCH}")


def resolve(cfg, keep, push=True):
    """After a Conflict: keep='mine' overwrites the other version with yours,
    keep='theirs' takes the team version and saves yours in a backup folder."""
    with Lock():
        if keep == "mine":
            # During a rebase, "theirs" means the commits being replayed: yours.
            _rebase_and_push(cfg, strategy="theirs", push=push)
            return None
        backup = app_dir() / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
        out = git(cfg, "diff", "--name-only", f"origin/{BRANCH}", "HEAD").stdout.split()
        for path in out:
            src = Path(cfg["parts_dir"]) / path
            if src.is_file():
                (backup / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, backup / path)
        git(cfg, "reset", "--hard", "--quiet", f"origin/{BRANCH}")
        return backup


def open_folder(path):
    path = str(path)
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
