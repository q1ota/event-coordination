"""Adapter for trusted bot handlers; never pass user-supplied actor IDs here.

The transport must authenticate Telegram updates and supply their sender ID.
This module is not a webhook authenticator or a public RPC API.
"""
from datetime import date

from .admission import AdmissionPolicy, AdmissionService
from .authorization import AuthorizationError, AuthorizationService
from .models import Role
from .registration import RegistrationService
from .repository import IdentityRepository
from .scopes import ScopePolicy
from .consent import ConsentService


class IdentityGateway:
    def __init__(self, repository: IdentityRepository, *, policy: AdmissionPolicy,
                 review_enabled: bool = False,
                 grantable_roles: frozenset[Role] = frozenset(),
                 scope_policy: ScopePolicy | None = None):
        if repository.age_policy is None or not isinstance(policy, AdmissionPolicy):
            raise ValueError("Explicit age and admission policies are required")
        self._repository = repository
        self._policy = policy
        self._registration = RegistrationService(repository)
        self._authorization = AuthorizationService(repository,
            grantable_roles=grantable_roles, scope_policy=scope_policy)
        self._admission = AdmissionService(repository, policy=policy,
            review_enabled=review_enabled)
        self._consent = ConsentService(repository, policy=policy)
        self._consent.ensure_policy()

    @staticmethod
    def _validate_sender(sender_id: int) -> None:
        if type(sender_id) is not int or sender_id <= 0:
            raise AuthorizationError("Invalid trusted sender ID")

    def _own_id(self, sender_id: int) -> str:
        self._validate_sender(sender_id)
        with self._repository.transaction() as db:
            row = db.execute("SELECT id FROM users WHERE telegram_user_id = ?",
                             (sender_id,)).fetchone()
            if row is None:
                raise AuthorizationError("Unknown user")
            return row["id"]

    def resolve_actor(self, sender_id: int, /) -> str:
        self._validate_sender(sender_id)
        return self._authorization.resolve_actor(sender_id)

    def start_registration(self, sender_id: int, /):
        self._validate_sender(sender_id)
        return self._registration.start_registration(sender_id)

    def get_my_profile(self, sender_id: int, /):
        return self._registration.get_profile(self._own_id(sender_id))

    def update_my_profile(self, sender_id: int, /, *, display_name: str,
                          territory_id: str, birth_date: date,
                          interests: tuple[str, ...], help_types: tuple[str, ...]):
        if type(birth_date) is not date:
            raise ValueError("Birth date is required")
        return self._registration.update_profile(self._own_id(sender_id),
            display_name=display_name, territory_id=territory_id,
            birth_date=birth_date, interests=interests, help_types=help_types)

    def accept_my_consent(self, sender_id: int, /, *, consent_type: str,
                          version: str) -> None:
        versions = {"rules": self._policy.rules_version,
                    "personal_data": self._policy.personal_data_version}
        if consent_type not in versions or version != versions[consent_type]:
            raise ValueError("Unknown or outdated consent version")
        self._consent.accept(self._own_id(sender_id), consent_type, version)

    def withdraw_my_consent(self, sender_id: int, /, *, consent_type: str) -> bool:
        return self._consent.withdraw(self._own_id(sender_id), consent_type)

    def submit_my_registration(self, sender_id: int, /):
        return self._registration.submit_registration(self._own_id(sender_id),
            rules_version=self._policy.rules_version,
            personal_data_version=self._policy.personal_data_version)

    def submit_my_parental_consent(self, sender_id: int, /) -> str:
        self._validate_sender(sender_id)
        return self._admission.submit_parental_consent(sender_id)

    def approve_registration(self, sender_id: int, /, *, target_id: str, reason: str):
        return self._admission.approve_registration(self.resolve_actor(sender_id), target_id, reason=reason)

    def reject_registration(self, sender_id: int, /, *, target_id: str, reason: str):
        return self._admission.reject_registration(self.resolve_actor(sender_id), target_id, reason=reason)

    def review_parental_consent(self, sender_id: int, /, *, consent_id: str, approved: bool, reason: str):
        return self._admission.review_parental_consent(self.resolve_actor(sender_id), consent_id,
            approved=approved, reason=reason)

    def revoke_parental_consent(self, sender_id: int, /, *, consent_id: str, reason: str):
        return self._admission.revoke_parental_consent(self.resolve_actor(sender_id), consent_id, reason=reason)

    def revoke_access(self, sender_id: int, /, *, target_id: str, reason: str):
        return self._admission.revoke_access(self.resolve_actor(sender_id), target_id, reason=reason)

    def grant_role(self, sender_id: int, /, *, target_id: str, role: Role, scope_id: str = "", reason: str):
        return self._authorization.grant_role(self.resolve_actor(sender_id), target_id, role,
            scope_id=scope_id, reason=reason)

    def revoke_role(self, sender_id: int, /, *, target_id: str, role: Role, scope_id: str = "", reason: str):
        return self._authorization.revoke_role(self.resolve_actor(sender_id), target_id, role,
            scope_id=scope_id, reason=reason)

    def list_my_roles(self, sender_id: int, /):
        actor = self.resolve_actor(sender_id)
        return self._authorization.list_roles(actor, actor)
