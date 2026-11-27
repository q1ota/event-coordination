
import json

from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from event_coordination.identity.repository import IdentityRepository
from event_coordination.identity.registration import RegistrationService


def run(database: Path) -> dict:
    repository = IdentityRepository(database)
    registration = RegistrationService(repository)

    registration.add_territory(
        "kazan",
        "Республика Татарстан",
        "Казань",
    )

    registration.add_direction("culture", "Культура")
    registration.add_direction("sport", "Спорт")

    user = registration.start_registration(123456789)

    registration.update_profile(
        user.user_id,
        display_name="Test Participant",
        territory_id="kazan",
        age=20,
        interests=("culture", "sport"),
        help_types=("transport", "organization"),
    )

    registration.accept_consent(
        user.user_id,
        "rules",
        "demo-v1",
    )

    registration.accept_consent(
        user.user_id,
        "personal_data",
        "demo-v1",
    )

    submitted = registration.submit_registration(
        user.user_id,
        rules_version="demo-v1",
        personal_data_version="demo-v1",
    )

    reopened = RegistrationService(
        IdentityRepository(database)
    )

    repeated = reopened.start_registration(123456789)

    return {
        "mode": "synthetic_local_demo",
        "profile": asdict(submitted),
        "duplicate_prevented": (
            user.user_id == repeated.user_id
        ),
        "profile_persisted": (
            submitted == repeated
        ),
        "access_granted": (
            repeated.status == "active"
        ),
    }


def main():
    with TemporaryDirectory() as folder:
        result = run(Path(folder) / "identity.sqlite")

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
