
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from event_coordination.identity.models import RegistrationStatus
from event_coordination.identity.repository import IdentityRepository
from event_coordination.identity.registration import RegistrationService


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)

        self.path = Path(folder.name) / "identity.sqlite"

        self.repository = IdentityRepository(self.path)
        self.service = RegistrationService(self.repository)

        self.service.add_territory(
            "kazan", "Tatarstan", "Kazan"
        )

        self.service.add_direction("culture", "Culture")
        self.service.add_direction("sport", "Sport")

    def fill_profile(self, user_id):
        return self.service.update_profile(
            user_id,
            display_name="Test User",
            territory_id="kazan",
            age=20,
            interests=("culture", "sport"),
            help_types=("transport",),
        )

    def accept_consents(self, user_id):
        self.service.accept_consent(
            user_id, "rules", "v1"
        )

        self.service.accept_consent(
            user_id, "personal_data", "v1"
        )

    def test_registration_creates_draft(self):
        user = self.service.start_registration(123456)

        self.assertEqual(
            RegistrationStatus.DRAFT,
            user.status,
        )

        self.assertIsNone(user.display_name)

    def test_duplicate_registration_is_prevented(self):
        first = self.service.start_registration(123456)
        second = self.service.start_registration(123456)

        self.assertEqual(first.user_id, second.user_id)

        with self.repository.transaction() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

        self.assertEqual(1, count)

    def test_profile_is_saved(self):
        user = self.service.start_registration(123456)

        updated = self.fill_profile(user.user_id)

        self.assertEqual("Test User", updated.display_name)
        self.assertEqual(20, updated.age)
        self.assertEqual("kazan", updated.territory_id)
        self.assertEqual(
            ("culture", "sport"),
            updated.interests,
        )

    def test_incomplete_registration_is_rejected(self):
        user = self.service.start_registration(123456)

        with self.assertRaises(ValueError):
            self.service.submit_registration(
                user.user_id,
                rules_version="v1",
                personal_data_version="v1",
            )

    def test_missing_consent_is_rejected(self):
        user = self.service.start_registration(123456)

        self.fill_profile(user.user_id)

        with self.assertRaises(ValueError):
            self.service.submit_registration(
                user.user_id,
                rules_version="v1",
                personal_data_version="v1",
            )

    def test_completed_profile_becomes_pending(self):
        user = self.service.start_registration(123456)

        self.fill_profile(user.user_id)
        self.accept_consents(user.user_id)

        result = self.service.submit_registration(
            user.user_id,
            rules_version="v1",
            personal_data_version="v1",
        )

        self.assertEqual(
            RegistrationStatus.PENDING,
            result.status,
        )

    def test_profile_persists_after_restart(self):
        user = self.service.start_registration(123456)

        self.fill_profile(user.user_id)

        reopened = RegistrationService(
            IdentityRepository(self.path)
        )

        result = reopened.get_profile(user.user_id)

        self.assertEqual("Test User", result.display_name)
        self.assertEqual(20, result.age)

    def test_invalid_telegram_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.start_registration(-1)

    def test_unknown_territory_is_rejected(self):
        user = self.service.start_registration(123456)

        with self.assertRaises(ValueError):
            self.service.update_profile(
                user.user_id,
                display_name="Test User",
                territory_id="unknown",
                age=20,
                interests=("culture",),
                help_types=("transport",),
            )

    def test_unknown_direction_is_rejected(self):
        user = self.service.start_registration(123456)

        with self.assertRaises(ValueError):
            self.service.update_profile(
                user.user_id,
                display_name="Test User",
                territory_id="kazan",
                age=20,
                interests=("unknown",),
                help_types=("transport",),
            )

    def test_duplicate_consent_is_prevented(self):
        user = self.service.start_registration(123456)

        self.service.accept_consent(
            user.user_id, "rules", "v1"
        )

        self.service.accept_consent(
            user.user_id, "rules", "v1"
        )

        with self.repository.transaction() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM consents"
            ).fetchone()[0]

        self.assertEqual(1, count)

    def test_outdated_consent_is_rejected(self):
        user = self.service.start_registration(123456)

        self.fill_profile(user.user_id)
        self.accept_consents(user.user_id)

        with self.assertRaises(ValueError):
            self.service.submit_registration(
                user.user_id,
                rules_version="v2",
                personal_data_version="v1",
            )


if __name__ == "__main__":
    unittest.main()
