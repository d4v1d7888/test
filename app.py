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
from datetime import datetime

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


def _regenerate_after(source_file: str):
    """Regenera el JSON de Prometheus y loguea cualquier error sin abortar."""
    try:
        json_manager.regenerate(source_file, user_ip=client_ip())
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


# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    """Comprueba que la API y la BD están operativas."""
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
    """
    GET /api/catalogs/environments
    GET /api/catalogs/exporters
    GET /api/catalogs/datacenters
    GET /api/catalogs/os
    GET /api/catalogs/projects
    """
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
    """
    GET /api/groups
    Parámetros de filtro opcionales (query string):
      ?source_file=targets.json
      ?environment=PRO
      ?exporter=Node+Exporter
      ?datacenter=AH
      ?os=Linux
      ?project=CBGI+Monitoring
    """
    filters = {k: request.args.get(k) for k in
               ("source_file", "environment", "exporter", "datacenter", "os", "project")}
    groups = db.list_groups(filters)
    return ok(groups, total=len(groups))


@app.get("/api/groups/<int:group_id>")
def get_group(group_id):
    """GET /api/groups/42  →  grupo con sus targets."""
    group = db.get_group(group_id)
    if not group:
        return err(f"Grupo {group_id} no encontrado", 404)
    return ok(group)


@app.post("/api/groups")
def create_group():
    """
    POST /api/groups
    Body JSON requerido:
    {
        "source_file":  "proyecto_1.json",
        "jobname":      "Node Exporter DES",
        "project":      "CBGI Monitoring",
        "environment":  "DES",
        "datacenter":   "AH",
        "os":           "Linux",
        "exporter":     "Node Exporter",
        "targets":      ["host01:9100", "host02:9100"],
        "instance":     null,
        "module":       null,
        "port_label":   null,
        "alerting":     true
    }
    """
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
        group_id = db.create_group(data)
        _regenerate_after(data["source_file"])
        return ok({"id": group_id}, status=201, message="Grupo creado correctamente")
    except Exception as e:
        logger.exception("Error creando grupo")
        return err(str(e), 500)


@app.put("/api/groups/<int:group_id>")
def update_group(group_id):
    """
    PUT /api/groups/42
    Body JSON con los campos a modificar (parcial):
    {
        "environment": "PRO",
        "exporter":    "WMI Exporter"
    }
    Al modificar source_file, el JSON se regenera en el nuevo fichero.
    """
    data = request.get_json(silent=True)
    if not data:
        return err("Body JSON requerido")

    # Obtener source_file actual antes de modificar (para saber qué JSON regenerar)
    group = db.get_group(group_id)
    if not group:
        return err(f"Grupo {group_id} no encontrado", 404)

    old_file = group["source_file"]
    new_file = data.get("source_file", old_file)

    try:
        updated = db.update_group(group_id, data)
        if not updated:
            return err(f"Grupo {group_id} no encontrado", 404)

        # Si cambió el source_file hay que regenerar ambos
        _regenerate_after(old_file)
        if new_file != old_file:
            _regenerate_after(new_file)

        return ok(message="Grupo actualizado correctamente")
    except Exception as e:
        logger.exception("Error actualizando grupo %d", group_id)
        return err(str(e), 500)


@app.delete("/api/groups/<int:group_id>")
def delete_group(group_id):
    """DELETE /api/groups/42"""
    group = db.get_group(group_id)
    if not group:
        return err(f"Grupo {group_id} no encontrado", 404)

    source_file = group["source_file"]
    try:
        db.delete_group(group_id)
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
    """
    POST /api/groups/42/targets
    Body JSON:
    {
        "targets": ["newhost01:9100", "newhost02:9100"]
    }
    """
    data = request.get_json(silent=True)
    if not data or not data.get("targets"):
        return err("Array 'targets' requerido")

    group = db.get_group(group_id)
    if not group:
        return err(f"Grupo {group_id} no encontrado", 404)

    try:
        new_ids = db.add_targets(group_id, data["targets"])
        _regenerate_after(group["source_file"])
        return ok({"created_ids": new_ids}, status=201,
                  message=f"{len(new_ids)} targets añadidos")
    except Exception as e:
        logger.exception("Error añadiendo targets al grupo %d", group_id)
        return err(str(e), 500)


@app.put("/api/targets/<int:target_id>")
def update_target(target_id):
    """
    PUT /api/targets/123
    Body JSON:
    {
        "address": "newhost01:9100"
    }
    """
    data = request.get_json(silent=True)
    if not data or not data.get("address"):
        return err("Campo 'address' requerido")

    # Obtener source_file para regenerar después
    try:
        import pymysql
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tg.source_file FROM targets t "
                    "JOIN target_groups tg ON tg.id = t.target_group_id "
                    "WHERE t.id = %s", (target_id,)
                )
                row = cur.fetchone()
        if not row:
            return err(f"Target {target_id} no encontrado", 404)

        source_file = row["source_file"]
        updated = db.update_target(target_id, data["address"])
        if not updated:
            return err(f"Target {target_id} no encontrado", 404)

        _regenerate_after(source_file)
        return ok(message="Target actualizado correctamente")
    except Exception as e:
        logger.exception("Error actualizando target %d", target_id)
        return err(str(e), 500)


@app.delete("/api/targets/<int:target_id>")
def delete_target(target_id):
    """DELETE /api/targets/123"""
    try:
        import pymysql
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tg.source_file FROM targets t "
                    "JOIN target_groups tg ON tg.id = t.target_group_id "
                    "WHERE t.id = %s", (target_id,)
                )
                row = cur.fetchone()
        if not row:
            return err(f"Target {target_id} no encontrado", 404)

        source_file = row["source_file"]
        db.delete_target(target_id)
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
    """
    POST /api/sync/targets.json
    Fuerza la regeneración del fichero JSON desde MariaDB.
    Útil si el fichero en disco se ha corrompido o eliminado.
    """
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
    """
    GET /api/sync_log
    GET /api/sync_log?limit=100
    """
    try:
        limit = int(request.args.get("limit", 50))
        limit = min(max(limit, 1), 500)
    except ValueError:
        limit = 50
    return ok(db.get_sync_log(limit), total=limit)


@app.get("/api/git_projects")
def get_git_projects():
    """
    Devuelve la configuración de proyectos Git con sus ficheros asignados.
    Combina config.py (ramas/subdirs) con el mapeo dinámico de la BD.
    """
    # Estructura base de proyectos desde config.py
    projects = {}
    for name, proj in getattr(config, "GIT_PROJECTS", {}).items():
        projects[name] = {
            "branch": proj.get("branch", config.GIT_BRANCH),
            "files":  list(proj.get("files", [])),
        }

    # Añadir ficheros del mapeo dinámico de BD
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
    """
    POST /api/git_projects/assign
    Body: { "source_file": "bbva_rico.json", "project": "ONPREM" }
    Asigna un fichero a un proyecto git: guarda en BD y actualiza config.py.
    """
    data = request.get_json(silent=True)
    if not data or not data.get("source_file") or not data.get("project"):
        return err("Se requieren source_file y project")

    source_file = data["source_file"]
    project     = data["project"]

    try:
        # 1. Guardar en BD
        db.set_file_project(source_file, project)

        # 2. Actualizar config.py añadiendo el fichero a la lista del proyecto
        _update_config_project(source_file, project)

        return ok(message=f"'{source_file}' asignado al proyecto {project}")
    except Exception as e:
        logger.exception("Error asignando proyecto a %s", source_file)
        return err(str(e), 500)


def _update_config_project(source_file: str, project: str):
    """
    Añade source_file a la lista 'files' del proyecto en config.py.
    Si el fichero ya está en otro proyecto, lo mueve.
    Opera directamente sobre el texto del fichero para preservar comentarios y formato.
    """
    import re
    import os

    config_path = os.path.join(os.path.dirname(__file__), "config.py")

    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Quitar el fichero de cualquier proyecto donde ya esté
    # Busca líneas del tipo:  "fichero.json",  o  'fichero.json',
    escaped = re.escape(source_file)
    content = re.sub(
        r'[ \t]*["\']' + escaped + r'["\'],?\s*\n',
        '',
        content
    )

    # Añadir el fichero al proyecto correcto
    # Busca el bloque "PROYECTO": { ... "files": [ ... ] }
    # e inserta justo antes del cierre del array
    pattern = r'("' + re.escape(project) + r'"\s*:\s*\{[^}]*?"files"\s*:\s*\[)(.*?)(\])'

    def inserter(m):
        before  = m.group(1)
        current = m.group(2)
        close   = m.group(3)
        # Evitar duplicados
        if source_file in current:
            return m.group(0)
        # Calcular indentación: buscar la de las líneas existentes o usar 12 espacios
        indent_match = re.search(r'\n(\s+)["\']', current)
        indent = indent_match.group(1) if indent_match else "            "
        # Añadir la entrada
        entry = f'\n{indent}"{source_file}",'
        return before + current + entry + "\n        " + close

    new_content = re.sub(pattern, inserter, content, flags=re.DOTALL)

    if new_content != content:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        logger.info("config.py actualizado: '%s' añadido a proyecto %s", source_file, project)
    else:
        logger.warning("config.py: no se encontró el proyecto '%s' para insertar '%s'",
                       project, source_file)


@app.delete("/api/files/<source_file>")
def delete_file(source_file):
    """
    DELETE /api/files/proyecto_1.json
    Elimina todos los grupos y targets del fichero de la BD
    y borra el fichero JSON del disco + push a git.
    """
    if ".." in source_file or "/" in source_file:
        return err("Nombre de fichero no válido", 400)
    try:
        # Contar grupos antes de borrar
        groups = db.get_groups_by_file(source_file)
        if not groups:
            return err(f"No existe ningún grupo para '{source_file}'", 404)

        # Eliminar todos los grupos (targets en cascada)
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM target_groups WHERE source_file = %s", (source_file,))
                cur.execute(
                    "INSERT INTO sync_log (source_file, action, detail) VALUES (%s, 'DELETE', %s)",
                    (source_file, f"Fichero completo eliminado: {len(groups)} grupos")
                )
            conn.commit()

        # Borrar el fichero del disco y hacer git rm + push
        json_manager.regenerate(source_file, user_ip=client_ip())  # genera [] → dispara delete en git

        return ok(message=f"Fichero '{source_file}' eliminado ({len(groups)} grupos)")
    except Exception as e:
        logger.exception("Error eliminando fichero %s", source_file)
        return err(str(e), 500)


@app.get("/api/files")
def list_files():
    """Lista los ficheros JSON conocidos en BD y en disco."""
    db_files  = set(db.get_source_files())
    disk_files = set(json_manager.list_json_files())
    return ok({
        "in_db":      sorted(db_files),
        "on_disk":    sorted(disk_files),
        "only_in_db": sorted(db_files - disk_files),
        "only_on_disk": sorted(disk_files - db_files),
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  ARRANQUE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(host=config.API_HOST, port=config.API_PORT, debug=config.DEBUG)

