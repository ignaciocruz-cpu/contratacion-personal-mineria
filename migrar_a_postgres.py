# -*- coding: utf-8 -*-
"""Migración única SQLite → Postgres QA (schema cia_reclutamiento_min).

Uso manual, una sola vez (o repetible: es idempotente por tabla vía verificación
de conteo — si el destino ya tiene filas, no vuelve a insertar esa tabla).

Preserva los `id` originales tal cual (documentos.filepath y las FKs de
pipeline/actividad dependen del id de candidatos, y las carpetas en uploads/
están nombradas por ese id) y al final reancla las secuencias IDENTITY de cada
tabla a MAX(id)+1 para que los próximos INSERT sigan de ahí.

    .venv/bin/python migrar_a_postgres.py
"""
import sqlite3
from pathlib import Path

import psycopg2
import psycopg2.extras

HERE = Path(__file__).parent
SQLITE_PATH = HERE / "flesan_recluta.db"

TABLAS = ["candidatos", "documentos", "ofertas", "pipeline", "actividad"]


def _leer_catalogo():
    """Parser mínimo del catálogo (mismo formato que `set -a; source` de bash):
    admite comentarios al final de línea después de un valor entre comillas."""
    env = {}
    with open("/home/desarrollo/config/connections.env") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if v[:1] in ("'", '"'):
                quote = v[0]
                end = v.find(quote, 1)
                v = v[1:end] if end != -1 else v[1:]
            else:
                v = v.split("#", 1)[0].strip()
            env[k] = v
    return env


def main():
    env = _leer_catalogo()
    schema = env["CONN_QA_RECLUTAMIENTOMIN_SCHEMA"]

    sconn = sqlite3.connect(str(SQLITE_PATH))
    sconn.row_factory = sqlite3.Row

    pconn = psycopg2.connect(
        host=env["CONN_QA_HOST"], port=env["CONN_QA_PORT"], dbname=env["CONN_QA_DB"],
        user=env["CONN_QA_USER"], password=env["CONN_QA_PASS"],
        options=f"-c search_path={schema}",
    )
    pconn.autocommit = False
    pcur = pconn.cursor()

    for tabla in TABLAS:
        pcur.execute(f"SELECT COUNT(*) FROM {tabla}")
        ya_tiene = pcur.fetchone()[0]
        if ya_tiene:
            print(f"[skip] {tabla}: ya tiene {ya_tiene} filas en Postgres, no se reimporta.")
            continue

        scur = sconn.cursor()
        scur.execute(f"SELECT * FROM {tabla}")
        filas = scur.fetchall()
        if not filas:
            print(f"[ok] {tabla}: 0 filas en origen, nada que migrar.")
            continue

        cols = filas[0].keys()
        col_list = ", ".join(cols)
        ph = ", ".join(["%s"] * len(cols))
        overriding = " OVERRIDING SYSTEM VALUE" if "id" in cols else ""
        insert_sql = f"INSERT INTO {tabla} ({col_list}){overriding} VALUES ({ph})"

        datos = [tuple(f[c] for c in cols) for f in filas]
        psycopg2.extras.execute_batch(pcur, insert_sql, datos, page_size=200)

        # Reancla la secuencia IDENTITY al máximo id migrado.
        if "id" in cols:
            pcur.execute(
                f"SELECT setval(pg_get_serial_sequence('{tabla}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {tabla}), 1))"
            )

        print(f"[ok] {tabla}: {len(datos)} filas migradas.")

    pconn.commit()

    print("\nVerificación de conteos:")
    for tabla in TABLAS:
        scur = sconn.cursor()
        scur.execute(f"SELECT COUNT(*) FROM {tabla}")
        n_sqlite = scur.fetchone()[0]
        pcur.execute(f"SELECT COUNT(*) FROM {tabla}")
        n_pg = pcur.fetchone()[0]
        estado = "OK" if n_sqlite == n_pg else "¡DISTINTO!"
        print(f"  {tabla}: sqlite={n_sqlite} postgres={n_pg}  {estado}")

    pcur.close()
    pconn.close()
    sconn.close()


if __name__ == "__main__":
    main()
