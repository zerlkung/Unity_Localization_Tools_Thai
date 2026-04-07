"""KR: JSON 교체 설정에서 대상 파일명 목록을 추출하여 TXT로 저장한다.
사용법:
      python extract_file_names_to_txt.py <replacements.json>
      또는 스크립트 위에 JSON 파일을 드래그 앤 드롭.
입력 JSON 구조에서 모든 "File" 키의 값을 재귀적으로 수집한 뒤,
    중복을 제거하고 쉼표로 연결하여 .txt 파일로 출력한다.
    이 목록은 --target-file 인자에 직접 사용할 수 있다.

EN: Extract target file name list from a JSON replacement config and save as TXT.
Usage:
      python extract_file_names_to_txt.py <replacements.json>
      Or drag-and-drop a JSON file onto this script.
Recursively collects all "File" key values from the input JSON structure,
    deduplicates them, joins with commas, and writes to a .txt file.
    The resulting list can be used directly with the --target-file argument.
"""

import json
import sys
from pathlib import Path
from typing import Any, Iterator


def iter_file_values(node: Any) -> Iterator[str]:
    """KR: JSON 트리를 재귀 탐색하여 "File" 키의 문자열 값을 yield 한다.
    교체 설정 JSON의 각 항목은 {"File": "assets_name", ...} 형태이며,
        중첩 구조(리스트/딕셔너리)도 모두 탐색한다.

    EN: Recursively traverse the JSON tree and yield string values of "File" keys.
    Each entry in the replacement config JSON has the form {"File": "assets_name", ...},
        and nested structures (lists/dicts) are all traversed.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "File" and isinstance(value, str):
                yield value
            yield from iter_file_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_file_values(item)


def main() -> None:
    """KR: 엔트리포인트: JSON에서 파일명을 추출해 쉼표 구분 TXT로 저장.
    EN: Entry point: extract file names from JSON and save as comma-separated TXT.
    """
    if len(sys.argv) < 2:
        print("Usage: drag-and-drop a JSON file onto this script.")
        print("Example: python extract_file_names_to_txt.py your_file.json")
        return

    input_path = Path(sys.argv[1])
    output_path = input_path.with_suffix(".txt")

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    with input_path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)

    files = list(iter_file_values(data))

    # KR: 순서를 유지하면서 중복 제거 (dict.fromkeys 활용)
    # EN: Deduplicate while preserving order (using dict.fromkeys)
    unique_files = list(dict.fromkeys(files))
    output_path.write_text(",".join(unique_files), encoding="utf-8")

    print(f"Saved {len(unique_files)} unique names to: {output_path}")


if __name__ == "__main__":
    main()
