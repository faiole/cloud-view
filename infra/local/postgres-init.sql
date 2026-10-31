-- Usuários separados: dono do schema (migrações) e aplicação (DML, sujeita a RLS)
CREATE ROLE cloudview_owner LOGIN PASSWORD 'cloudview_owner';
CREATE ROLE cloudview_app LOGIN PASSWORD 'cloudview_app';
CREATE DATABASE cloudview OWNER cloudview_owner;
CREATE ROLE keycloak LOGIN PASSWORD 'keycloak';
CREATE DATABASE keycloak OWNER keycloak;
\connect cloudview
GRANT USAGE ON SCHEMA public TO cloudview_app;
