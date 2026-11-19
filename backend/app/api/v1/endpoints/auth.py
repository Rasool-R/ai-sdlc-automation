"""Authentication endpoints for single-operator login and session verification."""

from fastapi import APIRouter

from app.api.deps import CurrentOperator
from app.core.security import authenticate_operator, create_access_token
from app.schemas.auth import LoginRequest, OperatorIdentity, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
    """Authenticate the operator and return a signed Bearer access token."""
    username = authenticate_operator(payload.username, payload.password)
    access_token, expires_in = create_access_token(subject=username)
    return TokenResponse(access_token=access_token, expires_in=expires_in)


@router.get("/me", response_model=OperatorIdentity)
def get_authenticated_operator(current_operator: CurrentOperator) -> OperatorIdentity:
    """Return the identity of the currently authenticated operator."""
    return current_operator