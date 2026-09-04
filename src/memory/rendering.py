from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RenderedSection:
    text: str
    refreshed_at: str | None
    content_fingerprint: str | None = None


def render_inert(text: str) -> str:
    return text.replace("<", "‹").replace(">", "›")


def format_age(updated_at: datetime, now: datetime) -> str:
    seconds = max(int((now - updated_at).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
