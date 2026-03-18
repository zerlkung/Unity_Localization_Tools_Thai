"""Core logic for exporting TMP SDF assets from Unity game data."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from functools import lru_cache
from typing import Any, Literal, NoReturn, cast

import UnityPy
from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator

logger = logging.getLogger(__name__)


Language = Literal["ko", "en"]
JsonDict = dict[str, Any]
_TMP_OLD_ONLY_LAST = (2018, 3, 14)
_TMP_NEW_SCHEMA_FIRST = (2018, 4, 2)


def _configure_logging(level: int = logging.INFO) -> None:
    """Configure console logging for CLI-mode output."""
    if logging.getLogger().handlers:
        logging.getLogger().setLevel(level)
        return
    logging.basicConfig(level=level, format="%(message)s")


def _coerce_log_level(message: str, default_level: int = logging.INFO) -> int:
    lowered = message.lower()
    if "경고" in message or "warning" in lowered:
        return logging.WARNING
    if (
        "오류" in message
        or "error" in lowered
        or "failed" in lowered
        or "실패" in message
    ):
        return logging.ERROR
    return default_level


def _log_console(
    *parts: object,
    sep: str = " ",
    level: int | None = None,
) -> None:
    """Print-compatible logging adapter."""
    message = sep.join(str(part) for part in parts)
    resolved_level = _coerce_log_level(message) if level is None else level
    logger.log(resolved_level, message)


def _debug_parse_enabled() -> bool:
    """KR: TMP 파싱 디버그 로그 출력 여부를 반환합니다.
    EN: Return whether TMP parsing debug logging is enabled.
    """
    return os.environ.get("UFR_DEBUG_PARSE", "").strip() == "1"


def _debug_parse_log(message: str) -> None:
    """KR: 디버그 모드일 때만 메시지를 출력합니다.
    EN: Print a debug message only when debug mode is enabled.
    """
    if _debug_parse_enabled():
        _log_console(message)


def exit_with_error(lang: Language, message: str) -> NoReturn:
    """KR: 로컬라이즈된 오류를 출력하고 종료합니다.
    EN: Print a localized error and terminate the process.
    """
    if lang == "ko":
        _log_console(f"오류: {message}")
        input("\n엔터를 눌러 종료...")
    else:
        _log_console(f"Error: {message}")
        input("\nPress Enter to exit...")
    sys.exit(1)


def find_ggm_file(data_path: str) -> str | None:
    """KR: 데이터 폴더에서 globalgamemanagers 계열 파일을 찾습니다.
    EN: Find a globalgamemanagers-like file under the data folder.
    """
    candidates = [
        "globalgamemanagers",
        "globalgamemanagers.assets",
        "data.unity3d",
    ]
    for candidate in candidates:
        ggm_path = os.path.join(data_path, candidate)
        if os.path.exists(ggm_path):
            return ggm_path
    return None


def resolve_game_path(lang: Language, path: str | None = None) -> tuple[str, str]:
    """KR: 입력 경로를 게임 루트/데이터 폴더 경로로 정규화합니다.
    EN: Normalize an input path into game-root and data-folder paths.
    """
    if path is None:
        path = os.getcwd()

    path = os.path.normpath(os.path.abspath(path))

    if path.lower().endswith("_data"):
        data_path = path
        game_path = os.path.dirname(path)
    else:
        game_path = path
        data_folders = [
            d
            for d in os.listdir(path)
            if d.lower().endswith("_data") and os.path.isdir(os.path.join(path, d))
        ]
        if not data_folders:
            if lang == "ko":
                exit_with_error(
                    lang,
                    f"'{path}'에서 _Data 폴더를 찾을 수 없습니다.\n게임 루트 폴더 또는 _Data 폴더에서 실행해주세요.",
                )
            else:
                exit_with_error(
                    lang,
                    f"Could not find the _Data folder in '{path}'.\nRun this from the game root folder or the _Data folder.",
                )
        data_path = os.path.join(game_path, data_folders[0])

    ggm_path = find_ggm_file(data_path)
    if not ggm_path:
        if lang == "ko":
            exit_with_error(
                lang,
                f"'{data_path}'에서 globalgamemanagers 파일을 찾을 수 없습니다.\n올바른 Unity 게임 폴더인지 확인해주세요.",
            )
        else:
            exit_with_error(
                lang,
                f"Could not find the globalgamemanagers file in '{data_path}'.\nPlease check that this is a valid Unity game folder.",
            )

    return game_path, data_path


def get_unity_version(data_path: str) -> str:
    """KR: 데이터 폴더의 Unity 버전을 반환합니다.
    EN: Return the Unity version detected from the data folder.
    """
    ggm_path = find_ggm_file(data_path)
    if not ggm_path:
        raise FileNotFoundError(f"globalgamemanagers not found in '{data_path}'")
    return str(UnityPy.load(ggm_path).objects[0].assets_file.unity_version)


def find_assets_files(data_path: str) -> list[str]:
    """KR: 교체/추출 대상이 될 에셋 파일 목록을 수집합니다.
    EN: Collect candidate asset files for replacement/export.
    """
    assets_files: list[str] = []
    exclude_exts = {
        ".dll",
        ".manifest",
        ".exe",
        ".txt",
        ".json",
        ".xml",
        ".log",
        ".ini",
        ".cfg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".wav",
        ".mp3",
        ".ogg",
        ".mp4",
        ".avi",
        ".mov",
    }
    for root, _, files in os.walk(data_path):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in exclude_exts:
                assets_files.append(os.path.join(root, fn))
    return assets_files


def get_compile_method(data_path: str) -> str:
    """KR: Managed 폴더 존재 여부로 Mono/Il2cpp를 판별합니다.
    EN: Detect Mono/Il2cpp based on the Managed folder presence.
    """
    return "Mono" if os.path.exists(os.path.join(data_path, "Managed")) else "Il2cpp"


def create_generator(
    unity_version: str,
    game_path: str,
    data_path: str,
    compile_method: str,
    lang: Language,
) -> TypeTreeGenerator:
    """KR: Unity 타입트리 생성기를 초기화하고 메타데이터를 로드합니다.
    EN: Initialize a Unity typetree generator and load metadata.
    """
    try:
        generator = TypeTreeGenerator(unity_version)
    except ImportError:
        if lang == "ko":
            raise RuntimeError(
                "TypeTreeGeneratorAPI가 설치되지 않아 TMP 폰트 타입트리를 생성할 수 없습니다.\n"
                "`pip install TypeTreeGeneratorAPI`를 실행해 주세요."
            )
        raise RuntimeError(
            "TypeTreeGeneratorAPI is required to generate TMP typetrees.\n"
            "Install it with: `pip install TypeTreeGeneratorAPI`."
        )

    if compile_method == "Mono":
        managed_dir = os.path.join(data_path, "Managed")
        for fn in os.listdir(managed_dir):
            if not fn.endswith(".dll"):
                continue
            try:
                with open(os.path.join(managed_dir, fn), "rb") as f:
                    generator.load_dll(f.read())
            except Exception as e:  # pragma: no cover
                if lang == "ko":
                    _log_console(f"경고: DLL 로드 실패 '{fn}': {e}")
                else:
                    _log_console(f"Warning: failed to load DLL '{fn}': {e}")
    else:
        il2cpp_path = os.path.join(game_path, "GameAssembly.dll")
        metadata_path = os.path.join(
            data_path, "il2cpp_data", "Metadata", "global-metadata.dat"
        )
        if not os.path.exists(il2cpp_path) or not os.path.exists(metadata_path):
            if lang == "ko":
                raise RuntimeError(
                    "Il2cpp 감지됨. 'GameAssembly.dll'과 'global-metadata.dat'가 필요합니다."
                )
            raise RuntimeError(
                "Detected Il2cpp. 'GameAssembly.dll' and 'global-metadata.dat' are required."
            )
        with open(il2cpp_path, "rb") as f:
            il2cpp = f.read()
        with open(metadata_path, "rb") as f:
            metadata = f.read()
        generator.load_il2cpp(il2cpp, metadata)

    return generator


@lru_cache(maxsize=256)
def _parse_unity_version_triplet(version_text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text or "")
    if not match:
        return None
    try:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    except Exception:
        return None


def _tmp_version_hint(unity_version: str | None) -> Literal["new", "old"] | None:
    if not unity_version:
        return None
    triplet = _parse_unity_version_triplet(str(unity_version))
    if triplet is None:
        return None
    if triplet <= _TMP_OLD_ONLY_LAST:
        return "old"
    if triplet >= _TMP_NEW_SCHEMA_FIRST:
        return "new"
    return None


def _safe_list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _first_atlas_ref(value: Any) -> JsonDict | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict):
            return cast(JsonDict, item)
    return None


def _atlas_ref_ids(ref: Any) -> tuple[int, int]:
    if not isinstance(ref, dict):
        return 0, 0
    try:
        file_id = int(ref.get("m_FileID", 0) or 0)
    except Exception:
        file_id = 0
    try:
        path_id = int(ref.get("m_PathID", 0) or 0)
    except Exception:
        path_id = 0
    return file_id, path_id


def _normalize_assets_basename(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    normalized = text.replace("\\", "/")
    name = os.path.basename(normalized)
    return name or None


def _normalize_asset_lookup_path(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    normalized = text.replace("\\", "/")
    lowered = normalized.lower()
    for prefix in ("archive://", "archive:/", "file://"):
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :]
            lowered = normalized.lower()
            break
    while normalized.startswith("/"):
        normalized = normalized[1:]
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = re.sub(r"/{2,}", "/", normalized).strip()
    return normalized.lower() if normalized else None


def _normalize_asset_file_key(path: Any) -> str | None:
    text = str(path).strip() if path is not None else ""
    if not text:
        return None
    return os.path.normcase(os.path.abspath(text))


def _normalize_assets_key(value: Any) -> str | None:
    name = _normalize_assets_basename(value)
    return name.lower() if name else None


def _build_asset_file_index(
    assets_files: list[str],
    data_path: str,
) -> dict[str, Any]:
    data_root = os.path.abspath(data_path)
    path_by_key: dict[str, str] = {}
    relpath_to_keys: dict[str, list[str]] = {}
    basename_to_keys: dict[str, list[str]] = {}
    relpath_by_key: dict[str, str] = {}
    basename_by_key: dict[str, str] = {}

    for candidate_path in sorted(assets_files):
        asset_key = _normalize_asset_file_key(candidate_path)
        if not asset_key:
            continue
        abs_path = os.path.abspath(candidate_path)
        rel_path = os.path.relpath(abs_path, data_root).replace("\\", "/").lower()
        basename = os.path.basename(abs_path).lower()
        path_by_key[asset_key] = abs_path
        relpath_by_key[asset_key] = rel_path
        basename_by_key[asset_key] = basename
        relpath_to_keys.setdefault(rel_path, []).append(asset_key)
        basename_to_keys.setdefault(basename, []).append(asset_key)

    return {
        "data_root": data_root,
        "path_by_key": path_by_key,
        "relpath_to_keys": relpath_to_keys,
        "basename_to_keys": basename_to_keys,
        "relpath_by_key": relpath_by_key,
        "basename_by_key": basename_by_key,
    }


def _extract_external_assets_name(external_ref: Any) -> str | None:
    if external_ref is None:
        return None

    candidates: list[Any] = []
    if isinstance(external_ref, dict):
        candidates.extend(
            [
                external_ref.get("path"),
                external_ref.get("pathName"),
                external_ref.get("name"),
                external_ref.get("fileName"),
                external_ref.get("asset_name"),
                external_ref.get("assetPath"),
            ]
        )
    else:
        for attr in (
            "path",
            "pathName",
            "name",
            "fileName",
            "asset_name",
            "assetPath",
        ):
            candidates.append(getattr(external_ref, attr, None))

    for candidate in candidates:
        name = _normalize_assets_basename(candidate)
        if name:
            return name
    return None


def _extract_external_assets_candidates(external_ref: Any) -> list[str]:
    if external_ref is None:
        return []

    raw_candidates: list[Any] = []
    if isinstance(external_ref, dict):
        raw_candidates.extend(
            [
                external_ref.get("path"),
                external_ref.get("pathName"),
                external_ref.get("name"),
                external_ref.get("fileName"),
                external_ref.get("asset_name"),
                external_ref.get("assetPath"),
            ]
        )
    else:
        for attr in (
            "path",
            "pathName",
            "name",
            "fileName",
            "asset_name",
            "assetPath",
        ):
            raw_candidates.append(getattr(external_ref, attr, None))

    resolved: list[str] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        normalized_path = _normalize_asset_lookup_path(candidate)
        if normalized_path and normalized_path not in seen:
            seen.add(normalized_path)
            resolved.append(normalized_path)
        normalized_name = _normalize_assets_basename(candidate)
        if normalized_name:
            lowered_name = normalized_name.lower()
            if lowered_name not in seen:
                seen.add(lowered_name)
                resolved.append(lowered_name)
    return resolved


def _resolve_external_ref(source_assets_file: Any, file_id: int) -> Any:
    try:
        resolved_file_id = int(file_id or 0)
    except Exception:
        resolved_file_id = 0

    if resolved_file_id == 0:
        return None

    externals = getattr(source_assets_file, "externals", None)
    if externals is None:
        externals = getattr(source_assets_file, "m_Externals", None)

    if isinstance(externals, dict):
        external_ref = externals.get(resolved_file_id)
        if external_ref is None:
            external_ref = externals.get(resolved_file_id - 1)
        return external_ref

    if isinstance(externals, (list, tuple)):
        ext_index = resolved_file_id - 1
        if 0 <= ext_index < len(externals):
            return externals[ext_index]
    return None


def _resolve_assets_name_from_file_id(source_assets_file: Any, file_id: int) -> str | None:
    try:
        resolved_file_id = int(file_id or 0)
    except Exception:
        resolved_file_id = 0

    if resolved_file_id == 0:
        return _normalize_assets_basename(getattr(source_assets_file, "name", ""))

    externals = getattr(source_assets_file, "externals", None)
    if externals is None:
        externals = getattr(source_assets_file, "m_Externals", None)

    external_ref = _resolve_external_ref(source_assets_file, resolved_file_id)
    if externals is None:
        return None
    return _extract_external_assets_name(external_ref)


def _collect_asset_file_index_matches(
    asset_file_index: dict[str, Any] | None,
    reference: Any,
) -> list[str]:
    if not isinstance(asset_file_index, dict):
        return []

    normalized_reference = _normalize_asset_lookup_path(reference)
    if not normalized_reference:
        normalized_reference = _normalize_assets_basename(reference)
        if normalized_reference:
            normalized_reference = normalized_reference.lower()
    if not normalized_reference:
        return []

    relpath_to_keys = cast(
        dict[str, list[str]],
        asset_file_index.get("relpath_to_keys", {}),
    )
    basename_to_keys = cast(
        dict[str, list[str]],
        asset_file_index.get("basename_to_keys", {}),
    )
    relpath_by_key = cast(dict[str, str], asset_file_index.get("relpath_by_key", {}))

    matches: list[str] = []
    seen: set[str] = set()

    def _append_match(match_key: str) -> None:
        if match_key and match_key not in seen:
            seen.add(match_key)
            matches.append(match_key)

    for match_key in relpath_to_keys.get(normalized_reference, []):
        _append_match(match_key)

    if not matches and "/" in normalized_reference:
        suffix = "/" + normalized_reference
        for match_key, rel_path in relpath_by_key.items():
            if rel_path == normalized_reference or rel_path.endswith(suffix):
                _append_match(match_key)

    basename = os.path.basename(normalized_reference)
    for match_key in basename_to_keys.get(basename, []):
        _append_match(match_key)

    return matches


def _choose_asset_file_match(
    asset_file_index: dict[str, Any] | None,
    matches: list[str],
    *,
    current_file_key: str | None,
    reference_desc: str,
) -> str | None:
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if current_file_key and isinstance(asset_file_index, dict):
        path_by_key = cast(dict[str, str], asset_file_index.get("path_by_key", {}))
        current_path = path_by_key.get(current_file_key)
        if current_path:
            current_dir = os.path.dirname(current_path)
            sibling_matches = [
                match_key
                for match_key in matches
                if os.path.dirname(path_by_key.get(match_key, "")) == current_dir
            ]
            if len(sibling_matches) == 1:
                return sibling_matches[0]
    chosen = sorted(matches)[0]
    _log_console(
        f"Warning: ambiguous asset reference '{reference_desc}' matched {len(matches)} files; using first: {chosen}"
    )
    return chosen


def _resolve_target_outer_key(
    current_outer_key: str,
    source_assets_file: Any,
    file_id: int,
    target_assets_name: str | None,
    *,
    local_assets_keys: set[str] | None = None,
    asset_file_index: dict[str, Any] | None,
) -> str | None:
    try:
        resolved_file_id = int(file_id or 0)
    except Exception:
        resolved_file_id = 0
    if resolved_file_id == 0:
        return current_outer_key
    target_assets_key = _normalize_assets_key(target_assets_name)
    if target_assets_key and isinstance(local_assets_keys, set):
        if target_assets_key in local_assets_keys:
            return current_outer_key

    external_ref = _resolve_external_ref(source_assets_file, resolved_file_id)
    candidates = _extract_external_assets_candidates(external_ref)
    if target_assets_name:
        normalized_assets_name = _normalize_assets_basename(target_assets_name)
        if normalized_assets_name:
            candidates.append(normalized_assets_name.lower())

    for candidate in candidates:
        matches = _collect_asset_file_index_matches(asset_file_index, candidate)
        chosen = _choose_asset_file_match(
            asset_file_index,
            matches,
            current_file_key=current_outer_key,
            reference_desc=str(candidate),
        )
        if chosen:
            return chosen
    return None


def _make_assets_object_key(assets_name: str, path_id: int) -> str:
    normalized_assets = _normalize_assets_key(assets_name)
    if not normalized_assets:
        normalized_assets = ""
    return f"{normalized_assets}|{int(path_id)}"


def _has_real_atlas_path(ref: Any) -> bool:
    _, path_id = _atlas_ref_ids(ref)
    return path_id > 0


def _first_valid_atlas_ref(value: Any) -> JsonDict | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict) and _has_real_atlas_path(item):
            return cast(JsonDict, item)
    return None


def _best_atlas_ref(
    data: JsonDict,
    *,
    prefer_new: bool,
) -> JsonDict | None:
    new_any = _first_atlas_ref(data.get("m_AtlasTextures"))
    new_valid = _first_valid_atlas_ref(data.get("m_AtlasTextures"))
    old_any = (
        cast(JsonDict | None, data.get("atlas"))
        if isinstance(data.get("atlas"), dict)
        else None
    )
    old_valid = old_any if _has_real_atlas_path(old_any) else None

    ordered = (
        (new_valid, old_valid, new_any, old_any)
        if prefer_new
        else (old_valid, new_valid, old_any, new_any)
    )
    for ref in ordered:
        if isinstance(ref, dict):
            return ref
    return None


def detect_tmp_version(
    data: JsonDict, unity_version: str | None = None
) -> Literal["new", "old"]:
    """KR: TMP 폰트 데이터가 신형/구형 포맷인지 판별합니다.
    EN: Detect whether TMP font data uses new or old schema.
    """
    new_glyph_count = _safe_list_len(data.get("m_GlyphTable"))
    old_glyph_count = _safe_list_len(data.get("m_glyphInfoList"))
    has_new_glyphs = new_glyph_count > 0
    has_old_glyphs = old_glyph_count > 0

    has_new_face = isinstance(data.get("m_FaceInfo"), dict)
    has_old_face = isinstance(data.get("m_fontInfo"), dict)
    has_new_atlas = _first_atlas_ref(data.get("m_AtlasTextures")) is not None
    has_old_atlas = isinstance(data.get("atlas"), dict)

    if has_new_glyphs != has_old_glyphs:
        return "new" if has_new_glyphs else "old"
    if new_glyph_count != old_glyph_count:
        return "new" if new_glyph_count > old_glyph_count else "old"
    if has_new_face != has_old_face:
        return "new" if has_new_face else "old"
    if has_new_atlas != has_old_atlas:
        return "new" if has_new_atlas else "old"

    hint = _tmp_version_hint(unity_version)
    if hint is not None:
        return hint

    if has_new_face or has_new_atlas or "m_CharacterTable" in data:
        return "new"
    if has_old_face or has_old_atlas:
        return "old"
    return "new"


def inspect_tmp_font_schema(
    data: JsonDict,
    unity_version: str | None = None,
) -> dict[str, Any]:
    """KR: TMP 스키마 판별 결과와 glyph/atlas 메타를 통합해 반환합니다.
    EN: Return TMP schema result with unified glyph/atlas metadata.
    """
    target_version = detect_tmp_version(data, unity_version=unity_version)
    new_glyph_count = _safe_list_len(data.get("m_GlyphTable"))
    old_glyph_count = _safe_list_len(data.get("m_glyphInfoList"))
    has_new_face = isinstance(data.get("m_FaceInfo"), dict)
    has_old_face = isinstance(data.get("m_fontInfo"), dict)
    new_atlas_ref = _first_atlas_ref(data.get("m_AtlasTextures"))
    old_atlas_ref = (
        cast(JsonDict | None, data.get("atlas"))
        if isinstance(data.get("atlas"), dict)
        else None
    )

    if target_version == "new":
        glyph_count = new_glyph_count if new_glyph_count > 0 else old_glyph_count
        atlas_ref = _best_atlas_ref(data, prefer_new=True)
    else:
        glyph_count = old_glyph_count if old_glyph_count > 0 else new_glyph_count
        atlas_ref = _best_atlas_ref(data, prefer_new=False)

    atlas_file_id, atlas_path_id = _atlas_ref_ids(atlas_ref)

    is_tmp = bool(
        new_glyph_count > 0
        or old_glyph_count > 0
        or has_new_face
        or has_old_face
        or new_atlas_ref is not None
        or old_atlas_ref is not None
    )
    return {
        "version": target_version,
        "is_tmp": is_tmp,
        "glyph_count": int(glyph_count),
        "atlas_file_id": int(atlas_file_id),
        "atlas_path_id": int(atlas_path_id),
    }


def is_tmp_font_asset(obj: Any) -> bool:
    """KR: MonoBehaviour 객체가 TMP 폰트 에셋인지 판별합니다.
    EN: Determine whether a MonoBehaviour object is a TMP font asset.
    """
    try:
        parse_obj = obj.parse_as_object()
        if hasattr(parse_obj, "get_type") and parse_obj.get_type() == "TMP_FontAsset":
            return True
    except Exception as e:  # pragma: no cover
        _debug_parse_log(
            f"[export_fonts] parse_as_object failed (PathID: {obj.path_id}): {e}"
        )

    try:
        parse_dict = obj.parse_as_dict()
    except Exception as e:  # pragma: no cover
        _debug_parse_log(
            f"[export_fonts] parse_as_dict failed (PathID: {obj.path_id}): {e}"
        )
        return False

    unity_version_hint = getattr(
        getattr(obj, "assets_file", None), "unity_version", None
    )
    info = inspect_tmp_font_schema(
        parse_dict,
        unity_version=str(unity_version_hint) if unity_version_hint else None,
    )
    return bool(info.get("is_tmp"))


def extract_tmp_refs(parse_dict: JsonDict) -> dict[str, int] | None:
    """KR: TMP 폰트 데이터에서 atlas/material FileID/PathID를 추출합니다.
    EN: Extract atlas/material FileID/PathID pairs from TMP font data.
    """
    info = inspect_tmp_font_schema(parse_dict)
    glyph_count = int(info.get("glyph_count", 0) or 0)
    version = str(info.get("version", "new"))
    atlas_ref = _best_atlas_ref(parse_dict, prefer_new=(version == "new"))
    file_id, path_id = _atlas_ref_ids(atlas_ref)
    if path_id <= 0:
        fallback_atlas_ref = _best_atlas_ref(parse_dict, prefer_new=(version != "new"))
        file_id, path_id = _atlas_ref_ids(fallback_atlas_ref)

    if glyph_count == 0:
        return None
    if file_id == 0 and path_id == 0:
        return None

    # KR: 외부 참조 stub(FileID!=0, PathID=0)은 실제 텍스처를 가리키지 않으므로 제외합니다.
    # EN: Skip external stubs (FileID!=0, PathID=0) because they do not point to a real texture.
    if file_id != 0 and path_id == 0:
        return None

    material_ref = parse_dict.get("m_Material")
    if not isinstance(material_ref, dict):
        alt_material = parse_dict.get("material")
        material_ref = alt_material if isinstance(alt_material, dict) else {}
    material_file_id, material_path_id = _atlas_ref_ids(material_ref)

    creation_settings = parse_dict.get("m_CreationSettings")
    if isinstance(creation_settings, dict):
        # KR: 교체/추출 시 문자열 시퀀스 잡음을 방지하기 위해 빈 문자열로 정규화합니다.
        # EN: Normalize characterSequence to empty to avoid noisy serialized diffs.
        creation_settings["characterSequence"] = ""

    return {
        "atlas_file_id": file_id,
        "atlas_path_id": path_id,
        "material_file_id": material_file_id,
        "material_path_id": material_path_id,
    }


def export_fonts(
    game_path: str,
    data_path: str,
    output_dir: str | None = None,
    lang: Language = "ko",
) -> int:
    """KR: 게임 내 TMP SDF 폰트 JSON/PNG를 추출합니다.
    EN: Export TMP SDF font JSON/PNG assets from a game.
    """
    if output_dir is None:
        output_dir = os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    unity_version = get_unity_version(data_path)
    assets_files = find_assets_files(data_path)
    compile_method = get_compile_method(data_path)
    generator = create_generator(
        unity_version, game_path, data_path, compile_method, lang
    )

    if lang == "ko":
        _log_console(f"게임 경로: {game_path}")
        _log_console(f"데이터 경로: {data_path}")
        _log_console(f"Unity 버전: {unity_version}")
        _log_console(f"컴파일 방식: {compile_method}")
        _log_console(f"출력 폴더: {output_dir}")
    else:
        _log_console(f"Game path: {game_path}")
        _log_console(f"Data path: {data_path}")
        _log_console(f"Unity version: {unity_version}")
        _log_console(f"Compile method: {compile_method}")
        _log_console(f"Output folder: {output_dir}")
    _log_console()

    exported_count = 0
    asset_file_index = _build_asset_file_index(assets_files, data_path)
    asset_path_by_key = cast(dict[str, str], asset_file_index.get("path_by_key", {}))
    basename_to_keys = cast(
        dict[str, list[str]],
        asset_file_index.get("basename_to_keys", {}),
    )
    texture_targets_by_outer: dict[str, dict[str, str]] = {}
    material_targets_by_outer: dict[str, dict[str, str]] = {}
    outer_display_by_key: dict[str, str] = {}
    duplicate_asset_names: dict[str, list[str]] = {
        basename: [asset_path_by_key[key] for key in keys if key in asset_path_by_key]
        for basename, keys in basename_to_keys.items()
        if len(keys) > 1
    }

    if duplicate_asset_names:
        for duplicate_name, duplicate_paths in sorted(duplicate_asset_names.items()):
            if lang == "ko":
                _log_console(
                    f"경고: 동일 basename 에셋이 여러 개 있습니다: '{duplicate_name}'. "
                    f"가능하면 외부 참조 path를 이용해 해석하지만, 모호하면 첫 경로를 사용합니다. "
                    f"경로 목록: {duplicate_paths}"
                )
            else:
                _log_console(
                    f"Warning: duplicate asset basename '{duplicate_name}' detected; "
                    f"the exporter will try external ref paths first, but falls back to the first match when ambiguous. "
                    f"Paths: {duplicate_paths}"
                )

    for outer_key, outer_path in asset_path_by_key.items():
        assets_file = outer_path
        outer_name = os.path.basename(assets_file)
        outer_display_by_key.setdefault(outer_key, outer_name)
        try:
            env = UnityPy.load(assets_file)
            env.typetree_generator = generator
        except Exception as e:  # pragma: no cover
            if lang == "ko":
                _log_console(
                    f"경고: 파일 로드 실패 '{os.path.basename(assets_file)}': {e}"
                )
            else:
                _log_console(
                    f"Warning: failed to load '{os.path.basename(assets_file)}': {e}"
                )
            continue

        local_assets_keys: set[str] = {outer_key}
        for env_obj in env.objects:
            asset_key = _normalize_assets_key(
                getattr(getattr(env_obj, "assets_file", None), "name", None)
            )
            if asset_key:
                local_assets_keys.add(asset_key)

        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                if not is_tmp_font_asset(obj):
                    continue
                parse_dict = obj.parse_as_dict()
                refs = extract_tmp_refs(parse_dict)
                if not refs:
                    continue

                objname = obj.peek_name() or f"TMP_FontAsset_{obj.path_id}"
                source_assets_file = getattr(obj, "assets_file", None)
                atlas_target_assets_name = _resolve_assets_name_from_file_id(
                    source_assets_file, refs["atlas_file_id"]
                ) or _resolve_assets_name_from_file_id(
                    source_assets_file, 0
                )
                atlas_target_outer_key = _resolve_target_outer_key(
                    outer_key,
                    source_assets_file,
                    refs["atlas_file_id"],
                    atlas_target_assets_name,
                    local_assets_keys=local_assets_keys,
                    asset_file_index=asset_file_index,
                )
                material_target_assets_name = _resolve_assets_name_from_file_id(
                    source_assets_file, refs["material_file_id"]
                ) or _resolve_assets_name_from_file_id(
                    source_assets_file, 0
                )
                material_target_outer_key = _resolve_target_outer_key(
                    outer_key,
                    source_assets_file,
                    refs["material_file_id"],
                    material_target_assets_name,
                    local_assets_keys=local_assets_keys,
                    asset_file_index=asset_file_index,
                )
                if lang == "ko":
                    _log_console(f"SDF 폰트 발견: {objname} (PathID: {obj.path_id})")
                    _log_console(
                        f"  Atlas 텍스처: FileID {refs['atlas_file_id']} / "
                        f"PathID {refs['atlas_path_id']} -> {atlas_target_assets_name}"
                    )
                    _log_console(
                        f"  머티리얼: FileID {refs['material_file_id']} / "
                        f"PathID {refs['material_path_id']} -> {material_target_assets_name}"
                    )
                else:
                    _log_console(f"SDF font found: {objname} (PathID: {obj.path_id})")
                    _log_console(
                        f"  Atlas texture: FileID {refs['atlas_file_id']} / "
                        f"PathID {refs['atlas_path_id']} -> {atlas_target_assets_name}"
                    )
                    _log_console(
                        f"  Material: FileID {refs['material_file_id']} / "
                        f"PathID {refs['material_path_id']} -> {material_target_assets_name}"
                    )

                if atlas_target_assets_name and atlas_target_outer_key:
                    outer_display_by_key.setdefault(
                        atlas_target_outer_key,
                        os.path.basename(
                            asset_path_by_key.get(atlas_target_outer_key, assets_file)
                        ),
                    )
                    texture_targets_by_outer.setdefault(atlas_target_outer_key, {})[
                        _make_assets_object_key(
                            atlas_target_assets_name,
                            refs["atlas_path_id"],
                        )
                    ] = objname.replace(" SDF", " SDF Atlas")

                if (
                    refs["material_path_id"] > 0
                    and material_target_assets_name
                    and material_target_outer_key
                ):
                    outer_display_by_key.setdefault(
                        material_target_outer_key,
                        os.path.basename(
                            asset_path_by_key.get(material_target_outer_key, assets_file)
                        ),
                    )
                    material_targets_by_outer.setdefault(material_target_outer_key, {})[
                        _make_assets_object_key(
                            material_target_assets_name,
                            refs["material_path_id"],
                        )
                    ] = objname

                json_path = os.path.join(output_dir, f"{objname}.json")
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(parse_dict, indent=4, ensure_ascii=False, fp=f)
                if lang == "ko":
                    _log_console(f"  -> {objname}.json 저장됨")
                else:
                    _log_console(f"  -> {objname}.json saved")
                exported_count += 1
            except Exception as e:  # pragma: no cover
                if lang == "ko":
                    _log_console(
                        f"경고: TMP 파싱 실패 (파일: {os.path.basename(assets_file)}, PathID: {obj.path_id}): {e}"
                    )
                else:
                    _log_console(
                        f"Warning: TMP parse failed (file: {os.path.basename(assets_file)}, PathID: {obj.path_id}): {e}"
                    )

    unresolved_texture_targets = {
        outer_key: dict(bucket)
        for outer_key, bucket in texture_targets_by_outer.items()
        if bucket
    }
    unresolved_material_targets = {
        outer_key: dict(bucket)
        for outer_key, bucket in material_targets_by_outer.items()
        if bucket
    }

    for outer_key, assets_file in asset_path_by_key.items():
        outer_name = os.path.basename(assets_file)
        texture_bucket = unresolved_texture_targets.get(outer_key)
        material_bucket = unresolved_material_targets.get(outer_key)
        if not texture_bucket and not material_bucket:
            continue

        try:
            env = UnityPy.load(assets_file)
            env.typetree_generator = generator
        except Exception as e:  # pragma: no cover
            if lang == "ko":
                _log_console(
                    f"경고: 추출 대상 파일 로드 실패 '{os.path.basename(assets_file)}': {e}"
                )
            else:
                _log_console(
                    f"Warning: failed to load export target '{os.path.basename(assets_file)}': {e}"
                )
            continue

        for obj in env.objects:
            try:
                object_assets_name = _resolve_assets_name_from_file_id(
                    getattr(obj, "assets_file", None), 0
                ) or outer_name
                object_key = _make_assets_object_key(object_assets_name, obj.path_id)

                if obj.type.name == "Texture2D" and texture_bucket:
                    export_name = texture_bucket.pop(object_key, None)
                    if export_name is None:
                        continue
                    tex = obj.parse_as_object()
                    image = tex.image
                    if lang == "ko":
                        _log_console(
                            f"텍스처 추출: {export_name} "
                            f"({object_assets_name}, PathID: {obj.path_id})"
                        )
                    else:
                        _log_console(
                            f"Extracting texture: {export_name} "
                            f"({object_assets_name}, PathID: {obj.path_id})"
                        )
                    png_path = os.path.join(output_dir, f"{export_name}.png")
                    image.save(png_path)
                    if lang == "ko":
                        _log_console(f"  -> {export_name}.png 저장됨")
                    else:
                        _log_console(f"  -> {export_name}.png saved")
                elif obj.type.name == "Material" and material_bucket:
                    source_font_name = material_bucket.pop(object_key, None)
                    if source_font_name is None:
                        continue
                    mat = obj.parse_as_dict()
                    mat_name = obj.peek_name() or f"Material_{obj.path_id}"
                    mat_path = os.path.join(output_dir, f"{mat_name}.json")
                    with open(mat_path, "w", encoding="utf-8") as f:
                        json.dump(mat, f, indent=4, ensure_ascii=False)
                    if lang == "ko":
                        _log_console(
                            f"머티리얼 추출: {mat_name} "
                            f"({object_assets_name}, PathID: {obj.path_id}, source: {source_font_name})"
                        )
                    else:
                        _log_console(
                            f"Extracting material: {mat_name} "
                            f"({object_assets_name}, PathID: {obj.path_id}, source: {source_font_name})"
                        )
            except Exception as e:  # pragma: no cover
                if lang == "ko":
                    _log_console(
                        f"경고: 추출 중 오류 (파일: {os.path.basename(assets_file)}, PathID: {obj.path_id}): {e}"
                    )
                else:
                    _log_console(
                        f"Warning: export error (file: {os.path.basename(assets_file)}, PathID: {obj.path_id}): {e}"
                    )

        if texture_bucket is not None and not texture_bucket:
            unresolved_texture_targets.pop(outer_key, None)
        if material_bucket is not None and not material_bucket:
            unresolved_material_targets.pop(outer_key, None)

    for outer_key, bucket in sorted(unresolved_texture_targets.items()):
        if not bucket:
            continue
        display_name = outer_display_by_key.get(outer_key, outer_key)
        if lang == "ko":
            _log_console(
                f"경고: 텍스처 참조 {len(bucket)}개를 찾지 못했습니다 ({display_name})"
            )
        else:
            _log_console(
                f"Warning: {len(bucket)} texture reference(s) could not be resolved ({display_name})"
            )

    for outer_key, bucket in sorted(unresolved_material_targets.items()):
        if not bucket:
            continue
        display_name = outer_display_by_key.get(outer_key, outer_key)
        if lang == "ko":
            _log_console(
                f"경고: 머티리얼 참조 {len(bucket)}개를 찾지 못했습니다 ({display_name})"
            )
        else:
            _log_console(
                f"Warning: {len(bucket)} material reference(s) could not be resolved ({display_name})"
            )

    return exported_count


def main_cli(lang: Language = "ko") -> None:
    """KR: SDF 폰트 추출 CLI 진입점입니다.
    EN: CLI entry point for SDF font export.
    """
    _configure_logging()
    parser = argparse.ArgumentParser(
        description=(
            "Unity 게임에서 TMP SDF 폰트를 추출합니다."
            if lang == "ko"
            else "Export TMP SDF fonts from a Unity game."
        )
    )
    parser.add_argument(
        "gamepath",
        nargs="?",
        help=(
            "게임 루트 경로 또는 _Data 경로 (예: D:\\Games\\Muck)"
            if lang == "ko"
            else "Game root path or _Data path (e.g. D:\\Games\\Muck)"
        ),
    )
    args = parser.parse_args()

    if lang == "ko":
        _log_console("=== Unity SDF 폰트 추출기 ===")
    else:
        _log_console("=== Unity SDF Font Exporter ===")
    _log_console()

    input_path = args.gamepath
    if not input_path:
        if lang == "ko":
            input_path = input("게임 경로를 입력하세요: ").strip()
            if not input_path:
                exit_with_error(lang, "게임 경로가 필요합니다.")
        else:
            input_path = input("Enter game path: ").strip()
            if not input_path:
                exit_with_error(lang, "Game path is required.")

    if not os.path.isdir(input_path):
        if lang == "ko":
            exit_with_error(lang, f"'{input_path}'는 유효한 디렉토리가 아닙니다.")
        else:
            exit_with_error(lang, f"'{input_path}' is not a valid directory.")

    try:
        game_path, data_path = resolve_game_path(lang, input_path)
        exported_count = export_fonts(game_path, data_path, lang=lang)
    except Exception as e:
        exit_with_error(lang, str(e))

    _log_console()
    if lang == "ko":
        _log_console(f"완료! {exported_count}개의 SDF 폰트가 추출되었습니다.")
        input("\n엔터를 눌러 종료...")
    else:
        _log_console(f"Done! Exported {exported_count} SDF font(s).")
        input("\nPress Enter to exit...")
