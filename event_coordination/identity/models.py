
from dataclasses import dataclass
from enum import StrEnum


class RegistrationStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"
    REVOKED = "revoked"


@dataclass(frozen=True)
class Territory:
    id: str
    region: str
    locality: str


@dataclass(frozen=True)
class Direction:
    id: str
    name: str


@dataclass(frozen=True)
class UserProfile:
    user_id: str
    telegram_user_id: int
    display_name: str | None
    territory_id: str | None
    age: int | None
    interests: tuple[str, ...]
    help_types: tuple[str, ...]
    status: RegistrationStatus
    created_at: str
    updated_at: str
