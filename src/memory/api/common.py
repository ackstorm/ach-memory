from pydantic import BaseModel, model_validator


class RenameForwarding(BaseModel):
    """Shared contract: notice is set if and only if resolved_from is set.

    resolved_from is populated when a caller's request followed a rename
    tombstone to reach the project (SPEC §8.6). Mixed into any response model
    that can carry that outcome so the notice/resolved_from pairing has one
    definition instead of a hand-copied ternary at every call site.
    """

    resolved_from: str | None = None
    notice: str | None = None

    @model_validator(mode="after")
    def _derive_notice(self) -> "RenameForwarding":
        self.notice = "PROJECT_RENAMED" if self.resolved_from else None
        return self
