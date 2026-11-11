"""Settings files, the sync lock and KiCad's own settings."""

import json
import os
import subprocess
import sys
import unittest

from common import Case
from plus381 import partsync

TABLE = '''(sym_lib_table
  (version 7)
  (lib (name "Device")(type "KiCad")(uri "${KICAD10_SYMBOL_DIR}/Device.kicad_sym")(options "")(descr "Basic (lib parts)"))
  (lib (name "Plus381_Old")(type "KiCad")(uri "/somewhere/else")(options "")(descr ""))
)
'''


class Files(Case):
    def test_config_round_trip_leaves_no_temporary_files(self):
        partsync.save_config({"parts_dir": "/x", "name": "šč"})
        partsync.save_config({"parts_dir": "/y", "name": "šč"})
        self.assertEqual(partsync.load_config(), {"parts_dir": "/y", "name": "šč"})
        self.assertEqual(sorted(p.name for p in partsync.app_dir().iterdir()), ["config.json"])

    def test_failed_write_keeps_the_old_file(self):
        partsync.save_config({"parts_dir": "/x"})
        with self.assertRaises(TypeError):
            partsync.write_atomic(partsync.config_path(), None)
        self.assertEqual(partsync.load_config(), {"parts_dir": "/x"})
        self.assertEqual(sorted(p.name for p in partsync.app_dir().iterdir()), ["config.json"])


class Locking(Case):
    def test_only_one_at_a_time(self):
        with partsync.Lock():
            with self.assertRaises(partsync.GitError):
                with partsync.Lock(wait=0):
                    pass
        with partsync.Lock(wait=0):
            pass
        self.assertFalse((partsync.app_dir() / "sync.lock").exists())

    def test_lock_of_a_dead_process_is_taken_over(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (partsync.app_dir() / "sync.lock").write_text(f"{dead.pid}.1")
        with partsync.Lock(wait=0):
            pass

    def test_lock_of_a_living_process_is_respected(self):
        (partsync.app_dir() / "sync.lock").write_text(f"{os.getpid()}.1")
        with self.assertRaises(partsync.GitError):
            with partsync.Lock(wait=0):
                pass

    def test_release_leaves_a_newer_lock_alone(self):
        path = partsync.app_dir() / "sync.lock"
        with partsync.Lock():
            path.write_text(f"{os.getpid()}.999")  # as if taken over by another run
        self.assertTrue(path.exists())


class KiCadSettings(Case):
    def setUp(self):
        super().setUp()
        self.kicad = self.tmp / "kicad" / "10.0"
        self.kicad.mkdir(parents=True)
        self.parts = self.tmp / "parts"
        for d in ("symbols/Plus381_ICs.kicad_symdir", "footprints/Plus381_ICs.pretty"):
            (self.parts / d).mkdir(parents=True)
        self.patch(partsync, "kicad_running", lambda: False)

    def test_path_variable_is_added_to_existing_settings(self):
        common = self.kicad / "kicad_common.json"
        common.write_text(json.dumps({"appearance": {"icon_theme": 1}, "environment": {"vars": {"A": "b"}}}))
        self.assertEqual(partsync.ensure_env_var(self.parts), "ok")
        data = json.loads(common.read_text())
        self.assertEqual(data["appearance"], {"icon_theme": 1})
        self.assertEqual(data["environment"]["vars"], {"A": "b", "PLUS381_PARTS": self.parts.as_posix()})
        self.assertTrue(partsync.env_var_ok(self.parts))

    def test_unreadable_settings_are_not_replaced(self):
        common = self.kicad / "kicad_common.json"
        common.write_text('{"appearance": {"icon_theme": 1}, "environ')  # cut off
        result = partsync.ensure_env_var(self.parts)
        self.assertIn("could not be read", result)
        self.assertEqual(common.read_text(), '{"appearance": {"icon_theme": 1}, "environ')

    def test_not_written_while_kicad_runs(self):
        self.patch(partsync, "kicad_running", lambda: True)
        (self.kicad / "kicad_common.json").write_text("{}")
        self.assertEqual(partsync.ensure_env_var(self.parts), "pending")
        self.assertEqual((self.kicad / "kicad_common.json").read_text(), "{}")

    def test_library_tables(self):
        (self.kicad / "sym-lib-table").write_text(TABLE)
        (self.kicad / "fp-lib-table").write_text("(fp_lib_table\n  (version 7)\n)\n")
        self.assertEqual(partsync.ensure_lib_tables(self.parts), [])
        text = (self.kicad / "sym-lib-table").read_text()
        self.assertEqual([name for _, _, name in partsync._lib_entries(text)], ["Device", "Plus381_ICs"])
        self.assertIn(f'(uri "{(self.parts / "symbols/Plus381_ICs.kicad_symdir").as_posix()}")', text)
        self.assertIn('(descr "Basic (lib parts)")', text)
        self.assertIn("Plus381_ICs.pretty", (self.kicad / "fp-lib-table").read_text())
        # Running it again changes nothing.
        partsync.ensure_lib_tables(self.parts)
        self.assertEqual((self.kicad / "sym-lib-table").read_text(), text)
        self.assertEqual(partsync.misplaced_libraries(self.parts), [])


class Watcher(Case):
    def watch(self, running):
        """Run the background check with KiCad open/closed as listed.
        Returns what it did: ("settings",) and ("sleep", seconds)."""
        did = []
        states = list(running)
        parts = self.tmp / "parts"
        (parts / ".git").mkdir(parents=True)
        partsync.save_config({"parts_dir": str(parts), "check_updates": False})
        self.patch(partsync, "remove_old_scheduler", lambda: None)
        self.patch(partsync, "kicad_running", lambda: states.pop(0))
        self.patch(partsync, "env_var_ok", lambda parts_dir: False)
        self.patch(partsync, "configure_kicad", lambda parts_dir: did.append(("settings",)))
        self.patch(partsync.time, "sleep", lambda s: did.append(("sleep", s)))
        self.assertEqual(partsync.watch(), 0)
        self.assertEqual(states, [])
        return did

    def test_settings_are_written_as_soon_as_kicad_closes(self):
        """After Setup people close KiCad and reopen it within seconds: the
        path variable has to be written in that gap."""
        did = self.watch([True, True, False, False])
        self.assertEqual(did, [("sleep", 3), ("sleep", 3), ("settings",), ("sleep", 15), ("settings",)])

    def test_keeps_running_through_a_restart(self):
        did = self.watch([True, False, True, False, False])
        self.assertEqual([d for d in did if d[0] == "settings"], [("settings",)] * 3)


if __name__ == "__main__":
    unittest.main()
