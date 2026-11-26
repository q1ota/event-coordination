"""Trusted integration boundaries owned by registration and chat modules."""

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Identity:
    user_id: str
    display_name: str
    active: bool
    can_coordinate: bool = False
    territory_id: str = ""
    interests: tuple[str, ...] = ()
    help_types: tuple[str, ...] = ()


class IdentityDirectory(Protocol):
    def get(self, user_id: str) -> Identity | None:
        """Return current identity and effective admission, never caller roles."""
        ...

    def candidates(
        self, territory_id: str, interest: str | None, help_type: str | None
    ) -> Sequence[Identity]:
        """Return candidate identities without contact or consent documents."""
        ...


@dataclass(frozen=True)
class CompletionEvidence:
    report_id: str | None = None
    report_delivered: bool = False
    chat_archived: bool = False


class CompletionReader(Protocol):
    def get(self, event_id: str) -> CompletionEvidence:
        """Read confirmed report/chat state from the trusted integration."""
        ...


@dataclass(frozen=True)
class Receipt:
    resource_id: str
    version: int
