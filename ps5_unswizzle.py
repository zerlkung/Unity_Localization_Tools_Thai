from __future__ import annotations

import argparse
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

PS5_SWIZZLE_MASK_X = 0x385F0
PS5_SWIZZLE_MASK_Y = 0x07A0F
PS5_SWIZZLE_ROTATE = 90


@lru_cache(maxsize=128)
def _bit_positions(mask: int) -> tuple[int, ...]:
    return tuple(i for i in range(max(mask.bit_length(), 0)) if (mask >> i) & 1)


@lru_cache(maxsize=128)
def _axis_tile_size(mask: int) -> int:
    positions = _bit_positions(mask)
    return 1 << len(positions) if positions else 1


@lru_cache(maxsize=128)
def _deposit_table(mask: int) -> tuple[int, ...]:
    """Build a lookup table for pdep-like bit deposit (tile-local axis)."""
    positions = _bit_positions(mask)
    axis_size = _axis_tile_size(mask)
    table: list[int] = [0] * axis_size
    for value in range(axis_size):
        deposited = 0
        for bit_index, dst_bit in enumerate(positions):
            if (value >> bit_index) & 1:
                deposited |= (1 << dst_bit)
        table[value] = deposited
    return tuple(table)


def _validate_shape(data: bytes, width: int, height: int, bytes_per_element: int) -> int:
    if width <= 0 or height <= 0 or bytes_per_element <= 0:
        raise ValueError(
            f"Invalid texture shape: w={width}, h={height}, bpe={bytes_per_element}"
        )
    total_elements = width * height
    expected_size = total_elements * bytes_per_element
    if len(data) != expected_size:
        raise ValueError(
            f"Size mismatch: expected {expected_size}, got {len(data)} "
            f"(w={width}, h={height}, bpe={bytes_per_element})"
        )
    return total_elements


def _bytes_to_image(data: bytes, width: int, height: int, bytes_per_element: int) -> Image.Image:
    arr = np.frombuffer(data, dtype=np.uint8)
    if bytes_per_element == 1:
        return Image.fromarray(arr.reshape(height, width), mode="L")
    if bytes_per_element == 2:
        return Image.fromarray(arr.reshape(height, width, 2), mode="LA")
    if bytes_per_element == 3:
        return Image.fromarray(arr.reshape(height, width, 3), mode="RGB")
    if bytes_per_element == 4:
        return Image.fromarray(arr.reshape(height, width, 4), mode="RGBA")
    # Fallback preview: first channel only.
    ch0 = arr.reshape(height, width, bytes_per_element)[:, :, 0]
    return Image.fromarray(ch0, mode="L")


def _image_to_bytes(path: Path, bytes_per_element: int | None) -> tuple[bytes, int, int, int]:
    img = Image.open(path)
    if bytes_per_element is None:
        if img.mode in ("L", "P"):
            img = img.convert("L")
            bytes_per_element = 1
        elif img.mode == "LA":
            bytes_per_element = 2
        elif img.mode == "RGB":
            bytes_per_element = 3
        elif img.mode == "RGBA":
            bytes_per_element = 4
        else:
            img = img.convert("RGBA")
            bytes_per_element = 4
    else:
        if bytes_per_element == 1:
            img = img.convert("L")
        elif bytes_per_element == 2:
            img = img.convert("LA")
        elif bytes_per_element == 3:
            img = img.convert("RGB")
        elif bytes_per_element == 4:
            img = img.convert("RGBA")
        else:
            raise ValueError("PNG input supports bytes-per-element 1/2/3/4 only.")

    return img.tobytes(), img.width, img.height, bytes_per_element


def unswizzle(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int,
    mask_y: int,
) -> bytes:
    total_elements = _validate_shape(data, width, height, bytes_per_element)

    src = np.frombuffer(data, dtype=np.uint8).reshape(total_elements, bytes_per_element)
    dst = np.empty_like(src)

    tile_w = _axis_tile_size(mask_x)
    tile_h = _axis_tile_size(mask_y)
    xdep = np.array(_deposit_table(mask_x), dtype=np.int64)
    ydep = np.array(_deposit_table(mask_y), dtype=np.int64)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h
    x = np.arange(width, dtype=np.int64)
    local_x = x % tile_w
    macro_x = x // tile_w

    for y in range(height):
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = int(ydep[local_y])
        tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
        src_idx = tile_base + row_offset + xdep[local_x]
        row_start = y * width
        dst[row_start : row_start + width] = src[src_idx]

    return dst.reshape(-1).tobytes()


def swizzle(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int,
    mask_y: int,
) -> bytes:
    total_elements = _validate_shape(data, width, height, bytes_per_element)

    src = np.frombuffer(data, dtype=np.uint8).reshape(total_elements, bytes_per_element)
    dst = np.empty_like(src)

    tile_w = _axis_tile_size(mask_x)
    tile_h = _axis_tile_size(mask_y)
    xdep = np.array(_deposit_table(mask_x), dtype=np.int64)
    ydep = np.array(_deposit_table(mask_y), dtype=np.int64)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h
    x = np.arange(width, dtype=np.int64)
    local_x = x % tile_w
    macro_x = x // tile_w

    for y in range(height):
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = int(ydep[local_y])
        tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
        dst_idx = tile_base + row_offset + xdep[local_x]
        row_start = y * width
        dst[dst_idx] = src[row_start : row_start + width]

    return dst.reshape(-1).tobytes()


def roughness_score(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    max_axis_samples: int = 256,
) -> float:
    _validate_shape(data, width, height, bytes_per_element)
    arr = np.frombuffer(data, dtype=np.uint8).reshape(height, width, bytes_per_element)

    step_x = max(1, width // max_axis_samples)
    step_y = max(1, height // max_axis_samples)

    if bytes_per_element == 1:
        channel_index = 0
    else:
        best_score = -1.0
        channel_index = 0
        for ch in range(bytes_per_element):
            y = arr[:, :, ch].astype(np.float32)
            dx = np.abs(y[:, 1:] - y[:, :-1]).mean() if width > 1 else 0.0
            dy = np.abs(y[1:, :] - y[:-1, :]).mean() if height > 1 else 0.0
            score = float(dx + dy)
            if score > best_score:
                best_score = score
                channel_index = ch

    y = arr[:, :, channel_index].astype(np.float32)
    if width > step_x:
        dx = np.abs(y[:, step_x:] - y[:, :-step_x]).mean()
    else:
        dx = 0.0
    if height > step_y:
        dy = np.abs(y[step_y:, :] - y[:-step_y, :]).mean()
    else:
        dy = 0.0
    return float(dx + dy)


def detect_swizzle_state(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int,
    mask_y: int,
) -> tuple[str, float, float, float, bytes, bytes]:
    raw_score = roughness_score(data, width, height, bytes_per_element)
    unswizzled = unswizzle(data, width, height, bytes_per_element, mask_x, mask_y)
    swizzled = swizzle(data, width, height, bytes_per_element, mask_x, mask_y)
    unsw_score = roughness_score(unswizzled, width, height, bytes_per_element)
    swz_score = roughness_score(swizzled, width, height, bytes_per_element)

    # Lower score generally means better local coherence.
    if unsw_score < raw_score * 0.92 and unsw_score <= swz_score * 0.98:
        verdict = "likely_swizzled_input"
    elif raw_score <= unsw_score * 0.92 and raw_score <= swz_score * 0.92:
        verdict = "likely_linear_input"
    else:
        verdict = "inconclusive"

    return verdict, raw_score, unsw_score, swz_score, unswizzled, swizzled


def apply_transforms(img: Image.Image, rotate: int, hflip: bool, vflip: bool) -> Image.Image:
    out = img
    if rotate:
        out = out.rotate(rotate, expand=False)
    if hflip:
        out = out.transpose(Image.FLIP_LEFT_RIGHT)
    if vflip:
        out = out.transpose(Image.FLIP_TOP_BOTTOM)
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PS5 swizzled texture analyzer")
    p.add_argument("--mode", choices=["unswizzle", "swizzle", "detect"], default="unswizzle")
    p.add_argument("--input", required=True, help="Input texture data (bin or png)")
    p.add_argument("--input-format", choices=["auto", "bin", "png"], default="auto")
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--bytes-per-element", type=int, default=None)
    p.add_argument("--mask-x", type=lambda s: int(s, 0), default=PS5_SWIZZLE_MASK_X)
    p.add_argument("--mask-y", type=lambda s: int(s, 0), default=PS5_SWIZZLE_MASK_Y)
    p.add_argument("--output-bin", default=None)
    p.add_argument("--output-png", default=None)
    p.add_argument("--skip-bin", action="store_true")
    p.add_argument("--skip-png", action="store_true")
    p.add_argument("--rotate", type=int, default=None)
    p.add_argument("--hflip", action="store_true")
    p.add_argument("--vflip", action="store_true")
    return p.parse_args()


def _resolve_input_format(path: Path, input_format: str) -> str:
    if input_format != "auto":
        return input_format
    return "png" if path.suffix.lower() == ".png" else "bin"


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    input_format = _resolve_input_format(input_path, args.input_format)

    if args.rotate is None:
        args.rotate = PS5_SWIZZLE_ROTATE if args.mode == "unswizzle" else 0
    if args.rotate not in (0, 90, 180, 270):
        raise ValueError("--rotate must be one of 0/90/180/270")

    if args.mode == "detect":
        if args.output_png is None and not args.skip_png:
            args.output_png = "detect_compare.png"
    else:
        if args.output_bin is None and not args.skip_bin:
            args.output_bin = "unswizzled.bin" if args.mode == "unswizzle" else "swizzled.bin"
        if args.output_png is None and not args.skip_png:
            args.output_png = "unswizzled.png" if args.mode == "unswizzle" else "swizzled.png"

    if input_format == "png":
        data, in_w, in_h, in_bpe = _image_to_bytes(input_path, args.bytes_per_element)
        if args.width is not None and args.width != in_w:
            raise ValueError(f"PNG width mismatch: arg={args.width}, image={in_w}")
        if args.height is not None and args.height != in_h:
            raise ValueError(f"PNG height mismatch: arg={args.height}, image={in_h}")
        width = in_w
        height = in_h
        bytes_per_element = in_bpe
    else:
        width = 512 if args.width is None else args.width
        height = 512 if args.height is None else args.height
        bytes_per_element = 1 if args.bytes_per_element is None else args.bytes_per_element
        data = input_path.read_bytes()
        expected = width * height * bytes_per_element
        if len(data) != expected:
            raise ValueError(
                f"BIN size mismatch: expected {expected}, got {len(data)} "
                f"(w={width}, h={height}, bpe={bytes_per_element})"
            )

    if args.mode == "detect":
        verdict, raw_score, unsw_score, swz_score, unsw_data, swz_data = detect_swizzle_state(
            data,
            width,
            height,
            bytes_per_element,
            args.mask_x,
            args.mask_y,
        )

        print("Detect")
        print(f"  input       : {args.input}")
        print(f"  format      : {input_format}")
        print(f"  size        : {width}x{height}")
        print(f"  bpe         : {bytes_per_element}")
        print(f"  raw score   : {raw_score:.6f}")
        print(f"  unsw score  : {unsw_score:.6f}")
        print(f"  swz score   : {swz_score:.6f}")
        print(f"  verdict     : {verdict}")
        if verdict == "likely_swizzled_input":
            print("  suggestion  : use --mode unswizzle")
        elif verdict == "likely_linear_input":
            print("  suggestion  : already linear (or use --mode swizzle to repack)")
        else:
            print("  suggestion  : inconclusive, inspect previews or try different mask/format")

        if not args.skip_png:
            raw_img = apply_transforms(
                _bytes_to_image(data, width, height, bytes_per_element),
                args.rotate,
                args.hflip,
                args.vflip,
            ).convert("RGB")
            unsw_img = apply_transforms(
                _bytes_to_image(unsw_data, width, height, bytes_per_element),
                args.rotate,
                args.hflip,
                args.vflip,
            ).convert("RGB")
            swz_img = apply_transforms(
                _bytes_to_image(swz_data, width, height, bytes_per_element),
                args.rotate,
                args.hflip,
                args.vflip,
            ).convert("RGB")

            raw_path = Path("detect_raw.png")
            unsw_path = Path("detect_unswizzled_candidate.png")
            swz_path = Path("detect_swizzled_candidate.png")
            raw_img.save(raw_path)
            unsw_img.save(unsw_path)
            swz_img.save(swz_path)

            w = 320
            h = 320
            sheet = Image.new("RGB", (w * 3, h), (0, 0, 0))
            draw = ImageDraw.Draw(sheet)
            tiles = [
                ("raw", raw_img),
                ("unswizzled_candidate", unsw_img),
                ("swizzled_candidate", swz_img),
            ]
            for i, (label, im) in enumerate(tiles):
                x = i * w
                sheet.paste(im.resize((w, h), Image.NEAREST), (x, 0))
                draw.text((x + 6, 6), label, fill=(255, 0, 0))

            if args.output_png:
                sheet.save(args.output_png)
                print(f"  compare png : {args.output_png}")
            print(f"  raw png     : {raw_path}")
            print(f"  unsw png    : {unsw_path}")
            print(f"  swz png     : {swz_path}")
        return

    if args.mode == "unswizzle":
        out = unswizzle(
            data=data,
            width=width,
            height=height,
            bytes_per_element=bytes_per_element,
            mask_x=args.mask_x,
            mask_y=args.mask_y,
        )
    else:
        out = swizzle(
            data=data,
            width=width,
            height=height,
            bytes_per_element=bytes_per_element,
            mask_x=args.mask_x,
            mask_y=args.mask_y,
        )

    if args.output_bin:
        Path(args.output_bin).write_bytes(out)
    if args.output_png:
        img = _bytes_to_image(out, width, height, bytes_per_element)
        img = apply_transforms(img, args.rotate, args.hflip, args.vflip)
        img.save(args.output_png)

    print("Done")
    print(f"  mode       : {args.mode}")
    print(f"  input      : {args.input}")
    print(f"  format     : {input_format}")
    print(f"  size       : {width}x{height}")
    print(f"  bpe        : {bytes_per_element}")
    if args.output_bin:
        print(f"  output bin : {args.output_bin}")
    if args.output_png:
        print(f"  output png : {args.output_png}")
    print(f"  mask_x     : {args.mask_x:#x}")
    print(f"  mask_y     : {args.mask_y:#x}")


if __name__ == "__main__":
    main()
