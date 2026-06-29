"""
config.py
---------
Configuración centralizada del Prometheus Target Manager.
Editar este fichero para adaptar la instalación a cada entorno.
"""

import os

# ── Base de datos ──────────────────────────────────────────────────────────────
DB_HOST     = os.getenv("DB_HOST",     "localhost")
DB_PORT     = int(os.getenv("DB_PORT", "3306"))
DB_USER     = os.getenv("DB_USER",     "prometheus_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_NAME     = os.getenv("DB_NAME",     "prometheus_targets")

# ── Ficheros JSON de Prometheus ───────────────────────────────────────────────
# Directorio del repositorio git donde están los ficheros JSON de targets.
# Este directorio debe ser el raíz del repo o contener el subdirectorio targets.
TARGETS_DIR = os.getenv("TARGETS_DIR", "/prometheus/repository/prometheus-server/targets")

# ── Git ────────────────────────────────────────────────────────────────────────
GIT_ENABLED       = os.getenv("GIT_ENABLED", "true").lower() == "true"
GIT_REPO_DIR      = os.getenv("GIT_REPO_DIR", "/home/SOLIUMES/david.ortiz/targettesting/targettesting")
GIT_REMOTE_URL    = os.getenv("GIT_REMOTE_URL", "https://oauth2:glpat-B267xA8GVqUs1JSk2K3ZJG86MQp1OngH.01.0w0aj58b6@ahgitlabp01.solium.es/monitoring1/targettesting.git")
GIT_USER_NAME     = os.getenv("GIT_USER_NAME",  "david.ortiz")
GIT_USER_EMAIL    = os.getenv("GIT_USER_EMAIL", "david.ortiz@accenture.com")
GIT_SSL_NO_VERIFY = os.getenv("GIT_SSL_NO_VERIFY", "false").lower() == "true"
GIT_BRANCH        = os.getenv("GIT_BRANCH", "main")  # rama por defecto (fallback)

# ── Proyectos Git (multi-proyecto) ─────────────────────────────────────────────
# Mapeo de ficheros JSON a proyectos Git.
# Cada proyecto puede usar una rama y/o subdirectorio distintos.
#
# "branch": rama git sobre la que hacer push
# "subdir": subdirectorio dentro del repo donde viven los JSON (relativo a GIT_REPO_DIR)
#           dejar "" si los JSON están en la raíz del repo o en TARGETS_DIR directamente
# "files":  lista de ficheros JSON que pertenecen a este proyecto
#
# Si un fichero no aparece en ningún proyecto se usa GIT_BRANCH como fallback.

GIT_PROJECTS = {
    "CLOUD": {
        "branch": "Cloud",
        "subdir": "Targets/",
        "files": [
            "golf.json",
        ],
    },
    "ONPREM": {
        "branch": "Onprem",
        "subdir": "Targets/",
        "files": [
            "cbgi_esx.json",

            "bbva_rico.json",
        ],
    },
}

# ── API ────────────────────────────────────────────────────────────────────────
API_HOST    = os.getenv("API_HOST", "0.0.0.0")
API_PORT    = int(os.getenv("API_PORT", "5000"))
DEBUG       = os.getenv("DEBUG", "false").lower() == "true"

# Clave secreta para Flask (cambiar en producción)
SECRET_KEY  = os.getenv("SECRET_KEY", "cambia-esta-clave-secreta")
