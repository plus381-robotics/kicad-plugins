#!/usr/bin/env python3
"""Keep the Plus381 parts library in sync and registered in KiCad.

This file only uses the Python standard library, so it also runs on its
own, outside the plugin:

    python partsync.py watch              check for new parts while KiCad is open (see watch())
    python partsync.py check              check once whether the team has new parts
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
MANAGED_DESCR = "Team parts library (managed by Plus381 Tools)"
MANAGED_MARK = "managed by Plus381 Tools"

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


def write_atomic(path, text):
    """Write a file so that readers (the background check, the other Plus381
    windows, KiCad) only ever see the old or the new content, never half of
    it, and a crash can't leave it cut off."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: someone is reading it right now
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def save_config(cfg):
    write_atomic(config_path(), json.dumps(cfg, indent=2))


def load_status():
    try:
        return json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_status(st):
    """After a sync: also clears the "new parts waiting" mark."""
    st["time"] = datetime.now().isoformat(timespec="seconds")
    if st.get("ok"):
        st.update(updates=0, update_parts=[])
    else:
        old = load_status()
        st.setdefault("updates", old.get("updates", 0))
        st.setdefault("update_parts", old.get("update_parts", []))
    _write_status(st)


def _write_status(st):
    write_atomic(status_path(), json.dumps(st, indent=2))


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


def _is_python(path):
    return bool(path) and os.path.basename(str(path)).lower().startswith("python") and os.path.isfile(path)


def base_python():
    """The real interpreter behind the plugin's virtual environment, which
    KiCad may delete and recreate at any time.

    Inside the PCB editor (the fallback buttons), sys.executable is KiCad
    itself, so look for the Python that ships with KiCad instead."""
    if not _is_python(sys.executable):
        here = Path(sys.executable).resolve().parent
        candidates = [load_config().get("python"),
                      here / "pythonw.exe", here / "python.exe",
                      Path(sys.prefix) / "bin" / "python3", Path(sys.base_prefix) / "bin" / "python3",
                      shutil.which("python3"), shutil.which("python")]
        for c in candidates:
            if c and _is_python(str(c)):
                return str(c)
        return sys.executable
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


def plugin_python():
    """The Python of the plugin's own environment (the one with the plugin's
    extra packages, e.g. easyeda2kicad), remembered from API runs."""
    if sys.prefix != sys.base_prefix and _is_python(sys.executable):
        return sys.executable
    venv = load_config().get("plugin_python")
    return venv if _is_python(venv) else base_python()


def _hidden():
    if IS_WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"startupinfo": si, "creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


class GitError(Exception):
    pass


# --------------------------------------------------------------------------
# GitHub login token
# --------------------------------------------------------------------------
# The plugin keeps its own copy of the GitHub login instead of relying on
# Git's shared credential store, where other tools (GitHub Desktop, VS Code,
# Git Credential Manager after a failed request) can replace or erase it.
# Windows: encrypted for the current Windows user (DPAPI).
# macOS/Linux: a file only the user can read (like ~/.git-credentials).

def _token_path():
    return app_dir() / ("github-token.dpapi" if IS_WIN else "github-token")


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise OSError("Windows could not " + ("encrypt" if protect else "decrypt") + " the GitHub login")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def save_token(token):
    path = _token_path()
    data = token.encode()
    if IS_WIN:
        data = _dpapi(data, True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def load_token():
    try:
        data = _token_path().read_bytes()
        if IS_WIN:
            data = _dpapi(data, False)
        return data.decode().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def delete_token():
    try:
        _token_path().unlink()
    except OSError:
        pass


# Where the GitHub login may be sent (PLUS381_GITHUB_WEB: tests with a fake server).
GITHUB_WEB = os.environ.get("PLUS381_GITHUB_WEB", "https://github.com").rstrip("/")


def _auth_env(cfg):
    """Git settings (passed in the environment, never on the command line or
    in a config file) that send the saved login to GitHub for this command.
    Only to GitHub: a library repository on another server never gets it."""
    token = load_token()
    if not token:
        return {}
    import base64
    login = cfg.get("github_login") or "x-access-token"
    basic = base64.b64encode(f"{login}:{token}".encode()).decode()
    n = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    return {"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": f"http.{GITHUB_WEB}/.extraheader",
            f"GIT_CONFIG_VALUE_{n}": f"AUTHORIZATION: basic {basic}"}


def git_exe(cfg):
    """The Git to use: the one remembered by Setup while it still exists
    (GitHub Desktop moves its Git with every update), else a fresh search."""
    exe = cfg.get("git")
    if exe and os.path.isfile(exe):
        return exe
    exe = find_git()
    if not exe:
        raise GitError("Git is not installed. Install Git for Windows / GitHub Desktop "
                       "(Windows) or git + git-lfs (macOS/Linux).")
    return exe


_AUTH_FAILED = re.compile(r"authentication failed|invalid username or password|bad credentials|"
                          r"returned error: 40[13]", re.I)


def git(cfg, *args, check=True, timeout=600):
    exe = git_exe(cfg)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="auto")
    auth = _auth_env(cfg)
    # core.quotepath: file names with non-ASCII letters are printed as they are.
    cmd = [exe, "-C", cfg["parts_dir"], "-c", "core.quotepath=false", *args]
    name = next((a for a in args if not a.startswith("-") and "=" not in a), args[0])  # "fetch", "push", ...

    def run(extra):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  env=dict(env, **extra), timeout=timeout, **_hidden())
        except subprocess.TimeoutExpired:
            raise GitError(f"git {name} took longer than {timeout} seconds and was stopped. "
                           "Check your internet connection.")

    r = run(auth)
    if r.returncode != 0 and auth and name != "push" and _AUTH_FAILED.search(r.stderr or ""):
        # The saved GitHub login was revoked: reading a public library
        # doesn't need one, so don't let it block getting new parts.
        r = run({})
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

def repo_url(cfg):
    """The library repository this computer uses (Setup can point it at
    another team's copy of kicad-parts)."""
    return cfg.get("repo_url") or REPO_URL


def lib_prefix(parts_dir):
    """Library name prefix, set by the library repository's categories.json."""
    try:
        return json.loads((Path(parts_dir) / "categories.json").read_text(encoding="utf-8"))["prefix"]
    except (OSError, ValueError, KeyError):
        return LIB_PREFIX


def libraries(parts_dir):
    """{nickname: {"sym": path, "fp": path}} for every library in the parts repo."""
    parts = Path(parts_dir)
    prefix = lib_prefix(parts_dir)
    libs = {}
    for d in sorted((parts / "symbols").glob(prefix + "*.kicad_symdir")):
        libs.setdefault(d.stem, {})["sym"] = d
    for d in sorted((parts / "footprints").glob(prefix + "*.pretty")):
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
            # Drop the entries this plugin added before (marked in their
            # description; "Plus381_" names from older versions), so a
            # switch to another library repository leaves nothing behind.
            for s, e, name in reversed(entries):
                if MANAGED_MARK in text[s:e] or name.startswith(LIB_PREFIX):
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
                write_atomic(path, new)
    return problems


def misplaced_libraries(parts_dir):
    """Team libraries that KiCad loads from somewhere other than parts_dir
    (left over from an older setup, or added by hand). Edits saved to them
    never reach the team. Returns ["nickname: path", ...]."""
    root = os.path.normcase(os.path.realpath(parts_dir))
    prefix = lib_prefix(parts_dir)
    out = []
    for cfgdir in kicad_config_dirs():
        for table in ("sym-lib-table", "fp-lib-table"):
            try:
                text = (cfgdir / table).read_text(encoding="utf-8")
            except OSError:
                continue
            for s, e, name in _lib_entries(text):
                block = text[s:e]
                if not (name.startswith(prefix) or MANAGED_MARK in block):
                    continue
                m = re.search(r'\(uri\s+"((?:[^"\\]|\\.)*)"\)', block)
                uri = (m.group(1) if m else "").replace("${%s}" % VAR, str(parts_dir))
                path = os.path.normcase(os.path.realpath(os.path.expandvars(uri)))
                if path != root and not path.startswith(root + os.sep):
                    out.append(f"{name}: {uri}")
    return sorted(set(out))


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
    problems = []
    for cfgdir in dirs:
        path = cfgdir / "kicad_common.json"
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            d = {}
        except (OSError, ValueError) as e:
            # Never replace settings that can't be read: that would reset
            # all of KiCad's common settings.
            problems.append(f"{path} could not be read ({e}), so the path variable {VAR} was not saved there. "
                            f"Set it by hand in Preferences > Configure Paths: {want}")
            continue
        if not isinstance(d.get("environment"), dict):
            d["environment"] = {}
        if not isinstance(d["environment"].get("vars"), dict):
            d["environment"]["vars"] = {}
        d["environment"]["vars"][VAR] = want
        write_atomic(path, json.dumps(d, indent=2))
    return "\n".join(problems) or "ok"


def configure_kicad(parts_dir):
    """Library tables and path variable; returns (problems, env_state)."""
    return ensure_lib_tables(parts_dir), ensure_env_var(parts_dir)


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def _pid_alive(pid):
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k.GetLastError() == 5  # access denied: it exists, as another user
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class Lock:
    """Stops the background check and the plugin from running git at the same time."""

    STALE = 2 * 3600  # seconds after which a lock is given up whatever it says

    def __init__(self, wait=60):
        self.path = app_dir() / "sync.lock"
        self.wait = wait
        self.mine = f"{os.getpid()}.{time.time_ns()}"

    def _stale(self):
        """Left behind by a run that crashed or was closed with KiCad?"""
        try:
            age = time.time() - self.path.stat().st_mtime
            owner = self.path.read_text().strip()
        except OSError:
            return False  # just released, or being written
        try:
            pid = int(owner.split(".")[0])
        except ValueError:
            return age > 30  # empty: its owner died between creating and writing it
        return age > self.STALE or not _pid_alive(pid)

    def __enter__(self):
        deadline = time.time() + self.wait
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, self.mine.encode())
                finally:
                    os.close(fd)
                return self
            except (FileExistsError, PermissionError):  # PermissionError: Windows, while it is being deleted
                if self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.time() > deadline:
                    raise GitError("Another sync is still running, try again in a minute.")
                time.sleep(0.5)

    def __exit__(self, *exc):
        try:
            if self.path.read_text().strip() == self.mine:  # not someone else's newer lock
                self.path.unlink()
        except OSError:
            pass


def is_repo(cfg):
    return bool(cfg.get("parts_dir")) and (Path(cfg["parts_dir"]) / ".git").exists()


def changed_files(cfg):
    """Local changes not yet committed: list of (status, path)."""
    # -z: paths exactly as they are (spaces, any letters), separated by NUL.
    fields = git(cfg, "status", "--porcelain", "-z", "-uall").stdout.split("\0")
    files = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) > 3:
            files.append((entry[:2].strip(), entry[3:]))
            if entry[0] in "RC":
                i += 1  # a rename or copy is followed by the old path
    return files


def diff_files(cfg, *revs):
    """Files that differ between revisions (git diff): list of (status
    letter, path). A moved file is listed as deleted and added."""
    fields = git(cfg, "diff", "--name-status", "--no-renames", "-z", *revs).stdout.split("\0")
    return [(fields[i][:1], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def my_files(cfg):
    """Files changed by local commits the team doesn't have yet (not what
    the team changed meanwhile): list of (status letter, path)."""
    return diff_files(cfg, f"origin/{BRANCH}...HEAD")


def ahead_behind(cfg):
    out = git(cfg, "rev-list", "--left-right", "--count", f"HEAD...origin/{BRANCH}").stdout.split()
    return int(out[0]), int(out[1])


class Conflict(GitError):
    def __init__(self, files):
        super().__init__("Someone else changed the same files: " + ", ".join(files))
        self.files = files


def _rebasing(cfg):
    out = git(cfg, "rev-parse", "--git-path", "rebase-merge", "--git-path", "rebase-apply").stdout.split("\n")
    return any(p and (Path(cfg["parts_dir"]) / p).exists() for p in out)


def _unmerged(cfg):
    return [f for f in git(cfg, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split("\0") if f]


def rebase_on_team(cfg, keep=None):
    """Put the local commits on top of the team's newest version
    (origin/main must be fetched). Call with the Lock held.

    keep=None: stop at the first file both sides changed, leave everything
    as it was and raise Conflict. keep="mine" / "theirs": for every such
    file take that side's whole version (a part is one file: mixing two
    edits of it line by line could break it). A file one side deleted is
    handled the same way."""
    if not ahead_behind(cfg)[1]:
        return
    mine = {path for _, path in my_files(cfg)}
    if not mine & {path for _, path in diff_files(cfg, "HEAD", f"origin/{BRANCH}")}:
        # Everything in the local commits is already in the team's version
        # (e.g. a pull request merged as one squashed commit): drop them
        # instead of replaying them, which would only produce conflicts.
        git(cfg, "reset", "--keep", "--quiet", f"origin/{BRANCH}")
        return
    r = git(cfg, "-c", "core.editor=true", "rebase", "--quiet", f"origin/{BRANCH}", check=False)
    for _ in range(10000):
        if not _rebasing(cfg):
            if r.returncode != 0:
                raise GitError("Could not combine your changes with the team's:\n" + (r.stderr or r.stdout).strip())
            return
        files = _unmerged(cfg)
        if keep is None or not files:
            git(cfg, "rebase", "--abort", check=False)
            if files:
                raise Conflict(files)
            raise GitError("Could not combine your changes with the team's:\n" + (r.stderr or r.stdout).strip())
        # While rebasing, git calls the team's version "ours" (stage 2) and
        # the local commit being replayed "theirs" (stage 3).
        stage, side = ("3", "--theirs") if keep == "mine" else ("2", "--ours")
        for f in files:
            if git(cfg, "cat-file", "-e", f":{stage}:{f}", check=False).returncode == 0:
                git(cfg, "checkout", side, "--", f)
                git(cfg, "add", "--", f)
            else:  # that side deleted the file
                git(cfg, "rm", "--quiet", "--force", "--", f)
        r = git(cfg, "-c", "core.editor=true", "rebase", "--continue", check=False)
        if r.returncode != 0 and _rebasing(cfg) and not _unmerged(cfg):
            # Nothing left of this commit after taking the other side.
            r = git(cfg, "-c", "core.editor=true", "rebase", "--skip", check=False)
    git(cfg, "rebase", "--abort", check=False)
    raise GitError("Could not combine your changes with the team's.")


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
                try:
                    rebase_on_team(cfg)
                except Conflict as c:
                    raise GitError("The team changed part(s) that you changed too and haven't published yet: "
                                   + ", ".join(c.files) + ".\nClick Publish to choose which version to keep.")
        ahead, behind = ahead_behind(cfg)
        # People without write access publish through pull requests: their
        # commits wait locally until the pull request is merged.
        direct = load_config().get("publish_mode") != "pr"
        if push and direct and ahead and not behind:
            git(cfg, "push", "--quiet", "origin", f"HEAD:{BRANCH}")
        after = git(cfg, "rev-parse", "HEAD").stdout.strip()
        if after != before:
            st["new_files"] = [path for _, path in diff_files(cfg, before, after)]
        problems, env = configure_kicad(cfg["parts_dir"])
        st["kicad_problems"] = problems
        st["env_var"] = env
        st["ahead"], st["behind"] = ahead_behind(cfg)
        st["dirty"] = len(changed_files(cfg))
    return st


def check_updates(cfg):
    """Look at GitHub for parts the team added or changed, without changing
    anything locally (git fetch only). Returns the number of changed files
    and the names of the changed parts."""
    with Lock():
        # A short time limit: this runs in the background holding the lock,
        # and a dead connection must not block Publish/Update for minutes.
        git(cfg, "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=30",
            "fetch", "--quiet", "origin", BRANCH, timeout=120)
        _, behind = ahead_behind(cfg)
        names = []
        if behind:
            out = [path for _, path in diff_files(cfg, f"HEAD...origin/{BRANCH}")]
            for path in out:
                name, ext = os.path.splitext(os.path.basename(path))
                if ext in (".kicad_sym", ".kicad_mod") and name not in names:
                    names.append(name)
            return len(out), names
    return 0, names


def run_check(cfg):
    """check_updates() with its result in the status file, where the toolbar
    buttons (red dot) and Library status read it."""
    st = load_status()
    try:
        n, names = check_updates(cfg)
        st.update(updates=n, update_parts=names, check_error="")
    except Exception as e:  # noqa: BLE001 - offline etc.: keep the last result
        st["check_error"] = str(e)
    st["checked"] = datetime.now().isoformat(timespec="seconds")
    _write_status(st)
    return st


def run_sync(cfg, quiet=False, push=True):
    """sync() with error handling and the result in the status file."""
    try:
        st = sync(cfg, push=push)
    except Exception as e:  # noqa: BLE001 - report instead of crashing
        st = {"ok": False, "message": str(e)}
    old = load_status()
    if st.get("ok"):  # a sync looks at GitHub too
        st.update(checked=datetime.now().isoformat(timespec="seconds"), check_error="")
    else:
        for k in ("checked", "check_error"):
            if k in old:
                st.setdefault(k, old[k])
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


# --------------------------------------------------------------------------
# Checking for new parts while KiCad is open
# --------------------------------------------------------------------------

CHECK_EVERY = 120   # seconds between looks at GitHub while KiCad is open
KICAD_EVERY = 15    # seconds between "is KiCad still open?" checks
KICAD_PENDING_EVERY = 3   # ... while KiCad's settings wait to be written (it must be closed for that)


def start_watcher():
    """Start the background check for new parts. Called whenever the plugin
    runs: when the PCB editor opens (it loads the plugin) and on every
    Plus381 button. A second copy notices the first one and exits at once."""
    py = base_python()
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "cwd": str(app_dir())}
    if IS_WIN:
        pyw = Path(py).with_name("pythonw.exe")
        py = str(pyw) if pyw.exists() else py
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([py, os.path.abspath(__file__), "watch"], **kwargs)
    except OSError:
        pass  # Library status still checks when it opens


def _only_watcher():
    """Hold watch.lock while this process lives; None if another watcher holds it."""
    f = open(app_dir() / "watch.lock", "a+")
    try:
        if IS_WIN:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def watch():
    """Every CHECK_EVERY seconds while KiCad is open, look whether the team
    has new parts (fetch only: nothing in the library changes until the
    user clicks Update). When KiCad closes, write its settings (KiCad
    overwrites them while it runs) and exit."""
    lock = _only_watcher()
    if not lock:
        return 0
    try:
        return _watch(lock)
    finally:
        lock.close()


def _watch(lock):
    remove_old_scheduler()
    me = os.path.abspath(__file__)
    code = Path(me).read_bytes()
    last = 0.0
    closed = 0
    started = time.time()
    while True:
        try:
            changed = Path(me).read_bytes() != code
        except OSError:
            return 0  # plugin uninstalled
        if changed:  # plugin updated: continue with the new code
            lock.close()
            os.execv(sys.executable, [sys.executable, me, "watch"])
        cfg = load_config()
        repo = is_repo(cfg)
        if kicad_running():
            closed = 0
            if cfg.get("check_updates", True) and repo and time.time() - last >= CHECK_EVERY:
                run_check(cfg)
                last = time.time()
        else:
            closed += 1
            if repo:
                try:  # right away: the user may reopen KiCad within seconds
                    configure_kicad(cfg["parts_dir"])
                except Exception:  # noqa: BLE001 - tried again next time
                    pass
            if closed >= 2:  # seen closed twice: not just restarting
                return 0
        # Settings still to be written (right after Setup): look often for a
        # while, so a quick "close KiCad and open it again" is not missed.
        waiting = repo and closed == 0 and time.time() - started < 900 and not env_var_ok(cfg["parts_dir"])
        time.sleep(KICAD_PENDING_EVERY if waiting else KICAD_EVERY)


def remove_old_scheduler():
    """Versions before 0.7 synced through the system scheduler (Task
    Scheduler, launchd, systemd or cron), every minute. Remove that once."""
    cfg = load_config()
    if cfg.get("old_scheduler_removed"):
        return

    def run(*cmd, input=None):
        try:
            return subprocess.run(list(cmd), capture_output=True, text=True, input=input, timeout=60,
                                  **_hidden())
        except (OSError, subprocess.TimeoutExpired):
            return None

    gone = True
    if IS_WIN:
        run("schtasks", "/Delete", "/F", "/TN", "Plus381 Parts Sync")
        r = run("schtasks", "/Query", "/TN", "Plus381 Parts Sync")
        gone = r is not None and r.returncode != 0
    elif IS_MAC:
        plist = Path.home() / "Library" / "LaunchAgents" / "com.plus381.partsync.plist"
        if plist.exists():
            run("launchctl", "bootout", f"gui/{os.getuid()}", str(plist))
            plist.unlink()
    else:
        if shutil.which("systemctl"):
            run("systemctl", "--user", "disable", "--now", "plus381-partsync.timer")
            d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
            for f in (d / "plus381-partsync.service", d / "plus381-partsync.timer"):
                if f.exists():
                    f.unlink()
            run("systemctl", "--user", "daemon-reload")
        if shutil.which("crontab"):
            r = run("crontab", "-l")
            if r is not None and r.returncode == 0 and "# plus381-partsync" in r.stdout:
                keep = "".join(l + "\n" for l in r.stdout.splitlines() if "# plus381-partsync" not in l)
                run("crontab", "-", input=keep)
    if not gone:
        return  # try again next time; its script stays so it keeps working meanwhile
    for name in ("partsync.py", "partsync.vbs"):
        try:
            (app_dir() / name).unlink()
        except OSError:
            pass
    cfg = load_config()
    cfg["old_scheduler_removed"] = True
    cfg.pop("plugin_partsync", None)
    save_config(cfg)


def update_self(cfg, argv):
    """An old scheduler job runs a copy of this file (see
    remove_old_scheduler). Until it is removed, let it run the newest code."""
    src = cfg.get("plugin_partsync")
    me = os.path.abspath(__file__)
    try:
        if src and os.path.isfile(src) and os.path.abspath(src) != me and \
                Path(src).read_bytes() != Path(me).read_bytes():
            shutil.copyfile(src, me)
            os.execv(sys.executable, [sys.executable, me] + argv[1:])
    except OSError:
        pass  # keep running the current copy


def main(argv):
    cfg = load_config()
    update_self(cfg, argv)
    cmd = argv[1] if len(argv) > 1 else "sync"
    if cmd == "watch":
        return watch()
    if not is_repo(cfg):
        print("Plus381 parts library is not set up (run Setup in the KiCad plugin).")
        return 1
    if cmd == "check":
        st = run_check(cfg)
        print(json.dumps({k: st.get(k) for k in ("updates", "update_parts", "check_error")}, indent=2))
        return 0
    if cmd == "sync":
        if "--always" not in argv and not kicad_running():
            # KiCad closed (an old scheduler job): nothing to sync for. Also
            # the moment the path variable can be written safely.
            ensure_env_var(cfg["parts_dir"])
            return 0
        st = run_sync(cfg, quiet="--quiet" in argv)
        return 0 if st.get("ok") else 1
    if cmd == "configure-kicad":
        # Wait (up to 12 h) until KiCad is closed, then write settings.
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
