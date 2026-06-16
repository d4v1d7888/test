-- add_git_projects.sql
-- Añade la tabla git_projects para almacenar el mapeo
-- fichero JSON → proyecto Git de forma dinámica.
--
-- Ejecutar con:
--   mysql -u prometheus_user -p prometheus_targets < add_git_projects.sql

USE prometheus_targets;

CREATE TABLE IF NOT EXISTS git_file_projects (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_file VARCHAR(255) NOT NULL,
    project     VARCHAR(100) NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_source_file (source_file)
) ENGINE=InnoDB;
[david.ortiz@ahpmttest01 prometheus-target-manager]$ cat fix_protocols.sql
-- fix_protocols.sql
-- Corrige el campo 'protocol' en la tabla targets re-parseándolo
-- desde el campo 'address' que siempre contiene el valor correcto.
--
-- Ejecutar con:
--   mysql -u prometheus_user -p prometheus_targets < fix_protocols.sql

USE prometheus_targets;

-- Corregir targets con URL http:// que tienen protocol incorrecto
UPDATE targets
SET protocol = 'http'
WHERE address LIKE 'http://%'
  AND (protocol IS NULL OR protocol != 'http');

-- Corregir targets con URL https:// que tienen protocol incorrecto
UPDATE targets
SET protocol = 'https'
WHERE address LIKE 'https://%'
  AND (protocol IS NULL OR protocol != 'https');

-- Limpiar protocol en targets que NO son URLs (host:port o solo hostname)
UPDATE targets
SET protocol = NULL
WHERE address NOT LIKE 'http://%'
  AND address NOT LIKE 'https://%'
  AND protocol IS NOT NULL;

-- Verificación: mostrar todos los targets con su protocol tras la corrección
SELECT
    t.id,
    t.address,
    t.hostname,
    t.port,
    t.protocol
FROM targets t
ORDER BY t.protocol, t.address;
