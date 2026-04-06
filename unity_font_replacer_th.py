"""Unity Font Replacer — Thai CLI launcher.
Calls run_main_en() from unity_font_replacer_core to run
the font replacement pipeline with Thai font support (Sarabun, Noto Sans Thai).

Place Thai TTF/SDF assets inside the TH_ASSETS/ folder next to this script.
Supported bulk modes: --sarabun, --notosansthai
"""

import logging

from unity_font_replacer_core import run_main_en

logger = logging.getLogger(__name__)


def main() -> None:
    """Thai CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_main_en()


if __name__ == "__main__":
    main()
