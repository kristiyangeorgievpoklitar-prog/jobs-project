"""The contract every job source implements.

Adding a second board means writing one class against this protocol; nothing
downstream (normalization, classification, matching, UI) changes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jobhunter.domain.schemas import RawJob


@runtime_checkable
class JobSource(Protocol):
    """A discoverable source of job listings."""

    name: str

    def search(
        self,
        *,
        location: str | None = None,
        keywords: str | None = None,
        entry_level_only: bool = False,
        max_results: int = 100,
    ) -> list[RawJob]:
        """Return listings matching the criteria, as scraped."""
        ...

    def fetch_detail(self, url: str) -> RawJob | None:
        """Fetch and parse one listing's full detail page."""
        ...
