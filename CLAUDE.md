# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Setup
```bash
pip install -e .
pip install -r requirements/requirements-dev.txt
pre-commit install
```

### Running MindsDB
```bash
python -m mindsdb
```

### Testing
```bash
make unit_tests                   # All unit tests with coverage
make unit_tests_slow              # Including slow tests
make integration_tests            # Integration tests
make datasource_integration_tests # Handler-specific integration tests

# Run a single test
pytest tests/unit/path/to/test_file.py::test_function_name -v

# Run with pattern match
pytest -v -k "test_name_pattern"
```

> Executor tests (`tests/unit/executor/`) are run separately due to side effects. Auth integration tests also run separately.

### Linting & Formatting
```bash
make format   # Run pre-commit formatting (ruff)
make check    # Full validation: requirements, print statements, pre-commit
```

Ruff is the linter/formatter (120-char line length, Python 3.10+ target). Config is in `pyproject.toml`.

---

## Architecture Overview

### Entry Point
`mindsdb/__main__.py` orchestrates startup — it launches HTTP (REST), MySQL wire protocol, MCP, A2A, LiteLLM proxy, jobs, tasks, and the ML execution queue as separate processes/threads. `mindsdb/utilities/starters.py` contains the individual process start functions.

### Top-Level Structure
```
mindsdb/
├── api/             # Protocol servers and query execution
├── integrations/    # ~220 data/app/ML handler connectors
├── interfaces/      # Business logic layer
├── utilities/       # Config, auth, logging, filesystem helpers
├── migrations/      # Alembic DB migrations
└── tests/
```

### Query Execution Flow
SQL comes in via HTTP REST or the MySQL wire protocol → `mindsdb/api/executor/command_executor.py` parses it with `mindsdb-sql-parser` → dispatches to the appropriate interface (model, agent, knowledge base, job, etc.) or passes through to a connected data handler.

MindsDB extends standard SQL with custom DDL: `CREATE AGENT`, `CREATE KNOWLEDGE_BASE`, `CREATE JOB`, `CREATE TRIGGER`, `CREATE CHATBOT`, etc.

### Handler Architecture (`mindsdb/integrations/`)
Each connector lives in `handlers/{name}_handler/` and follows a consistent pattern:
- `__init__.py` — exports `Handler`, `name`, `type`, `support_level`, `connection_args`, `connection_args_example`
- Main handler class inherits from a base in `integrations/libs/`
- Handler types are defined in `integrations/libs/const.py`: `HANDLER_TYPE` (DATA, ML, APP, FILE) and `HANDLER_SUPPORT_LEVEL`
- Each handler can have its own `requirements.txt`; installed as extras via `setup.py` (e.g., `pip install -e .[postgres]`)

### Interfaces Layer (`mindsdb/interfaces/`)
Business logic for each MindsDB concept:
- `agents/` — AI agent framework built on pydantic-ai; handles multi-step tool-calling agents
- `model/` — ML model lifecycle (train, predict, update)
- `knowledge_base/` — Vector DB abstraction (ChromaDB, pgvector)
- `jobs/` — Scheduled job execution
- `skills/` — Reusable agent skills (SQL query, knowledge base retrieval, etc.)
- `storage/` — SQLAlchemy ORM models for MindsDB's internal metadata DB

### PydanticAI Agent (`mindsdb/interfaces/agents/pydantic_ai_agent.py`)

The agent runs a **two-phase loop** per request:

1. **Planning phase** — a separate `Agent` call generates a `PlanResponse` (step-by-step plan + estimated step count). The planning prompt includes the data catalog, KB pre-fetch context, and any SQL cache examples.
2. **Execution loop** — a second `Agent` streams `AgentResponse` objects. Each response carries a `type`:
   - `exploratory_query` — run the SQL, feed results back as context, loop again (max 20 iterations, `MAX_EXPLORATORY_QUERIES`)
   - `final_query` — the answer SQL; execute it and return rows, or if `mode='sql'`, return the SQL text directly
   - `final_text` — return a plain-text answer with no SQL execution (`mode='text'` only)

**Key agent params** (set in `CREATE AGENT … USING` or `UPDATE agent … SET params`):

| Param | Values | Effect |
|-------|--------|--------|
| `mode` | `text` (default), `sql` | `sql` → return SQL text, never execute. `text` → execute and return data or a text answer. |
| `sql_dialect` | `db2`, `mindsdb`/absent | Transforms final SQL syntax. **Only active when `mode='sql'`** — silently ignored otherwise. |
| `sql_cache_kb` | KB name string | Enables semantic SQL cache; see below. |
| `sql_cache_min_relevance` | float, default `0.7` | Minimum similarity to inject a cached example. |
| `sql_cache_max_examples` | int, default `3` | Max cached examples injected per query. |

**Mode files** (`mindsdb/interfaces/agents/modes/`):
- `sql.py` — `AgentResponse` has `sql_query + type` only; planning skips `final_text` path.
- `text_sql.py` — `AgentResponse` adds a `text` field; LLM can answer without SQL.
- `base.py` — `ResponseType` constants and `PlanResponse`.

**SQL execution permission model** (`utils/sql_toolkit.py`):
- `sql_toolkit.execute(ast_node)` — SELECT / SHOW / DESCRIBE / EXPLAIN only. Raises on INSERT/UPDATE/DELETE.
- INSERT/DDL must go through `sql_toolkit.command_executor.execute_command(ast_node)` directly.

**ContextVar propagation in threads**: `mindsdb.utilities.context` uses `contextvars.ContextVar`. When spawning threads (e.g. `ThreadPoolExecutor`, `threading.Thread`), call `contextvars.copy_context().run(fn, *args)` **per thread** — a single copied context cannot be entered by two threads simultaneously.

**Semantic SQL cache flow**:
1. After returning SQL (`mode='sql'`), `_stage_query_example()` appends a JSONL entry to `<mindsdb-root>/sql_cache_staging.jsonl` in a daemon thread. Nothing is written to the KB automatically.
2. User reviews the file and runs the logged `INSERT INTO <sql_cache_kb>` command to approve an entry.
3. On the next similar query, `_fetch_similar_sql_examples()` queries the KB (filtered by `relevance >= threshold` and `tenant_id = ctx.company_id`) and injects matching (question → SQL) pairs into the planning prompt as few-shot examples.

### API Layer (`mindsdb/api/`)
- `http/` — Flask + Starlette/Uvicorn REST API; namespaces under `http/namespaces/`; OpenAPI spec at `http/openapi.yml`
- `mysql/` — MySQL wire protocol server (allows BI tools to connect via MySQL drivers)
- `mcp/` — Model Context Protocol server
- `a2a/` — Agent-to-Agent communication protocol
- `litellm/` — LLM provider proxy

### Configuration
YAML-based config loaded via `mindsdb/utilities/config.py`. Key environment variables control which APIs are enabled (`MINDSDB_APIS`) and deployment behavior.

### Requirements Files
| File | Purpose |
|------|---------|
| `requirements/requirements.txt` | Core runtime |
| `requirements/requirements-dev.txt` | Development tools |
| `requirements/requirements-test.txt` | Test dependencies |
| `requirements/requirements-agents.txt` | Agent framework extras |
| `requirements/requirements-kb.txt` | Knowledge base extras |

### CI
GitHub Actions workflows in `.github/workflows/`: unit tests run on a matrix of Python 3.11/3.12 × Linux/macOS using `uv`. Integration tests run against deployed environments. Coverage reports land in `reports/htmlcov/`.
