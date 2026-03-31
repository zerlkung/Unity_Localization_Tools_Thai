"""KR: TMP 폰트 에셋 추출기 영문 CLI 런처.
    export_fonts_core의 main_cli(lang="en")을 호출하여
    게임 에셋에서 TMP SDF 폰트 JSON/PNG를 영문 인터페이스로 추출한다.
EN: TMP font asset extractor English CLI launcher.
    Calls main_cli(lang="en") from export_fonts_core to extract
    TMP SDF font JSON/PNG from game assets with an English interface.
"""

import logging
import sys

from export_fonts_core import main_cli

logger = logging.getLogger(__name__)


def main() -> None:
    """KR: 영문 추출기 CLI 엔트리포인트.
    EN: English extractor CLI entry point.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main_cli(lang="en")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.exception("An unexpected error occurred: %s", error)
        input("\nPress Enter to exit...")
        sys.exit(1)
