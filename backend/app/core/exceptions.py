"""Structured application exception hierarchy with secret-safe error payloads."""

from typing import Any


class AppError(Exception):
    """Base exception for all controlled application errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INTERNAL_ERROR",
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class ConfigurationError(AppError):
    """Raised when startup or runtime configuration is invalid."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="CONFIGURATION_ERROR",
            status_code=500,
            details=details,
        )


class AuthenticationError(AppError):
    """Raised when operator authentication fails or credentials are invalid."""

    def __init__(
        self,
        message: str = "Authentication required",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code="AUTHENTICATION_ERROR",
            status_code=401,
            details=details,
        )


class AuthorizationError(AppError):
    """Raised when an action violates security invariants or permissions."""

    def __init__(
        self,
        message: str = "Operation not permitted",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code="AUTHORIZATION_ERROR",
            status_code=403,
            details=details,
        )


class ResourceNotFoundError(AppError):
    """Raised when a requested entity or artifact does not exist."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="RESOURCE_NOT_FOUND",
            status_code=404,
            details=details,
        )


class ConflictError(AppError):
    """Raised on version drift, lock contention, or idempotency conflicts."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="CONFLICT_ERROR",
            status_code=409,
            details=details,
        )


class ValidationFailedError(AppError):
    """Raised when patches, schemas, or safety invariants fail validation."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="VALIDATION_ERROR",
            status_code=422,
            details=details,
        )


class BudgetExceededError(AppError):
    """Raised when a workflow exceeds cost, token, call, or loop budgets."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="BUDGET_EXCEEDED",
            status_code=429,
            details=details,
        )