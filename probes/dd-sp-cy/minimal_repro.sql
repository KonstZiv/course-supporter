-- DD-SP-CY minimal reproduction. Run in a UTF8 database:
--   psql -X -v ON_ERROR_STOP=0 -f minimal_repro.sql
-- Value: one ASCII byte + 2000 Cyrillic 'а' (U+0430 = 0xd0 0xb0), 4001 bytes.
-- EXTERNAL keeps it out of line and uncompressed, so left() reads a slice.
SELECT version();
DROP TABLE IF EXISTS dd_sp_cy;
CREATE TABLE dd_sp_cy (c text);
ALTER TABLE dd_sp_cy ALTER COLUMN c SET STORAGE EXTERNAL;
INSERT INTO dd_sp_cy VALUES ('x' || repeat('а', 2000));
SELECT pg_column_toast_chunk_id(c) IS NOT NULL AS out_of_line,
       pg_column_compression(c) AS compression,
       octet_length(c) AS octets
FROM dd_sp_cy;
SELECT left(c, 1) FROM dd_sp_cy;                         -- 17.8: ERROR 0xd0
SELECT substring(c from 1 for 1) FROM dd_sp_cy;          -- 17.8: ERROR 0xd0
SELECT left(c || '', 1) FROM dd_sp_cy;                   -- workaround
SELECT right(c, 1) FROM dd_sp_cy;                        -- unaffected
DROP TABLE dd_sp_cy;
