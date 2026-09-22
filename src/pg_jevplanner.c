/* Join-order-only planner integration. PostgreSQL owns physical planning. */
#include "postgres.h"

#include <limits.h>

#include "fmgr.h"
#include "catalog/pg_class.h"
#include "catalog/pg_inherits.h"
#include "miscadmin.h"
#include "mb/pg_wchar.h"
#include "nodes/nodeFuncs.h"
#include "optimizer/cost.h"
#include "optimizer/geqo.h"
#include "optimizer/paths.h"
#include "optimizer/plancat.h"
#include "optimizer/planmain.h"
#include "optimizer/planner.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"

#include "jev_planner.h"

#if PG_VERSION_NUM < 180000 || PG_VERSION_NUM >= 190000
#error "pg_jevplanner requires PostgreSQL 18 server headers."
#endif

/* PostgreSQL checks the server's major-version ABI when loading the module;
 * no extra minor-version check is needed for this native-hook extension. */
PG_MODULE_MAGIC;

bool jev_enabled = false;
bool jev_debug = false;
bool jev_log_http = false;
char *jev_endpoint = NULL;
char *jev_model = NULL;
int jev_timeout_ms = 2000;
int jev_max_decisions = 1000;
int jev_max_candidates = 255;
int jev_planning_timeout_ms = 30000;

static planner_hook_type previous_planner_hook = NULL;
static join_search_hook_type previous_join_search_hook = NULL;
static bool planning = false;

PGDLLEXPORT void _PG_init(void);
PGDLLEXPORT void _PG_fini(void);
Datum jevplanner_version(PG_FUNCTION_ARGS);

PG_FUNCTION_INFO_V1(jevplanner_version);

Datum
jevplanner_version(PG_FUNCTION_ARGS)
{
    PG_RETURN_TEXT_P(cstring_to_text("pg_jevplanner 0.1.0 / PostgreSQL " PG_MAJORVERSION " / join-order-only"));
}

static bool
check_query(Node *node, void *context)
{
    if (node == NULL)
        return false;
    check_stack_depth();
    if (IsA(node, Query))
    {
        Query *query = (Query *) node;
        ListCell *lc;

        if (query->commandType != CMD_SELECT || query->hasModifyingCTE)
            jev_unsupported("data-modifying queries or CTEs");
        if (query->hasRecursive)
            jev_unsupported("recursive CTEs");
        if (query->hasRowSecurity)
            jev_unsupported("row-security policies in model query context");
        foreach(lc, query->rtable)
        {
            RangeTblEntry *rte = lfirst_node(RangeTblEntry, lc);

            if (rte->lateral)
                jev_unsupported("LATERAL references");
            if (rte->security_barrier || rte->securityQuals != NIL)
                jev_unsupported("security-barrier query context");
            if (rte->rtekind == RTE_RELATION)
            {
                char kind = get_rel_relkind(rte->relid);

                if (kind == RELKIND_FOREIGN_TABLE)
                    jev_unsupported("foreign tables");
                if (kind == RELKIND_PARTITIONED_TABLE ||
                    (rte->inh && has_subclass(rte->relid)))
                    jev_unsupported("partitioned or inherited relation scans");
            }
        }
        return query_tree_walker(query, check_query, context, 0);
    }
    if (IsA(node, JoinExpr) && ((JoinExpr *) node)->jointype != JOIN_INNER)
        jev_unsupported("non-inner joins (outer, semi and anti joins)");
    if (IsA(node, SubLink))
        jev_unsupported("subquery expressions (including IN/EXISTS subqueries)");
    return expression_tree_walker(node, check_query, context);
}

static RelOptInfo *
join_search(PlannerInfo *root, int levels_needed, List *initial_rels)
{
    if (planning)
        return jev_join_search(root, levels_needed, initial_rels);
    if (jev_enabled)
        jev_unsupported("join search outside the JEV planning lifecycle");
    /* Installing this hook must not change disabled-mode native/GEQO policy. */
    if (previous_join_search_hook)
        return previous_join_search_hook(root, levels_needed, initial_rels);
    if (enable_geqo && levels_needed >= geqo_threshold)
        return geqo(root, levels_needed, initial_rels);
    return standard_join_search(root, levels_needed, initial_rels);
}

static PlannedStmt *
jev_planner(Query *parse, const char *query_string,
            int cursorOptions, ParamListInfo boundParams)
{
    PlannedStmt *result = NULL;
    int saved_from_collapse = from_collapse_limit;
    int saved_join_collapse = join_collapse_limit;

    if (planning)
        jev_unsupported("reentrant planning from a function or extension");
    if (!jev_enabled)
        return previous_planner_hook
            ? previous_planner_hook(parse, query_string, cursorOptions, boundParams)
            : standard_planner(parse, query_string, cursorOptions, boundParams);

    if (previous_planner_hook || previous_join_search_hook ||
        join_search_hook != join_search || set_rel_pathlist_hook ||
        set_join_pathlist_hook || create_upper_paths_hook || get_relation_info_hook)
        jev_unsupported("other planner/path-generation hooks");
    if (max_parallel_workers_per_gather != 0)
        jev_unsupported("parallel planning; SET max_parallel_workers_per_gather = 0");
    if (GetDatabaseEncoding() != PG_UTF8)
        jev_unsupported("non-UTF8 databases for query-context serialization");
    check_query((Node *) parse, NULL);
    planning = true;
    PG_TRY();
    {
        jev_begin_planning(parse);
        /* Do not let collapse-limit heuristics preselect inner-join groups.
         * SQL-semantic barriers (e.g. a nonflattenable subquery) still apply.
         * Restore both globals on success and all error paths. */
        from_collapse_limit = INT_MAX;
        join_collapse_limit = INT_MAX;
        result = standard_planner(parse, query_string, cursorOptions, boundParams);
        jev_check_planning_budget();
        if (jev_debug)
            ereport(NOTICE,
                    (errmsg("jev join-order planning complete: %d model decisions",
                            jev_decision_count())));
    }
    PG_FINALLY();
    {
        from_collapse_limit = saved_from_collapse;
        join_collapse_limit = saved_join_collapse;
        jev_end_planning();
        planning = false;
    }
    PG_END_TRY();
    return result;
}

void
_PG_init(void)
{
    DefineCustomBoolVariable("jev.enabled", "Use JEV to select inner-join order.",
                             "PostgreSQL retains physical planning; model failures never fall back to native join search.",
                             &jev_enabled, false, PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomBoolVariable("jev.debug", "Log join merges and model decision IDs.",
                             NULL, &jev_debug, false, PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomBoolVariable("jev.log_http", "Show JEV request and response bodies as NOTICE messages.",
                             "Exposes full query SQL, names, literals, estimates and provider output; not authentication headers.",
                             &jev_log_http, false, PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomStringVariable("jev.endpoint", "Administrator-controlled JEV endpoint.",
                               "HTTPS required, except loopback HTTP for tests.",
                               &jev_endpoint, "https://api.typesafe.ai/v1/systemone",
                               PGC_SUSET, GUC_SUPERUSER_ONLY, NULL, NULL, NULL);
    DefineCustomStringVariable("jev.model", "Pinned TypeSafe model ID.",
                               NULL, &jev_model, "jev-1.13.0", PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomIntVariable("jev.timeout_ms", "Timeout for each JEV request.",
                            NULL, &jev_timeout_ms, 2000, 1, 60000,
                            PGC_SUSET, GUC_UNIT_MS, NULL, NULL, NULL);
    DefineCustomIntVariable("jev.planning_timeout_ms", "Budget for the whole planning invocation.",
                            NULL, &jev_planning_timeout_ms, 30000, 1, 600000,
                            PGC_SUSET, GUC_UNIT_MS, NULL, NULL, NULL);
    DefineCustomIntVariable("jev.max_decisions", "Maximum JEV requests per planning invocation.",
                            NULL, &jev_max_decisions, 1000, 1, 100000,
                            PGC_SUSET, 0, NULL, NULL, NULL);
    DefineCustomIntVariable("jev.max_candidates", "Maximum legal join pairs offered at one merge step.",
                            "Overflow errors; no cost-based shortlisting of join pairs.",
                            &jev_max_candidates, 255, 2, 255, PGC_SUSET, 0, NULL, NULL, NULL);
    MarkGUCPrefixReserved("jev");
    previous_planner_hook = planner_hook;
    previous_join_search_hook = join_search_hook;
    planner_hook = jev_planner;
    join_search_hook = join_search;
}

void
_PG_fini(void)
{
    if (planner_hook == jev_planner)
        planner_hook = previous_planner_hook;
    if (join_search_hook == join_search)
        join_search_hook = previous_join_search_hook;
}
