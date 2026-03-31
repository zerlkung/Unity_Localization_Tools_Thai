"""KR: Unity Font Replacer 한국어 CLI 런처.
    unity_font_replacer_core의 run_main_ko()를 호출하여
    한국어 인터페이스로 폰트 교체 파이프라인을 실행한다.
EN: Unity Font Replacer Korean CLI launcher.
    Calls run_main_ko() from unity_font_replacer_core to run
    the font replacement pipeline with a Korean interface.
"""

import logging

from unity_font_replacer_core import run_main_ko

logger = logging.getLogger(__name__)


def main() -> None:
    """KR: 한국어 CLI 엔트리포인트.
    EN: Korean CLI entry point.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_main_ko()


if __name__ == "__main__":
    main()
