#!/usr/bin/env python3
"""Keep the Plus381 parts library in sync and registered in KiCad.

This file only uses the Python standard library. It is imported by the
Plus381 Tools plugin and is also copied to the user's config folder, where
the operating system's scheduler runs it every minute:

    python partsync.py sync [--quiet]     get the newest parts, push unpublished ones
    python partsync.py configure-kicad    write KiCad settings (waits until KiCad is closed)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

APP = "plus381"
VAR = "PLUS381_PARTS"
LIB_PREFIX = "Plus381_"
# PLUS381_PARTS_REPO lets a fork or a test setup use another library repository.
REPO_URL = os.environ.get("PLUS381_PARTS_REPO", "https://github.com/plus381-robotics/kicad-parts.git")
BRANCH = "main"
MANAGED_DESCR = "Plus381 team library (managed by Plus381 Tools)"

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

def app_dir():
    if IS_WIN:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path():
    return app_dir() / "config.json"


def status_path():
    return app_dir() / "status.json"


def load_config():
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_status():
    try:
        return json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_status(st):
    st["time"] = datetime.now().isoformat(timespec="seconds")
    status_path().write_text(json.dumps(st, indent=2), encoding="utf-8")


def default_parts_dir():
    # Not in Documents: on Windows it is often synced by OneDrive (bad for Git
    # repositories) and on macOS background jobs need extra permission for it.
    return Path.home() / "KiCad" / "plus381-parts"


def kicad_config_base():
    env = os.environ.get("KICAD_CONFIG_HOME")
    if env:
        return Path(env)
    if IS_WIN:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "kicad"
    if IS_MAC:
        return Path.home() / "Library" / "Preferences" / "kicad"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kicad"


def kicad_config_dirs():
    """Settings folders of every installed KiCad 10 or newer."""
    base = kicad_config_base()
    dirs = []
    if base.is_dir():
        for d in base.iterdir():
            m = re.fullmatch(r"(\d+)\.(\d+)", d.name)
            if m and int(m.group(1)) >= 10 and d.is_dir():
                dirs.append(d)
    return sorted(dirs)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def find_git():
    exe = shutil.which("git")
    if exe:
        return exe
    candidates = []
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Git" / "cmd" / "git.exe")
                candidates.append(Path(base) / "Programs" / "Git" / "cmd" / "git.exe")
        gd = Path(os.environ.get("LOCALAPPDATA", "")) / "GitHubDesktop"
        if gd.is_dir():
            candidates += sorted(gd.glob("app-*/resources/app/git/cmd/git.exe"), reverse=True)
    else:
        candidates += [Path("/opt/homebrew/bin/git"), Path("/usr/local/bin/git"), Path("/usr/bin/git")]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def base_python():
    """The real interpreter behind the plugin's virtual environment, which
    KiCad may delete and recreate at any time."""
    exe = getattr(sys, "_base_executable", None) or sys.executable
    if sys.prefix != sys.base_prefix and exe == sys.executable:
        cfg = Path(sys.prefix) / "pyvenv.cfg"
        try:
            home = re.search(r"^home\s*=\s*(.+)$", cfg.read_text(), re.M).group(1).strip()
            name = "python.exe" if IS_WIN else "python3"
            if (Path(home) / name).exists():
                exe = str(Path(home) / name)
        except (OSError, AttributeError):
            pass
    return exe


def _hidden():
    if IS_WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"startupinfo": si, "creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


class GitError(Exception):
    pass


def git(cfg, *args, check=True, timeout=600):
    exe = cfg.get("git") or find_git()
    if not exe:
        raise GitError("Git is not installed. Install Git for Windows / GitHub Desktop "
                       "(Windows) or git + git-lfs (macOS/Linux).")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="auto")
    r = subprocess.run([exe, "-C", cfg["parts_dir"], *args], capture_output=True, text=True,
                       env=env, timeout=timeout, **_hidden())
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}")
    return r


def kicad_running():
    names = {"kicad", "pcbnew", "eeschema", "kicad.exe", "pcbnew.exe", "eeschema.exe"}
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 **_hidden()).stdout
            procs = {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}
        else:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True).stdout
            procs = {os.path.basename(line.strip()).lower() for line in out.splitlines()}
    except OSError:
        return True  # cannot tell: be careful
    return bool(procs & names)


# --------------------------------------------------------------------------
# KiCad settings
# --------------------------------------------------------------------------

def libraries(parts_dir):
    """{nickname: {"sym": path, "fp": path}} for every library in the parts repo."""
    parts = Path(parts_dir)
    libs = {}
    for d in sorted((parts / "symbols").glob(LIB_PREFIX + "*.kicad_symdir")):
        libs.setdefault(d.stem, {})["sym"] = d
    for d in sorted((parts / "footprints").glob(LIB_PREFIX + "*.pretty")):
        libs.setdefault(d.stem, {})["fp"] = d
    return libs


def _lib_entries(text):
    """(start, end, name) of every `(lib ...)` entry in a lib table."""
    out = []
    i = 0
    while True:
        i = text.find("(lib ", i)
        if i < 0:
            return out
        depth = 0
        j = i
        while j < len(text):
            c = text[j]
            if c == '"':
                j += 1
                while text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        m = re.search(r'\(name\s+"((?:[^"\\]|\\.)*)"\)', text[i:j])
        out.append((i, j + 1, m.group(1) if m else ""))
        i = j + 1


def ensure_lib_tables(parts_dir):
    """Register every Plus381 library in KiCad's global library tables.
    Returns a list of human readable problems (empty when all good)."""
    problems = []
    libs = libraries(parts_dir)
    dirs = kicad_config_dirs()
    if not dirs:
        return ["KiCad 10 settings not found: start KiCad once and finish its welcome wizard."]
    for cfgdir in dirs:
        for table, kind in (("sym-lib-table", "sym"), ("fp-lib-table", "fp")):
            path = cfgdir / table
            if not path.exists():
                problems.append(f"{path} does not exist yet: start KiCad once and finish its welcome wizard.")
                continue
            text = path.read_text(encoding="utf-8")
            entries = _lib_entries(text)
            # Drop our old entries (from the end so offsets stay valid).
            new = text
            for s, e, name in reversed(entries):
                if name.startswith(LIB_PREFIX):
                    line_start = new.rfind("\n", 0, s) + 1
                    line_end = new.find("\n", e)
                    line_end = len(new) if line_end < 0 else line_end + 1
                    if new[line_start:s].strip() == "" and new[e:line_end].strip() == "":
                        new = new[:line_start] + new[line_end:]
                    else:
                        new = new[:s] + new[e:]
            add = ""
            for nick, paths in libs.items():
                if kind in paths:
                    uri = paths[kind].as_posix()
                    add += (f'  (lib (name "{nick}")(type "KiCad")(uri "{uri}")'
                            f'(options "")(descr "{MANAGED_DESCR}"))\n')
            close = new.rstrip().rfind(")")
            new = new[:close].rstrip("\n ") + "\n" + add + new[close:]
            if new != text:
                path.write_text(new, encoding="utf-8")
    return problems


def env_var_ok(parts_dir):
    want = Path(parts_dir).as_posix()
    dirs = kicad_config_dirs()
    if not dirs:
        return False
    for cfgdir in dirs:
        try:
            d = json.loads((cfgdir / "kicad_common.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if (d.get("environment", {}).get("vars") or {}).get(VAR) != want:
            return False
    return True


def ensure_env_var(parts_dir):
    """Set the PLUS381_PARTS path variable. KiCad rewrites its settings when
    it closes, so this is only done while KiCad is not running.
    Returns "ok", "pending" (KiCad open) or an error text."""
    if env_var_ok(parts_dir):
        return "ok"
    if kicad_running():
        return "pending"
    want = Path(parts_dir).as_posix()
    dirs = kicad_config_dirs()
    if not dirs:
        return "KiCad 10 settings not found: start KiCad once and finish its welcome wizard."
    for cfgdir in dirs:
        path = cfgdir / "kicad_common.json"
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d.setdefault("environment", {})
        if not isinstance(d["environment"].get("vars"), dict):
            d["environment"]["vars"] = {}
        d["environment"]["vars"][VAR] = want
        path.write_text(json.dumps(d, indent=2), encoding="utf-8")
    return "ok"


def configure_kicad(parts_dir):
    """Library tables and path variable; returns (problems, env_state)."""
    return ensure_lib_tables(parts_dir), ensure_env_var(parts_dir)


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

class Lock:
    """Stops the scheduler and the plugin from running git at the same time."""

    def __init__(self, wait=60):
        self.path = app_dir() / "sync.lock"
        self.wait = wait
        self.fd = None

    def __enter__(self):
        deadline = time.time() + self.wait
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 600:
                        self.path.unlink()  # stale lock from a crashed run
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    raise GitError("Another sync is still running, try again in a minute.")
                time.sleep(1)

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            self.path.unlink()
        except OSError:
            pass


def is_repo(cfg):
    return bool(cfg.get("parts_dir")) and (Path(cfg["parts_dir"]) / ".git").exists()


def changed_files(cfg):
    """Local changes not yet committed: list of (status, path)."""
    out = git(cfg, "status", "--porcelain", "-uall").stdout
    files = []
    for line in out.splitlines():
        if len(line) > 3:
            path = line[3:].split(" -> ")[-1].strip('"')
            files.append((line[:2].strip(), path))
    return files


def ahead_behind(cfg):
    out = git(cfg, "rev-list", "--left-right", "--count", f"HEAD...origin/{BRANCH}").stdout.split()
    return int(out[0]), int(out[1])


class Conflict(GitError):
    def __init__(self, files):
        super().__init__("Someone else changed the same files: " + ", ".join(files))
        self.files = files


def sync(cfg, push=True):
    """Bring the local library up to date with GitHub without ever touching
    files the user has unsaved/unpublished edits in. Returns a status dict."""
    st = {"ok": True, "message": "", "new_files": []}
    with Lock():
        git(cfg, "fetch", "--quiet", "origin", BRANCH)
        before = git(cfg, "rev-parse", "HEAD").stdout.strip()
        ahead, behind = ahead_behind(cfg)
        dirty = changed_files(cfg)
        if behind and not ahead:
            r = git(cfg, "merge", "--ff-only", "--quiet", f"origin/{BRANCH}", check=False)
            if r.returncode != 0:
                st.update(ok=False, message="New parts are waiting, but they touch files you are editing. "
                                            "Publish or undo your changes to get them.")
        elif behind and ahead:
            if dirty:
                st.update(ok=False, message="Waiting: you have unpublished changes. Publish them to sync.")
            else:
                r = git(cfg, "rebase", "--quiet", f"origin/{BRANCH}", check=False)
                if r.returncode != 0:
                    files = [l[3:] for l in git(cfg, "status", "--porcelain").stdout.splitlines()
                             if l[:2] in ("UU", "AA", "DU", "UD")]
                    git(cfg, "rebase", "--abort", check=False)
                    raise Conflict(files)
        ahead, behind = ahead_behind(cfg)
        if push and ahead and not behind:
            git(cfg, "push", "--quiet", "origin", f"HEAD:{BRANCH}")
        after = git(cfg, "rev-parse", "HEAD").stdout.strip()
        if after != before:
            st["new_files"] = git(cfg, "diff", "--name-only", before, after).stdout.split()
        problems, env = configure_kicad(cfg["parts_dir"])
        st["kicad_problems"] = problems
        st["env_var"] = env
        st["ahead"], st["behind"] = ahead_behind(cfg)
        st["dirty"] = len(changed_files(cfg))
    return st


def run_sync(cfg, quiet=False):
    """sync() with error handling and status file, used by the scheduler."""
    try:
        st = sync(cfg)
    except Exception as e:  # noqa: BLE001 - keep the scheduler alive, report instead
        st = {"ok": False, "message": str(e)}
    old = load_status()
    if st.get("new_files"):
        st["last_update"] = datetime.now().isoformat(timespec="seconds")
        st["last_new_files"] = st["new_files"]
    else:
        st["last_update"] = old.get("last_update")
        st["last_new_files"] = old.get("last_new_files", [])
    save_status(st)
    if not quiet:
        print(json.dumps(st, indent=2))
    return st


def main(argv):
    cfg = load_config()
    if not is_repo(cfg):
        print("Plus381 parts library is not set up (run Setup in the KiCad plugin).")
        return 1
    cmd = argv[1] if len(argv) > 1 else "sync"
    if cmd == "sync":
        st = run_sync(cfg, quiet="--quiet" in argv)
        return 0 if st.get("ok") else 1
    if cmd == "configure-kicad":
        # Started by Setup: wait (up to 12 h) until KiCad is closed, then write settings.
        deadline = time.time() + 12 * 3600
        while kicad_running() and time.time() < deadline:
            time.sleep(2)
        problems, env = configure_kicad(cfg["parts_dir"])
        print(env, *problems, sep="\n")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
