# MindsDB Text-to-SQL Agent Setup Guide

Complete guide from database connection to natural language querying with internal process details.

## Overview

This guide shows how to set up MindsDB to query your IBM DB2 database using natural language through AI agents with Google Gemini (Vertex AI). The system automatically generates SQL queries from your questions and executes them against your database.

## Architecture Flow

```
Natural Language Question → Agent → Text2SQL Skill → Gemini LLM → Generated SQL → DB2 Database → Natural Language Answer
```

## Step 1: Database Connection Setup

### 1.1 DB2 JDBC Handler Installation

The DB2 JDBC handler bypasses SSL certificate issues by using JDBC drivers (similar to DBeaver).

**Internal Process:**
- Uses `jaydebeapi` to connect via JDBC instead of IBM's CLI drivers
- Automatically handles SSL/TLS certificates without GSKit configuration
- Falls back from CLI to JDBC in `db2_handler/__init__.py`

**Prerequisites:**
- IBM DB2 JDBC driver (`jcc-11.5.9.0.jar`)
- Java Runtime Environment

**Connection Setup:**
```sql
CREATE DATABASE rtmdb
WITH
  ENGINE = 'db2',
  PARAMETERS = {
    "host": "z182sd-rflxreferencedemo01.rfx.zebra.com",
    "port": 50010,
    "database": "rtmdb",
    "user": "your_username",
    "password": "your_password",
    "jdbc_driver_path": "C:\\path\\to\\jcc-11.5.9.0.jar"
  };
```

**Internal Flow:**
1. `DB2JDBCHandler.connect()` loads JDBC driver via `jaydebeapi`
2. Constructs JDBC URL: `jdbc:db2://host:port/database`
3. Establishes connection with automatic SSL handling
4. Returns connection status and schema information

## Step 2: ML Engine Configuration (Google Gemini + Vertex AI)

### 2.1 Service Account Setup

**Prerequisites:**
- Google Cloud service account with `cloud-platform` scope
- Service account JSON key file
- Vertex AI API enabled

**Engine Creation:**
```sql
CREATE ML_ENGINE gemini_enterprise_engine_v1
FROM google_gemini
USING
    service_account_json = 'C:\\Gourav\\Company\\Document\\GCP\\Credential\\vertex-ai.json';
```

**Internal Process (`google_gemini_handler.py`):**
1. `_get_service_account_credentials()` loads service account from:
   - Model creation args
   - Engine creation args  
   - `GOOGLE_APPLICATION_CREDENTIALS` env var
2. `_configure_genai()` creates `genai.Client(vertexai=True)` 
3. Sets up SSL certificates at module level to fix corporate proxy issues
4. Uses `google.auth.default` monkey-patching for credentials

### 2.2 Model Creation

```sql
CREATE MODEL gemini_2_5_flash_v17
PREDICT response
USING
    engine = 'gemini_enterprise_engine_v1',
    model_name = 'gemini-2.5-flash',
    prompt_template = 'Answer the following question: {{question}}';
```

**Key Points:**
- Use `{{question}}` not `{{text}}` for agent compatibility
- The agent sends data in a `question` column
- `prompt_template` provides flexibility over `question_column`

## Step 3: Text-to-SQL Skill Creation

### 3.1 Skill Setup

```sql
CREATE SKILL db2_sql_skill
USING
    type = 'text2sql',
    database = 'rtmdb',
    tables = ['rtmdb.SCHEMA.TABLE1', 'rtmdb.SCHEMA.TABLE2', 'rtmdb.SCHEMA.TABLE3'],
    description = 'Skill to query IBM DB2 database with enterprise data';
```

**Internal Process (`skill_tool.py`):**
1. `_make_text_to_sql_tools()` creates MindsDB SQL toolkit
2. Extracts database names from dot notation (`rtmdb.SCHEMA.TABLE`)
3. Connects to each database via `integration_controller.get_data_handler()`
4. Builds table list with proper escaping: `database.schema.`table``
5. Creates `SQLAgent` with table restrictions and sample data configuration

**Tools Created:**
- `sql_db_list_tables`: Lists available tables
- `sql_db_schema`: Gets table schemas and sample data (`SELECT * FROM table LIMIT 3`)
- `sql_db_query`: Executes generated SQL queries
- `mindsdb_sql_parser_tool`: Validates SQL syntax

## Step 4: Agent Creation

### 4.1 Agent Setup

```sql
CREATE AGENT db2_agent
USING
    model = 'gemini_2_5_flash_v17',
    skills = ['db2_sql_skill'];
```

**Internal Process (`langchain_agent.py`):**
1. `create_agent()` initializes LangChain agent executor
2. `create_chat_model()` creates ChatGoogleGenerativeAI instance  
3. `_langchain_tools_from_skills()` converts MindsDB skills to LangChain tools
4. Sets up conversation memory with `ConversationSummaryBufferMemory`
5. Configures agent with `SafeOutputParser` and custom prompt prefixes

**Agent Components:**
- **LLM**: Google Gemini 2.5 Flash via LangChain
- **Tools**: SQL database tools from Text2SQL skill
- **Memory**: Conversation history tracking
- **Executor**: Manages tool calling and response generation

## Step 5: Natural Language Querying

### 5.1 Query Execution

```sql
SELECT question, answer
FROM db2_agent
WHERE question = 'How many records are in the customers table?';
```

**Internal Flow (Step-by-Step):**

#### Phase 1: Agent Initialization
1. **Input Processing**: Question arrives as DataFrame with `question` column
2. **Agent Loading**: `LangchainAgent.create_agent()` called
3. **Model Setup**: Creates `ChatGoogleGenerativeAI` with Vertex AI credentials
4. **Tool Loading**: Converts Text2SQL skill to LangChain SQL tools
5. **Memory Setup**: Initializes conversation buffer with system prompt

#### Phase 2: Query Planning  
6. **Agent Invocation**: `run_agent()` processes the question
7. **Tool Selection**: Agent decides which SQL tools to use
8. **Schema Discovery**: Agent calls `sql_db_list_tables` to see available tables
9. **Table Inspection**: Calls `sql_db_schema` to get column information and sample data

#### Phase 3: SQL Generation
10. **Question Analysis**: Gemini LLM analyzes the natural language question
11. **SQL Construction**: Generates appropriate SQL query based on schema
12. **Query Validation**: `mindsdb_sql_parser_tool` validates the SQL syntax
13. **Query Enhancement**: Adds proper JOINs, WHERE clauses, aggregations as needed

#### Phase 4: Execution
14. **SQL Execution**: `sql_db_query` tool executes against DB2 via JDBC
15. **Result Processing**: Raw SQL results returned from database
16. **Response Generation**: Gemini LLM converts results to natural language
17. **Answer Formatting**: Final answer returned in conversational format

### 5.2 Complex Query Example

**Input:** "Show me the top 5 products with their category names and total sales"

**Generated SQL Process:**
1. Agent identifies need for JOIN between products, categories, and sales tables
2. Calls `sql_db_schema` for each relevant table to understand relationships  
3. Generates complex SQL with proper table aliases and aggregation:
```sql
SELECT p.PRODUCT_NAME, c.CATEGORY_NAME, SUM(s.AMOUNT) as total_sales
FROM rtmdb.SCHEMA.PRODUCTS p
JOIN rtmdb.SCHEMA.CATEGORIES c ON p.CATEGORY_ID = c.ID
JOIN rtmdb.SCHEMA.SALES s ON s.PRODUCT_ID = p.ID
GROUP BY p.PRODUCT_NAME, c.CATEGORY_NAME
ORDER BY total_sales DESC
LIMIT 5;
```

## Advanced Configuration

### Environment Variables (Startup Scripts)

The startup scripts configure multiple environment variables:

```powershell
# SSL Certificates (fixes corporate proxy issues)
$env:SSL_CERT_FILE = "path\\to\\certifi\\cacert.pem"
$env:REQUESTS_CA_BUNDLE = "path\\to\\certifi\\cacert.pem"
$env:GRPC_DEFAULT_SSL_ROOTS_FILE_PATH = "path\\to\\certifi\\cacert.pem"

# Google Cloud Authentication
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\\path\\to\\vertex-ai.json"

# ChromaDB (for knowledge bases)
$env:ANONYMIZED_TELEMETRY = "False"  # Suppress PostHog errors

# DB2 CLI Drivers (fallback)
$env:IBM_DB_HOME = "C:\\path\\to\\clidriver"
$env:DB2_HOME = "C:\\path\\to\\clidriver"
```

### Text2SQL Skill Advanced Features

**Categorical Data Handling:**
- Agent automatically runs `SELECT DISTINCT column` for categorical filters
- Ensures accurate `WHERE col IN (...)` clauses

**Date Operations:**
- Uses `CURRENT_DATE` and `NOW()` functions
- Proper date casting: `column_name::DATE`
- Interval operations with keywords: `NOW() + INTERVAL 5 DAY`

**Result Management:**
- Runs `SELECT COUNT(*)` first for large result sets
- Limits to 10 results initially, informs user of total count
- Allows users to request more results or different ordering

**Query Best Practices:**
- Uses backticks for identifiers: `database`.`table`.`column`
- Single quotes for string constants
- Handles MySQL quoting rules specifically

### Error Handling and Debugging

**Common Issues:**
1. **SSL Certificate Errors**: Resolved by setting certificate env vars
2. **API Key Missing**: Clear error messages with troubleshooting steps  
3. **Column Mismatch**: Template `{{text}}` vs `{{question}}` compatibility
4. **JDBC Driver Path**: Fallback from CLI to JDBC handler
5. **Service Account Scope**: Must use `cloud-platform` for Vertex AI

**Debugging Tools:**
- Set `verbose = True` in agent creation for detailed logging
- Use `SHOW SKILLS` and `SHOW AGENTS` to verify configuration
- Check MindsDB logs for SQL generation and execution details

## Troubleshooting

### Model Template Issues
If you get `None of [Index(['text'], dtype='object')] are in the [columns]` error:

**Problem**: Model expects `text` column, agent provides `question` column

**Solution**: Recreate model with `prompt_template = '{{question}}'`

### SSL/Certificate Issues
Corporate environments may block SSL connections:

**Solution**: Startup scripts set certificate environment variables automatically

### JDBC Connection Issues
If DB2 CLI drivers fail:

**Solution**: Handler automatically falls back to JDBC with proper driver path

### Agent Timeout
For complex queries that take too long:

**Solution**: Increase timeout in agent creation:
```sql
CREATE AGENT db2_agent
USING
    model = 'gemini_2_5_flash_v17',
    skills = ['db2_sql_skill'],
    timeout_seconds = 300;
```

## Summary of Internal Components

| Component | Technology | Purpose |
|-----------|------------|---------|
| **Database Handler** | JayDeBeApi (JDBC) | Connects to DB2 via JDBC, handles SSL automatically |
| **ML Engine** | google-genai library | Vertex AI integration with service account auth |
| **Agent LLM** | ChatGoogleGenerativeAI | LangChain wrapper for Gemini via Vertex AI |
| **Text2SQL Skill** | MindsDBSQLToolkit | Schema introspection and SQL generation tools |
| **Agent Executor** | LangChain AgentExecutor | Orchestrates tool calling and response generation |
| **Memory** | ConversationSummaryBufferMemory | Tracks conversation history |
| **SQL Agent** | Custom SQLAgent | Manages database connections and query execution |

The system provides a seamless natural language interface to your DB2 database, automatically handling authentication, schema discovery, SQL generation, and response formatting.