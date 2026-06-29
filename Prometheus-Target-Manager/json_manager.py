"""
json_manager.py
---------------
Gestión de los ficheros JSON de file_sd_config de Prometheus.

Responsabilidades:
  - Leer el estado actual de un fichero JSON
  - Regenerar un fichero JSON completo a partir de los datos de MariaDB
  - Escritura atómica (escribe en .tmp y luego rename) para evitar que
    Prometheus lea un fichero a medias
  - Push automático a git tras cada modificación (si GIT_ENABLED=true)

Prometheus detecta los cambios en los ficheros JSON automáticamente
si file_sd_config tiene configurado refresh_interval (por defecto 5m).
"""

import json
import os
import subprocess
import tempfile
import logging
from pathlib import Path

import config
import db

logger = logging.getLogger(__name__)


def _json_path(source_file: str) -> Path:
    """Devuelve la ruta absoluta del fichero JSON."""
    return Path(config.TARGETS_DIR) / source_file


def _resolve_project(source_file: str) -> dict:
    """
    Devuelve la configuración del proyecto git al que pertenece source_file.
    Consulta primero la BD (mapeo dinámico), luego config.py como fallback.
    Si no está en ningún proyecto retorna los valores por defecto.
    """
    # Obtener mapeo dinámico (BD + config.py)
    try:
        file_projects = db.get_file_projects()
    except Exception:
        file_projects = {}

    project_name = file_projects.get(source_file)

    if project_name:
        # Buscar la configuración de rama/subdir en config.py
        proj_cfg = getattr(config, "GIT_PROJECTS", {}).get(project_name, {})
        return {
            "name":   project_name,
            "branch": proj_cfg.get("branch", config.GIT_BRANCH),
            "subdir": proj_cfg.get("subdir", ""),
        }

    return {"name": "default", "branch": config.GIT_BRANCH, "subdir": ""}


def _git_bin() -> str | None:
    """Detecta la ruta absoluta del binario git."""
    import shutil
    git_bin = shutil.which("git")
    if git_bin and Path(git_bin).exists():
        return git_bin
    for candidate in ["/usr/bin/git", "/usr/local/bin/git", "/bin/git"]:
        if Path(candidate).exists():
            return candidate
    return None


def _git_env() -> dict:
    """Construye el entorno para los subprocesos git."""
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


def _run_git(git_bin: str, args: list, cwd: Path, env: dict, timeout: int = 30):
    """
    Ejecuta un comando git. Devuelve (ok: bool, stdout, stderr).
    No lanza excepciones; cualquier fallo se traduce en ok=False.
    """
    cmd = [git_bin, "-C", str(cwd)] + args
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        ok = result.returncode == 0
        if not ok:
            logger.debug("Git: %s → rc=%d stderr=%s", " ".join(args), result.returncode, result.stderr.strip())
        return ok, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error("Git: timeout ejecutando %s", " ".join(args))
        return False, "", "timeout"
    except Exception as e:
        logger.error("Git: excepción ejecutando %s: %s", " ".join(args), e)
        return False, "", str(e)


def _worktrees_root() -> Path:
    repo = Path(config.GIT_REPO_DIR)
    return repo.parent / f"{repo.name}-worktrees"


def _ensure_worktree(git_bin: str, env: dict, branch: str) -> Path | None:
    """
    Garantiza que existe un checkout local en la rama `branch`,
    actualizado con el remoto. Devuelve la ruta a usar para esa rama,
    o None si falla.

    - Si `branch` coincide con la rama actual del repo principal
      (normalmente "main", donde vive TARGETS_DIR), se usa el repo
      principal directamente, SIN hacer fetch/reset destructivo
      (para no pisar el working tree que usa Prometheus).
    - En cualquier otro caso se usa/crea un worktree dedicado en
      <repo>-worktrees/<branch>, aislado del resto de ramas.
    """
    repo = Path(config.GIT_REPO_DIR)

    # Asegurar que el remote tiene la URL correcta (con token actualizado)
    _run_git(git_bin, ["remote", "set-url", "origin", config.GIT_REMOTE_URL], repo, env)
    _run_git(git_bin, ["fetch", "origin"], repo, env)

    # ¿La rama del proyecto es la misma que la del repo principal?
    ok, out, _ = _run_git(git_bin, ["rev-parse", "--abbrev-ref", "HEAD"], repo, env)
    current_branch = out.strip() if ok else None

    if current_branch == branch:
        logger.info("Git: rama '%s' es la del repo principal, usando %s directamente", branch, repo)
        return repo

    wt_path = _worktrees_root() / branch

    if (wt_path / ".git").exists():
        # Worktree ya existe: actualizarlo a la última versión de la rama remota
        _run_git(git_bin, ["fetch", "origin", branch], wt_path, env)
        ok, _, _ = _run_git(git_bin, ["reset", "--hard", f"origin/{branch}"], wt_path, env)
        if not ok:
            logger.debug("Git: rama remota %s aún no existe, se mantiene worktree local", branch)
        return wt_path

    # Worktree no existe todavía: crearlo
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    ok, out, _ = _run_git(git_bin, ["ls-remote", "--heads", "origin", branch], repo, env)
    branch_exists_remote = ok and out.strip() != ""

    if branch_exists_remote:
        ok, _, err = _run_git(
            git_bin, ["worktree", "add", "-B", branch, str(wt_path), f"origin/{branch}"], repo, env
        )
    else:
        ok, _, err = _run_git(
            git_bin, ["worktree", "add", "-B", branch, str(wt_path)], repo, env
        )

    if not ok:
        # Si la rama ya está activa en otro worktree (incluido el repo principal),
        # git lo indica con "already used by worktree at '<ruta>'"
        import re as _re
        m = _re.search(r"already used by worktree at '(.+)'", err)
        if m:
            existing = Path(m.group(1))
            if existing.exists():
                logger.info("Git: rama '%s' ya está activa en %s, reutilizando", branch, existing)
                if existing.resolve() != repo.resolve():
                    _run_git(git_bin, ["fetch", "origin", branch], existing, env)
                    _run_git(git_bin, ["reset", "--hard", f"origin/{branch}"], existing, env)
                return existing

        logger.error("Git: no se pudo crear worktree para rama %s: %s", branch, err.strip())
        return None

    logger.info("Git: worktree creado para rama '%s' en %s (remoto previo: %s)",
                 branch, wt_path, branch_exists_remote)
    return wt_path


def _git_push(source_file: str, user_ip: str = None, deleted: bool = False):
    """
    Publica el fichero (o su eliminación) en el proyecto/rama git que
    corresponda según GIT_PROJECTS. Usa un worktree dedicado por rama,
    de modo que los ficheros de un proyecto nunca aparecen en la rama
    de otro proyecto.

    Si falla, loguea el error pero NO lanza excepción para no
    interrumpir la operación principal (el JSON ya está escrito en TARGETS_DIR).
    """
    if not config.GIT_ENABLED:
        return

    project = _resolve_project(source_file)
    branch  = project["branch"]
    subdir  = project["subdir"].strip("/")

    logger.info("Git: proyecto=%s rama=%s fichero=%s", project["name"], branch, source_file)

    git_bin = _git_bin()
    if not git_bin:
        logger.error("Git: binario 'git' no encontrado en el sistema")
        return

    env = _git_env()

    wt_path = _ensure_worktree(git_bin, env, branch)
    if wt_path is None:
        return

    rel_path = Path(subdir) / source_file if subdir else Path(source_file)
    dest_path = wt_path / rel_path

    if deleted:
        # Eliminar el fichero del worktree (si existe) y de git
        if dest_path.exists():
            try:
                dest_path.unlink()
            except OSError as e:
                logger.error("Git: no se pudo borrar %s del worktree: %s", dest_path, e)
        stage_ok, _, stage_err = _run_git(
            git_bin, ["rm", "--ignore-unmatch", str(rel_path)], wt_path, env
        )
    else:
        # Copiar la versión regenerada al worktree (si no es ya el mismo fichero)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        src_path = _json_path(source_file)
        if src_path.resolve() != dest_path.resolve():
            import shutil as _shutil
            try:
                _shutil.copy2(src_path, dest_path)
            except Exception as e:
                logger.error("Git: no se pudo copiar %s al worktree: %s", source_file, e)
                return
        stage_ok, _, stage_err = _run_git(git_bin, ["add", str(rel_path)], wt_path, env)

    if not stage_ok:
        logger.error("Git: error en stage de %s: %s", source_file, stage_err.strip())
        return

    commit_msg = f"[prometheus-target-manager] {'Remove' if deleted else 'Update'} {source_file} ({project['name']})"
    if user_ip:
        commit_msg += f" (from {user_ip})"

    commit_ok, commit_out, commit_err = _run_git(git_bin, ["commit", "-m", commit_msg], wt_path, env)
    if not commit_ok:
        if "nothing to commit" in (commit_out + commit_err):
            logger.info("Git: sin cambios en %s (%s), push omitido", source_file, project["name"])
            return
        logger.error("Git: error en commit de %s: %s", source_file, commit_err.strip())
        return

    push_ok, _, push_err = _run_git(git_bin, ["push", "origin", branch], wt_path, env)
    if not push_ok:
        logger.error("Git: error en push de %s a rama %s: %s", source_file, branch, push_err.strip())
        return

    logger.info("Git push completado: %s → proyecto=%s rama=%s subdir=%s",
                 source_file, project["name"], branch, subdir or "/")


def read_json(source_file: str) -> list:
    """Lee y devuelve el contenido de un fichero JSON de targets."""
    path = _json_path(source_file)
    if not path.exists():
        raise FileNotFoundError(f"No existe el fichero: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def regenerate(source_file: str, user_ip: str = None) -> Path:
    """
    Reconstruye el fichero JSON de Prometheus a partir de MariaDB.
    Tras escribir el fichero, hace push a git si GIT_ENABLED=true.
    """
    groups = db.get_groups_by_file(source_file)

    entries = []
    for g in groups:
        if not g:
            continue

        # Solo los targets habilitados
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

        # Labels opcionales estándar
        if g.get("instance"):
            labels["instance"] = g["instance"]
        if g.get("module"):
            labels["module"] = g["module"]
        if g.get("port_label"):
            labels["PORT"] = g["port_label"]

        # Labels extra definidos por el usuario (importación Excel)
        extra = g.get("extra_labels")
        if extra and isinstance(extra, dict):
            for k, v in extra.items():
                if k and v:
                    labels[k.upper()] = str(v)

        entries.append({"targets": enabled_targets, "labels": labels})

    # Escritura atómica
    target_path = _json_path(source_file)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=target_path.parent,
        prefix=f".{source_file}.tmp",
    )
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

    # Registrar en el log de auditoría
    db.log_sync_export(source_file, user_ip)

    # Si el fichero quedó vacío, eliminarlo del disco
    if not entries:
        try:
            target_path.unlink(missing_ok=True)
            logger.info("Fichero %s eliminado (sin targets)", source_file)
        except Exception as e:
            logger.error("Error eliminando fichero vacío %s: %s", source_file, e)

    # Push a git (no bloquea si falla)
    _git_push(source_file, user_ip, deleted=not entries)

    return target_path


def list_json_files() -> list:
    """
    Devuelve los ficheros *.json que existen en TARGETS_DIR.
    Útil para detectar ficheros que aún no han sido importados.
    """
    d = Path(config.TARGETS_DIR)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.json") if not p.name.startswith("."))


def validate_json(source_file: str) -> dict:
    """
    Valida que el JSON en disco sea parseable y tenga la estructura
    esperada por Prometheus. Devuelve un dict con el resultado.
    """
    try:
        data = read_json(source_file)
    except FileNotFoundError:
        return {"valid": False, "error": "Fichero no encontrado"}
    except json.JSONDecodeError as e:
        return {"valid": False, "error": f"JSON inválido: {e}"}

    errors = []
    for i, entry in enumerate(data):
        if "targets" not in entry:
            errors.append(f"Entry {i}: falta el campo 'targets'")
        elif not isinstance(entry["targets"], list):
            errors.append(f"Entry {i}: 'targets' no es una lista")
        if "labels" not in entry:
            errors.append(f"Entry {i}: falta el campo 'labels'")

    if errors:
        return {"valid": False, "errors": errors}

    return {
        "valid": True,
        "groups": len(data),
        "targets": sum(len(e.get("targets", [])) for e in data),
    }
