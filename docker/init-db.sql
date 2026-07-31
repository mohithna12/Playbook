-- Extensions needed by the application
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Separate schema and role for Airflow metadata (shares RDS instance, isolated otherwise)
CREATE ROLE airflow_user WITH LOGIN PASSWORD 'airflow';
CREATE SCHEMA IF NOT EXISTS airflow AUTHORIZATION airflow_user;
