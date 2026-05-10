# MindsDB Agent — Query Flow & Technical Workflow

How a user's natural-language question becomes a DB2 SQL query.  
All timestamps and token counts are taken from the actual log file `mindsdb_2026-05-10_14-57-58.log`.

---

## Table of Contents

1. [High-Level Flow (One Page)](#1-high-level-flow-one-page)
2. [Detailed Phase-by-Phase Walkthrough](#2-detailed-phase-by-phase-walkthrough)
   - [Phase 0 — Agent Initialization](#phase-0--agent-initialization)
   - [Phase 1 — Parallel Preparation](#phase-1--parallel-preparation)
   - [Phase 2 — Planning LLM Call](#phase-2--planning-llm-call)
   - [Phase 3 — Execution Loop](#phase-3--execution-loop)
   - [Phase 4 — Dialect Conversion & Response](#phase-4--dialect-conversion--response)
   - [Phase 5 — SQL Cache Staging (Background)](#phase-5--sql-cache-staging-background)
3. [What the LLM Can Do Inside the Loop](#3-what-the-llm-can-do-inside-the-loop)
4. [SQL Cache — Impact on Speed & Tokens](#4-sql-cache--impact-on-speed--tokens)
5. [What Flows Into the LLM Prompt](#5-what-flows-into-the-llm-prompt)
6. [Token Cost Breakdown (Real Numbers)](#6-token-cost-breakdown-real-numbers)
7. [Error Handling & Safety Nets](#7-error-handling--safety-nets)
8. [Sequence Diagrams](#8-sequence-diagrams)

---

## 1. High-Level Flow (One Page)

```
User types a question
        │
        ▼
  HTTP POST /api/agents/my_agent/completions
        │
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  PHASE 0: Agent Boot (runs once per HTTP request)          │
  │  • Read agent params (mode, sql_dialect, data.tables, KBs) │
  │  • Build SQL toolkit pointing to DB2 connection            │
  │  • Scan each KB → find kb_type='sql_cache' → auto-detect  │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  PHASE 1: Parallel Preparation (~3 seconds)                │
  │                                                             │
  │   Thread A                      Thread B                   │
  │   ─────────────────             ─────────────────          │
  │   Build Data Catalog            KB Pre-fetch               │
  │   • SELECT * LIMIT 3            • Search schema KBs with   │
  │     per table (sample rows)       the user's question      │
  │   • SHOW COLUMNS per table      • Returns column/table     │
  │   • SQL cache KB excluded         descriptions relevant    │
  │     from catalog                  to the question          │
  │                                                             │
  │   Thread C (after A & B)                                   │
  │   ─────────────────                                        │
  │   SQL Cache Lookup                                         │
  │   • Search sql_cache KB for similar past questions         │
  │   • Returns (question → SQL) pairs as few-shot examples    │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  PHASE 2: Planning LLM Call (one call, ~8 seconds)         │
  │                                                             │
  │  Input:                                                     │
  │    • Data catalog (sample rows + column schema)            │
  │    • KB pre-fetch context (relevant column descriptions)   │
  │    • SQL cache examples (past similar queries)             │
  │    • User's question                                       │
  │                                                             │
  │  Output:  PlanResponse                                     │
  │    • plan: "Step 1: Query X to find Y. Step 2: ..."        │
  │    • estimated_steps: 1                                    │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  PHASE 3: Execution Loop (max 20 iterations)               │
  │                                                             │
  │  Each iteration sends to LLM:                              │
  │    • Same data catalog + plan                              │
  │    • All previous query results (accumulated context)      │
  │    • DB2 dialect rules                                     │
  │                                                             │
  │  LLM responds with AgentResponse:                          │
  │    • type = "exploratory_query"  →  execute + loop again   │
  │    • type = "final_query"        →  done → Phase 4         │
  │    • type = "final_text"         →  plain text answer      │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼ (on final_query)
  ┌─────────────────────────────────────────────────────────────┐
  │  PHASE 4: Dialect Conversion + Response                    │
  │                                                             │
  │  mode='sql':                                               │
  │    MindsDB SQL  ──→  _to_db2_sql()  ──→  DB2 SQL           │
  │    Transformations: strip connection prefix, uppercase,    │
  │    remove backticks, LIMIT → FETCH FIRST N ROWS ONLY       │
  │    Returns: DB2 SQL text to user                           │
  │                                                             │
  │  mode='text':                                              │
  │    Execute MindsDB SQL → DB2 runs query → return data rows │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼ (in background, zero latency to user)
  ┌─────────────────────────────────────────────────────────────┐
  │  PHASE 5: SQL Cache Staging                                │
  │  • Write JSONL entry to sql_cache_staging.jsonl            │
  │  • Log ready-to-run INSERT command                         │
  │  • User reviews and approves → INSERT into cache KB        │
  └─────────────────────────────────────────────────────────────┘
```

---

## 2. Detailed Phase-by-Phase Walkthrough

---

### Phase 0 — Agent Initialization

Every HTTP request creates a fresh `PydanticAIAgent` instance. This takes about 3 seconds.

**What happens:**

```
HTTP request received
        │
        ▼
Read agent params from database:
  mode          = 'sql'
  sql_dialect   = 'db2'
  data.tables   = ['conn.schema.table1', 'conn.schema.table2', ...]
  data.kbs      = ['mindsdb.columns_kb', 'mindsdb.tables_kb', 'mindsdb.sql_cache_kb']
        │
        ▼
Build MindsDBQuery (SQL toolkit)
  → wraps the MindsDB command executor
  → knows which tables and KBs are accessible
        │
        ▼
Scan each KB to detect roles:
  For each KB name in data.kbs:
    call session.kb_controller.get(kb_bare_name, project_id)
    read kb.params.get('kb_type')
    if 'sql_cache' → set self.sql_cache_kb = this kb
        │
        ▼
Log output:
  [SQL CACHE] sql_cache_kb auto-detected via kb_type: 'my_sql_cache_kb'
  [SQL CACHE] sql_cache_kb='my_sql_cache_kb'  agent_params_keys=[...]
```

**From the actual log:**
```
14:59:46  sql_cache_kb auto-detected via kb_type: 'aar_db2_sql_kb_knl01pt_v1'
14:59:46  [SQL CACHE] sql_cache_kb='aar_db2_sql_kb_knl01pt_v1'
```

---

### Phase 1 — Parallel Preparation

The most token-expensive phase. Runs before any LLM call.

#### Part A — Data Catalog (Thread 1)

MindsDB queries DB2 directly to build the catalog that tells the LLM about table structure.

```
For each table in data.tables:
    ┌────────────────────────────────────────────┐
    │  SELECT * FROM conn.schema.table LIMIT 3   │  → 3 sample rows (real data)
    └────────────────────────────────────────────┘
    ┌────────────────────────────────────────────┐
    │  SHOW COLUMNS FROM conn.schema.table       │  → column names + types
    └────────────────────────────────────────────┘

Result (catalog entry for one table):
  --- Table: conn.schema.fact_project ---
  Sample Data Query: SELECT * FROM conn.schema.fact_project LIMIT 3
  Sample Data (csv):
    PROJECT_SKEY,PROJECT_NAME,STATUS_CODE,...
    1001,Alpha,A,...
    1002,Beta,C,...
  Metadata (csv):
    Field,Type,...
    PROJECT_SKEY,INTEGER,...
    PROJECT_NAME,VARCHAR(255),...
```

> The SQL cache KB is **excluded** from the catalog. It stores SQL pairs, not schema info — if included, the LLM would see SQL fragments instead of table structure.

#### Part B — KB Pre-fetch (Thread 2, runs in parallel with Thread 1)

Asks the schema KBs: *"what do you know that's relevant to this question?"*

```
For each schema KB (not sql_cache type):
    SELECT * FROM my_columns_kb
    WHERE content LIKE '<user question>'
    LIMIT 5

ChromaDB converts the question to a vector, finds nearest neighbours.
Results are filtered to:
  → Only the chunk_content column (skip metadata columns)
  → Max 300 chars per chunk
  → Max 3,000 chars total across all KBs

Injected into planning prompt as:
  === Knowledge Base Context (schema / column info) ===
  --- my_columns_kb ---
  COMPL_DATE_SKEY: Completion date surrogate key.
  20991231 = open/not yet completed...
```

**From the actual log:**
```
14:59:49  KB pre-fetch injected 2042 chars into planning prompt
```

#### Part C — SQL Cache Lookup (after threads complete)

Searches for past approved queries that match the current question.

```
SELECT * FROM my_sql_cache_kb
WHERE content LIKE '<user question>'
  AND relevance >= 0.7
  AND tenant_id  = '<current_tenant>'
LIMIT 3

If hits found:
  For each hit, read chunk_content (the stored question) + sql_query metadata
  Build injection block:

  === Past Similar SQL Queries (REFERENCE ONLY) ===
  Adapt these for the current question. Verify column names.

  Question: How many open projects?
  SQL:
  SELECT COUNT(*) FROM conn.schema.fact_project
  WHERE COMPL_DATE_SKEY = 20991231
```

**From the actual log (Query 1 — cache hit):**
```
14:59:51  SQL cache: 1 similar example(s) found for prompt injection
14:59:51  SQL cache: injected 780 chars of examples into planning prompt
```

**From the actual log (Query 3 — no cache hit):**
```
  [no SQL cache log line] → 0 examples found → 19 execution loops needed
```

---

### Phase 2 — Planning LLM Call

A dedicated LLM call that generates a step-by-step execution plan **before** any SQL is written. This prevents the LLM from jumping straight into SQL with wrong assumptions.

```
Input prompt assembled from:
  ┌─────────────────────────────────────────────────────────┐
  │  Data Catalog                     (from Phase 1A)       │
  │  KB Pre-fetch Context             (from Phase 1B)       │
  │  SQL Cache Examples               (from Phase 1C)       │
  │  User's Question                                        │
  │  Planning instructions                                  │
  └─────────────────────────────────────────────────────────┘
              │
              ▼ LLM Call (Planning Agent)
  ┌─────────────────────────────────────────────────────────┐
  │  Output: PlanResponse (structured JSON)                 │
  │    {                                                    │
  │      "plan": "Step 1: Query fact_project WHERE          │
  │               COMPL_DATE_SKEY = 20991231 to count       │
  │               open projects.                           │
  │               Step 2: Return the count.",              │
  │      "estimated_steps": 1                              │
  │    }                                                   │
  └─────────────────────────────────────────────────────────┘
```

**From the actual log:**
```
14:59:59  [TOKEN USAGE] planning: request=12,416  response=134  total=13,055
          details={thoughts_tokens: 505, text_candidates_tokens: 134}
```

Breaking this down:
- **12,416 request tokens** = data catalog + KB context + SQL examples + question
- **505 thoughts tokens** = LLM's internal reasoning (not returned to user)
- **134 response tokens** = the actual plan text

The plan is shown to the user as a status message and then included in the execution loop prompt.

---

### Phase 3 — Execution Loop

The main reasoning loop. The LLM writes SQL, MindsDB runs it, the result is fed back, and the LLM writes the next SQL. Repeats until the LLM signals it has the final answer.

```
┌──────────────────────────────────────────────────────────────┐
│  Build Execution Prompt:                                     │
│    • Data Catalog (same as planning)                         │
│    • Plan from Phase 2                                       │
│    • DB2 dialect rules ("write MindsDB SQL, system converts")│
│    • KB query rules                                          │
│    • User's question                                         │
│    • [Accumulated] previous query results                    │
└──────────────────────────────────────────────────────────────┘
              │
              ▼ LLM Call
┌──────────────────────────────────────────────────────────────┐
│  Output: AgentResponse                                       │
│    {                                                         │
│      "sql_query": "SELECT COUNT(*) AS cnt FROM ...",         │
│      "type": "exploratory_query" | "final_query",           │
│      "short_description": "Count open projects"             │
│    }                                                         │
└──────────────────────────────────────────────────────────────┘
              │
    ┌─────────┴──────────────┐
    │                        │
    ▼                        ▼
exploratory_query         final_query
    │                        │
    ▼                        └──→ Phase 4
MindsDB executes SQL
against DB2 connection
    │
    ▼
Result → markdown table
(truncated to 3,000 chars)
    │
    ▼
Append to context:
  "Query: SELECT ...
   Result:
   | CNT |
   | 42  |"
    │
    ▼
Loop counter + 1
(max 20 loops)
    │
    ▼
Back to top of loop
(next LLM call with richer context)
```

**From the actual log (Query 1 — 1 loop):**
```
15:00:11  [TOKEN USAGE] loop#0: request=11,668  response=206  total=12,888
15:00:11  return_sql_only=True (dialect='db2'): returning SQL as answer
```
One loop → immediate final_query. Total time: **25 seconds**.

**From the actual log (Query 3 — 19 loops):**
```
15:15:55  [TOKEN USAGE] loop#0:  request=11,795  total=12,907
15:16:06  [TOKEN USAGE] loop#1:  request=12,025  total=12,335
15:16:13  [TOKEN USAGE] loop#2:  request=12,438  total=12,746
15:16:19  [TOKEN USAGE] loop#3:  request=12,838  total=13,099
15:16:29  [TOKEN USAGE] loop#4:  request=12,959  total=13,776
... (14 more loops)
15:21:16  [TOKEN USAGE] loop#18: request=16,348  total=18,092
15:21:16  return_sql_only=True: returning SQL as answer
```
19 loops. Total time: **~6 minutes**.

Notice how the request token count grows each loop — every previous query result is appended to the prompt, so each LLM call gets longer.

---

### Phase 4 — Dialect Conversion & Response

Once the LLM signals `type = "final_query"`:

```
sql_query (MindsDB format — what LLM generated):
  SELECT COUNT(*) AS open_count
  FROM my_db2_connection.myschema.fact_project
  WHERE COMPL_DATE_SKEY = 20991231
  LIMIT 1

                    │
                    ▼  _to_db2_sql() transformation pipeline

  Step 1: Strip MindsDB connection prefix
    my_db2_connection.myschema.fact_project → myschema.fact_project

  Step 2: Remove backticks from schema.table pairs
    `myschema`.`fact_project` → MYSCHEMA.FACT_PROJECT

  Step 3: Uppercase remaining identifiers
    open_count → OPEN_COUNT

  Step 4: Convert LIMIT → FETCH FIRST
    LIMIT 1 → FETCH FIRST 1 ROWS ONLY
                    │
                    ▼

final_sql (DB2 format — returned to user):
  SELECT COUNT(*) AS OPEN_COUNT
  FROM MYSCHEMA.FACT_PROJECT
  WHERE COMPL_DATE_SKEY = 20991231
  FETCH FIRST 1 ROWS ONLY
```

The response also appends a comment block with the cache approve command:
```sql
-- ===== To save this query for future use, run the INSERT below: =====
-- INSERT INTO my_sql_cache_kb (content, sql_query, ...) VALUES (...);
```

---

### Phase 5 — SQL Cache Staging (Background)

Runs in a daemon thread — the response has already been sent to the user, so there is **zero added latency**.

```
Background thread:
    1. Build JSONL entry:
       {
         "id": "de6900fc",
         "question": "How many open projects?",
         "sql_query": "SELECT COUNT(*) AS open_count FROM ...",  ← MindsDB SQL, not DB2!
         "dialect": "mindsdb",
         "tenant_id": "1",
         "user_id": "42",
         "created_at": "2026-05-10T14:59:46+00:00"
       }

    2. Append to: <mindsdb-root>/sql_cache_staging.jsonl

    3. Log the ready-to-run INSERT:
       ===== SQL CACHE: copy/paste INSERT below to approve =====
       INSERT INTO my_sql_cache_kb (content, sql_query, dialect, ...)
       VALUES ('How many open projects?', 'SELECT COUNT(*) ...', 'mindsdb', ...);
       ===== END SQL CACHE INSERT =====
```

**From the actual log:**
```
15:00:11  SQL cache: launching stage thread for kb='...', question_len=200, sql_len=364
15:00:11  SQL cache staged [de6900fc]: 'My distict name is ...'
```

> **Why MindsDB SQL, not DB2 SQL?**  
> The cache stores the MindsDB format because it must work regardless of future agent configuration.  
> If someone later changes `mode` from `sql` to `text`, MindsDB needs to execute the SQL itself — and DB2 syntax (`FETCH FIRST`, 2-part names) would fail.  
> The `_to_db2_sql()` conversion always runs on the way out, so the cache entry is always reusable.

---

## 3. What the LLM Can Do Inside the Loop

The LLM is not limited to querying DB2 tables. It has access to everything in `data.tables` and `data.knowledge_bases`.

```
Available data sources:

  DB2 Tables:
    ├─ conn.schema.table1    ← business data
    ├─ conn.schema.table2    ← business data
    └─ conn.schema.table3    ← business data

  Schema KBs:
    ├─ mindsdb.my_columns_kb  ← column descriptions (semantic search)
    └─ mindsdb.my_tables_kb   ← table descriptions (semantic search)

  SQL Cache KB:
    └─ mindsdb.my_sql_cache_kb  ← NOT visible to LLM (excluded from catalog)
                                   Only used internally for Phase 1C
```

**From Query 3's execution loop (log evidence), the LLM issued these exploratory queries:**

```
Loop 0:   SELECT PROJ_ATTR_SKEY, CNT FROM fact_project...
            → 10 rows returned (DB2 table query)

Loop 1:   SELECT * FROM my_columns_kb WHERE content LIKE '...'
            → 10 rows returned (KB semantic search)

Loop 2:   SELECT column_name, chunk_content FROM my_columns_kb WHERE...
            → 10 rows returned (focused KB query)

Loop 3:   SELECT column_name, chunk_content WHERE... (refined)
            → 1 row returned

Loop 4:   SELECT PROJ_ATTR_SKEY, PROJ_CATEGORY, CNT FROM fact_project...
            → 20 rows returned (DB2 table, refined query)

Loop 5:   SELECT column_name, chunk_content FROM my_columns_kb WHERE...
            → 10 rows returned (KB, different search term)
...
Loop 18:  SELECT DISTINCT ... (final_query)
            → DB2 SQL returned to user
```

The LLM autonomously decides to query KBs when it needs more column information and queries DB2 tables to explore data patterns. It accumulates this knowledge across loops until it is confident enough to write the final SQL.

---

## 4. SQL Cache — Impact on Speed & Tokens

Real data from three consecutive queries in the same log session:

```
Query 1: "Find distinct projects for unit 510055 under domain 1111100"
  ┌─────────────────────────────────────────────────────────┐
  │  Cache hit: 1 example found (relevance ≥ 0.7)          │
  │  Planning:  13,055 tokens                               │
  │  Loop#0:    12,888 tokens  →  FINAL ANSWER              │
  │  Total:    ~25,943 tokens                               │
  │  Time:      25 seconds                                  │
  └─────────────────────────────────────────────────────────┘

Query 2: "Find distinct projects for unit 510056 under domain 1111100"
  ┌─────────────────────────────────────────────────────────┐
  │  Cache hit: 1 example found                             │
  │  Planning:  13,216 tokens                               │
  │  Loop#0:    13,027 tokens  →  FINAL ANSWER              │
  │  Total:    ~26,243 tokens                               │
  │  Time:      26 seconds                                  │
  └─────────────────────────────────────────────────────────┘

Query 3: "Find distinct projects for unit 510056 under domain 1111100" (different filter)
  ┌─────────────────────────────────────────────────────────┐
  │  Cache hit: 0 examples found                            │
  │  Planning:  13,622 tokens                               │
  │  Loop#0:    12,907 tokens                               │
  │  Loop#1:    12,335 tokens                               │
  │  ...                                                    │
  │  Loop#18:   18,092 tokens  →  FINAL ANSWER              │
  │  Total:    ~295,000 tokens (estimated across 19 loops)  │
  │  Time:      ~6 minutes                                  │
  └─────────────────────────────────────────────────────────┘
```

```
Comparison:
  With SQL cache:     1 loop   ~26K tokens    25 seconds
  Without SQL cache:  19 loops ~295K tokens   6 minutes

  Speed improvement:  ~14x faster
  Token reduction:    ~91% fewer tokens
```

The cache doesn't change the correctness of the answer — the LLM still writes fresh SQL. The few-shot examples just show it the correct join patterns, column naming conventions, and filter logic, so it reaches the answer in one shot instead of exploring for 6 minutes.

---

## 5. What Flows Into the LLM Prompt

Here is what the planning LLM receives, in order, for a typical query:

```
╔══════════════════════════════════════════════════════════════╗
║  SYSTEM PROMPT                                               ║
║  "You are an expert IBM DB2 SQL analyst..."                  ║
║  (from prompt_template param on the agent)                   ║
╠══════════════════════════════════════════════════════════════╣
║  DATA CATALOG                                   ~8,000 chars ║
║                                                              ║
║  === TABLES CATALOG ===                                      ║
║  --- Table: conn.schema.fact_project ---                     ║
║  Sample Data (csv):                                          ║
║    PROJECT_SKEY,PROJECT_NAME,...                             ║
║    1001,Alpha,...                                            ║
║  Metadata (csv):                                             ║
║    Field,Type                                                ║
║    PROJECT_SKEY,INTEGER                                      ║
║    PROJECT_NAME,VARCHAR(255)                                 ║
║    COMPL_DATE_SKEY,INTEGER                                   ║
║    ...                                                       ║
║                                                              ║
║  (repeated for each table in data.tables)                    ║
╠══════════════════════════════════════════════════════════════╣
║  KB CONTEXT (schema/column info)                ~2,042 chars ║
║                                                              ║
║  === Knowledge Base Context ===                              ║
║  --- my_columns_kb ---                                       ║
║  COMPL_DATE_SKEY: Completion date surrogate key.             ║
║  20991231 = record is still open (not yet completed).        ║
║  Status column: A=Active, C=Complete, X=Cancelled...         ║
╠══════════════════════════════════════════════════════════════╣
║  SQL CACHE EXAMPLES (if cache hit)                ~780 chars ║
║                                                              ║
║  === Past Similar SQL Queries (REFERENCE ONLY) ===           ║
║  Question: How many open projects?                           ║
║  SQL:                                                        ║
║  SELECT COUNT(*) AS open_count                               ║
║    FROM conn.schema.fact_project                             ║
║    WHERE COMPL_DATE_SKEY = 20991231                          ║
╠══════════════════════════════════════════════════════════════╣
║  PLANNING INSTRUCTIONS                            ~500 chars ║
║  "Generate a step-by-step plan for answering..."             ║
╠══════════════════════════════════════════════════════════════╣
║  USER QUESTION                                               ║
║  "How many projects are currently open?"                     ║
╚══════════════════════════════════════════════════════════════╝

Total planning prompt: ~12,416 tokens (from log)
```

For the **execution loop**, the same content is included, plus:
- The plan from the planning call
- DB2 dialect rules (write MindsDB SQL, system converts)
- KB query rules (don't query KBs for schema; always use WHERE content LIKE)
- After each iteration: accumulated query results

---

## 6. Token Cost Breakdown (Real Numbers)

From the first query in the log session:

```
Phase         │ Request  │ Response │ Thoughts │ Total   │ Purpose
──────────────┼──────────┼──────────┼──────────┼─────────┼──────────────────────────
Planning      │  12,416  │     134  │     505  │  13,055 │ Generate execution plan
Execution #0  │  11,668  │     206  │   1,014  │  12,888 │ Write final SQL query
──────────────┼──────────┼──────────┼──────────┼─────────┼──────────────────────────
TOTAL         │          │          │          │  25,943 │
```

**What each component costs:**
```
Data Catalog:         ~8,000 tokens   (sample rows + SHOW COLUMNS × tables)
KB Pre-fetch context:   ~500 tokens   (2,042 chars ÷ ~4 chars/token)
SQL Cache examples:     ~200 tokens   (780 chars ÷ ~4 chars/token)
Planning instructions:  ~400 tokens
User question:           ~50 tokens
Thoughts (LLM):       ~1,500 tokens   (internal reasoning, billed but not shown)
Plan + response:         ~340 tokens
```

---

## 7. Error Handling & Safety Nets

### 7a — SQL Syntax Errors

If MindsDB cannot execute the SQL the LLM generated:
```
LLM generates SQL
        │
        ▼
MindsDB executes against DB2
        │
        ▼ (error)
Error message appended to context:
  "Query: SELECT ... ERROR: SQLCODE=-206, column not found"
        │
        ▼
Retry counter + 1 (max 3 retries per loop)
        │
        ▼
LLM sees error → writes corrected SQL
```

### 7b — Empty Result (0 Rows)

If the final SQL runs successfully but returns 0 rows:
```
final_query → 0 rows returned
        │
        ▼ (if exploratory budget remains)
Fed back as exploratory with hint:
  "IMPORTANT: This query returned no results.
   Hint: date surrogate keys with value 20991231 mean
   'not yet completed / open record'.
   To find open records, filter for = 20991231;
   to find completed records, filter for < 20991231."
        │
        ▼
LLM revises filter logic
```

This prevents the user from receiving an empty or null answer without any explanation.

### 7c — Runaway Loops

```
MAX_EXPLORATORY_QUERIES = 20

If loop counter reaches 20:
  → Agent stops regardless of LLM output type
  → Returns whatever result it has (or an error message)
```

### 7d — Oversized Query Results

```
Exploratory result > 3,000 chars:
  → Truncated with message:
    "... [truncated — 500 row(s), 102 column(s) total;
     use SELECT with specific columns for a focused result]"
  → Prevents context window from being consumed by wide table dumps
```

---

## 8. Sequence Diagrams

### Fast Query (SQL Cache Hit — 1 Loop)

```
User       MindsDB    DB2        ChromaDB    Gemini LLM
  │             │        │            │            │
  │─ question ─►│        │            │            │
  │             │        │            │            │
  │         ┌──────────────────────────┐           │
  │         │   PARALLEL (3 seconds)   │           │
  │         │  ┌────┐      ┌─────────┐ │           │
  │         │  │SHOW│      │KB search│ │           │
  │         │  │COLS│      │question │ │           │
  │         │  └──┬─┘      └────┬────┘ │           │
  │         │     │─ SHOW ─────►│DB2   │           │
  │         │     │◄─ columns ──│      │           │
  │         │     │             │─ vec ►│ChromaDB  │
  │         │     │             │◄─ ctx─┤           │
  │         └──────────────────────────┘           │
  │             │                                   │
  │             │─ vec search ─────────────────────►│ChromaDB
  │             │◄─ 1 example ──────────────────────┤
  │             │                                   │
  │             │─ planning prompt ─────────────────►│ Gemini
  │             │◄─ PlanResponse ────────────────────┤ (~8s)
  │             │                                   │
  │             │─ execution prompt ────────────────►│ Gemini
  │             │◄─ AgentResponse(final_query) ──────┤ (~12s)
  │             │                                   │
  │             │  _to_db2_sql() [local, instant]   │
  │             │                                   │
  │◄─ DB2 SQL ──│                                   │
  │             │                                   │
  │         [background] write staging JSONL        │
  │             │                                   │

Total: ~25 seconds
```

---

### Slow Query (No Cache Hit — Multiple Loops)

```
User       MindsDB    DB2        ChromaDB    Gemini LLM
  │             │        │            │            │
  │─ question ─►│        │            │            │
  │             │        │            │            │
  │         [parallel: catalog + KB prefetch, ~3s] │
  │             │                                   │
  │             │  [sql cache lookup → 0 hits]      │
  │             │                                   │
  │             │─ planning prompt ─────────────────►│ Gemini
  │             │◄─ PlanResponse ────────────────────┤ (~15s)
  │             │                                   │
  │   LOOP 0    │─ exec prompt ─────────────────────►│ Gemini
  │             │◄─ exploratory_query ───────────────┤ (~11s)
  │             │─ SELECT ... FROM fact_project ─────►│ DB2
  │             │◄─ 10 rows ──────────────────────────┤
  │             │                                   │
  │   LOOP 1    │─ exec prompt + loop0 result ──────►│ Gemini
  │             │◄─ exploratory_query (KB search) ───┤ (~7s)
  │             │─ WHERE content LIKE '...' ─────────►│ ChromaDB
  │             │◄─ column descriptions ──────────────┤
  │             │                                   │
  │   ... (loops 2-17: more DB2 + KB queries) ...  │
  │             │                                   │
  │   LOOP 18   │─ exec prompt (much larger) ───────►│ Gemini
  │             │◄─ final_query ──────────────────────┤ (~18s)
  │             │                                   │
  │             │  _to_db2_sql() [local, instant]   │
  │             │                                   │
  │◄─ DB2 SQL ──│                                   │
  │             │                                   │
  │         [background] write staging JSONL        │

Total: ~6 minutes, ~295K tokens
```

---

### SQL Cache Approval (after query)

```
Log file shows:
  ===== SQL CACHE: INSERT below to approve =====
  INSERT INTO my_sql_cache_kb
    (content, sql_query, dialect, tenant_id, user_id, created_at)
  VALUES ('...question...', '...mindsdb sql...', 'mindsdb', ...);
  ===== END SQL CACHE INSERT =====

User reviews sql_cache_staging.jsonl
        │
        ▼ (if SQL is correct)
User runs INSERT in MindsDB console

ChromaDB stores:
  content     = question text  ←── what gets embedded & searched
  sql_query   = MindsDB SQL    ←── returned as few-shot example
  dialect     = 'mindsdb'
  tenant_id   = ...
  created_at  = ...
        │
        ▼
Next similar question:
  Phase 1C finds this entry (relevance ≥ 0.7)
  Injected into planning prompt
  LLM jumps to final answer in 1 loop
```

---

## Summary

| What | Detail |
|------|--------|
| **Phases per query** | 5 (Init → Parallel Prep → Planning → Execution Loop → Response) |
| **LLM calls per query (best case)** | 2 (1 planning + 1 execution) |
| **LLM calls per query (worst case)** | 21 (1 planning + 20 execution loops) |
| **Parallel operations** | Data catalog build + KB pre-fetch run concurrently |
| **SQL cache benefit** | 14× faster, 91% fewer tokens when a matching example exists |
| **What LLM writes** | Always MindsDB SQL (3-part names, LIMIT N) |
| **What user receives** | DB2 SQL (FETCH FIRST, UPPERCASE, no connection prefix) |
| **Cache stores** | MindsDB SQL (works for both mode='sql' and mode='text') |
| **Max loops** | 20 (safety limit to prevent infinite loops) |
| **Context growth** | Each loop appends query results → prompt grows → costs more |
| **Staging** | Background thread, zero latency to user |
