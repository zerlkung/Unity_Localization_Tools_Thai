"""Generate TextMeshPro-compatible SDF/Raster atlas assets from TTF fonts."""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Literal

from PIL import Image, ImageFont

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dependency fallback
    np = None  # type: ignore[assignment]

try:
    from scipy import ndimage as scipy_ndimage
except Exception:  # pragma: no cover - optional dependency fallback
    scipy_ndimage = None  # type: ignore[assignment]

try:
    from fontTools.ttLib import TTFont
except Exception:  # pragma: no cover - optional dependency fallback
    TTFont = None  # type: ignore[assignment]


RenderMode = Literal["sdf", "raster"]
JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure module logging for CLI mode."""
    if logging.getLogger().handlers:
        logging.getLogger().setLevel(level)
        return
    logging.basicConfig(level=level, format="%(message)s")


def normalize_font_name(name: str) -> str:
    for ext in [".ttf", ".json", ".png"]:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    if name.endswith(" SDF Atlas"):
        name = name[: -len(" SDF Atlas")]
    elif name.endswith(" SDF"):
        name = name[: -len(" SDF")]
    elif name.endswith(" Raster Atlas"):
        name = name[: -len(" Raster Atlas")]
    elif name.endswith(" Raster"):
        name = name[: -len(" Raster")]
    return name


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return default


def _parse_atlas_size(value: str) -> tuple[int, int]:
    text = value.strip().replace(" ", "")
    parts = text.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("atlas size must be in 'W,H' format.")
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("atlas size must contain integers.") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("atlas size must be positive.")
    return width, height


def _parse_point_size(value: str) -> int:
    text = value.strip().lower()
    if text == "auto":
        return 0
    try:
        point_size = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "point size must be an integer or 'auto'."
        ) from exc
    if point_size <= 0:
        raise argparse.ArgumentTypeError("point size must be positive or 'auto'.")
    return point_size


def _resolve_ttf_path(ttf_arg: str) -> Path:
    raw = Path(ttf_arg)
    if raw.is_file():
        return raw.resolve()

    candidate_names = [ttf_arg]
    if not ttf_arg.lower().endswith(".ttf"):
        candidate_names.append(f"{ttf_arg}.ttf")

    base_dirs = [
        Path.cwd(),
        Path(__file__).resolve().parent,
        Path.cwd() / "KR_ASSETS",
        Path(__file__).resolve().parent / "KR_ASSETS",
    ]
    for base_dir in base_dirs:
        for candidate_name in candidate_names:
            candidate = base_dir / candidate_name
            if candidate.is_file():
                return candidate.resolve()

    raise FileNotFoundError(f"TTF file not found: {ttf_arg}")


def _load_charset_text(charset_arg: str) -> str:
    path = Path(charset_arg)
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")

    looks_like_path = any(
        sep in charset_arg for sep in ["/", "\\"]
    ) or charset_arg.lower().endswith(".txt")
    if looks_like_path:
        raise FileNotFoundError(f"Charset file not found: {charset_arg}")

    return charset_arg


def _text_to_unicodes(text: str) -> list[int]:
    seen: set[int] = set()
    unicodes: list[int] = []
    for char in text:
        code = ord(char)
        if code == 0:
            continue
        if 0xD800 <= code <= 0xDFFF:
            continue
        if code in seen:
            continue
        seen.add(code)
        unicodes.append(code)
    return unicodes


def _get_ttf_name_info(ttf_data: bytes, fallback_name: str) -> tuple[str, str, int]:
    family_name = normalize_font_name(fallback_name)
    style_name = "Regular"
    units_per_em = 1000

    if TTFont is None:
        return family_name, style_name, units_per_em

    try:
        with TTFont(io.BytesIO(ttf_data), lazy=True) as font:
            head = font.get("head")
            if head is not None:
                units_per_em = max(1, int(getattr(head, "unitsPerEm", units_per_em)))
            name_table = font.get("name")
            if name_table is not None:
                family_name = str(name_table.getBestFamilyName() or family_name)
                style_name = str(name_table.getBestSubFamilyName() or style_name)
    except Exception:
        pass

    return family_name, style_name, units_per_em


def _measure_glyph_metrics(
    font: ImageFont.FreeTypeFont,
    unicode_value: int,
    ascent: int,
) -> tuple[int, int, JsonDict]:
    character = chr(unicode_value)
    bbox = font.getbbox(character)
    if bbox is None:
        bbox = (0, 0, 0, 0)

    x0, y0, x1, y1 = bbox
    width = max(0, int(x1 - x0))
    height = max(0, int(y1 - y0))

    try:
        advance = float(font.getlength(character))
    except Exception:
        advance = float(width)

    metrics = {
        "m_Width": float(width),
        "m_Height": float(height),
        "m_HorizontalBearingX": float(x0),
        # TMP expects vertical bearing from baseline to glyph top in pixels.
        "m_HorizontalBearingY": float(ascent - y0),
        "m_HorizontalAdvance": float(advance),
    }
    return width, height, metrics


def _render_glyph_bitmap(font: ImageFont.FreeTypeFont, unicode_value: int) -> Any:
    if np is None:
        raise RuntimeError("numpy is required for SDF generation.")

    try:
        mask = font.getmask(chr(unicode_value), mode="L")
    except OSError:
        # Some glyphs have metrics but no bitmap (e.g. certain CJK fonts with
        # missing outlines for specific code points).  Return an empty bitmap
        # so the caller can still place an empty tile in the atlas.
        return np.zeros((0, 0), dtype=np.uint8)

    mask_w, mask_h = mask.size
    if mask_w <= 0 or mask_h <= 0:
        return np.zeros((0, 0), dtype=np.uint8)

    mask_bytes = bytes(mask)
    bitmap = np.frombuffer(mask_bytes, dtype=np.uint8)
    if bitmap.size != mask_w * mask_h:
        bitmap = np.array(mask, dtype=np.uint8)
    return bitmap.reshape((mask_h, mask_w))


def _pack_rectangles_shelf(
    rectangles: list[tuple[int, int, int]],
    atlas_width: int,
    atlas_height: int,
) -> tuple[dict[int, tuple[int, int, int, int]], list[JsonDict]] | None:
    placements: dict[int, tuple[int, int, int, int]] = {}
    used_rects: list[JsonDict] = []

    x = 0
    y = 0
    row_height = 0
    ordered = sorted(
        rectangles, key=lambda item: (item[1] * item[2], item[2], item[1]), reverse=True
    )

    for glyph_index, rect_w, rect_h in ordered:
        if rect_w > atlas_width or rect_h > atlas_height:
            return None

        if x + rect_w > atlas_width:
            x = 0
            y += row_height
            row_height = 0

        if y + rect_h > atlas_height:
            return None

        placements[glyph_index] = (x, y, rect_w, rect_h)
        used_rects.append({"m_X": x, "m_Y": y, "m_Width": rect_w, "m_Height": rect_h})
        x += rect_w
        row_height = max(row_height, rect_h)

    return placements, used_rects


def _validate_layout_rectangles(
    placements: dict[int, tuple[int, int, int, int]],
    used_rects: list[JsonDict],
    glyph_indices: set[int],
    atlas_width: int,
    atlas_height: int,
) -> tuple[bool, str]:
    if len(placements) != len(glyph_indices):
        return False, "placement count mismatch"
    if set(placements.keys()) != glyph_indices:
        return False, "placement glyph index mismatch"
    if len(used_rects) != len(placements):
        return False, "used rect count mismatch"

    occupied: set[tuple[int, int, int, int]] = set()
    for glyph_index, rect in placements.items():
        px, py, pw, ph = rect
        if px < 0 or py < 0 or pw <= 0 or ph <= 0:
            return (
                False,
                f"invalid rectangle for glyph {glyph_index}: ({px}, {py}, {pw}, {ph})",
            )
        if px + pw > atlas_width or py + ph > atlas_height:
            return (
                False,
                f"out-of-bounds rectangle for glyph {glyph_index}: ({px}, {py}, {pw}, {ph})",
            )
        key = (px, py, pw, ph)
        if key in occupied:
            return False, f"duplicate rectangle allocation: ({px}, {py}, {pw}, {ph})"
        occupied.add(key)
    return True, ""


def _compute_sdf_tile(alpha_tile: Any, spread: int) -> Any:
    if np is None or scipy_ndimage is None:
        raise RuntimeError("numpy and scipy are required for SDF generation.")

    inside = alpha_tile > 127
    if not inside.any():
        return np.zeros_like(alpha_tile, dtype=np.uint8)
    if inside.all():
        return np.full_like(alpha_tile, 255, dtype=np.uint8)

    outside = ~inside
    dist_outside = scipy_ndimage.distance_transform_edt(outside)
    dist_inside = scipy_ndimage.distance_transform_edt(inside)
    # TMP / SDF shader convention:
    # edge ~= 0.5, inside > 0.5, outside < 0.5.
    signed_distance = dist_inside - dist_outside

    spread_value = float(max(1, spread))
    normalized = np.clip(0.5 + (signed_distance / (2.0 * spread_value)), 0.0, 1.0)
    return (normalized * 255.0).astype(np.uint8)


def _normalize_sdf_payload(data: JsonDict) -> JsonDict:
    # make_sdf 독립 동작을 위해 최소 정규화만 수행
    data = json.loads(json.dumps(data))
    data.setdefault("m_AtlasTextures", [{"m_FileID": 0, "m_PathID": 0}])
    data.setdefault("m_UsedGlyphRects", [])
    data.setdefault("m_FreeGlyphRects", [])
    data.setdefault("m_FontWeightTable", [])
    if isinstance(data.get("m_AtlasTextures"), list):
        normalized = []
        for tex in data["m_AtlasTextures"]:
            if isinstance(tex, dict):
                normalized.append(
                    {
                        "m_FileID": int(tex.get("m_FileID", 0) or 0),
                        "m_PathID": int(tex.get("m_PathID", 0) or 0),
                    }
                )
        data["m_AtlasTextures"] = normalized
    return data


def generate_sdf_assets_from_ttf(
    ttf_data: bytes,
    font_name: str,
    unicodes: list[int],
    point_size: int,
    atlas_padding: int,
    atlas_width: int,
    atlas_height: int,
    render_mode: RenderMode = "sdf",
    log_fn: Callable[[str], None] | None = None,
) -> JsonDict | None:
    def _emit(message: str) -> None:
        if callable(log_fn):
            try:
                log_fn(message)
            except Exception:
                pass

    if np is None or scipy_ndimage is None:
        logger.error("Error: numpy/scipy are required.")
        return None
    if not unicodes:
        return None

    normalized_render_mode = str(render_mode).strip().lower()
    if normalized_render_mode not in {"sdf", "raster"}:
        normalized_render_mode = "sdf"

    requested_point_size = _safe_int(point_size, 0)
    if requested_point_size > 0:
        requested_point_size = max(8, min(requested_point_size, 512))

    atlas_padding = max(1, min(int(atlas_padding), 64))
    atlas_width = max(64, min(int(atlas_width), 8192))
    atlas_height = max(64, min(int(atlas_height), 8192))

    family_name, style_name, units_per_em = _get_ttf_name_info(ttf_data, font_name)
    layout_cache: dict[int, JsonDict | None] = {}

    def _build_layout(candidate_point_size: int) -> JsonDict | None:
        if candidate_point_size in layout_cache:
            return layout_cache[candidate_point_size]

        try:
            font = ImageFont.truetype(io.BytesIO(ttf_data), size=candidate_point_size)
        except Exception:
            layout_cache[candidate_point_size] = None
            return None

        ascent, descent = font.getmetrics()
        glyph_entries: list[JsonDict] = []
        rectangles: list[tuple[int, int, int]] = []

        for code in unicodes:
            glyph_w, glyph_h, metrics = _measure_glyph_metrics(
                font, int(code), int(ascent)
            )
            if glyph_w > 0 and glyph_h > 0:
                rect_w = glyph_w + atlas_padding * 2
                rect_h = glyph_h + atlas_padding * 2
            else:
                rect_w = 1
                rect_h = 1

            rectangles.append((int(code), rect_w, rect_h))
            glyph_entries.append(
                {
                    "unicode": int(code),
                    "glyph_index": int(code),
                    "width": glyph_w,
                    "height": glyph_h,
                    "metrics": metrics,
                }
            )

        packed_result = _pack_rectangles_shelf(rectangles, atlas_width, atlas_height)

        if packed_result is None:
            layout_cache[candidate_point_size] = None
            return None

        placements, used_rects = packed_result
        valid_layout, reason = _validate_layout_rectangles(
            placements=placements,
            used_rects=used_rects,
            glyph_indices={int(code) for code in unicodes},
            atlas_width=atlas_width,
            atlas_height=atlas_height,
        )
        if not valid_layout:
            _emit(
                f"[layout] point-size {candidate_point_size}: invalid layout ({reason})"
            )
            layout_cache[candidate_point_size] = None
            return None

        layout: JsonDict = {
            "font": font,
            "point_size": int(candidate_point_size),
            "ascent": int(ascent),
            "descent": int(descent),
            "glyph_entries": glyph_entries,
            "placements": placements,
            "used_rects": used_rects,
            "atlas_w": int(atlas_width),
            "atlas_h": int(atlas_height),
        }
        layout_cache[candidate_point_size] = layout
        return layout

    selected_layout: JsonDict | None = None
    if requested_point_size <= 0:
        low = 8
        high = 512
        best_size = 0
        best_layout: JsonDict | None = None
        _emit(f"[auto] point-size search range: {low}-{high}")
        while low <= high:
            mid = (low + high) // 2
            layout = _build_layout(mid)
            if layout is not None:
                _emit(f"[auto] point-size {mid}: fit")
                best_size = mid
                best_layout = layout
                low = mid + 1
            else:
                _emit(f"[auto] point-size {mid}: overflow")
                high = mid - 1
        selected_layout = best_layout
        if best_size > 0:
            _emit(f"[auto] selected point-size: {best_size}")
    else:
        candidates = [requested_point_size]
        for step in [4, 8, 12, 16, 24, 32, 48, 64, 96, 128]:
            candidate = requested_point_size - step
            if candidate >= 8 and candidate not in candidates:
                candidates.append(candidate)
        if 8 not in candidates:
            candidates.append(8)
        for candidate in candidates:
            layout = _build_layout(candidate)
            _emit(
                f"[fixed] point-size {candidate}: {'fit' if layout is not None else 'overflow'}"
            )
            if layout is not None:
                selected_layout = layout
                break

    if selected_layout is None:
        return None

    selected_font = selected_layout["font"]
    selected_entries = selected_layout["glyph_entries"]
    selected_placements = selected_layout["placements"]
    selected_used_rects = selected_layout["used_rects"]
    selected_atlas_w = int(selected_layout["atlas_w"])
    selected_atlas_h = int(selected_layout["atlas_h"])
    selected_ascent = int(selected_layout["ascent"])
    selected_descent = int(selected_layout["descent"])
    selected_point_size = int(selected_layout["point_size"])

    expected_glyph_indices = {
        int(entry.get("glyph_index", -1)) for entry in selected_entries
    }
    if set(int(key) for key in selected_placements.keys()) != expected_glyph_indices:
        _emit("[layout] selected placements do not match glyph entries.")
        return None
    if len(selected_used_rects) != len(selected_placements):
        _emit("[layout] selected used rect count mismatch.")
        return None

    atlas_alpha = np.zeros((selected_atlas_h, selected_atlas_w), dtype=np.uint8)
    glyph_table: list[JsonDict] = []
    char_table: list[JsonDict] = []
    total_entries = len(selected_entries)
    progress_step = max(200, total_entries // 10 if total_entries > 0 else 1)

    for idx, entry in enumerate(selected_entries, start=1):
        glyph_index = int(entry["glyph_index"])
        unicode_value = int(entry["unicode"])
        placement = selected_placements.get(glyph_index)
        if placement is None:
            continue
        px, py, pw, ph = placement
        if (
            px < 0
            or py < 0
            or pw <= 0
            or ph <= 0
            or (px + pw) > selected_atlas_w
            or (py + ph) > selected_atlas_h
        ):
            _emit(
                f"[layout] invalid placement for glyph={glyph_index}: ({px}, {py}, {pw}, {ph})"
            )
            return None
        glyph_w = int(entry["width"])
        glyph_h = int(entry["height"])

        if glyph_w > 0 and glyph_h > 0:
            bitmap = _render_glyph_bitmap(selected_font, unicode_value)
            bitmap_h = int(bitmap.shape[0]) if bitmap.ndim == 2 else 0
            bitmap_w = int(bitmap.shape[1]) if bitmap.ndim == 2 else 0
            copy_w = min(glyph_w, bitmap_w)
            copy_h = min(glyph_h, bitmap_h)

            tile = np.zeros((ph, pw), dtype=np.uint8)
            offset_x = min(atlas_padding, max(0, pw - glyph_w))
            offset_y = min(atlas_padding, max(0, ph - glyph_h))
            if copy_w > 0 and copy_h > 0:
                tile[offset_y : offset_y + copy_h, offset_x : offset_x + copy_w] = (
                    bitmap[:copy_h, :copy_w]
                )

            if normalized_render_mode == "sdf":
                mode_tile = _compute_sdf_tile(tile, atlas_padding)
            else:
                mode_tile = tile
            atlas_alpha[py : py + ph, px : px + pw] = np.maximum(
                atlas_alpha[py : py + ph, px : px + pw], mode_tile
            )

            glyph_x = px + offset_x
            glyph_y_top = py + offset_y
            glyph_rect_w = glyph_w
            glyph_rect_h = glyph_h
        else:
            glyph_x = px
            glyph_y_top = py
            glyph_rect_w = 1
            glyph_rect_h = 1

        if glyph_x < 0 or glyph_y_top < 0:
            _emit(
                f"[layout] negative glyph origin for glyph={glyph_index}: ({glyph_x}, {glyph_y_top})"
            )
            return None
        if (
            glyph_x + glyph_rect_w > selected_atlas_w
            or glyph_y_top + glyph_rect_h > selected_atlas_h
        ):
            _emit(
                "[layout] glyph rect exceeds atlas for glyph="
                f"{glyph_index}: ({glyph_x}, {glyph_y_top}, {glyph_rect_w}, {glyph_rect_h})"
            )
            return None

        glyph_y = selected_atlas_h - glyph_y_top - glyph_rect_h
        glyph_table.append(
            {
                "m_Index": glyph_index,
                "m_Metrics": entry["metrics"],
                "m_GlyphRect": {
                    "m_X": int(glyph_x),
                    "m_Y": int(glyph_y),
                    "m_Width": int(glyph_rect_w),
                    "m_Height": int(glyph_rect_h),
                },
                "m_Scale": 1.0,
                "m_AtlasIndex": 0,
                "m_ClassDefinitionType": 0,
            }
        )
        char_table.append(
            {
                "m_ElementType": 1,
                "m_Unicode": unicode_value,
                "m_GlyphIndex": glyph_index,
                "m_Scale": 1.0,
            }
        )

        if idx == 1 or idx == total_entries or idx % progress_step == 0:
            _emit(f"[render] glyph {idx}/{total_entries}")

    unique_glyph_table: list[JsonDict] = []
    glyph_index_seen: set[int] = set()
    duplicate_glyph_indices: list[int] = []
    for glyph in glyph_table:
        glyph_index = _safe_int(glyph.get("m_Index", -1), -1)
        if glyph_index < 0:
            continue
        if glyph_index in glyph_index_seen:
            duplicate_glyph_indices.append(glyph_index)
            continue
        glyph_index_seen.add(glyph_index)
        unique_glyph_table.append(glyph)
    if duplicate_glyph_indices:
        _emit(
            "[render] duplicate glyph indices removed: "
            + ", ".join(str(x) for x in sorted(set(duplicate_glyph_indices)))
        )
    glyph_table = unique_glyph_table

    unique_char_table: list[JsonDict] = []
    char_pair_seen: set[tuple[int, int]] = set()
    duplicate_char_pairs = 0
    for ch in char_table:
        unicode_value = _safe_int(ch.get("m_Unicode", -1), -1)
        glyph_index = _safe_int(ch.get("m_GlyphIndex", -1), -1)
        if unicode_value < 0 or glyph_index < 0:
            continue
        if glyph_index not in glyph_index_seen:
            continue
        key = (unicode_value, glyph_index)
        if key in char_pair_seen:
            duplicate_char_pairs += 1
            continue
        char_pair_seen.add(key)
        unique_char_table.append(ch)
    if duplicate_char_pairs:
        _emit(f"[render] duplicate character entries removed: {duplicate_char_pairs}")
    char_table = unique_char_table

    if len(glyph_table) != len(char_table):
        _emit(
            f"[render] glyph/char count mismatch after dedupe: glyphs={len(glyph_table)}, chars={len(char_table)}"
        )

    glyph_table.sort(key=lambda item: int(item.get("m_Index", 0)))
    char_table.sort(key=lambda item: int(item.get("m_Unicode", 0)))

    cap_bbox = selected_font.getbbox("H")
    mean_bbox = selected_font.getbbox("x")
    cap_line = (
        float(selected_ascent - cap_bbox[1])
        if cap_bbox is not None
        else float(selected_ascent)
    )
    mean_line = (
        float(selected_ascent - mean_bbox[1])
        if mean_bbox is not None
        else float(selected_ascent * 0.5)
    )
    line_height = float(selected_ascent + selected_descent)
    descent_line = float(-selected_descent)
    underline_thickness = max(1.0, float(selected_point_size) * 0.06)
    strikethrough_offset = (
        cap_line / 2.5 if cap_line != 0 else float(selected_ascent) * 0.4
    )

    face_info: JsonDict = {
        "m_FaceIndex": 0,
        "m_FamilyName": family_name,
        "m_StyleName": style_name,
        "m_PointSize": int(selected_point_size),
        "m_Scale": 1.0,
        "m_UnitsPerEM": int(units_per_em),
        "m_LineHeight": line_height,
        "m_AscentLine": float(selected_ascent),
        "m_CapLine": cap_line,
        "m_MeanLine": mean_line,
        "m_Baseline": 0.0,
        "m_DescentLine": descent_line,
        "m_SuperscriptOffset": float(selected_ascent) * 0.5,
        "m_SuperscriptSize": 0.5,
        "m_SubscriptOffset": descent_line * 0.5,
        "m_SubscriptSize": 0.5,
        "m_UnderlineOffset": descent_line * 0.5,
        "m_UnderlineThickness": underline_thickness,
        "m_StrikethroughOffset": strikethrough_offset,
        "m_StrikethroughThickness": underline_thickness,
        "m_TabWidth": float(selected_point_size) * 0.5,
    }

    atlas_rgba = np.zeros((selected_atlas_h, selected_atlas_w, 4), dtype=np.uint8)
    atlas_rgba[:, :, 3] = atlas_alpha
    atlas_image = Image.fromarray(atlas_rgba, mode="RGBA")

    atlas_render_mode_value = 4118 if normalized_render_mode == "sdf" else 4
    generated_sdf_data: JsonDict = {
        "m_FaceInfo": face_info,
        "m_GlyphTable": glyph_table,
        "m_CharacterTable": char_table,
        "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 0}],
        "m_AtlasWidth": int(selected_atlas_w),
        "m_AtlasHeight": int(selected_atlas_h),
        "m_AtlasPadding": int(atlas_padding),
        "m_AtlasRenderMode": atlas_render_mode_value,
        "m_UsedGlyphRects": selected_used_rects,
        "m_FreeGlyphRects": [],
        "m_FontWeightTable": [],
    }

    gradient_scale = (
        float(atlas_padding + 1) if normalized_render_mode == "sdf" else 1.0
    )
    generated_materials: JsonDict = {
        "m_SavedProperties": {
            "m_Floats": [
                ["_GradientScale", gradient_scale],
                ["_TextureWidth", float(selected_atlas_w)],
                ["_TextureHeight", float(selected_atlas_h)],
            ]
        }
    }
    _emit(
        f"[done] point-size={selected_point_size}, atlas={selected_atlas_w}x{selected_atlas_h}, "
        f"glyphs={len(glyph_table)}, rendermode={normalized_render_mode}"
    )
    return {
        "ttf_data": ttf_data,
        "sdf_data": generated_sdf_data,
        "sdf_data_normalized": _normalize_sdf_payload(generated_sdf_data),
        "sdf_atlas": atlas_image,
        "sdf_materials": generated_materials,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate TMP-compatible atlas/json from TTF.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--ttf", required=True, help="TTF file path or name.")
    parser.add_argument(
        "--atlas-size",
        default="4096,4096",
        help="Atlas size in 'W,H' format. Default: 4096,4096",
    )
    parser.add_argument(
        "--point-size",
        default="auto",
        help="Sampling point size or 'auto'. Default: auto",
    )
    parser.add_argument(
        "--padding", type=int, default=7, help="Atlas padding. Default: 7"
    )
    parser.add_argument(
        "--charset",
        default="./CharList_3911.txt",
        help="Charset file path or literal characters.",
    )
    parser.add_argument(
        "--rendermode",
        choices=["sdf", "raster"],
        default="sdf",
        help="Render mode. Default: sdf",
    )
    return parser


def run_make_sdf(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        atlas_width, atlas_height = _parse_atlas_size(args.atlas_size)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2

    try:
        point_size = _parse_point_size(args.point_size)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2

    if args.padding <= 0:
        parser.error("--padding must be a positive integer.")
        return 2

    try:
        ttf_path = _resolve_ttf_path(args.ttf)
    except FileNotFoundError as exc:
        logger.error("Error: %s", exc)
        return 1

    try:
        charset_text = _load_charset_text(args.charset)
    except FileNotFoundError as exc:
        logger.error("Error: %s", exc)
        return 1

    unicodes = _text_to_unicodes(charset_text)
    if not unicodes:
        logger.error("Error: charset is empty.")
        return 1

    ttf_data = ttf_path.read_bytes()
    render_mode = args.rendermode.lower()
    mode_suffix = "SDF" if render_mode == "sdf" else "Raster"

    generated = generate_sdf_assets_from_ttf(
        ttf_data=ttf_data,
        font_name=ttf_path.stem,
        unicodes=unicodes,
        point_size=point_size,
        atlas_padding=int(args.padding),
        atlas_width=atlas_width,
        atlas_height=atlas_height,
        render_mode=render_mode,
        log_fn=logger.info,
    )
    if generated is None:
        logger.error("Error: failed to generate atlas/json from TTF.")
        return 1

    normalized_name = normalize_font_name(ttf_path.stem)
    output_dir = ttf_path.parent
    json_path = output_dir / f"{normalized_name} {mode_suffix}.json"
    atlas_path = output_dir / f"{normalized_name} {mode_suffix} Atlas.png"
    material_path = output_dir / f"{normalized_name} {mode_suffix} Material.json"

    sdf_data = generated.get("sdf_data")
    atlas_image = generated.get("sdf_atlas")
    material_data = generated.get("sdf_materials")
    if not isinstance(sdf_data, dict) or atlas_image is None:
        logger.error("Error: generator returned invalid payload.")
        return 1

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(sdf_data, f, indent=4, ensure_ascii=False)
    atlas_image.save(atlas_path)

    if isinstance(material_data, dict):
        with open(material_path, "w", encoding="utf-8") as f:
            json.dump(material_data, f, indent=4, ensure_ascii=False)

    logger.info("Generated: %s", json_path)
    logger.info("Generated: %s", atlas_path)
    if material_path.exists():
        logger.info("Generated: %s", material_path)
    logger.info(
        "Summary: "
        f"glyphs={len(sdf_data.get('m_GlyphTable', []))}, "
        f"chars={len(sdf_data.get('m_CharacterTable', []))}, "
        f"atlas={int(sdf_data.get('m_AtlasWidth', 0))}x{int(sdf_data.get('m_AtlasHeight', 0))}, "
        f"padding={int(sdf_data.get('m_AtlasPadding', 0))}, "
        f"rendermode={render_mode}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_make_sdf())
