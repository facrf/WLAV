-- Extensões usadas pelo WLAV. O schema (tabelas, colunas, índices) NÃO é
-- definido aqui: a migração do Alembic é a única fonte de verdade, executada
-- pelo contêiner `app` em `alembic upgrade head`.
--
-- Este arquivo roda apenas na criação inicial do cluster, porque o Postgres o
-- executa uma única vez (docker-entrypoint-initdb.d). Manter o DDL duplicado
-- aqui fazia o schema divergir silenciosamente da migração.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
