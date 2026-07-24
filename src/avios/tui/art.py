"""ASCII banner art for the TUI."""

from __future__ import annotations

from rich.text import Text

# "AVIOS" in the figlet "ANSI Shadow" style.
BANNER = r"""
 █████╗ ██╗   ██╗██╗ ██████╗ ███████╗
██╔══██╗██║   ██║██║██╔═══██╗██╔════╝
███████║██║   ██║██║██║   ██║███████╗
██╔══██║╚██╗ ██╔╝██║██║   ██║╚════██║
██║  ██║ ╚████╔╝ ██║╚██████╔╝███████║
╚═╝  ╚═╝  ╚═══╝  ╚═╝ ╚═════╝ ╚══════╝
"""

TAGLINE = "✈  your Avios, in the terminal"

# Teal → blue gradient applied line by line (evokes the Avios/BA palette).
_GRADIENT = ["#00d7af", "#00d7d7", "#00afd7", "#0087d7", "#005fd7", "#5f5fd7"]


def banner_text() -> Text:
    """Return the AVIOS banner as a colour-gradient Rich ``Text``."""
    text = Text(justify="center")
    lines = BANNER.strip("\n").splitlines()
    for index, line in enumerate(lines):
        color = _GRADIENT[min(index, len(_GRADIENT) - 1)]
        text.append(line + "\n", style=f"bold {color}")
    text.append(TAGLINE, style="italic #9e9e9e")
    return text
