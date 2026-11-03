"""Tests for where app.py keeps its files, and how Import hands a dump to the cpap repo.

A one-file PyInstaller exe unpacks itself into a temporary %TEMP%\\_MEIxxxx
folder that Windows deletes when the app closes, and __file__ points into it.
So the frozen app must not keep dump.txt or the converted tree beside
__file__ (a Pull would vanish with the window); it keeps its data in
%LOCALAPPDATA%\\TranscendSync.

Import runs the cpap repo's transcend_import.py (~/projects/cpap) on the dump,
which stores it and refreshes SleepHQ Local. It needs a real Python with numpy,
so the frozen exe finds one, skipping the Microsoft Store placeholder.

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sleephq"))

import app  # noqa: E402

HERE = os.path.join("C:\\", "Temp", "_MEI1234")          # where a frozen __file__ points
LOCAL = os.path.join("C:\\", "Users", "scott", "AppData", "Local")
HOME = os.path.join("C:\\", "Users", "scott")


class TestAppPaths(unittest.TestCase):
    def test_frozen_data_lives_in_localappdata_not_the_unpack_folder(self):
        p = app.app_paths(frozen=True, here=HERE, localappdata=LOCAL, env_cpap_repo=None, home=HOME)
        data = os.path.join(LOCAL, "TranscendSync")
        self.assertEqual(p["dump"], os.path.join(data, "dump.txt"))
        self.assertEqual(p["out"], os.path.join(data, "sleephq", "out"))
        self.assertFalse(p["dump"].startswith(HERE))

    def test_frozen_without_localappdata_falls_back_to_home(self):
        p = app.app_paths(frozen=True, here=HERE, localappdata=None, env_cpap_repo=None, home=HOME)
        self.assertEqual(p["dump"], os.path.join(HOME, "TranscendSync", "dump.txt"))

    def test_running_from_source_keeps_files_beside_the_app(self):
        p = app.app_paths(frozen=False, here=ROOT, localappdata=LOCAL, env_cpap_repo=None, home=HOME)
        self.assertEqual(p["dump"], os.path.join(ROOT, "dump.txt"))
        self.assertEqual(p["out"], os.path.join(ROOT, "sleephq", "out"))

    def test_default_cpap_repo_and_importer(self):
        p = app.app_paths(frozen=False, here=ROOT, localappdata=None, env_cpap_repo=None, home=HOME)
        repo = os.path.join(HOME, "projects", "cpap")
        self.assertEqual(p["cpap_repo"], repo)
        self.assertEqual(p["importer"], os.path.join(repo, "transcend_import.py"))

    def test_env_override_wins(self):
        for frozen in (True, False):
            p = app.app_paths(frozen=frozen, here=ROOT, localappdata=LOCAL,
                              env_cpap_repo="/x/cpap", home=HOME)
            self.assertEqual(p["importer"], os.path.join("/x/cpap", "transcend_import.py"))


class TestImportCommand(unittest.TestCase):
    def test_no_zone_keeps_the_zone_in_force(self):
        self.assertEqual(app.import_command("py", "imp.py", "d.txt", "  ", ""), ["py", "imp.py", "d.txt"])

    def test_zone_and_arrival_are_passed_through(self):
        self.assertEqual(app.import_command("py", "imp.py", "d.txt", " Asia/Tokyo ", "2026-10-16 17:00"),
                         ["py", "imp.py", "d.txt", "--tz", "Asia/Tokyo", "--since", "2026-10-16 17:00"])

    def test_from_source_the_decoder_is_this_checkout(self):
        self.assertEqual(app.import_env(False, ROOT, {})["TRANSCEND_REPO"], ROOT)
        self.assertEqual(app.import_env(False, ROOT, {"TRANSCEND_REPO": "/r"})["TRANSCEND_REPO"], "/r")

    def test_frozen_leaves_transcend_repo_to_the_environment(self):
        self.assertNotIn("TRANSCEND_REPO", app.import_env(True, HERE, {}))


class TestWslRoute(unittest.TestCase):
    REPO = "\\\\wsl.localhost\\Ubuntu\\home\\scott\\projects\\cpap"

    def test_split_wsl_unc_paths(self):
        self.assertEqual(app.wsl_split(self.REPO), ("Ubuntu", "/home/scott/projects/cpap"))
        self.assertEqual(app.wsl_split("\\\\wsl$\\Debian\\srv\\"), ("Debian", "/srv"))
        self.assertIsNone(app.wsl_split("C:\\Users\\scott\\cpap"))
        self.assertIsNone(app.wsl_split("/home/scott/cpap"))

    def test_windows_paths_as_wsl_sees_them(self):
        self.assertEqual(app.to_wsl_path("C:\\Users\\scott\\AppData\\Local\\TranscendSync\\dump.txt"),
                         "/mnt/c/Users/scott/AppData/Local/TranscendSync/dump.txt")
        self.assertEqual(app.to_wsl_path(self.REPO), "/home/scott/projects/cpap")

    def test_runs_the_importer_inside_wsl(self):
        cmd = app.wsl_import_command("Ubuntu", "/home/scott/projects/cpap", "C:\\d\\dump.txt",
                                     "Asia/Tokyo", "",
                                     "\\\\wsl.localhost\\Ubuntu\\home\\scott\\projects\\transcend-mini-CPAP")
        self.assertEqual(cmd, ["wsl.exe", "-d", "Ubuntu", "--cd", "/home/scott/projects/cpap", "--",
                               "env", "TRANSCEND_REPO=/home/scott/projects/transcend-mini-CPAP",
                               "python3", "transcend_import.py", "/mnt/c/d/dump.txt",
                               "--tz", "Asia/Tokyo"])

    def test_without_transcend_repo_wsl_uses_the_importer_default(self):
        cmd = app.wsl_import_command("Ubuntu", "/r", "C:\\d.txt")
        self.assertEqual(cmd[cmd.index("--") + 1:], ["env", "python3", "transcend_import.py", "/mnt/c/d.txt"])


class TestFindPython(unittest.TestCase):
    STUB = os.path.join(LOCAL, "Microsoft", "WindowsApps", "python.exe")
    REAL = os.path.join(LOCAL, "Programs", "Python")

    def test_from_source_uses_this_interpreter(self):
        self.assertEqual(app.find_python(False), sys.executable)

    def test_env_override_wins(self):
        self.assertEqual(app.find_python(True, "D:\\py\\python.exe", which=lambda n: None),
                         "D:\\py\\python.exe")

    def test_skips_the_store_stub_and_finds_the_newest_install(self):
        installs = [os.path.join(self.REAL, d, "python.exe")
                    for d in ("Python39", "Python312", "Python311")]
        got = app.find_python(True, which=lambda n: self.STUB if n == "python" else None,
                              localappdata=LOCAL, glob=lambda pat: installs)
        self.assertEqual(got, installs[1])

    def test_a_real_python_on_path_is_used(self):
        real = os.path.join("C:\\", "Python312", "python.exe")
        self.assertEqual(app.find_python(True, which=lambda n: real), real)

    def test_nothing_found_is_none(self):
        self.assertIsNone(app.find_python(True, which=lambda n: self.STUB,
                                          localappdata=LOCAL, glob=lambda pat: []))


class _Var:
    def __init__(self, v=""):
        self.v = v

    def get(self):
        return self.v


class _FakeApp:
    def __init__(self, tz="", since=""):
        self.lines = []
        self.tz, self.since = _Var(tz), _Var(since)

    def log(self, line):
        self.lines.append(line)


class TestImport(unittest.TestCase):
    """Runs do_import against a stand-in importer that records its argv, cwd
    and TRANSCEND_REPO, so the real cpap repo isn't touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.repo = os.path.join(t, "cpap")
        os.makedirs(self.repo)
        self.dump = os.path.join(t, "dump.txt")
        self.record = os.path.join(t, "record.txt")
        self.importer = os.path.join(self.repo, "transcend_import.py")
        self.patches = [mock.patch.object(app, "DUMP", self.dump),
                        mock.patch.object(app, "CPAP_REPO", self.repo),
                        mock.patch.object(app, "IMPORTER", self.importer),
                        mock.patch.object(app, "FROZEN", False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _write_importer(self, rc):
        with open(self.importer, "w") as f:
            f.write("import os, sys\n"
                    f"open({self.record!r}, 'w').write('\\n'.join(sys.argv[1:] + "
                    "[os.getcwd(), os.environ.get('TRANSCEND_REPO', '')]))\n"
                    "print('Stored transcend/dumps/2026-11-03.txt')\n"
                    f"sys.exit({rc})\n")

    def _write_dump(self):
        open(self.dump, "w").write("dump")

    def test_imports_the_dump_from_the_cpap_repo(self):
        self._write_dump()
        self._write_importer(0)
        fa = _FakeApp(tz="Asia/Tokyo", since="2026-10-16 17:00")
        app.App.do_import(fa)
        rec = open(self.record).read().split("\n")
        self.assertEqual(rec[:5], [self.dump, "--tz", "Asia/Tokyo", "--since", "2026-10-16 17:00"])
        self.assertEqual(os.path.realpath(rec[5]), os.path.realpath(self.repo))
        self.assertEqual(rec[6], app.HERE)
        self.assertIn("Stored transcend/dumps/2026-11-03.txt", fa.lines)
        self.assertTrue(fa.lines[-1].startswith("Import complete"))

    def test_a_failing_import_is_reported(self):
        self._write_dump()
        self._write_importer(1)
        fa = _FakeApp()
        app.App.do_import(fa)
        self.assertEqual(fa.lines[-1], "Import FAILED (exit 1).")

    def test_needs_a_dump_first(self):
        self._write_importer(0)
        fa = _FakeApp()
        app.App.do_import(fa)
        self.assertIn("Pull first", fa.lines[-1])
        self.assertFalse(os.path.exists(self.record))

    def test_missing_importer_is_reported(self):
        self._write_dump()
        fa = _FakeApp()
        app.App.do_import(fa)
        self.assertIn("set CPAP_REPO", fa.lines[-1])


if __name__ == "__main__":
    unittest.main()
