from dataclasses import dataclass
from datetime import date

from .models import Role


COORDINATION_ROLES = frozenset({
    Role.GENERAL_COORDINATOR,
    Role.TERRITORY_COORDINATOR,
    Role.COUNCIL_REPRESENTATIVE,
})


def age_on(birth_date: date, today: date | None = None) -> int:
    if type(birth_date) is not date:
        raise ValueError("Invalid birth date")
    today = today if today is not None else date.today()
    if birth_date > today:
        raise ValueError("Birth date cannot be in the future")
    age = today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )
    if age > 120:
        raise ValueError("Invalid age")
    return age


def effective_age(row) -> int | None:
    if row["birth_date"] is not None:
        return age_on(date.fromisoformat(row["birth_date"]))
    return row["age"]


@dataclass(frozen=True)
class AgePolicy:
    minimum_age: int = 12
    parental_consent_age: int = 16
    coordinator_minimum_age: int = 18
    minors_enabled: bool = False

    def __post_init__(self):
        ages = (self.minimum_age, self.parental_consent_age,
                self.coordinator_minimum_age)
        if any(type(value) is not int for value in ages):
            raise ValueError("Age limits must be integers")
        if not 0 <= self.minimum_age <= self.parental_consent_age <= 18:
            raise ValueError("Invalid registration or consent age")
        if not 18 <= self.coordinator_minimum_age <= 120:
            raise ValueError("Invalid coordinator age")
        if type(self.minors_enabled) is not bool:
            raise ValueError("Invalid minor admission setting")

    def allows_registration(self, age: int | None) -> bool:
        return type(age) is int and self.minimum_age <= age <= 120

    def allows_admission(self, age: int | None) -> bool:
        return self.allows_registration(age) and (age >= 18 or self.minors_enabled)

    def requires_parental_consent(self, age: int) -> bool:
        return self.minimum_age <= age < self.parental_consent_age

    def allows_role(self, age: int | None, role: Role) -> bool:
        return self.allows_admission(age) and (
            role not in COORDINATION_ROLES or age >= self.coordinator_minimum_age
        )
