import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unity_font_replacer_core import find_assets_files


class FindAssetsFilesTests(unittest.TestCase):
    def test_only_collects_assets_and_bundle_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_path = Path(tmp_dir)
            (data_path / "a.assets").write_bytes(b"")
            (data_path / "b.bundle").write_bytes(b"")
            (data_path / "c.assets.resS").write_bytes(b"")
            (data_path / "d").write_bytes(b"")
            (data_path / "e.txt").write_text("", encoding="utf-8")

            with patch("unity_font_replacer_core.get_data_path", return_value=str(data_path)):
                found = find_assets_files("dummy-game")

        self.assertEqual(
            {Path(path).name for path in found},
            set(),
        )

    def test_skips_zero_byte_files_but_keeps_nonempty_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_path = Path(tmp_dir)
            (data_path / "a.assets").write_bytes(b"x")
            (data_path / "b.bundle").write_bytes(b"y")
            (data_path / "c.bundle").write_bytes(b"")

            with patch("unity_font_replacer_core.get_data_path", return_value=str(data_path)):
                found = find_assets_files("dummy-game")

        self.assertEqual(
            {Path(path).name for path in found},
            {"a.assets", "b.bundle"},
        )

    def test_bundle_targets_limit_only_bundle_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_path = Path(tmp_dir)
            (data_path / "a.assets").write_bytes(b"x")
            (data_path / "keep.bundle").write_bytes(b"y")
            (data_path / "skip.bundle").write_bytes(b"z")

            with patch("unity_font_replacer_core.get_data_path", return_value=str(data_path)):
                found = find_assets_files("dummy-game", bundle_targets={"keep.bundle"})

        self.assertEqual(
            {Path(path).name for path in found},
            {"a.assets", "keep.bundle"},
        )


if __name__ == "__main__":
    unittest.main()
