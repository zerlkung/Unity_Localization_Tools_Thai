import unittest
from types import SimpleNamespace

from unity_font_replacer_core import get_font_bundle_targets_from_catalog


class CatalogFontBundleTargetTests(unittest.TestCase):
    def test_collects_unique_bundle_names_from_font_resources(self) -> None:
        font_loc_1 = SimpleNamespace(
            provider_id="TextMeshPro",
            internal_id="Assets/Fonts/Example SDF.asset",
            primary_key="Assets/Fonts/Example SDF.asset",
            resource_type=SimpleNamespace(class_name="TMP_FontAsset"),
            data=None,
            dependencies=[
                SimpleNamespace(
                    internal_id="aa/Windows/font_a.bundle",
                    data=None,
                )
            ],
        )
        font_loc_2 = SimpleNamespace(
            provider_id="Font",
            internal_id="Assets/Fonts/Example.ttf",
            primary_key="Assets/Fonts/Example.ttf",
            resource_type=SimpleNamespace(class_name="Font"),
            data=None,
            dependencies=[
                SimpleNamespace(
                    internal_id="aa/Windows/font_b.bundle",
                    data=None,
                )
            ],
        )
        non_font_loc = SimpleNamespace(
            provider_id="PrefabProvider",
            internal_id="Assets/Prefabs/Test.prefab",
            primary_key="Assets/Prefabs/Test.prefab",
            resource_type=SimpleNamespace(class_name="GameObject"),
            data=None,
            dependencies=[
                SimpleNamespace(
                    internal_id="aa/Windows/prefabs.bundle",
                    data=None,
                )
            ],
        )
        catalog = SimpleNamespace(
            resources={
                "k1": [font_loc_1],
                "k2": [font_loc_2],
                "k3": [non_font_loc],
            }
        )

        bundle_targets, summary = get_font_bundle_targets_from_catalog(catalog)

        self.assertEqual(bundle_targets, {"font_a.bundle", "font_b.bundle"})
        self.assertEqual(summary["font_resource_count"], 2)
        self.assertEqual(summary["resource_name_count"], 2)
        self.assertEqual(summary["resource_types"], ["Font", "TMP_FontAsset"])


if __name__ == "__main__":
    unittest.main()
