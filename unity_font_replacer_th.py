"""Unity Font Replacer - Thai asset preset launcher.

This launcher keeps the CLI text in English, but enables the Thai-focused
workflow documented in this repository (for example Sarabun and
Noto Sans Thai assets stored in TH_ASSETS/).
"""

import logging
import sys

from unity_font_replacer_core import run_main_en

logger = logging.getLogger(__name__)


def main() -> None:
    """Thai CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run_main_en()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.exception("An unexpected error occurred: %s", error)
        input("\nPress Enter to exit...")
        sys.exit(1)
