from datetime import datetime, timezone

from .errors import ValidationError


def text(value: str, field: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError(f"{field} must contain 1..{maximum} characters")
    return value.strip()


def timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValidationError(f"{field} must be an ISO timestamp with timezone") from exc


def version(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValidationError("expected_version must be a positive integer")
    return value


def pagination(limit: int, offset: int = 0) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("limit must be between 1 and 100")
    if type(offset) is not int or offset < 0:
        raise ValidationError("offset must be a nonnegative integer")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
