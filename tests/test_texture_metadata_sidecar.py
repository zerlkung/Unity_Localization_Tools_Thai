import base64
import json
import tempfile
import unittest
from pathlib import Path

from unity_font_replacer_core import load_texture_metadata_sidecar


class TextureMetadataSidecarTests(unittest.TestCase):
    def test_decodes_platform_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "Atlas.texture-meta.json"
            path.write_text(
                json.dumps(
                    {
                        "width": 512,
                        "height": 256,
                        "texture_format": 4,
                        "platform": 13,
                        "is_readable": False,
                        "stream_size": 2048,
                        "image_data_size": 4096,
                        "platform_blob_base64": base64.b64encode(b"\x01\x02\x03").decode(
                            "ascii"
                        ),
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_texture_metadata_sidecar(str(path))

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["width"], 512)
        self.assertEqual(loaded["platform"], 13)
        self.assertFalse(loaded["is_readable"])
        self.assertEqual(loaded["platform_blob"], b"\x01\x02\x03")


if __name__ == "__main__":
    unittest.main()
