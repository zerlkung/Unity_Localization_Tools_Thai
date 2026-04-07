import tempfile
import unittest
from pathlib import Path

from unity_font_replacer_core import find_catalog_json, get_data_path, resolve_game_path


class PS4PathResolutionTests(unittest.TestCase):
    def test_resolve_image0_root_to_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image0 = Path(tmp_dir) / "Image0"
            media = image0 / "Media"
            media.mkdir(parents=True)
            (media / "globalgamemanagers").write_bytes(b"x")

            game_path, data_path = resolve_game_path(str(image0), lang="en")

        self.assertEqual(game_path, str(image0))
        self.assertEqual(data_path, str(media))

    def test_find_catalog_from_image0_root_uses_media_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image0 = Path(tmp_dir) / "Image0"
            media = image0 / "Media"
            media.mkdir(parents=True)
            (media / "globalgamemanagers").write_bytes(b"x")
            media_catalog = media / "StreamingAssets" / "aa" / "catalog.json"
            media_catalog.parent.mkdir(parents=True)
            media_catalog.write_text("{}", encoding="utf-8")

            catalog_path = find_catalog_json(str(image0), lang="en")

        self.assertEqual(catalog_path, str(media_catalog))

    def test_resolve_media_path_to_parent_image0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image0 = Path(tmp_dir) / "Image0"
            media = image0 / "Media"
            media.mkdir(parents=True)
            (media / "globalgamemanagers.assets").write_bytes(b"x")

            game_path, data_path = resolve_game_path(str(media), lang="en")
            direct_data_path = get_data_path(str(media), lang="en")

        self.assertEqual(game_path, str(image0))
        self.assertEqual(data_path, str(media))
        self.assertEqual(direct_data_path, str(media))

    def test_find_catalog_prefers_media_streamingassets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image0 = Path(tmp_dir) / "Image0"
            media = image0 / "Media"
            media_catalog = media / "StreamingAssets" / "aa" / "catalog.json"
            media_catalog.parent.mkdir(parents=True)
            (media / "globalgamemanagers").write_bytes(b"x")
            media_catalog.write_text("{}", encoding="utf-8")

            catalog_path = find_catalog_json(str(media), lang="en")

        self.assertEqual(catalog_path, str(media_catalog))


if __name__ == "__main__":
    unittest.main()
