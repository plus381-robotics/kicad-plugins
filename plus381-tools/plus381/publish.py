"""Publishing local library changes straight to the team repository."""

import getpass
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import partsync
from .partsync import BRANCH, GitError, Lock, app_dir, git

KINDS = {".kicad_sym": "symbol", ".kicad_mod": "footprint"}
CHECK_TIMEOUT = 120  # seconds for the library's own check script


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
        # Only what the local commits changed, not what the team changed meanwhile.
        for st, path in partsync.my_files(cfg):
            if not any(i[3] == path for i in items):
                name, kind = describe(path)
                items.append((name, kind, STATUS_WORDS.get(st, "changed") + note, path))
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
    try:
        r = subprocess.run([partsync.base_python(), str(script)], capture_output=True, text=True,
                           timeout=CHECK_TIMEOUT, **partsync._hidden())
    except subprocess.TimeoutExpired:
        return False, f"The library check took longer than {CHECK_TIMEOUT} seconds and was stopped."
    except OSError as e:
        return False, f"The library check could not be started: {e}"
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
            if _rebase_and_push(cfg, push=False):  # else: the team already has all of it
                url, _ = github.open_pull_request(cfg, token, message, _pr_body(cfg))
        else:
            _rebase_and_push(cfg)
    partsync.run_sync(cfg, quiet=True)
    return url


def _pr_body(cfg):
    lines = ["Parts published from KiCad with Plus381 Tools.", ""]
    for st, path in partsync.my_files(cfg):
        name, kind = describe(path)
        lines.append(f"- {STATUS_WORDS.get(st, 'changed')} {kind}: `{name}`")
    lines += ["", "Check the 3D model and pins, then merge. Teammates then see a red dot on Library status "
                  "and get the part with Update library."]
    return "\n".join(lines)


def _rebase_and_push(cfg, keep=None, push=True):
    """Returns how many local commits the team doesn't have (before pushing)."""
    git(cfg, "fetch", "--quiet", "origin", BRANCH)
    partsync.rebase_on_team(cfg, keep)
    ahead = partsync.ahead_behind(cfg)[0]
    if push and ahead:
        git(cfg, "push", "--quiet", "origin", f"HEAD:{BRANCH}")
    return ahead


def resolve(cfg, keep, push=True):
    """After a Conflict, for the parts both sides changed: keep='mine' puts
    your version over the team's, keep='theirs' takes the team's version and
    saves yours in a backup folder. Your other unpublished parts are kept
    and published either way (push=False: left for a pull request).
    Returns (backup folder or None, number of commits published / to send)."""
    backup = None
    with Lock():
        if keep != "mine":
            backup = app_dir() / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
            backup.mkdir(parents=True, exist_ok=True)
            for _, path in partsync.my_files(cfg):
                src = Path(cfg["parts_dir"]) / path
                if src.is_file():
                    (backup / path).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, backup / path)
        ahead = _rebase_and_push(cfg, keep="mine" if keep == "mine" else "theirs", push=push)
    partsync.run_sync(cfg, quiet=True, push=False)
    return backup, ahead


def open_folder(path):
    path = str(path)
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
