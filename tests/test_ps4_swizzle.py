import unittest

from unity_font_replacer_core import (
    _ps4_expected_swizzled_bc_size,
    ps4_swizzle_bc_blocks,
    ps4_unswizzle_bc_blocks,
)


class PS4SwizzleTests(unittest.TestCase):
    def test_roundtrip_bc1_square(self) -> None:
        width = 32
        height = 32
        block_width = 4
        block_height = 4
        block_size = 8
        linear = bytes(i % 256 for i in range(512))
        swizzled = ps4_swizzle_bc_blocks(
            linear, width, height, block_width, block_height, block_size
        )
        restored = ps4_unswizzle_bc_blocks(
            swizzled, width, height, block_width, block_height, block_size
        )
        self.assertEqual(restored, linear)

    def test_padding_changes_swizzled_size(self) -> None:
        width = 20
        height = 20
        block_width = 4
        block_height = 4
        block_size = 8
        linear = bytes(range(200))
        swizzled = ps4_swizzle_bc_blocks(
            linear, width, height, block_width, block_height, block_size
        )
        self.assertEqual(
            len(swizzled),
            _ps4_expected_swizzled_bc_size(
                width, height, block_width, block_height, block_size
            ),
        )
        restored = ps4_unswizzle_bc_blocks(
            swizzled, width, height, block_width, block_height, block_size
        )
        self.assertEqual(restored, linear)


if __name__ == "__main__":
    unittest.main()
