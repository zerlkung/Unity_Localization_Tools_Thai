"""Unity Font Replacer - Thai asset workflow extractor launcher.

This launcher keeps the extractor UI in English while pairing with the
Thai-font workflow documented in this repository.
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
