"""
app.py
------
API REST del Prometheus Target Manager.

Arranque:
    python3 app.py

O con un servidor WSGI en producción:
    gunicorn -w 4 -b 0.0.0.0:5000 app:app
"""

import logging
from datetime import datetime, timezone, timedelta
from functools import wraps

import bcrypt
import jwt
from flask import Flask, jsonify, request, abort

import config
import db
import json_manager

# ── Configuración del logger ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if config.DEBUG else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── App Flask ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = config.SECRET_KEY


# ── Helpers ────────────────────────────────────────────────────────────────────

def ok(data=None, status=200, **kwargs):
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(kwargs)
    return jsonify(payload), status


def err(message: str, status=400):
    return jsonify({"ok": False, "error": message}), status


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr)


def current_username():
    return getattr(request, 'current_user', None)


def _regenerate_after(source_file: str):
    """Regenera el JSON de Prometheus y loguea cualquier error sin abortar."""
    try:
        json_manager.regenerate(source_file, user_ip=client_ip(),
                                username=current_username())
    except Exception as e:
        logger.error("Error regenerando %s: %s", source_file, e)


# ── Serialización de fechas ────────────────────────────────────────────────────
import flask.json.provider as _prov
from datetime import date

class _DateEncoder(_prov.DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)

app.json_provider_class = _DateEncoder
app.json = _DateEncoder(app)

# ── JWT helpers ────────────────────────────────────────────────────────────────
JWT_ALGORITHM  = "HS256"
JWT_EXPIRY_H   = 8  # horas de validez del token

def _make_token(user: dict) -> str:
    payload = {
        "sub":      user["username"],
        "role":     user["role"],
        "exp":      datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_H),
        "iat":      datetime.now(timezone.utc),
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, config.SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def require_auth(f):
    """Decorador: exige token JWT válido en la cabecera Authorization: Bearer <token>."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"ok": False, "error": "Token requerido"}), 401
        token = auth_header[7:]
        payload = _decode_token(token)
        if not payload:
            return jsonify({"ok": False, "error": "Token inválido o expirado"}), 401
        request.current_user  = payload["sub"]
        request.current_role  = payload["role"]
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """Decorador: exige rol admin (además de token válido)."""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if request.current_role != "admin":
            return jsonify({"ok": False, "error": "Se requiere rol admin"}), 403
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True)
    if not data or not data.get("username") or not data.get("password"):
        return err("Se requieren username y password", 400)

    user = db.get_user(data["username"])
    if not user or not user.get("enabled"):
        return err("Credenciales incorrectas", 401)

    try:
        valid = bcrypt.checkpw(data["password"].encode(), user["password_hash"].encode())
    except Exception:
        valid = False

    if not valid:
        return err("Credenciales incorrectas", 401)

    db.update_last_login(user["id"])
    token = _make_token(user)
    logger.info("Login: usuario '%s' autenticado", user["username"])
    return ok({
        "token":    token,
        "username": user["username"],
        "role":     user["role"],
        "expires_in": JWT_EXPIRY_H * 3600,
    })


@app.get("/api/auth/me")
@require_auth
def me():
    """GET /api/auth/me — Devuelve el usuario autenticado actual."""
    return ok({"username": request.current_user, "role": request.current_role})


# ── Protección global de rutas /api/ ──────────────────────────────────────────
WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
PUBLIC_PATHS  = {"/api/auth/login", "/health"}

@app.before_request
def check_auth():
    path = request.path
    if path in PUBLIC_PATHS:
        return
    if not path.startswith("/api/"):
        return
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"ok": False, "error": "Token requerido"}), 401
    payload = _decode_token(auth_header[7:])
    if not payload:
        return jsonify({"ok": False, "error": "Token inválido o expirado"}), 401
    request.current_user = payload["sub"]
    request.current_role = payload["role"]
    if request.method in WRITE_METHODS and request.current_role != "admin":
        return jsonify({"ok": False, "error": "Se requiere rol admin para modificar datos"}), 403


# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    try:
        conn = db.get_connection()
        conn.close()
        bd_ok = True
    except Exception:
        bd_ok = False
    return ok({"api": True, "db": bd_ok})


# ═══════════════════════════════════════════════════════════════════════════════
#  CATÁLOGOS
# ═══════════════════════════════════════════════════════════════════════════════

CATALOG_MAP = {
    "projects":     "projects",
    "environments": "environments",
    "datacenters":  "datacenters",
    "os":           "os_types",
    "exporters":    "exporters",
}

@app.get("/api/catalogs/<resource>")
def get_catalog(resource):
    table = CATALOG_MAP.get(resource)
    if not table:
        return err(f"Recurso de catálogo no válido: {resource}. "
                   f"Opciones: {list(CATALOG_MAP.keys())}", 404)
    return ok(db.get_catalog(table))


# ═══════════════════════════════════════════════════════════════════════════════
#  TARGET GROUPS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/groups")
def list_groups():
    filters = {k: request.args.get(k) for k in
               ("source_file", "environment", "exporter", "datacenter", "os", "project")}
    groups = db.list_groups(filters)
    return ok(groups, total=len(groups))


@app.get("/api/groups/<int:group_id>")
def get_group(group_id):
    group = db.get_group(group_id)
    if not group:
        return err(f"Grupo {group_id} no encontrado", 404)
    return ok(group)


@app.post("/api/groups")
def create_group():
    data = request.get_json(silent=True)
    if not data:
        return err("Body JSON requerido")
    required = ("source_file", "jobname", "project", "environment",
                "datacenter", "os", "exporter")
    missing = [f for f in required if not data.get(f)]
    if missing:
        return err(f"Campos obligatorios: {missing}")
    if not data.get("targets"):
        return err("Debe incluir al menos un target en el array 'targets'")
    try:
        # Si viene git_project, guardarlo en BD ANTES del push para que
        # _git_push use la rama correcta desde el primer momento
        git_project = data.get("git_project")
        if git_project:
            db.set_file_project(data["source_file"], git_project)
            logger.info("Proyecto git '%s' asignado a '%s' antes del push",
                        git_project, data["source_file"])

        group_id = db.create_group(data, user_ip=client_ip(), username=current_username())
        _regenerate_after(data["source_file"])
        return ok({"id": group_id}, status=201, message="Grupo creado correctamente")
    except Exception as e:
        logger.exception("Error creando grupo")
        return err(str(e), 500)


@app.put("/api/groups/<int:group_id>")
def update_group(group_id):
    data = request.get_json(silent=True)
    if not data:
        return err("Body JSON requerido")
    group = db.get_group(group_id)
    if not group:
        return err(f"Grupo {group_id} no encontrado", 404)
    old_file = group["source_file"]
    new_file = data.get("source_file", old_file)
    try:
        updated = db.update_group(group_id, data, user_ip=client_ip(), username=current_username())
        if not updated:
            return err(f"Grupo {group_id} no encontrado", 404)
        _regenerate_after(old_file)
        if new_file != old_file:
            _regenerate_after(new_file)
        return ok(message="Grupo actualizado correctamente")
    except Exception as e:
        logger.exception("Error actualizando grupo %d", group_id)
        return err(str(e), 500)


@app.delete("/api/groups/<int:group_id>")
def delete_group(group_id):
    group = db.get_group(group_id)
    if not group:
        return err(f"Grupo {group_id} no encontrado", 404)
    source_file = group["source_file"]
    try:
        db.delete_group(group_id, user_ip=client_ip(), username=current_username())
        _regenerate_after(source_file)
        return ok(message=f"Grupo {group_id} eliminado correctamente")
    except Exception as e:
        logger.exception("Error eliminando grupo %d", group_id)
        return err(str(e), 500)


# ═══════════════════════════════════════════════════════════════════════════════
#  TARGETS INDIVIDUALES
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/groups/<int:group_id>/targets")
def add_targets(group_id):
    data = request.get_json(silent=True)
    if not data or not data.get("targets"):
        return err("Array 'targets' requerido")
    group = db.get_group(group_id)
    if not group:
        return err(f"Grupo {group_id} no encontrado", 404)
    try:
        new_ids = db.add_targets(group_id, data["targets"],
                                 user_ip=client_ip(), username=current_username())
        _regenerate_after(group["source_file"])
        return ok({"created_ids": new_ids}, status=201,
                  message=f"{len(new_ids)} targets añadidos")
    except Exception as e:
        logger.exception("Error añadiendo targets al grupo %d", group_id)
        return err(str(e), 500)


@app.put("/api/targets/<int:target_id>")
def update_target(target_id):
    data = request.get_json(silent=True)
    if not data or not data.get("address"):
        return err("Campo 'address' requerido")
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tg.source_file FROM targets t "
                    "JOIN target_groups tg ON tg.id = t.target_group_id WHERE t.id = %s", (target_id,)
                )
                row = cur.fetchone()
        if not row:
            return err(f"Target {target_id} no encontrado", 404)
        source_file = row["source_file"]
        updated = db.update_target(target_id, data["address"],
                                   user_ip=client_ip(), username=current_username())
        if not updated:
            return err(f"Target {target_id} no encontrado", 404)
        _regenerate_after(source_file)
        return ok(message="Target actualizado correctamente")
    except Exception as e:
        logger.exception("Error actualizando target %d", target_id)
        return err(str(e), 500)


@app.delete("/api/targets/<int:target_id>")
def delete_target(target_id):
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tg.source_file FROM targets t "
                    "JOIN target_groups tg ON tg.id = t.target_group_id WHERE t.id = %s", (target_id,)
                )
                row = cur.fetchone()
        if not row:
            return err(f"Target {target_id} no encontrado", 404)
        source_file = row["source_file"]
        db.delete_target(target_id, user_ip=client_ip(), username=current_username())
        _regenerate_after(source_file)
        return ok(message=f"Target {target_id} eliminado correctamente")
    except Exception as e:
        logger.exception("Error eliminando target %d", target_id)
        return err(str(e), 500)


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILIDADES
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/sync/<source_file>")
def force_sync(source_file):
    if ".." in source_file or "/" in source_file:
        return err("Nombre de fichero no válido", 400)
    try:
        path = json_manager.regenerate(source_file, user_ip=client_ip())
        result = json_manager.validate_json(source_file)
        return ok({"file": str(path), "validation": result},
                  message="Fichero regenerado correctamente")
    except Exception as e:
        logger.exception("Error en sync de %s", source_file)
        return err(str(e), 500)


@app.get("/api/sync_log")
def get_sync_log():
    try:
        limit = int(request.args.get("limit", 50))
        limit = min(max(limit, 1), 500)
    except ValueError:
        limit = 50
    return ok(db.get_sync_log(limit), total=limit)


@app.get("/api/git_projects")
def get_git_projects():
    projects = {}
    for name, proj in getattr(config, "GIT_PROJECTS", {}).items():
        projects[name] = {
            "branch": proj.get("branch", config.GIT_BRANCH),
            "files":  list(proj.get("files", [])),
        }
    try:
        file_projects = db.get_file_projects()
        for f, project_name in file_projects.items():
            if project_name not in projects:
                projects[project_name] = {"branch": config.GIT_BRANCH, "files": []}
            if f not in projects[project_name]["files"]:
                projects[project_name]["files"].append(f)
    except Exception:
        pass
    return ok(projects)


@app.post("/api/git_projects/assign")
def assign_file_project():
    data = request.get_json(silent=True)
    if not data or not data.get("source_file") or not data.get("project"):
        return err("Se requieren source_file y project")
    source_file = data["source_file"]
    project     = data["project"]
    try:
        db.set_file_project(source_file, project)
        _sync_config_projects()
        return ok(message=f"'{source_file}' asignado al proyecto {project}")
    except Exception as e:
        logger.exception("Error asignando proyecto a %s", source_file)
        return err(str(e), 500)


def _sync_config_projects():
    """
    Sincroniza la sección GIT_PROJECTS del config.py con el estado real
    de la tabla git_file_projects en BD. Reconstruye las listas 'files'
    de cada proyecto desde cero, usando SOLO la BD como fuente de verdad.
    """
    import re
    import os

    config_path = os.path.join(os.path.dirname(__file__), "config.py")

    # Leer SOLO desde git_file_projects en BD (no get_file_projects que mezcla con config)
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT source_file, project FROM git_file_projects")
                rows = cur.fetchall()
    except Exception as e:
        logger.error("config.py sync: no se pudo leer git_file_projects: %s", e)
        return

    # Agrupar ficheros por proyecto
    project_files: dict = {}
    for row in rows:
        pname = row["project"]
        fname = row["source_file"]
        project_files.setdefault(pname, [])
        if fname not in project_files[pname]:
            project_files[pname].append(fname)

    # Leer config.py
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.error("config.py sync: no se pudo leer el fichero: %s", e)
        return

    # Reconstruir cada bloque "files": [...] en GIT_PROJECTS
    def rebuild_files(m):
        before = m.group(1)
        close  = m.group(3)
        project_key_match = re.search(
            r'"([^"]+)"\s*:\s*\{[^{]*$', content[:m.start()], re.DOTALL
        )
        if not project_key_match:
            return m.group(0)

        proj_key = project_key_match.group(1)
        files = sorted(project_files.get(proj_key, []))

        if not files:
            return before + "\n        " + close

        indent = "            "
        entries = "\n".join(f'{indent}"{f}",' for f in files)
        return before + "\n" + entries + "\n        " + close

    pattern = r'("files"\s*:\s*\[)(.*?)(\])'
    new_content = re.sub(pattern, rebuild_files, content, flags=re.DOTALL)

    if new_content != content:
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            logger.info("config.py sincronizado: %s", project_files)
        except Exception as e:
            logger.error("config.py sync: no se pudo escribir: %s", e)
    else:
        logger.debug("config.py sync: sin cambios necesarios")


@app.delete("/api/files/<source_file>")
def delete_file(source_file):
    """
    DELETE /api/files/proyecto_1.json
    Elimina todos los grupos y targets del fichero de la BD,
    purga catálogos huérfanos y borra el fichero JSON del disco + push a git.
    """
    if ".." in source_file or "/" in source_file:
        return err("Nombre de fichero no válido", 400)
    try:
        groups = db.get_groups_by_file(source_file)
        if not groups:
            return err(f"No existe ningún grupo para '{source_file}'", 404)

        # Eliminar grupos (targets en cascada), purgar catálogos (SIN tocar git_file_projects)
        # y loguear — el proyecto debe seguir en BD hasta después del push git
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM target_groups WHERE source_file = %s", (source_file,))
                db._purge_orphan_catalogs(cur)
                cur.execute(
                    "INSERT INTO sync_log (source_file, action, detail, user_ip, username) "
                    "VALUES (%s, 'DELETE', %s, %s, %s)",
                    (source_file, f"Fichero completo eliminado: {len(groups)} grupos",
                     client_ip(), current_username())
                )
            conn.commit()

        # PRIMERO hacer el push git (necesita el proyecto en BD para saber la rama)
        json_manager.regenerate(source_file, user_ip=client_ip(),
                                username=current_username())

        # DESPUÉS eliminar la asignación de proyecto y sincronizar config.py
        db.delete_file_project(source_file)
        _sync_config_projects()

        return ok(message=f"Fichero '{source_file}' eliminado ({len(groups)} grupos)")
    except Exception as e:
        logger.exception("Error eliminando fichero %s", source_file)
        return err(str(e), 500)


@app.get("/api/admin/users")
def admin_list_users():
    """GET /api/admin/users — Lista todos los usuarios (solo admin)."""
    if request.current_role != "admin":
        return err("Se requiere rol admin", 403)
    try:
        users = db.list_users()
        # No devolver password_hash
        return ok(users)
    except Exception as e:
        logger.exception("Error listando usuarios")
        return err(str(e), 500)


@app.post("/api/admin/users")
def admin_create_user():
    """
    POST /api/admin/users
    Body: { "username": "david", "password": "xxxx", "role": "readonly" }
    """
    if request.current_role != "admin":
        return err("Se requiere rol admin", 403)
    data = request.get_json(silent=True)
    if not data or not data.get("username") or not data.get("password"):
        return err("Se requieren username y password")
    role = data.get("role", "readonly")
    if role not in ("admin", "readonly"):
        return err("Rol no válido: 'admin' o 'readonly'")
    try:
        new_id = db.create_user(data["username"], data["password"], role)
        logger.info("Usuario '%s' creado por '%s'", data["username"], current_username())
        return ok({"id": new_id}, status=201, message=f"Usuario '{data['username']}' creado")
    except Exception as e:
        if "Duplicate entry" in str(e):
            return err(f"El usuario '{data['username']}' ya existe", 409)
        logger.exception("Error creando usuario")
        return err(str(e), 500)


@app.post("/api/admin/users/<int:user_id>/toggle")
def admin_toggle_user(user_id):
    """POST /api/admin/users/3/toggle — Activa o desactiva un usuario."""
    if request.current_role != "admin":
        return err("Se requiere rol admin", 403)
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    try:
        ok_ = db.set_user_enabled(user_id, enabled)
        if not ok_:
            return err(f"Usuario {user_id} no encontrado", 404)
        return ok(message=f"Usuario {'activado' if enabled else 'desactivado'}")
    except Exception as e:
        logger.exception("Error toggling usuario %d", user_id)
        return err(str(e), 500)


@app.post("/api/admin/password")
def admin_change_password():
    """
    POST /api/admin/password
    Body: { "current_password": "old", "new_password": "new" }
    Cambia la contraseña del usuario autenticado actualmente.
    """
    data = request.get_json(silent=True)
    if not data or not data.get("current_password") or not data.get("new_password"):
        return err("Se requieren current_password y new_password")
    if len(data["new_password"]) < 8:
        return err("La nueva contraseña debe tener al menos 8 caracteres")

    username = current_username()
    user = db.get_user(username)
    if not user:
        return err("Usuario no encontrado", 404)

    try:
        valid = bcrypt.checkpw(data["current_password"].encode(), user["password_hash"].encode())
    except Exception:
        valid = False

    if not valid:
        return err("Contraseña actual incorrecta", 401)

    try:
        db.change_password(username, data["new_password"])
        logger.info("Contraseña cambiada para usuario '%s'", username)
        return ok(message="Contraseña actualizada correctamente")
    except Exception as e:
        logger.exception("Error cambiando contraseña de '%s'", username)
        return err(str(e), 500)


# ═══════════════════════════════════════════════════════════════════════════════
#  FILES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/files")
def list_files():
    db_files   = set(db.get_source_files())
    disk_files = set(json_manager.list_json_files())
    return ok({
        "in_db":        sorted(db_files),
        "on_disk":      sorted(disk_files),
        "only_in_db":   sorted(db_files - disk_files),
        "only_on_disk": sorted(disk_files - db_files),
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  ARRANQUE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(host=config.API_HOST, port=config.API_PORT, debug=config.DEBUG)
