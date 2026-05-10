# MindsDB — Setup & Query Flow Guide

A practical guide covering what MindsDB is, how the AI agent pipeline works end-to-end, every object type you need to create, all parameters explained, and the exact processing flow triggered by a user query.

---

## Table of Contents

1. [What is MindsDB?](#1-what-is-mindsdb)
2. [How It Works — Big Picture](#2-how-it-works--big-picture)
3. [Object Types Overview](#3-object-types-overview)
4. [Engine](#4-engine)
5. [Database Connection](#5-database-connection)
6. [Knowledge Base (KB)](#6-knowledge-base-kb)
7. [Agent](#7-agent)
8. [How to Query](#8-how-to-query)
9. [Query Processing Flow (Step by Step)](#9-query-processing-flow-step-by-step)
10. [SQL Cache Workflow](#10-sql-cache-workflow)
11. [Complete Setup — Example](#11-complete-setup--example)

---

## 1. What is MindsDB?

MindsDB is an AI layer that sits in front of your existing databases. It lets you ask natural-language questions and get back results — or SQL — without writing queries yourself.

Key capabilities relevant to this setup:

| Capability | What it does |
|-----------|-------------|
| **AI Agent** | Accepts a natural-language question, reasons about it, writes SQL, and returns either data rows or the SQL itself |
| **Knowledge Base (KB)** | A vector store of schema/column/past-query information that the agent consults to understand your database structure |
| **SQL Cache** | A specialised KB that stores approved past (question → SQL) pairs; injected as few-shot examples to improve future queries |
| **Multi-database** | Connects to DB2, PostgreSQL, MySQL, and others simultaneously via handler engines |
| **MindsDB SQL** | A superset of SQL for managing all of the above via familiar CREATE / DROP / SELECT / INSERT syntax |

---

## 2. How It Works — Big Picture

```
User Natural Language Question
         │
         ▼
   MindsDB Agent
         │
   ┌─────┴──────────────────────────────────────┐
   │  Parallel Preparation (before LLM call)    │
   │                                            │
   │  ┌─────────────────┐  ┌──────────────────┐ │
   │  │  Data Catalog   │  │  KB Pre-fetch    │ │
   │  │  (sample rows + │  │  (schema/column  │ │
   │  │   SHOW COLUMNS) │  │   context from   │ │
   │  │                 │  │   vector search) │ │
   │  └─────────────────┘  └──────────────────┘ │
   │                                            │
   │  ┌─────────────────────────────────────┐   │
   │  │  SQL Cache Lookup                   │   │
   │  │  (similar past questions → SQL)     │   │
   │  └─────────────────────────────────────┘   │
   └────────────────────────────────────────────┘
         │
         ▼
   Planning Phase (LLM Call 1)
   → Generates a step-by-step execution plan
         │
         ▼
   Execution Loop (LLM Call 2..N, max 20 iterations)
   → LLM writes SQL
   → If exploratory: MindsDB executes it, feeds results back
   → If final:
       mode='sql'  → convert to target dialect, return SQL text
       mode='text' → execute, return data rows
         │
         ▼
   SQL Cache Staging (background)
   → Writes (question, MindsDB SQL) to local JSONL file
   → Logs ready-to-run INSERT command for manual approval
```

---

## 3. Object Types Overview

To use the AI agent against a database you must create these objects in order:

| Step | Object | Purpose |
|------|--------|---------|
| 1 | **Engine** | Registers a data source driver (DB2, ChromaDB, etc.) |
| 2 | **Database Connection** | A named connection to your actual database using an engine |
| 3 | **Knowledge Bases** | Vector stores for table info, column info, and SQL cache |
| 4 | **Agent** | The AI agent that ties everything together |

All objects are created using MindsDB SQL syntax and persist across restarts.

---

## 4. Engine

An engine is a driver plugin that knows how to communicate with a specific data source. MindsDB ships with many built-in engines — you do not usually need to `CREATE ENGINE` for built-in ones like `db2` or `chromadb`. Engines are used implicitly when you specify `WITH ENGINE = '...'` in `CREATE DATABASE`.

**Built-in engines relevant to this setup:**

| Engine name | Data source |
|-------------|-------------|
| `db2` | IBM DB2 via JDBC (uses `jaydebeapi`) |
| `chromadb` | ChromaDB local vector store |
| `vertex_ai` (embedding) | Google Vertex AI embedding models |
| `google` (LLM) | Google Gemini / Vertex AI generative models |

---

## 5. Database Connection

A `CREATE DATABASE` in MindsDB creates a named connection to an external data source. Once created, you can query tables inside it using 3-part names: `<connection_name>.<schema>.<table>`.

### 5a — DB2 JDBC Connection

```sql
CREATE DATABASE my_db2_connection
WITH ENGINE = 'db2',
PARAMETERS = {
    "user":             "<db_user>",
    "password":         "<your_password>",
    "host":             "<your_db2_host>",
    "port":             "50010",
    "database":         "<your_database>",
    "jdbc_driver_path": "<path_to_db2jcc4.jar>"
};
```

**Parameter reference:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `user` | Yes | DB2 database user |
| `password` | Yes | DB2 user password |
| `host` | Yes | Hostname or IP of the DB2 server |
| `port` | Yes | DB2 listener port (default: `50000`; SSL often `50001`) |
| `database` | Yes | DB2 database name (as shown in `db2 list database directory`) |
| `jdbc_driver_path` | Yes (custom) | Absolute path to `db2jcc4.jar`. DBeaver stores a copy at `%APPDATA%\Local\DBeaver\drivers\...` |

> **Why JDBC instead of `ibm_db`?**  
> The native `ibm_db` driver requires IBM GSKit for SSL, which is complex to configure on Windows. JDBC (via `jaydebeapi`) handles SSL automatically — the same way DBeaver connects.

Once created, tables are reachable as:
```sql
SELECT * FROM my_db2_connection.myschema.my_table LIMIT 5;
```

---

### 5b — ChromaDB Store

ChromaDB is the vector database that backs the Knowledge Bases. Create one shared store; multiple KBs can use different collections within it.

```sql
CREATE DATABASE my_chroma_store
WITH ENGINE = 'chromadb',
PARAMETERS = {
    "persist_directory": "chroma_kb_storage"
};
```

**Parameter reference:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `persist_directory` | Yes | Relative or absolute path where ChromaDB persists its data. Relative paths are resolved from the MindsDB working directory. |

> All KBs created with `storage = my_chroma_store.<collection_name>` share the same on-disk directory but use separate ChromaDB collections (namespaced by collection name).

---

## 6. Knowledge Base (KB)

A Knowledge Base is a vector store that embeds text and allows semantic search. In this setup three KBs serve different purposes:

| KB | Role | `kb_type` |
|----|------|-----------|
| Tables KB | Stores table names + descriptions for semantic discovery | (default / absent) |
| Columns KB | Stores column names + descriptions per table | (default / absent) |
| SQL Cache KB | Stores approved (question → SQL) pairs as few-shot examples | `sql_cache` |

---

### 6a — Tables KB

```sql
CREATE KNOWLEDGE BASE my_tables_kb
USING
    storage        = my_chroma_store.tables_collection,
    embedding_model = {
        "provider":   "vertex_ai",
        "model_name": "gemini-embedding-001",
        "project":    "<your-gcp-project>",
        "location":   "us-central1"
    },
    metadata_columns = ['table_name'];
```

**Purpose:** When the agent receives a question, it semantically searches this KB to find which tables are relevant. A question like "how many projects are delayed?" will surface tables with descriptions containing words like "project", "schedule", "completion".

---

### 6b — Columns KB

```sql
CREATE KNOWLEDGE BASE my_columns_kb
USING
    storage        = my_chroma_store.columns_collection,
    embedding_model = {
        "provider":   "vertex_ai",
        "model_name": "gemini-embedding-001",
        "project":    "<your-gcp-project>",
        "location":   "us-central1"
    },
    metadata_columns = ['column_name', 'table_name'];
```

**Purpose:** Stores column-level descriptions so the agent can find the right column for phrases like "completion date" or "project owner" without guessing from sample data alone.

---

### 6c — SQL Cache KB

```sql
CREATE KNOWLEDGE BASE my_sql_cache_kb
USING
    storage        = my_chroma_store.sql_collection,
    embedding_model = {
        "provider":   "vertex_ai",
        "model_name": "gemini-embedding-001",
        "project":    "<your-gcp-project>",
        "location":   "us-central1"
    },
    kb_type          = 'sql_cache',
    metadata_columns = ['sql_query', 'dialect', 'tenant_id', 'user_id', 'created_at'];
```

**Purpose:** Stores approved (question → SQL) pairs. When a similar question is asked in the future, matching pairs are injected into the planning prompt as few-shot examples, improving accuracy and reducing exploratory iterations.

The `kb_type = 'sql_cache'` marker tells the agent to:
- **Not** include this KB in the data catalog shown to the LLM (it contains SQL, not schema info)
- **Not** pre-fetch it for schema context
- **Do** query it for similar past examples before planning

---

### KB Parameter Reference

| Parameter | Required | Description |
|-----------|----------|-------------|
| `storage` | Yes | `<chroma_connection>.<collection_name>` — which ChromaDB store and which collection inside it. Each KB should use a unique collection name. |
| `embedding_model` | Yes | Defines the model that converts text to vectors for storage and search. |
| `embedding_model.provider` | Yes | Provider for embedding. `vertex_ai` for Google Vertex AI; `openai` for OpenAI. |
| `embedding_model.model_name` | Yes | Embedding model name. `gemini-embedding-001` is Google's current production embedding model. |
| `embedding_model.project` | Vertex AI | GCP project ID with Vertex AI API enabled. |
| `embedding_model.location` | Vertex AI | GCP region. `us-central1` is the most widely supported Vertex AI region. |
| `metadata_columns` | No | List of columns that are stored as filterable metadata alongside the embedded text. Used for filtering on `tenant_id`, `dialect`, etc. |
| `kb_type` | No | Role of this KB. `sql_cache` = SQL pair store. Absent or `schema` = normal schema/column KB. |
| `content_columns` | No | Column(s) whose text is embedded (defaults to `content`). |
| `id_column` | No | Column used as document ID for deduplication (defaults to `id`). |

---

### Populating the Knowledge Bases

KBs are populated via INSERT statements. For the tables and columns KBs:

```sql
-- Insert a table description
INSERT INTO my_tables_kb (content, table_name)
VALUES (
    'Stores project-level information including project name, status, responsible unit, and schedule dates',
    'FACT_PROJECT'
);

-- Insert a column description
INSERT INTO my_columns_kb (content, column_name, table_name)
VALUES (
    'Completion date surrogate key. Value 20991231 means the record is still open (not yet completed).',
    'COMPL_DATE_SKEY',
    'FACT_PROJECT'
);
```

Each `content` value is what gets embedded and searched semantically. The `metadata_columns` values are stored as-is for filtering.

---

### Clearing KB Data

```sql
-- Delete entries by metadata column (safest approach)
DELETE FROM my_tables_kb WHERE table_name = 'FACT_PROJECT';

-- Delete all entries for one dialect
DELETE FROM my_sql_cache_kb WHERE dialect = 'mindsdb';
```

> **Note:** `DELETE FROM kb_name` without a WHERE clause is **not supported** by the ChromaDB handler — at least one filter condition is required.

> **Note:** `DROP KNOWLEDGE BASE my_kb` removes the MindsDB metadata record but does **not** clear the underlying ChromaDB collection. To guarantee a clean start, use a new collection name (e.g., `my_chroma_store.sql_collection_v2`) when recreating.

---

## 7. Agent

The Agent is the AI-powered query handler. It takes a natural-language question, consults the data catalog and KBs, uses an LLM to generate SQL, and returns either data rows or a SQL string.

```sql
CREATE AGENT my_db2_agent
USING
    model = {
        "provider":   "google",
        "model_name": "gemini-3.1-pro-preview",
        "project":    "<your-gcp-project>",
        "location":   "global"
    },
    mode        = 'sql',
    sql_dialect = 'db2',
    data = {
        "tables": [
            "my_db2_connection.myschema.dim_hierarchy",
            "my_db2_connection.myschema.fact_project",
            "my_db2_connection.myschema.fact_execution"
        ],
        "knowledge_bases": [
            "mindsdb.my_columns_kb",
            "mindsdb.my_tables_kb",
            "mindsdb.my_sql_cache_kb"
        ]
    },
    prompt_template = 'You are an expert SQL analyst working with the <your_schema> schema. All identifiers are uppercase.';
```

---

### Agent Parameter Reference

#### `model` block

| Parameter | Required | Description |
|-----------|----------|-------------|
| `provider` | Yes | LLM provider. `google` = Gemini / Vertex AI. `anthropic` = Claude. `openai` = OpenAI. |
| `model_name` | Yes | The specific model to use. For Google: `gemini-2.0-flash`, `gemini-3.1-pro-preview`, etc. |
| `project` | Vertex AI | GCP project ID. Required when using Vertex AI endpoints (service account auth). |
| `location` | Vertex AI | GCP region for the LLM. Use `global` while in beta/preview; `us-central1` for stable models. |
| `api_key` | API key path | Alternative to service account. Pass the API key directly (not recommended for production). |

---

#### Top-level agent params

| Parameter | Values | Default | Description |
|-----------|--------|---------|-------------|
| `mode` | `sql`, `text` | `text` | `sql` — return the final SQL as text, never execute it. `text` — execute the SQL and return data rows (or a plain-text answer if no SQL is needed). |
| `sql_dialect` | `db2`, `mysql`, `postgres`, absent | absent | When set, the final answer SQL is transformed to that dialect's syntax. `db2` converts `LIMIT N` → `FETCH FIRST N ROWS ONLY`, strips MindsDB connection prefixes, uppercases identifiers. Independent of `mode`. |
| `sql_cache_min_relevance` | float 0–1 | `0.7` | Minimum cosine similarity score for a cached SQL example to be injected into the planning prompt. Raise to inject only highly similar examples; lower to cast a wider net. |
| `sql_cache_max_examples` | integer | `3` | Maximum number of cached examples injected per query. More examples = better few-shot guidance but higher token cost. |
| `prompt_template` | string | built-in | System prompt injected at the start of every LLM call. Use to give domain context: schema conventions, identifier casing, business rules. |

---

#### `data` block

| Parameter | Description |
|-----------|-------------|
| `tables` | List of 3-part table names (`<connection>.<schema>.<table>`) the agent is allowed to query. The agent can only write SQL against these tables — it never invents table names. Use wildcard: `"my_db2_connection.myschema.*"` to include all tables in a schema. |
| `knowledge_bases` | List of KBs the agent can use. Format: `"<project>.<kb_name>"`. Schema KBs are pre-fetched for column context; SQL cache KBs (with `kb_type='sql_cache'`) are used for few-shot injection only. |

---

### Updating an Agent

```sql
-- Change the model or add a new table
UPDATE AGENT my_db2_agent
SET params = {
    "model": {
        "provider":   "google",
        "model_name": "gemini-2.5-pro",
        "project":    "<your-gcp-project>",
        "location":   "us-central1"
    },
    "mode": "sql",
    "sql_dialect": "db2",
    "data": {
        "tables": [
            "my_db2_connection.myschema.dim_hierarchy",
            "my_db2_connection.myschema.fact_project",
            "my_db2_connection.myschema.fact_execution",
            "my_db2_connection.myschema.fact_new_table"
        ],
        "knowledge_bases": [
            "mindsdb.my_columns_kb",
            "mindsdb.my_tables_kb",
            "mindsdb.my_sql_cache_kb"
        ]
    }
};
```

---

## 8. How to Query

Users query the agent using `SELECT` against the agent's name, passing the question in a `WHERE` clause:

```sql
SELECT answer
FROM my_db2_agent
WHERE question = 'How many projects are currently open?';
```

**With conversation history:**
```sql
SELECT answer
FROM my_db2_agent
WHERE question = 'Break that down by unit'
  AND context = '[{"role":"user","content":"How many open projects?"},
                  {"role":"assistant","content":"SELECT COUNT(*) ..."}]';
```

**From application code (Python via MindsDB REST API):**
```python
import requests

response = requests.post(
    "http://localhost:47334/api/agents/my_db2_agent/completions",
    json={"messages": [{"role": "user", "content": "How many projects are open?"}]}
)
print(response.json())
```

---

## 9. Query Processing Flow (Step by Step)

When a user sends a question, the following sequence executes inside the `PydanticAIAgent`:

### Step 1 — Initialization (agent startup / first request)

When the agent is first loaded:
- `tables_list` and `knowledge_bases_list` are read from `data.tables` and `data.knowledge_bases`.
- The SQL toolkit (`MindsDBQuery`) is built — this wraps the executor that will run queries against your DB2 connection.
- Each KB in the list is inspected: if any has `kb_type='sql_cache'` stored in its params, it is registered as `sql_cache_kb` automatically. No extra agent parameter needed.

```
[LOG] sql_cache_kb auto-detected via kb_type: 'my_sql_cache_kb'
[LOG] [SQL CACHE] sql_cache_kb='my_sql_cache_kb'
```

---

### Step 2 — Parallel: Data Catalog + KB Pre-fetch

Two operations run concurrently to save 3–5 seconds:

**Data Catalog (Thread 1):**
For each table in `data.tables`:
- `SELECT * FROM table LIMIT 3` — fetches 3 sample rows to show the LLM what the data looks like
- `SHOW COLUMNS FROM table` — fetches column names and types
- The SQL cache KB is **excluded** from this catalog

The catalog is formatted as:
```
=== TABLES CATALOG ===

--- Table: my_db2_connection.myschema.fact_project ---
Sample Data Query: SELECT * FROM my_db2_connection.myschema.fact_project LIMIT 3
Sample Data (csv):
PROJECT_SKEY,PROJECT_NAME,STATUS_CODE,COMPL_DATE_SKEY,...
1001,Alpha,A,20991231,...

Metadata (csv):
Field,Type,...
PROJECT_SKEY,INTEGER,...
```

**KB Pre-fetch (Thread 2):**
For each schema KB (non-`sql_cache` type):
```sql
SELECT * FROM my_columns_kb
WHERE content LIKE '<user question>'
LIMIT 5
```
Results are trimmed to 300 chars/chunk, capped at 3,000 chars total, and appended to the planning prompt as:
```
=== Knowledge Base Context (schema / column info) ===
--- my_columns_kb ---
COMPL_DATE_SKEY: Completion date surrogate key. 20991231 means open/not completed.
...
```

---

### Step 3 — SQL Cache Lookup

```sql
SELECT * FROM my_sql_cache_kb
WHERE content LIKE '<user question>'
  AND relevance >= 0.7
  AND tenant_id = '<current_tenant>'
LIMIT 3
```

If matches are found, they are appended to the planning prompt as:
```
=== Past Similar SQL Queries (REFERENCE ONLY — DO NOT COPY VERBATIM) ===
Adapt these for the current question. Verify column names against the data catalog.

Question: How many open projects are there?
SQL:
SELECT COUNT(*) FROM my_db2_connection.myschema.fact_project
WHERE COMPL_DATE_SKEY = 20991231
```

---

### Step 4 — Planning Phase (LLM Call 1)

The agent sends a single LLM call with:
- The full data catalog (sample rows + column metadata)
- KB pre-fetch context (relevant schema snippets)
- SQL cache examples (if any)
- The user's question

The LLM returns a `PlanResponse`:
```json
{
  "plan": "1. Query fact_project to count records where COMPL_DATE_SKEY = 20991231\n2. Return the count",
  "estimated_steps": 1
}
```

Token usage is logged:
```
[TOKEN USAGE] planning: request=4821, response=143, total=4964
```

---

### Step 5 — Execution Loop (LLM Calls 2..N, max 20 iterations)

The agent sends the execution LLM call with:
- The same data catalog
- The approved plan from Step 4
- DB2 dialect rules (telling the LLM to write MindsDB SQL — the system converts to DB2 on the way out)
- Knowledge base query rules (don't query KBs just for schema; always use WHERE content LIKE)

The LLM responds with an `AgentResponse`:
```json
{
  "sql_query": "SELECT COUNT(*) AS open_count FROM my_db2_connection.myschema.fact_project WHERE COMPL_DATE_SKEY = 20991231",
  "type": "final_query",
  "short_description": "Count open projects"
}
```

**Response type handling:**

| `type` value | What happens |
|-------------|-------------|
| `exploratory_query` | MindsDB executes the SQL, formats the result as a markdown table (capped at 3,000 chars), appends it to `exploratory_query_results`, increments the loop counter, and calls the LLM again with the new context. |
| `final_query` + `mode='sql'` | SQL is converted to DB2 dialect → returned as text to the user. SQL cache staging triggered. |
| `final_query` + `mode='text'` | SQL is executed against DB2 → data rows returned to the user. SQL cache staging triggered. |
| `final_text` | Plain-text answer returned (no SQL). Only available in `mode='text'`. |

**0-row safety net:** If the final query returns 0 rows and there is still exploratory budget remaining, the empty result is fed back with a hint about surrogate key conventions, and the LLM gets another chance to revise its filters.

---

### Step 6 — Dialect Conversion (mode='sql' + sql_dialect='db2')

The MindsDB SQL generated by the LLM is converted to DB2 SQL:

```
Input (MindsDB):
SELECT COUNT(*) FROM my_db2_connection.myschema.fact_project
WHERE COMPL_DATE_SKEY = 20991231
LIMIT 1

Output (DB2):
SELECT COUNT(*) FROM MYSCHEMA.FACT_PROJECT
WHERE COMPL_DATE_SKEY = 20991231
FETCH FIRST 1 ROWS ONLY
```

Transformations applied:
1. Strip MindsDB connection name prefix (`my_db2_connection.`) 
2. Remove backtick quoting around identifiers
3. Uppercase all identifiers
4. Convert `LIMIT N` → `FETCH FIRST N ROWS ONLY`

---

### Step 7 — SQL Cache Staging (background thread)

After returning the answer, a daemon thread writes to `<mindsdb-root>/sql_cache_staging.jsonl`:
```json
{"id":"a1b2c3d4","question":"How many open projects?","sql_query":"SELECT COUNT(*)...","dialect":"mindsdb","tenant_id":"1","user_id":"42","created_at":"2026-05-10T09:00:00+00:00"}
```

The log also contains a ready-to-run INSERT for manual approval:
```
===== SQL CACHE: copy/paste the INSERT below to approve this query =====
INSERT INTO my_sql_cache_kb (content, sql_query, dialect, tenant_id, user_id, created_at)
VALUES ('How many open projects?', 'SELECT COUNT(*) ...', 'mindsdb', '1', '42', '2026-05-10T09:00:00+00:00');
===== END SQL CACHE INSERT =====
```

The MindsDB SQL (not the DB2 SQL) is stored in the cache. This ensures cache entries work regardless of whether the agent is in `mode='sql'` or `mode='text'` in the future.

---

## 10. SQL Cache Workflow

```
1. Run a query → agent returns DB2 SQL (or data rows)
2. Review the JSONL staging file or the log INSERT
3. If the SQL is correct, run the INSERT to approve it:

   INSERT INTO my_sql_cache_kb
     (content, sql_query, dialect, tenant_id, user_id, created_at)
   VALUES
     ('<the question>', '<the mindsdb sql>', 'mindsdb', '<tenant>', '<user>', '<iso_timestamp>');

4. Future similar questions will find this entry
   → injected as a few-shot example in the planning prompt
   → fewer exploratory iterations needed
   → lower token usage
```

**Controlling injection quality:**
```sql
-- Require tighter similarity before injecting (default 0.7)
UPDATE AGENT my_db2_agent
SET params = { ..., "sql_cache_min_relevance": 0.85 };

-- Inject fewer examples (default 3)
UPDATE AGENT my_db2_agent
SET params = { ..., "sql_cache_max_examples": 1 };
```

---

## 11. Complete Setup — Example

Below is the full creation sequence with all sensitive values replaced by placeholders.

```sql
-- ============================================================
-- STEP 1: Database connection to DB2
-- ============================================================
CREATE DATABASE my_db2_connection
WITH ENGINE = 'db2',
PARAMETERS = {
    "user":             "<db_user>",
    "password":         "<your_password>",
    "host":             "<your_db2_host>",
    "port":             "50010",
    "database":         "<your_database>",
    "jdbc_driver_path": "<path_to_db2jcc4.jar>"
};

-- ============================================================
-- STEP 2: ChromaDB vector store
-- ============================================================
CREATE DATABASE my_chroma_store
WITH ENGINE = 'chromadb',
PARAMETERS = {
    "persist_directory": "chroma_kb_storage"
};

-- ============================================================
-- STEP 3a: Tables KB — embed table descriptions
-- ============================================================
CREATE KNOWLEDGE BASE my_tables_kb
USING
    storage        = my_chroma_store.tables_collection,
    embedding_model = {
        "provider":   "vertex_ai",
        "model_name": "gemini-embedding-001",
        "project":    "<your-gcp-project>",
        "location":   "us-central1"
    },
    metadata_columns = ['table_name'];

-- ============================================================
-- STEP 3b: Columns KB — embed column descriptions
-- ============================================================
CREATE KNOWLEDGE BASE my_columns_kb
USING
    storage        = my_chroma_store.columns_collection,
    embedding_model = {
        "provider":   "vertex_ai",
        "model_name": "gemini-embedding-001",
        "project":    "<your-gcp-project>",
        "location":   "us-central1"
    },
    metadata_columns = ['column_name', 'table_name'];

-- ============================================================
-- STEP 3c: SQL Cache KB — stores approved (question → SQL) pairs
-- ============================================================
CREATE KNOWLEDGE BASE my_sql_cache_kb
USING
    storage        = my_chroma_store.sql_collection,
    embedding_model = {
        "provider":   "vertex_ai",
        "model_name": "gemini-embedding-001",
        "project":    "<your-gcp-project>",
        "location":   "us-central1"
    },
    kb_type          = 'sql_cache',
    metadata_columns = ['sql_query', 'dialect', 'tenant_id', 'user_id', 'created_at'];

-- ============================================================
-- STEP 4: Populate the KBs with table and column descriptions
-- (Run this for each table and column you want the agent to understand)
-- ============================================================
INSERT INTO my_tables_kb (content, table_name)
VALUES ('Stores project-level information: name, status, owner unit, planned and actual dates', 'FACT_PROJECT');

INSERT INTO my_columns_kb (content, column_name, table_name)
VALUES ('Completion date surrogate key. 20991231 = record is still open/not yet completed.', 'COMPL_DATE_SKEY', 'FACT_PROJECT');

-- ============================================================
-- STEP 5: Create the agent
-- ============================================================
CREATE AGENT my_db2_agent
USING
    model = {
        "provider":   "google",
        "model_name": "gemini-3.1-pro-preview",
        "project":    "<your-gcp-project>",
        "location":   "global"
    },
    mode        = 'sql',
    sql_dialect = 'db2',
    data = {
        "tables": [
            "my_db2_connection.myschema.dim_hierarchy",
            "my_db2_connection.myschema.fact_project",
            "my_db2_connection.myschema.fact_execution"
        ],
        "knowledge_bases": [
            "mindsdb.my_columns_kb",
            "mindsdb.my_tables_kb",
            "mindsdb.my_sql_cache_kb"
        ]
    },
    prompt_template = 'You are an expert SQL analyst. All identifiers are uppercase. Use FETCH FIRST N ROWS ONLY. Surrogate keys ending in _skey with value 20991231 mean the record is still open.';

-- ============================================================
-- STEP 6: Run a query
-- ============================================================
SELECT answer
FROM my_db2_agent
WHERE question = 'How many projects are currently open?';

-- ============================================================
-- STEP 7: Approve a staged SQL entry into the cache
-- (copy from the log file after reviewing sql_cache_staging.jsonl)
-- ============================================================
INSERT INTO my_sql_cache_kb (content, sql_query, dialect, tenant_id, user_id, created_at)
VALUES (
    'How many projects are currently open?',
    'SELECT COUNT(*) AS open_count FROM my_db2_connection.myschema.fact_project WHERE COMPL_DATE_SKEY = 20991231',
    'mindsdb',
    '<tenant_id>',
    '<user_id>',
    '2026-05-10T09:00:00+00:00'
);
```

---

## Key Concepts Summary

| Concept | Where set | What it controls |
|---------|-----------|-----------------|
| `mode = 'sql'` | Agent USING | Returns SQL text instead of executing it |
| `mode = 'text'` | Agent USING | Executes SQL, returns data rows |
| `sql_dialect = 'db2'` | Agent USING | Transforms MindsDB SQL → DB2 SQL in the response |
| `kb_type = 'sql_cache'` | KB USING | Marks a KB as the SQL pair store; auto-detected by agent |
| `sql_cache_min_relevance` | Agent USING | Min similarity threshold for cache injection (default 0.7) |
| `sql_cache_max_examples` | Agent USING | Max examples from cache per query (default 3) |
| 3-part table name | `data.tables` | `<connection>.<schema>.<table>` — only these tables are queryable |
| `prompt_template` | Agent USING | System-level domain instructions for the LLM |
| ChromaDB collection | KB USING `storage` | Unique namespace within the chroma store; use a new name when recreating to get a clean KB |
