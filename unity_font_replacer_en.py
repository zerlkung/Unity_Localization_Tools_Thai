"""KR: Unity Font Replacer 영문 CLI 런처.
    unity_font_replacer_core의 run_main_en()을 호출하여
    영문 인터페이스로 폰트 교체 파이프라인을 실행한다.
EN: Unity Font Replacer English CLI launcher.
    Calls run_main_en() from unity_font_replacer_core to run
    the font replacement pipeline with an English interface.
"""

import logging
import sys

from unity_font_replacer_core import run_main_en

logger = logging.getLogger(__name__)


def main() -> None:
    """KR: 영문 CLI 엔트리포인트.
    EN: English CLI entry point.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_main_en()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.exception("An unexpected error occurred: %s", error)
        input("\nPress Enter to exit...")
        sys.exit(1)
