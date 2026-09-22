#ifndef JEV_PLANNER_H
#define JEV_PLANNER_H

#include "postgres.h"
#include "nodes/pathnodes.h"

/* GUCs belong to the PostgreSQL integration, never the protocol SDK. */
extern bool jev_enabled;
extern bool jev_debug;
extern bool jev_log_http;
extern char *jev_endpoint;
extern char *jev_model;
extern int jev_timeout_ms;
extern int jev_max_decisions;
extern int jev_max_candidates;
extern int jev_planning_timeout_ms;

extern void jev_begin_planning(Query *query);
extern void jev_end_planning(void);
extern int jev_decision_count(void);
extern void jev_check_planning_budget(void);
extern RelOptInfo *jev_join_search(PlannerInfo *root, int levels_needed, List *initial_rels);
pg_noreturn extern void jev_unsupported(const char *feature);

#endif
