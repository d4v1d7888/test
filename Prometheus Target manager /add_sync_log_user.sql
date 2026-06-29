-- add_sync_log_user.sql
-- Añade la columna username a sync_log para registrar
-- el usuario autenticado que realizó cada cambio.
--
-- Ejecutar con:
--   mysql -u prometheus_user -p prometheus_targets < add_sync_log_user.sql
 
USE prometheus_targets;
 
ALTER TABLE sync_log
  ADD COLUMN username VARCHAR(64) DEFAULT NULL
  AFTER user_ip;
