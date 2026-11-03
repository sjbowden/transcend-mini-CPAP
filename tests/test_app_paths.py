"""Tests for where app.py keeps its files, and how it uploads, when frozen.

A one-file PyInstaller exe unpacks itself into a temporary %TEMP%\\_MEIxxxx
folder that Windows deletes when the app closes, and __file__ points into it.
So the frozen app must not keep dump.txt or the converted tree beside
__file__ (a Pull would vanish with the window), and it cannot run
sleephq/upload.py as a script (that file isn't bundled, and the "python on
PATH" it would need is usually the Microsoft Store placeholder). It keeps its
data in %LOCALAPPDATA%\\TranscendSync and runs the uploader in-process.

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sleephq"))

import app  # noqa: E402
import upload  # noqa: E402

HERE = os.path.join("C:\\", "Temp", "_MEI1234")          # where a frozen __file__ points
LOCAL = os.path.join("C:\\", "Users", "scott", "AppData", "Local")


class TestAppPaths(unittest.TestCase):
    def test_frozen_data_lives_in_localappdata_not_the_unpack_folder(self):
        p = app.app_paths(frozen=True, here=HERE, localappdata=LOCAL, env_uploader=None)
        data = os.path.join(LOCAL, "TranscendSync")
        self.assertEqual(p["dump"], os.path.join(data, "dump.txt"))
        self.assertEqual(p["out"], os.path.join(data, "sleephq", "out"))
        self.assertFalse(p["dump"].startswith(HERE))

    def test_frozen_without_localappdata_falls_back_to_home(self):
        p = app.app_paths(frozen=True, here=HERE, localappdata=None, env_uploader=None)
        self.assertEqual(p["dump"], os.path.join(os.path.expanduser("~"), "TranscendSync", "dump.txt"))

    def test_running_from_source_keeps_files_beside_the_app(self):
        p = app.app_paths(frozen=False, here=ROOT, localappdata=LOCAL, env_uploader=None)
        self.assertEqual(p["dump"], os.path.join(ROOT, "dump.txt"))
        self.assertEqual(p["out"], os.path.join(ROOT, "sleephq", "out"))
        self.assertEqual(p["uploader"], os.path.join(ROOT, "sleephq", "upload.py"))

    def test_frozen_uploads_in_process_unless_overridden(self):
        p = app.app_paths(frozen=True, here=HERE, localappdata=LOCAL, env_uploader=None)
        self.assertIsNone(p["uploader"])      # None = the bundled upload module
        p = app.app_paths(frozen=True, here=HERE, localappdata=LOCAL, env_uploader="D:\\up.py")
        self.assertEqual(p["uploader"], "D:\\up.py")

    def test_override_also_wins_from_source(self):
        p = app.app_paths(frozen=False, here=ROOT, localappdata=None, env_uploader="/x/up.py")
        self.assertEqual(p["uploader"], "/x/up.py")


class _FakeApp:
    """Just enough of App for do_upload: a log and the capture helper."""
    _captured = app.App._captured

    def __init__(self):
        self.lines = []

    def log(self, line):
        self.lines.append(line)


class TestInProcessUpload(unittest.TestCase):
    def setUp(self):
        self.saved = (app.UPLOADER, app.OUT, upload.main)
        app.UPLOADER, app.OUT = None, os.path.join(LOCAL, "TranscendSync", "sleephq", "out")

    def tearDown(self):
        app.UPLOADER, app.OUT, upload.main = self.saved

    def _run(self, rc):
        seen = {}

        def fake_main(argv):
            seen["argv"] = argv
            print("uploading 3 files")
            print("a warning", file=sys.stderr)
            return rc
        upload.main = fake_main
        fa = _FakeApp()
        app.App.do_upload(fa)
        return seen, fa.lines

    def test_runs_the_bundled_uploader_on_the_converted_tree(self):
        seen, lines = self._run(0)
        self.assertEqual(seen["argv"][:3], ["--data-dir", app.OUT, "--all"])
        self.assertIn("uploading 3 files", lines)
        self.assertIn("a warning", lines)          # stderr reaches the log too
        self.assertEqual(lines[-1], "Upload complete.")

    def test_a_failing_upload_is_reported(self):
        _, lines = self._run(2)
        self.assertEqual(lines[-1], "Upload FAILED (exit 2).")


if __name__ == "__main__":
    unittest.main()
