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
