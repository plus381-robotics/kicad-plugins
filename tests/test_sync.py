"""Publishing, updating and conflicts against throw-away Git repositories."""

import json
import unittest

from common import SYMDIR, RepoCase, run_git
from plus381 import partsync, publish
from plus381.partsync import Conflict


class PendingChanges(RepoCase):
    def test_names_with_spaces_and_other_letters(self):
        self.write(self.mine, "My Part", "x\n")
        self.write(self.mine, "Otpornik_šč", "x\n")
        self.write(self.mine, "Base", "edited\n")
        self.assertEqual(sorted(partsync.changed_files(self.cfg)), [
            ("??", f"{SYMDIR}/My Part.kicad_sym"),
            ("??", f"{SYMDIR}/Otpornik_šč.kicad_sym"),
            ("M", f"{SYMDIR}/Base.kicad_sym")])
        names = sorted(i[0] for i in publish.pending_changes(self.cfg))
        self.assertEqual(names, ["Plus381_ICs:Base", "Plus381_ICs:My Part", "Plus381_ICs:Otpornik_šč"])

    def test_team_parts_are_not_listed_as_my_deletions(self):
        """With an unpublished commit of mine and new parts from the team,
        only my part is listed (the team's used to show up as "deleted")."""
        self.write(self.mine, "Mine", "mine\n")
        self.commit(self.mine, "mine")
        self.write(self.team, "TeamPart", "team\n")
        self.team_publishes()
        run_git(self.mine, "fetch", "-q", "origin")
        items = publish.pending_changes(self.cfg)
        self.assertEqual([(i[0], i[2]) for i in items], [("Plus381_ICs:Mine", "new, not uploaded yet")])

    def test_check_names_parts_with_spaces(self):
        self.write(self.team, "Team Part", "team\n")
        self.team_publishes()
        self.assertEqual(partsync.check_updates(self.cfg), (1, ["Team Part"]))


class Publish(RepoCase):
    def test_publish_on_top_of_team_changes(self):
        self.write(self.team, "TeamPart", "team\n")
        self.team_publishes()
        self.write(self.mine, "Mine", "mine\n")
        publish.publish(self.cfg, "Add Mine")
        self.assertEqual(self.on_github("Mine"), "mine\n")
        self.assertEqual(self.parts(self.mine), ["Base", "Mine", "TeamPart"])
        self.assertEqual(publish.pending_changes(self.cfg), [])

    def test_conflict_leaves_everything_as_it_was(self):
        self.write(self.team, "Base", "team\n")
        self.team_publishes()
        self.write(self.mine, "Base", "mine\n")
        with self.assertRaises(Conflict) as c:
            publish.publish(self.cfg, "Edit Base")
        self.assertEqual(c.exception.files, [f"{SYMDIR}/Base.kicad_sym"])
        self.assertEqual(self.part(self.mine, "Base").read_text(), "mine\n")
        self.assertEqual(self.on_github("Base"), "team\n")
        self.assertFalse(partsync._rebasing(self.cfg))


class Resolve(RepoCase):
    def conflict_on_base(self):
        """I edited Base and added three parts; the team edited Base too."""
        self.write(self.team, "Base", "team\n")
        self.team_publishes()
        self.write(self.mine, "Base", "mine\n")
        self.write(self.mine, "My Part", "space\n")
        self.write(self.mine, "Otpornik_šč", "letters\n")
        self.write(self.mine, "Plain", "plain\n")
        with self.assertRaises(Conflict):
            publish.publish(self.cfg, "My parts")

    def test_keep_theirs_keeps_my_other_parts(self):
        self.conflict_on_base()
        backup, ahead = publish.resolve(self.cfg, "theirs")
        self.assertEqual(self.part(self.mine, "Base").read_text(), "team\n")
        # My other parts are still in the library and were published.
        self.assertEqual(self.parts(self.mine), ["Base", "My Part", "Otpornik_šč", "Plain"])
        self.assertEqual(ahead, 1)
        self.assertEqual(self.on_github("My Part"), "space\n")
        self.assertEqual(self.on_github("Otpornik_šč"), "letters\n")
        self.assertEqual(self.on_github("Base"), "team\n")
        # My version of everything is in the backup, whatever the file is called.
        self.assertEqual(sorted(p.stem for p in backup.rglob("*.kicad_sym")),
                         ["Base", "My Part", "Otpornik_šč", "Plain"])
        self.assertEqual((backup / SYMDIR / "Base.kicad_sym").read_text(), "mine\n")

    def test_keep_mine(self):
        self.conflict_on_base()
        backup, ahead = publish.resolve(self.cfg, "mine")
        self.assertIsNone(backup)
        self.assertEqual(self.on_github("Base"), "mine\n")
        self.assertEqual(self.on_github("Plain"), "plain\n")

    def test_keep_for_pull_request_does_not_push(self):
        self.conflict_on_base()
        _, ahead = publish.resolve(self.cfg, "mine", push=False)
        self.assertEqual(ahead, 1)
        self.assertEqual(self.on_github("Base"), "team\n")
        self.assertEqual(self.part(self.mine, "Base").read_text(), "mine\n")

    def test_whole_file_is_taken_not_single_lines(self):
        """Both edited different lines of one part: git could merge that
        line by line when another line conflicts, giving a part nobody made."""
        text = "".join(f"line {i}\n" for i in range(20))
        self.write(self.team, "Big", text)
        self.team_publishes()
        run_git(self.mine, "pull", "-q", "origin", "main")
        self.write(self.team, "Big", text.replace("line 1\n", "team 1\n").replace("line 18\n", "team 18\n"))
        self.team_publishes()
        mine = text.replace("line 1\n", "mine 1\n").replace("line 10\n", "mine 10\n")
        self.write(self.mine, "Big", mine)
        with self.assertRaises(Conflict):
            publish.publish(self.cfg, "Edit Big")
        publish.resolve(self.cfg, "mine")
        self.assertEqual(self.on_github("Big"), mine)

    def deleted_by_team(self):
        run_git(self.team, "rm", "-q", f"{SYMDIR}/Base.kicad_sym")
        self.write(self.team, "Other", "other\n")
        self.team_publishes()
        self.write(self.mine, "Base", "mine\n")
        with self.assertRaises(Conflict):
            publish.publish(self.cfg, "Edit Base")

    def test_team_deleted_my_edited_part_keep_mine(self):
        self.deleted_by_team()
        publish.resolve(self.cfg, "mine")
        self.assertEqual(self.on_github("Base"), "mine\n")

    def test_team_deleted_my_edited_part_keep_theirs(self):
        self.deleted_by_team()
        backup, ahead = publish.resolve(self.cfg, "theirs")
        self.assertEqual(self.parts(self.mine), ["Other"])
        self.assertEqual(ahead, 0)
        self.assertEqual((backup / SYMDIR / "Base.kicad_sym").read_text(), "mine\n")

    def deleted_by_me(self):
        self.write(self.team, "Base", "team\n")
        self.team_publishes()
        self.part(self.mine, "Base").unlink()
        self.write(self.mine, "Other", "other\n")
        with self.assertRaises(Conflict):
            publish.publish(self.cfg, "Remove Base")

    def test_i_deleted_a_part_the_team_edited_keep_mine(self):
        self.deleted_by_me()
        publish.resolve(self.cfg, "mine")
        self.assertIsNone(self.on_github("Base"))
        self.assertEqual(self.on_github("Other"), "other\n")

    def test_i_deleted_a_part_the_team_edited_keep_theirs(self):
        self.deleted_by_me()
        publish.resolve(self.cfg, "theirs")
        self.assertEqual(self.on_github("Base"), "team\n")
        self.assertEqual(self.on_github("Other"), "other\n")

    def test_several_commits_with_conflicts(self):
        self.write(self.team, "Base", "team\n")
        self.team_publishes()
        for i in (1, 2, 3):
            self.write(self.mine, "Base", f"mine {i}\n")
            self.write(self.mine, f"New{i}", "new\n")
            self.commit(self.mine, f"edit {i}")
        with self.assertRaises(Conflict):
            publish.publish(self.cfg, "unused")
        publish.resolve(self.cfg, "theirs")
        self.assertEqual(self.on_github("Base"), "team\n")
        self.assertEqual(self.parts(self.mine), ["Base", "New1", "New2", "New3"])
        self.assertEqual(self.on_github("New3"), "new\n")


class Update(RepoCase):
    def setUp(self):
        super().setUp()
        self.patch(partsync, "configure_kicad", lambda parts_dir: ([], "ok"))

    def test_update_gets_new_parts(self):
        self.write(self.team, "Team Part", "team\n")
        self.team_publishes()
        st = partsync.run_sync(self.cfg, quiet=True, push=False)
        self.assertTrue(st["ok"])
        self.assertEqual(st["new_files"], [f"{SYMDIR}/Team Part.kicad_sym"])
        saved = partsync.load_status()
        self.assertEqual((saved["updates"], saved["check_error"]), (0, ""))
        self.assertTrue(saved["checked"])

    def test_pull_request_merged_as_one_squashed_commit(self):
        """Two local commits to the same new part, merged upstream as one
        commit: nothing of mine is left to publish and Update just works."""
        cfg = partsync.load_config()
        cfg["publish_mode"] = "pr"
        partsync.save_config(cfg)
        for text in ("v1\n", "v2\n"):
            self.write(self.mine, "New", text)
            self.commit(self.mine, "New " + text.strip())
        self.write(self.team, "New", "v2\n")
        self.write(self.team, "Other", "other\n")
        self.team_publishes("New (#1)")
        st = partsync.run_sync(self.cfg, quiet=True, push=False)
        self.assertTrue(st["ok"], st.get("message"))
        self.assertEqual((st["ahead"], st["behind"]), (0, 0))
        self.assertEqual(self.parts(self.mine), ["Base", "New", "Other"])

    def test_conflict_points_to_publish(self):
        self.write(self.mine, "Base", "mine\n")
        self.commit(self.mine, "mine")
        self.write(self.team, "Base", "team\n")
        self.team_publishes()
        st = partsync.run_sync(self.cfg, quiet=True, push=False)
        self.assertFalse(st["ok"])
        self.assertIn("Click Publish", st["message"])
        self.assertEqual(self.part(self.mine, "Base").read_text(), "mine\n")
        self.assertFalse(partsync._rebasing(self.cfg))

    def test_uncommitted_edits_are_never_touched(self):
        self.write(self.mine, "Base", "editing\n")
        self.write(self.team, "Base", "team\n")
        self.team_publishes()
        st = partsync.run_sync(self.cfg, quiet=True, push=False)
        self.assertFalse(st["ok"])
        self.assertEqual(self.part(self.mine, "Base").read_text(), "editing\n")


class GitProgram(RepoCase):
    def test_remembered_git_that_is_gone(self):
        """GitHub Desktop keeps Git in a folder that changes with each update."""
        cfg = dict(self.cfg, git=str(self.tmp / "GitHubDesktop" / "app-3.4.1" / "git.exe"))
        self.assertEqual(partsync.git(cfg, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")

    def test_login_only_goes_to_github(self):
        partsync.save_token("secret-token")
        for url in ("https://github.com/plus381-robotics/kicad-parts.git", "https://git.example.com/x/parts.git"):
            env = partsync._auth_env({"repo_url": url, "github_login": "someone"})
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
            self.assertNotIn("secret-token", json.dumps(env))


if __name__ == "__main__":
    unittest.main()
