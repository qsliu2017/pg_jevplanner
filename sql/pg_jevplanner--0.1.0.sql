\echo Use "CREATE EXTENSION pg_jevplanner" to load this file. \quit

-- SQL-callable marker forces loading the module and installs its planner hook
-- in this backend. Other backends must LOAD it or use session_preload_libraries.
CREATE FUNCTION jevplanner_version() RETURNS text
AS 'MODULE_PATHNAME', 'jevplanner_version'
LANGUAGE C STRICT;

SELECT jevplanner_version();
