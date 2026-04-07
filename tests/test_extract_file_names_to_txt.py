import unittest

from extract_file_names_to_txt import iter_file_values


class IterFileValuesTests(unittest.TestCase):
    def test_walks_nested_structures_in_order(self) -> None:
        payload = {
            "File": "sharedassets0.assets",
            "Entries": [
                {"File": "resources.assets"},
                {
                    "Nested": {
                        "File": "sharedassets0.assets",
                        "Other": [
                            {"File": "level0"},
                            {"Ignored": 123},
                        ],
                    }
                },
            ],
        }

        self.assertEqual(
            list(iter_file_values(payload)),
            [
                "sharedassets0.assets",
                "resources.assets",
                "sharedassets0.assets",
                "level0",
            ],
        )


if __name__ == "__main__":
    unittest.main()
