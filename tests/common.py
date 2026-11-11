"""Shared set-up for the tests: a private settings folder and throw-away
Git repositories (a "GitHub" origin, my library folder and a teammate's)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plus381-tools"))

from plus381 import partsync  # noqa: E402

SYMDIR = "symbols/Plus381_ICs.kicad_symdir"


def run_git(folder, *args):
    r = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout


class Case(unittest.TestCase):
    """Every test gets its own settings folder, so nothing touches the real one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plus381-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {"HOME": str(self.tmp / "home"), "USERPROFILE": str(self.tmp / "home"),
               "XDG_CONFIG_HOME": str(self.tmp / "config"), "APPDATA": str(self.tmp / "config"),
               "KICAD_CONFIG_HOME": str(self.tmp / "kicad"),
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        for k, v in env.items():
            self._setenv(k, v)
        for k in ("GIT_CONFIG_COUNT", "PLUS381_PARTS_REPO"):
            self._setenv(k, None)
        (self.tmp / "home").mkdir()

    def _setenv(self, key, value):
        old = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
        self.addCleanup(lambda: os.environ.pop(key, None) if old is None else os.environ.__setitem__(key, old))

    def patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)


class RepoCase(Case):
    """self.mine: my library folder, self.team: a teammate's, both clones of
    one origin that already holds the part "Base"."""

    def setUp(self):
        super().setUp()
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        for who in ("mine", "team"):
            folder = self.tmp / who
            subprocess.run(["git", "clone", "-q", str(origin), str(folder)], check=True, capture_output=True)
            run_git(folder, "config", "user.name", who)
            run_git(folder, "config", "user.email", f"{who}@example.com")
            run_git(folder, "checkout", "-q", "-B", "main")
            setattr(self, who, folder)
        self.write(self.mine, "Base", "base\n")
        (self.mine / "categories.json").write_text('{"prefix": "Plus381_", "categories": {"ICs": "Chips"}}')
        self.commit(self.mine, "start")
        run_git(self.mine, "push", "-q", "origin", "HEAD:main")
        run_git(self.mine, "fetch", "-q", "origin")
        run_git(self.team, "pull", "-q", "origin", "main")
        self.cfg = {"parts_dir": str(self.mine), "git": shutil.which("git")}
        partsync.save_config(dict(self.cfg))

    @staticmethod
    def part(folder, name):
        return Path(folder) / SYMDIR / f"{name}.kicad_sym"

    def write(self, folder, name, text):
        f = self.part(folder, name)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    def commit(self, folder, message):
        run_git(folder, "add", "-A")
        run_git(folder, "commit", "-q", "-m", message)

    def team_publishes(self, message="team"):
        self.commit(self.team, message)
        run_git(self.team, "push", "-q", "origin", "HEAD:main")

    def parts(self, folder):
        return sorted(p.stem for p in (Path(folder) / SYMDIR).iterdir())

    def on_github(self, name):
        """Content of a part in origin's main, or None."""
        r = subprocess.run(["git", "-C", str(self.tmp / "origin.git"), "show", f"main:{SYMDIR}/{name}.kicad_sym"],
                           capture_output=True, text=True, encoding="utf-8")
        return r.stdout if r.returncode == 0 else None
