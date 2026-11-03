"""Tests for where app.py keeps its files, and how Analyze runs the analyzer.

A one-file PyInstaller exe unpacks itself into a temporary %TEMP%\\_MEIxxxx
folder that Windows deletes when the app closes, and __file__ points into it.
So the frozen app must not keep dump.txt or the converted tree beside
__file__ (a Pull would vanish with the window); it keeps its data in
%LOCALAPPDATA%\\TranscendSync.

Analyze runs the separate ResMed analyzer (~/projects/cpap/cpap_analyzer.py)
on the converted tree, and must send its output to the app's own "analysis"
folder: the analyzer's default output folder is its own directory, where the
ResMed charts and CSV live.

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
        p = app.app_paths(frozen=True, here=HERE, localappdata=LOCAL, env_analyzer=None, home=HOME)
        data = os.path.join(LOCAL, "TranscendSync")
        self.assertEqual(p["dump"], os.path.join(data, "dump.txt"))
        self.assertEqual(p["out"], os.path.join(data, "sleephq", "out"))
        self.assertEqual(p["analysis"], os.path.join(data, "analysis"))
        self.assertFalse(p["dump"].startswith(HERE))

    def test_frozen_without_localappdata_falls_back_to_home(self):
        p = app.app_paths(frozen=True, here=HERE, localappdata=None, env_analyzer=None, home=HOME)
        self.assertEqual(p["dump"], os.path.join(HOME, "TranscendSync", "dump.txt"))

    def test_running_from_source_keeps_files_beside_the_app(self):
        p = app.app_paths(frozen=False, here=ROOT, localappdata=LOCAL, env_analyzer=None, home=HOME)
        self.assertEqual(p["dump"], os.path.join(ROOT, "dump.txt"))
        self.assertEqual(p["out"], os.path.join(ROOT, "sleephq", "out"))
        self.assertEqual(p["analysis"], os.path.join(ROOT, "analysis"))

    def test_default_analyzer_is_the_cpap_repo(self):
        p = app.app_paths(frozen=False, here=ROOT, localappdata=None, env_analyzer=None, home=HOME)
        self.assertEqual(p["analyzer"], os.path.join(HOME, "projects", "cpap", "cpap_analyzer.py"))

    def test_env_override_wins(self):
        for frozen in (True, False):
            p = app.app_paths(frozen=frozen, here=ROOT, localappdata=LOCAL,
                              env_analyzer="/x/an.py", home=HOME)
            self.assertEqual(p["analyzer"], "/x/an.py")


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


class _FakeApp:
    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)


class TestAnalyze(unittest.TestCase):
    """Runs do_analyze against a stand-in analyzer script that records its
    argv, so the real pandas/matplotlib analyzer isn't needed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.out = os.path.join(t, "out")
        self.analysis = os.path.join(t, "analysis")
        self.argv_file = os.path.join(t, "argv.txt")
        self.script = os.path.join(t, "fake_analyzer.py")
        self.patches = [mock.patch.object(app, "OUT", self.out),
                        mock.patch.object(app, "ANALYSIS", self.analysis),
                        mock.patch.object(app, "ANALYZER", self.script)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _write_analyzer(self, rc):
        with open(self.script, "w") as f:
            f.write("import sys\n"
                    f"open({self.argv_file!r}, 'w').write('\\n'.join(sys.argv[1:]))\n"
                    "print('Total days analyzed: 2')\n"
                    f"sys.exit({rc})\n")

    def test_runs_on_the_converted_tree_into_the_analysis_folder(self):
        os.makedirs(self.out)
        self._write_analyzer(0)
        fa = _FakeApp()
        app.App.do_analyze(fa)
        argv = open(self.argv_file).read().split("\n")
        self.assertEqual(argv, ["--data-dir", self.out, "--output-dir", self.analysis])
        self.assertTrue(os.path.isdir(self.analysis))
        self.assertIn("Total days analyzed: 2", fa.lines)
        self.assertTrue(fa.lines[-1].startswith("Analysis complete"))

    def test_a_failing_analyzer_is_reported(self):
        os.makedirs(self.out)
        self._write_analyzer(3)
        fa = _FakeApp()
        app.App.do_analyze(fa)
        self.assertTrue(fa.lines[-1].startswith("Analyze FAILED (exit 3)"))

    def test_needs_converted_data_first(self):
        self._write_analyzer(0)
        fa = _FakeApp()
        app.App.do_analyze(fa)
        self.assertIn("Convert first", fa.lines[-1])
        self.assertFalse(os.path.exists(self.argv_file))

    def test_missing_analyzer_is_reported(self):
        os.makedirs(self.out)
        fa = _FakeApp()
        app.App.do_analyze(fa)
        self.assertIn("set CPAP_ANALYZER", fa.lines[-1])


if __name__ == "__main__":
    unittest.main()
