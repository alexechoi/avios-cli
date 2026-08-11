"""Console-script wrapper for ``avios-mcp``.

Kept separate from :mod:`avios.mcp_server` so that a missing ``mcp`` extra produces
one actionable line on stderr instead of an ``ImportError`` traceback — the server
is normally launched by an MCP client, which shows the user very little.
"""

from __future__ import annotations

import sys
from importlib.util import find_spec

INSTALL_HINT = (
    "avios-mcp needs the `mcp` extra, which is not installed.\n"
    "\n"
    "  uv tool install 'avios-cli[mcp]'\n"
    "\n"
    "or run it without installing:\n"
    "\n"
    "  uvx --from 'avios-cli[mcp]' avios-mcp"
)


def main() -> None:
    """Serve the Avios MCP server over stdio."""
    if find_spec("mcp") is None:
        print(INSTALL_HINT, file=sys.stderr)
        raise SystemExit(1)

    from avios.mcp_server import mcp

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
