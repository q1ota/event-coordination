
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
    birth_date: str | None = None



class Role(StrEnum):
    PARTICIPANT = "participant"
    EVENT_RESPONSIBLE = "event_responsible"
    TERRITORY_COORDINATOR = "territory_coordinator"
    COUNCIL_REPRESENTATIVE = "council_representative"
    GENERAL_COORDINATOR = "general_coordinator"
    TECH_ADMIN = "tech_admin"


@dataclass(frozen=True)
class RoleAssignment:
    id: str
    user_id: str
    role: Role
    scope_id: str
    granted_by: str
    granted_at: str
    revoked_by: str | None
    revoked_at: str | None
