#!/bin/sh
# Primeira subida do Postgres: usuários separados para migrações (dono do schema),
# aplicação (DML, sujeita a RLS) e Keycloak. Senhas vêm do .env.
set -eu
psql -v ON_ERROR_STOP=1 -U postgres <<SQL
CREATE ROLE cloudview_owner LOGIN PASSWORD '${OWNER_DB_PASSWORD}';
CREATE ROLE cloudview_app LOGIN PASSWORD '${APP_DB_PASSWORD}';
CREATE DATABASE cloudview OWNER cloudview_owner;
CREATE ROLE keycloak LOGIN PASSWORD '${KEYCLOAK_DB_PASSWORD}';
CREATE DATABASE keycloak OWNER keycloak;
\connect cloudview
GRANT USAGE ON SCHEMA public TO cloudview_app;
SQL
