"""Unity Font Replacer — Thai font extractor CLI launcher.
Calls main_cli(lang="en") from export_fonts_core to extract
TMP SDF font JSON/PNG from game assets.
"""

import logging
import sys

from export_fonts_core import main_cli

logger = logging.getLogger(__name__)


def main() -> None:
    """Thai extractor CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main_cli(lang="en")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.exception("An unexpected error occurred: %s", error)
        input("\nPress Enter to exit...")
        sys.exit(1)
