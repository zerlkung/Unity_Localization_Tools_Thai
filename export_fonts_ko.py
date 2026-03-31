"""KR: TMP 폰트 에셋 추출기 한국어 CLI 런처.
    export_fonts_core의 main_cli(lang="ko")를 호출하여
    게임 에셋에서 TMP SDF 폰트 JSON/PNG를 한국어 인터페이스로 추출한다.
EN: TMP font asset extractor Korean CLI launcher.
    Calls main_cli(lang="ko") from export_fonts_core to extract
    TMP SDF font JSON/PNG from game assets with a Korean interface.
"""

import logging
import sys

from export_fonts_core import main_cli

logger = logging.getLogger(__name__)


def main() -> None:
    """KR: 한국어 추출기 CLI 엔트리포인트.
    EN: Korean extractor CLI entry point.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main_cli(lang="ko")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.exception("예상치 못한 오류가 발생했습니다: %s", error)
        input("\n엔터를 눌러 종료...")
        sys.exit(1)
