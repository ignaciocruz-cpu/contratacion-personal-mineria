# -*- coding: utf-8 -*-
"""
Plataforma de Reclutamiento - Flesan Minería
Backend FastAPI. Postgres QA (schema propio `cia_reclutamiento_min`, catálogo
CONN_QA_*) + almacenamiento local de documentos + login CIA (Google, cia_auth).
"""

import re
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()  # no-op si la variable ya viene del entorno (Docker/compose gana)

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
import shutil
import io
import os
import asyncio
import unicodedata
import openpyxl
import httpx
import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values
from pathlib import Path
from datetime import datetime
import uvicorn, threading, webbrowser, time

from cia_auth import (
    get_auth_settings, verify_google_id_token, issue_session_token,
    get_current_user, require_auth, require_admin,
)
from usuarios import resolver_login, AccesoDenegado

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    print("[INFO] Base de datos lista.")
    _reanudar_lotes_whatsapp()
    yield

app = FastAPI(title="Flesan Minería - Reclutamiento", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

HERE    = Path(__file__).parent
UPLOADS = HERE / "uploads"
UPLOADS.mkdir(exist_ok=True)

# ── WhatsApp (Evolution API — misma instancia "FlesanNumber" usada por el resto
#    de plataformas del área). Por defecto apunta al contenedor local; si esta
#    app corre dockerizada, sumar la red externa whastapp_infrastructure_default
#    y sobreescribir EVOLUTION_API_URL con el nombre del contenedor. ──────────────
EVOLUTION_API_URL  = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "ClaveMaestraSegura123")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "FlesanNumber")

# ── Buzón CIA (badge/chatbot) ───────────────────────────────────────────────────
CIA_FEEDBACK_URL   = os.getenv("CIA_FEEDBACK_URL", "http://192.168.10.22:8083")
CIA_SERVICE_TOKEN  = os.getenv("CIA_SERVICE_TOKEN", "")

AUTH = get_auth_settings()

# ── Base de datos (Postgres QA — schema propio, catálogo CONN_QA_*) ─────────────
# DB_SCHEMA sin valor debe reventar al arrancar: es el único aislamiento entre
# plataformas en QA (todas comparten el usuario flesan_peru), jamás un default.
DB_HOST   = os.environ.get("DB_HOST", "")
DB_PORT   = os.environ.get("DB_PORT", "5432")
DB_NAME   = os.environ.get("DB_NAME", "")
DB_USER   = os.environ.get("DB_USER", "")
DB_PASS   = os.environ.get("DB_PASSWORD", "")
DB_SCHEMA = os.environ.get("DB_SCHEMA", "")
if not DB_SCHEMA:
    raise RuntimeError("Falta DB_SCHEMA — sin schema explícito la app podría escribir en el de otra plataforma.")

def _norm_sql(s):
    """Minúsculas sin tildes — debe dar el mismo resultado que la función SQL
    contratacion_normalizar() (schema_postgres.sql) para que los LIKE calcen."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFD", str(s).lower().strip())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", s)

def _norm_rut(rut):
    """Normaliza un RUT chileno a formato NNNNNNNN-D (sin puntos, con guión, DV en mayúscula)."""
    if not rut:
        return rut
    s = re.sub(r"[^0-9kK]", "", str(rut)).upper()
    if len(s) < 2:
        return s
    return f"{s[:-1]}-{s[-1]}"

def get_db():
    """Conexión Postgres con el search_path fijado SOLO al schema propio (sin
    ',public'): si una tabla no está acá, la query falla en vez de caer a otro
    schema — es el mismo usuario compartido (flesan_peru) que usan las demás
    plataformas de QA."""
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS,
        options=f"-c search_path={DB_SCHEMA}",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    return conn

def init_db():
    conn = get_db()
    with open(HERE / "schema_postgres.sql", encoding="utf-8") as f:
        conn.cursor().execute(f.read())
    conn.commit()
    conn.close()

# ── Login CIA (Google) ───────────────────────────────────────────────────────────

RUTAS_PUBLICAS = {
    "/login", "/auth/google", "/auth/logout", "/auth/me", "/auth/config",
    "/postulacion", "/logo-mineria.png", "/logo-mineria-color.png",
}
_RE_DOC_PUBLICO = re.compile(r"^/api/candidatos/\d+/documentos/[\w]+$")

def _es_publica(method: str, path: str) -> bool:
    if method == "OPTIONS":
        return True
    if path == "/api/candidatos":
        return method == "POST"          # alta manual (interna) Y postulación pública comparten endpoint
    if _RE_DOC_PUBLICO.match(path) and method == "POST":
        return True
    return path in RUTAS_PUBLICAS

@app.middleware("http")
async def exigir_sesion(request: Request, call_next):
    path = request.url.path
    if _es_publica(request.method, path):
        return await call_next(request)
    if not AUTH.auth_active:
        return await call_next(request)  # rollout seguro: sin GOOGLE_CLIENT_ID no bloquea nada
    user = get_current_user(request)
    if user:
        return await call_next(request)
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/login?next={path}", status_code=303)
    return JSONResponse({"detail": "no_autenticado"}, status_code=401)

LOGIN_HTML = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><title>Flesan Minería · Reclutamiento</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0b0d10;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;}
  .card{background:#15181c;border:1px solid #23272c;border-radius:16px;padding:40px 36px;
        width:340px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.4);}
  img.logo{height:56px;margin-bottom:18px;}
  h1{color:#fff;font-size:18px;margin:0 0 4px;}
  p.sub{color:#8a8f98;font-size:13px;margin:0 0 26px;}
  #g_btn{display:flex;justify-content:center;min-height:44px;}
  .error{color:#ff6b6b;font-size:13px;margin-top:16px;display:none;}
  .footer{margin-top:28px;color:#5b6169;font-size:11px;}
  .footer b{color:#E30613;}
</style></head>
<body>
  <div class="card">
    <img class="logo" src="/logo-mineria-color.png" alt="Flesan Minería">
    <h1>Reclutamiento</h1>
    <p class="sub">Ingresa con tu cuenta @flesan.cl</p>
    <div id="g_btn"></div>
    <p class="error" id="err">No se pudo iniciar sesión.</p>
    <div class="footer">Desarrollado por <b>CIA</b> · Grupo Flesan</div>
  </div>
  <script src="https://accounts.google.com/gsi/client" async defer></script>
  <script>
    const DESTINO = new URLSearchParams(location.search).get('next') || '/';
    async function onCredential(resp) {
      const r = await fetch('/auth/google', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        credentials: 'same-origin', body: JSON.stringify({credential: resp.credential})
      });
      if (r.ok) { location.href = DESTINO; return; }
      document.getElementById('err').style.display = 'block';
    }
    fetch('/auth/config').then(r => r.json()).then(cfg => {
      if (!cfg.google_client_id) {
        document.getElementById('g_btn').innerHTML =
          '<p style="color:#8a8f98;font-size:12px">Login sin configurar todavía.</p>';
        return;
      }
      (function render() {
        if (!window.google) { setTimeout(render, 150); return; }
        google.accounts.id.initialize({client_id: cfg.google_client_id, callback: onCredential});
        google.accounts.id.renderButton(document.getElementById('g_btn'),
          {theme:'filled_black', size:'large', shape:'pill', text:'signin_with', width:260});
      })();
    });
  </script>
</body></html>"""

@app.get("/login")
def login_page():
    return HTMLResponse(LOGIN_HTML)

@app.get("/auth/config")
def auth_config():
    return {"google_client_id": AUTH.google_client_id, "allowed_domains": AUTH.allowed_domains_list,
            "auth_active": AUTH.auth_active}

@app.post("/auth/google")
async def auth_google(request: Request, response: Response):
    body = await request.json()
    google_user = await verify_google_id_token(body.get("credential", ""))
    conn = get_db()
    try:
        user = resolver_login(conn, google_user)
        conn.commit()
    except AccesoDenegado:
        conn.rollback()
        raise HTTPException(403, "cuenta_desactivada")
    finally:
        conn.close()
    token = issue_session_token(user)
    response.set_cookie(
        AUTH.session_cookie_name, token, httponly=True, secure=AUTH.session_cookie_secure,
        samesite="lax", max_age=AUTH.session_ttl_hours * 3600, path="/",
    )
    return {"user": user}

@app.get("/auth/me")
def auth_me(user: dict = Depends(require_auth)):
    return user

@app.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(AUTH.session_cookie_name, path="/")
    return {"ok": True}

# ── Buzón CIA (BFF → feedback-api) ───────────────────────────────────────────────

@app.post("/api/feedback")
async def api_feedback(request: Request, user: dict = Depends(require_auth)):
    if not CIA_SERVICE_TOKEN:
        raise HTTPException(503, "buzon_no_configurado")
    body = await request.json()
    payload = {**body, "plataforma": "cia_reclutamiento_min",
               "usuario_email": user.get("email"), "usuario_nombre": user.get("name")}
    async with httpx.AsyncClient(timeout=10.0) as cli:
        r = await cli.post(f"{CIA_FEEDBACK_URL}/feedback", json=payload,
                            headers={"X-CIA-Service-Token": CIA_SERVICE_TOKEN})
    return JSONResponse(r.json() if r.content else {}, status_code=r.status_code)

# ── Static ──────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse(HERE / "index.html")

@app.get("/logo-mineria.png")
def logo_blanco():
    return FileResponse(HERE / "logo-mineria.png")

@app.get("/logo-mineria-color.png")
def logo_color():
    return FileResponse(HERE / "logo-mineria-color.png")

# ── Candidatos ──────────────────────────────────────────────────────────────────

CAMPOS_CANDIDATO = [
    "apellido_paterno", "apellido_materno", "primer_nombre", "segundo_nombre",
    "rut", "fecha_nacimiento", "estado_civil", "nacionalidad",
    "telefono", "correo", "direccion", "comuna", "region",
    "cargo", "profesion", "experiencia_general", "experiencia_especifica",
    "talla_overol", "talla_zapatos", "licencia_conducir",
    "categoria", "especialidad", "notas", "estado", "creado_por",
]

ORDEN_CANDIDATOS = {
    "nombre":           "c.apellido_paterno, c.primer_nombre",
    "actualizado_desc": "c.updated_at DESC NULLS LAST",
    "actualizado_asc":  "c.updated_at ASC NULLS LAST",
    "creado_desc":      "c.created_at DESC NULLS LAST",
    "creado_asc":       "c.created_at ASC NULLS LAST",
}

@app.get("/api/candidatos")
def listar_candidatos(
    q: str = "", cargo: str = "", region: str = "",
    exp_min: int = 0, categoria: str = "", especialidad: str = "",
    estado: str = "", con_cv: str = "",
    limit: int = 100, offset: int = 0, sort: str = "nombre",
):
    conn = get_db()
    c = conn.cursor()
    sql = """SELECT c.*, COALESCE(d.cnt, 0) as n_docs, COALESCE(dt.tipos, '') as doc_tipos
             FROM candidatos c
             LEFT JOIN (SELECT candidato_id, COUNT(*) cnt FROM documentos GROUP BY candidato_id) d
             ON c.id = d.candidato_id
             LEFT JOIN (SELECT candidato_id, STRING_AGG(tipo, ',') tipos FROM documentos GROUP BY candidato_id) dt
             ON c.id = dt.candidato_id
             WHERE 1=1"""
    params = []

    if q:
        p = f"%{_norm_sql(q)}%"
        sql += """ AND (
            contratacion_normalizar(c.primer_nombre)    LIKE %s OR
            contratacion_normalizar(c.apellido_paterno) LIKE %s OR
            contratacion_normalizar(c.apellido_materno) LIKE %s OR
            contratacion_normalizar(c.segundo_nombre)   LIKE %s OR
            contratacion_normalizar(c.rut)              LIKE %s OR
            contratacion_normalizar(c.correo)           LIKE %s OR
            contratacion_normalizar(c.cargo)             LIKE %s OR
            contratacion_normalizar(c.profesion)        LIKE %s OR
            contratacion_normalizar(c.especialidad)     LIKE %s OR
            contratacion_normalizar(c.comuna)           LIKE %s
        )"""
        params.extend([p] * 10)
    if cargo:
        sql += " AND contratacion_normalizar(c.cargo) LIKE %s"
        params.append(f"%{_norm_sql(cargo)}%")
    if region:
        sql += " AND c.region = %s"
        params.append(region)
    if exp_min:
        sql += " AND CAST(c.experiencia_general AS INTEGER) >= %s"
        params.append(exp_min)
    if categoria:
        sql += " AND c.categoria = %s"
        params.append(categoria)
    if especialidad:
        sql += " AND contratacion_normalizar(c.especialidad) LIKE %s"
        params.append(f"%{_norm_sql(especialidad)}%")
    if estado:
        sql += " AND c.estado = %s"
        params.append(estado)
    if con_cv == "1":
        sql += " AND EXISTS (SELECT 1 FROM documentos dcv WHERE dcv.candidato_id = c.id AND dcv.tipo = 'cv')"

    sql += " ORDER BY " + ORDEN_CANDIDATOS.get(sort, ORDEN_CANDIDATOS["nombre"])

    # Total sin paginación
    count_sql = f"SELECT COUNT(*) FROM ({sql}) AS sub"
    c.execute(count_sql, params)
    total = c.fetchone()["count"]

    sql += " LIMIT %s OFFSET %s"
    c.execute(sql, params + [limit, offset])
    rows = [dict(r) for r in c.fetchall()]

    conn.close()
    return {"total": total, "candidatos": rows}

@app.get("/api/candidatos/exportar")
def exportar_candidatos():
    from openpyxl.styles import Font, PatternFill, Alignment
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT c.*, COALESCE(d.cnt,0) as n_docs, COALESCE(dt.tipos,'') as doc_tipos
                 FROM candidatos c
                 LEFT JOIN (SELECT candidato_id, COUNT(*) cnt FROM documentos GROUP BY candidato_id) d
                   ON c.id = d.candidato_id
                 LEFT JOIN (SELECT candidato_id, STRING_AGG(tipo, ',') tipos FROM documentos GROUP BY candidato_id) dt
                   ON c.id = dt.candidato_id
                 ORDER BY c.apellido_paterno, c.primer_nombre""")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Candidatos"

    headers = [
        "ID","Apellido Paterno","Apellido Materno","Primer Nombre","Segundo Nombre","RUT",
        "Fecha Nacimiento","Estado Civil","Nacionalidad","Teléfono","Correo",
        "Dirección","Comuna","Región","Cargo","Profesión",
        "Exp. General (años)","Exp. Específica (años)",
        "Talla Overol","Talla Zapatos","Licencia Conducir","Categoría","Especialidad",
        "Estado","Notas","N° Documentos","Tipos Documentos","Creado Por","Fecha Registro","Última Actualización",
    ]
    fields = [
        "id","apellido_paterno","apellido_materno","primer_nombre","segundo_nombre","rut",
        "fecha_nacimiento","estado_civil","nacionalidad","telefono","correo",
        "direccion","comuna","region","cargo","profesion",
        "experiencia_general","experiencia_especifica",
        "talla_overol","talla_zapatos","licencia_conducir","categoria","especialidad",
        "estado","notas","n_docs","doc_tipos","creado_por","created_at","updated_at",
    ]

    fill = PatternFill("solid", fgColor="E30613")
    font_h = Font(bold=True, color="FFFFFF", size=10)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = fill
        cell.font = font_h
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 18
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    for r_idx, row in enumerate(rows, 2):
        for c_idx, field in enumerate(fields, 1):
            ws.cell(row=r_idx, column=c_idx, value=row.get(field))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fecha = datetime.now().strftime("%Y%m%d_%H%M")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=candidatos_{fecha}.xlsx"},
    )

@app.get("/api/candidatos/{cid}")
def get_candidato(cid: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM candidatos WHERE id = %s", (cid,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Candidato no encontrado")
    d = dict(row)
    c.execute("SELECT * FROM documentos WHERE candidato_id = %s ORDER BY tipo", (cid,))
    d["documentos"] = [dict(r) for r in c.fetchall()]
    conn.close()
    return d

@app.post("/api/candidatos")
async def crear_candidato(request: Request):
    data = await request.json()
    vals = {k: data.get(k) for k in CAMPOS_CANDIDATO}
    vals["rut"] = _norm_rut(vals.get("rut"))
    vals["origen"] = "postulacion" if data.get("origen") == "postulacion" else "manual"
    for campo in ("experiencia_general", "experiencia_especifica"):
        if vals.get(campo) not in (None, ""):
            vals[campo] = int(vals[campo])

    # Consentimiento de datos (Ley 19.628 / Ley 21.719): obligatorio solo en el
    # formulario público (origen=postulacion); el alta manual desde el panel
    # (un reclutador cargando datos de alguien que llamó, etc.) no pasa por acá.
    if vals["origen"] == "postulacion":
        if not data.get("acepta_datos"):
            raise HTTPException(400, "Debes aceptar el tratamiento de tus datos personales para postular")
        vals["acepta_datos"] = True
        vals["acepta_datos_fecha"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    c = conn.cursor()

    # RUT repetido (p.ej. alguien que ya estaba en la base y usa el formulario
    # público para complementar sus datos, en vez de tirarle un error): se
    # actualiza el registro existente en lugar de rechazar. Solo se pisan los
    # campos que vengan con un valor no vacío, para no borrar lo que ya se
    # había cargado (carga masiva, otra postulación, edición manual, etc.).
    if vals.get("rut"):
        c.execute("SELECT id FROM candidatos WHERE rut = %s", (vals["rut"],))
        existente = c.fetchone()
        if existente:
            cid = existente["id"]
            campos = {k: v for k, v in vals.items() if k != "origen" and v not in (None, "")}
            if campos:
                campos["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                set_clause = ", ".join(f"{k} = %s" for k in campos)
                c.execute(f"UPDATE candidatos SET {set_clause} WHERE id = %s", list(campos.values()) + [cid])
            c.execute("INSERT INTO actividad (candidato_id,tipo,descripcion) VALUES (%s,%s,%s)",
                      (cid, "registro", "Información complementada vía formulario público"))
            conn.commit()
            conn.close()
            return {"ok": True, "id": cid, "actualizado": True}

    cols = ", ".join(vals.keys())
    ph   = ", ".join(["%s"] * len(vals))
    try:
        c.execute(f"INSERT INTO candidatos ({cols}) VALUES ({ph}) RETURNING id", list(vals.values()))
        new_id = c.fetchone()["id"]
        c.execute("INSERT INTO actividad (candidato_id,tipo,descripcion) VALUES (%s,%s,%s)",
                  (new_id, "registro", "Perfil registrado en el sistema"))
        conn.commit()
        return {"ok": True, "id": new_id}
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(400, "El RUT ya existe en la base de datos")
    finally:
        conn.close()

@app.put("/api/candidatos/{cid}")
async def actualizar_candidato(cid: int, request: Request):
    data = await request.json()
    vals = {k: data[k] for k in CAMPOS_CANDIDATO if k in data}
    if "rut" in vals:
        vals["rut"] = _norm_rut(vals["rut"])
    for campo in ("experiencia_general", "experiencia_especifica"):
        if campo in vals and vals[campo] not in (None, ""):
            vals[campo] = int(vals[campo])
    vals["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    set_clause = ", ".join(f"{k} = %s" for k in vals)
    conn = get_db()
    c = conn.cursor()
    try:
        if "estado" in vals:
            c.execute("SELECT estado FROM candidatos WHERE id=%s", (cid,))
            row2 = c.fetchone()
            if row2 and row2["estado"] != vals["estado"]:
                c.execute("INSERT INTO actividad (candidato_id,tipo,descripcion) VALUES (%s,%s,%s)",
                          (cid, "estado", f"Estado cambiado a '{vals['estado']}'"))
        c.execute(f"UPDATE candidatos SET {set_clause} WHERE id = %s", list(vals.values()) + [cid])
        conn.commit()
        return {"ok": True}
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(400, "El RUT ya existe en otro candidato")
    finally:
        conn.close()

@app.delete("/api/candidatos/{cid}")
def eliminar_candidato(cid: int, user: dict = Depends(require_admin)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT filepath FROM documentos WHERE candidato_id = %s", (cid,))
    for row in c.fetchall():
        Path(row["filepath"]).unlink(missing_ok=True)
    cand_dir = UPLOADS / str(cid)
    shutil.rmtree(cand_dir, ignore_errors=True)
    c.execute("DELETE FROM candidatos WHERE id = %s", (cid,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ── Documentos ───────────────────────────────────────────────────────────────────

TIPOS_DOC = {
    "cv":                  "CV",
    "licencia":            "Licencia de Conducir",
    "cert_titulo":         "Certificado de Título",
    "cert_afp":            "Certificado AFP",
    "cert_salud":          "Certificado Salud",
    "comprobante_domicilio": "Comprobante Domicilio",
    "carnet":              "Fotocopia Carnet",
    "transferencia":       "Datos Transferencia",
    "hoja_vida_conductor": "Hoja de Vida Conductor",
    "otros":               "Otros Documentos",
}

@app.post("/api/candidatos/{cid}/documentos/{tipo}")
async def subir_documento(cid: int, tipo: str, file: UploadFile = File(...)):
    if tipo not in TIPOS_DOC:
        raise HTTPException(400, f"Tipo inválido: {tipo}")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM candidatos WHERE id = %s", (cid,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(404, "Candidato no encontrado")

    cand_dir = UPLOADS / str(cid)
    cand_dir.mkdir(exist_ok=True)
    ext = Path(file.filename).suffix
    filepath = cand_dir / f"{tipo}{ext}"

    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    c.execute("SELECT id FROM documentos WHERE candidato_id = %s AND tipo = %s", (cid, tipo))
    existing = c.fetchone()
    if existing:
        c.execute(
            "UPDATE documentos SET filename=%s, filepath=%s, uploaded_at=(now() AT TIME ZONE 'America/Santiago') WHERE id=%s",
            (file.filename, str(filepath), existing["id"])
        )
    else:
        c.execute(
            "INSERT INTO documentos (candidato_id, tipo, filename, filepath) VALUES (%s,%s,%s,%s)",
            (cid, tipo, file.filename, str(filepath))
        )
    conn.commit()
    conn.close()
    return {"ok": True, "filename": file.filename}

@app.get("/api/documentos/{doc_id}/descargar")
def descargar_documento(doc_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM documentos WHERE id = %s", (doc_id,))
    doc = c.fetchone()
    conn.close()
    if not doc:
        raise HTTPException(404, "Documento no encontrado")
    fp = Path(doc["filepath"])
    if not fp.exists():
        raise HTTPException(404, "Archivo no encontrado en disco")
    return FileResponse(str(fp), filename=doc["filename"])

@app.delete("/api/documentos/{doc_id}")
def eliminar_documento(doc_id: int, user: dict = Depends(require_admin)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT filepath FROM documentos WHERE id = %s", (doc_id,))
    doc = c.fetchone()
    if doc:
        Path(doc["filepath"]).unlink(missing_ok=True)
        c.execute("DELETE FROM documentos WHERE id = %s", (doc_id,))
        conn.commit()
    conn.close()
    return {"ok": True}

# ── WhatsApp Masivo ──────────────────────────────────────────────────────────────

def _normalizar_telefono_cl(tel: str):
    """Deja el teléfono en E.164 sin '+' (ej: 56912345678) para Evolution API."""
    digitos = re.sub(r"\D", "", tel or "")
    if not digitos:
        return None
    if digitos.startswith("56") and len(digitos) >= 11:
        return digitos
    if digitos.startswith("9") and len(digitos) == 9:
        return "56" + digitos
    if len(digitos) == 8:
        return "569" + digitos
    return digitos

async def _wa_send_texto(numero: str, texto: str):
    """Envía un mensaje de texto por Evolution API. Devuelve (ok, error)."""
    url = f"{EVOLUTION_API_URL.rstrip('/')}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {
        "number": numero,
        "text": texto,
        "options": {"delay": 1200, "presence": "composing", "linkPreview": True},
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as cli:
            r = await cli.post(url, json=payload, headers=headers)
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        if r.status_code >= 400:
            # Evolution API mete el motivo real (ej: número sin WhatsApp) adentro
            # de response.message[], no en el "error" genérico ("Bad Request") —
            # sin esto el motivo que ve el usuario no dice nada útil.
            detalles = ((data.get("response") or {}).get("message")) or []
            if isinstance(detalles, list) and detalles and isinstance(detalles[0], dict) and detalles[0].get("exists") is False:
                return False, "El número no está registrado en WhatsApp"
            return False, str(data.get("message") or data.get("error") or r.text)[:200]
        return True, ""
    except Exception as e:
        return False, str(e)[:200]

@app.get("/api/whatsapp/estado")
async def whatsapp_estado():
    """Estado de conexión de la instancia de WhatsApp (para mostrar un badge en el panel)."""
    url = f"{EVOLUTION_API_URL.rstrip('/')}/instance/fetchInstances"
    headers = {"apikey": EVOLUTION_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=8.0) as cli:
            r = await cli.get(url, headers=headers)
        data = r.json() if r.content else []
        inst = next((i for i in data if i.get("name") == EVOLUTION_INSTANCE), None) if isinstance(data, list) else None
        estado = (inst or {}).get("connectionStatus", "no_encontrada")
        return {"conectado": estado == "open", "estado": estado, "numero": (inst or {}).get("number", "")}
    except Exception as e:
        return {"conectado": False, "estado": "error", "detalle": str(e)[:150]}

# Tareas de fondo de lotes de WhatsApp en curso. asyncio.create_task() no
# retiene una referencia fuerte a la tarea — sin este set, el garbage collector
# puede matarla a mitad de camino ("Task was destroyed but it is pending").
_TAREAS_WA: set = set()

def _lanzar_lote_whatsapp(lote_id: int):
    t = asyncio.create_task(_procesar_lote_whatsapp(lote_id))
    _TAREAS_WA.add(t)
    t.add_done_callback(_TAREAS_WA.discard)

def _reanudar_lotes_whatsapp():
    """Al arrancar el proceso, retoma lotes que quedaron 'en_curso' (crash o
    restart a mitad de un envío masivo). El procesador solo toma envíos
    'pendiente', así que no reenvía nada de lo que ya salió."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM whatsapp_lotes WHERE estado = 'en_curso'")
    lote_ids = [row["id"] for row in c.fetchall()]
    conn.close()
    for lote_id in lote_ids:
        print(f"[INFO] Retomando lote de WhatsApp masivo #{lote_id} tras restart.")
        _lanzar_lote_whatsapp(lote_id)

async def _procesar_lote_whatsapp(lote_id: int):
    """Envía en background los pendientes de un lote, con el mismo espaciado
    anti-baneo de siempre. Corre fuera de la request HTTP que lo creó: para
    miles de candidatos esto puede tardar horas, y una request no debe quedar
    abierta ese tiempo (timeout de proxy/navegador) ni depender de que la
    pestaña siga abierta."""
    conn = get_db()
    # Commit por mensaje, no al final del lote: un corte a mitad de camino no
    # debe perder el registro de lo que ya salió de verdad (incidente
    # 2026-08-31: 35 WhatsApp enviados sin quedar registrados).
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SELECT mensaje FROM whatsapp_lotes WHERE id = %s", (lote_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
    mensaje_base = row["mensaje"]

    c.execute(
        """SELECT e.id AS envio_id, c.id AS candidato_id, c.primer_nombre, c.apellido_paterno, c.telefono
           FROM whatsapp_envios e JOIN candidatos c ON c.id = e.candidato_id
           WHERE e.lote_id = %s AND e.estado = 'pendiente'
           ORDER BY e.id""",
        (lote_id,),
    )
    pendientes = c.fetchall()

    for i, cand in enumerate(pendientes):
        c.execute("SELECT estado FROM whatsapp_lotes WHERE id = %s", (lote_id,))
        if c.fetchone()["estado"] == "cancelado":
            conn.close()
            return

        cid = cand["candidato_id"]
        nombre = " ".join(x for x in [cand.get("primer_nombre"), cand.get("apellido_paterno")] if x)
        try:
            numero = _normalizar_telefono_cl(cand.get("telefono") or "")
            if not numero:
                ok, err = False, "Sin teléfono registrado"
            else:
                nombre_pila = (cand.get("primer_nombre") or "").strip() or "candidato/a"
                texto = mensaje_base.replace("{nombre}", nombre_pila)
                ok, err = await _wa_send_texto(numero, texto)
        except Exception as e:
            ok, err = False, str(e)[:200]

        c.execute(
            "UPDATE whatsapp_envios SET estado=%s, motivo=%s, enviado_at=(now() AT TIME ZONE 'America/Santiago') WHERE id=%s",
            ("enviado" if ok else "fallido", "" if ok else (err or "Error al enviar"), cand["envio_id"]),
        )
        c.execute(
            "INSERT INTO actividad (candidato_id, tipo, descripcion) VALUES (%s,%s,%s)",
            (cid, "whatsapp", "WhatsApp masivo: enviado" if ok else f"WhatsApp masivo: falló ({err})"),
        )
        campo = "enviados" if ok else "fallidos"
        c.execute(
            f"UPDATE whatsapp_lotes SET {campo} = {campo} + 1, updated_at=(now() AT TIME ZONE 'America/Santiago') WHERE id=%s",
            (lote_id,),
        )
        if i < len(pendientes) - 1:
            await asyncio.sleep(1.3)  # anti-baneo: espacia los envíos a Evolution API

    c.execute(
        "UPDATE whatsapp_lotes SET estado='completado', updated_at=(now() AT TIME ZONE 'America/Santiago') WHERE id=%s",
        (lote_id,),
    )
    conn.close()

@app.post("/api/whatsapp/masivo")
async def whatsapp_masivo(request: Request, user: dict = Depends(require_admin)):
    d = await request.json()
    mensaje_base = (d.get("mensaje") or "").strip()
    try:
        ids = [int(x) for x in (d.get("candidato_ids") or [])]
    except (TypeError, ValueError):
        raise HTTPException(400, "candidato_ids inválido")
    if not ids:
        raise HTTPException(400, "Debes seleccionar al menos un candidato")
    if not mensaje_base:
        raise HTTPException(400, "El mensaje no puede estar vacío")

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO whatsapp_lotes (mensaje, total, creado_por) VALUES (%s,%s,%s) RETURNING id",
        (mensaje_base, len(ids), user.get("email", "")),
    )
    lote_id = c.fetchone()["id"]
    execute_values(
        c,
        "INSERT INTO whatsapp_envios (lote_id, candidato_id) VALUES %s",
        [(lote_id, cid) for cid in ids],
    )
    conn.commit()
    conn.close()

    _lanzar_lote_whatsapp(lote_id)
    return {"ok": True, "lote_id": lote_id, "total": len(ids)}

@app.get("/api/whatsapp/lotes/{lote_id}")
def whatsapp_lote_estado(lote_id: int, user: dict = Depends(require_auth)):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT id, mensaje, total, enviados, fallidos, estado, created_at, updated_at FROM whatsapp_lotes WHERE id=%s",
        (lote_id,),
    )
    lote = c.fetchone()
    conn.close()
    if not lote:
        raise HTTPException(404, "Lote no encontrado")
    return dict(lote)

@app.get("/api/whatsapp/lotes/{lote_id}/detalle")
def whatsapp_lote_detalle(lote_id: int, user: dict = Depends(require_auth)):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """SELECT c.id, c.primer_nombre, c.apellido_paterno, c.telefono, e.estado, e.motivo
           FROM whatsapp_envios e JOIN candidatos c ON c.id = e.candidato_id
           WHERE e.lote_id = %s ORDER BY e.id""",
        (lote_id,),
    )
    filas = c.fetchall()
    conn.close()
    detalle = [
        {
            "id": r["id"],
            "nombre": " ".join(x for x in [r.get("primer_nombre"), r.get("apellido_paterno")] if x),
            "telefono": r.get("telefono") or "",
            "ok": r["estado"] == "enviado",
            "pendiente": r["estado"] == "pendiente",
            "motivo": r.get("motivo") or "",
        }
        for r in filas
    ]
    return {"detalle": detalle}

@app.post("/api/whatsapp/lotes/{lote_id}/cancelar")
def whatsapp_lote_cancelar(lote_id: int, user: dict = Depends(require_admin)):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE whatsapp_lotes SET estado='cancelado', updated_at=(now() AT TIME ZONE 'America/Santiago') WHERE id=%s AND estado='en_curso'", (lote_id,))
    afectado = c.rowcount > 0
    conn.commit()
    conn.close()
    if not afectado:
        raise HTTPException(404, "Lote no encontrado o ya no está en curso")
    return {"ok": True}

@app.get("/api/whatsapp/contactados")
def whatsapp_contactados():
    """IDs de candidatos que ya recibieron un WhatsApp masivo exitoso alguna
    vez (cualquier lote, cualquier oferta) — para que el panel de envío
    masivo no vuelva a ofrecerlos y así evitar re-spam accidental.
    Incluye también los envíos previos a whatsapp_envios (esa tabla no
    existía antes de esta versión): los 35 del incidente 2026-08-31 quedaron
    registrados solo en `actividad`, reconstruidos a mano — sin este UNION
    el filtro no los vería y podrían volver a aparecer seleccionables."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT candidato_id FROM whatsapp_envios WHERE estado = 'enviado'
        UNION
        SELECT candidato_id FROM actividad WHERE tipo = 'whatsapp' AND descripcion LIKE 'WhatsApp masivo: enviado%'
    """)
    ids = [r["candidato_id"] for r in c.fetchall()]
    conn.close()
    return {"candidato_ids": ids}

@app.get("/api/whatsapp/respuestas")
def whatsapp_respuestas(dias: int = 30):
    """Para cada candidato al que se le mandó un WhatsApp masivo (últimos `dias`
    días), indica si respondió completando el formulario público después de
    ese envío. No sabemos si el mensaje llegó o se leyó (eso requeriría
    escuchar los webhooks de Evolution API, que hoy no están conectados) —
    solo si, después del envío, esa persona actualizó su ficha vía /postulacion."""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        WITH envios AS (
            SELECT candidato_id, MAX(created_at) AS enviado_at
            FROM actividad
            WHERE tipo = 'whatsapp' AND descripcion LIKE 'WhatsApp masivo: enviado%%'
              AND created_at >= (now() AT TIME ZONE 'America/Santiago') - (%s || ' days')::interval
            GROUP BY candidato_id
        ),
        respuestas AS (
            SELECT candidato_id, MIN(created_at) AS respondio_at
            FROM actividad
            WHERE tipo = 'registro' AND descripcion = 'Información complementada vía formulario público'
            GROUP BY candidato_id
        )
        SELECT c.id, c.primer_nombre, c.apellido_paterno, c.telefono,
               e.enviado_at, r.respondio_at,
               (r.respondio_at IS NOT NULL AND r.respondio_at > e.enviado_at) AS respondio
        FROM envios e
        JOIN candidatos c ON c.id = e.candidato_id
        LEFT JOIN respuestas r ON r.candidato_id = e.candidato_id
        ORDER BY respondio ASC, e.enviado_at DESC
        """,
        (str(dias),),
    )
    filas = [dict(r) for r in c.fetchall()]
    conn.close()
    respondieron = sum(1 for f in filas if f["respondio"])
    return {"total_enviados": len(filas), "respondieron": respondieron, "candidatos": filas}

# ── Ofertas ──────────────────────────────────────────────────────────────────────

@app.get("/api/ofertas")
def listar_ofertas(q: str = ""):
    conn = get_db()
    c = conn.cursor()
    if q:
        p = f"%{_norm_sql(q)}%"
        c.execute("""SELECT * FROM ofertas
                     WHERE contratacion_normalizar(titulo) LIKE %s OR contratacion_normalizar(cargo) LIKE %s
                        OR contratacion_normalizar(profesion) LIKE %s OR contratacion_normalizar(especialidad) LIKE %s
                        OR contratacion_normalizar(descripcion) LIKE %s
                     ORDER BY created_at DESC""",
                  [p, p, p, p, p])
    else:
        c.execute("SELECT * FROM ofertas ORDER BY created_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"ofertas": rows}

@app.post("/api/ofertas")
async def crear_oferta(request: Request):
    d = await request.json()
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO ofertas (titulo,cargo,profesion,categoria,especialidad,experiencia_min,region,descripcion,creado_por) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (d.get("titulo",""), d.get("cargo",""), d.get("profesion",""),
         d.get("categoria",""), d.get("especialidad",""),
         int(d.get("experiencia_min") or 0), d.get("region",""), d.get("descripcion",""),
         d.get("creado_por",""))
    )
    new_id = c.fetchone()["id"]
    conn.commit()
    conn.close()
    return {"ok": True, "id": new_id}

CAMPOS_OFERTA = ["titulo","cargo","profesion","categoria","especialidad",
                  "experiencia_min","region","descripcion","activa"]

@app.put("/api/ofertas/{oid}")
async def actualizar_oferta(oid: int, request: Request):
    d = await request.json()
    vals = {k: d[k] for k in CAMPOS_OFERTA if k in d}
    if not vals:
        return {"ok": True}
    set_clause = ", ".join(f"{k} = %s" for k in vals)
    conn = get_db()
    conn.cursor().execute(f"UPDATE ofertas SET {set_clause} WHERE id = %s", list(vals.values()) + [oid])
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/ofertas/{oid}")
def eliminar_oferta(oid: int, user: dict = Depends(require_admin)):
    conn = get_db()
    conn.cursor().execute("DELETE FROM ofertas WHERE id = %s", (oid,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ── Motor de Matching v2 ──────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normaliza: minúsculas, sin tildes, espacios simples."""
    s = unicodedata.normalize("NFD", (s or "").lower().strip())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)

# Grupos de sinónimos para cargos en minería / industria
_GRUPOS_CARGO = [
    {"operador de planta","operador de equipos","operador","operador de maquinaria","operador de proceso"},
    {"tecnico electricista","electricista industrial","tecnico en electricidad","electricista"},
    {"mecanico industrial","tecnico mecanico","mecanico de mantenimiento","mecanico","oficial mecanico"},
    {"supervisor de terreno","supervisor de area","jefe de turno","supervisor","capataz"},
    {"soldador","soldador certificado","tecnico soldador","soldador 6g"},
    {"tecnico en instrumentacion","instrumentista","tecnico instrumentista","tecnico en automatizacion"},
    {"topografo","tecnico en topografia","asistente topografo"},
    {"perforista","operador de perforacion","tronador","operador de jumbos"},
    {"bodeguero","encargado de bodega","panolero","auxiliar de bodega"},
    {"ingeniero de minas","ingeniero en minas","ingeniero civil de minas"},
    {"operador de grua","gruero","operador grua horquilla","operador grua horquilla"},
    {"oficial de mantencion","oficial de mantenimiento","tecnico de mantencion","tecnico mantenimiento"},
    {"operador de cargador frontal","operador cargador frontal","operador de retroexcavadora"},
    {"jefe de proyecto","administrador de contrato","coordinador de proyecto"},
    {"prevencionista","prevencionista de riesgos","tecnico en prevencion de riesgos"},
]

def _cargo_score(a: str, b: str) -> float:
    """Similitud entre dos cargos: 0.0 – 1.0"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb: return 0.0
    if na == nb: return 1.0
    if na in nb or nb in na: return 0.82
    for grupo in _GRUPOS_CARGO:
        if na in grupo and nb in grupo: return 0.88
    wa = {w for w in na.split() if len(w) > 3}
    wb = {w for w in nb.split() if len(w) > 3}
    if wa and wb:
        ov = len(wa & wb) / max(len(wa), len(wb))
        if ov >= 0.5: return 0.50
        if ov > 0:    return 0.25
    return 0.0

def _text_score(a: str, b: str) -> float:
    """Similitud genérica entre dos textos: 0.0 – 1.0"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb: return 0.0
    if na == nb: return 1.0
    if na in nb or nb in na: return 0.72
    wa = {w for w in na.split() if len(w) > 3}
    wb = {w for w in nb.split() if len(w) > 3}
    if wa and wb:
        ov = len(wa & wb) / max(len(wa), len(wb))
        if ov >= 0.5: return 0.48
        if ov > 0:    return 0.22
    return 0.0

def _exp_score(candidato: int, requerido: int) -> float:
    """Score continuo de experiencia (no binario)."""
    if requerido <= 0: return 1.0
    if candidato <= 0: return 0.0
    ratio = candidato / requerido
    if ratio >= 1.5: return 0.93   # sobre-calificado → leve reducción
    if ratio >= 1.0: return 1.0
    if ratio >= 0.80: return 0.75
    if ratio >= 0.60: return 0.45
    if ratio >= 0.40: return 0.20
    return 0.05

# Pesos fijos de cada dimensión (suman 100)
_PESOS = {"cargo": 35, "profesion": 20, "exp": 18, "especialidad": 12, "region": 10, "categoria": 5}

def _score_v2(cand: dict, oferta: dict) -> dict:
    dims = {}
    detalle = []

    # 1. Cargo (35 pts) — siempre evaluado
    sc = _cargo_score(oferta.get("cargo"), cand.get("cargo"))
    dims["cargo"] = {"pts": round(sc * 35, 1), "max": 35, "label": "Cargo / Rol"}
    if sc >= 0.95:   detalle.append("Cargo exacto")
    elif sc >= 0.80: detalle.append("Cargo muy similar")
    elif sc >= 0.45: detalle.append("Cargo relacionado")

    # 2. Profesión (20 pts)
    if oferta.get("profesion"):
        sp = _text_score(oferta.get("profesion"), cand.get("profesion"))
        dims["profesion"] = {"pts": round(sp * 20, 1), "max": 20, "label": "Profesión"}
        if sp >= 0.90: detalle.append("Profesión exacta")
        elif sp >= 0.45: detalle.append("Profesión relacionada")
    else:
        dims["profesion"] = {"pts": 20, "max": 20, "label": "Profesión", "na": True}

    # 3. Experiencia General (18 pts)
    exp_req = int(oferta.get("experiencia_min") or 0)
    exp_can = int(cand.get("experiencia_general") or 0)
    if exp_req > 0:
        se = _exp_score(exp_can, exp_req)
        dims["exp"] = {"pts": round(se * 18, 1), "max": 18, "label": "Experiencia"}
        if se >= 1.0:    detalle.append(f"{exp_can}a exp ✓ (req. {exp_req}a)")
        elif se >= 0.70: detalle.append(f"{exp_can}a exp (req. {exp_req}a)")
        else:            detalle.append(f"Exp. insuficiente ({exp_can}a / {exp_req}a)")
    else:
        dims["exp"] = {"pts": 18, "max": 18, "label": "Experiencia", "na": True}

    # 4. Especialidad (12 pts) — match exacto sobre valores controlados
    if oferta.get("especialidad"):
        ne = _norm(oferta.get("especialidad", ""))
        nc_e = _norm(cand.get("especialidad", ""))
        ss = 1.0 if ne and nc_e and ne == nc_e else (0.5 if ne and nc_e and (ne in nc_e or nc_e in ne) else 0.0)
        dims["especialidad"] = {"pts": round(ss * 12, 1), "max": 12, "label": "Especialidad"}
        if ss >= 1.0: detalle.append("Especialidad exacta")
        elif ss > 0:  detalle.append("Especialidad compatible")
    else:
        dims["especialidad"] = {"pts": 12, "max": 12, "label": "Especialidad", "na": True}

    # 5. Región (10 pts)
    if oferta.get("region"):
        sr = 1.0 if _norm(oferta.get("region", "")) == _norm(cand.get("region", "")) else 0.0
        dims["region"] = {"pts": round(sr * 10, 1), "max": 10, "label": "Región"}
        if sr == 1.0: detalle.append("Misma región")
        else:         detalle.append("Región diferente")
    else:
        dims["region"] = {"pts": 10, "max": 10, "label": "Región", "na": True}

    # 6. Categoría (5 pts) — match exacto sobre valores controlados
    if oferta.get("categoria"):
        nc = _norm(oferta.get("categoria", ""))
        cc2 = _norm(cand.get("categoria", ""))
        scat = 1.0 if nc and cc2 and nc == cc2 else 0.0
        dims["categoria"] = {"pts": round(scat * 5, 1), "max": 5, "label": "Categoría"}
        if scat: detalle.append("Categoría exacta")
        else:    detalle.append("Categoría distinta")
    else:
        dims["categoria"] = {"pts": 5, "max": 5, "label": "Categoría", "na": True}

    score_base = round(sum(d["pts"] for d in dims.values()), 1)

    # Bonus perfil completo (hasta 5 pts)
    bonus = 0.0
    if cand.get("correo"):     bonus += 1.0
    if cand.get("telefono"):   bonus += 1.0
    if cand.get("profesion"):  bonus += 0.5
    if cand.get("especialidad"): bonus += 0.5
    n_docs = int(cand.get("n_docs") or 0)
    bonus += min(2.0, n_docs * 0.5)
    bonus = round(min(5.0, bonus), 1)

    score = min(100.0, round(score_base + bonus, 1))

    return {"score": score, "score_base": score_base, "bonus_perfil": bonus,
            "dimensiones": dims, "detalle": detalle}

@app.get("/api/ofertas/{oid}/match")
def matching(oid: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM ofertas WHERE id = %s", (oid,))
    oferta = c.fetchone()
    if not oferta:
        conn.close()
        raise HTTPException(404, "Oferta no encontrada")
    oferta = dict(oferta)

    c.execute("""SELECT c.*, COALESCE(d.cnt,0) as n_docs
                 FROM candidatos c
                 LEFT JOIN (SELECT candidato_id, COUNT(*) cnt FROM documentos GROUP BY candidato_id) d
                 ON c.id = d.candidato_id""")
    candidatos = [dict(r) for r in c.fetchall()]
    conn.close()

    resultados, excluidos = [], []
    exp_req_global = int(oferta.get("experiencia_min") or 0)

    for cand in candidatos:
        # — Exclusión dura 1: cargo completamente distinto —
        if oferta.get("cargo") and _cargo_score(oferta.get("cargo"), cand.get("cargo")) == 0:
            excluidos.append({**cand, "razon_exclusion": "Cargo no compatible"})
            continue
        # — Exclusión dura 2: experiencia < 35% del mínimo requerido —
        if exp_req_global > 0:
            exp_can = int(cand.get("experiencia_general") or 0)
            if exp_can < exp_req_global * 0.35:
                excluidos.append({**cand, "razon_exclusion": f"Exp. insuficiente ({exp_can}a / mín. {exp_req_global}a)"})
                continue

        m = _score_v2(cand, oferta)
        if m["score"] >= 15:
            resultados.append({**cand, **m})

    resultados.sort(key=lambda x: x["score"], reverse=True)
    total = len(resultados)
    for i, r in enumerate(resultados):
        r["rank"] = i + 1
        r["percentil"] = round((1 - i / total) * 100) if total > 1 else 100

    return {"oferta": oferta, "matches": resultados, "total": total,
            "excluidos": len(excluidos), "total_evaluados": len(candidatos)}

# ── Carga masiva ─────────────────────────────────────────────────────────────────

EXCEL_COL_MAP = {
    "apellido paterno":            "apellido_paterno",
    "apellido materno":            "apellido_materno",
    "primer nombre":               "primer_nombre",
    "segundo nombre":              "segundo_nombre",
    "rut":                         "rut",
    "fecha de nacimiento":         "fecha_nacimiento",
    "estado civil":                "estado_civil",
    "nacionalidad":                "nacionalidad",
    "teléfono":                    "telefono",
    "telefono":                    "telefono",
    "correo electrónico":          "correo",
    "correo electronico":          "correo",
    "correo":                      "correo",
    "dirección":                   "direccion",
    "direccion":                   "direccion",
    "comuna":                      "comuna",
    "región":                      "region",
    "region":                      "region",
    "cargo":                       "cargo",
    "profesión":                   "profesion",
    "profesion":                   "profesion",
    "experiencia general (años)":  "experiencia_general",
    "experiencia general (anos)":  "experiencia_general",
    "experiencia general":         "experiencia_general",
    "experiencia específica (años)": "experiencia_especifica",
    "experiencia especifica (anos)": "experiencia_especifica",
    "experiencia específica":      "experiencia_especifica",
    "experiencia especifica":      "experiencia_especifica",
    "talla overol":                "talla_overol",
    "talla zapatos":               "talla_zapatos",
    "licencia conducir":           "licencia_conducir",
    "categoría":                   "categoria",
    "categoria":                   "categoria",
    "especialidad":                "especialidad",
    "notas":                       "notas",
    "estado":                      "estado",
    # Variantes abreviadas del exportador
    "fecha nacimiento":            "fecha_nacimiento",
    "exp. general (años)":         "experiencia_general",
    "exp. general (anos)":         "experiencia_general",
    "exp. específica (años)":      "experiencia_especifica",
    "exp. especifica (anos)":      "experiencia_especifica",
    "exp. específica (anos)":      "experiencia_especifica",
}

@app.post("/api/candidatos/bulk/preview")
async def preview_masivo(file: UploadFile = File(...)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Solo se aceptan archivos .xlsx o .xls")
    content = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    except Exception:
        raise HTTPException(400, "No se pudo leer el archivo Excel")
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(400, "El archivo está vacío")
    headers = [str(h) if h is not None else "" for h in rows[0]]
    data_rows = [[str(v) if v is not None else None for v in row] for row in rows[1:] if any(v is not None for v in row)]
    return {"headers": headers, "rows": data_rows, "total": len(data_rows)}


@app.post("/api/candidatos/bulk")
async def carga_masiva(file: UploadFile = File(...), user: dict = Depends(require_admin)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Solo se aceptan archivos .xlsx o .xls")

    content = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    except Exception:
        raise HTTPException(400, "No se pudo leer el archivo Excel")

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        raise HTTPException(400, "El archivo no tiene datos")

    # Mapear encabezados
    raw_headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    col_map = {}  # índice → campo DB
    for i, h in enumerate(raw_headers):
        db_field = EXCEL_COL_MAP.get(h.lower())
        if db_field:
            col_map[i] = db_field

    if "rut" not in col_map.values():
        raise HTTPException(400, "El archivo no tiene columna RUT")

    conn = get_db()
    c = conn.cursor()
    importados, omitidos, errores = 0, 0, []

    for row_num, row in enumerate(rows[1:], start=2):
        vals = {}
        for i, db_field in col_map.items():
            v = row[i] if i < len(row) else None
            vals[db_field] = str(v).strip() if v is not None and str(v).strip() != "" else None

        if not vals.get("rut"):
            omitidos += 1
            continue
        vals["rut"] = _norm_rut(vals["rut"])
        for campo in ("experiencia_general", "experiencia_especifica"):
            if vals.get(campo) not in (None, ""):
                try:
                    vals[campo] = int(float(vals[campo]))
                except ValueError:
                    vals[campo] = None

        # Solo insertar campos válidos de candidatos
        insert_vals = {k: v for k, v in vals.items() if k in CAMPOS_CANDIDATO}
        insert_vals["origen"] = "carga_masiva"
        cols = ", ".join(insert_vals.keys())
        ph   = ", ".join(["%s"] * len(insert_vals))
        c.execute("SAVEPOINT fila")
        try:
            c.execute(f"INSERT INTO candidatos ({cols}) VALUES ({ph})", list(insert_vals.values()))
            c.execute("RELEASE SAVEPOINT fila")
            importados += 1
        except psycopg2.IntegrityError:
            c.execute("ROLLBACK TO SAVEPOINT fila")
            omitidos += 1
        except Exception as e:
            c.execute("ROLLBACK TO SAVEPOINT fila")
            errores.append(f"Fila {row_num}: {str(e)}")

    conn.commit()
    conn.close()
    return {"importados": importados, "omitidos": omitidos, "errores": errores}


@app.get("/api/plantilla")
def descargar_plantilla():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Candidatos"
    headers = [
        "Apellido Paterno","Apellido Materno","Primer Nombre","Segundo Nombre","RUT",
        "Fecha de Nacimiento","Estado Civil","Nacionalidad","Teléfono","Correo Electrónico",
        "Dirección","Comuna","Región","Cargo","Profesión",
        "Experiencia General (años)","Experiencia Específica (años)",
        "Talla Overol","Talla Zapatos","Licencia Conducir","Categoría","Especialidad","Notas",
    ]
    from openpyxl.styles import Font, PatternFill, Alignment
    fill = PatternFill("solid", fgColor="E30613")
    font = Font(bold=True, color="FFFFFF", size=10)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 20
    ws.row_dimensions[1].height = 35
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=plantilla_candidatos.xlsx"},
    )


# ── Catálogos y Stats ─────────────────────────────────────────────────────────────

REGIONES = [
    "Región de Arica y Parinacota","Región de Tarapacá","Región de Antofagasta",
    "Región de Atacama","Región de Coquimbo","Región de Valparaíso",
    "Región Metropolitana","Región del Libertador B. O'Higgins","Región del Maule",
    "Región de Ñuble","Región del Biobío","Región de La Araucanía",
    "Región de Los Ríos","Región de Los Lagos","Región de Aysén",
    "Región de Magallanes",
]

@app.get("/api/catalogos")
def catalogos():
    conn = get_db()
    c = conn.cursor()
    def uniq(col):
        c.execute(f"SELECT DISTINCT {col} FROM candidatos WHERE {col} IS NOT NULL AND {col}!='' ORDER BY {col}")
        return [r[col] for r in c.fetchall()]
    result = {
        "cargos":        uniq("cargo"),
        "profesiones":   uniq("profesion"),
        "regiones":      REGIONES,
        "categorias":    ["Supervisor","MM","Capataz","M1","M2","TIG","MIG","Cañería","Calderería","HDP","Oxigenista","Alta","Baja"],
        "especialidades":["OOCC","EEII","Mecánico","Estructura","Piping","Soldador","Rigger"],
        "espec_categorias":{
            "OOCC":      ["Supervisor","Capataz","M1","M2"],
            "EEII":      ["Supervisor","MM","Capataz","M1","M2"],
            "Mecánico":  ["Supervisor","MM","Capataz","M1","M2"],
            "Estructura":["Supervisor","MM","Capataz","M1","M2"],
            "Piping":    ["Supervisor","MM","Capataz","M1","M2"],
            "Soldador":  ["TIG","MIG","Cañería","Calderería","HDP","Oxigenista"],
            "Rigger":    ["Alta","Baja"],
        },
        "estados_civiles":["Soltero/a","Casado/a","Divorciado/a","Viudo/a","Conviviente civil"],
        "tallas_overol": ["XS","S","M","L","XL","XXL","XXXL"],
        "licencias":     ["No","A","A1","A2","A3","A4","B","C","D","E","F"],
        "tipos_doc":     TIPOS_DOC,
    }
    conn.close()
    return result

@app.get("/api/stats")
def stats(region: str = "", especialidad: str = "", categoria: str = "", estado: str = ""):
    from datetime import date
    conn = get_db()
    c = conn.cursor()

    # Filtro opcional del dashboard: mismos parámetros que /api/candidatos.
    # Un clic en el mapa o en una barra recalcula TODO con este WHERE, para que
    # mapa, anillos de completitud y el resto de los gráficos queden coherentes
    # entre sí sobre el mismo subconjunto (drill-down), en vez de solo filtrar
    # la lista de candidatos aparte.
    where_sql = "WHERE 1=1"
    where_params = []
    if region:
        where_sql += " AND c.region = %s"; where_params.append(region)
    if especialidad:
        where_sql += " AND contratacion_normalizar(c.especialidad) LIKE %s"; where_params.append(f"%{_norm_sql(especialidad)}%")
    if categoria:
        where_sql += " AND c.categoria = %s"; where_params.append(categoria)
    if estado:
        where_sql += " AND c.estado = %s"; where_params.append(estado)

    def n1(sql, params=()):
        c.execute(sql, params)
        return c.fetchone()["n"]

    n_cand    = n1(f"SELECT COUNT(*) n FROM candidatos c {where_sql}", where_params)
    n_ofertas = n1("SELECT COUNT(*) n FROM ofertas WHERE activa=1")  # las ofertas no son del candidato filtrado: se deja global
    n_docs    = n1(f"SELECT COUNT(*) n FROM documentos d JOIN candidatos c ON c.id = d.candidato_id {where_sql}", where_params)
    n_cv      = n1(f"SELECT COUNT(DISTINCT d.candidato_id) n FROM documentos d JOIN candidatos c ON c.id = d.candidato_id {where_sql} AND d.tipo='cv'", where_params)
    n_correo  = n1(f"SELECT COUNT(*) n FROM candidatos c {where_sql} AND c.correo IS NOT NULL AND c.correo!=''", where_params)
    n_tel     = n1(f"SELECT COUNT(*) n FROM candidatos c {where_sql} AND c.telefono IS NOT NULL AND c.telefono!=''", where_params)
    n_lic     = n1(f"SELECT COUNT(*) n FROM candidatos c {where_sql} AND c.licencia_conducir IS NOT NULL AND c.licencia_conducir NOT IN ('','No')", where_params)
    n_disp    = n1(f"SELECT COUNT(*) n FROM candidatos c {where_sql} AND (c.estado='Disponible' OR c.estado IS NULL)", where_params)
    mes = date.today().strftime("%Y-%m")
    n_mes = n1(f"SELECT COUNT(*) n FROM candidatos c {where_sql} AND to_char(c.created_at,'YYYY-MM') = %s", where_params + [mes])
    n_sin_docs = n1(f"SELECT COUNT(*) n FROM candidatos c {where_sql} AND NOT EXISTS (SELECT 1 FROM documentos d WHERE d.candidato_id=c.id)", where_params)
    n_sin_contacto = n1(f"SELECT COUNT(*) n FROM candidatos c {where_sql} AND (c.correo IS NULL OR c.correo='') AND (c.telefono IS NULL OR c.telefono='')", where_params)
    n_postulaciones_nuevas = n1(
        f"""SELECT COUNT(*) n FROM candidatos c {where_sql}
            AND c.origen='postulacion'
            AND c.created_at >= (now() AT TIME ZONE 'America/Santiago') - interval '1 day'""",
        where_params
    )
    c.execute(f"SELECT c.cargo cargo, COUNT(*) n FROM candidatos c {where_sql} AND c.cargo!='' GROUP BY c.cargo ORDER BY n DESC LIMIT 6", where_params)
    top_cargos        = [dict(r) for r in c.fetchall()]
    c.execute(f"SELECT c.region region, COUNT(*) n FROM candidatos c {where_sql} AND c.region!='' GROUP BY c.region ORDER BY n DESC LIMIT 6", where_params)
    top_regiones      = [dict(r) for r in c.fetchall()]
    c.execute(f"SELECT c.region region, COUNT(*) n FROM candidatos c {where_sql} AND c.region!='' GROUP BY c.region", where_params)
    _reg_counts        = {r["region"]: r["n"] for r in c.fetchall()}
    regiones_mapa      = [{"region": r, "n": _reg_counts.get(r, 0)} for r in REGIONES]
    c.execute(f"SELECT c.especialidad especialidad, COUNT(*) n FROM candidatos c {where_sql} AND c.especialidad!='' GROUP BY c.especialidad ORDER BY n DESC LIMIT 6", where_params)
    top_especialidades= [dict(r) for r in c.fetchall()]
    c.execute(f"SELECT c.categoria categoria, COUNT(*) n FROM candidatos c {where_sql} AND c.categoria!='' GROUP BY c.categoria ORDER BY n DESC", where_params)
    dist_categorias   = [dict(r) for r in c.fetchall()]
    c.execute(f"SELECT COALESCE(c.estado,'Disponible') estado, COUNT(*) n FROM candidatos c {where_sql} GROUP BY c.estado ORDER BY n DESC", where_params)
    dist_estados      = [dict(r) for r in c.fetchall()]
    c.execute(f"SELECT c.nacionalidad nacionalidad, COUNT(*) n FROM candidatos c {where_sql} AND c.nacionalidad!='' GROUP BY c.nacionalidad ORDER BY n DESC LIMIT 6", where_params)
    dist_nacionalidades=[dict(r) for r in c.fetchall()]
    conn.close()
    return {
        "n_candidatos": n_cand, "n_ofertas": n_ofertas, "n_documentos": n_docs,
        "n_cv": n_cv, "n_correo": n_correo, "n_telefono": n_tel,
        "n_con_licencia": n_lic, "n_disponibles": n_disp, "n_este_mes": n_mes,
        "n_sin_docs": n_sin_docs, "n_sin_contacto": n_sin_contacto,
        "n_postulaciones_nuevas": n_postulaciones_nuevas,
        "top_cargos": top_cargos, "top_regiones": top_regiones, "regiones_mapa": regiones_mapa,
        "top_especialidades": top_especialidades, "dist_categorias": dist_categorias,
        "dist_estados": dist_estados, "dist_nacionalidades": dist_nacionalidades,
    }


# ── Pipeline ──────────────────────────────────────────────────────────────────────

ETAPAS_PIPELINE = ["Postulado","En Revisión","Entrevista","Evaluación","Seleccionado","Descartado"]

@app.get("/api/pipeline")
def get_pipeline(oferta_id: int = 0):
    conn = get_db()
    c = conn.cursor()
    base = """SELECT p.*, c.primer_nombre, c.apellido_paterno, c.apellido_materno,
                     c.cargo as cand_cargo, c.experiencia_general, c.region as cand_region,
                     o.titulo as oferta_titulo
              FROM pipeline p
              JOIN candidatos c ON p.candidato_id = c.id
              JOIN ofertas o    ON p.oferta_id    = o.id"""
    if oferta_id:
        c.execute(base + " WHERE p.oferta_id=%s ORDER BY p.etapa, c.apellido_paterno", (oferta_id,))
    else:
        c.execute(base + " ORDER BY p.oferta_id, p.etapa, c.apellido_paterno")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"pipeline": rows, "etapas": ETAPAS_PIPELINE}

@app.post("/api/pipeline")
async def agregar_pipeline(request: Request):
    d = await request.json()
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(
            """INSERT INTO pipeline (candidato_id,oferta_id,etapa,score) VALUES (%s,%s,%s,%s)
               ON CONFLICT (candidato_id, oferta_id) DO UPDATE
               SET etapa = EXCLUDED.etapa, score = EXCLUDED.score,
                   updated_at = (now() AT TIME ZONE 'America/Santiago')
               RETURNING id""",
            (d["candidato_id"], d["oferta_id"], d.get("etapa","Postulado"), d.get("score",0))
        )
        new_id = c.fetchone()["id"]
        c.execute("INSERT INTO actividad (candidato_id,tipo,descripcion) VALUES (%s,%s,%s)",
                  (d["candidato_id"], "pipeline", f"Agregado al pipeline: {d.get('oferta_titulo','')}"))
        conn.commit()
        return {"ok": True, "id": new_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        conn.close()

@app.put("/api/pipeline/{pid}")
async def mover_pipeline(pid: int, request: Request):
    d = await request.json()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT p.*,o.titulo FROM pipeline p JOIN ofertas o ON p.oferta_id=o.id WHERE p.id=%s", (pid,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404)
    row = dict(row)
    nueva = d.get("etapa", row["etapa"])
    c.execute("UPDATE pipeline SET etapa=%s,updated_at=(now() AT TIME ZONE 'America/Santiago') WHERE id=%s", (nueva,pid))
    c.execute("INSERT INTO actividad (candidato_id,tipo,descripcion) VALUES (%s,%s,%s)",
              (row["candidato_id"],"pipeline",f"Movido a '{nueva}' en '{row['titulo']}'"))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/pipeline/{pid}")
def quitar_pipeline(pid: int):
    conn = get_db()
    conn.cursor().execute("DELETE FROM pipeline WHERE id=%s", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ── Actividad ─────────────────────────────────────────────────────────────────────

@app.get("/api/candidatos/{cid}/actividad")
def get_actividad(cid: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM actividad WHERE candidato_id=%s ORDER BY created_at DESC LIMIT 50", (cid,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"actividad": rows}

@app.post("/api/candidatos/{cid}/actividad")
async def add_actividad(cid: int, request: Request):
    d = await request.json()
    conn = get_db()
    conn.cursor().execute("INSERT INTO actividad (candidato_id,tipo,descripcion) VALUES (%s,%s,%s)",
                 (cid, d.get("tipo","nota"), d.get("descripcion","")))
    conn.commit()
    conn.close()
    return {"ok": True}

# ── Auto-match ───────────────────────────────────────────────────────────────────

@app.get("/api/candidatos/{cid}/auto-match")
def auto_match_candidato(cid: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT ca.*, COALESCE(d.cnt,0) as n_docs FROM candidatos ca
                 LEFT JOIN (SELECT candidato_id, COUNT(*) cnt FROM documentos GROUP BY candidato_id) d
                 ON ca.id=d.candidato_id WHERE ca.id=%s""", (cid,))
    cand = c.fetchone()
    if not cand:
        conn.close()
        raise HTTPException(404)
    cand = dict(cand)
    c.execute("SELECT * FROM ofertas WHERE activa=1")
    ofertas = [dict(r) for r in c.fetchall()]
    conn.close()
    matches = []
    for o in ofertas:
        m = _score_v2(cand, o)
        if m["score"] >= 55:
            matches.append({"oferta_id": o["id"], "titulo": o["titulo"], "cargo": o["cargo"], "score": m["score"]})
    matches.sort(key=lambda x: x["score"], reverse=True)
    return {"matches": matches, "total": len(matches)}

# ── Formulario público ────────────────────────────────────────────────────────────

@app.get("/postulacion")
def postulacion():
    return FileResponse(HERE / "postulacion.html")

# ── Main ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    def _open():
        time.sleep(2)
        webbrowser.open("http://localhost:3025")
    threading.Thread(target=_open, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=3025, log_level="info")
