"""`ach-memory init <host>`: install the plugin into a coding-agent host."""

from pathlib import Path


def _plugins_root() -> Path:
    """Wheel ships `plugins/` as `memory/plugins/`; a checkout keeps it at the repo root."""
    packaged = Path(__file__).parent / "plugins"
    return packaged if packaged.exists() else Path(__file__).parents[2] / "plugins"
