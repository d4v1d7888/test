"""
json_manager.py
---------------
Gestión de los ficheros JSON de file_sd_config de Prometheus.
 
Estrategia git: clone temporal por operación.
Cada push clona la rama en un directorio temporal, aplica el cambio
y hace push. Al terminar el directorio se elimina. Esto garantiza
un estado limpio en cada operación, sin dependencias de worktrees
que puedan quedar inconsistentes.
"""
 
import json
import os
import shutil
import subprocess
import tempfile
import logging
from pathlib import Path
 
import config
import db
 
logger = logging.getLogger(__name__)
 
 
# ── Helpers de ruta ───────────────────────────────────────────────────────────
 
def _json_path(source_file: str) -> Path:
    return Path(config.TARGETS_DIR) / source_file
 
 
def _resolve_project(source_file: str) -> dict:
    """
    Resuelve qué proyecto/rama/subdir corresponde a source_file.
    Orden de prioridad:
      1. Tabla git_file_projects de BD (asignación dinámica desde el frontend)
      2. Lista 'files' de GIT_PROJECTS en config.py (asignación estática)
      3. Fallback: rama GIT_BRANCH por defecto
    La búsqueda en GIT_PROJECTS es case-insensitive para evitar errores
    de capitalización entre lo que guarda la BD y las claves de config.
    """
    try:
        file_projects = db.get_file_projects()
    except Exception:
        file_projects = {}
 
    project_name = file_projects.get(source_file)
 
    git_projects = getattr(config, "GIT_PROJECTS", {})
 
    if project_name:
        # Búsqueda exacta primero, luego case-insensitive
        proj_cfg = git_projects.get(project_name)
        if proj_cfg is None:
            # Intentar case-insensitive
            for key, val in git_projects.items():
                if key.upper() == project_name.upper():
                    proj_cfg = val
                    logger.warning(
                        "Git: proyecto '%s' (BD) coincide con '%s' (config) "
                        "solo en case-insensitive — revisa las mayúsculas",
                        project_name, key
                    )
                    break
 
        if proj_cfg:
            result = {
                "name":   project_name,
                "branch": proj_cfg.get("branch", config.GIT_BRANCH),
                "subdir": proj_cfg.get("subdir", "").strip("/"),
            }
            logger.info("Git: '%s' → proyecto=%s rama=%s (desde BD)",
                        source_file, result["name"], result["branch"])
            return result
        else:
            logger.warning(
                "Git: proyecto '%s' (de BD) no encontrado en GIT_PROJECTS de config.py "
                "— usando rama por defecto '%s'",
                project_name, config.GIT_BRANCH
            )
 
    # Fallback: buscar en la lista 'files' estática de config.py
    for proj_key, proj_cfg in git_projects.items():
        if source_file in proj_cfg.get("files", []):
            result = {
                "name":   proj_key,
                "branch": proj_cfg.get("branch", config.GIT_BRANCH),
                "subdir": proj_cfg.get("subdir", "").strip("/"),
            }
            logger.info("Git: '%s' → proyecto=%s rama=%s (desde config.py files)",
                        source_file, result["name"], result["branch"])
            return result
 
    # Fallback final
    logger.warning("Git: '%s' no asignado a ningún proyecto — usando rama por defecto '%s'",
                   source_file, config.GIT_BRANCH)
    return {"name": "default", "branch": config.GIT_BRANCH, "subdir": ""}
 
 
# ── Helpers git ───────────────────────────────────────────────────────────────
 
def _git_bin() -> str | None:
    import shutil as _shutil
    git = _shutil.which("git")
    if git:
        return git
    for c in ["/usr/bin/git", "/usr/local/bin/git", "/bin/git"]:
        if Path(c).exists():
            return c
    return None
 
 
def _git_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    env["GIT_AUTHOR_NAME"]     = config.GIT_USER_NAME
    env["GIT_AUTHOR_EMAIL"]    = config.GIT_USER_EMAIL
    env["GIT_COMMITTER_NAME"]  = config.GIT_USER_NAME
    env["GIT_COMMITTER_EMAIL"] = config.GIT_USER_EMAIL
    env["GIT_TERMINAL_PROMPT"] = "0"
    if getattr(config, "GIT_SSL_NO_VERIFY", False):
        env["GIT_SSL_NO_VERIFY"] = "1"
    return env
 
 
def _run(args: list, cwd: Path, env: dict, timeout: int = 60):
    """Ejecuta un comando. Devuelve (ok, stdout, stderr)."""
    try:
        r = subprocess.run(
            args, cwd=str(cwd), env=env,
            capture_output=True, text=True, timeout=timeout
        )
        ok = r.returncode == 0
        if not ok:
            logger.debug("CMD %s → rc=%d\nSTDOUT: %s\nSTDERR: %s",
                         " ".join(args), r.returncode,
                         r.stdout.strip(), r.stderr.strip())
        return ok, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        logger.error("Timeout ejecutando: %s", " ".join(args))
        return False, "", "timeout"
    except Exception as e:
        logger.error("Excepción ejecutando %s: %s", " ".join(args), e)
        return False, "", str(e)
 
 
# ── Operación git principal ───────────────────────────────────────────────────
 
def _git_push(source_file: str, user_ip: str = None, deleted: bool = False):
    """
    Clona la rama correspondiente en un directorio temporal,
    aplica el cambio (add o rm) y hace push.
    El directorio temporal se elimina siempre al final.
    """
    if not config.GIT_ENABLED:
        return
 
    project = _resolve_project(source_file)
    branch  = project["branch"]
    subdir  = project["subdir"]  # sin slashes extremos
 
    logger.info("Git: %s | proyecto=%s | rama=%s | subdir=%s | deleted=%s",
                source_file, project["name"], branch, subdir or "/", deleted)
 
    git = _git_bin()
    if not git:
        logger.error("Git: binario no encontrado")
        return
 
    env = _git_env()
    tmp_dir = None
 
    try:
        # 1. Clonar la rama en un directorio temporal limpio
        tmp_dir = Path(tempfile.mkdtemp(prefix="ptm_git_"))
        logger.info("Git: clonando rama '%s' en %s", branch, tmp_dir)
 
        clone_ok, _, clone_err = _run(
            [git, "clone",
             "--branch", branch,
             "--single-branch",
             "--depth", "1",
             config.GIT_REMOTE_URL,
             str(tmp_dir)],
            cwd=tmp_dir.parent, env=env, timeout=120
        )
 
        if not clone_ok:
            # Si la rama no existe remotamente, clonar rama por defecto y crear la rama
            logger.warning("Git: rama '%s' no existe remotamente, clonando rama por defecto", branch)
            clone_ok, _, clone_err = _run(
                [git, "clone",
                 "--single-branch",
                 "--depth", "1",
                 config.GIT_REMOTE_URL,
                 str(tmp_dir)],
                cwd=tmp_dir.parent, env=env, timeout=120
            )
            if not clone_ok:
                logger.error("Git: fallo al clonar repositorio: %s", clone_err.strip())
                return
            # Crear la rama nueva
            _run([git, "checkout", "-b", branch], cwd=tmp_dir, env=env)
 
        # 2. Calcular ruta relativa del fichero dentro del clone
        rel_path = Path(subdir) / source_file if subdir else Path(source_file)
        dest_path = tmp_dir / rel_path
 
        if deleted:
            # Buscar el fichero en el índice (puede estar en ruta distinta al subdir)
            ok, out, _ = _run(
                [git, "ls-files", "--", source_file, f"*/{source_file}"],
                cwd=tmp_dir, env=env
            )
            if ok and out.strip():
                tracked_rel = Path(out.strip().splitlines()[0].strip())
                logger.info("Git: fichero encontrado en índice como '%s'", tracked_rel)
            else:
                tracked_rel = rel_path
                logger.warning("Git: '%s' no en índice, usando ruta configurada '%s'",
                               source_file, tracked_rel)
 
            tracked_full = tmp_dir / tracked_rel
            if tracked_full.exists():
                tracked_full.unlink()
 
            _run([git, "rm", "--ignore-unmatch", "-f", str(tracked_rel)],
                 cwd=tmp_dir, env=env)
 
        else:
            # Copiar el JSON desde TARGETS_DIR al clone
            src_path = _json_path(source_file)
            if not src_path.exists():
                logger.error("Git: fichero fuente no existe: %s", src_path)
                return
 
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_path), str(dest_path))
            logger.info("Git: copiado %s → %s", src_path, dest_path)
 
            ok, _, err = _run([git, "add", str(rel_path)], cwd=tmp_dir, env=env)
            if not ok:
                logger.error("Git: error en git add: %s", err.strip())
                return
 
        # 3. Comprobar si hay cambios reales que commitear
        _, status_out, _ = _run([git, "status", "--porcelain"], cwd=tmp_dir, env=env)
        if not status_out.strip():
            logger.info("Git: sin cambios que commitear para '%s'", source_file)
            return
 
        # 4. Commit
        action = "Remove" if deleted else "Update"
        msg = f"[prometheus-target-manager] {action} {source_file} ({project['name']})"
        if user_ip:
            msg += f" (from {user_ip})"
 
        commit_ok, _, commit_err = _run(
            [git, "commit", "-m", msg], cwd=tmp_dir, env=env
        )
        if not commit_ok:
            logger.error("Git: error en commit: %s", commit_err.strip())
            return
 
        # 5. Push
        push_ok, _, push_err = _run(
            [git, "push", "origin", branch], cwd=tmp_dir, env=env
        )
        if not push_ok:
            logger.error("Git: error en push a rama '%s': %s", branch, push_err.strip())
            return
 
        logger.info("Git push OK: %s → %s (rama: %s)", source_file, project["name"], branch)
 
    except Exception as e:
        logger.exception("Git: excepción inesperada en _git_push: %s", e)
 
    finally:
        # Limpiar siempre el directorio temporal
        if tmp_dir and tmp_dir.exists():
            try:
                shutil.rmtree(str(tmp_dir))
                logger.debug("Git: directorio temporal eliminado: %s", tmp_dir)
            except Exception as e:
                logger.warning("Git: no se pudo eliminar directorio temporal %s: %s", tmp_dir, e)
 
 
# ── API pública ───────────────────────────────────────────────────────────────
 
def read_json(source_file: str) -> list:
    path = _json_path(source_file)
    if not path.exists():
        raise FileNotFoundError(f"No existe: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)
 
 
def regenerate(source_file: str, user_ip: str = None, username: str = None) -> Path:
    """
    Reconstruye el JSON desde MariaDB.
    Si no quedan grupos → elimina del disco y hace push con deleted=True.
    """
    groups = db.get_groups_by_file(source_file)
 
    entries = []
    for g in groups:
        if not g:
            continue
        enabled_targets = [t["address"] for t in g.get("targets", []) if t["enabled"]]
        if not enabled_targets:
            continue
        labels = {
            "jobname":     g["jobname"],
            "PROJECT":     g["project"],
            "ENVIRONMENT": g["environment"],
            "DATACENTER":  g["datacenter"],
            "OS":          g["os"],
            "EXPORTER":    g["exporter"],
            "ALERTING":    "true" if g["alerting"] else "false",
        }
        if g.get("instance"):   labels["instance"] = g["instance"]
        if g.get("module"):     labels["module"]   = g["module"]
        if g.get("port_label"): labels["PORT"]     = g["port_label"]
        extra = g.get("extra_labels")
        if extra and isinstance(extra, dict):
            for k, v in extra.items():
                if k and v:
                    labels[k.upper()] = str(v)
        entries.append({"targets": enabled_targets, "labels": labels})
 
    # Escritura atómica en TARGETS_DIR (para Prometheus)
    target_path = _json_path(source_file)
    target_path.parent.mkdir(parents=True, exist_ok=True)
 
    fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, prefix=f".{source_file}.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
 
    logger.info("Regenerado %s (%d grupos, %d targets)",
                source_file, len(entries),
                sum(len(e["targets"]) for e in entries))
 
    db.log_sync_export(source_file, user_ip, username)
 
    # Si vacío → eliminar del disco antes del push
    if not entries:
        try:
            target_path.unlink(missing_ok=True)
            logger.info("Fichero %s eliminado del disco (sin targets)", source_file)
        except Exception as e:
            logger.error("Error eliminando %s del disco: %s", source_file, e)
 
    # Push git (no bloquea si falla)
    _git_push(source_file, user_ip, deleted=not entries)
 
    return target_path
 
 
def list_json_files() -> list:
    d = Path(config.TARGETS_DIR)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.json") if not p.name.startswith("."))
 
 
def validate_json(source_file: str) -> dict:
    try:
        data = read_json(source_file)
    except FileNotFoundError:
        return {"valid": False, "error": "Fichero no encontrado"}
    except json.JSONDecodeError as e:
        return {"valid": False, "error": f"JSON inválido: {e}"}
    errors = []
    for i, entry in enumerate(data):
        if "targets" not in entry:
            errors.append(f"Entry {i}: falta 'targets'")
        elif not isinstance(entry["targets"], list):
            errors.append(f"Entry {i}: 'targets' no es lista")
        if "labels" not in entry:
            errors.append(f"Entry {i}: falta 'labels'")
    if errors:
        return {"valid": False, "errors": errors}
    return {
        "valid": True,
        "groups": len(data),
        "targets": sum(len(e.get("targets", [])) for e in data),
    }
