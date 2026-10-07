-- Esquema Postgres de contratacion-personal-mineria en QA (192.168.10.17/srvpd_bd).
-- Schema: cia_reclutamiento_min (preaprovisionado, owner flesan_peru — el único
-- usuario compartido de QA). Aislamiento = search_path fijado SOLO a este schema,
-- nunca ",public": si una tabla no está acá, la query debe fallar, no caer a otro
-- schema. Ver README de ~/config y CLAUDE.md de servicios-compartidos.
--
-- Se aplica desde init_db() en api.py (CREATE TABLE IF NOT EXISTS, igual que hacía
-- el init_db() de SQLite). Este archivo es la referencia legible, no se corre a
-- mano salvo para inspección manual con psql/DBeaver.

-- ── Normalización sin tildes (reemplaza el norm() UDF de SQLite) ───────────────
CREATE OR REPLACE FUNCTION contratacion_normalizar(txt TEXT) RETURNS TEXT AS $$
  SELECT lower(translate(coalesce(txt, ''),
    'áàäâãÁÀÄÂÃéèëêÉÈËÊíìïîÍÌÏÎóòöôõÓÒÖÔÕúùüûÚÙÜÛñÑçÇ',
    'aaaaaAAAAAeeeeEEEEiiiiIIIIooooOOOOOuuuuUUUUnNcC'
  ));
$$ LANGUAGE SQL IMMUTABLE;

-- ── Candidatos ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS candidatos (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    apellido_paterno        TEXT,
    apellido_materno        TEXT,
    primer_nombre           TEXT,
    segundo_nombre          TEXT,
    rut                     TEXT UNIQUE,
    fecha_nacimiento        TEXT,
    estado_civil            TEXT,
    nacionalidad            TEXT DEFAULT 'Chilena',
    telefono                TEXT,
    correo                  TEXT,
    direccion               TEXT,
    comuna                  TEXT,
    region                  TEXT,
    cargo                   TEXT,
    profesion               TEXT,
    experiencia_general     INTEGER DEFAULT 0,
    experiencia_especifica  INTEGER DEFAULT 0,
    talla_overol            TEXT,
    talla_zapatos           TEXT,
    licencia_conducir       TEXT,
    categoria               TEXT,
    especialidad            TEXT,
    notas                   TEXT,
    estado                  TEXT DEFAULT 'Disponible',
    creado_por              TEXT DEFAULT '',
    origen                  TEXT DEFAULT 'manual',
    -- Consentimiento de tratamiento de datos (Ley 19.628 / Ley 21.719). Se pide
    -- y se guarda en cada envío del formulario público, con fecha, para tener
    -- evidencia real — no solo un candado visual en el frontend.
    acepta_datos            BOOLEAN DEFAULT FALSE,
    acepta_datos_fecha      TIMESTAMP,
    created_at              TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago'),
    updated_at              TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago')
);
ALTER TABLE candidatos ADD COLUMN IF NOT EXISTS acepta_datos BOOLEAN DEFAULT FALSE;
ALTER TABLE candidatos ADD COLUMN IF NOT EXISTS acepta_datos_fecha TIMESTAMP;

CREATE TABLE IF NOT EXISTS documentos (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidato_id  INTEGER NOT NULL REFERENCES candidatos(id) ON DELETE CASCADE,
    tipo          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    filepath      TEXT NOT NULL,
    uploaded_at   TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago')
);

CREATE TABLE IF NOT EXISTS ofertas (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    titulo           TEXT NOT NULL,
    cargo            TEXT,
    profesion        TEXT,
    categoria        TEXT,
    especialidad     TEXT,
    experiencia_min  INTEGER DEFAULT 0,
    region           TEXT,
    descripcion      TEXT,
    activa           INTEGER DEFAULT 1,
    creado_por       TEXT DEFAULT '',
    created_at       TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago')
);

CREATE TABLE IF NOT EXISTS pipeline (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidato_id INTEGER NOT NULL REFERENCES candidatos(id) ON DELETE CASCADE,
    oferta_id    INTEGER NOT NULL REFERENCES ofertas(id)    ON DELETE CASCADE,
    etapa        TEXT NOT NULL DEFAULT 'Postulado',
    score        REAL DEFAULT 0,
    created_at   TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago'),
    updated_at   TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago'),
    UNIQUE(candidato_id, oferta_id)
);

CREATE TABLE IF NOT EXISTS actividad (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidato_id INTEGER NOT NULL REFERENCES candidatos(id) ON DELETE CASCADE,
    tipo         TEXT NOT NULL DEFAULT 'nota',
    descripcion  TEXT,
    created_at   TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago')
);

-- ── WhatsApp masivo (job en background + cola por candidato) ───────────────────
-- whatsapp_lotes = el envío masivo como job; whatsapp_envios = la cola, una fila
-- por candidato, para saber exactamente qué falta sin parsear texto de
-- `actividad`. Permite pollear progreso, cancelar a mitad de camino y retomar
-- tras un restart del proceso sin reenviar lo ya enviado (ver init_db()/lifespan
-- en api.py).
CREATE TABLE IF NOT EXISTS whatsapp_lotes (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mensaje      TEXT NOT NULL,
    total        INTEGER NOT NULL,
    enviados     INTEGER NOT NULL DEFAULT 0,
    fallidos     INTEGER NOT NULL DEFAULT 0,
    estado       TEXT NOT NULL DEFAULT 'en_curso', -- en_curso | completado | cancelado
    creado_por   TEXT DEFAULT '',
    created_at   TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago'),
    updated_at   TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago')
);

CREATE TABLE IF NOT EXISTS whatsapp_envios (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lote_id      INTEGER NOT NULL REFERENCES whatsapp_lotes(id) ON DELETE CASCADE,
    candidato_id INTEGER NOT NULL REFERENCES candidatos(id) ON DELETE CASCADE,
    estado       TEXT NOT NULL DEFAULT 'pendiente', -- pendiente | enviado | fallido
    motivo       TEXT,
    enviado_at   TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_wa_envios_lote_estado ON whatsapp_envios(lote_id, estado);

-- ── Usuarios (login CIA, modelo estilo plataforma-sync-360) ────────────────────
-- Auto-alta al primer login de un correo del dominio permitido. CIA_ADMIN_EMAILS
-- es solo "bootstrap": si el correo está en esa lista y la fila NO existe todavía,
-- se crea como admin; si no, como member. Logins siguientes usan el rol que quedó
-- guardado acá (permite promover/degradar a mano). `activo=false` vetа el acceso
-- aunque el dominio siga siendo válido — única forma de bloquear a alguien.
CREATE TABLE IF NOT EXISTS usuarios (
    email       TEXT PRIMARY KEY,
    sub         TEXT,
    nombre      TEXT,
    picture     TEXT,
    role        TEXT NOT NULL DEFAULT 'member',
    activo      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMP DEFAULT (now() AT TIME ZONE 'America/Santiago'),
    last_login  TIMESTAMP
);
