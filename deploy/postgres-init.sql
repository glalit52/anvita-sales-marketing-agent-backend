-- The application connects as a regular role: superusers bypass row-level security.
CREATE ROLE anvita LOGIN PASSWORD 'anvita';
CREATE DATABASE anvita OWNER anvita;
CREATE DATABASE anvita_test OWNER anvita;
\connect anvita
ALTER SCHEMA public OWNER TO anvita;
\connect anvita_test
ALTER SCHEMA public OWNER TO anvita;
