EXTENSION = pg_jevplanner
MODULE_big = pg_jevplanner
DATA = sql/pg_jevplanner--0.1.0.sql
PGFILEDESC = "pg_jevplanner - JEV inner-join ordering with native physical planning"

PG_CONFIG ?= pg_config
CURL_CONFIG ?= curl-config
UV ?= uv
SOURCES = src/pg_jevplanner.c src/jev_planner.c src/sdk.c
OBJS = $(SOURCES:.c=.o)
PG_CPPFLAGS = -I$(srcdir)/src $(shell $(CURL_CONFIG) --cflags)
SHLIB_LINK += $(shell $(CURL_CONFIG) --libs)

PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)

$(OBJS): src/sdk.h src/jev_planner.h

.PHONY: integration audit
integration: all
	$(UV) run benchmark/run.py --self-test --pg-config "$(PG_CONFIG)"
audit: all
	$(UV) run benchmark/run.py --audit
