#!/usr/bin/env python3
"""
import_targets.py
-----------------
Importa uno o varios ficheros JSON de targets de Prometheus
a la base de datos MariaDB del Prometheus Target Manager.

Uso:
    python3 import_targets.py --file targets.json
    python3 import_targets.py --file proyecto_1.json --file proyecto_2.json
    python3 import_targets.py --dir /etc/prometheus/targets/

Requisitos:
    pip install pymysql
"""

import json
import re
import sys
import argparse
from pathlib import Path
from urllib.parse import urlparse

import pymysql
import pymysql.cursors

# ── Configuración de conexión ──────────────────────────────────────────────────
DB_CONFIG = {
    "host":     "localhost",
    "port":     3306,
    "user":     "prometheus_user",
    "password": "password",
    "database": "prometheus_targets",
    "charset":  "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}
# ──────────────────────────────────────────────────────────────────────────────


def parse_address(address: str) -> dict:
    """
    Extrae hostname, puerto y protocolo de una dirección target.
    Soporta los tres formatos del JSON real:
      - "host:port"              → hostname=host, port=port, protocol=None
      - "http://host:port/path"  → hostname=host, port=port, protocol=http
      - "hostname"               → hostname=hostname, port=None, protocol=None
    """
    result = {"hostname": None, "port": None, "protocol": None}

    if address.startswith(("http://", "https://")):
        parsed = urlparse(address)
        result["protocol"] = parsed.scheme
        result["hostname"] = parsed.hostname
        result["port"] = parsed.port
    elif ":" in address:
        parts = address.rsplit(":", 1)
        result["hostname"] = parts[0]
        try:
            result["port"] = int(parts[1])
        except ValueError:
            result["hostname"] = address  # no era un puerto
    else:
        result["hostname"] = address

    return result


def get_or_create(cursor, table: str, field: str, value: str) -> int:
    """
    Devuelve el id del registro con field=value en table.
    Si no existe, lo crea y devuelve el nuevo id.
    Soporta tablas con campo 'name' o 'code'.
    """
    cursor.execute(f"SELECT id FROM {table} WHERE {field} = %s", (value,))
    row = cursor.fetchone()
    if row:
        return row["id"]
    cursor.execute(f"INSERT INTO {table} ({field}) VALUES (%s)", (value,))
    return cursor.lastrowid


def import_file(conn, json_path: Path):
    """Importa todos los target groups de un fichero JSON."""
    print(f"\n→ Importando: {json_path.name}")

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    source_file = json_path.name
    imported = 0
    skipped = 0

    with conn.cursor() as cur:
        # Registrar inicio en sync_log
        cur.execute(
            "INSERT INTO sync_log (source_file, action, detail) VALUES (%s, 'IMPORT', %s)",
            (source_file, f"Importación inicial desde {json_path}"),
        )

        for entry in data:
            labels = entry.get("labels", {})
            targets_list = entry.get("targets", [])

            if not targets_list or not labels:
                skipped += 1
                continue

            # Resolver FKs a catálogos (crea el valor si no existe)
            project_id     = get_or_create(cur, "projects",     "name", labels.get("PROJECT",     "Sin proyecto"))
            environment_id = get_or_create(cur, "environments", "name", labels.get("ENVIRONMENT", "UNKNOWN"))
            datacenter_id  = get_or_create(cur, "datacenters",  "code", labels.get("DATACENTER",  "UNKNOWN"))
            os_id          = get_or_create(cur, "os_types",     "name", labels.get("OS",          "Unknown"))
            exporter_id    = get_or_create(cur, "exporters",    "name", labels.get("EXPORTER",    "Unknown"))

            alerting = 1 if labels.get("ALERTING", "true").lower() == "true" else 0

            # Insertar target_group
            cur.execute(
                """
                INSERT INTO target_groups
                    (source_file, jobname, project_id, environment_id, datacenter_id,
                     os_id, exporter_id, instance, module, port_label, alerting)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source_file,
                    labels.get("jobname", ""),
                    project_id,
                    environment_id,
                    datacenter_id,
                    os_id,
                    exporter_id,
                    labels.get("instance"),
                    labels.get("module"),
                    labels.get("PORT"),
                    alerting,
                ),
            )
            group_id = cur.lastrowid

            # Insertar cada target individual
            for address in targets_list:
                parsed = parse_address(address)
                cur.execute(
                    """
                    INSERT INTO targets
                        (target_group_id, address, hostname, port, protocol)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        group_id,
                        address,
                        parsed["hostname"],
                        parsed["port"],
                        parsed["protocol"],
                    ),
                )

            imported += 1
            print(f"   ✓ Grupo '{labels.get('jobname', '?')}' — {len(targets_list)} targets")

    conn.commit()
    print(f"   Resultado: {imported} grupos importados, {skipped} saltados")


def main():
    parser = argparse.ArgumentParser(description="Importa targets JSON a MariaDB")
    parser.add_argument("--file", action="append", dest="files", metavar="FICHERO",
                        help="Fichero JSON a importar (repetible)")
    parser.add_argument("--dir",  dest="directory", metavar="DIRECTORIO",
                        help="Directorio con ficheros *.json a importar")
    args = parser.parse_args()

    paths = []
    if args.files:
        for f in args.files:
            p = Path(f)
            if not p.exists():
                print(f"ERROR: No se encuentra el fichero {f}")
                sys.exit(1)
            paths.append(p)

    if args.directory:
        d = Path(args.directory)
        if not d.is_dir():
            print(f"ERROR: No se encuentra el directorio {args.directory}")
            sys.exit(1)
        paths.extend(sorted(d.glob("*.json")))

    if not paths:
        parser.print_help()
        sys.exit(1)

    print("Conectando a MariaDB...")
    try:
        conn = pymysql.connect(**DB_CONFIG)
    except pymysql.Error as e:
        print(f"ERROR de conexión: {e}")
        sys.exit(1)

    try:
        for path in paths:
            import_file(conn, path)
        print("\n✅ Importación completada.")
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Error durante la importación: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
