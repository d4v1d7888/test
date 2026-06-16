-- add_extra_labels.sql
-- Añade la columna extra_labels a target_groups para almacenar
-- labels adicionales definidos por el usuario en la importación Excel.
--
-- Ejecutar con:
--   mysql -u prometheus_user -p prometheus_targets < add_extra_labels.sql

USE prometheus_targets;

ALTER TABLE target_groups
  ADD COLUMN extra_labels JSON DEFAULT NULL
  COMMENT 'Labels adicionales en formato JSON {"CLAVE": "valor"}'
  AFTER alerting;

