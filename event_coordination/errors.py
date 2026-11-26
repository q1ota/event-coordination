class DomainError(Exception):
    """Expected application error; adapters translate it for their transport."""


class ValidationError(DomainError):
    pass


class Forbidden(DomainError):
    pass


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class IdempotencyConflict(Conflict):
    pass
