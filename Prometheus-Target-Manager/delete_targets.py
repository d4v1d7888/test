#!/usr/bin/env python3
"""
delete_targets.py
-----------------
Elimina de MariaDB todos los target groups (y sus targets) asociados
a uno o varios ficheros JSON de Prometheus.

Es el complemento inverso de import_targets.py — úsalo cuando retires
un fichero JSON del directorio de Prometheus y quieras que la BD
quede limpia.

Uso:
    # Eliminar los datos de un fichero concreto
    python3 delete_targets.py --file proyecto_1.json

    # Eliminar varios ficheros a la vez
    python3 delete_targets.py --file proyecto_1.json --file proyecto_2.json

    # Ver qué ficheros hay en BD sin eliminar nada
    python3 delete_targets.py --list

    # Simular la eliminación sin ejecutarla (dry-run)
    python3 delete_targets.py --file proyecto_1.json --dry-run

Requisitos:
    pip install pymysql
"""

import sys
import argparse

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


def list_files(conn):
    """Muestra los ficheros presentes en BD con su conteo de grupos y targets."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                tg.source_file,
                COUNT(DISTINCT tg.id)  AS grupos,
                COUNT(t.id)            AS targets
            FROM target_groups tg
            LEFT JOIN targets t ON t.target_group_id = tg.id
            GROUP BY tg.source_file
            ORDER BY tg.source_file
        """)
        rows = cur.fetchall()

    if not rows:
        print("No hay ficheros en la base de datos.")
        return

    print(f"\n{'Fichero':<40} {'Grupos':>7} {'Targets':>8}")
    print("─" * 58)
    for r in rows:
        print(f"{r['source_file']:<40} {r['grupos']:>7} {r['targets']:>8}")
    print()


def delete_file(conn, source_file: str, dry_run: bool = False):
    """Elimina todos los grupos y targets asociados a source_file."""
    with conn.cursor() as cur:
        # Cuántos grupos y targets se van a borrar
        cur.execute("""
            SELECT COUNT(DISTINCT tg.id) AS grupos, COUNT(t.id) AS targets
            FROM target_groups tg
            LEFT JOIN targets t ON t.target_group_id = tg.id
            WHERE tg.source_file = %s
        """, (source_file,))
        stats = cur.fetchone()

        if not stats or stats["grupos"] == 0:
            print(f"  ⚠  '{source_file}' no existe en la base de datos.")
            return

        print(f"  → '{source_file}': {stats['grupos']} grupos, {stats['targets']} targets")

        if dry_run:
            print("     [DRY-RUN] No se ha eliminado nada.")
            return

        # Los targets se eliminan en cascada por ON DELETE CASCADE
        cur.execute(
            "DELETE FROM target_groups WHERE source_file = %s",
            (source_file,)
        )
        affected = cur.rowcount

        # Registrar en sync_log
        cur.execute(
            "INSERT INTO sync_log (source_file, action, detail) VALUES (%s, 'DELETE', %s)",
            (source_file, f"Eliminados {affected} grupos via delete_targets.py")
        )

    conn.commit()
    print(f"     ✓ Eliminado correctamente.")


def main():
    parser = argparse.ArgumentParser(
        description="Elimina targets de un fichero JSON de la base de datos MariaDB"
    )
    parser.add_argument(
        "--file", action="append", dest="files", metavar="FICHERO",
        help="Nombre del fichero JSON a eliminar de BD (ej: proyecto_1.json). Repetible."
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Muestra los ficheros presentes en BD sin eliminar nada"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simula la eliminación sin ejecutarla"
    )
    args = parser.parse_args()

    if not args.list and not args.files:
        parser.print_help()
        sys.exit(1)

    print("Conectando a MariaDB...")
    try:
        conn = pymysql.connect(**DB_CONFIG)
    except pymysql.Error as e:
        print(f"ERROR de conexión: {e}")
        sys.exit(1)

    try:
        if args.list:
            list_files(conn)

        if args.files:
            if args.dry_run:
                print("\n[DRY-RUN] Simulación — no se elimina nada:\n")
            else:
                print()

            for f in args.files:
                delete_file(conn, f, dry_run=args.dry_run)

            if not args.dry_run:
                print("\n✅ Operación completada.")
            else:
                print("\n[DRY-RUN] Fin de la simulación. Ejecuta sin --dry-run para eliminar.")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()