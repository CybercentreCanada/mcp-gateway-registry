"""CLI entrypoint: write the resolved RUM snippet to a file at container start.

Invoked by ``docker/registry-entrypoint.sh``:

    python -m registry.utils.rum_snippet_writer /app/frontend/build/rum.js

The output path is passed as argv (not sensitive). The snippet and allowlist are
read from the environment (``RUM_SNIPPET_B64`` / ``RUM_ALLOWED_HOSTS``) so a
token-bearing snippet is never placed on argv, where it would be world-readable
via ``ps``. The resolver fails closed (see ``rum_snippet.resolve_rum_snippet``),
and this writer always exits 0 after writing a valid file so ``set -e`` in the
entrypoint never aborts container startup on a bad snippet.
"""

import argparse
import logging
import os
import sys

from registry.utils.rum_snippet import resolve_rum_snippet

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,  # Set the log level to INFO
    # Define log message format
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


def _write_rum_file(
    output_path: str,
    content: str,
) -> None:
    """Write the resolved snippet content to the output path.

    Args:
        output_path: Destination file (e.g. /app/frontend/build/rum.js).
        content: Resolved snippet or stub text.
    """
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("RUM: wrote %d bytes to %s", len(content), output_path)


def main() -> int:
    """Resolve RUM_SNIPPET_B64 (with allowlist) and write it to the given path.

    Returns:
        Process exit code (always 0; failures fall back to a stub file).
    """
    parser = argparse.ArgumentParser(
        description="Write the resolved RUM snippet to rum.js at container start.",
    )
    parser.add_argument(
        "output_path",
        help="Path to write the resolved rum.js file to.",
    )
    args = parser.parse_args()

    snippet_b64 = os.environ.get("RUM_SNIPPET_B64", "")
    allowed_hosts = os.environ.get("RUM_ALLOWED_HOSTS", "")

    content = resolve_rum_snippet(snippet_b64, allowed_hosts)

    try:
        _write_rum_file(args.output_path, content)
    except OSError as e:
        # Do not abort container startup; the FastAPI route serves an empty
        # stub when the file is missing, so RUM simply stays disabled.
        logger.error("RUM: failed to write %s: %s", args.output_path, e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
