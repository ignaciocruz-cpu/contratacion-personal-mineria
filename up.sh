#!/bin/bash
# Levanta el stack inyectando el catálogo central de conexiones.
#
#   ~/config/connections.env  -> provee CONN_QA_* para interpolar en docker-compose.yml
#   ./.env                    -> provee EVOLUTION_*, GOOGLE_*, CIA_*, SESSION_*
#
# Uso:
#   ./up.sh                 -> docker compose up -d
#   ./up.sh up -d --build   -> pasa los args tal cual
#   ./up.sh logs -f
#   ./up.sh down
#
# IMPORTANTE: usá SIEMPRE este script en vez de `docker compose ...` directo.
# Las credenciales de BD NO viven en el .env de este repo: salen del catálogo
# y se pasan a docker-compose.yml. Si falta el catálogo o la variable de
# schema (CONN_QA_RECLUTAMIENTOMIN_SCHEMA), el arranque falla ruidosamente
# (los `:?` del compose) en vez de quedar apuntando a una BD vacía o al schema
# de otra plataforma — en QA todas comparten el usuario flesan_peru, el schema
# es lo ÚNICO que las separa.
#
# A diferencia de las apps Next.js del parque, acá NO se arma un DATABASE_URL
# (string URI): la app usa psycopg2.connect() con parámetros sueltos
# (DB_HOST/DB_PORT/...), así que no hace falta percent-encodear la contraseña.
set -euo pipefail
cd "$(dirname "$0")"

# Dónde vive el catálogo. Es un dato de la máquina, no de la cuenta: uno solo, en el home
# de `desarrollo`, que lee todo el grupo. Buscarlo únicamente en $HOME lo rompía apenas
# alguien entraba con su propio usuario, porque resolvía a /home/<quien-sea>/config.
CATALOGO_COMPARTIDO=/home/desarrollo/config/connections.env
CATALOG="${CONNECTIONS_ENV:-}"
if [ -z "$CATALOG" ]; then
  for candidato in "$HOME/config/connections.env" "$CATALOGO_COMPARTIDO"; do
    if [ -f "$candidato" ]; then CATALOG="$candidato"; break; fi
  done
  CATALOG="${CATALOG:-$CATALOGO_COMPARTIDO}"
fi
if [ ! -f "$CATALOG" ]; then
  echo "ERROR: no existe el catálogo de conexiones: $CATALOG" >&2
  exit 1
fi
if [ ! -r "$CATALOG" ]; then
  echo "ERROR: el catálogo existe pero tu usuario no puede leerlo: $CATALOG" >&2
  echo "       Lo arregla, para todo el grupo, ~/config/acceso-multiusuario.sh" >&2
  exit 1
fi
if [ ! -f ./.env ]; then
  echo "ERROR: falta ./.env — copialo con: cp .env.example .env" >&2
  exit 1
fi
if [ ! -r ./.env ]; then
  echo "ERROR: no puedes leer $(pwd)/.env" >&2
  echo "       Lo arregla, para todo el grupo, ~/config/acceso-multiusuario.sh" >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  set -- up -d
fi

exec docker compose \
  --env-file "$CATALOG" \
  --env-file ./.env \
  "$@"
