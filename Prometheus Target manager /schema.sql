-- ============================================================
--  PROMETHEUS TARGET MANAGER — Esquema MariaDB
--  Basado en el análisis del fichero targets.json real
--  Versión: 1.0
-- ============================================================

-- Crear la base de datos
CREATE DATABASE IF NOT EXISTS prometheus_targets
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE prometheus_targets;

-- ============================================================
--  TABLAS DE CATÁLOGO (valores controlados / maestros)
--  Estas tablas alimentan los desplegables del frontend
--  y garantizan integridad referencial en la tabla targets.
-- ============================================================

-- ------------------------------------------------------------
--  projects
--  Ejemplo: "CBGI Monitoring"
--  Actualmente un solo proyecto, pero el diseño lo soporta.
-- ------------------------------------------------------------
CREATE TABLE projects (
    id          SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    description TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_projects_name (name)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
--  environments
--  Valores actuales: DES | PRE | CONT | PRO
-- ------------------------------------------------------------
CREATE TABLE environments (
    id          SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(50) NOT NULL,
    description VARCHAR(200),
    sort_order  TINYINT UNSIGNED DEFAULT 0,  -- para ordenar DES→PRE→CONT→PRO
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_env_name (name)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
--  datacenters
--  Valores actuales: AH | YC
-- ------------------------------------------------------------
CREATE TABLE datacenters (
    id          SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    code        VARCHAR(10) NOT NULL,        -- AH, YC
    name        VARCHAR(100),               -- nombre largo opcional
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_dc_code (code)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
--  os_types  (sistema operativo)
--  Valores actuales: Linux | Windows
-- ------------------------------------------------------------
CREATE TABLE os_types (
    id          SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(50) NOT NULL,        -- Linux, Windows
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_os_name (name)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
--  exporters
--  Valores actuales: Node Exporter, WMI Exporter, Blackbox Exporter,
--                    Thanos, Thanos-remote-read, SNMP Exporter,
--                    SQL Exporter, Mtail Exporter
-- ------------------------------------------------------------
CREATE TABLE exporters (
    id            SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    default_port  SMALLINT UNSIGNED,         -- puerto habitual (referencia)
    description   TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_exp_name (name)
) ENGINE=InnoDB;

-- ============================================================
--  TABLA PRINCIPAL: target_groups
--  Representa cada bloque { "targets": [...], "labels": {...} }
--  del fichero JSON de Prometheus.
--
--  Un target_group tiene:
--    - Un conjunto de labels comunes (FK a catálogos)
--    - Labels opcionales: instance, module, PORT, ALERTING
--    - Una relación 1:N con la tabla targets (los hosts)
--    - Referencia al fichero JSON de origen (para saber a qué
--      fichero pertenece y regenerarlo correctamente)
-- ============================================================
CREATE TABLE target_groups (
    id             INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

    -- Fichero JSON de origen (p.ej. "proyecto_1.json")
    source_file    VARCHAR(255) NOT NULL,

    -- Jobname: campo descriptivo, no FK (es derivado de exporter+env)
    jobname        VARCHAR(150) NOT NULL,

    -- Labels controlados mediante FK a catálogos
    project_id     SMALLINT UNSIGNED NOT NULL,
    environment_id SMALLINT UNSIGNED NOT NULL,
    datacenter_id  SMALLINT UNSIGNED NOT NULL,
    os_id          SMALLINT UNSIGNED NOT NULL,
    exporter_id    SMALLINT UNSIGNED NOT NULL,

    -- Labels opcionales (presentes solo en ~50% de los entries)
    instance       VARCHAR(255),    -- ej: "ahsnmpp01:9115"
    module         VARCHAR(100),    -- ej: "tcp_test", "icmp_test", "https_2xx_auth"
    port_label     VARCHAR(10),     -- ej: HTTP, HTTPS, TCP, ICMP  (PORT en el JSON)

    -- ALERTING casi siempre es "true", pero lo guardamos como flag
    alerting       TINYINT(1) NOT NULL DEFAULT 1,

    -- Auditoría
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Claves foráneas
    CONSTRAINT fk_tg_project     FOREIGN KEY (project_id)     REFERENCES projects(id),
    CONSTRAINT fk_tg_environment FOREIGN KEY (environment_id) REFERENCES environments(id),
    CONSTRAINT fk_tg_datacenter  FOREIGN KEY (datacenter_id)  REFERENCES datacenters(id),
    CONSTRAINT fk_tg_os          FOREIGN KEY (os_id)          REFERENCES os_types(id),
    CONSTRAINT fk_tg_exporter    FOREIGN KEY (exporter_id)    REFERENCES exporters(id),

    -- Índices para las búsquedas más frecuentes desde el frontend
    KEY idx_source_file    (source_file),
    KEY idx_project        (project_id),
    KEY idx_environment    (environment_id),
    KEY idx_exporter       (exporter_id)

) ENGINE=InnoDB;

-- ============================================================
--  TABLA: targets
--  Cada fila es un host:puerto dentro de un target_group.
--  Relación N:1 con target_groups.
--
--  Ejemplos de valores en el JSON:
--    "cursomon01:9182"
--    "http://ahpmtp01:9093/#/alerts"
--    "https://alertmanager.solium.es"
--    "ahpmtp01"   (sin puerto → ICMP / PING)
-- ============================================================
CREATE TABLE targets (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    target_group_id INT UNSIGNED NOT NULL,

    -- El valor exacto tal como aparece en el array "targets" del JSON
    -- Puede ser host:puerto, URL completa, o solo hostname
    address         VARCHAR(512) NOT NULL,

    -- Campos derivados (parseados del address para facilitar búsquedas)
    hostname        VARCHAR(255),           -- solo el host, sin puerto ni protocolo
    port            SMALLINT UNSIGNED,      -- null si es ICMP (sin puerto)
    protocol        VARCHAR(10),            -- http, https, o null si es host:puerto

    -- Estado (útil para deshabilitar un target sin borrarlo)
    enabled         TINYINT(1) NOT NULL DEFAULT 1,

    -- Auditoría
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_target_group FOREIGN KEY (target_group_id)
        REFERENCES target_groups(id) ON DELETE CASCADE,

    KEY idx_tg_id    (target_group_id),
    KEY idx_hostname (hostname),
    KEY idx_enabled  (enabled)

) ENGINE=InnoDB;

-- ============================================================
--  TABLA: sync_log
--  Registro de cada vez que se escribe un fichero JSON.
--  Sirve para auditoría y para saber qué cambió y cuándo.
-- ============================================================
CREATE TABLE sync_log (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_file VARCHAR(255) NOT NULL,
    action      ENUM('CREATE','UPDATE','DELETE','IMPORT','EXPORT') NOT NULL,
    detail      TEXT,                       -- descripción del cambio
    user_ip     VARCHAR(45),                -- IP de quien hizo el cambio
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_sync_file (source_file),
    KEY idx_sync_date (created_at)
) ENGINE=InnoDB;

-- ============================================================
--  DATOS INICIALES (catálogos)
--  Poblados con los valores reales del targets.json analizado
-- ============================================================

-- Proyecto
INSERT INTO projects (name, description) VALUES
  ('CBGI Monitoring', 'Proyecto principal de monitorización CBGI');

-- Entornos (ordenados de menor a mayor criticidad)
INSERT INTO environments (name, description, sort_order) VALUES
  ('DES',  'Desarrollo',    1),
  ('PRE',  'Preproducción', 2),
  ('CONT', 'Contingencia',  3),
  ('PRO',  'Producción',    4);

-- Datacenters
INSERT INTO datacenters (code, name) VALUES
  ('AH', 'Datacenter AH'),
  ('YC', 'Datacenter YC');

-- Sistemas operativos
INSERT INTO os_types (name) VALUES
  ('Linux'),
  ('Windows');

-- Exporters (con puertos de referencia)
INSERT INTO exporters (name, default_port, description) VALUES
  ('Node Exporter',       9100, 'Métricas de sistema Linux'),
  ('WMI Exporter',        9182, 'Métricas de sistema Windows'),
  ('Blackbox Exporter',   9115, 'Sondas externas HTTP/TCP/ICMP'),
  ('Thanos',             10900, 'Componentes Thanos (sidecar, query, store...)'),
  ('Thanos-remote-read', 10081, 'Thanos remote read'),
  ('SNMP Exporter',       9116, 'Métricas vía SNMP'),
  ('SQL Exporter',        9399, 'Métricas de bases de datos SQL'),
  ('Mtail Exporter',      3903, 'Parseo de logs con mtail');


-- ============================================================
--  VISTAS ÚTILES PARA EL FRONTEND Y LA API
-- ============================================================

-- Vista completa desnormalizada de target_groups
-- Evita JOINs repetitivos en la API
CREATE OR REPLACE VIEW v_target_groups AS
SELECT
    tg.id,
    tg.source_file,
    tg.jobname,
    p.name                      AS project,
    e.name                      AS environment,
    e.sort_order                AS env_order,
    d.code                      AS datacenter,
    o.name                      AS os,
    ex.name                     AS exporter,
    tg.instance,
    tg.module,
    tg.port_label,
    tg.alerting,
    tg.created_at,
    tg.updated_at,
    COUNT(t.id)                 AS target_count
FROM target_groups tg
JOIN projects     p  ON p.id  = tg.project_id
JOIN environments e  ON e.id  = tg.environment_id
JOIN datacenters  d  ON d.id  = tg.datacenter_id
JOIN os_types     o  ON o.id  = tg.os_id
JOIN exporters    ex ON ex.id = tg.exporter_id
LEFT JOIN targets t  ON t.target_group_id = tg.id AND t.enabled = 1
GROUP BY tg.id;

-- Vista de todos los targets individuales con su contexto completo
CREATE OR REPLACE VIEW v_targets_full AS
SELECT
    t.id                        AS target_id,
    t.address,
    t.hostname,
    t.port,
    t.protocol,
    t.enabled,
    tg.id                       AS group_id,
    tg.source_file,
    tg.jobname,
    p.name                      AS project,
    e.name                      AS environment,
    d.code                      AS datacenter,
    o.name                      AS os,
    ex.name                     AS exporter,
    tg.instance,
    tg.module,
    tg.port_label,
    tg.alerting
FROM targets t
JOIN target_groups tg ON tg.id = t.target_group_id
JOIN projects      p  ON p.id  = tg.project_id
JOIN environments  e  ON e.id  = tg.environment_id
JOIN datacenters   d  ON d.id  = tg.datacenter_id
JOIN os_types      o  ON o.id  = tg.os_id
JOIN exporters     ex ON ex.id = tg.exporter_id;
