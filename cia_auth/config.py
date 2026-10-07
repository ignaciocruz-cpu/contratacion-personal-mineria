"""Configuración del módulo de auth compartido (pydantic-settings).

Toma como base el `config.py` de plataforma-combustible, generalizado para que
cualquier backend FastAPI lo reuse. Nombres canónicos del estándar CIA:

  GOOGLE_CLIENT_ID        — mismo Client ID para todas las apps
  CIA_ALLOWED_DOMAINS     — dominios permitidos (default flesan.cl)
  CIA_ADMIN_EMAILS        — lista central de admins (saca el hardcode de cia-dolores)
  SESSION_SECRET          — secreto de la sesión propia (HS256). Cada app el suyo.
  SESSION_COOKIE_NAME     — cia_session (igual en todas, son cookies por dominio)
"""
from functools import lru_cache

from pydantic_settings import BaseSettings


class AuthSettings(BaseSettings):
    auth_enabled: bool = True
    google_client_id: str = ""

    # Acepta CIA_ALLOWED_DOMAINS (canónico). Default corporativo.
    cia_allowed_domains: str = "flesan.cl"
    # Lista central de admins (coma-separada). El resto entra como 'member'.
    cia_admin_emails: str = ""

    # Sesión propia (JWT HS256 en cookie HttpOnly). NO se comparte entre apps:
    # cada una mantiene su SESSION_SECRET (esto NO es SSO total).
    session_secret: str = "change-me"
    session_ttl_hours: int = 12
    session_cookie_name: str = "cia_session"
    # En LAN sobre HTTP debe ir False; detrás de TLS, True.
    session_cookie_secure: bool = False

    @property
    def allowed_domains_list(self) -> list[str]:
        return [d.strip().lower() for d in self.cia_allowed_domains.split(",") if d.strip()]

    @property
    def admin_emails_list(self) -> list[str]:
        return [e.strip().lower() for e in self.cia_admin_emails.split(",") if e.strip()]

    @property
    def auth_active(self) -> bool:
        """True cuando la verificación de identidad debe exigirse."""
        return self.auth_enabled and bool(self.google_client_id)

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_auth_settings() -> AuthSettings:
    return AuthSettings()
