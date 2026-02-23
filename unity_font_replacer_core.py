from __future__ import annotations

import argparse
import atexit
import gc
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback as tb_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Any, Callable, Iterable, Literal, NoReturn, cast

import UnityPy
from PIL import Image, ImageStat
from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator


Language = Literal["ko", "en"]
JsonDict = dict[str, Any]
_REGISTERED_TEMP_DIRS: set[str] = set()
PS5_SWIZZLE_MASK_X = 0x385F0
PS5_SWIZZLE_MASK_Y = 0x07A0F
PS5_SWIZZLE_ROTATE = 90


class TeeWriter:
    """KR: stdout/stderr를 콘솔과 파일에 동시에 기록합니다.
    EN: Mirror stdout/stderr to both console and file.
    """

    def __init__(self, file: io.TextIOBase, original_stream: io.TextIOBase) -> None:
        """KR: 출력 대상 파일과 원본 스트림을 저장합니다.
        EN: Store target file stream and original stream.
        """
        self.file = file
        self.original = original_stream

    def write(self, data: str) -> int:
        """KR: 문자열을 두 스트림에 동시에 기록합니다.
        EN: Write text to both streams.
        """
        self.original.write(data)
        self.file.write(data)
        self.file.flush()
        return len(data)

    def flush(self) -> None:
        """KR: 두 스트림 버퍼를 모두 비웁니다.
        EN: Flush both stream buffers.
        """
        self.original.flush()
        self.file.flush()

    def fileno(self) -> int:
        """KR: 원본 스트림 파일 디스크립터를 반환합니다.
        EN: Return original stream file descriptor.
        """
        return self.original.fileno()

    @property
    def encoding(self) -> str:
        """KR: 원본 스트림 인코딩을 반환합니다.
        EN: Return encoding of the original stream.
        """
        return self.original.encoding


def find_ggm_file(data_path: str) -> str | None:
    """KR: 데이터 폴더에서 globalgamemanagers 계열 파일 경로를 찾습니다.
    EN: Find a globalgamemanagers-like file inside the data folder.
    """
    candidates = ["globalgamemanagers", "globalgamemanagers.assets", "data.unity3d"]
    candidates_resources = ["unity default resources", "unity_builtin_extra"]
    fls: list[str] = []
    # Prefer core globalgamemanagers files first.
    for candidate in candidates:
        ggm_path = os.path.join(data_path, candidate)
        if os.path.exists(ggm_path):
            fls.append(ggm_path)
    for candidate in candidates_resources:
        ggm_path = os.path.join(data_path, "Resources", candidate)
        if os.path.exists(ggm_path):
            fls.append(ggm_path)
    if fls:
        return fls[0]
    return None


def resolve_game_path(path: str, lang: Language = "ko") -> tuple[str, str]:
    """KR: 입력 경로를 게임 루트와 _Data 경로로 정규화합니다.
    EN: Normalize input path to game root and _Data folder path.
    """
    path = os.path.normpath(os.path.abspath(path))

    if path.lower().endswith("_data"):
        data_path = path
        game_path = os.path.dirname(path)
    else:
        game_path = path
        data_folders = [d for d in os.listdir(path) if d.lower().endswith("_data") and os.path.isdir(os.path.join(path, d))]

        if not data_folders:
            if lang == "ko":
                raise FileNotFoundError(f"'{path}'에서 _Data 폴더를 찾을 수 없습니다.")
            raise FileNotFoundError(f"Could not find _Data folder in '{path}'.")

        data_path = os.path.join(game_path, data_folders[0])

    ggm_path = find_ggm_file(data_path)
    if not ggm_path:
        if lang == "ko":
            raise FileNotFoundError(
                f"'{data_path}'에서 globalgamemanagers 파일을 찾을 수 없습니다.\n올바른 Unity 게임 폴더인지 확인해주세요."
            )
        raise FileNotFoundError(
            f"Could not find a globalgamemanagers file in '{data_path}'.\nPlease verify this is a valid Unity game folder."
        )

    return game_path, data_path


def get_data_path(game_path: str, lang: Language = "ko") -> str:
    """KR: 게임 루트에서 _Data 폴더 경로를 반환합니다.
    EN: Return _Data folder path from game root.
    """
    data_folders = [i for i in os.listdir(game_path) if i.lower().endswith("_data")]
    if not data_folders:
        if lang == "ko":
            raise FileNotFoundError(f"'{game_path}'에서 _Data 폴더를 찾을 수 없습니다.")
        raise FileNotFoundError(f"Could not find _Data folder in '{game_path}'.")
    return os.path.join(game_path, data_folders[0])


def get_unity_version(game_path: str, lang: Language = "ko") -> str:
    """KR: 게임 경로에서 Unity 버전을 읽어 반환합니다.
    EN: Read and return Unity version from the game path.
    """
    data_path = get_data_path(game_path, lang=lang)
    candidates = [
        os.path.join(data_path, "globalgamemanagers"),
        os.path.join(data_path, "globalgamemanagers.assets"),
        os.path.join(data_path, "data.unity3d"),
    ]
    existing_candidates = [p for p in candidates if os.path.exists(p)]
    if not existing_candidates:
        if lang == "ko":
            raise FileNotFoundError(
                f"'{data_path}'에서 globalgamemanagers 파일을 찾을 수 없습니다.\n올바른 Unity 게임 폴더인지 확인해주세요."
            )
        raise FileNotFoundError(
            f"Could not find a globalgamemanagers file in '{data_path}'.\nPlease verify this is a valid Unity game folder."
        )

    for candidate in existing_candidates:
        env = None
        try:
            env = UnityPy.load(candidate)

            # 1) Fast path: top-level file may already expose unity_version.
            top_file = getattr(env, "file", None)
            top_version = getattr(top_file, "unity_version", None)
            if top_version:
                return str(top_version)

            # 2) Check loaded files.
            env_files = getattr(env, "files", None)
            if isinstance(env_files, dict):
                for loaded in env_files.values():
                    uv = getattr(loaded, "unity_version", None)
                    if uv:
                        return str(uv)

            # 3) Fallback: inspect parsed objects only when present.
            objs = getattr(env, "objects", None)
            if objs:
                first_obj = objs[0]
                assets_file = getattr(first_obj, "assets_file", None)
                uv = getattr(assets_file, "unity_version", None)
                if uv:
                    return str(uv)
        except Exception:
            continue
        finally:
            env = None
            gc.collect()

    tried = ", ".join(os.path.basename(p) for p in existing_candidates)
    if lang == "ko":
        raise RuntimeError(f"Unity 버전 감지에 실패했습니다. 시도한 파일: {tried}")
    raise RuntimeError(f"Failed to detect Unity version. Tried files: {tried}")


def get_script_dir() -> str:
    """KR: 실행 기준 디렉터리(스크립트/배포 바이너리)를 반환합니다.
    EN: Return runtime directory for script or frozen executable.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def parse_target_files_arg(target_file_args: list[str] | None) -> set[str]:
    """KR: --target-file 인자(반복/콤마 구분)를 파일명 집합으로 정규화합니다.
    EN: Normalize --target-file args (repeatable/comma-separated) into a basename set.
    """
    selected_files: set[str] = set()
    if not target_file_args:
        return selected_files
    for entry in target_file_args:
        for token in str(entry).split(","):
            name = os.path.basename(token.strip())
            if name:
                selected_files.add(name)
    return selected_files


def strip_wrapping_quotes_repeated(value: str) -> str:
    """KR: 앞뒤 따옴표(' 또는 ")를 반복 제거합니다.
    EN: Repeatedly strip wrapping quotes (' or ") from both ends.
    """
    text = str(value).strip()
    while True:
        updated = text.strip().strip('"').strip("'")
        if updated == text:
            return updated
        text = updated


def sanitize_filename_component(value: str, fallback: str = "unnamed", max_len: int = 96) -> str:
    """KR: 파일명 구성요소에서 경로/예약 문자를 안전한 문자로 치환합니다.
    EN: Sanitize filename component by replacing path/reserved characters.
    """
    text = str(value or "").strip()
    invalid_chars = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid_chars else ch for ch in text)
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def resolve_output_only_path(source_file: str, data_path: str, output_root: str) -> str:
    """KR: output-only 저장 시 원본 data_path 기준 상대 경로를 유지한 출력 경로를 계산합니다.
    EN: Resolve output-only destination path while preserving path relative to data_path.
    """
    source_abs = os.path.abspath(source_file)
    data_abs = os.path.abspath(data_path)
    output_abs = os.path.abspath(output_root)
    try:
        rel_path = os.path.relpath(source_abs, data_abs)
    except ValueError:
        rel_path = os.path.basename(source_abs)
    if rel_path.startswith("..") or os.path.isabs(rel_path):
        rel_path = os.path.basename(source_abs)
    return os.path.join(output_abs, rel_path)


def register_temp_dir_for_cleanup(path: str) -> str:
    """KR: 종료 시 삭제할 임시 디렉터리를 등록하고 정규화 경로를 반환합니다.
    EN: Register a temp directory for cleanup at exit and return normalized path.
    """
    normalized = os.path.abspath(path)
    _REGISTERED_TEMP_DIRS.add(normalized)
    return normalized


def cleanup_registered_temp_dirs() -> None:
    """KR: 등록된 임시 디렉터리를 깊은 경로부터 안전하게 삭제합니다.
    EN: Safely remove registered temp directories from deepest paths first.
    """
    if not _REGISTERED_TEMP_DIRS:
        return
    for temp_dir in sorted(_REGISTERED_TEMP_DIRS, key=len, reverse=True):
        try:
            if os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception:
            pass
    _REGISTERED_TEMP_DIRS.clear()


atexit.register(cleanup_registered_temp_dirs)


def _close_unitypy_reader(obj: Any) -> None:
    """KR: UnityPy 내부 reader/object를 안전하게 dispose합니다.
    EN: Safely dispose UnityPy internal reader/object resources.
    """
    if obj is None:
        return
    reader = getattr(obj, "reader", None)
    if reader is not None and hasattr(reader, "dispose"):
        try:
            reader.dispose()
        except Exception:
            pass
    if hasattr(obj, "dispose"):
        try:
            obj.dispose()
        except Exception:
            pass


def close_unitypy_env(environment: Any) -> None:
    """KR: Environment에 연결된 UnityPy 파일 리소스를 순회 종료합니다.
    EN: Walk and close UnityPy file resources attached to environment.
    """
    if environment is None:
        return
    stack: list[Any] = []
    files = getattr(environment, "files", None)
    if isinstance(files, dict):
        stack.extend(files.values())
    while stack:
        item = stack.pop()
        _close_unitypy_reader(item)
        sub_files = getattr(item, "files", None)
        if isinstance(sub_files, dict):
            stack.extend(sub_files.values())


def normalize_font_name(name: str) -> str:
    """KR: 확장자/SDF 접미사를 제거해 폰트 기본 이름으로 정규화합니다.
    EN: Normalize font name by removing extension and SDF suffixes.
    """
    for ext in [".ttf", ".otf", ".json", ".png"]:
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
    for suffix in (
        " SDF Atlas",
        " Raster Atlas",
        " Atlas",
        " SDF Material",
        " Raster Material",
        " Material",
        " SDF",
        " Raster",
    ):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name


def parse_bool_flag(value: Any) -> bool:
    """KR: 문자열/숫자/불리언 입력을 안전하게 bool로 해석합니다.
    EN: Safely interpret string/number/bool values as bool.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


@lru_cache(maxsize=128)
def _ps5_bit_positions(mask: int) -> tuple[int, ...]:
    return tuple(i for i in range(max(mask.bit_length(), 0)) if (mask >> i) & 1)


@lru_cache(maxsize=128)
def _ps5_axis_tile_size(mask: int) -> int:
    positions = _ps5_bit_positions(mask)
    return 1 << len(positions) if positions else 1


@lru_cache(maxsize=128)
def _ps5_deposit_table(mask: int) -> tuple[int, ...]:
    """KR: 마스크 비트폭(타일 기준) pdep 유사 배치 테이블을 생성합니다.
    EN: Build a pdep-like deposit table using mask bit-width (tile-local axis).
    """
    positions = _ps5_bit_positions(mask)
    axis_size = _ps5_axis_tile_size(mask)
    table: list[int] = [0] * axis_size
    for value in range(axis_size):
        deposited = 0
        for bit_index, dst_bit in enumerate(positions):
            if (value >> bit_index) & 1:
                deposited |= (1 << dst_bit)
        table[value] = deposited
    return tuple(table)


def _ps5_validate_texture_shape(data: bytes, width: int, height: int, bytes_per_element: int) -> int:
    if width <= 0 or height <= 0 or bytes_per_element <= 0:
        raise ValueError(
            f"Invalid texture shape for swizzle: width={width}, height={height}, bpe={bytes_per_element}"
        )
    total_elements = width * height
    expected_size = total_elements * bytes_per_element
    if len(data) != expected_size:
        raise ValueError(
            f"Texture data size mismatch: expected={expected_size}, got={len(data)} "
            f"(w={width}, h={height}, bpe={bytes_per_element})"
        )
    return total_elements


def ps5_unswizzle_bytes(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int = PS5_SWIZZLE_MASK_X,
    mask_y: int = PS5_SWIZZLE_MASK_Y,
) -> bytes:
    """KR: PS5 swizzled 바이트 배열을 선형 순서로 변환합니다.
    EN: Convert PS5-swizzled bytes into linear row-major bytes.
    """
    total_elements = _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    src = memoryview(data)
    dst = bytearray(len(data))
    tile_w = _ps5_axis_tile_size(mask_x)
    tile_h = _ps5_axis_tile_size(mask_y)
    xdep = _ps5_deposit_table(mask_x)
    ydep = _ps5_deposit_table(mask_y)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h

    for y in range(height):
        row_start = y * width
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = ydep[local_y]
        for x in range(width):
            macro_x = x // tile_w
            local_x = x % tile_w
            tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
            src_idx = tile_base + row_offset + xdep[local_x]
            if src_idx < 0 or src_idx >= total_elements:
                raise ValueError(
                    f"PS5 unswizzle index out of range: idx={src_idx}, total={total_elements}, "
                    f"w={width}, h={height}, mask_x={mask_x:#x}, mask_y={mask_y:#x}"
                )
            src_off = src_idx * bytes_per_element
            dst_off = (row_start + x) * bytes_per_element
            dst[dst_off: dst_off + bytes_per_element] = src[src_off: src_off + bytes_per_element]

    return bytes(dst)


def ps5_swizzle_bytes(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int = PS5_SWIZZLE_MASK_X,
    mask_y: int = PS5_SWIZZLE_MASK_Y,
) -> bytes:
    """KR: 선형 순서 바이트 배열을 PS5 swizzle 순서로 변환합니다.
    EN: Convert linear row-major bytes into PS5-swizzled order.
    """
    total_elements = _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    src = memoryview(data)
    dst = bytearray(len(data))
    tile_w = _ps5_axis_tile_size(mask_x)
    tile_h = _ps5_axis_tile_size(mask_y)
    xdep = _ps5_deposit_table(mask_x)
    ydep = _ps5_deposit_table(mask_y)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h

    for y in range(height):
        row_start = y * width
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = ydep[local_y]
        for x in range(width):
            macro_x = x // tile_w
            local_x = x % tile_w
            tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
            dst_idx = tile_base + row_offset + xdep[local_x]
            if dst_idx < 0 or dst_idx >= total_elements:
                raise ValueError(
                    f"PS5 swizzle index out of range: idx={dst_idx}, total={total_elements}, "
                    f"w={width}, h={height}, mask_x={mask_x:#x}, mask_y={mask_y:#x}"
                )
            src_off = (row_start + x) * bytes_per_element
            dst_off = dst_idx * bytes_per_element
            dst[dst_off: dst_off + bytes_per_element] = src[src_off: src_off + bytes_per_element]

    return bytes(dst)


def _ps5_mode_for_swizzle(image: Image.Image) -> str:
    mode = image.mode
    if mode in {"L", "LA", "RGB", "RGBA"}:
        return mode
    if mode == "P":
        return "L"
    return "RGBA"


def _ps5_prepare_image(image: Image.Image) -> Image.Image:
    mode = _ps5_mode_for_swizzle(image)
    if image.mode == mode:
        return image
    return image.convert(mode)


def _ps5_roughness_score(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    max_axis_samples: int = 256,
) -> float:
    """KR: 로컬 픽셀 변화량 기반 거칠기 점수를 계산합니다.
    EN: Compute a local variation roughness score.
    """
    _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    view = memoryview(data)
    step_x = max(1, width // max_axis_samples)
    step_y = max(1, height // max_axis_samples)

    channel_index = 0
    if bytes_per_element > 1:
        sums = [0.0] * bytes_per_element
        sums_sq = [0.0] * bytes_per_element
        sample_count = 0
        for y in range(0, height, step_y):
            row_base = y * width * bytes_per_element
            for x in range(0, width, step_x):
                base = row_base + x * bytes_per_element
                sample_count += 1
                for channel in range(bytes_per_element):
                    value = float(view[base + channel])
                    sums[channel] += value
                    sums_sq[channel] += value * value
        if sample_count > 0:
            best_var = -1.0
            for channel in range(bytes_per_element):
                mean = sums[channel] / sample_count
                variance = (sums_sq[channel] / sample_count) - (mean * mean)
                if variance > best_var:
                    best_var = variance
                    channel_index = channel

    dx_sum = 0.0
    dx_count = 0
    if width > step_x:
        for y in range(0, height, step_y):
            row_base = y * width * bytes_per_element
            for x in range(0, width - step_x, step_x):
                left_idx = row_base + x * bytes_per_element + channel_index
                right_idx = left_idx + step_x * bytes_per_element
                dx_sum += abs(float(view[right_idx]) - float(view[left_idx]))
                dx_count += 1

    dy_sum = 0.0
    dy_count = 0
    if height > step_y:
        row_stride = width * bytes_per_element
        for y in range(0, height - step_y, step_y):
            row_base = y * row_stride
            down_base = row_base + step_y * row_stride
            for x in range(0, width, step_x):
                up_idx = row_base + x * bytes_per_element + channel_index
                down_idx = down_base + x * bytes_per_element + channel_index
                dy_sum += abs(float(view[down_idx]) - float(view[up_idx]))
                dy_count += 1

    dx = dx_sum / dx_count if dx_count else 0.0
    dy = dy_sum / dy_count if dy_count else 0.0
    return float(dx + dy)


def detect_ps5_swizzle_state(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int = PS5_SWIZZLE_MASK_X,
    mask_y: int = PS5_SWIZZLE_MASK_Y,
) -> tuple[str, float, float, float, bytes, bytes]:
    """KR: 입력 바이트가 swizzled인지 휴리스틱으로 판별합니다.
    EN: Heuristically detect whether input bytes are likely swizzled.
    """
    raw_score = _ps5_roughness_score(data, width, height, bytes_per_element)
    unswizzled = ps5_unswizzle_bytes(data, width, height, bytes_per_element, mask_x=mask_x, mask_y=mask_y)
    swizzled = ps5_swizzle_bytes(data, width, height, bytes_per_element, mask_x=mask_x, mask_y=mask_y)
    unsw_score = _ps5_roughness_score(unswizzled, width, height, bytes_per_element)
    swz_score = _ps5_roughness_score(swizzled, width, height, bytes_per_element)

    if unsw_score < raw_score * 0.92 and unsw_score <= swz_score * 0.98:
        verdict = "likely_swizzled_input"
    elif raw_score <= unsw_score * 0.92 and raw_score <= swz_score * 0.92:
        verdict = "likely_linear_input"
    else:
        verdict = "inconclusive"

    return verdict, raw_score, unsw_score, swz_score, unswizzled, swizzled


def detect_ps5_swizzle_state_from_image(
    image: Image.Image,
    mask_x: int = PS5_SWIZZLE_MASK_X,
    mask_y: int = PS5_SWIZZLE_MASK_Y,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> tuple[str, float, float, float]:
    """KR: Pillow 이미지의 swizzle 상태를 판별합니다.
    EN: Detect swizzle state from a Pillow image.
    """
    prepared = _ps5_prepare_image(image)

    data = prepared.tobytes()
    bytes_per_element = len(prepared.getbands())
    verdict, raw_score, unsw_score, swz_score, _, _ = detect_ps5_swizzle_state(
        data,
        prepared.width,
        prepared.height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
    )
    return verdict, raw_score, unsw_score, swz_score


def apply_ps5_swizzle_to_image(
    image: Image.Image,
    mask_x: int = PS5_SWIZZLE_MASK_X,
    mask_y: int = PS5_SWIZZLE_MASK_Y,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> Image.Image:
    """KR: 선형 이미지에 PS5 swizzle 변환을 적용합니다.
    EN: Apply PS5 swizzle transform to a linear image.
    """
    prepared = _ps5_prepare_image(image)
    # KR: PS5 atlas 좌표계 보정을 위해 정사각 Atlas에서는 swizzle 전에 역방향 회전을 적용합니다.
    # EN: For PS5 atlas orientation, apply inverse rotation before swizzle on square atlases.
    if rotate % 360 != 0 and prepared.width == prepared.height:
        prepared = prepared.rotate((-rotate) % 360, expand=False)

    data = prepared.tobytes()
    bytes_per_element = len(prepared.getbands())
    swizzled = ps5_swizzle_bytes(
        data,
        prepared.width,
        prepared.height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
    )
    return Image.frombytes(prepared.mode, (prepared.width, prepared.height), swizzled)


def apply_ps5_unswizzle_to_image(
    image: Image.Image,
    mask_x: int = PS5_SWIZZLE_MASK_X,
    mask_y: int = PS5_SWIZZLE_MASK_Y,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> Image.Image:
    """KR: swizzled 이미지에 PS5 unswizzle 변환을 적용합니다.
    EN: Apply PS5 unswizzle transform to a swizzled image.
    """
    prepared = _ps5_prepare_image(image)
    data = prepared.tobytes()
    bytes_per_element = len(prepared.getbands())
    unswizzled = ps5_unswizzle_bytes(
        data,
        prepared.width,
        prepared.height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
    )
    output = Image.frombytes(prepared.mode, (prepared.width, prepared.height), unswizzled)
    if rotate % 360 != 0 and output.width == output.height:
        output = output.rotate(rotate % 360, expand=False)
    return output


def detect_texture_object_ps5_swizzle(
    texture_obj: Any,
    mask_x: int = PS5_SWIZZLE_MASK_X,
    mask_y: int = PS5_SWIZZLE_MASK_Y,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> str | None:
    """KR: Texture2D 오브젝트의 swizzle 상태를 판별합니다.
    EN: Detect swizzle state for a Texture2D object.
    """
    verdict, _ = detect_texture_object_ps5_swizzle_detail(
        texture_obj,
        mask_x=mask_x,
        mask_y=mask_y,
        rotate=rotate,
    )
    return verdict


def detect_texture_object_ps5_swizzle_detail(
    texture_obj: Any,
    mask_x: int = PS5_SWIZZLE_MASK_X,
    mask_y: int = PS5_SWIZZLE_MASK_Y,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> tuple[str | None, str | None]:
    """KR: Texture2D 오브젝트의 swizzle 상태를 판별합니다.
    KR: 반환값은 (판정값, 판정근거)입니다.
    EN: Detect swizzle state for a Texture2D object.
    EN: Returns (verdict, source).
    """
    try:
        texture = texture_obj.parse_as_object()
        width = int(getattr(texture, "m_Width", 0) or 0)
        height = int(getattr(texture, "m_Height", 0) or 0)
        stream_data = getattr(texture, "m_StreamData", None)
        try:
            stream_size = int(getattr(stream_data, "size", 0) or 0)
        except Exception:
            stream_size = 0
        is_readable = bool(getattr(texture, "m_IsReadable", False))
        try:
            texture_format = int(getattr(texture, "m_TextureFormat", -1) or -1)
        except Exception:
            texture_format = -1

        image_data = getattr(texture, "image_data", None)
        if isinstance(image_data, (bytes, bytearray)):
            image_data_len = len(image_data)
        else:
            image_data_len = 0

        # KR: PS5 샘플에서 stream-backed + non-readable 조합은 swizzle 가능성이 매우 높고,
        # KR: inline readable(image_data) 조합은 linear인 경우가 많습니다.
        # EN: In PS5 samples, stream-backed + non-readable textures are strong swizzle candidates,
        # EN: while inline readable(image_data) payloads are often linear.
        meta_hint: str | None = None
        meta_source: str | None = None
        if width > 0 and height > 0:
            expected_alpha8_size = width * height
            if texture_format == 1 and stream_size > 0 and not is_readable and stream_size == expected_alpha8_size:
                meta_hint = "likely_swizzled_input"
                meta_source = "meta-alpha8-stream"
            elif (
                texture_format == 1
                and stream_size == 0
                and not is_readable
                and image_data_len == expected_alpha8_size
            ):
                meta_hint = "likely_swizzled_input"
                meta_source = "meta-alpha8-inline-nonread"
            elif stream_size > 0 and not is_readable:
                meta_hint = "likely_swizzled_input"
                meta_source = "meta-stream"
            elif stream_size == 0 and is_readable and image_data_len > 0:
                meta_hint = "likely_linear_input"
                meta_source = "meta-inline"

        # KR: 메타 기준이 확실하면 유사도보다 우선합니다.
        # EN: Prefer metadata verdict when it is available.
        if meta_hint is not None:
            return meta_hint, meta_source or "meta"

        if width > 0 and height > 0:
            raw_data: bytes | None = None
            get_image_data = getattr(texture, "get_image_data", None)
            if callable(get_image_data):
                try:
                    candidate = get_image_data()
                    if isinstance(candidate, (bytes, bytearray)):
                        raw_data = bytes(candidate)
                except Exception:
                    raw_data = None
            if raw_data is None:
                image_data = getattr(texture, "image_data", None)
                if isinstance(image_data, (bytes, bytearray)):
                    raw_data = bytes(image_data)

            if raw_data:
                total_elements = width * height
                if total_elements > 0 and (len(raw_data) % total_elements) == 0:
                    bytes_per_element = len(raw_data) // total_elements
                    if bytes_per_element > 0:
                        try:
                            verdict, *_ = detect_ps5_swizzle_state(
                                raw_data,
                                width,
                                height,
                                bytes_per_element,
                                mask_x=mask_x,
                                mask_y=mask_y,
                            )
                            if verdict != "inconclusive":
                                return verdict, "raw-data"
                        except Exception:
                            pass

        image = getattr(texture, "image", None)
        if isinstance(image, Image.Image):
            verdict, _, _, _ = detect_ps5_swizzle_state_from_image(
                image,
                mask_x=mask_x,
                mask_y=mask_y,
                rotate=rotate,
            )
            return verdict, "image"
        return None, None
    except Exception:
        return None, None


def warn_unitypy_version(
    expected_major_minor: tuple[int, int] = (1, 24),
    lang: Language = "ko",
) -> None:
    """KR: UnityPy 버전을 점검하고 권장 버전과 다르면 경고합니다.
    EN: Check UnityPy version and print warning when it differs from recommendation.
    """
    version = getattr(UnityPy, "__version__", "")
    try:
        parts = version.split(".")
        major = int(parts[0])
        minor = int(parts[1])
    except (ValueError, IndexError, AttributeError):
        if lang == "ko":
            print(f"[경고] UnityPy 버전을 확인할 수 없습니다: '{version}'")
        else:
            print(f"[Warning] Could not determine UnityPy version: '{version}'")
        return

    if (major, minor) != expected_major_minor:
        expected = f"{expected_major_minor[0]}.{expected_major_minor[1]}.x"
        if lang == "ko":
            print(f"[경고] 현재 UnityPy {version} 사용 중입니다. 권장 검증 버전은 {expected}입니다.")
        else:
            print(f"[Warning] Using UnityPy {version}. Recommended validated version is {expected}.")


def build_replacement_lookup(
    replacements: dict[str, JsonDict],
) -> tuple[dict[tuple[str, str, str, int], str], set[str]]:
    """KR: 교체 JSON을 빠른 조회용 룩업 테이블로 변환합니다.
    EN: Build fast lookup structures from replacement JSON data.
    """
    lookup: dict[tuple[str, str, str, int], str] = {}
    files_to_process: set[str] = set()

    for info in replacements.values():
        replace_to = info.get("Replace_to")
        if not replace_to:
            continue

        file_name_raw = info.get("File")
        assets_name_raw = info.get("assets_name")
        path_id_raw = info.get("Path_ID")
        type_name_raw = info.get("Type")

        if not isinstance(file_name_raw, str) or not file_name_raw:
            continue
        if not isinstance(assets_name_raw, str) or not assets_name_raw:
            continue
        if not isinstance(type_name_raw, str) or not type_name_raw:
            continue
        if path_id_raw is None:
            continue

        try:
            path_id = int(path_id_raw)
        except (TypeError, ValueError):
            continue

        normalized_target = normalize_font_name(str(replace_to))
        lookup[(type_name_raw, file_name_raw, assets_name_raw, path_id)] = normalized_target
        files_to_process.add(file_name_raw)

    return lookup, files_to_process


def debug_parse_enabled() -> bool:
    """KR: 디버그 파싱 로그 활성화 여부를 반환합니다.
    EN: Return whether parse debug logging is enabled.
    """
    return os.environ.get("UFR_DEBUG_PARSE", "").strip() == "1"


def debug_parse_log(message: str) -> None:
    """KR: 디버그 모드일 때만 파싱 로그를 출력합니다.
    EN: Print parsing debug message only when enabled.
    """
    if debug_parse_enabled():
        print(message)


def ensure_int(data: JsonDict | None, keys: Iterable[str]) -> None:
    """KR: 딕셔너리의 지정 키 값을 int로 강제 변환합니다.
    EN: Force-convert specified dictionary keys to integers.
    """
    if not data:
        return
    for key in keys:
        if key in data and data[key] is not None:
            data[key] = int(data[key])


def detect_tmp_version(data: JsonDict) -> Literal["new", "old"]:
    """KR: SDF TMP 데이터가 신형/구형 포맷인지 판별합니다.
    EN: Detect whether SDF TMP data uses new or old schema.
    """
    has_new_glyphs = len(data.get("m_GlyphTable", [])) > 0
    has_old_glyphs = len(data.get("m_glyphInfoList", [])) > 0

    # KR: 두 포맷 키가 동시에 있어도 실제 글리프가 있는 쪽을 우선합니다.
    # EN: When both schema keys exist, prefer the side that has real glyph data.
    if has_new_glyphs:
        return "new"
    if has_old_glyphs:
        return "old"

    # KR: 글리프가 비어 있으면 필드 존재 여부로 포맷을 추정합니다.
    # EN: If glyphs are empty, infer format by field presence.
    if "m_FaceInfo" in data:
        return "new"
    if "m_fontInfo" in data:
        return "old"

    return "new"


def convert_face_info_new_to_old(
    face_info: JsonDict,
    atlas_padding: int = 0,
    atlas_width: int = 0,
    atlas_height: int = 0,
) -> JsonDict:
    """KR: 신형 m_FaceInfo를 구형 m_fontInfo 구조로 변환합니다.
    EN: Convert new m_FaceInfo to old m_fontInfo schema.
    """
    return {
        "Name": face_info.get("m_FamilyName", ""),
        "PointSize": face_info.get("m_PointSize", 0),
        "Scale": face_info.get("m_Scale", 1.0),
        "CharacterCount": 0,
        "LineHeight": face_info.get("m_LineHeight", 0),
        "Baseline": face_info.get("m_Baseline", 0),
        "Ascender": face_info.get("m_AscentLine", 0),
        "CapHeight": face_info.get("m_CapLine", 0),
        "Descender": face_info.get("m_DescentLine", 0),
        "CenterLine": face_info.get("m_MeanLine", 0),
        "SuperscriptOffset": face_info.get("m_SuperscriptOffset", 0),
        "SubscriptOffset": face_info.get("m_SubscriptOffset", 0),
        "SubSize": face_info.get("m_SubscriptSize", 0.5),
        "Underline": face_info.get("m_UnderlineOffset", 0),
        "UnderlineThickness": face_info.get("m_UnderlineThickness", 0),
        "strikethrough": face_info.get("m_StrikethroughOffset", 0),
        "strikethroughThickness": face_info.get("m_StrikethroughThickness", 0),
        "TabWidth": face_info.get("m_TabWidth", 0),
        "Padding": atlas_padding,
        "AtlasWidth": atlas_width,
        "AtlasHeight": atlas_height,
    }


def convert_face_info_old_to_new(font_info: JsonDict) -> JsonDict:
    """KR: 구형 m_fontInfo를 신형 m_FaceInfo 구조로 변환합니다.
    EN: Convert old m_fontInfo to new m_FaceInfo schema.
    """
    return {
        "m_FaceIndex": 0,
        "m_FamilyName": font_info.get("Name", ""),
        "m_StyleName": "regular",
        "m_PointSize": font_info.get("PointSize", 0),
        "m_Scale": font_info.get("Scale", 1.0),
        "m_UnitsPerEM": 0,
        "m_LineHeight": font_info.get("LineHeight", 0),
        "m_AscentLine": font_info.get("Ascender", 0),
        "m_CapLine": font_info.get("CapHeight", 0),
        "m_MeanLine": font_info.get("CenterLine", 0),
        "m_Baseline": font_info.get("Baseline", 0),
        "m_DescentLine": font_info.get("Descender", 0),
        "m_SuperscriptOffset": font_info.get("SuperscriptOffset", 0),
        "m_SuperscriptSize": 0.5,
        "m_SubscriptOffset": font_info.get("SubscriptOffset", 0),
        "m_SubscriptSize": font_info.get("SubSize", 0.5),
        "m_UnderlineOffset": font_info.get("Underline", 0),
        "m_UnderlineThickness": font_info.get("UnderlineThickness", 0),
        "m_StrikethroughOffset": font_info.get("strikethrough", 0),
        "m_StrikethroughThickness": font_info.get("strikethroughThickness", 0),
        "m_TabWidth": font_info.get("TabWidth", 0),
    }


def _new_glyph_rect_to_int(rect: JsonDict) -> tuple[int, int, int, int]:
    """KR: 신형 TMP glyph rect를 정수 좌표/크기로 정규화합니다.
    EN: Normalize new TMP glyph rect to integer coordinates/sizes.
    """
    x = int(round(float(rect.get("m_X", 0))))
    y = int(round(float(rect.get("m_Y", 0))))
    w = max(1, int(round(float(rect.get("m_Width", 0)))))
    h = max(1, int(round(float(rect.get("m_Height", 0)))))
    return x, y, w, h


def detect_new_glyph_y_flip(
    glyph_table: list[JsonDict],
    char_table: list[JsonDict],
    atlas_image: Image.Image | None,
    sample_limit: int = 256,
) -> bool:
    """KR: 신형 TMP glyph Y축이 구형 TMP 기준으로 반전되어 있는지 추정합니다.
    EN: Estimate whether new TMP glyph Y coordinates must be flipped for old TMP.
    """
    if atlas_image is None or not glyph_table or not char_table:
        return False

    glyph_by_index: dict[int, JsonDict] = {}
    for glyph in glyph_table:
        glyph_by_index[int(glyph.get("m_Index", 0))] = glyph

    # KR: 문자 테이블 순서를 따라 샘플을 뽑아 실제 렌더와 가까운 분포를 사용합니다.
    # EN: Sample in character-table order to match runtime usage distribution.
    rect_samples: list[tuple[int, int, int, int]] = []
    seen_indices: set[int] = set()
    for char in char_table:
        glyph_idx = int(char.get("m_GlyphIndex", -1))
        if glyph_idx in seen_indices:
            continue
        seen_indices.add(glyph_idx)
        glyph = glyph_by_index.get(glyph_idx)
        if not glyph:
            continue
        rect = glyph.get("m_GlyphRect", {})
        x, y, w, h = _new_glyph_rect_to_int(rect)
        if w <= 1 or h <= 1:
            continue
        rect_samples.append((x, y, w, h))

    if not rect_samples:
        return False

    if len(rect_samples) > sample_limit:
        step = max(1, len(rect_samples) // sample_limit)
        rect_samples = rect_samples[::step][:sample_limit]

    if "A" in atlas_image.getbands():
        alpha = atlas_image.getchannel("A")
    else:
        alpha = atlas_image.convert("L")

    atlas_w, atlas_h = alpha.size

    def _score(flip_y: bool) -> tuple[int, float, int]:
        """KR: 후보 좌표계(flip 여부)에서 글리프 영역 유효도를 계산합니다.
        EN: Score glyph-region validity for a candidate coordinate space (flipped or not).
        """
        non_zero_count = 0
        mean_sum = 0.0
        valid_rects = 0

        for x, y, w, h in rect_samples:
            yy = atlas_h - y - h if flip_y else y
            x0 = max(0, min(atlas_w - 1, x))
            y0 = max(0, min(atlas_h - 1, yy))
            x1 = max(x0 + 1, min(atlas_w, x0 + w))
            y1 = max(y0 + 1, min(atlas_h, y0 + h))

            if x1 <= x0 or y1 <= y0:
                continue

            region = alpha.crop((x0, y0, x1, y1))
            stats = ImageStat.Stat(region)
            mean_sum += float(stats.mean[0]) if stats.mean else 0.0
            if region.getbbox() is not None:
                non_zero_count += 1
            valid_rects += 1

        return non_zero_count, mean_sum, valid_rects

    direct_non_zero, direct_mean, direct_valid = _score(False)
    flipped_non_zero, flipped_mean, flipped_valid = _score(True)

    valid_count = min(direct_valid, flipped_valid)
    if valid_count == 0:
        return False

    non_zero_margin = max(2, valid_count // 20)  # 5%
    return (
        flipped_non_zero > direct_non_zero + non_zero_margin
        or (flipped_non_zero >= direct_non_zero and flipped_mean > (direct_mean * 1.2))
    )


def convert_glyphs_new_to_old(
    glyph_table: list[JsonDict],
    char_table: list[JsonDict],
    atlas_height: int | None = None,
    flip_y: bool = False,
) -> list[JsonDict]:
    """KR: 신형 글리프/문자 테이블을 구형 m_glyphInfoList로 변환합니다.
    EN: Convert new glyph/character tables into old m_glyphInfoList.
    """
    glyph_by_index: dict[int, JsonDict] = {}
    for g in glyph_table:
        glyph_by_index[int(g.get("m_Index", 0))] = g
    result: list[JsonDict] = []
    for char in char_table:
        unicode_val = char.get("m_Unicode", 0)
        glyph_idx = char.get("m_GlyphIndex", 0)
        g = glyph_by_index.get(glyph_idx, {})
        metrics = g.get("m_Metrics", {})
        rect = g.get("m_GlyphRect", {})
        rect_y = float(rect.get("m_Y", 0))
        rect_h = float(rect.get("m_Height", 0))
        if flip_y and atlas_height:
            rect_y = float(atlas_height) - rect_y - rect_h
        result.append({
            "id": int(unicode_val),
            "x": float(rect.get("m_X", 0)),
            "y": rect_y,
            "width": float(metrics.get("m_Width", 0)),
            "height": float(metrics.get("m_Height", 0)),
            "xOffset": float(metrics.get("m_HorizontalBearingX", 0)),
            "yOffset": float(metrics.get("m_HorizontalBearingY", 0)),
            "xAdvance": float(metrics.get("m_HorizontalAdvance", 0)),
            "scale": float(g.get("m_Scale", 1.0)),
        })
    return result


def convert_glyphs_old_to_new(glyph_info_list: list[JsonDict]) -> tuple[list[JsonDict], list[JsonDict]]:
    """KR: 구형 m_glyphInfoList를 신형 테이블 구조로 변환합니다.
    EN: Convert old m_glyphInfoList into new glyph/character tables.
    """
    glyph_table: list[JsonDict] = []
    char_table: list[JsonDict] = []
    glyph_idx = 0
    for glyph in glyph_info_list:
        uid = glyph.get("id", 0)
        glyph_table.append({
            "m_Index": glyph_idx,
            "m_Metrics": {
                "m_Width": glyph.get("width", 0),
                "m_Height": glyph.get("height", 0),
                "m_HorizontalBearingX": glyph.get("xOffset", 0),
                "m_HorizontalBearingY": glyph.get("yOffset", 0),
                "m_HorizontalAdvance": glyph.get("xAdvance", 0),
            },
            "m_GlyphRect": {
                "m_X": int(glyph.get("x", 0)),
                "m_Y": int(glyph.get("y", 0)),
                "m_Width": int(glyph.get("width", 0)),
                "m_Height": int(glyph.get("height", 0)),
            },
            "m_Scale": glyph.get("scale", 1.0),
            "m_AtlasIndex": 0,
            "m_ClassDefinitionType": 0,
        })
        char_table.append({
            "m_ElementType": 1,
            "m_Unicode": int(uid),
            "m_GlyphIndex": glyph_idx,
            "m_Scale": 1.0,
        })
        glyph_idx += 1
    return glyph_table, char_table


def normalize_sdf_data(data: JsonDict, deep_copy: bool = True) -> JsonDict:
    """KR: SDF 교체 데이터를 신형 TMP 형식으로 정규화해 반환합니다.
    KR: deep_copy=True면 입력 데이터를 복사해 원본 변형을 방지합니다.
    EN: Normalize SDF replacement data into the new TMP schema.
    EN: With deep_copy=True, clone input data to avoid mutating the original.
    """
    import copy

    result: JsonDict = copy.deepcopy(data) if deep_copy else data
    version = detect_tmp_version(result)

    if version == "old":
        font_info = result.get("m_fontInfo", {})
        glyph_info_list = result.get("m_glyphInfoList", [])
        atlas_padding = font_info.get("Padding", 0)
        atlas_width = font_info.get("AtlasWidth", 0)
        atlas_height = font_info.get("AtlasHeight", 0)

        # KR: 구형 face/glyph 구조를 신형 TMP 필드로 승격합니다.
        # EN: Upgrade old face/glyph structures to new TMP fields.
        result["m_FaceInfo"] = convert_face_info_old_to_new(font_info)

        glyph_table, char_table = convert_glyphs_old_to_new(glyph_info_list)
        result["m_GlyphTable"] = glyph_table
        result["m_CharacterTable"] = char_table

        # KR: 구형 atlas 참조를 신형 atlas 배열 필드로 보정합니다.
        # EN: Normalize old atlas reference into new atlas-list field.
        if "m_AtlasTextures" not in result or not result["m_AtlasTextures"]:
            atlas_ref = result.get("atlas", {"m_FileID": 0, "m_PathID": 0})
            result["m_AtlasTextures"] = [atlas_ref]
        result.setdefault("m_AtlasWidth", int(atlas_width))
        result.setdefault("m_AtlasHeight", int(atlas_height))
        result.setdefault("m_AtlasPadding", int(atlas_padding))
        result.setdefault("m_AtlasRenderMode", 4118)
        result.setdefault("m_UsedGlyphRects", [])
        result.setdefault("m_FreeGlyphRects", [])

        # KR: 구형 데이터에 누락된 weight table은 기본값으로 채웁니다.
        # EN: Fill missing weight table in old data with a safe default.
        if "m_FontWeightTable" not in result:
            font_weights = result.get("fontWeights", [])
            result["m_FontWeightTable"] = font_weights if font_weights else []

    # KR: 정규화 후 반복 사용을 위해 숫자 타입/기본값을 한 번만 정리합니다.
    # EN: Canonicalize numeric fields/defaults once for repeated reuse.
    try:
        result["m_AtlasWidth"] = int(result.get("m_AtlasWidth", 0) or 0)
        result["m_AtlasHeight"] = int(result.get("m_AtlasHeight", 0) or 0)
        result["m_AtlasPadding"] = int(result.get("m_AtlasPadding", 0) or 0)
    except Exception:
        pass
    result.setdefault("m_AtlasRenderMode", 4118)
    result.setdefault("m_UsedGlyphRects", [])
    result.setdefault("m_FreeGlyphRects", [])
    result.setdefault("m_FontWeightTable", [])

    face_info = result.get("m_FaceInfo")
    if isinstance(face_info, dict):
        ensure_int(face_info, ["m_PointSize", "m_AtlasWidth", "m_AtlasHeight"])

    # KR: Atlas 참조 목록은 공유 변형을 피하기 위해 독립 딕셔너리로 재구성합니다.
    # EN: Rebuild atlas references as standalone dicts to avoid shared mutations.
    atlas_textures_raw = result.get("m_AtlasTextures", [])
    atlas_textures: list[JsonDict] = []
    if isinstance(atlas_textures_raw, list):
        for tex in atlas_textures_raw:
            if isinstance(tex, dict):
                atlas_textures.append({
                    "m_FileID": int(tex.get("m_FileID", 0) or 0),
                    "m_PathID": int(tex.get("m_PathID", 0) or 0),
                })
    if not atlas_textures and isinstance(result.get("atlas"), dict):
        atlas_ref = cast(JsonDict, result.get("atlas"))
        atlas_textures.append({
            "m_FileID": int(atlas_ref.get("m_FileID", 0) or 0),
            "m_PathID": int(atlas_ref.get("m_PathID", 0) or 0),
        })
    result["m_AtlasTextures"] = atlas_textures

    glyph_table = result.get("m_GlyphTable")
    if isinstance(glyph_table, list):
        for glyph in glyph_table:
            if not isinstance(glyph, dict):
                continue
            ensure_int(glyph, ["m_Index", "m_AtlasIndex", "m_ClassDefinitionType"])
            glyph["m_ClassDefinitionType"] = 0
            rect = glyph.get("m_GlyphRect")
            if isinstance(rect, dict):
                ensure_int(rect, ["m_X", "m_Y", "m_Width", "m_Height"])

    char_table = result.get("m_CharacterTable")
    if isinstance(char_table, list):
        for char in char_table:
            if isinstance(char, dict):
                ensure_int(char, ["m_Unicode", "m_GlyphIndex", "m_ElementType"])

    for rect_list_name in ["m_UsedGlyphRects", "m_FreeGlyphRects"]:
        rect_list = result.get(rect_list_name)
        if isinstance(rect_list, list):
            for rect in rect_list:
                if isinstance(rect, dict):
                    ensure_int(rect, ["m_X", "m_Y", "m_Width", "m_Height"])

    creation_settings = result.get("m_CreationSettings")
    if isinstance(creation_settings, dict):
        ensure_int(creation_settings, ["pointSize", "atlasWidth", "atlasHeight", "padding"])

    return result


def find_assets_files(
    game_path: str,
    lang: Language = "ko",
    target_files: set[str] | None = None,
) -> list[str]:
    """KR: 게임에서 처리 대상 에셋 파일 목록을 수집합니다.
    KR: target_files가 있으면 해당 파일명으로 스캔 대상을 제한합니다.
    EN: Collect candidate asset files from the game.
    EN: If target_files is provided, limit candidates to those basenames.
    """
    data_path = get_data_path(game_path, lang=lang)
    assets_files: list[str] = []
    normalized_targets = {os.path.basename(name) for name in target_files} if target_files else None
    blacklist_exts = {
        ".dll", ".manifest", ".exe", ".txt", ".json", ".xml", ".log", ".ini", ".cfg",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".wav", ".mp3", ".ogg", ".mp4",
        ".avi", ".mov",
        ".bak", ".info", ".config",
    }

    for root, _, files in os.walk(data_path):
        for fn in files:
            if normalized_targets is not None and fn not in normalized_targets:
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in blacklist_exts:
                continue
            assets_files.append(os.path.join(root, fn))
    assets_files.sort()
    return assets_files

def get_compile_method(datapath: str) -> str:
    """KR: 데이터 폴더의 컴파일 방식을 Mono/Il2cpp로 판별합니다.
    EN: Detect compile method as Mono or Il2cpp.
    """
    if "Managed" in os.listdir(datapath):
        return "Mono"
    else:
        return "Il2cpp"


def _create_generator(
    unity_version: str,
    game_path: str,
    data_path: str,
    compile_method: str,
    lang: Language = "ko",
) -> TypeTreeGenerator:
    """KR: 타입트리 생성기를 구성하고 Mono/Il2cpp 메타데이터를 로드합니다.
    EN: Build typetree generator and load Mono/Il2cpp metadata.
    """
    generator = TypeTreeGenerator(unity_version)
    if compile_method == "Mono":
        managed_dir = os.path.join(data_path, "Managed")
        for fn in os.listdir(managed_dir):
            if not fn.endswith(".dll"):
                continue
            try:
                with open(os.path.join(managed_dir, fn), "rb") as f:
                    generator.load_dll(f.read())
            except Exception as e:
                if lang == "ko":
                    print(f"[generator] DLL 로드 실패: {fn} ({e})")
                else:
                    print(f"[generator] Failed to load DLL: {fn} ({e})")
    else:
        il2cpp_path = os.path.join(game_path, "GameAssembly.dll")
        with open(il2cpp_path, "rb") as f:
            il2cpp = f.read()
        metadata_path = os.path.join(data_path, "il2cpp_data", "Metadata", "global-metadata.dat")
        with open(metadata_path, "rb") as f:
            metadata = f.read()
        generator.load_il2cpp(il2cpp, metadata)
    return generator


def _scan_fonts_from_env(
    env: Any,
    file_name: str,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> dict[str, list[JsonDict]]:
    """KR: 로드된 UnityPy env에서 TTF/SDF 폰트 정보를 추출합니다.
    EN: Extract TTF/SDF font entries from a loaded UnityPy env.
    """
    scanned: dict[str, list[JsonDict]] = {"ttf": [], "sdf": []}
    texture_lookup: dict[tuple[str, int], Any] = {}
    texture_swizzle_cache: dict[str, str | None] = {}
    if detect_ps5_swizzle:
        for item in env.objects:
            if item.type.name != "Texture2D":
                continue
            texture_lookup[(item.assets_file.name, int(item.path_id))] = item

    for obj in env.objects:
        try:
            if obj.type.name == "Font":
                font_name = obj.peek_name()
                if not font_name:
                    try:
                        font = obj.parse_as_object()
                        font_name = getattr(font, "m_Name", "") or ""
                    except Exception:
                        font_name = ""
                scanned["ttf"].append({
                    "file": file_name,
                    "assets_name": obj.assets_file.name,
                    "name": font_name,
                    "path_id": obj.path_id,
                })
            elif obj.type.name == "MonoBehaviour":
                parse_dict = None
                is_font = False
                atlas_file_id = 0
                atlas_path_id = 0
                try:
                    parse_dict = obj.parse_as_dict()
                    # KR: TMP 스키마 판별: 신형(m_FaceInfo/m_AtlasTextures) 또는 구형(m_fontInfo/atlas)
                    # EN: Detect TMP schema: new(m_FaceInfo/m_AtlasTextures) or old(m_fontInfo/atlas)
                    if ("m_AtlasTextures" in parse_dict and "m_FaceInfo" in parse_dict) or \
                       ("atlas" in parse_dict and "m_fontInfo" in parse_dict):
                        is_font = True
                except Exception:
                    if lang == "ko":
                        debug_parse_log(f"[scan_fonts] parse_as_dict 실패: {file_name} | PathID {obj.path_id}")
                    else:
                        debug_parse_log(f"[scan_fonts] parse_as_dict failed: {file_name} | PathID {obj.path_id}")

                if not is_font:
                    continue

                try:
                    if parse_dict is None:
                        parse_dict = obj.parse_as_dict()
                    atlas_textures = parse_dict.get("m_AtlasTextures", [])
                    glyph_count = len(parse_dict.get("m_GlyphTable", []))
                    if not atlas_textures and isinstance(parse_dict.get("atlas"), dict):
                        atlas_textures = [cast(JsonDict, parse_dict.get("atlas"))]
                    if glyph_count == 0:
                        glyph_count = len(parse_dict.get("m_glyphInfoList", []))
                    if atlas_textures:
                        first_atlas = atlas_textures[0]
                        if isinstance(first_atlas, dict):
                            atlas_file_id = int(first_atlas.get("m_FileID", 0) or 0)
                            atlas_path_id = int(first_atlas.get("m_PathID", 0) or 0)
                            # KR: 외부 참조 stub(FileID!=0, PathID=0)은 실제 교체 대상이 아닙니다.
                            # EN: External stubs (FileID!=0, PathID=0) are not valid replacement targets.
                            if atlas_file_id != 0 and atlas_path_id == 0:
                                continue
                    if glyph_count == 0:
                        continue
                except Exception:
                    if lang == "ko":
                        debug_parse_log(f"[scan_fonts] SDF 필드 검사 실패: {file_name} | PathID {obj.path_id}")
                    else:
                        debug_parse_log(f"[scan_fonts] SDF field check failed: {file_name} | PathID {obj.path_id}")
                    continue

                sdf_info: JsonDict = {
                    "file": file_name,
                    "assets_name": obj.assets_file.name,
                    "name": obj.peek_name(),
                    "path_id": obj.path_id,
                }
                if detect_ps5_swizzle:
                    swizzle_state = False
                    if atlas_file_id == 0 and atlas_path_id != 0:
                        cache_key = f"{obj.assets_file.name}|{atlas_path_id}"
                        if cache_key in texture_swizzle_cache:
                            swizzle_verdict = texture_swizzle_cache[cache_key]
                        else:
                            texture_obj = texture_lookup.get((obj.assets_file.name, atlas_path_id))
                            swizzle_verdict = (
                                detect_texture_object_ps5_swizzle(texture_obj)
                                if texture_obj is not None
                                else None
                            )
                            texture_swizzle_cache[cache_key] = swizzle_verdict
                        swizzle_state = swizzle_verdict == "likely_swizzled_input"
                    sdf_info["swizzle"] = "True" if swizzle_state else "False"

                scanned["sdf"].append(sdf_info)
        except Exception as e:
            if lang == "ko":
                print(f"[scan_fonts] 오브젝트 처리 실패: {file_name} | PathID {obj.path_id} ({e})")
            else:
                print(f"[scan_fonts] Object processing failed: {file_name} | PathID {obj.path_id} ({e})")
            continue

    return scanned


def _scan_fonts_in_asset_file(
    assets_file: str,
    generator: TypeTreeGenerator,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> tuple[dict[str, list[JsonDict]], str | None]:
    """KR: 단일 에셋 파일을 로드해 폰트 정보를 추출합니다.
    EN: Load one asset file and extract font entries.
    """
    file_name = os.path.basename(assets_file)
    scanned: dict[str, list[JsonDict]] = {"ttf": [], "sdf": []}

    env = None
    try:
        env = UnityPy.load(assets_file)
        env.typetree_generator = generator
    except Exception as e:
        if lang == "ko":
            return scanned, f"UnityPy.load 실패: {assets_file} ({e})"
        return scanned, f"UnityPy.load failed: {assets_file} ({e})"

    try:
        scanned = _scan_fonts_from_env(env, file_name, lang=lang, detect_ps5_swizzle=detect_ps5_swizzle)
    finally:
        close_unitypy_env(env)
        env = None
        gc.collect()

    return scanned, None


def _scan_fonts_via_worker(
    game_path: str,
    assets_file: str,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> tuple[dict[str, list[JsonDict]], str | None]:
    """KR: 파일 단위 서브프로세스 워커로 스캔해 크래시를 격리합니다.
    EN: Scan using a per-file subprocess worker to isolate hard crashes.
    """
    fd, output_path = tempfile.mkstemp(prefix="scan_worker_", suffix=".json")
    os.close(fd)
    worker_exit_hints = {
        -1073741819: "ACCESS_VIOLATION(0xC0000005)",
        3221225477: "ACCESS_VIOLATION(0xC0000005)",
    }
    try:
        if getattr(sys, "frozen", False):
            cmd = [
                sys.executable,
                "--gamepath",
                game_path,
                "--_scan-file-worker",
                assets_file,
                "--_scan-file-worker-output",
                output_path,
            ]
        else:
            cmd = [
                sys.executable,
                os.path.abspath(__file__),
                "--gamepath",
                game_path,
                "--_scan-file-worker",
                assets_file,
                "--_scan-file-worker-output",
                output_path,
            ]
        if detect_ps5_swizzle:
            cmd.append("--ps5-swizzle")

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            hint = worker_exit_hints.get(int(proc.returncode))
            hint_text = f" [{hint}]" if hint else ""
            if lang == "ko":
                return {"ttf": [], "sdf": []}, f"scan worker 실패 (exit={proc.returncode}{hint_text}): {detail}"
            return {"ttf": [], "sdf": []}, f"scan worker failed (exit={proc.returncode}{hint_text}): {detail}"

        if not os.path.exists(output_path):
            if lang == "ko":
                return {"ttf": [], "sdf": []}, "scan worker 결과 파일이 없습니다."
            return {"ttf": [], "sdf": []}, "scan worker output file is missing."

        with open(output_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        scanned = {
            "ttf": list(payload.get("ttf", [])) if isinstance(payload, dict) else [],
            "sdf": list(payload.get("sdf", [])) if isinstance(payload, dict) else [],
        }
        worker_error = None
        if isinstance(payload, dict):
            worker_error = payload.get("error")
            if not isinstance(worker_error, str):
                worker_error = None
        return scanned, worker_error
    except Exception as e:
        if lang == "ko":
            return {"ttf": [], "sdf": []}, f"scan worker 실행 실패: {e!r}"
        return {"ttf": [], "sdf": []}, f"failed to run scan worker: {e!r}"
    finally:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass


def scan_fonts(
    game_path: str,
    lang: Language = "ko",
    target_files: set[str] | None = None,
    isolate_files: bool = True,
    scan_jobs: int = 1,
    ps5_swizzle: bool = False,
) -> dict[str, list[JsonDict]]:
    """KR: 게임 에셋을 스캔해 TTF/SDF 폰트 목록을 반환합니다.
    KR: target_files가 있으면 해당 파일만 스캔합니다.
    KR: isolate_files=True면 파일 단위 워커 프로세스로 스캔해 크래시를 격리합니다.
    KR: scan_jobs>1이면 isolate_files 경로에서 워커를 병렬 실행합니다.
    EN: Scan game assets and return TTF/SDF font entries.
    EN: If target_files is provided, only scan those files.
    EN: If isolate_files=True, scan each file via worker subprocess to isolate hard crashes.
    EN: If scan_jobs>1, worker subprocesses are executed in parallel for isolate_files mode.
    """
    data_path = get_data_path(game_path, lang=lang)
    unity_version = get_unity_version(game_path, lang=lang)
    assets_files = find_assets_files(game_path, lang=lang, target_files=target_files)
    compile_method = get_compile_method(data_path)
    generator = _create_generator(unity_version, game_path, data_path, compile_method, lang=lang)

    fonts: dict[str, list[JsonDict]] = {
        "ttf": [],
        "sdf": [],
    }

    total_files = len(assets_files)
    try:
        scan_jobs = int(scan_jobs)
    except Exception:
        scan_jobs = 1
    if scan_jobs < 1:
        scan_jobs = 1
    if lang == "ko":
        if target_files:
            print(f"[scan_fonts] --target-file 기준 스캔 시작: {total_files}개 파일")
        else:
            print(f"[scan_fonts] 전체 스캔 시작: {total_files}개 파일")
    else:
        if target_files:
            print(f"[scan_fonts] Starting target-file scan: {total_files} file(s)")
        else:
            print(f"[scan_fonts] Starting full scan: {total_files} file(s)")

    if isolate_files and scan_jobs > 1 and total_files > 1:
        max_workers = min(scan_jobs, total_files)
        if lang == "ko":
            print(f"[scan_fonts] 병렬 워커 모드: {max_workers}개")
        else:
            print(f"[scan_fonts] Parallel worker mode: {max_workers}")

        indexed_results: dict[int, tuple[dict[str, list[JsonDict]], str | None, str]] = {}
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_meta = {
                executor.submit(
                    _scan_fonts_via_worker,
                    game_path,
                    assets_file,
                    lang,
                    ps5_swizzle,
                ): (idx, os.path.basename(assets_file))
                for idx, assets_file in enumerate(assets_files)
            }
            for future in as_completed(future_to_meta):
                idx, fn = future_to_meta[future]
                try:
                    scanned, worker_error = future.result()
                except Exception as e:
                    scanned = {"ttf": [], "sdf": []}
                    worker_error = (
                        f"scan worker 실행 실패: {e!r}"
                        if lang == "ko"
                        else f"failed to run scan worker: {e!r}"
                    )
                indexed_results[idx] = (scanned, worker_error, fn)
                completed += 1
                if lang == "ko":
                    print(f"[scan_fonts] 진행 {completed}/{total_files}: {fn}")
                else:
                    print(f"[scan_fonts] Progress {completed}/{total_files}: {fn}")

        for idx in range(total_files):
            scanned, worker_error, _ = indexed_results.get(idx, ({"ttf": [], "sdf": []}, None, ""))
            if worker_error:
                if lang == "ko":
                    print(f"[scan_fonts] 워커 경고: {worker_error}")
                else:
                    print(f"[scan_fonts] Worker warning: {worker_error}")
            fonts["ttf"].extend(scanned.get("ttf", []))
            fonts["sdf"].extend(scanned.get("sdf", []))
    else:
        for idx, assets_file in enumerate(assets_files, start=1):
            fn = os.path.basename(assets_file)
            if lang == "ko":
                print(f"[scan_fonts] 진행 {idx}/{total_files}: {fn}")
            else:
                print(f"[scan_fonts] Progress {idx}/{total_files}: {fn}")

            if isolate_files:
                scanned, worker_error = _scan_fonts_via_worker(
                    game_path,
                    assets_file,
                    lang=lang,
                    detect_ps5_swizzle=ps5_swizzle,
                )
                if worker_error:
                    if lang == "ko":
                        print(f"[scan_fonts] 워커 경고: {worker_error}")
                    else:
                        print(f"[scan_fonts] Worker warning: {worker_error}")
                fonts["ttf"].extend(scanned.get("ttf", []))
                fonts["sdf"].extend(scanned.get("sdf", []))
                continue

            scanned, load_error = _scan_fonts_in_asset_file(
                assets_file,
                generator,
                lang=lang,
                detect_ps5_swizzle=ps5_swizzle,
            )
            if load_error:
                print(f"[scan_fonts] {load_error}")
                continue
            fonts["ttf"].extend(scanned.get("ttf", []))
            fonts["sdf"].extend(scanned.get("sdf", []))

    return fonts


def parse_fonts(
    game_path: str,
    lang: Language = "ko",
    target_files: set[str] | None = None,
    scan_jobs: int = 1,
    ps5_swizzle: bool = False,
) -> str:
    """KR: 스캔한 폰트를 JSON으로 저장하고 결과 파일 경로를 반환합니다.
    KR: target_files가 있으면 해당 파일만 파싱합니다.
    EN: Save scanned fonts to JSON and return output file path.
    EN: If target_files is provided, parse only those files.
    """
    # KR: parse 모드는 파일 단위 워커로 스캔해 UnityPy 하드 크래시를 격리합니다.
    # EN: Parse mode scans via per-file workers to isolate hard UnityPy crashes.
    fonts = scan_fonts(
        game_path,
        lang=lang,
        target_files=target_files,
        isolate_files=True,
        scan_jobs=scan_jobs,
        ps5_swizzle=ps5_swizzle,
    )
    game_name = os.path.basename(game_path)
    output_file = os.path.join(get_script_dir(), f"{game_name}.json")

    result: dict[str, JsonDict] = {}

    for font in fonts["ttf"]:
        key = f"{font['file']}|{font['assets_name']}|{font['name']}|TTF|{font['path_id']}"
        result[key] = {
            "File": font["file"],
            "assets_name": font["assets_name"],
            "Path_ID": font["path_id"],
            "Type": "TTF",
            "Name": font["name"],
            "Replace_to": ""
        }

    for font in fonts["sdf"]:
        key = f"{font['file']}|{font['assets_name']}|{font['name']}|SDF|{font['path_id']}"
        if ps5_swizzle:
            swizzle_flag = "True" if parse_bool_flag(font.get("swizzle")) else "False"
            process_swizzle_flag = (
                "True" if parse_bool_flag(font.get("process_swizzle")) else "False"
            )
            entry: JsonDict = {
                "File": font["file"],
                "assets_name": font["assets_name"],
                "Path_ID": font["path_id"],
                "Type": "SDF",
                "Name": font["name"],
                "swizzle": swizzle_flag,
                "process_swizzle": process_swizzle_flag,
                "Replace_to": "",
            }
        else:
            entry = {
                "File": font["file"],
                "assets_name": font["assets_name"],
                "Path_ID": font["path_id"],
                "Type": "SDF",
                "Name": font["name"],
                "Replace_to": "",
            }
        result[key] = entry

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)

    if lang == "ko":
        print(f"폰트 정보가 '{output_file}'에 저장되었습니다.")
        print(f"  - TTF 폰트: {len(fonts['ttf'])}개")
        print(f"  - SDF 폰트: {len(fonts['sdf'])}개")
    else:
        print(f"Font information saved to '{output_file}'.")
        print(f"  - TTF fonts: {len(fonts['ttf'])}")
        print(f"  - SDF fonts: {len(fonts['sdf'])}")
    return output_file


@lru_cache(maxsize=64)
def _load_font_assets_cached(script_dir: str, normalized: str) -> JsonDict:
    """KR: KR_ASSETS에서 폰트 리소스를 읽어 캐시에 저장합니다.
    EN: Load and cache font resources from KR_ASSETS.
    """
    kr_assets = os.path.join(script_dir, "KR_ASSETS")
    raw_name = str(normalized).strip()

    def _dedupe_preserve_order(names: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in names:
            key = item.strip()
            if not key:
                continue
            lowered = key.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            ordered.append(key)
        return ordered

    def _strip_render_suffix(name: str) -> str:
        if name.endswith(" SDF"):
            return name[:-len(" SDF")]
        if name.endswith(" Raster"):
            return name[:-len(" Raster")]
        return name

    base_name = _strip_render_suffix(raw_name)
    has_explicit_variant = raw_name.endswith(" SDF") or raw_name.endswith(" Raster")
    if has_explicit_variant:
        explicit_candidates = [raw_name, base_name]
        if raw_name.endswith(" Raster"):
            explicit_candidates.append(f"{base_name} SDF")
        elif raw_name.endswith(" SDF"):
            explicit_candidates.append(f"{base_name} Raster")
        name_candidates = _dedupe_preserve_order(explicit_candidates)
    else:
        name_candidates = _dedupe_preserve_order([raw_name, f"{base_name} SDF", f"{base_name} Raster"])

    font_name_candidates = _dedupe_preserve_order([raw_name, base_name] + name_candidates)

    ttf_data = None
    for font_name in font_name_candidates:
        for ext in (".ttf", ".otf"):
            font_path = os.path.join(kr_assets, f"{font_name}{ext}")
            if os.path.exists(font_path):
                with open(font_path, "rb") as f:
                    ttf_data = f.read()
                break
        if ttf_data is not None:
            break

    sdf_data = None
    sdf_data_normalized = None
    sdf_swizzle = False
    sdf_process_swizzle = False
    for name_candidate in name_candidates:
        sdf_json_path = os.path.join(kr_assets, f"{name_candidate}.json")
        if not os.path.exists(sdf_json_path):
            continue
        with open(sdf_json_path, "r", encoding="utf-8") as f:
            sdf_data = json.load(f)
        if isinstance(sdf_data, dict):
            sdf_data_normalized = normalize_sdf_data(sdf_data, deep_copy=True)
            sdf_swizzle = parse_bool_flag(sdf_data.get("swizzle"))
            sdf_process_swizzle = parse_bool_flag(sdf_data.get("process_swizzle"))
        break

    sdf_atlas = None
    for name_candidate in name_candidates:
        sdf_atlas_path = os.path.join(kr_assets, f"{name_candidate} Atlas.png")
        if not os.path.exists(sdf_atlas_path):
            continue
        with open(sdf_atlas_path, "rb") as f:
            sdf_atlas = Image.open(f)
            sdf_atlas.load()
        break

    sdf_material_data = None
    for name_candidate in name_candidates:
        sdf_material_path = os.path.join(kr_assets, f"{name_candidate} Material.json")
        if not os.path.exists(sdf_material_path):
            continue
        with open(sdf_material_path, "r", encoding="utf-8") as f:
            sdf_material_data = json.load(f)
        break

    return {
        "ttf_data": ttf_data,
        "sdf_data": sdf_data,
        "sdf_data_normalized": sdf_data_normalized,
        "sdf_atlas": sdf_atlas,
        "sdf_materials": sdf_material_data,
        "sdf_swizzle": sdf_swizzle,
        "sdf_process_swizzle": sdf_process_swizzle,
    }


def load_font_assets(font_name: str) -> JsonDict:
    """KR: 지정 폰트명의 교체용 리소스(TTF/SDF/Atlas/Material)를 로드합니다.
    EN: Load replacement assets (TTF/SDF/Atlas/Material) for a font name.
    """
    normalized = normalize_font_name(font_name)
    cached_assets = _load_font_assets_cached(get_script_dir(), normalized)
    atlas = cached_assets["sdf_atlas"]
    return {
        "ttf_data": cached_assets["ttf_data"],
        "sdf_data": cached_assets["sdf_data"],
        "sdf_data_normalized": cached_assets.get("sdf_data_normalized"),
        # Reuse cached atlas object to avoid per-replacement image duplication.
        "sdf_atlas": atlas,
        "sdf_materials": cached_assets["sdf_materials"],
        "sdf_swizzle": cached_assets.get("sdf_swizzle"),
        "sdf_process_swizzle": bool(cached_assets.get("sdf_process_swizzle", False)),
    }


def replace_fonts_in_file(
    unity_version: str,
    game_path: str,
    assets_file: str,
    replacements: dict[str, JsonDict],
    replace_ttf: bool = True,
    replace_sdf: bool = True,
    use_game_mat: bool = False,
    use_game_line_metrics: bool = False,
    material_scale_by_padding: bool = True,
    prefer_original_compress: bool = False,
    temp_root_dir: str | None = None,
    generator: TypeTreeGenerator | None = None,
    replacement_lookup: dict[tuple[str, str, str, int], str] | None = None,
    ps5_swizzle: bool = False,
    preview: bool = False,
    preview_root: str | None = None,
    lang: Language = "ko",
) -> bool:
    """KR: 단일 assets 파일의 TTF/SDF 폰트를 교체하고 저장합니다.
    KR: 기본 모드는 줄 간격 관련 메트릭(LineHeight/Ascender/Descender 등)을 게임 원본 비율로 보정해
    KR: 교체 pointSize에 맞춰 적용합니다.
    KR: use_game_line_metrics=True면 게임 원본 줄 간격 메트릭을 그대로 사용합니다.
    KR: pointSize는 옵션과 무관하게 교체 폰트 값을 유지합니다.
    KR: material_scale_by_padding=True면 SDF 머티리얼 float를 (게임 padding / 교체 padding) 비율로 보정합니다.
    KR: prefer_original_compress=True면 원본 압축 우선, False면 무압축 계열 우선 저장 전략을 사용합니다.
    KR: ps5_swizzle=True면 대상 Atlas의 swizzle 상태를 판별해 교체 Atlas를 자동 swizzle/unswizzle합니다.
    KR: preview=True이고 ps5_swizzle=True면 preview 폴더에 unswizzle 결과 PNG를 저장합니다.
    KR: temp_root_dir가 지정되면 임시 저장 디렉터리 루트로 사용합니다.
    EN: Replace TTF/SDF fonts in one assets file and save changes.
    EN: By default, line-related metrics (LineHeight/Ascender/Descender, etc.) are adjusted from in-game ratios
    EN: and scaled to match replacement pointSize.
    EN: With use_game_line_metrics=True, original in-game line metrics are used directly.
    EN: pointSize still follows replacement font data regardless of this option.
    EN: If material_scale_by_padding=True, SDF material floats are adjusted by (game padding / replacement padding).
    EN: When prefer_original_compress=True, original compression is tried first; otherwise uncompressed-family is preferred.
    EN: If ps5_swizzle=True, auto-detect target atlas swizzle state and swizzle/unswizzle replacement atlas.
    EN: If preview=True with ps5_swizzle=True, save unswizzled preview PNGs to preview folder.
    EN: If temp_root_dir is set, it is used as the root directory for temporary save files.
    """
    fn_without_path = os.path.basename(assets_file)
    data_path = get_data_path(game_path, lang=lang)
    using_custom_temp_root = temp_root_dir is not None
    tmp_root = os.path.abspath(temp_root_dir) if using_custom_temp_root else os.path.join(data_path, "temp")
    tmp_path = os.path.join(tmp_root, "unity_font_replacer_temp")
    if using_custom_temp_root:
        register_temp_dir_for_cleanup(tmp_path)
    else:
        register_temp_dir_for_cleanup(tmp_root)
    bundle_signatures = {"UnityFS", "UnityWeb", "UnityRaw"}

    def _read_bundle_signature(path: str) -> str | None:
        """KR: 파일 헤더에서 Unity 번들 시그니처를 읽습니다.
        EN: Read Unity bundle signature from file header.
        """
        try:
            with open(path, "rb") as f:
                header = f.read(16)
        except Exception:
            return None

        for sig in bundle_signatures:
            token = (sig + "\x00").encode("ascii")
            if header.startswith(token):
                return sig
        return None

    source_bundle_signature = _read_bundle_signature(assets_file)

    if not os.path.exists(tmp_root):
        os.makedirs(tmp_root, exist_ok=True)

    if os.path.exists(tmp_path):
        shutil.rmtree(tmp_path)
    os.makedirs(tmp_path, exist_ok=True)

    env = UnityPy.load(assets_file)
    env_file = getattr(env, "file", None)
    if env_file is None:
        files = getattr(env, "files", None)
        if isinstance(files, dict) and len(files) == 1:
            env_file = next(iter(files.values()))
    if env_file is None:
        raise RuntimeError("Could not determine primary UnityPy file object for saving.")
    if generator is None:
        compile_method = get_compile_method(data_path)
        generator = _create_generator(unity_version, game_path, data_path, compile_method, lang=lang)
    env.typetree_generator = generator
    if replacement_lookup is None:
        replacement_lookup, _ = build_replacement_lookup(replacements)
    replacement_meta_lookup: dict[tuple[str, str, str, int], JsonDict] = {}
    preview_target_lookup: dict[tuple[str, str, int], JsonDict] = {}
    for info in replacements.values():
        if not isinstance(info, dict):
            continue
        type_raw = info.get("Type")
        file_raw = info.get("File")
        assets_raw = info.get("assets_name")
        path_raw = info.get("Path_ID")
        if not isinstance(type_raw, str) or not isinstance(file_raw, str) or not isinstance(assets_raw, str):
            continue
        try:
            path_id = int(path_raw)
        except (TypeError, ValueError):
            continue
        if type_raw == "SDF":
            preview_target_lookup[(file_raw, assets_raw, path_id)] = info
        if not info.get("Replace_to"):
            continue
        replacement_meta_lookup[(type_raw, file_raw, assets_raw, path_id)] = info

    texture_object_lookup: dict[tuple[str, int], Any] = {}
    texture_swizzle_state_cache: dict[str, tuple[str | None, str | None]] = {}
    for item in env.objects:
        if item.type.name != "Texture2D":
            continue
        texture_object_lookup[(item.assets_file.name, int(item.path_id))] = item

    target_sdf_pathids: set[int] = set()
    target_sdf_font_by_pathid: dict[int, str] = {}
    old_line_metric_keys = (
        "LineHeight",
        "Baseline",
        "Ascender",
        "CapHeight",
        "Descender",
        "CenterLine",
        "Scale",
        "SuperscriptOffset",
        "SubscriptOffset",
        "SubSize",
        "Underline",
        "UnderlineThickness",
        "strikethrough",
        "strikethroughThickness",
        "TabWidth",
    )
    old_line_metric_scale_keys = (
        "LineHeight",
        "Baseline",
        "Ascender",
        "CapHeight",
        "Descender",
        "CenterLine",
        "SuperscriptOffset",
        "SubscriptOffset",
        "Underline",
        "UnderlineThickness",
        "strikethrough",
        "strikethroughThickness",
        "TabWidth",
    )
    new_line_metric_keys = (
        "m_LineHeight",
        "m_AscentLine",
        "m_CapLine",
        "m_MeanLine",
        "m_Baseline",
        "m_DescentLine",
        "m_Scale",
        "m_SuperscriptOffset",
        "m_SuperscriptSize",
        "m_SubscriptOffset",
        "m_SubscriptSize",
        "m_UnderlineOffset",
        "m_UnderlineThickness",
        "m_StrikethroughOffset",
        "m_StrikethroughThickness",
        "m_TabWidth",
    )
    new_line_metric_scale_keys = (
        "m_LineHeight",
        "m_AscentLine",
        "m_CapLine",
        "m_MeanLine",
        "m_Baseline",
        "m_DescentLine",
        "m_SuperscriptOffset",
        "m_SubscriptOffset",
        "m_UnderlineOffset",
        "m_UnderlineThickness",
        "m_StrikethroughOffset",
        "m_StrikethroughThickness",
        "m_TabWidth",
    )
    material_padding_scale_keys = (
        "_GradientScale",
        "_FaceDilate",
        "_OutlineWidth",
        "_OutlineSoftness",
        "_UnderlayDilate",
        "_UnderlaySoftness",
        "_UnderlayOffsetX",
        "_UnderlayOffsetY",
        "_GlowOffset",
        "_GlowInner",
        "_GlowOuter",
    )

    def _safe_metric_scale(game_point_size: Any, replacement_point_size: Any) -> float:
        """KR: 게임 pointSize 대비 교체 pointSize 비율을 계산합니다.
        EN: Compute scaling ratio from game pointSize to replacement pointSize.
        """
        try:
            game_ps = float(game_point_size)
            repl_ps = float(replacement_point_size)
            if game_ps > 0 and repl_ps > 0:
                return repl_ps / game_ps
        except Exception:
            pass
        return 1.0

    def _detect_target_texture_swizzle(assets_name: str, path_id: int) -> tuple[str | None, str | None]:
        cache_key = f"{assets_name}|{path_id}"
        if cache_key in texture_swizzle_state_cache:
            return texture_swizzle_state_cache[cache_key]
        texture_obj = texture_object_lookup.get((assets_name, int(path_id)))
        verdict, source = (
            detect_texture_object_ps5_swizzle_detail(texture_obj)
            if texture_obj is not None
            else (None, None)
        )
        texture_swizzle_state_cache[cache_key] = (verdict, source)
        return verdict, source

    def _save_swizzle_preview(
        image: Image.Image,
        assets_name: str,
        atlas_path_id: int,
        font_name: str,
        target_swizzled: bool,
    ) -> None:
        if not (preview and ps5_swizzle and preview_root):
            return
        try:
            visible = _preview_visible_image(image)
            file_dir = sanitize_filename_component(fn_without_path, fallback="assets_file")
            out_dir = os.path.join(preview_root, file_dir)
            os.makedirs(out_dir, exist_ok=True)
            safe_assets = sanitize_filename_component(assets_name, fallback="assets")
            safe_font = sanitize_filename_component(font_name, fallback="font")
            state_label = "target_swizzled" if target_swizzled else "target_linear"
            out_name = f"{safe_assets}__{atlas_path_id}__{safe_font}__unswizzled__{state_label}.png"
            out_path = os.path.join(out_dir, out_name)
            visible.save(out_path, format="PNG")
            if lang == "ko":
                print(f"  Preview 저장: {out_path}")
            else:
                print(f"  Preview saved: {out_path}")
        except Exception as preview_error:
            if lang == "ko":
                print(f"  경고: preview 저장 실패 ({preview_error})")
            else:
                print(f"  Warning: failed to save preview ({preview_error})")

    def _preview_visible_image(image: Image.Image) -> Image.Image:
        """KR: RGBA/LA Atlas를 사람이 보기 쉬운 단일 채널 이미지로 정규화합니다.
        EN: Normalize RGBA/LA atlas into a human-visible single-channel image.
        """
        try:
            if image.mode == "RGBA":
                alpha = image.getchannel("A")
                rgb = image.convert("RGB")
                rgb_bbox = rgb.getbbox()
                alpha_bbox = alpha.getbbox()
                if alpha_bbox and not rgb_bbox:
                    return alpha
                return alpha if alpha_bbox else image.convert("L")
            if image.mode == "LA":
                alpha = image.getchannel("A")
                return alpha if alpha.getbbox() else image.getchannel("L")
            if image.mode == "P":
                return image.convert("L")
            if image.mode not in {"L", "RGB"}:
                return image.convert("L")
            return image
        except Exception:
            return image.convert("L")

    def _load_target_unswizzled_preview_image(
        assets_name: str,
        atlas_path_id: int,
        swizzle_verdict: str | None,
        preview_rotate: int = PS5_SWIZZLE_ROTATE,
    ) -> Image.Image | None:
        """KR: 대상 게임 Atlas(Texture2D)에서 검증용 unswizzle preview 이미지를 생성합니다.
        EN: Build an unswizzled preview image from the target in-game Texture2D atlas.
        """
        texture_obj = texture_object_lookup.get((assets_name, int(atlas_path_id)))
        if texture_obj is None:
            return None
        try:
            texture = texture_obj.parse_as_object()
            width = int(getattr(texture, "m_Width", 0) or 0)
            height = int(getattr(texture, "m_Height", 0) or 0)
            raw_data: bytes | None = None

            get_image_data = getattr(texture, "get_image_data", None)
            if callable(get_image_data):
                try:
                    candidate = get_image_data()
                    if isinstance(candidate, (bytes, bytearray)):
                        raw_data = bytes(candidate)
                except Exception:
                    raw_data = None
            if raw_data is None:
                image_data = getattr(texture, "image_data", None)
                if isinstance(image_data, (bytes, bytearray)):
                    raw_data = bytes(image_data)

            if width > 0 and height > 0 and raw_data:
                total_elements = width * height
                if total_elements > 0 and (len(raw_data) % total_elements) == 0:
                    bpe = len(raw_data) // total_elements
                    if bpe in {1, 2, 3, 4}:
                        processed = raw_data
                        if swizzle_verdict == "likely_swizzled_input":
                            try:
                                processed = ps5_unswizzle_bytes(
                                    raw_data,
                                    width,
                                    height,
                                    bpe,
                                    mask_x=PS5_SWIZZLE_MASK_X,
                                    mask_y=PS5_SWIZZLE_MASK_Y,
                                )
                            except Exception:
                                processed = raw_data
                        mode_map = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}
                        preview_image = Image.frombytes(mode_map[bpe], (width, height), processed)
                        if swizzle_verdict == "likely_swizzled_input" and preview_rotate % 360 != 0 and width == height:
                            preview_image = preview_image.rotate(preview_rotate % 360, expand=False)
                        return preview_image

            image = getattr(texture, "image", None)
            if isinstance(image, Image.Image):
                preview_image = image
                if swizzle_verdict == "likely_swizzled_input":
                    try:
                        preview_image = apply_ps5_unswizzle_to_image(preview_image, rotate=preview_rotate)
                    except Exception:
                        pass
                return preview_image
        except Exception:
            return None
        return None

    def _save_glyph_crop_previews(
        image: Image.Image,
        assets_name: str,
        atlas_path_id: int,
        font_name: str,
        sdf_data: JsonDict,
    ) -> None:
        if not (preview and ps5_swizzle and preview_root):
            return
        glyph_table = sdf_data.get("m_GlyphTable")
        char_table = sdf_data.get("m_CharacterTable")
        if not isinstance(glyph_table, list) or not isinstance(char_table, list):
            return
        try:
            visible = _preview_visible_image(image)
            file_dir = sanitize_filename_component(fn_without_path, fallback="assets_file")
            safe_assets = sanitize_filename_component(assets_name, fallback="assets")
            safe_font = sanitize_filename_component(font_name, fallback="font")
            glyph_dir = os.path.join(
                preview_root,
                file_dir,
                f"{safe_assets}__{atlas_path_id}__{safe_font}",
            )
            os.makedirs(glyph_dir, exist_ok=True)

            glyph_rect_by_index: dict[int, tuple[int, int, int, int]] = {}
            for glyph in glyph_table:
                if not isinstance(glyph, dict):
                    continue
                try:
                    glyph_index = int(glyph.get("m_Index", -1))
                except Exception:
                    continue
                rect_raw = glyph.get("m_GlyphRect", {})
                if not isinstance(rect_raw, dict):
                    continue
                try:
                    gx = int(rect_raw.get("m_X", 0))
                    gy = int(rect_raw.get("m_Y", 0))
                    gw = int(rect_raw.get("m_Width", 0))
                    gh = int(rect_raw.get("m_Height", 0))
                except Exception:
                    continue
                if gw <= 0 or gh <= 0:
                    continue
                glyph_rect_by_index[glyph_index] = (gx, gy, gw, gh)

            if not glyph_rect_by_index:
                return

            flip_preview_y = detect_new_glyph_y_flip(glyph_table, char_table, visible)
            if flip_preview_y:
                if lang == "ko":
                    print("  Glyph preview 좌표 보정: Y-flip 적용")
                else:
                    print("  Glyph preview coordinate fix: applying Y-flip")

            saved = 0
            used_names: set[str] = set()
            for ch in char_table:
                if not isinstance(ch, dict):
                    continue
                try:
                    codepoint = int(ch.get("m_Unicode", -1))
                    glyph_index = int(ch.get("m_GlyphIndex", -1))
                except Exception:
                    continue
                if codepoint < 0:
                    continue
                rect = glyph_rect_by_index.get(glyph_index)
                if rect is None:
                    continue

                x, y, w, h = rect
                if flip_preview_y:
                    y = visible.height - y - h
                x0 = max(0, min(visible.width, x))
                y0 = max(0, min(visible.height, y))
                x1 = max(0, min(visible.width, x + w))
                y1 = max(0, min(visible.height, y + h))
                if x1 <= x0 or y1 <= y0:
                    continue

                base = f"U+{codepoint:04X}"
                try:
                    ch_text = chr(codepoint)
                    if ch_text.isprintable() and not ch_text.isspace():
                        safe_char = sanitize_filename_component(ch_text, fallback="", max_len=8)
                        if safe_char and safe_char != "unnamed":
                            base = f"{base}_{safe_char}"
                except Exception:
                    pass

                name = base
                if name in used_names:
                    name = f"{name}_g{glyph_index}"
                used_names.add(name)
                out_path = os.path.join(glyph_dir, f"{name}.png")
                visible.crop((x0, y0, x1, y1)).save(out_path, format="PNG")
                saved += 1

            if saved > 0:
                if lang == "ko":
                    print(f"  Glyph preview 저장: {saved}개 -> {glyph_dir}")
                else:
                    print(f"  Glyph previews saved: {saved} -> {glyph_dir}")
        except Exception as preview_error:
            if lang == "ko":
                print(f"  경고: glyph preview 저장 실패 ({preview_error})")
            else:
                print(f"  Warning: failed to save glyph previews ({preview_error})")

    def _image_to_alpha8_bytes(image: Image.Image) -> tuple[bytes, int, int]:
        """KR: Pillow 이미지를 Alpha8 raw bytes로 변환합니다.
        EN: Convert Pillow image into Alpha8 raw bytes.
        """
        if image.mode in {"RGBA", "LA"}:
            alpha = image.getchannel("A")
        elif image.mode == "L":
            alpha = image
        else:
            alpha = image.convert("L")
        return alpha.tobytes(), alpha.width, alpha.height

    if replace_sdf:
        for key, value in replacement_lookup.items():
            if len(key) == 4 and key[0] == "SDF" and key[1] == fn_without_path:
                path_id = key[3]
                target_sdf_pathids.add(path_id)
                target_sdf_font_by_pathid.setdefault(path_id, value)
    matched_sdf_targets = 0
    patched_sdf_targets = 0
    sdf_parse_failure_reasons: list[str] = []

    def _close_reader(obj: Any) -> None:
        """KR: UnityPy 내부 reader/객체를 안전하게 dispose합니다.
        EN: Safely dispose UnityPy internal reader/object resources.
        """
        reader = getattr(obj, "reader", None)
        if reader is not None and hasattr(reader, "dispose"):
            try:
                reader.dispose()
            except Exception:
                pass
        if hasattr(obj, "dispose"):
            try:
                obj.dispose()
            except Exception:
                pass

    def _close_env(environment: Any) -> None:
        """KR: Environment에 연결된 파일 리소스를 순회 종료합니다.
        EN: Walk and close file resources attached to environment.
        """
        if not environment:
            return
        stack: list[Any] = []
        files = getattr(environment, "files", None)
        if isinstance(files, dict):
            stack.extend(files.values())
        while stack:
            item = stack.pop()
            _close_reader(item)
            sub_files = getattr(item, "files", None)
            if isinstance(sub_files, dict):
                stack.extend(sub_files.values())

    texture_replacements: dict[str, Any] = {}
    material_replacements: dict[str, JsonDict] = {}
    modified = False

    for obj in env.objects:
        assets_name = obj.assets_file.name
        if obj.type.name == "Font" and replace_ttf:
            font_pathid = obj.path_id
            replacement_font = replacement_lookup.get(("TTF", fn_without_path, assets_name, font_pathid))

            if replacement_font:
                assets = load_font_assets(replacement_font)
                if assets["ttf_data"]:
                    font = obj.parse_as_object()
                    current_ttf_data = bytes(getattr(font, "m_FontData", b""))
                    if current_ttf_data == assets["ttf_data"]:
                        if lang == "ko":
                            print(
                                f"TTF 폰트 동일(건너뜀): {assets_name} | {font.m_Name} | "
                                f"(PathID: {font_pathid} == {replacement_font})"
                            )
                        else:
                            print(
                                f"TTF already same (skip): {assets_name} | {font.m_Name} | "
                                f"(PathID: {font_pathid} == {replacement_font})"
                            )
                        continue
                    if lang == "ko":
                        print(f"TTF 폰트 교체: {assets_name} | {font.m_Name} | (PathID: {font_pathid} -> {replacement_font})")
                    else:
                        print(f"TTF font replaced: {assets_name} | {font.m_Name} | (PathID: {font_pathid} -> {replacement_font})")
                    font.m_FontData = assets["ttf_data"]
                    font.save()
                    modified = True

        if obj.type.name == "MonoBehaviour" and replace_sdf:
            pathid = obj.path_id
            if target_sdf_pathids and pathid not in target_sdf_pathids:
                continue
            try:
                parse_dict = obj.parse_as_dict()
            except Exception as e:
                reason = f"PathID {obj.path_id} parse_as_dict 실패 [{type(e).__name__}]: {e!r}"
                sdf_parse_failure_reasons.append(reason)
                if lang == "ko":
                    print(f"  경고: {reason}")
                    debug_parse_log(f"[replace_fonts] MonoBehaviour parse_as_dict 실패: {fn_without_path} | {reason}")
                else:
                    print(f"  Warning: PathID {obj.path_id} parse_as_dict failed [{type(e).__name__}]: {e!r}")
                    debug_parse_log(f"[replace_fonts] MonoBehaviour parse_as_dict failed: {fn_without_path} | {reason}")
                continue
            has_new_keys = "m_FaceInfo" in parse_dict and "m_AtlasTextures" in parse_dict
            has_old_keys = "m_fontInfo" in parse_dict and "atlas" in parse_dict
            if has_new_keys or has_old_keys:
                target_version = detect_tmp_version(parse_dict)
                is_new_tmp = (target_version == "new")
                is_old_tmp = (target_version == "old")
                # KR: 외부 참조 stub만 제외하고 실제 TMP 폰트만 처리합니다.
                # EN: Skip external stubs and process only concrete TMP font assets.
                if is_new_tmp:
                    atlas_textures = parse_dict.get("m_AtlasTextures", [])
                    glyph_count = len(parse_dict.get("m_GlyphTable", []))
                else:
                    atlas_textures = []
                    glyph_count = len(parse_dict.get("m_glyphInfoList", []))
                if atlas_textures:
                    first_atlas = atlas_textures[0]
                    if first_atlas.get("m_FileID", 0) != 0 and first_atlas.get("m_PathID", 0) == 0:
                        continue
                if glyph_count == 0:
                    continue

                objname = obj.peek_name()
                replacement_font = replacement_lookup.get(("SDF", fn_without_path, assets_name, pathid))
                if replacement_font is None:
                    replacement_font = target_sdf_font_by_pathid.get(pathid)

                preview_target_meta = preview_target_lookup.get((fn_without_path, assets_name, int(pathid)))
                if replacement_font is None and preview_target_meta is not None and preview and ps5_swizzle:
                    atlas_path_id_preview = 0
                    if is_old_tmp:
                        atlas_ref_preview = parse_dict.get("atlas", {})
                        if isinstance(atlas_ref_preview, dict):
                            try:
                                atlas_path_id_preview = int(atlas_ref_preview.get("m_PathID", 0) or 0)
                            except Exception:
                                atlas_path_id_preview = 0
                    else:
                        atlas_textures_preview = parse_dict.get("m_AtlasTextures", [])
                        if isinstance(atlas_textures_preview, list) and atlas_textures_preview:
                            first_preview = atlas_textures_preview[0]
                            if isinstance(first_preview, dict):
                                try:
                                    atlas_path_id_preview = int(first_preview.get("m_PathID", 0) or 0)
                                except Exception:
                                    atlas_path_id_preview = 0

                    if atlas_path_id_preview:
                        target_swizzle_verdict, _ = _detect_target_texture_swizzle(
                            assets_name,
                            int(atlas_path_id_preview),
                        )
                        target_preview_image = _load_target_unswizzled_preview_image(
                            assets_name,
                            int(atlas_path_id_preview),
                            target_swizzle_verdict,
                            preview_rotate=PS5_SWIZZLE_ROTATE,
                        )
                        if isinstance(target_preview_image, Image.Image):
                            _save_swizzle_preview(
                                target_preview_image,
                                assets_name,
                                int(atlas_path_id_preview),
                                str(objname),
                                bool(target_swizzle_verdict == "likely_swizzled_input"),
                            )
                            preview_sdf_data = normalize_sdf_data(parse_dict)
                            _save_glyph_crop_previews(
                                target_preview_image,
                                assets_name,
                                int(atlas_path_id_preview),
                                str(objname),
                                preview_sdf_data,
                            )

                if replacement_font:
                    replacement_meta = replacement_meta_lookup.get(
                        ("SDF", fn_without_path, assets_name, int(pathid)),
                        {},
                    )
                    replacement_process_swizzle = parse_bool_flag(replacement_meta.get("process_swizzle"))
                    replacement_swizzle_hint = parse_bool_flag(replacement_meta.get("swizzle"))
                    matched_sdf_targets += 1
                    assets = load_font_assets(replacement_font)
                    if assets["sdf_data"] and assets["sdf_atlas"]:
                        if lang == "ko":
                            print(f"SDF 폰트 교체: {assets_name} | {objname} | (PathID: {pathid}) -> {replacement_font}")
                        else:
                            print(f"SDF font replaced: {assets_name} | {objname} | (PathID: {pathid}) -> {replacement_font}")
                        source_atlas = assets["sdf_atlas"]
                        source_swizzled = parse_bool_flag(assets.get("sdf_swizzle"))
                        asset_process_swizzle = parse_bool_flag(assets.get("sdf_process_swizzle"))
                        target_swizzle_verdict: str | None = None
                        target_swizzle_source: str | None = None
                        target_is_swizzled: bool | None = None

                        # KR: 입력 JSON이 신형/구형이어도 내부 교체는 신형 TMP 스키마로 통일합니다.
                        # EN: Normalize replacement JSON to the new TMP schema regardless of input format.
                        replace_data = assets.get("sdf_data_normalized")
                        if not isinstance(replace_data, dict):
                            replace_data = normalize_sdf_data(assets["sdf_data"])
                        try:
                            replacement_render_mode = int(replace_data.get("m_AtlasRenderMode", 4118) or 0)
                        except Exception:
                            replacement_render_mode = 4118
                        replacement_is_sdf = (replacement_render_mode & 0x1000) != 0
                        game_padding_for_material = 0.0

                        # KR: GameObject/Script/Material/Atlas 참조는 기존 PathID를 유지해야 런타임 연결이 깨지지 않습니다.
                        # EN: Preserve original GameObject/Script/Material/Atlas references to keep runtime links intact.
                        m_GameObject_FileID = parse_dict["m_GameObject"]["m_FileID"]
                        m_GameObject_PathID = parse_dict["m_GameObject"]["m_PathID"]
                        m_Script_FileID = parse_dict["m_Script"]["m_FileID"]
                        m_Script_PathID = parse_dict["m_Script"]["m_PathID"]

                        if parse_dict.get("m_Material") is not None:
                            m_Material_FileID = parse_dict["m_Material"]["m_FileID"]
                            m_Material_PathID = parse_dict["m_Material"]["m_PathID"]
                        else:
                            m_Material_FileID = parse_dict["material"]["m_FileID"]
                            m_Material_PathID = parse_dict["material"]["m_PathID"]

                        if is_old_tmp:
                            # KR: 대상이 구형 TMP면 교체 데이터도 구형 필드로 역변환해 적용합니다.
                            # EN: For old TMP targets, convert replacement data back to old schema before patching.
                            atlas_ref = parse_dict["atlas"]
                            m_AtlasTextures_FileID = atlas_ref["m_FileID"]
                            m_AtlasTextures_PathID = atlas_ref["m_PathID"]
                            game_font_info = parse_dict.get("m_fontInfo", {})
                            try:
                                game_padding_for_material = float(
                                    game_font_info.get(
                                        "Padding",
                                        parse_dict.get("m_CreationSettings", {}).get("padding", 0),
                                    )
                                )
                            except Exception:
                                game_padding_for_material = 0.0

                            old_font_info = convert_face_info_new_to_old(
                                replace_data["m_FaceInfo"],
                                replace_data.get("m_AtlasPadding", 0),
                                replace_data.get("m_AtlasWidth", 0),
                                replace_data.get("m_AtlasHeight", 0)
                            )
                            if isinstance(game_font_info, dict):
                                if use_game_line_metrics:
                                    metric_scale = 1.0
                                else:
                                    metric_scale = _safe_metric_scale(
                                        game_font_info.get("PointSize", 0),
                                        old_font_info.get("PointSize", 0),
                                    )
                                for metric_key in old_line_metric_keys:
                                    if metric_key in game_font_info:
                                        metric_value = game_font_info[metric_key]
                                        if metric_key in old_line_metric_scale_keys and metric_scale != 1.0:
                                            try:
                                                metric_value = float(metric_value) * metric_scale
                                            except Exception:
                                                pass
                                        old_font_info[metric_key] = metric_value
                            replacement_atlas = assets.get("sdf_atlas")
                            atlas_height = int(
                                replace_data.get(
                                    "m_AtlasHeight",
                                    replacement_atlas.height if replacement_atlas is not None else 0,
                                )
                            )
                            flip_new_glyph_y = detect_new_glyph_y_flip(
                                replace_data.get("m_GlyphTable", []),
                                replace_data.get("m_CharacterTable", []),
                                replacement_atlas if isinstance(replacement_atlas, Image.Image) else None,
                            )
                            if flip_new_glyph_y:
                                if lang == "ko":
                                    print("  구형 TMP 좌표계 보정(Y-flip) 적용")
                                else:
                                    print("  Applying old TMP coordinate fix (Y-flip)")
                            old_glyph_list = convert_glyphs_new_to_old(
                                replace_data.get("m_GlyphTable", []),
                                replace_data.get("m_CharacterTable", []),
                                atlas_height=atlas_height,
                                flip_y=flip_new_glyph_y,
                            )
                            old_font_info["CharacterCount"] = len(old_glyph_list)
                            parse_dict["m_fontInfo"] = old_font_info
                            parse_dict["m_glyphInfoList"] = old_glyph_list

                            if "m_CreationSettings" in parse_dict:
                                cs = parse_dict["m_CreationSettings"]
                                cs["atlasWidth"] = int(replace_data.get("m_AtlasWidth", cs.get("atlasWidth", 0)))
                                cs["atlasHeight"] = int(replace_data.get("m_AtlasHeight", cs.get("atlasHeight", 0)))
                                cs["pointSize"] = int(old_font_info["PointSize"])
                                if not use_game_line_metrics:
                                    cs["padding"] = int(old_font_info["Padding"])
                                cs["characterSequence"] = ""

                        else:
                            # KR: 대상이 신형 TMP면 정규화된 신형 필드를 그대로 적용합니다.
                            # EN: For new TMP targets, apply normalized new-schema fields directly.
                            m_SourceFontFile_FileID = parse_dict["m_SourceFontFile"]["m_FileID"]
                            m_SourceFontFile_PathID = parse_dict["m_SourceFontFile"]["m_PathID"]
                            m_AtlasTextures_FileID = parse_dict["m_AtlasTextures"][0]["m_FileID"]
                            m_AtlasTextures_PathID = parse_dict["m_AtlasTextures"][0]["m_PathID"]
                            game_face_info = parse_dict.get("m_FaceInfo", {})
                            try:
                                game_padding_for_material = float(
                                    parse_dict.get(
                                        "m_AtlasPadding",
                                        parse_dict.get("m_CreationSettings", {}).get("padding", 0),
                                    )
                                )
                            except Exception:
                                game_padding_for_material = 0.0

                            target_face_info = dict(replace_data["m_FaceInfo"])
                            if isinstance(game_face_info, dict):
                                if use_game_line_metrics:
                                    metric_scale = 1.0
                                else:
                                    metric_scale = _safe_metric_scale(
                                        game_face_info.get("m_PointSize", 0),
                                        target_face_info.get("m_PointSize", 0),
                                    )
                                for metric_key in new_line_metric_keys:
                                    if metric_key in game_face_info:
                                        metric_value = game_face_info[metric_key]
                                        if metric_key in new_line_metric_scale_keys and metric_scale != 1.0:
                                            try:
                                                metric_value = float(metric_value) * metric_scale
                                            except Exception:
                                                pass
                                        target_face_info[metric_key] = metric_value
                            ensure_int(target_face_info, ["m_PointSize", "m_AtlasWidth", "m_AtlasHeight"])
                            parse_dict["m_FaceInfo"] = target_face_info
                            parse_dict["m_GlyphTable"] = replace_data["m_GlyphTable"]
                            parse_dict["m_CharacterTable"] = replace_data["m_CharacterTable"]
                            atlas_textures = replace_data.get("m_AtlasTextures", [])
                            if isinstance(atlas_textures, list):
                                parse_dict["m_AtlasTextures"] = [
                                    {
                                        "m_FileID": int(tex.get("m_FileID", 0) or 0),
                                        "m_PathID": int(tex.get("m_PathID", 0) or 0),
                                    }
                                    for tex in atlas_textures
                                    if isinstance(tex, dict)
                                ]
                            else:
                                parse_dict["m_AtlasTextures"] = []
                            if not parse_dict["m_AtlasTextures"]:
                                parse_dict["m_AtlasTextures"] = [{"m_FileID": 0, "m_PathID": 0}]
                            parse_dict["m_AtlasWidth"] = replace_data["m_AtlasWidth"]
                            parse_dict["m_AtlasHeight"] = replace_data["m_AtlasHeight"]
                            parse_dict["m_AtlasPadding"] = replace_data["m_AtlasPadding"]
                            parse_dict["m_AtlasRenderMode"] = replace_data.get("m_AtlasRenderMode", 4118)
                            parse_dict["m_UsedGlyphRects"] = replace_data.get("m_UsedGlyphRects", [])
                            parse_dict["m_FreeGlyphRects"] = replace_data.get("m_FreeGlyphRects", [])
                            parse_dict["m_FontWeightTable"] = replace_data.get("m_FontWeightTable", [])

                            if "m_CreationSettings" in parse_dict:
                                ensure_int(parse_dict["m_CreationSettings"], ["pointSize", "atlasWidth", "atlasHeight", "padding"])

                            # KR: 신형 TMP를 쓰더라도 legacy m_fontInfo가 남아 있으면 동기화해 런타임 차이를 줄입니다.
                            # EN: Keep legacy m_fontInfo in sync when present to reduce runtime schema differences.
                            if "m_fontInfo" in parse_dict and isinstance(parse_dict["m_fontInfo"], dict):
                                parse_dict["m_fontInfo"] = convert_face_info_new_to_old(
                                    parse_dict["m_FaceInfo"],
                                    int(parse_dict.get("m_AtlasPadding", 0)),
                                    int(parse_dict.get("m_AtlasWidth", 0)),
                                    int(parse_dict.get("m_AtlasHeight", 0)),
                                )

                            parse_dict["m_SourceFontFile"]["m_FileID"] = m_SourceFontFile_FileID
                            parse_dict["m_SourceFontFile"]["m_PathID"] = m_SourceFontFile_PathID
                            parse_dict["m_AtlasTextures"][0]["m_FileID"] = m_AtlasTextures_FileID
                            parse_dict["m_AtlasTextures"][0]["m_PathID"] = m_AtlasTextures_PathID
                            if "m_CreationSettings" in parse_dict:
                                # KR: creation settings를 현재 atlas/face 값에 맞춰 동기화합니다.
                                # EN: Align creation settings with the current atlas/face values.
                                parse_dict["m_CreationSettings"]["atlasWidth"] = int(parse_dict.get("m_AtlasWidth", 0))
                                parse_dict["m_CreationSettings"]["atlasHeight"] = int(parse_dict.get("m_AtlasHeight", 0))
                                parse_dict["m_CreationSettings"]["padding"] = int(parse_dict.get("m_AtlasPadding", 0))
                                parse_dict["m_CreationSettings"]["pointSize"] = int(
                                    parse_dict["m_FaceInfo"].get("m_PointSize", parse_dict["m_CreationSettings"].get("pointSize", 0))
                                )
                                parse_dict["m_CreationSettings"]["characterSequence"] = ""

                        # KR: 포맷 분기 후 공통 참조를 원래 값으로 되돌립니다.
                        # EN: Restore shared references to original values after schema-specific patching.
                        parse_dict["m_GameObject"]["m_FileID"] = m_GameObject_FileID
                        parse_dict["m_GameObject"]["m_PathID"] = m_GameObject_PathID
                        parse_dict["m_Script"]["m_FileID"] = m_Script_FileID
                        parse_dict["m_Script"]["m_PathID"] = m_Script_PathID

                        if parse_dict.get("m_Material") is not None:
                            parse_dict["m_Material"]["m_FileID"] = m_Material_FileID
                            parse_dict["m_Material"]["m_PathID"] = m_Material_PathID
                        else:
                            parse_dict["material"]["m_FileID"] = m_Material_FileID
                            parse_dict["material"]["m_PathID"] = m_Material_PathID

                        if is_old_tmp:
                            parse_dict["atlas"]["m_FileID"] = m_AtlasTextures_FileID
                            parse_dict["atlas"]["m_PathID"] = m_AtlasTextures_PathID

                        desired_swizzle_state = source_swizzled
                        if ps5_swizzle:
                            target_swizzle_verdict, target_swizzle_source = _detect_target_texture_swizzle(
                                assets_name,
                                int(m_AtlasTextures_PathID),
                            )
                            if target_swizzle_verdict == "likely_swizzled_input":
                                target_is_swizzled = True
                            elif target_swizzle_verdict == "likely_linear_input":
                                target_is_swizzled = False
                            elif replacement_swizzle_hint:
                                target_is_swizzled = True

                            if target_is_swizzled is not None:
                                desired_swizzle_state = target_is_swizzled
                        if replacement_process_swizzle or asset_process_swizzle:
                            desired_swizzle_state = True

                        if ps5_swizzle:
                            if target_swizzle_verdict == "likely_swizzled_input":
                                if lang == "ko":
                                    reason = f" (근거: {target_swizzle_source})" if target_swizzle_source else ""
                                    print(f"  PS5 swizzle 감지: 대상 Atlas가 swizzled 상태로 판별되었습니다.{reason}")
                                else:
                                    reason = f" (source: {target_swizzle_source})" if target_swizzle_source else ""
                                    print(f"  PS5 swizzle detect: target atlas is likely swizzled.{reason}")
                            elif target_swizzle_verdict == "likely_linear_input":
                                if lang == "ko":
                                    reason = f" (근거: {target_swizzle_source})" if target_swizzle_source else ""
                                    print(f"  PS5 swizzle 감지: 대상 Atlas가 선형(linear) 상태로 판별되었습니다.{reason}")
                                else:
                                    reason = f" (source: {target_swizzle_source})" if target_swizzle_source else ""
                                    print(f"  PS5 swizzle detect: target atlas is likely linear.{reason}")
                            elif replacement_swizzle_hint:
                                if lang == "ko":
                                    print("  PS5 swizzle 힌트: JSON swizzle=yes 값을 기준으로 swizzle 적용합니다.")
                                else:
                                    print("  PS5 swizzle hint: applying swizzle based on JSON swizzle=yes.")
                            elif lang == "ko":
                                print("  PS5 swizzle 감지: inconclusive, 교체 Atlas 원본 상태를 유지합니다.")
                            else:
                                print("  PS5 swizzle detect: inconclusive, keeping replacement atlas state.")
                        elif replacement_process_swizzle:
                            if lang == "ko":
                                print("  process_swizzle=True: 교체 Atlas를 swizzle 상태로 변환합니다.")
                            else:
                                print("  process_swizzle=True: converting replacement atlas to swizzled state.")

                        atlas_for_write = source_atlas
                        if desired_swizzle_state != source_swizzled:
                            try:
                                if desired_swizzle_state:
                                    atlas_for_write = apply_ps5_swizzle_to_image(source_atlas)
                                else:
                                    atlas_for_write = apply_ps5_unswizzle_to_image(source_atlas)
                            except Exception as swizzle_error:
                                atlas_for_write = source_atlas
                                if lang == "ko":
                                    print(f"  경고: PS5 swizzle 변환 실패, 원본 Atlas를 사용합니다. ({swizzle_error})")
                                else:
                                    print(f"  Warning: PS5 swizzle transform failed; using original atlas. ({swizzle_error})")

                        if preview and ps5_swizzle:
                            preview_image = atlas_for_write
                            if desired_swizzle_state:
                                try:
                                    preview_image = apply_ps5_unswizzle_to_image(atlas_for_write)
                                except Exception as preview_unswizzle_error:
                                    preview_image = atlas_for_write
                                    if lang == "ko":
                                        print(
                                            "  경고: preview unswizzle 실패, 저장 상태 Atlas 그대로 미리보기를 저장합니다. "
                                            f"({preview_unswizzle_error})"
                                        )
                                    else:
                                        print(
                                            "  Warning: preview unswizzle failed; saving preview from stored atlas state. "
                                            f"({preview_unswizzle_error})"
                                        )
                            _save_swizzle_preview(
                                preview_image,
                                assets_name,
                                int(m_AtlasTextures_PathID),
                                str(objname),
                                bool(desired_swizzle_state),
                            )
                            if isinstance(replace_data, dict):
                                _save_glyph_crop_previews(
                                    preview_image,
                                    assets_name,
                                    int(m_AtlasTextures_PathID),
                                    str(objname),
                                    replace_data,
                                )

                        texture_replacements[f"{assets_name}|{m_AtlasTextures_PathID}"] = atlas_for_write
                        if m_Material_FileID == 0 and m_Material_PathID != 0:
                            gradient_scale = None
                            apply_replacement_material = not use_game_mat
                            float_overrides: dict[str, float] = {}
                            material_padding_ratio = 1.0
                            material_data = assets.get("sdf_materials")
                            if (not replacement_is_sdf) and use_game_mat:
                                if lang == "ko":
                                    print("  경고: Raster 폰트에 --use-game-material 사용 시 박스 아티팩트가 생길 수 있습니다.")
                                else:
                                    print("  Warning: using --use-game-material with Raster fonts may cause box artifacts.")
                            try:
                                replacement_padding = float(replace_data.get("m_AtlasPadding", 0))
                            except Exception:
                                replacement_padding = 0.0
                            if (
                                replacement_is_sdf
                                and
                                material_scale_by_padding
                                and game_padding_for_material > 0
                                and replacement_padding > 0
                            ):
                                material_padding_ratio = game_padding_for_material / replacement_padding
                                if material_padding_ratio <= 0:
                                    material_padding_ratio = 1.0
                            if material_data and apply_replacement_material:
                                material_props = material_data.get("m_SavedProperties", {})
                                float_properties = material_props.get("m_Floats", [])
                                for prop in float_properties:
                                    if not isinstance(prop, (list, tuple)) or len(prop) < 2:
                                        continue
                                    key = str(prop[0])
                                    try:
                                        value = float(prop[1])
                                    except (TypeError, ValueError):
                                        continue
                                    float_overrides[key] = value
                                if material_padding_ratio != 1.0:
                                    for key in material_padding_scale_keys:
                                        if key in float_overrides:
                                            float_overrides[key] = float(float_overrides[key] * material_padding_ratio)
                                gradient_scale = float_overrides.get("_GradientScale")
                            if apply_replacement_material and not replacement_is_sdf:
                                # KR: Raster atlas를 SDF 머티리얼로 렌더링할 때 박스 아티팩트를 줄이기 위해
                                # KR: dilate/outline/underlay/glow 계열을 0으로 리셋합니다.
                                # EN: Reduce box artifacts when raster atlases are sampled by SDF materials by
                                # EN: resetting dilate/outline/underlay/glow-like params to 0.
                                float_overrides["_GradientScale"] = 1.0
                                for key in material_padding_scale_keys:
                                    if key == "_GradientScale":
                                        continue
                                    float_overrides[key] = 0.0
                                gradient_scale = 1.0
                                if lang == "ko":
                                    print("  Raster 모드 감지: Material SDF 효과값을 0으로 보정합니다.")
                                else:
                                    print("  Raster mode detected: neutralizing SDF material effect floats.")
                            if material_scale_by_padding and apply_replacement_material and material_padding_ratio != 1.0:
                                if lang == "ko":
                                    print(
                                        f"  Material padding 비율 보정 적용: {game_padding_for_material:.2f}/{replacement_padding:.2f} "
                                        f"(x{material_padding_ratio:.3f})"
                                    )
                                else:
                                    print(
                                        f"  Applied material padding ratio: {game_padding_for_material:.2f}/{replacement_padding:.2f} "
                                        f"(x{material_padding_ratio:.3f})"
                                    )
                            material_replacements[f"{assets_name}|{m_Material_PathID}"] = {
                                "w": atlas_for_write.width,
                                "h": atlas_for_write.height,
                                "gs": gradient_scale,
                                "float_overrides": float_overrides,
                            }
                        obj.patch(parse_dict)
                        patched_sdf_targets += 1
                        modified = True
                    else:
                        missing_parts: list[str] = []
                        if assets.get("sdf_data") is None:
                            missing_parts.append("json")
                        if assets.get("sdf_atlas") is None:
                            missing_parts.append("atlas")
                        if lang == "ko":
                            print(
                                f"  경고: 교체 리소스 누락으로 SDF 적용 건너뜀: {replacement_font} "
                                f"(누락: {', '.join(missing_parts) if missing_parts else 'unknown'})"
                            )
                        else:
                            print(
                                f"  Warning: skipping SDF patch due to missing replacement assets: {replacement_font} "
                                f"(missing: {', '.join(missing_parts) if missing_parts else 'unknown'})"
                            )

    for obj in env.objects:
        assets_name = obj.assets_file.name
        if obj.type.name == "Texture2D":
            if f"{assets_name}|{obj.path_id}" in texture_replacements:
                parse_dict = obj.parse_as_object()
                if lang == "ko":
                    print(f"텍스처 교체: {obj.peek_name()} (PathID: {obj.path_id})")
                else:
                    print(f"Texture replaced: {obj.peek_name()} (PathID: {obj.path_id})")
                replacement_image = texture_replacements[f"{assets_name}|{obj.path_id}"]
                applied_raw_alpha8 = False
                try:
                    texture_format = int(getattr(parse_dict, "m_TextureFormat", -1) or -1)
                except Exception:
                    texture_format = -1
                if ps5_swizzle and texture_format == 1 and isinstance(replacement_image, Image.Image):
                    try:
                        alpha_raw, aw, ah = _image_to_alpha8_bytes(replacement_image)
                        parse_dict.m_Width = int(aw)
                        parse_dict.m_Height = int(ah)
                        if hasattr(parse_dict, "m_CompleteImageSize"):
                            parse_dict.m_CompleteImageSize = int(len(alpha_raw))
                        parse_dict.image_data = alpha_raw
                        stream_data = getattr(parse_dict, "m_StreamData", None)
                        if stream_data is not None:
                            try:
                                stream_data.offset = 0
                                stream_data.size = 0
                                stream_data.path = ""
                            except Exception:
                                pass
                        applied_raw_alpha8 = True
                        if lang == "ko":
                            print("  Alpha8 raw 주입 적용: swizzle 바이트를 image_data에 직접 기록합니다.")
                        else:
                            print("  Applied Alpha8 raw injection: writing swizzled bytes directly to image_data.")
                    except Exception as raw_inject_error:
                        if lang == "ko":
                            print(f"  경고: Alpha8 raw 주입 실패, 일반 image 저장으로 폴백합니다. ({raw_inject_error})")
                        else:
                            print(f"  Warning: Alpha8 raw injection failed; falling back to image save. ({raw_inject_error})")
                if not applied_raw_alpha8:
                    parse_dict.image = replacement_image
                parse_dict.save()
                modified = True
        if obj.type.name == "Material":
            if f"{assets_name}|{obj.path_id}" in material_replacements:
                parse_dict = obj.parse_as_object()

                mat_info = material_replacements[f"{assets_name}|{obj.path_id}"]
                float_overrides = mat_info.get("float_overrides", {})
                for i in range(len(parse_dict.m_SavedProperties.m_Floats)):
                    prop_name = parse_dict.m_SavedProperties.m_Floats[i][0]
                    if prop_name in float_overrides:
                        parse_dict.m_SavedProperties.m_Floats[i] = (prop_name, float(float_overrides[prop_name]))
                    elif prop_name == '_TextureHeight':
                        parse_dict.m_SavedProperties.m_Floats[i] = ('_TextureHeight', float(mat_info["h"]))
                    elif prop_name == '_TextureWidth':
                        parse_dict.m_SavedProperties.m_Floats[i] = ('_TextureWidth', float(mat_info["w"]))
                    elif prop_name == '_GradientScale' and mat_info["gs"] is not None:
                        parse_dict.m_SavedProperties.m_Floats[i] = ('_GradientScale', float(mat_info["gs"]))
                parse_dict.save()

    if modified:
        if lang == "ko":
            print(f"'{fn_without_path}' 저장 중...")
        else:
            print(f"Saving '{fn_without_path}'...")

        save_success = False
        last_save_failure_reason: str | None = None

        def _save_env_file(
            packer: Any = None,
            save_path: str | None = None,
            use_save_to: bool = False,
        ) -> bytes | int:
            """KR: 지정 packer로 기본 파일 객체의 save/save_to를 호출합니다.
            KR: save_path가 주어지면 save_to()로 파일에 직접 기록하여 메모리를 절약합니다.
            KR: 반환값은 bytes(legacy) 또는 저장된 파일 크기(int)입니다.
            EN: Call save/save_to on the primary file object with an optional packer.
            EN: If save_path is provided, it writes via save_to() to reduce memory usage.
            EN: Returns bytes (legacy path) or written file size as int (save_to path).
            """
            # KR: use_save_to=True 이고 save_to()가 존재하면 파일에 직접 저장합니다.
            # EN: When use_save_to=True and save_to() exists, save directly to file.
            save_to_fn = getattr(env_file, "save_to", None)
            if use_save_to and save_path and callable(save_to_fn):
                try:
                    supports_packer = "packer" in inspect.signature(save_to_fn).parameters
                except (TypeError, ValueError):
                    supports_packer = False
                if packer is None or not supports_packer:
                    return save_to_fn(save_path)
                return save_to_fn(save_path, packer=packer)

            # KR: 기존 bytes 반환 방식 폴백
            # EN: Fallback to legacy bytes-returning save()
            save_fn = getattr(env_file, "save", None)
            if not callable(save_fn):
                raise AttributeError("UnityPy environment file object has no callable save().")
            typed_save = cast(Callable[..., bytes], save_fn)
            # KR: save() 시그니처를 기준으로 packer 지원 여부를 판별해 내부 TypeError를 가리지 않도록 합니다.
            # EN: Detect packer support from save() signature so we don't swallow internal TypeError.
            try:
                supports_packer = "packer" in inspect.signature(typed_save).parameters
            except (TypeError, ValueError):
                supports_packer = False

            if packer is None or not supports_packer:
                return typed_save()
            return typed_save(packer=packer)

        def _validate_saved_file(saved_path: str) -> tuple[bool, str | None]:
            """KR: 저장 결과 파일이 Unity bundle로 다시 열리는지 검증합니다.
            EN: Validate saved output by attempting to reload from file path.
            """
            signature = source_bundle_signature or getattr(env_file, "signature", None)
            if signature not in bundle_signatures:
                return True, None
            saved_signature = _read_bundle_signature(saved_path)
            if saved_signature != signature:
                reason = (
                    f"번들 시그니처 불일치 (기대: {signature}, 결과: {saved_signature or 'None'})"
                    if lang == "ko"
                    else f"bundle signature mismatch (expected: {signature}, got: {saved_signature or 'None'})"
                )
                if lang == "ko":
                    print(f"  저장 검증 실패: {reason}")
                else:
                    print(f"  Save validation failed: {reason}")
                return False, reason
            try:
                if getattr(sys, "frozen", False):
                    cmd = [sys.executable, "--_validate-bundle", saved_path]
                else:
                    cmd = [sys.executable, os.path.abspath(__file__), "--_validate-bundle", saved_path]
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=1800,
                )
                if proc.returncode == 0:
                    return True, None
                detail = (proc.stderr or proc.stdout or "").strip()
                reason = (
                    f"worker exit={proc.returncode}: {detail}"
                    if detail
                    else f"worker exit={proc.returncode}"
                )
                if lang == "ko":
                    print(f"  저장 검증 실패 [{reason}]")
                else:
                    print(f"  Save validation failed [{reason}]")
                return False, reason
            except Exception as e:
                reason = f"검증 워커 실행 실패: {e!r}" if lang == "ko" else f"failed to run validation worker: {e!r}"
                if lang == "ko":
                    print(f"  저장 검증 워커 실행 실패: {e!r}")
                else:
                    print(f"  Failed to run save validation worker: {e!r}")
                return False, reason

        def _try_save(packer_label: Any, log_label: str) -> bool:
            """KR: 단일 저장 전략을 시도하고 성공 여부를 반환합니다.
            EN: Try one save strategy and return success status.
            """
            nonlocal save_success, last_save_failure_reason
            tmp_file = os.path.join(tmp_path, fn_without_path)
            has_save_to = callable(getattr(env_file, "save_to", None))
            saved_blob: bytes | None = None
            try:
                use_stream_fallback = False
                if has_save_to and source_bundle_signature in bundle_signatures:
                    # KR: 번들은 안정성을 위해 legacy save()를 우선 시도하고, 메모리 부족 시에만 save_to로 폴백합니다.
                    # EN: For bundles, prefer legacy save() for stability; fall back to save_to on MemoryError.
                    try:
                        saved_blob = _save_env_file(packer_label, use_save_to=False)
                    except MemoryError:
                        use_stream_fallback = True
                        if lang == "ko":
                            print("  메모리 부족으로 스트리밍 저장(save_to)으로 폴백합니다...")
                        else:
                            print("  Falling back to streaming save_to due to MemoryError...")

                    if not use_stream_fallback:
                        with open(tmp_file, "wb") as f:
                            f.write(cast(bytes, saved_blob))
                        saved_blob = None
                    else:
                        _save_env_file(packer_label, save_path=tmp_file, use_save_to=True)
                elif has_save_to:
                    # KR: save_to()로 파일에 직접 저장 — bytes 중간 변수 없음 (메모리 절약)
                    # EN: save_to() writes directly to file — no intermediate bytes blob (memory-efficient)
                    _save_env_file(packer_label, save_path=tmp_file, use_save_to=True)
                else:
                    # KR: 기존 bytes 반환 방식 폴백
                    # EN: Legacy bytes-returning fallback
                    saved_blob = _save_env_file(packer_label, use_save_to=False)
                    with open(tmp_file, "wb") as f:
                        f.write(cast(bytes, saved_blob))
                    # Release large in-memory blob before optional validation to lower peak memory.
                    saved_blob = None
                gc.collect()
                is_valid, validation_reason = _validate_saved_file(tmp_file)
                if not is_valid:
                    try:
                        saved_size = os.path.getsize(tmp_file)
                    except Exception:
                        saved_size = 0
                    if saved_size > 0:
                        if lang == "ko":
                            print("  경고: 저장 검증에 실패했지만 무검증 저장으로 계속 진행합니다.")
                            if validation_reason:
                                print(f"  검증 실패 원인: {validation_reason}")
                        else:
                            print("  Warning: save validation failed, continuing with unvalidated save.")
                            if validation_reason:
                                print(f"  Validation failure reason: {validation_reason}")
                        save_success = True
                        return True
                    last_save_failure_reason = validation_reason or "validation failed (empty output file)"
                    try:
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                    except Exception:
                        pass
                    return False
                save_success = True
                return True
            except Exception as e:
                last_save_failure_reason = f"method {log_label} [{type(e).__name__}]: {e!r}"
                if lang == "ko":
                    print(f"  저장 방법 {log_label} 실패 [{type(e).__name__}]: {e!r}")
                else:
                    print(f"  Save method {log_label} failed [{type(e).__name__}]: {e!r}")
                if debug_parse_enabled():
                    tb_module.print_exc()
                try:
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                except Exception:
                    pass
                return False
            finally:
                saved_blob = None
                gc.collect()

        dataflags = getattr(env_file, "dataflags", None)
        safe_none_packer = (int(dataflags), 0) if dataflags is not None else "none"
        legacy_none_packer = ((int(dataflags) & ~0x3F), 0) if dataflags is not None else None

        if prefer_original_compress:
            # KR: 옵션이 있으면 원본 압축 우선으로 저장합니다.
            # EN: With option enabled, keep original compression as first choice.
            if not _try_save("original", "1"):
                if lang == "ko":
                    print("  lz4 압축 모드로 재시도...")
                else:
                    print("  Retrying with lz4 packer...")
                if not _try_save("lz4", "2"):
                    if lang == "ko":
                        print("  비압축 계열 모드로 재시도...")
                    else:
                        print("  Retrying with uncompressed-style packer...")
                    if not _try_save(safe_none_packer, "3") and legacy_none_packer is not None:
                        if lang == "ko":
                            print("  레거시 비트마스크 모드로 재시도...")
                        else:
                            print("  Retrying with legacy bitmask packer...")
                        _try_save(legacy_none_packer, "4")
        else:
            # KR: 기본은 무압축 계열 우선으로 저장해 시간을 줄이고, 실패 시 압축 모드로 폴백합니다.
            # EN: Default prefers uncompressed-family save for speed, then falls back to compressed modes.
            if not _try_save(safe_none_packer, "1"):
                if legacy_none_packer is not None:
                    if lang == "ko":
                        print("  레거시 비트마스크 무압축 모드로 재시도...")
                    else:
                        print("  Retrying with legacy bitmask uncompressed packer...")
                    if _try_save(legacy_none_packer, "2"):
                        pass
                    else:
                        if lang == "ko":
                            print("  원본 압축 모드로 재시도...")
                        else:
                            print("  Retrying with original compression...")
                        if not _try_save("original", "3"):
                            if lang == "ko":
                                print("  lz4 압축 모드로 재시도...")
                            else:
                                print("  Retrying with lz4 packer...")
                            _try_save("lz4", "4")
                else:
                    if lang == "ko":
                        print("  원본 압축 모드로 재시도...")
                    else:
                        print("  Retrying with original compression...")
                    if not _try_save("original", "2"):
                        if lang == "ko":
                            print("  lz4 압축 모드로 재시도...")
                        else:
                            print("  Retrying with lz4 packer...")
                        _try_save("lz4", "3")

        _close_env(env)
        gc.collect()

        if save_success:
            saved_file_path = os.path.join(tmp_path, fn_without_path)
            if os.path.exists(saved_file_path):
                saved_size = os.path.getsize(saved_file_path)
                shutil.move(saved_file_path, assets_file)
                if lang == "ko":
                    print(f"  저장 완료 (크기: {saved_size} bytes)")
                else:
                    print(f"  Save complete (size: {saved_size} bytes)")
            else:
                if lang == "ko":
                    print("  경고: 저장된 파일을 찾을 수 없습니다")
                else:
                    print("  Warning: saved file was not found")
                last_save_failure_reason = "saved file was not found after save phase"
                save_success = False

        if not save_success:
            if lang == "ko":
                print("  오류: 파일 저장에 실패했습니다.")
                if last_save_failure_reason:
                    print(f"  실패 원인: {last_save_failure_reason}")
            else:
                print("  Error: failed to save file.")
                if last_save_failure_reason:
                    print(f"  Failure reason: {last_save_failure_reason}")
    elif replace_sdf and target_sdf_pathids:
        if lang == "ko":
            print(
                f"  경고: SDF 대상 {len(target_sdf_pathids)}건 중 매칭 {matched_sdf_targets}건, 적용 {patched_sdf_targets}건"
            )
            if sdf_parse_failure_reasons:
                print(f"  파싱 오류: {sdf_parse_failure_reasons[-1]}")
        else:
            print(
                f"  Warning: SDF targets={len(target_sdf_pathids)}, matched={matched_sdf_targets}, patched={patched_sdf_targets}"
            )
            if sdf_parse_failure_reasons:
                print(f"  Parse error: {sdf_parse_failure_reasons[-1]}")

    if os.path.exists(tmp_path):
        shutil.rmtree(tmp_path)
    if not using_custom_temp_root and os.path.isdir(tmp_root):
        try:
            os.rmdir(tmp_root)
        except OSError:
            pass

    return save_success if modified else False


def create_batch_replacements(
    game_path: str,
    font_name: str,
    replace_ttf: bool = True,
    replace_sdf: bool = True,
    target_files: set[str] | None = None,
    scan_jobs: int = 1,
    lang: Language = "ko",
    ps5_swizzle: bool = False,
) -> dict[str, JsonDict]:
    """KR: 게임 내 모든 폰트를 지정 폰트로 치환하는 배치 매핑을 생성합니다.
    KR: target_files가 있으면 해당 파일만 대상으로 매핑을 생성합니다.
    EN: Create batch replacement mapping for all fonts in a game.
    EN: If target_files is provided, build mapping only for those files.
    """
    fonts = scan_fonts(
        game_path,
        lang=lang,
        target_files=target_files,
        scan_jobs=scan_jobs,
        ps5_swizzle=ps5_swizzle,
    )
    replacements: dict[str, JsonDict] = {}

    if replace_ttf:
        for font in fonts["ttf"]:
            key = f"{font['file']}|TTF|{font['path_id']}"
            replacements[key] = {
                "Name": font["name"],
                "assets_name": font["assets_name"],
                "Path_ID": font["path_id"],
                "Type": "TTF",
                "File": font["file"],
                "Replace_to": font_name
            }

    if replace_sdf:
        for font in fonts["sdf"]:
            key = f"{font['file']}|SDF|{font['path_id']}"
            if ps5_swizzle:
                swizzle_flag = "True" if parse_bool_flag(font.get("swizzle")) else "False"
                process_swizzle_flag = (
                    "True" if parse_bool_flag(font.get("process_swizzle")) else "False"
                )
                entry: JsonDict = {
                    "File": font["file"],
                    "assets_name": font["assets_name"],
                    "Path_ID": font["path_id"],
                    "Type": "SDF",
                    "Name": font["name"],
                    "swizzle": swizzle_flag,
                    "process_swizzle": process_swizzle_flag,
                    "Replace_to": font_name,
                }
            else:
                entry = {
                    "File": font["file"],
                    "assets_name": font["assets_name"],
                    "Path_ID": font["path_id"],
                    "Type": "SDF",
                    "Name": font["name"],
                    "Replace_to": font_name,
                }
            replacements[key] = entry

    return replacements


def exit_with_error(message: str, lang: Language = "ko") -> NoReturn:
    """KR: 로컬라이즈된 오류 메시지를 출력하고 종료합니다.
    EN: Print localized error message and terminate the process.
    """
    if lang == "ko":
        print(f"오류: {message}")
    else:
        print(f"Error: {message}")
    if lang == "ko":
        input("\n엔터를 눌러 종료...")
    else:
        input("\nPress Enter to exit...")
    sys.exit(1)


def exit_with_error_en(message: str) -> NoReturn:
    """KR: 영문 오류 메시지를 출력하고 종료합니다.
    EN: Print English error message and terminate the process.
    """
    exit_with_error(message, lang="en")


def run_validation_worker(bundle_path: str, lang: Language = "ko") -> int:
    """KR: 저장 검증 전용 워커입니다. bundle_path를 UnityPy로 로드해 성공/실패 코드만 반환합니다.
    EN: Validation worker that loads bundle_path with UnityPy and returns a status code.
    """
    try:
        if not os.path.exists(bundle_path):
            if lang == "ko":
                print("[validate] 검증 실패: 저장 파일이 존재하지 않습니다.")
            else:
                print("[validate] Validation failed: saved file does not exist.")
            return 2

        env = UnityPy.load(bundle_path)
        files = getattr(env, "files", None)
        if not isinstance(files, dict) or len(files) == 0:
            if lang == "ko":
                print("[validate] 검증 실패: UnityPy.load 결과에 파일이 없습니다.")
            else:
                print("[validate] Validation failed: UnityPy.load returned no files.")
            return 2

        # KR: 실제 오브젝트가 없으면 저장 결과가 비정상일 가능성이 높습니다.
        # EN: Empty object list usually indicates an invalid or incomplete save result.
        if not getattr(env, "objects", None):
            if lang == "ko":
                print("[validate] 검증 실패: 로드된 오브젝트가 없습니다.")
            else:
                print("[validate] Validation failed: loaded object list is empty.")
            return 2

        return 0
    except Exception as e:
        if lang == "ko":
            print(f"[validate] 검증 실패: {e!r}")
        else:
            print(f"[validate] Validation failed: {e!r}")
        if debug_parse_enabled():
            tb_module.print_exc()
        return 2


def run_scan_file_worker(
    game_path: str,
    assets_file: str,
    output_path: str,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> int:
    """KR: 단일 파일 파싱 워커입니다. 결과를 JSON 파일로 저장합니다.
    EN: Single-file scan worker. Writes results to a JSON file.
    """
    try:
        game_path, data_path = resolve_game_path(game_path, lang=lang)
        unity_version = get_unity_version(game_path, lang=lang)
        compile_method = get_compile_method(data_path)
        generator = _create_generator(unity_version, game_path, data_path, compile_method, lang=lang)
        scanned, load_error = _scan_fonts_in_asset_file(
            assets_file,
            generator,
            lang=lang,
            detect_ps5_swizzle=detect_ps5_swizzle,
        )
        payload: JsonDict = {
            "ttf": scanned.get("ttf", []),
            "sdf": scanned.get("sdf", []),
            "error": load_error,
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return 0
    except Exception as e:
        if lang == "ko":
            print(f"[scan_worker] 실패: {e!r}")
        else:
            print(f"[scan_worker] failed: {e!r}")
        if debug_parse_enabled():
            tb_module.print_exc()
        return 2


def main_cli(lang: Language = "ko") -> None:
    """KR: 언어별 공통 CLI 진입점입니다.
    EN: Shared CLI entrypoint parameterized by language.
    """
    is_ko = lang == "ko"

    if is_ko:
        description = "Unity 게임의 폰트를 한글 폰트로 교체합니다."
        epilog = """
예시:
  %(prog)s --gamepath "D:\\Games\\Muck" --parse
  %(prog)s --gamepath "D:\\Games\\Muck" --mulmaru
  %(prog)s --gamepath "D:\\Games\\Muck" --nanumgothic --sdfonly
  %(prog)s --gamepath "D:\\Games\\Muck" --list Muck.json
        """
        gamepath_help = "게임의 루트 경로 (예: D:\\Games\\Muck)"
        parse_help = "폰트 정보를 JSON으로 출력"
        mulmaru_help = "모든 폰트를 Mulmaru로 일괄 교체"
        nanum_help = "모든 폰트를 NanumGothic으로 일괄 교체"
        sdf_help = "SDF 폰트만 교체"
        ttf_help = "TTF 폰트만 교체"
        list_help = "JSON 파일을 읽어서 폰트 교체"
        target_file_help = "지정한 파일명만 교체 대상에 포함 (여러 번 사용 가능)"
        game_mat_help = "SDF 교체 시 게임 원본 Material 파라미터를 유지 (기본: 교체 Material 보정 적용)"
        game_line_metrics_help = "SDF 교체 시 게임 원본 줄 간격 메트릭 사용 (기본: 교체 폰트 메트릭 보정 적용)"
        original_compress_help = "저장 시 원본 압축 모드를 우선 사용 (기본: 무압축 계열 우선)"
        temp_dir_help = "임시 저장 폴더 루트 경로 (가능하면 빠른 SSD/NVMe 권장)"
        output_only_help = "원본 파일은 유지하고, 수정된 파일만 지정 폴더에 원본 상대 경로로 저장"
        preview_help = "--ps5-swizzle와 함께 사용 시 unswizzle 미리보기 PNG를 preview 폴더에 저장"
        scan_jobs_help = "폰트 스캔 병렬 워커 수 (기본: 1, parse/일괄교체 스캔에 적용)"
        split_save_force_help = "대형 SDF 다건 교체에서 one-shot을 건너뛰고 SDF 1개씩 강제 분할 저장"
        oneshot_save_force_help = "대형 SDF 다건 교체에서도 분할 저장 폴백 없이 one-shot 저장만 시도"
        ps5_swizzle_help = "PS5 swizzle 자동 판별/변환 모드 (mask_x=0x385F0, mask_y=0x07A0F, rotate=90 보정)"
        verbose_help = "모든 로그를 verbose.txt 파일로 저장"
    else:
        description = "Replace Unity game fonts with Korean fonts."
        epilog = """
Examples:
  %(prog)s --gamepath "D:\\Games\\Muck" --parse
  %(prog)s --gamepath "D:\\Games\\Muck" --mulmaru
  %(prog)s --gamepath "D:\\Games\\Muck" --nanumgothic --sdfonly
  %(prog)s --gamepath "D:\\Games\\Muck" --list Muck.json
        """
        gamepath_help = "Game root path (e.g. D:\\Games\\Muck)"
        parse_help = "Export font info to JSON"
        mulmaru_help = "Replace all fonts with Mulmaru"
        nanum_help = "Replace all fonts with NanumGothic"
        sdf_help = "Replace SDF fonts only"
        ttf_help = "Replace TTF fonts only"
        list_help = "Replace fonts using a JSON file"
        target_file_help = "Limit replacement targets to specific file name(s) (repeatable)"
        game_mat_help = "Use original in-game Material parameters for SDF replacement (default: adjusted replacement material)"
        game_line_metrics_help = "Use original in-game line metrics for SDF replacement (default: adjusted replacement font metrics)"
        original_compress_help = "Prefer original compression mode on save (default: uncompressed-family first)"
        temp_dir_help = "Root path for temporary save files (fast SSD/NVMe recommended)"
        output_only_help = "Keep originals untouched and write modified files only to this folder (preserve relative paths)"
        preview_help = "With --ps5-swizzle, save unswizzled preview PNGs into preview folder"
        scan_jobs_help = "Number of parallel scan workers (default: 1, used for parse/bulk scan paths)"
        split_save_force_help = "Skip one-shot and force one-by-one SDF split save for large multi-SDF replacements"
        oneshot_save_force_help = "Force one-shot save even for large multi-SDF targets (disable split-save fallback)"
        ps5_swizzle_help = "Enable PS5 swizzle detect/transform mode (mask_x=0x385F0, mask_y=0x07A0F, rotate=90 compensation)"
        verbose_help = "Save all logs to verbose.txt"

    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    parser.add_argument("--gamepath", type=str, help=gamepath_help)
    parser.add_argument("--parse", action="store_true", help=parse_help)
    parser.add_argument("--mulmaru", action="store_true", help=mulmaru_help)
    parser.add_argument("--nanumgothic", action="store_true", help=nanum_help)
    parser.add_argument("--sdfonly", action="store_true", help=sdf_help)
    parser.add_argument("--ttfonly", action="store_true", help=ttf_help)
    parser.add_argument("--list", type=str, metavar="JSON_FILE", help=list_help)
    parser.add_argument("--target-file", action="append", metavar="FILE_NAME", help=target_file_help)
    parser.add_argument("--use-game-material", action="store_true", help=game_mat_help)
    parser.add_argument("--use-game-mat", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--use-game-line-metrics", action="store_true", help=game_line_metrics_help)
    parser.add_argument("--use-game-line-matrics", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--material-scale-by-padding", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--original-compress", action="store_true", help=original_compress_help)
    parser.add_argument("--temp-dir", type=str, metavar="PATH", help=temp_dir_help)
    parser.add_argument("--output-only", type=str, metavar="PATH", help=output_only_help)
    parser.add_argument("--preview", action="store_true", help=preview_help)
    parser.add_argument("--scan-jobs", type=int, default=1, metavar="N", help=scan_jobs_help)
    parser.add_argument("--split-save-force", action="store_true", help=split_save_force_help)
    parser.add_argument("--oneshot-save-force", action="store_true", help=oneshot_save_force_help)
    parser.add_argument("--ps5-swizzle", action="store_true", help=ps5_swizzle_help)
    parser.add_argument("--verbose", action="store_true", help=verbose_help)
    parser.add_argument("--_validate-bundle", type=str, metavar="BUNDLE_PATH", help=argparse.SUPPRESS)
    parser.add_argument("--_scan-file-worker", type=str, metavar="ASSET_FILE_PATH", help=argparse.SUPPRESS)
    parser.add_argument("--_scan-file-worker-output", type=str, metavar="OUTPUT_JSON_PATH", help=argparse.SUPPRESS)

    args = parser.parse_args()
    if isinstance(args.gamepath, str):
        args.gamepath = strip_wrapping_quotes_repeated(args.gamepath)
    if isinstance(args.list, str):
        args.list = strip_wrapping_quotes_repeated(args.list)
    if isinstance(args.output_only, str):
        args.output_only = strip_wrapping_quotes_repeated(args.output_only)

    # KR: 이전 옵션(--use-game-mat) 호환을 위해 새 옵션에 병합합니다.
    # EN: Merge legacy flag (--use-game-mat) into the new option for compatibility.
    args.use_game_material = bool(getattr(args, "use_game_material", False) or getattr(args, "use_game_mat", False))
    # KR: 오타/레거시 옵션(--use-game-line-matrics)도 동일 동작으로 병합합니다.
    # EN: Merge typo/legacy option (--use-game-line-matrics) into the canonical flag.
    args.use_game_line_metrics = bool(
        getattr(args, "use_game_line_metrics", False) or getattr(args, "use_game_line_matrics", False)
    )
    selected_files = parse_target_files_arg(getattr(args, "target_file", None))
    if args.target_file and not selected_files:
        if is_ko:
            exit_with_error("--target-file 값이 비어 있습니다.", lang=lang)
        else:
            exit_with_error("--target-file values are empty.", lang=lang)

    if args.split_save_force and args.oneshot_save_force:
        if is_ko:
            exit_with_error("--split-save-force와 --oneshot-save-force를 동시에 사용할 수 없습니다.", lang=lang)
        else:
            exit_with_error("Cannot use --split-save-force and --oneshot-save-force at the same time.", lang=lang)

    # KR: 기본은 split-save 폴백을 활성화합니다.
    # EN: Split-save fallback is enabled by default.
    args.split_save = not args.oneshot_save_force
    if args.scan_jobs < 1:
        if is_ko:
            exit_with_error("--scan-jobs는 1 이상의 정수여야 합니다.", lang=lang)
        else:
            exit_with_error("--scan-jobs must be an integer greater than or equal to 1.", lang=lang)

    if args._scan_file_worker:
        if not args.gamepath:
            if is_ko:
                print("[scan_worker] 오류: --gamepath가 필요합니다.")
            else:
                print("[scan_worker] Error: --gamepath is required.")
            raise SystemExit(2)
        if not args._scan_file_worker_output:
            if is_ko:
                print("[scan_worker] 오류: --_scan-file-worker-output 경로가 필요합니다.")
            else:
                print("[scan_worker] Error: --_scan-file-worker-output path is required.")
            raise SystemExit(2)
        raise SystemExit(
            run_scan_file_worker(
                args.gamepath,
                args._scan_file_worker,
                args._scan_file_worker_output,
                lang=lang,
                detect_ps5_swizzle=args.ps5_swizzle,
            )
        )

    if args.temp_dir:
        args.temp_dir = os.path.abspath(str(args.temp_dir))
        try:
            os.makedirs(args.temp_dir, exist_ok=True)
        except Exception as e:
            if is_ko:
                exit_with_error(f"임시 폴더를 만들 수 없습니다: {args.temp_dir} ({e})", lang=lang)
            else:
                exit_with_error(f"Failed to create temp directory: {args.temp_dir} ({e})", lang=lang)
        if is_ko:
            print(f"임시 저장 경로: {args.temp_dir}")
        else:
            print(f"Temp save path: {args.temp_dir}")
        register_temp_dir_for_cleanup(os.path.join(args.temp_dir, "unity_font_replacer_temp"))

    output_only_root: str | None = None
    if args.output_only:
        output_only_root = os.path.abspath(str(args.output_only))
        try:
            os.makedirs(output_only_root, exist_ok=True)
        except Exception as e:
            if is_ko:
                exit_with_error(f"출력 폴더를 만들 수 없습니다: {output_only_root} ({e})", lang=lang)
            else:
                exit_with_error(f"Failed to create output folder: {output_only_root} ({e})", lang=lang)
        if is_ko:
            print(f"출력 전용 모드: 수정 파일을 '{output_only_root}'에 저장합니다.")
        else:
            print(f"Output-only mode: writing modified files to '{output_only_root}'.")

    preview_root: str | None = None
    if args.preview:
        preview_root = os.path.join(get_script_dir(), "preview")
        try:
            os.makedirs(preview_root, exist_ok=True)
        except Exception as e:
            if is_ko:
                exit_with_error(f"preview 폴더를 만들 수 없습니다: {preview_root} ({e})", lang=lang)
            else:
                exit_with_error(f"Failed to create preview folder: {preview_root} ({e})", lang=lang)
        if is_ko:
            print(f"Preview 모드: '{preview_root}'에 미리보기를 저장합니다.")
        else:
            print(f"Preview mode: saving previews to '{preview_root}'.")
        if not args.ps5_swizzle:
            if is_ko:
                print("  안내: --preview는 --ps5-swizzle와 함께 사용할 때 unswizzle 미리보기를 출력합니다.")
            else:
                print("  Note: --preview outputs unswizzled previews when used with --ps5-swizzle.")

    if args.use_game_line_metrics:
        if is_ko:
            print("줄 간격 메트릭 모드: 게임 원본 줄 간격 메트릭을 사용합니다.")
        else:
            print("Line metrics mode: using original in-game line metrics.")
    else:
        if is_ko:
            print("줄 간격 메트릭 모드: 교체 폰트 메트릭 보정을 기본 적용합니다.")
        else:
            print("Line metrics mode: using adjusted replacement font metrics by default.")

    if args.use_game_material:
        if is_ko:
            print("Material 모드: 게임 원본 Material 파라미터를 사용합니다.")
        else:
            print("Material mode: using original in-game Material parameters.")
    else:
        if is_ko:
            print("Material 모드: 교체 Material 보정(패딩 비율)을 기본 적용합니다.")
        else:
            print("Material mode: using adjusted replacement material by default (padding ratio).")
    if args.ps5_swizzle:
        if is_ko:
            print(
                "PS5 swizzle 모드: 대상 Atlas swizzle을 자동 판별해 교체 Atlas를 변환합니다 "
                f"(mask_x={PS5_SWIZZLE_MASK_X:#x}, mask_y={PS5_SWIZZLE_MASK_Y:#x}, rotate={PS5_SWIZZLE_ROTATE})."
            )
        else:
            print(
                "PS5 swizzle mode: auto-detecting target atlas swizzle state and transforming replacement atlas "
                f"(mask_x={PS5_SWIZZLE_MASK_X:#x}, mask_y={PS5_SWIZZLE_MASK_Y:#x}, rotate={PS5_SWIZZLE_ROTATE})."
            )
    else:
        if is_ko:
            print("PS5 swizzle 모드: 비활성화")
        else:
            print("PS5 swizzle mode: disabled")

    if args._validate_bundle:
        raise SystemExit(run_validation_worker(args._validate_bundle, lang=lang))

    import struct
    py_bits = struct.calcsize("P") * 8
    print(f"Python {sys.version} ({py_bits}-bit)")

    warn_unitypy_version(lang=lang)

    verbose_file = None
    if args.verbose:
        verbose_path = os.path.join(get_script_dir(), "verbose.txt")
        verbose_file = open(verbose_path, "w", encoding="utf-8")
        original_stdout = sys.__stdout__
        original_stderr = sys.__stderr__
        if original_stdout is None or original_stderr is None:
            if is_ko:
                exit_with_error("표준 출력 스트림을 사용할 수 없습니다.", lang=lang)
            else:
                exit_with_error("Standard output streams are unavailable.", lang=lang)
        sys.stdout = TeeWriter(verbose_file, original_stdout)
        sys.stderr = TeeWriter(verbose_file, original_stderr)
        if is_ko:
            print(f"[verbose] 로그를 '{verbose_path}'에 저장합니다.")
        else:
            print(f"[verbose] Saving logs to '{verbose_path}'.")

    input_path = strip_wrapping_quotes_repeated(args.gamepath) if args.gamepath else ""
    if not input_path:
        while True:
            if is_ko:
                entered_path = input("게임 경로를 입력하세요: ").strip()
            else:
                entered_path = input("Enter game path: ").strip()
            input_path = strip_wrapping_quotes_repeated(entered_path)
            if not input_path:
                if is_ko:
                    print("게임 경로가 필요합니다. 다시 입력해주세요.")
                else:
                    print("Game path is required. Please try again.")
                continue
            if not os.path.isdir(input_path):
                if is_ko:
                    print(f"'{input_path}'는 유효한 디렉토리가 아닙니다. 다시 입력해주세요.")
                else:
                    print(f"'{input_path}' is not a valid directory. Please try again.")
                continue
            try:
                game_path, data_path = resolve_game_path(input_path, lang=lang)
            except FileNotFoundError as e:
                if is_ko:
                    print(f"{e}\n다시 입력해주세요.")
                else:
                    print(f"{e}\nPlease try again.")
                continue
            break
    else:
        if not os.path.isdir(input_path):
            if is_ko:
                exit_with_error(f"'{input_path}'는 유효한 디렉토리가 아닙니다.", lang=lang)
            else:
                exit_with_error(f"'{input_path}' is not a valid directory.", lang=lang)
        try:
            game_path, data_path = resolve_game_path(input_path, lang=lang)
        except FileNotFoundError as e:
            exit_with_error(str(e), lang=lang)

    compile_method = get_compile_method(data_path)
    if is_ko:
        print(f"게임 경로: {game_path}")
        print(f"데이터 경로: {data_path}")
        print(f"컴파일 방식: {compile_method}")
        print(f"스캔 워커 수: {args.scan_jobs}")
    else:
        print(f"Game path: {game_path}")
        print(f"Data path: {data_path}")
        print(f"Compile method: {compile_method}")
        print(f"Scan workers: {args.scan_jobs}")

    if selected_files:
        target_text = ", ".join(sorted(selected_files))
        if is_ko:
            print(f"--target-file 적용: {target_text}")
        else:
            print(f"Applied --target-file: {target_text}")

    default_temp_root = register_temp_dir_for_cleanup(os.path.join(data_path, "temp"))
    if os.path.exists(default_temp_root):
        shutil.rmtree(default_temp_root)

    replace_ttf = not args.sdfonly
    replace_sdf = not args.ttfonly
    if args.sdfonly and args.ttfonly:
        if is_ko:
            exit_with_error("--sdfonly와 --ttfonly를 동시에 사용할 수 없습니다.", lang=lang)
        else:
            exit_with_error("Cannot use --sdfonly and --ttfonly at the same time.", lang=lang)

    replacements: dict[str, JsonDict] | None = None
    mode: str | None = None
    interactive_session = False
    if args.parse:
        mode = "parse"
    elif args.mulmaru:
        mode = "mulmaru"
    elif args.nanumgothic:
        mode = "nanumgothic"
    elif args.list:
        mode = "list"
    else:
        interactive_session = True
        if is_ko:
            while True:
                print("작업을 선택하세요:")
                print("  1. 폰트 정보 추출 (JSON 파일 생성)")
                print("  2. JSON 파일로 폰트 교체")
                print("  3. Mulmaru(물마루체)로 일괄 교체")
                print("  4. NanumGothic(나눔고딕)으로 일괄 교체")
                print()
                choice = input("선택 (1-4): ").strip()
                if choice in {"1", "2", "3", "4"}:
                    break
                print("잘못된 선택입니다. 다시 입력해주세요.")
        else:
            while True:
                print("Select a task:")
                print("  1. Export font info (create JSON)")
                print("  2. Replace fonts using JSON")
                print("  3. Bulk replace with Mulmaru")
                print("  4. Bulk replace with NanumGothic")
                print()
                choice = input("Choose (1-4): ").strip()
                if choice in {"1", "2", "3", "4"}:
                    break
                print("Invalid selection. Please try again.")

        if choice == "1":
            mode = "parse"
        elif choice == "2":
            mode = "list"
            while True:
                if is_ko:
                    entered = input("JSON 파일 경로를 입력하세요: ").strip()
                else:
                    entered = input("Enter JSON file path: ").strip()
                entered = strip_wrapping_quotes_repeated(entered)
                if not entered:
                    if is_ko:
                        print("JSON 파일 경로가 필요합니다. 다시 입력해주세요.")
                    else:
                        print("JSON file path is required. Please try again.")
                    continue
                if os.path.exists(entered):
                    args.list = entered
                    break
                if is_ko:
                    print(f"파일을 찾을 수 없습니다: '{entered}'")
                else:
                    print(f"File not found: '{entered}'")
        elif choice == "3":
            mode = "mulmaru"
        elif choice == "4":
            mode = "nanumgothic"

    if compile_method == "Il2cpp" and not os.path.exists(os.path.join(data_path, "Managed")):
        binary_path = os.path.join(game_path, "GameAssembly.dll")
        metadata_path = os.path.join(data_path, "il2cpp_data", "Metadata", "global-metadata.dat")
        if not os.path.exists(binary_path) or not os.path.exists(metadata_path):
            if is_ko:
                exit_with_error(
                    "Il2cpp 게임의 경우 'Managed' 폴더 또는 'GameAssembly.dll'과 'global-metadata.dat' 파일이 필요합니다.\n올바른 Unity 게임 폴더인지 확인해주세요.",
                    lang=lang,
                )
            else:
                exit_with_error(
                    "For Il2cpp games, the 'Managed' folder or 'GameAssembly.dll' and 'global-metadata.dat' files are required.\nPlease check that this is a valid Unity game folder.",
                    lang=lang,
                )

        dumper_path = os.path.join(get_script_dir(), "Il2CppDumper", "Il2CppDumper.exe")
        target_path = os.path.join(data_path, "Managed_")
        os.makedirs(target_path, exist_ok=True)
        command = [os.path.abspath(dumper_path), os.path.abspath(binary_path), os.path.abspath(metadata_path), os.path.abspath(target_path)]
        if is_ko:
            print("Il2cpp 게임을 위한 Managed 폴더를 생성합니다...")
        else:
            print("Creating Managed folder for Il2cpp game...")
        print(os.path.abspath(target_path))

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                startupinfo=startupinfo,
                encoding="utf-8",
            )
            if process.returncode == 0:
                print(process.stdout)
                shutil.move(os.path.join(data_path, "Managed_", "DummyDll"), os.path.join(data_path, "Managed"))
                shutil.rmtree(os.path.join(data_path, "Managed_"))
                if is_ko:
                    print("더미 DLL 생성에 성공했습니다!")
                else:
                    print("Dummy DLL generated successfully!")
                compile_method = get_compile_method(data_path)
                if is_ko:
                    print(f"컴파일 방식 재감지: {compile_method}")
                else:
                    print(f"Compile method re-detected: {compile_method}")
            else:
                print(process.stderr)
                if is_ko:
                    exit_with_error("Il2cpp 더미 DLL 생성 실패", lang=lang)
                else:
                    exit_with_error("Failed to generate Il2cpp dummy DLL", lang=lang)
        except Exception as e:
            if is_ko:
                exit_with_error(f"Il2CppDumper 실행 중 예외 발생: {e}", lang=lang)
            else:
                exit_with_error(f"Exception while running Il2CppDumper: {e}", lang=lang)

    if mode == "parse":
        parse_fonts(
            game_path,
            lang=lang,
            target_files=selected_files if selected_files else None,
            scan_jobs=args.scan_jobs,
            ps5_swizzle=args.ps5_swizzle,
        )
        if is_ko:
            input("\n엔터를 눌러 종료...")
        else:
            input("\nPress Enter to exit...")
        return

    if mode == "mulmaru":
        if is_ko:
            print("Mulmaru 폰트로 일괄 교체합니다...")
        else:
            print("Bulk replacing with Mulmaru...")
        replacements = create_batch_replacements(
            game_path,
            "Mulmaru",
            replace_ttf,
            replace_sdf,
            target_files=selected_files if selected_files else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        ttf_count = sum(1 for v in replacements.values() if v["Type"] == "TTF")
        sdf_count = sum(1 for v in replacements.values() if v["Type"] == "SDF")
        if is_ko:
            print(f"발견된 폰트: TTF {ttf_count}개, SDF {sdf_count}개")
        else:
            print(f"Found fonts: TTF {ttf_count}, SDF {sdf_count}")
    elif mode == "nanumgothic":
        if is_ko:
            print("NanumGothic 폰트로 일괄 교체합니다...")
        else:
            print("Bulk replacing with NanumGothic...")
        replacements = create_batch_replacements(
            game_path,
            "NanumGothic",
            replace_ttf,
            replace_sdf,
            target_files=selected_files if selected_files else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        ttf_count = sum(1 for v in replacements.values() if v["Type"] == "TTF")
        sdf_count = sum(1 for v in replacements.values() if v["Type"] == "SDF")
        if is_ko:
            print(f"발견된 폰트: TTF {ttf_count}개, SDF {sdf_count}개")
        else:
            print(f"Found fonts: TTF {ttf_count}, SDF {sdf_count}")
    elif mode == "list":
        if isinstance(args.list, str):
            args.list = strip_wrapping_quotes_repeated(args.list)

        if interactive_session:
            while not args.list or not os.path.exists(args.list):
                if args.list:
                    if is_ko:
                        print(f"'{args.list}' 파일을 찾을 수 없습니다.")
                    else:
                        print(f"File not found: '{args.list}'")
                if is_ko:
                    entered = input("JSON 파일 경로를 다시 입력하세요: ").strip()
                else:
                    entered = input("Re-enter JSON file path: ").strip()
                args.list = strip_wrapping_quotes_repeated(entered)

        if not args.list or not os.path.exists(args.list):
            if is_ko:
                exit_with_error(f"'{args.list}' 파일을 찾을 수 없습니다.", lang=lang)
            else:
                exit_with_error(f"File not found: '{args.list}'", lang=lang)

        if is_ko:
            print(f"'{args.list}' 파일을 읽어서 교체합니다...")
        else:
            print(f"Replacing using '{args.list}'...")
        with open(args.list, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            if is_ko:
                exit_with_error("JSON 루트는 객체(dict)여야 합니다.", lang=lang)
            else:
                exit_with_error("JSON root must be an object (dict).", lang=lang)
        replacements = cast(dict[str, JsonDict], loaded)

    if replacements is None:
        if is_ko:
            exit_with_error("교체 정보가 생성되지 않았습니다.", lang=lang)
        else:
            exit_with_error("Replacement mapping was not generated.", lang=lang)

    if selected_files:
        replacements = {
            key: value
            for key, value in replacements.items()
            if isinstance(value, dict)
            and os.path.basename(str(value.get("File", ""))) in selected_files
        }

        if not replacements:
            target_text = ", ".join(sorted(selected_files))
            if is_ko:
                exit_with_error(f"--target-file 조건에 맞는 교체 대상이 없습니다: {target_text}", lang=lang)
            else:
                exit_with_error(f"No replacement targets matched --target-file: {target_text}", lang=lang)

    unity_version = get_unity_version(game_path, lang=lang)
    generator = _create_generator(unity_version, game_path, data_path, compile_method, lang=lang)
    replacement_lookup, files_to_process = build_replacement_lookup(replacements)
    preview_files_to_process: set[str] = set()
    if args.preview and args.ps5_swizzle:
        preview_files_to_process = {
            os.path.basename(str(value.get("File", "")))
            for value in replacements.values()
            if isinstance(value, dict) and str(value.get("Type", "")) == "SDF"
        }
        preview_files_to_process.discard("")
    process_files = set(files_to_process) | preview_files_to_process
    assets_files = find_assets_files(
        game_path,
        lang=lang,
        target_files=process_files if process_files else None,
    )

    modified_count = 0
    for assets_file in assets_files:
        fn = os.path.basename(assets_file)
        if fn in process_files:
            working_assets_file = assets_file
            if output_only_root:
                working_assets_file = resolve_output_only_path(assets_file, data_path, output_only_root)
                working_dir = os.path.dirname(working_assets_file)
                if working_dir and not os.path.exists(working_dir):
                    os.makedirs(working_dir, exist_ok=True)
                shutil.copy2(assets_file, working_assets_file)
                if is_ko:
                    rel_out = os.path.relpath(working_assets_file, output_only_root)
                    print(f"  출력 대상 준비: {rel_out}")
                else:
                    rel_out = os.path.relpath(working_assets_file, output_only_root)
                    print(f"  Prepared output target: {rel_out}")
            if is_ko:
                print(f"\n처리 중: {fn}")
            else:
                print(f"\nProcessing: {fn}")
            # KR: 기본은 split-save 폴백을 사용하고, --oneshot-save-force일 때만 비활성화합니다.
            # EN: Split-save fallback is enabled by default and disabled only by --oneshot-save-force.
            file_replacements = {
                key: value
                for key, value in replacements.items()
                if isinstance(value, dict)
                and value.get("File") == fn
                and value.get("Replace_to")
            }
            file_ttf_replacements = {
                key: value
                for key, value in file_replacements.items()
                if value.get("Type") == "TTF"
            }
            file_sdf_replacements = {
                key: value
                for key, value in file_replacements.items()
                if value.get("Type") == "SDF"
            }

            file_modified = False
            use_split_sdf_save = args.split_save and replace_sdf and len(file_sdf_replacements) > 1

            if use_split_sdf_save:
                if is_ko:
                    print(
                        f"  SDF 대상 {len(file_sdf_replacements)}건: one-shot 실패 시 적응형 분할 저장으로 폴백합니다..."
                    )
                else:
                    print(
                        f"  {len(file_sdf_replacements)} SDF targets: will fall back to adaptive split save if one-shot fails..."
                    )

                # KR: 먼저 한 번에 저장을 시도하고, 실패 시에만 적응형 분할 저장으로 폴백합니다.
                # EN: Try one-shot save first, then fall back to adaptive split save on failure.
                file_lookup, _ = build_replacement_lookup(file_replacements)
                one_shot_ok = False
                if args.split_save_force:
                    if is_ko:
                        print("  --split-save-force 활성화: one-shot을 건너뛰고 SDF 1개씩 강제 분할 저장을 시작합니다...")
                    else:
                        print("  --split-save-force enabled: skipping one-shot and forcing one-by-one SDF split save...")
                else:
                    try:
                        one_shot_ok = replace_fonts_in_file(
                            unity_version,
                            game_path,
                            working_assets_file,
                            file_replacements,
                            replace_ttf=replace_ttf,
                            replace_sdf=replace_sdf,
                            use_game_mat=args.use_game_material,
                            use_game_line_metrics=args.use_game_line_metrics,
                            material_scale_by_padding=not args.use_game_material,
                            prefer_original_compress=args.original_compress,
                            temp_root_dir=args.temp_dir,
                            generator=generator,
                            replacement_lookup=file_lookup,
                            ps5_swizzle=args.ps5_swizzle,
                            preview=args.preview,
                            preview_root=preview_root,
                            lang=lang,
                        )
                    except MemoryError as e:
                        if is_ko:
                            print(f"  one-shot 저장 실패 [MemoryError]: {e!r}")
                            print("  적응형 분할 저장으로 폴백합니다...")
                        else:
                            print(f"  One-shot save failed [MemoryError]: {e!r}")
                            print("  Falling back to adaptive split save...")
                    except Exception as e:
                        if is_ko:
                            print(f"  one-shot 저장 실패 [{type(e).__name__}]: {e!r}")
                            print("  적응형 분할 저장으로 폴백합니다...")
                        else:
                            print(f"  One-shot save failed [{type(e).__name__}]: {e!r}")
                            print("  Falling back to adaptive split save...")

                if one_shot_ok:
                    file_modified = True
                else:
                    split_stopped = False
                    if replace_ttf and file_ttf_replacements:
                        file_ttf_lookup, _ = build_replacement_lookup(file_ttf_replacements)
                        try:
                            if replace_fonts_in_file(
                                unity_version,
                                game_path,
                                working_assets_file,
                                file_ttf_replacements,
                                replace_ttf=True,
                                replace_sdf=False,
                                use_game_mat=args.use_game_material,
                                use_game_line_metrics=args.use_game_line_metrics,
                                material_scale_by_padding=not args.use_game_material,
                                prefer_original_compress=args.original_compress,
                                temp_root_dir=args.temp_dir,
                                generator=generator,
                                replacement_lookup=file_ttf_lookup,
                                ps5_swizzle=args.ps5_swizzle,
                                preview=args.preview,
                                preview_root=preview_root,
                                lang=lang,
                            ):
                                file_modified = True
                        except Exception as e:
                            if is_ko:
                                print(f"  TTF 분할 저장 실패 [{type(e).__name__}]: {e!r}")
                            else:
                                print(f"  TTF split save failed [{type(e).__name__}]: {e!r}")
                            split_stopped = True

                    if replace_sdf and not split_stopped:
                        sdf_items = list(file_sdf_replacements.items())
                        sdf_total = len(sdf_items)
                        if sdf_total > 0:
                            if args.split_save_force:
                                batch_size = 1
                            else:
                                batch_size = min(sdf_total, max(1, sdf_total // 2))

                            idx = 0
                            while idx < sdf_total:
                                current_batch = min(batch_size, sdf_total - idx)
                                batch_dict = dict(sdf_items[idx : idx + current_batch])
                                batch_lookup, _ = build_replacement_lookup(batch_dict)

                                try:
                                    ok = replace_fonts_in_file(
                                        unity_version,
                                        game_path,
                                        working_assets_file,
                                        batch_dict,
                                        replace_ttf=False,
                                        replace_sdf=True,
                                        use_game_mat=args.use_game_material,
                                        use_game_line_metrics=args.use_game_line_metrics,
                                        material_scale_by_padding=not args.use_game_material,
                                        prefer_original_compress=args.original_compress,
                                        temp_root_dir=args.temp_dir,
                                        generator=generator,
                                        replacement_lookup=batch_lookup,
                                        ps5_swizzle=args.ps5_swizzle,
                                        preview=args.preview,
                                        preview_root=preview_root,
                                        lang=lang,
                                    )
                                except Exception as e:
                                    ok = False
                                    if is_ko:
                                        print(f"  SDF 배치 저장 실패 [{type(e).__name__}]: {e!r}")
                                    else:
                                        print(f"  SDF batch save failed [{type(e).__name__}]: {e!r}")

                                if ok:
                                    file_modified = True
                                    idx += current_batch
                                    if idx < sdf_total:
                                        if args.split_save_force:
                                            if is_ko:
                                                print(f"  SDF 배치 진행: {idx}/{sdf_total} (다음 배치: 1, 강제)")
                                            else:
                                                print(f"  SDF batch progress: {idx}/{sdf_total} (next batch: 1, forced)")
                                        else:
                                            # KR: 성공하면 배치를 키워 쓰기 횟수를 줄입니다.
                                            # EN: Grow batch size after success to reduce write count.
                                            batch_size = min(sdf_total - idx, max(current_batch + 1, current_batch * 2))
                                            if is_ko:
                                                print(f"  SDF 배치 진행: {idx}/{sdf_total} (다음 배치: {batch_size})")
                                            else:
                                                print(f"  SDF batch progress: {idx}/{sdf_total} (next batch: {batch_size})")
                                else:
                                    if is_ko:
                                        print("  SDF 배치 저장 실패: 내부 저장 단계가 False를 반환했습니다. 위 오류 로그를 확인하세요.")
                                    else:
                                        print("  SDF batch save failed: internal save stage returned False. Check previous error logs.")
                                    if current_batch <= 1:
                                        split_stopped = True
                                        if is_ko:
                                            print("  SDF 분할 저장 중단: 배치 1개에서도 저장 실패")
                                        else:
                                            print("  Stopping SDF split save: failed even with batch size 1")
                                        break

                                    batch_size = max(1, current_batch // 2)
                                    gc.collect()
                                    if is_ko:
                                        print(f"  SDF 배치 크기를 {batch_size}로 줄여 재시도합니다...")
                                    else:
                                        print(f"  Reducing SDF batch size to {batch_size} and retrying...")
            else:
                if replace_sdf and len(file_sdf_replacements) > 1 and not args.split_save:
                    if is_ko:
                        print("  참고: --oneshot-save-force로 split-save 폴백이 비활성화되어 메모리 피크가 증가할 수 있습니다.")
                    else:
                        print("  Note: --oneshot-save-force disables split-save fallback and may increase memory peak.")
                try:
                    if replace_fonts_in_file(
                        unity_version,
                        game_path,
                        working_assets_file,
                        replacements,
                        replace_ttf,
                        replace_sdf,
                        use_game_mat=args.use_game_material,
                        use_game_line_metrics=args.use_game_line_metrics,
                        material_scale_by_padding=not args.use_game_material,
                        prefer_original_compress=args.original_compress,
                        temp_root_dir=args.temp_dir,
                        generator=generator,
                        replacement_lookup=replacement_lookup,
                        ps5_swizzle=args.ps5_swizzle,
                        preview=args.preview,
                        preview_root=preview_root,
                        lang=lang,
                    ):
                        file_modified = True
                except Exception as e:
                    if is_ko:
                        print(f"  파일 처리 실패 [{type(e).__name__}]: {e!r}")
                    else:
                        print(f"  File processing failed [{type(e).__name__}]: {e!r}")

            if file_modified:
                modified_count += 1

    if is_ko:
        print(f"\n완료! {modified_count}개의 파일이 수정되었습니다.")
        input("\n엔터를 눌러 종료...")
    else:
        print(f"\nDone! Modified {modified_count} file(s).")
        input("\nPress Enter to exit...")


def main() -> None:
    """KR: 한국어 CLI 진입점입니다.
    EN: Korean CLI entrypoint.
    """
    main_cli(lang="ko")


def main_en() -> None:
    """KR: 영어 CLI 진입점입니다.
    EN: English CLI entrypoint.
    """
    main_cli(lang="en")


def _restore_tee_streams() -> None:
    """KR: TeeWriter로 교체된 stdout/stderr를 원상복구합니다.
    EN: Restore stdout/stderr replaced by TeeWriter.
    """
    if isinstance(sys.stdout, TeeWriter):
        sys.stdout.file.close()
        sys.stdout = sys.__stdout__
    if isinstance(sys.stderr, TeeWriter):
        sys.stderr.file.close()
        sys.stderr = sys.__stderr__


def run_main_ko() -> None:
    """KR: 한국어 실행 진입점을 예외 처리와 함께 실행합니다.
    EN: Run Korean entrypoint with top-level exception handling.
    """
    try:
        main()
    except Exception as e:
        print(f"\n예상치 못한 오류가 발생했습니다: {e}")
        tb_module.print_exc()
        input("\n엔터를 눌러 종료...")
        sys.exit(1)
    finally:
        _restore_tee_streams()
        cleanup_registered_temp_dirs()


def run_main_en() -> None:
    """KR: 영어 실행 진입점을 예외 처리와 함께 실행합니다.
    EN: Run English entrypoint with top-level exception handling.
    """
    try:
        main_en()
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
        tb_module.print_exc()
        input("\nPress Enter to exit...")
        sys.exit(1)
    finally:
        _restore_tee_streams()
        cleanup_registered_temp_dirs()


if __name__ == "__main__":
    try:
        run_main_ko()
    except Exception as e:
        print(f"\n예상치 못한 오류가 발생했습니다: {e}")
        tb_module.print_exc()
        input("\n엔터를 눌러 종료...")
        sys.exit(1)
