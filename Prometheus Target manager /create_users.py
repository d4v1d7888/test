#!/usr/bin/env python3
"""
create_users.py
---------------
Crea o actualiza usuarios en la base de datos.
Ejecutar una vez tras aplicar add_users.sql.
 
Uso:
    python3 create_users.py                      # crea admin y readonly con passwords por defecto
    python3 create_users.py --user admin --role admin        # crear/actualizar usuario
    python3 create_users.py --list                           # listar usuarios existentes
    python3 create_users.py --disable readonly               # deshabilitar usuario
 
Requisitos:
    pip install pymysql bcrypt
"""
 
import sys
import argparse
import getpass
import pymysql
import pymysql.cursors
import bcrypt
 
DB_CONFIG = {
    "host":     "localhost",
    "port":     3306,
    "user":     "prometheus_user",
    "password": "password",
    "database": "prometheus_targets",
    "charset":  "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}
 
 
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
 
 
def create_or_update_user(conn, username: str, password: str, role: str):
    hashed = hash_password(password)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO users (username, password_hash, role)
               VALUES (%s, %s, %s)
               ON DUPLICATE KEY UPDATE
                 password_hash = VALUES(password_hash),
                 role          = VALUES(role),
                 enabled       = 1""",
            (username, hashed, role)
        )
    conn.commit()
    print(f"  ✓ Usuario '{username}' ({role}) creado/actualizado.")
 
 
def list_users(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, role, enabled, last_login, created_at FROM users ORDER BY id")
        rows = cur.fetchall()
    if not rows:
        print("  Sin usuarios en la BD.")
        return
    print(f"\n  {'ID':<4} {'Usuario':<20} {'Rol':<10} {'Activo':<8} {'Último login'}")
    print("  " + "─" * 60)
    for r in rows:
        login = str(r["last_login"])[:16] if r["last_login"] else "—"
        activo = "✓" if r["enabled"] else "✗"
        print(f"  {r['id']:<4} {r['username']:<20} {r['role']:<10} {activo:<8} {login}")
 
 
def disable_user(conn, username: str):
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET enabled = 0 WHERE username = %s", (username,))
        if cur.rowcount == 0:
            print(f"  ⚠ Usuario '{username}' no encontrado.")
            return
    conn.commit()
    print(f"  ✓ Usuario '{username}' deshabilitado.")
 
 
def main():
    parser = argparse.ArgumentParser(description="Gestión de usuarios del Prometheus Target Manager")
    parser.add_argument("--user",    help="Nombre de usuario a crear/actualizar")
    parser.add_argument("--role",    choices=["admin", "readonly"], default="readonly")
    parser.add_argument("--list",    action="store_true", help="Listar usuarios")
    parser.add_argument("--disable", metavar="USERNAME", help="Deshabilitar usuario")
    args = parser.parse_args()
 
    print("Conectando a MariaDB...")
    try:
        conn = pymysql.connect(**DB_CONFIG)
    except pymysql.Error as e:
        print(f"ERROR de conexión: {e}")
        sys.exit(1)
 
    try:
        if args.list:
            list_users(conn)
 
        elif args.disable:
            disable_user(conn, args.disable)
 
        elif args.user:
            password = getpass.getpass(f"Password para '{args.user}': ")
            if len(password) < 8:
                print("ERROR: la password debe tener al menos 8 caracteres.")
                sys.exit(1)
            create_or_update_user(conn, args.user, password, args.role)
 
        else:
            # Modo interactivo: crear usuarios por defecto con passwords pedidas por consola
            print("\nCreando usuarios iniciales del sistema:\n")
            print("  1. admin (rol: admin) — acceso completo")
            admin_pwd = getpass.getpass("  Password para 'admin': ")
            if len(admin_pwd) < 8:
                print("ERROR: mínimo 8 caracteres.")
                sys.exit(1)
            create_or_update_user(conn, "admin", admin_pwd, "admin")
 
            print("\n  2. readonly (rol: readonly) — solo lectura")
            ro_pwd = getpass.getpass("  Password para 'readonly': ")
            if len(ro_pwd) < 8:
                print("ERROR: mínimo 8 caracteres.")
                sys.exit(1)
            create_or_update_user(conn, "readonly", ro_pwd, "readonly")
 
            print("\n✅ Usuarios creados. Ya puedes acceder al frontend.")
 
    finally:
        conn.close()
 
 
if __name__ == "__main__":
    main()
