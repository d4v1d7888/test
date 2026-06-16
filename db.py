"""
db.py
-----
Capa de acceso a MariaDB. Todas las operaciones con la base de datos
pasan por aquí, manteniendo la lógica de negocio separada en app.py.
"""

import pymysql
import pymysql.cursors
import config


def get_connection():
    """Abre y devuelve una conexión a MariaDB."""
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


# ── Catálogos ──────────────────────────────────────────────────────────────────

def get_catalog(table: str) -> list:
    """Devuelve todos los registros de una tabla de catálogo."""
    allowed = {"projects", "environments", "datacenters", "os_types", "exporters"}
    if table not in allowed:
        raise ValueError(f"Tabla de catálogo no válida: {table}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} ORDER BY id")
            return cur.fetchall()


def get_or_create_catalog(cur, table: str, field: str, value: str) -> int:
    """Devuelve el id del valor en el catálogo; lo crea si no existe."""
    cur.execute(f"SELECT id FROM {table} WHERE {field} = %s", (value,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute(f"INSERT INTO {table} ({field}) VALUES (%s)", (value,))
    return cur.lastrowid


# ── Target groups ──────────────────────────────────────────────────────────────

def list_groups(filters: dict = None) -> list:
    """
    Devuelve todos los target groups con conteo de targets.
    Filtros opcionales: source_file, environment, exporter, datacenter, os, project
    """
    where_clauses = []
    params = []

    if filters:
        mapping = {
            "source_file": "tg.source_file",
            "environment": "e.name",
            "exporter":    "ex.name",
            "datacenter":  "d.code",
            "os":          "o.name",
            "project":     "p.name",
        }
        for key, col in mapping.items():
            if filters.get(key):
                where_clauses.append(f"{col} = %s")
                params.append(filters[key])

    sql = "SELECT * FROM v_target_groups"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " ORDER BY source_file, env_order, exporter"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def get_group(group_id: int) -> dict | None:
    """Devuelve un target group con el listado completo de sus targets."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM v_target_groups WHERE id = %s", (group_id,))
            group = cur.fetchone()
            if not group:
                return None
            # Deserializar extra_labels si existe
            import json as _json
            if group.get("extra_labels") and isinstance(group["extra_labels"], str):
                try:
                    group["extra_labels"] = _json.loads(group["extra_labels"])
                except Exception:
                    group["extra_labels"] = {}
            cur.execute(
                "SELECT id, address, hostname, port, protocol, enabled "
                "FROM targets WHERE target_group_id = %s ORDER BY address",
                (group_id,),
            )
            group["targets"] = cur.fetchall()
            return group


def create_group(data: dict) -> int:
    """
    Crea un nuevo target group con sus targets.
    data debe contener: source_file, jobname, project, environment,
                        datacenter, os, exporter, targets (lista de strings)
    Opcionales: instance, module, port_label, alerting, extra_labels (dict)
    """
    import json as _json
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                project_id     = get_or_create_catalog(cur, "projects",     "name", data["project"])
                environment_id = get_or_create_catalog(cur, "environments", "name", data["environment"])
                datacenter_id  = get_or_create_catalog(cur, "datacenters",  "code", data["datacenter"])
                os_id          = get_or_create_catalog(cur, "os_types",     "name", data["os"])
                exporter_id    = get_or_create_catalog(cur, "exporters",    "name", data["exporter"])

                extra_labels = data.get("extra_labels")
                extra_labels_json = _json.dumps(extra_labels) if extra_labels else None

                cur.execute(
                    """INSERT INTO target_groups
                       (source_file, jobname, project_id, environment_id, datacenter_id,
                        os_id, exporter_id, instance, module, port_label, alerting, extra_labels)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        data["source_file"],
                        data["jobname"],
                        project_id,
                        environment_id,
                        datacenter_id,
                        os_id,
                        exporter_id,
                        data.get("instance"),
                        data.get("module"),
                        data.get("port_label"),
                        1 if data.get("alerting", True) else 0,
                        extra_labels_json,
                    ),
                )
                group_id = cur.lastrowid

                _insert_targets(cur, group_id, data.get("targets", []))

                _log(cur, data["source_file"], "CREATE",
                     f"Nuevo grupo id={group_id} jobname='{data['jobname']}'")
                conn.commit()
                return group_id
            except Exception:
                conn.rollback()
                raise


def update_group(group_id: int, data: dict) -> bool:
    """
    Actualiza los labels de un target group existente.
    Solo modifica los campos presentes en data.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT source_file, jobname FROM target_groups WHERE id = %s", (group_id,))
                existing = cur.fetchone()
                if not existing:
                    return False

                fields = []
                params = []

                # Campos directos
                for col in ("source_file", "jobname", "instance", "module", "port_label"):
                    if col in data:
                        fields.append(f"{col} = %s")
                        params.append(data[col])

                if "alerting" in data:
                    fields.append("alerting = %s")
                    params.append(1 if data["alerting"] else 0)

                # FK a catálogos
                catalog_map = [
                    ("project",     "project_id",     "projects",     "name"),
                    ("environment", "environment_id",  "environments", "name"),
                    ("datacenter",  "datacenter_id",   "datacenters",  "code"),
                    ("os",          "os_id",           "os_types",     "name"),
                    ("exporter",    "exporter_id",     "exporters",    "name"),
                ]
                for key, col, table, field in catalog_map:
                    if key in data:
                        fk_id = get_or_create_catalog(cur, table, field, data[key])
                        fields.append(f"{col} = %s")
                        params.append(fk_id)

                if fields:
                    params.append(group_id)
                    cur.execute(
                        f"UPDATE target_groups SET {', '.join(fields)} WHERE id = %s",
                        params,
                    )

                # Si el body incluye 'targets', reemplazar los existentes
                if "targets" in data and isinstance(data["targets"], list):
                    cur.execute("DELETE FROM targets WHERE target_group_id = %s", (group_id,))
                    if data["targets"]:
                        _insert_targets(cur, group_id, data["targets"])

                source_file = data.get("source_file", existing["source_file"])
                _log(cur, source_file, "UPDATE", f"Grupo id={group_id} modificado")
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise


def delete_group(group_id: int) -> bool:
    """Elimina un target group y sus targets (ON DELETE CASCADE)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT source_file, jobname FROM target_groups WHERE id = %s", (group_id,))
                row = cur.fetchone()
                if not row:
                    return False
                cur.execute("DELETE FROM target_groups WHERE id = %s", (group_id,))
                _log(cur, row["source_file"], "DELETE",
                     f"Eliminado grupo id={group_id} jobname='{row['jobname']}'")
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise


# ── Targets individuales ───────────────────────────────────────────────────────

def add_targets(group_id: int, addresses: list) -> list:
    """Añade hosts a un grupo existente. Devuelve los ids creados."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT source_file FROM target_groups WHERE id = %s", (group_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Grupo {group_id} no encontrado")
                new_ids = _insert_targets(cur, group_id, addresses)
                _log(cur, row["source_file"], "UPDATE",
                     f"Añadidos {len(addresses)} targets al grupo id={group_id}")
                conn.commit()
                return new_ids
            except Exception:
                conn.rollback()
                raise


def update_target(target_id: int, address: str) -> bool:
    """Modifica la dirección de un target existente."""
    from urllib.parse import urlparse

    def _parse(addr):
        if addr.startswith(("http://", "https://")):
            p = urlparse(addr)
            return p.hostname, p.port, p.scheme
        if ":" in addr:
            h, p = addr.rsplit(":", 1)
            return h, int(p) if p.isdigit() else None, None
        return addr, None, None

    hostname, port, protocol = _parse(address)

    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT tg.source_file FROM targets t "
                    "JOIN target_groups tg ON tg.id = t.target_group_id "
                    "WHERE t.id = %s",
                    (target_id,),
                )
                row = cur.fetchone()
                if not row:
                    return False
                cur.execute(
                    "UPDATE targets SET address=%s, hostname=%s, port=%s, protocol=%s WHERE id=%s",
                    (address, hostname, port, protocol, target_id),
                )
                _log(cur, row["source_file"], "UPDATE",
                     f"Target id={target_id} modificado a '{address}'")
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise


def delete_target(target_id: int) -> bool:
    """Elimina un target individual."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT t.address, tg.source_file FROM targets t "
                    "JOIN target_groups tg ON tg.id = t.target_group_id "
                    "WHERE t.id = %s",
                    (target_id,),
                )
                row = cur.fetchone()
                if not row:
                    return False
                cur.execute("DELETE FROM targets WHERE id = %s", (target_id,))
                _log(cur, row["source_file"], "DELETE",
                     f"Eliminado target id={target_id} '{row['address']}'")
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise


# ── Sync log ───────────────────────────────────────────────────────────────────

def get_sync_log(limit: int = 50) -> list:
    """Devuelve las últimas entradas del log de cambios."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM sync_log ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()


def log_sync_export(source_file: str, user_ip: str = None):
    """Registra en el log que se ha exportado (regenerado) un fichero JSON."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            _log(cur, source_file, "EXPORT",
                 f"Fichero {source_file} regenerado", user_ip)
            conn.commit()


# ── Mapeo fichero → proyecto Git ──────────────────────────────────────────────

def get_file_projects() -> dict:
    """
    Devuelve el mapeo completo {source_file: project} desde la BD.
    Se combina con GIT_PROJECTS de config.py (la BD tiene prioridad).
    """
    import config as _config
    # Partir de la configuración estática
    mapping = {}
    for project, proj_cfg in getattr(_config, "GIT_PROJECTS", {}).items():
        for f in proj_cfg.get("files", []):
            mapping[f] = project

    # Sobreescribir con los valores dinámicos de la BD
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT source_file, project FROM git_file_projects")
                for row in cur.fetchall():
                    mapping[row["source_file"]] = row["project"]
    except Exception:
        pass  # Si la tabla no existe aún, usar solo config.py

    return mapping


def set_file_project(source_file: str, project: str):
    """Asigna un fichero a un proyecto git, guardándolo en BD."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO git_file_projects (source_file, project)
                   VALUES (%s, %s)
                   ON DUPLICATE KEY UPDATE project = VALUES(project)""",
                (source_file, project)
            )
        conn.commit()


def delete_file_project(source_file: str):
    """Elimina el mapeo de un fichero en BD."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM git_file_projects WHERE source_file = %s",
                (source_file,)
            )
        conn.commit()


# ── Helpers privados ──────────────────────────────────────────────────────────

def _insert_targets(cur, group_id: int, addresses: list) -> list:
    """Inserta una lista de addresses en la tabla targets. Devuelve ids."""
    from urllib.parse import urlparse

    new_ids = []
    for address in addresses:
        if address.startswith(("http://", "https://")):
            p = urlparse(address)
            hostname, port, protocol = p.hostname, p.port, p.scheme
        elif ":" in address:
            parts = address.rsplit(":", 1)
            hostname = parts[0]
            port = int(parts[1]) if parts[1].isdigit() else None
            protocol = None
        else:
            hostname, port, protocol = address, None, None

        cur.execute(
            "INSERT INTO targets (target_group_id, address, hostname, port, protocol) "
            "VALUES (%s, %s, %s, %s, %s)",
            (group_id, address, hostname, port, protocol),
        )
        new_ids.append(cur.lastrowid)
    return new_ids


def _log(cur, source_file: str, action: str, detail: str, user_ip: str = None):
    """Inserta una entrada en sync_log (usa el cursor activo para respetar la transacción)."""
    cur.execute(
        "INSERT INTO sync_log (source_file, action, detail, user_ip) VALUES (%s,%s,%s,%s)",
        (source_file, action, detail, user_ip),
    )


def get_source_files() -> list:
    """Devuelve la lista de ficheros JSON que tienen al menos un target group."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT source_file FROM target_groups ORDER BY source_file"
            )
            return [row["source_file"] for row in cur.fetchall()]


def get_groups_by_file(source_file: str) -> list:
    """Devuelve todos los grupos de un fichero concreto con sus targets."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM target_groups WHERE source_file = %s", (source_file,)
            )
            group_ids = [r["id"] for r in cur.fetchall()]

    return [get_group(gid) for gid in group_ids]
