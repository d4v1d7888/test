-- add_users.sql
-- Añade la tabla users para autenticación JWT.
--
-- Ejecutar con:
--   mysql -u prometheus_user -p prometheus_targets < add_users.sql
--
-- Después crear los usuarios iniciales con:
--   python3 create_users.py
 
USE prometheus_targets;
 
CREATE TABLE IF NOT EXISTS users (
    id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(255) NOT NULL,   -- bcrypt hash
    role          ENUM('admin', 'readonly') NOT NULL DEFAULT 'readonly',
    enabled       TINYINT(1) NOT NULL DEFAULT 1,
    last_login    DATETIME,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_username (username)
) ENGINE=InnoDB;
