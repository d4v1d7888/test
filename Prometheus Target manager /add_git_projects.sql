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
