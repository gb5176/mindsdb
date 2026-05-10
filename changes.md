# Changes — Consolidated Summary

All changes made across multiple sessions to integrate MindsDB with IBM DB2 (via JDBC), Google Vertex AI / Gemini, and a semantic SQL cache. Organized by file and area, with each entry explaining what changed and why.

---

## Table of Contents

1. [DB2 JDBC Handler (new)](#1-db2-jdbc-handler)
2. [Google Gemini Handler — Vertex AI support](#2-google-gemini-handler)
3. [PydanticAI Agent — Mode & Dialect](#3-pydanticai-agent--mode--dialect)
4. [PydanticAI Agent — Knowledge Base Pre-fetch](#4-pydanticai-agent--knowledge-base-pre-fetch)
5. [PydanticAI Agent — Token & Result Visibility](#5-pydanticai-agent--token--result-visibility)
6. [PydanticAI Agent — SQL Cache System](#6-pydanticai-agent--sql-cache-system)
7. [PydanticAI Agent — KB Type Auto-detection](#7-pydanticai-agent--kb-type-auto-detection)
8. [PydanticAI Agent — Misc Fixes](#8-pydanticai-agent--misc-fixes)
9. [DataCatalogBuilder](#9-datacatalogbuilder)
10. [Knowledge Base Controller](#10-knowledge-base-controller)
11. [Model Factory — Vertex AI & MindsDB Model](#11-model-factory)
12. [Agent Constants](#12-agent-constants)
13. [Environment / Startup](#13-environment--startup)
14. [Reverted or Superseded Changes](#14-reverted-or-superseded-changes)

---

## 1. DB2 JDBC Handler

**File:** `mindsdb/integrations/handlers/db2_handler/db2_jdbc_handler.py`

### 1a — New JDBC-based DB2 handler

**What:** Created a new handler class `DB2JDBCHandler` using `jaydebeapi` (JDBC bridge). Supports SSL/TLS via JDBC without requiring IBM GSKit keystores. Searches common Windows paths (DBeaver driver cache, IBM SQLLIB) to locate `db2jcc4.jar` automatically when not explicitly configured.

**Why:** The existing `ibm_db` CLI-based handler fails in SSL/TLS corporate environments because it requires manual GSKit keystore setup that is difficult on Windows. JDBC handles SSL certificate negotiation automatically, matching how DBeaver connects to the same DB2 instance.

---

### 1b — Backtick identifier quoting regex extended to uppercase

**What:** In the `query()` method, the regex patterns for steps 2 and 3 (backtick removal) were extended from `[a-z_][a-z0-9_]*` to `[a-zA-Z_][a-zA-Z0-9_]*`.

**Why:** MindsDB wraps SQL identifiers in backticks (MySQL wire protocol convention). The original regex only matched lowercase identifiers, so already-uppercase identifiers like `` `PROJECT_ID` `` or `` `UNIT_SKEY` `` were left with backticks. DB2 does not support backtick quoting and returned `SQLCODE=-7` (character not valid) errors on every query.

---

### 1c — LIMIT → FETCH FIRST conversion

**What:** Added Step 4 regex in `query()` that converts:
- `LIMIT N` → `FETCH FIRST N ROWS ONLY`
- `LIMIT N OFFSET M` → `OFFSET M ROWS FETCH FIRST N ROWS ONLY`

**Why:** MindsDB appends `LIMIT N` to sample-data and agent exploratory queries. DB2 does not support the `LIMIT` keyword (ISO SQL 2008 standard: `FETCH FIRST N ROWS ONLY`). Without this fix, every catalog build query and every agent query failed with a DB2 syntax error.

---

## 2. Google Gemini Handler

**File:** `mindsdb/integrations/handlers/google_gemini_handler/google_gemini_handler.py`

### 2a — Vertex AI / service account support

**What:** Replaced the direct `google.generativeai` API with the new `google.genai` client SDK. Added `_configure_genai()` and `_get_service_account_credentials()` methods. When a service account JSON is present (from model params, engine params, or `GOOGLE_APPLICATION_CREDENTIALS`), the client is configured for Vertex AI with `vertexai=True`, `project=<project>`, and `location=<location>`. Falls back to API key otherwise.

**Why:** The old `genai.configure(api_key=...)` approach does not support Vertex AI endpoints required by corporate GCP environments. Service account credentials allow access to Vertex AI's `gemini-embedding-001` and other enterprise-grade models that are not available via public API keys.

---

### 2b — SSL certificate fix

**What:** At module load time, set `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, and `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` to the path returned by `certifi.where()`.

**Why:** On Windows in corporate environments, gRPC/SSL fails with `CERTIFICATE_VERIFY_FAILED` because Python's bundled certificate bundle may be outdated or bypassed by a corporate proxy CA. Using `certifi`'s CA bundle resolves this before any SSL connection is opened.

---

### 2c — `time.sleep(40)` between Gemini predict calls

**What:** Added `time.sleep(40)` before each `client.models.generate_content()` call in the `predict()` method.

**Why:** The Gemini API free tier (and some Vertex AI quotas) enforce a requests-per-minute limit. When MindsDB sends multiple prompts in a tight loop, the API returns quota errors. The 40-second sleep keeps the request rate within limits. This is a temporary workaround — in production with higher quotas, this sleep can be removed.

---

### 2d — Location defaulting

**What:** `location = args.get('location', connection_args.get('location', 'global'))` — falls back to `'global'` if not specified in either model or engine args.

**Why:** Vertex AI requires a location (region). Rather than fail silently, the code defaults to `'global'` so it works in simple cases. For production, the correct location (e.g., `'us-central1'`) should always be specified explicitly.

---

### 2e — Stop sequence to prevent fake Observation generation (langchain path)

**What:** In `langchain_agent.py`, a stop sequence `'\nObservation:'` is added to model kwargs before calling the LLM.

**Why:** Gemini tends to generate the entire ReAct loop by itself — it produces an `Action`, then fabricates an `Observation` (fake query result), and continues to the next `Action`, all without actually executing any tool. The agent was consuming these hallucinated observations as if they were real query results. Adding `'\nObservation:'` as a stop sequence forces the LLM to stop after generating an `Action` so the framework can execute the actual tool and inject the real result.

---

## 3. PydanticAI Agent — Mode & Dialect

**File:** `mindsdb/interfaces/agents/pydantic_ai_agent.py`

### 3a — `sql_dialect` and `return_sql_only` params

**What:**
- Added `self.sql_dialect = self.agent.params.get("sql_dialect", None)` — stores the target SQL dialect (`"db2"`, `"mysql"`, `"postgres"`, or absent for MindsDB).
- `self.return_sql_only = self.agent_mode == "sql"` — when `mode='sql'`, the agent returns the SQL text instead of executing it. Driven solely by `mode`, independent of `sql_dialect`.

**Why:** `sql_dialect` controls only syntax transformation (MindsDB SQL → DB2 SQL). `mode='sql'` controls whether the SQL is executed or returned as text. These are separate concerns: a `mode='text'` agent can still target DB2 (it executes the query via the DB2 handler), while `mode='sql'` returns the SQL for the client to execute. Coupling them was confusing and broke the SQL cache (see Change 6a).

---

### 3b — DB2 dialect instructions updated: LLM writes MindsDB SQL

**What:** The `"db2"` entry in `_dialect_instructions` now tells the LLM to write standard MindsDB SQL with 3-part table names and `LIMIT N`. The system converts the final answer automatically via `_apply_sql_dialect()`.

**Why (why this replaced the earlier approach of telling the LLM to write DB2 SQL):** The earlier approach instructed the LLM to generate DB2 syntax (`FETCH FIRST`, 2-part names) for the final query. This caused the SQL cache to store DB2-format SQL. If the agent mode is later switched to `mode='text'`, MindsDB must execute the SQL itself — it cannot understand `FETCH FIRST` or 2-part names. By keeping the LLM in MindsDB SQL mode, cache entries remain valid in both `mode='sql'` and `mode='text'`, and `_to_db2_sql()` handles the full conversion on the way out.

---

### 3c — `_to_db2_sql()` static method

**What:** Encapsulates the same regex pipeline as the DB2 JDBC handler's `query()` method: strips the MindsDB connection-name prefix, removes backtick quoting, uppercases identifiers, converts `LIMIT N` to `FETCH FIRST N ROWS ONLY`.

**Why:** The agent needs to convert the LLM's MindsDB SQL into DB2 SQL when returning it to the user in `mode='sql'`. Centralizing this logic in one place (rather than duplicating the regex in both the handler and the agent) ensures consistency and makes future changes easier.

---

### 3d — Skip MindsDB SQL instructions when targeting non-MindsDB dialect

**What:** When `return_sql_only=True` and `sql_dialect` is not `"mindsdb"`, the MindsDB-specific SQL instructions block is omitted from `base_prompt`.

**Why:** The standard MindsDB SQL instructions (MindsDB 3-part names, agent SQL conventions) are irrelevant when the agent is configured to produce a query for DB2 or another external database. The dialect-specific instructions added for that mode replace them entirely.

---

### 3e — Legacy skills bridge

**What:** After building `tables_list` and `knowledge_bases_list` from `data.tables` and `data.knowledge_bases`, the agent loops over `agent.skills_relationships` and appends any text2sql skill tables and retrieval skill KBs to those lists.

**Why:** Agents created via the old skill-based API (before the `data` param existed) stored table names inside skill objects. The new code path only reads `data.tables`, so legacy agents were silently missing their data sources. The bridge ensures backwards compatibility without requiring users to recreate their agents.

---

## 4. PydanticAI Agent — Knowledge Base Pre-fetch

**File:** `mindsdb/interfaces/agents/pydantic_ai_agent.py`

### 4a — `_fetch_kb_context()` method

**What:** Queries all schema KBs with the user's question using `SELECT * FROM kb WHERE content LIKE '<question>'`, extracts the `chunk_content` column, truncates each result to 300 chars, caps total output at 3,000 chars.

**Why:** KBs containing schema/column information were never being consulted during the planning step — the LLM relied entirely on the sample-data catalog. By pre-fetching KB content before the planning call, focused column information is injected into the planning prompt, making KBs actively useful. The size caps are critical: without them, the initial implementation injected 21,168 chars (18,620 request tokens) into the planning prompt, far exceeding the token cost of the original approach.

---

### 4b — KB pre-fetch runs in parallel with catalog build

**What:** When KBs are present, the catalog build (`DataCatalogBuilder.build_data_catalog`) and KB pre-fetch (`_fetch_kb_context`) run concurrently in a `ThreadPoolExecutor` with 2 workers. Each thread gets its own `contextvars.copy_context()` to safely propagate MindsDB's `ContextVar`-based session context.

**Why:** Both operations make multiple database/vector-store round trips and are independent. Running them sequentially adds 3–5 seconds to every agent call. The ContextVar copy is required because a single Context object cannot be entered by two threads simultaneously — sharing it would corrupt the session state or deadlock.

---

### 4c — SQL cache KB excluded from schema pre-fetch

**What:** In `_fetch_kb_context`, KBs whose bare name matches `self.sql_cache_kb` are skipped.

**Why:** The SQL cache KB stores `(question → SQL)` pairs, not schema information. Querying it for schema context would inject SQL fragments into the planning prompt, confusing the LLM about table structure. Only KBs with a schema role should be pre-fetched for context.

---

## 5. PydanticAI Agent — Token & Result Visibility

**File:** `mindsdb/interfaces/agents/pydantic_ai_agent.py`

### 5a — Token usage logged per LLM call

**What:** After the planning `run_sync()` and after each execution-loop `run_sync()`, `result.usage()` is called and the token counts are logged:
```
[TOKEN USAGE] planning: request=N, response=N, total=N, details={...}
[TOKEN USAGE] loop#N: request=N, response=N, total=N, details={...}
```

**Why:** There was no visibility into LLM token consumption. This made it impossible to verify whether KB integration was saving tokens (the primary goal). These logs enable direct before/after comparison in `mindsdb_*.log`.

---

### 5b — Query result logged after every SQL execution

**What:** `logger.info(f"query_data columns={...}, rows={...}, head=\n{...}")` after every `execute_sql()` call.

**Why:** The MindsDB console was showing `null` for the agent's answer with no indication of what the final SQL query returned. This log line exposes the column names, row count, and first 2 rows, making it immediately clear whether the query returned data or was empty.

---

### 5c — Exploratory query results truncated to 3,000 chars

**What:** After converting an exploratory result to a markdown table, if the result exceeds 3,000 chars it is truncated with a note showing total row and column count and a hint to use specific columns.

**Why:** Wide tables (SELECT * on a 100-column table) and long result sets can permanently bloat the message history passed to the LLM on every subsequent loop. Each overly large result competes with the data catalog and reduces the effective context window available for the LLM's reasoning.

---

### 5d — Empty final query fed back as exploratory

**What:** If a `final_query` returns 0 rows and the exploratory budget is not exhausted, the empty result is appended to `exploratory_query_results` with a hint about DB2 date sentinel values (`20991231` = open/not-completed), and `continue` triggers another LLM loop iteration.

**Why:** The agent returned `null` in the console when the DB2 query returned 0 rows. Root cause: the LLM generated `COMPL_DATE_SKEY = 20991231 AND EXEC_FIN_DATE_TIME < CURRENT_TIMESTAMP` which is contradictory — `20991231` is a sentinel meaning "record is still open", but the execution time filter required a past timestamp. Feeding back the empty result with the sentinel hint allows the LLM to correct its filter logic instead of silently returning null.

---

## 6. PydanticAI Agent — SQL Cache System

**File:** `mindsdb/interfaces/agents/pydantic_ai_agent.py`

### 6a — `_fetch_similar_sql_examples()` method + `chunk_content` fix

**What:** Queries the SQL cache KB using semantic search (`WHERE content LIKE '<question>'`), filters by `relevance >= threshold` and `tenant_id = current_tenant`, then reads `chunk_content` (not `content`) to extract the stored question text.

The critical fix: the old code read `row.get("content", "")` which always returned an empty string. ChromaDB stores the embedded text in a column named `chunk_content` — the `content` column only exists in the WHERE clause as a semantic search trigger and never appears in SELECT results. Because `q` was always `""`, the condition `if q and sql` was always False, so every cache hit was silently discarded. The KB was finding matches but throwing them all away.

**Why:** Injecting past approved (question → SQL) pairs into the planning prompt as few-shot examples reduces token usage and improves SQL accuracy for recurring queries. The `chunk_content` bug completely disabled this feature despite the KB returning valid hits.

---

### 6b — `_stage_query_example()` method

**What:** After returning the final SQL answer, writes a `(question, sql, dialect, tenant_id, user_id, created_at)` entry to a local JSONL file (`<mindsdb-root>/sql_cache_staging.jsonl`) in a background daemon thread. Also logs a ready-to-run `INSERT INTO <kb>` command for the user to approve.

**Why:** Automatically inserting every generated SQL into the cache would pollute it with incorrect or low-quality queries. The staging approach lets the user review generated SQL before promoting it. The daemon thread means zero latency impact on the agent response. The INSERT is logged so it is always visible regardless of UI rendering.

---

### 6c — INSERT and staging store MindsDB SQL, not DB2 SQL

**What:** In the `mode='sql'` final-query block:
- `_safe_sql = sql_query.replace(...)` (was `final_sql`) — uses the pre-dialect MindsDB SQL.
- `_stage_query_example(original_question, sql_query)` (was `final_sql`).
- `_dialect = "mindsdb"` hardcoded in the INSERT (was `self.sql_dialect`).

The answer returned to the user (`response_text` = `final_sql`) is still DB2 format — only the cache entry stores MindsDB SQL.

**Why:** DB2-format SQL in the cache (`FETCH FIRST`, 2-part names) would fail if the agent runs in `mode='text'` later, because MindsDB must execute the SQL itself and does not support DB2 syntax. MindsDB SQL is the canonical form that works in both modes.

---

### 6d — `mode='text'` final query also staged

**What:** Added `self._stage_query_example(original_question, sql_query)` in the `mode='text'` FINAL_QUERY path, before the final yields.

**Why:** Previously only `mode='sql'` staged entries. When `mode='text'` is used, the LLM naturally generates MindsDB SQL (no dialect conversion), so staging here gives the cleanest cache entries. Placed before yields to avoid `GeneratorExit` silently skipping the call if the consumer closes the generator early.

---

### 6e — SQL cache examples injected into planning prompt

**What:** Before the planning LLM call, `_fetch_similar_sql_examples()` is called. If matching examples are found, they are appended to `planning_prompt_text` with a header:
```
=== Past Similar SQL Queries (REFERENCE ONLY — DO NOT COPY VERBATIM) ===
```

**Why:** The planning step determines the execution plan before any SQL is written. Injecting prior approved queries at this stage gives the LLM direct evidence of correct SQL patterns for similar questions, reducing exploratory iterations and improving accuracy. The "REFERENCE ONLY" disclaimer prevents the LLM from blindly copying stale SQL with wrong column names.

---

## 7. PydanticAI Agent — KB Type Auto-detection

**Files:**
- `mindsdb/interfaces/knowledge_base/controller.py`
- `mindsdb/interfaces/agents/pydantic_ai_agent.py`
- `mindsdb/interfaces/agents/utils/data_catalog_builder.py`

### 7a — `kb_type` field in `KnowledgeBaseInputParams`

**File:** `controller.py`

**What:** Added `kb_type: str | None = None` to `KnowledgeBaseInputParams` (which has `extra = "forbid"`).

**Why:** Without this, `CREATE KNOWLEDGE BASE ... USING kb_type='sql_cache'` raised "Parameter 'kb_type' is not allowed". The `KnowledgeBaseInputParams` pydantic model rejects unknown fields by default.

---

### 7b — Agent auto-detects SQL cache KB from `kb_type` metadata

**File:** `pydantic_ai_agent.py __init__`

**What:** After `MindsDBQuery` is constructed, the agent loops through `knowledge_bases_list`, looks up each KB's stored params via `session.kb_controller.get()`, and if `kb_type='sql_cache'` is found, sets `self.sql_cache_kb` automatically. Explicit `sql_cache_kb` agent param still works as an override.

**Why:** Previously the SQL cache KB had to be specified twice: once when creating the KB, and again as `sql_cache_kb='my_kb'` in the agent's USING clause. The KB itself is the right place to declare its role. Storing `kb_type` on the KB means the agent needs no extra configuration — it just lists KB names in `data.knowledge_bases` and the system discovers each one's role automatically. This is also extensible to future types (`domain`, `glossary`, etc.) without changing any agent code.

---

### 7c — SQL cache KB excluded from data catalog

**File:** `data_catalog_builder.py`

**What:** Added `exclude_kbs` parameter to `DataCatalogBuilder.__init__`. The `build_data_catalog()` method filters out any KB whose bare name is in `_exclude_kbs` before building the catalog string.

**Why:** The SQL cache KB stores `(question → SQL)` pairs, not schema or business data. If included in the data catalog, these entries would appear to the LLM as queryable tables containing SQL fragments — confusing the planning prompt and potentially causing the LLM to try querying the cache for business data.

---

## 8. PydanticAI Agent — Misc Fixes

**File:** `mindsdb/interfaces/agents/pydantic_ai_agent.py`

### 8a — Single-column alias fallback in `execute()`

**What:** When mapping LLM-requested target columns to query result columns, if a target column is not found by name (even case-insensitively) but the result has exactly one column, that column's values are used.

**Why:** Some DB2 aggregate queries (e.g., `SELECT COUNT(*) AS total_count FROM ...`) have their alias stripped by the JDBC driver, returning a generic column name like `1` or `EXPR$0`. Without this fallback, the expected target column was set to `None`, resulting in a null column in the response instead of the actual count value.

---

### 8b — Knowledge base query rules appended to `base_prompt`

**What:** When KBs are present, a block is added to `base_prompt` reminding the LLM not to query KBs just for schema discovery, to always include `WHERE content LIKE '...'` when querying KBs, and to match 2-part KB result names to full 3-part data catalog names before using them in SQL.

**Why:** Without this guidance, the LLM sometimes issued open-ended `SELECT * FROM kb` queries (expensive, returns noise) or tried to use 2-part table names found in KB results directly in SQL, causing table-not-found errors.

---

## 9. DataCatalogBuilder

**File:** `mindsdb/interfaces/agents/utils/data_catalog_builder.py`

### 9a — `include_metadata` parameter

**What:** Added `include_metadata: bool = True` constructor parameter. When `False`, the `SHOW COLUMNS` fetch is skipped and `metadata_csv` is set to `None`.

**Why:** Needed to support the option of skipping `SHOW COLUMNS` when KBs are already supplying column information. Currently `include_metadata=True` always (see [Section 14](#14-reverted-or-superseded-changes)), but the parameter remains for future use.

---

### 9b — `sample_rows` parameter

**What:** Added `sample_rows: int = 5` constructor parameter. When `0`, no sample data is fetched. Passed through to `build_table_catalog_entry()`. Set to `3` when KBs are present, `5` otherwise.

**Why:** When KBs supply focused column/schema context, the sample data's primary role is to show data format and types rather than infer schema. Fewer rows reduce token cost without losing useful information. Without KBs the agent relies on sample data to understand schema, so more rows are kept.

---

### 9c — `exclude_kbs` parameter

**What:** Added `exclude_kbs: list = None` constructor parameter. The `build_data_catalog()` method filters out any KB whose bare name (last `.`-delimited part) matches any entry in `_exclude_kbs` before building catalog entries.

**Why:** See [Section 7c](#7c--sql-cache-kb-excluded-from-data-catalog).

---

## 10. Knowledge Base Controller

**File:** `mindsdb/interfaces/knowledge_base/controller.py`

### 10a — `kb_type` field added to `KnowledgeBaseInputParams`

```python
kb_type: str | None = None  # 'schema' (default) | 'sql_cache' | future types
```

**What:** Added this field to the Pydantic model that validates `CREATE KNOWLEDGE BASE ... USING` parameters.

**Why:** See [Section 7a](#7a--kb_type-field-in-knowledgebaseinputparams).

**Usage:**
```sql
CREATE KNOWLEDGE BASE my_sql_cache_kb
USING
    storage = my_chroma_store.sql_kb_v2,
    embedding_model = { "provider": "vertex_ai", "model_name": "gemini-embedding-001",
                        "project": "my-gcp-project", "location": "us-central1" },
    metadata_columns = ['sql_query', 'dialect', 'tenant_id', 'user_id', 'created_at'],
    kb_type = 'sql_cache';
```

---

## 11. Model Factory

**File:** `mindsdb/interfaces/agents/utils/pydantic_ai_model_factory.py`

### 11a — `MindsDBModel` class

**What:** Added a `MindsDBModel(Model)` class that wraps a MindsDB-managed predictor as a pydantic-ai `Model`. Converts pydantic-ai message formats to `role/content` dicts, injects JSON schema / tool definitions into the prompt, and parses `<tool_call>` blocks or raw JSON from the model's text response.

**Why:** This allows any MindsDB predictor (e.g., a fine-tuned model or an API-proxied LLM stored in MindsDB) to be used as the underlying LLM for an agent, without requiring the predictor to natively support pydantic-ai's function-calling API.

---

### 11b — Vertex AI project + location in model kwargs

**What:** In `get_pydantic_ai_model_kwargs()`, when `provider == "google"` and `args.get("project")` is present, `project` and `location` are added to `kwargs` for the Vertex AI path. Falls back to API key for standard Google AI.

**Why:** pydantic-ai's `GoogleProvider` needs `project` and `location` to route requests to Vertex AI endpoints. Previously the handler always tried the public Google AI API, failing for users authenticated via service accounts without a public API key.

---

## 12. Agent Constants

**File:** `mindsdb/interfaces/agents/utils/constants.py`

### 12a — Added `gemini-3.1-pro-preview` model

**What:** Added `"gemini-3.1-pro-preview"` to the `GOOGLE_GEMINI_CHAT_MODELS` tuple.

**Why:** The model was rejected during agent creation with an "unknown model" validation error. The model is a valid Vertex AI preview model that was not yet in the allowed list.

---

## 13. Environment / Startup

### 13a — `GOOGLE_APPLICATION_CREDENTIALS` set in startup script

**File:** `start_mindsdb_with_db2.ps1`

**What:** The startup PowerShell script sets `$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\...\vertex-ai.json"` before launching MindsDB.

**Why:** pydantic-ai's `GoogleProvider` uses Application Default Credentials (ADC) automatically when `GOOGLE_APPLICATION_CREDENTIALS` is set. Setting it in the startup script means all MindsDB processes (HTTP, MySQL proxy, job scheduler) inherit the credential without any code changes. This is the standard GCP authentication pattern.

---

## 14. Reverted or Superseded Changes

These changes were made and later undone or replaced when a better approach emerged.

---

### 14a — Skip SHOW COLUMNS when KBs present (reverted)

**Original (Session 1):** `_include_metadata = not _has_kbs` — when KBs were present, SHOW COLUMNS was skipped to save ~500 tokens/table.

**Reverted to:** `_include_metadata = True` always.

**Why reverted:** Skipping SHOW COLUMNS caused the LLM to hallucinate column names for tables that were not semantically close to the question in the KB. For example, join tables or dimension tables without KB coverage were queried with made-up column names, producing SQL errors. The KB pre-fetch can reduce the *need* for metadata but cannot fully replace it. The `include_metadata` parameter remains available for cases where KBs genuinely provide complete coverage.

---

### 14b — LLM told to write DB2 SQL directly (superseded)

**Original (Session 2):** The `"db2"` entry in `_dialect_instructions` told the LLM to generate DB2 syntax directly: `FETCH FIRST N ROWS ONLY`, 2-part table names, uppercase, no backticks.

**Superseded by:** LLM always writes MindsDB SQL; `_apply_sql_dialect()` converts on the way out.

**Why superseded:** The SQL cache was storing DB2-format SQL entries. When the agent mode is switched to `mode='text'`, MindsDB executes the SQL itself and needs MindsDB-format SQL (3-part names, `LIMIT N`). DB2-format cache entries caused table-not-found errors in that mode. See [Section 6c](#6c--insert-and-staging-store-mindsdb-sql-not-db2-sql).

---

### 14c — `return_sql_only` coupled to `sql_dialect` (superseded)

**Original (Session 2):** `return_sql_only` was enabled when either `mode='sql'` OR `sql_dialect` was explicitly set to a non-MindsDB value.

**Superseded by:** `return_sql_only = self.agent_mode == "sql"` — driven solely by `mode`.

**Why superseded:** `sql_dialect` controls the syntax of the returned SQL. `mode='sql'` controls whether the SQL is returned or executed. A `mode='text'` agent can target DB2 and still execute the query via the DB2 JDBC handler (the handler does its own MindsDB→DB2 SQL conversion in `query()`). Coupling them broke `mode='text'` with DB2 targets.

---

## Quick Reference — SQL Cache KB Setup

```sql
-- 1. Create the SQL cache KB with kb_type='sql_cache'
CREATE KNOWLEDGE BASE my_sql_cache_kb
USING
    storage = my_chroma_store.sql_kb_v2,
    embedding_model = {
        "provider": "vertex_ai",
        "model_name": "gemini-embedding-001",
        "project": "my-gcp-project",
        "location": "us-central1"
    },
    metadata_columns = ['sql_query', 'dialect', 'tenant_id', 'user_id', 'created_at'],
    kb_type = 'sql_cache';

-- 2. Create the agent — no sql_cache_kb param needed; auto-detected from kb_type
CREATE AGENT my_agent
USING
    model_name  = 'claude-sonnet-4-6',
    mode        = 'sql',
    sql_dialect = 'db2',
    data = {
        'tables':          ['my_db2_connection.SCHEMA.*'],
        'knowledge_bases': ['my_schema_kb', 'my_sql_cache_kb']
    };

-- 3. After a query, approve the staged entry from the log INSERT command
--    INSERT INTO my_sql_cache_kb (content, sql_query, dialect, ...) VALUES (...);

-- 4. To clear KB entries (no-WHERE DELETE is not supported):
DELETE FROM my_sql_cache_kb WHERE dialect = 'mindsdb';
-- Or drop + recreate with a NEW storage collection name (DROP does not clear ChromaDB data):
DROP KNOWLEDGE BASE my_sql_cache_kb;
CREATE KNOWLEDGE BASE my_sql_cache_kb USING storage = my_chroma_store.sql_kb_v3, ...;
```
