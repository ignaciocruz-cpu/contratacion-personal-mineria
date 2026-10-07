# -*- coding: utf-8 -*-
"""Modelo de roles del login, calcado de `lib/auth.ts` de plataforma-sync-360:

  - Login válido (dominio permitido, verificado por cia_auth) + correo sin fila
    en `usuarios` → alta automática. Si el correo está en CIA_ADMIN_EMAILS en
    ESE momento, entra como 'admin'; si no, como 'member'. Es solo un
    *bootstrap*: nunca vuelve a mirar la lista para una cuenta que ya existe.
  - Login de un correo que ya tiene fila → se respeta el rol guardado (permite
    ascender/degradar a mano después, editando la tabla).
  - `activo = false` veta el acceso aunque el dominio y el correo sigan siendo
    válidos — es la única forma de bloquear a alguien sin tocar la lista de
    dominios permitidos.
"""
from __future__ import annotations

from typing import Optional


class AccesoDenegado(Exception):
    """La cuenta existe pero fue desactivada (`activo = false`)."""


def resolver_login(conn, google_user: dict) -> dict:
    """Aplica el modelo de roles y deja la fila en `usuarios` al día.

    `conn` es una conexión psycopg2 con el `search_path` ya fijado al schema
    propio. No hace commit: el llamador controla la transacción (igual que el
    resto de `api.py`).
    """
    email = google_user["email"]
    cur = conn.cursor()
    cur.execute("SELECT role, activo FROM usuarios WHERE email = %s", (email,))
    row = cur.fetchone()

    if row is None:
        role = google_user.get("role", "member")  # cia_auth ya lo calculó vs CIA_ADMIN_EMAILS
        cur.execute(
            """INSERT INTO usuarios (email, sub, nombre, picture, role, activo, last_login)
               VALUES (%s, %s, %s, %s, %s, TRUE, now() AT TIME ZONE 'America/Santiago')""",
            (email, google_user.get("sub"), google_user.get("name"), google_user.get("picture"), role),
        )
        activo = True
    else:
        role, activo = row["role"], row["activo"]
        cur.execute(
            """UPDATE usuarios SET sub=%s, nombre=%s, picture=%s,
                      last_login = now() AT TIME ZONE 'America/Santiago'
               WHERE email = %s""",
            (google_user.get("sub"), google_user.get("name"), google_user.get("picture"), email),
        )

    if not activo:
        raise AccesoDenegado(email)

    return {**google_user, "role": role}


def obtener_usuario(conn, email: str) -> Optional[dict]:
    cur = conn.cursor()
    cur.execute("SELECT email, nombre, role, activo FROM usuarios WHERE email = %s", (email,))
    row = cur.fetchone()
    if not row:
        return None
    return {"email": row["email"], "nombre": row["nombre"], "role": row["role"], "activo": row["activo"]}
