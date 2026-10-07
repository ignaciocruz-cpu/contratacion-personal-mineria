"""cia_auth — login estándar CIA para backends FastAPI (copia vendorizada,
recortada para este proyecto).

Verificación de ID token de Google + sesión propia (JWT HS256 en cookie
HttpOnly), dominios y admins centralizados. El upsert/rol/auditoría de usuario
NO usa `audit.py` del paquete compartido (asyncpg): este proyecto usa
psycopg2 sync, ver `usuarios.py` en la raíz del repo (modelo de roles estilo
plataforma-sync-360).
"""
from .auth import (
    AuthError,
    decode_session_token,
    get_current_user,
    issue_session_token,
    require_admin,
    require_auth,
    verify_google_id_token,
)
from .config import AuthSettings, get_auth_settings

__all__ = [
    "AuthError",
    "AuthSettings",
    "get_auth_settings",
    "verify_google_id_token",
    "issue_session_token",
    "decode_session_token",
    "get_current_user",
    "require_auth",
    "require_admin",
]
