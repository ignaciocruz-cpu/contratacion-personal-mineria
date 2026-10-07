"""Autenticación SSO con Google + sesión propia (JWT HS256 en cookie HttpOnly).

Port del `plataforma-combustible/backend/app/security.py` (el patrón más
completo del parque) como módulo reutilizable. Flujo:

  1. El frontend obtiene un ID token de Google (Google Identity Services).
  2. `verify_google_id_token` lo valida contra el JWKS de Google (firma RS256,
     issuer, audience = client_id, expiración) y el dominio corporativo.
  3. `issue_session_token` emite un JWT propio (HS256) que viaja en cookie
     HttpOnly `cia_session`.
  4. `get_current_user` / `require_auth` resuelven el usuario en cada request.

Rollout seguro: sin `GOOGLE_CLIENT_ID`, `require_auth` deja pasar como anónimo
(comportamiento legado) para no bloquear la app antes de configurar GCP.
"""
from __future__ import annotations

import time
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from jwt import PyJWKClient

from .config import get_auth_settings

S = get_auth_settings()

_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_jwks_client = PyJWKClient(_GOOGLE_CERTS_URL)


class AuthError(HTTPException):
    def __init__(self, detail: str, code: int = status.HTTP_401_UNAUTHORIZED):
        super().__init__(status_code=code, detail=detail)


# ---------------------------------------------------------------------------
# Verificación del ID token de Google
# ---------------------------------------------------------------------------
def _verify_google_id_token_sync(credential: str) -> dict:
    if not S.google_client_id:
        raise AuthError("auth_no_configurada", status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(credential)
        claims = jwt.decode(
            credential,
            signing_key.key,
            algorithms=["RS256"],
            audience=S.google_client_id,
            issuer=list(_GOOGLE_ISSUERS),
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("token_expirado")
    except jwt.InvalidAudienceError:
        raise AuthError("audience_invalida")
    except jwt.InvalidIssuerError:
        raise AuthError("issuer_invalido")
    except jwt.PyJWTError as e:
        raise AuthError(f"token_invalido: {type(e).__name__}")

    if not claims.get("email_verified"):
        raise AuthError("email_no_verificado")
    email = (claims.get("email") or "").lower()
    if not email:
        raise AuthError("sin_email")

    domain = email.split("@")[-1] if "@" in email else ""
    allowed = S.allowed_domains_list
    if allowed:
        hd = (claims.get("hd") or "").lower()
        if domain not in allowed and hd not in allowed:
            raise AuthError("dominio_no_autorizado", status.HTTP_403_FORBIDDEN)

    role = "admin" if email in S.admin_emails_list else "member"
    return {
        "sub": claims["sub"],
        "email": email,
        "name": claims.get("name") or email.split("@")[0],
        "picture": claims.get("picture"),
        "domain": domain or (claims.get("hd") or ""),
        "role": role,
    }


async def verify_google_id_token(credential: str) -> dict:
    """Valida el ID token de Google (la red/JWKS corre en threadpool)."""
    return await run_in_threadpool(_verify_google_id_token_sync, credential)


# ---------------------------------------------------------------------------
# Sesión propia (JWT HS256)
# ---------------------------------------------------------------------------
def issue_session_token(user: dict) -> str:
    now = int(time.time())
    payload = {
        "sub": user["sub"],
        "email": user["email"],
        "name": user.get("name"),
        "picture": user.get("picture"),
        "domain": user.get("domain"),
        "role": user.get("role", "member"),
        "iat": now,
        "exp": now + S.session_ttl_hours * 3600,
        "typ": "session",
    }
    return jwt.encode(payload, S.session_secret, algorithm="HS256")


def decode_session_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, S.session_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# Dependencias FastAPI
# ---------------------------------------------------------------------------
def _user_from_request(request: Request) -> Optional[dict]:
    token = request.cookies.get(S.session_cookie_name)
    if not token:
        return None
    return decode_session_token(token)


def get_current_user(request: Request) -> Optional[dict]:
    """Devuelve el usuario de la sesión o None (no levanta error)."""
    return _user_from_request(request)


def require_auth(request: Request) -> dict:
    """Exige sesión válida cuando la auth está activa. Si NO está configurada,
    deja pasar con un actor anónimo (comportamiento legado durante el rollout)."""
    user = _user_from_request(request)
    if user:
        return user
    if not S.auth_active:
        return {"sub": None, "email": None, "name": "anónimo", "role": "member", "anonymous": True}
    raise AuthError("no_autenticado")


def require_admin(user: dict = Depends(require_auth)) -> dict:
    if user.get("role") != "admin" and not user.get("anonymous"):
        raise AuthError("requiere_admin", status.HTTP_403_FORBIDDEN)
    return user
