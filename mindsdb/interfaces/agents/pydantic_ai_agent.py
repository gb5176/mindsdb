"""Pydantic AI Agent wrapper to replace LangchainAgent"""

import json
import warnings
import functools
from typing import Dict, List, Optional, Any, Iterable

import pandas as pd
from mindsdb_sql_parser import parse_sql, ast
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, ModelMessage, TextPart

from mindsdb.utilities import log
from mindsdb.interfaces.storage import db
from mindsdb.interfaces.agents.utils.constants import (
    USER_COLUMN,
    ASSISTANT_COLUMN,
    CONTEXT_COLUMN,
    TRACE_ID_COLUMN,
)

from mindsdb.interfaces.agents.utils.sql_toolkit import MindsDBQuery
from mindsdb.interfaces.agents.utils.pydantic_ai_model_factory import get_model_instance_from_kwargs
from mindsdb.interfaces.agents.utils.data_catalog_builder import DataCatalogBuilder, dataframe_to_markdown
from mindsdb.utilities.context import context as ctx
from mindsdb.utilities.langfuse import LangfuseClientWrapper
from mindsdb.interfaces.agents.modes import sql as sql_mode, text_sql as text_sql_mode
from mindsdb.interfaces.agents.modes.base import ResponseType, PlanResponse

logger = log.getLogger(__name__)
DEBUG_LOGGER = logger.debug


# Suppress asyncio warnings about unretrieved task exceptions from httpx cleanup
# This is a known issue where httpx.AsyncClient tries to close connections after the event loop is closed
warnings.filterwarnings("ignore", message=".*Task exception was never retrieved.*", category=RuntimeWarning)


def langfuse_traced_stream(trace_name="api-completion", span_name="run-completion"):
    """Decorator that wraps a generator method with Langfuse trace/span lifecycle."""

    def decorator(method):
        @functools.wraps(method)
        def wrapper(self, messages, *args, **kwargs):
            # Setup trace & span
            self.langfuse_client_wrapper.setup_trace(
                name=trace_name,
                input=messages,
                tags=self.get_tags(),
                metadata=self.get_metadata(),
                user_id=ctx.user_id,
                session_id=ctx.session_id,
            )
            self.run_completion_span = self.langfuse_client_wrapper.start_span(
                name=span_name,
                input=messages,
            )
            try:
                yield from method(self, messages, *args, **kwargs)
            finally:
                self.langfuse_client_wrapper.end_span(self.run_completion_span)

        return wrapper

    return decorator


class PydanticAIAgent:
    """Pydantic AI-based agent to replace LangchainAgent"""

    def __init__(
        self,
        agent: db.Agents,
        llm_params: dict = None,
    ):
        """
        Initialize Pydantic AI agent.

        Args:
            agent: Agent database record
            args: Agent parameters (optional)
            llm_params: LLM parameters (optional)
        """
        self.agent = agent

        self.run_completion_span: Optional[object] = None
        self.llm: Optional[object] = None
        self.embedding_model: Optional[object] = None

        self.log_callback_handler: Optional[object] = None
        self.langfuse_callback_handler: Optional[object] = None
        self.mdb_langfuse_callback_handler: Optional[object] = None

        self.langfuse_client_wrapper = LangfuseClientWrapper()
        self.agent_mode = self.agent.params.get("mode", "text")
        # Target SQL dialect for the returned query: "db2", "mysql", "postgres", "mindsdb" (default).
        # When set to any value other than "mindsdb", the final SQL is returned as text (not executed)
        # with dialect-specific transformations applied.  "mindsdb" (or absent) preserves normal
        # execute-and-return-data behaviour.
        self.sql_dialect = self.agent.params.get("sql_dialect", None)
        # return_sql_only: skip execution and return the SQL text as the answer.
        # Driven solely by mode='sql' — sql_dialect is independent (controls syntax only).
        self.return_sql_only = self.agent_mode == "sql"
        # sql_cache_kb: explicit param takes precedence (backward compat).
        # If not set, auto-detected after sql_toolkit is built by checking each KB's stored kb_type.
        self.sql_cache_kb = self.agent.params.get("sql_cache_kb", None)
        self.sql_cache_min_relevance = float(self.agent.params.get("sql_cache_min_relevance", 0.7))
        self.sql_cache_max_examples = int(self.agent.params.get("sql_cache_max_examples", 3))

        self.llm_params = llm_params

        # Provider model instance
        self.model_instance = get_model_instance_from_kwargs(self.llm_params)

        # Command executor for queries
        tables_list = list(self.agent.params.get("data", {}).get("tables", []))
        knowledge_bases_list = list(self.agent.params.get("data", {}).get("knowledge_bases", []))

        # Bridge legacy skills-based config to the new data format.
        # text2sql skills → tables_list; retrieval skills → knowledge_bases_list
        for rel in getattr(self.agent, 'skills_relationships', []):
            skill = rel.skill
            if skill.type == 'text2sql':
                db_name = skill.params.get('database', '')
                for t in skill.params.get('tables', []):
                    if isinstance(t, dict):
                        schema = t.get('schema', '')
                        table = t.get('table', '')
                        if db_name and schema and table:
                            tables_list.append(f'{db_name}.{schema}.{table}')
                        elif db_name and table:
                            tables_list.append(f'{db_name}.{table}')
                    elif isinstance(t, str):
                        tables_list.append(f'{db_name}.{t}' if db_name else t)
            elif skill.type == 'retrieval':
                source = skill.params.get('source', '')
                if source and source not in knowledge_bases_list:
                    knowledge_bases_list.append(source)

        self.sql_toolkit = MindsDBQuery(tables_list, knowledge_bases_list)

        # Auto-detect sql_cache_kb from KB metadata if not explicitly set.
        # KBs created with USING kb_type='sql_cache' are identified here and excluded from
        # schema pre-fetch and the LLM data catalog — used only for few-shot injection.
        if not self.sql_cache_kb:
            _project_id = getattr(self.agent, "project_id", None)
            _session = self.sql_toolkit.command_executor.session
            for _kb_name in knowledge_bases_list:
                _kb_bare = str(_kb_name).split(".")[-1]
                try:
                    _kb_rec = _session.kb_controller.get(_kb_bare, _project_id)
                    if _kb_rec and (_kb_rec.params or {}).get("kb_type") == "sql_cache":
                        self.sql_cache_kb = _kb_bare
                        logger.info(f"sql_cache_kb auto-detected via kb_type: {self.sql_cache_kb!r}")
                        break
                except Exception:
                    pass

        logger.info(f"[SQL CACHE] sql_cache_kb={self.sql_cache_kb!r}  agent_params_keys={list(self.agent.params.keys())}")

        self.system_prompt = self.agent.params.get("prompt_template", "You are an expert MindsDB SQL data analyst")

        # Track current query state
        self._current_prompt: Optional[str] = None
        self._current_sql_query: Optional[str] = None
        self._current_query_result: Optional[pd.DataFrame] = None

        self.select_targets = None

    def _convert_messages_to_history(self, df: pd.DataFrame, args: dict) -> List[ModelMessage]:
        """
        Convert DataFrame messages to Pydantic AI message history format.

        Args:
            df: DataFrame with user/assistant columns or role/content columns

        Returns:
            List of Pydantic AI Message objects
        """
        messages = []

        # Check if DataFrame has 'role' and 'content' columns (API format)
        if "role" in df.columns and "content" in df.columns:
            for _, row in df.iterrows():
                role = row.get("role")
                content = row.get("content", "")
                if pd.notna(role) and pd.notna(content):
                    if role == "user":
                        messages.append(ModelRequest.user_text_prompt(str(content)))
                    elif role == "assistant":
                        messages.append(ModelResponse(parts=[TextPart(content=str(content))]))
        else:
            # Legacy format with question/answer columns
            user_column = args.get("user_column", USER_COLUMN)
            assistant_column = args.get("assistant_column", ASSISTANT_COLUMN)

            for _, row in df.iterrows():
                user_msg = row.get(user_column)
                assistant_msg = row.get(assistant_column)

                if pd.notna(user_msg) and str(user_msg).strip():
                    messages.append(ModelRequest.user_text_prompt(str(user_msg)))

                if pd.notna(assistant_msg) and str(assistant_msg).strip():
                    messages.append(ModelResponse(parts=[TextPart(content=str(assistant_msg))]))

        return messages

    def _extract_current_prompt_and_history(self, messages: Any, args: Dict) -> tuple[str, List[ModelMessage]]:
        """
        Extract current prompt and message history from messages in various formats.

        Args:
            messages: Can be:
                - List of dicts with 'role' and 'content' (API format)
                - List of dicts with 'question' and 'answer' (Q&A format from A2A)
                - DataFrame with 'role'/'content' columns (API format)
                - DataFrame with 'question'/'answer' columns (legacy format)
            args: Arguments dict

        Returns:
            Tuple of (current_prompt: str, message_history: List[ModelMessage])
        """
        # Handle list of dicts with 'role' and 'content' (API format)
        if isinstance(messages, list) and len(messages) > 0:
            if isinstance(messages[0], dict) and "role" in messages[0]:
                # Convert to Pydantic AI Message objects
                pydantic_messages = []
                for msg in messages:
                    if msg.get("role") == "user":
                        pydantic_messages.append(ModelRequest.user_text_prompt(msg.get("content", "")))
                    elif msg.get("role") == "assistant":
                        pydantic_messages.append(ModelResponse(parts=[TextPart(content=msg.get("content", ""))]))

                # Get current prompt (last user message)
                current_prompt = ""
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        current_prompt = msg.get("content", "")
                        break

                # Get message history (all except last message)
                message_history = pydantic_messages[:-1] if len(pydantic_messages) > 1 else []
                return current_prompt, message_history

            # Handle Q&A format (from A2A conversion): list of dicts with 'question' and 'answer' keys
            elif isinstance(messages[0], dict) and "question" in messages[0]:
                # Convert Q&A format to role/content format for processing
                role_content_messages = []
                for qa_msg in messages:
                    question = qa_msg.get("question", "")
                    answer = qa_msg.get("answer", "")

                    # Add user message (question)
                    if question:
                        role_content_messages.append({"role": "user", "content": str(question)})

                    # Add assistant message (answer) if present
                    if answer:
                        role_content_messages.append({"role": "assistant", "content": str(answer)})

                # Now process as role/content format
                if len(role_content_messages) > 0:
                    pydantic_messages = []
                    for msg in role_content_messages:
                        if msg.get("role") == "user":
                            pydantic_messages.append(ModelRequest.user_text_prompt(msg.get("content", "")))
                        elif msg.get("role") == "assistant":
                            pydantic_messages.append(ModelResponse(parts=[TextPart(content=msg.get("content", ""))]))

                    # Get current prompt (last user message)
                    current_prompt = ""
                    for msg in reversed(role_content_messages):
                        if msg.get("role") == "user":
                            current_prompt = msg.get("content", "")
                            break

                    # Get message history (all except last message)
                    message_history = pydantic_messages[:-1] if len(pydantic_messages) > 1 else []
                    return current_prompt, message_history

        # Handle DataFrame format
        df = messages if isinstance(messages, pd.DataFrame) else pd.DataFrame(messages)
        df = df.reset_index(drop=True)

        # Check if DataFrame has 'role' and 'content' columns (API format)
        if "role" in df.columns and "content" in df.columns:
            # Convert to Pydantic AI Message objects
            pydantic_messages = []
            for _, row in df.iterrows():
                role = row.get("role")
                content = row.get("content", "")
                if pd.notna(role) and pd.notna(content):
                    if role == "user":
                        pydantic_messages.append(ModelRequest.user_text_prompt(str(content)))
                    elif role == "assistant":
                        pydantic_messages.append(ModelResponse(parts=[TextPart(content=str(content))]))

            # Get current prompt (last user message)
            current_prompt = ""
            for index in reversed(range(len(df))):
                row = df.iloc[index]
                if row.get("role") == "user":
                    current_prompt = str(row.get("content", ""))
                    break

            # Get message history (all except last message)
            message_history = pydantic_messages[:-1] if len(pydantic_messages) > 1 else []
            return current_prompt, message_history

        # Legacy DataFrame format with question/answer columns
        user_column = args.get("user_column", USER_COLUMN)
        current_prompt = ""
        if len(df) > 0 and user_column in df.columns:
            user_messages = df[user_column].dropna()
            if len(user_messages) > 0:
                current_prompt = str(user_messages.iloc[-1])

        # Convert history (all except last)
        history_df = df[:-1] if len(df) > 1 else pd.DataFrame()
        message_history = self._convert_messages_to_history(history_df, args)
        return current_prompt, message_history

    def get_metadata(self) -> Dict:
        """Get metadata for observability"""
        return {
            "model_name": self.llm_params["model_name"],
            "user_id": ctx.user_id,
            "session_id": ctx.session_id,
            "company_id": ctx.company_id,
            "user_class": ctx.user_class,
        }

    def get_tags(self) -> List:
        """Get tags for observability"""
        return ["AGENT", "PYDANTIC_AI"]

    def get_select_targets_from_sql(self, sql) -> Optional[List[str]]:
        """
        Get the SELECT targets from the original SQL query if available.
        Extracts only the column names, ignoring aliases (e.g., "col1 as alias" -> "col1").

        Returns:
            List of SELECT target column names if available, None otherwise
        """

        try:
            parsed = parse_sql(sql)
        except Exception:
            return

        if not isinstance(parsed, ast.Select):
            return

        targets = []
        for target in parsed.targets:
            if isinstance(target, ast.Identifier):
                targets.append(target.parts[-1])

            elif isinstance(target, ast.Star):
                return  # ['question', 'answer']

            elif isinstance(target, ast.Function):
                # For functions, get the function name and args
                func_str = target.op
                targets.append(func_str)
                if target.args:
                    for arg in target.args:
                        if isinstance(arg, ast.Identifier):
                            targets.append(arg.parts[-1])

        return targets

    def get_completion(self, messages, stream: bool = False, params: dict | None = None):
        """
        Get completion from agent.

        Args:
            messages: List of message dictionaries or DataFrame
            stream: Whether to stream the response
            params: Additional parameters

        Returns:
            DataFrame with assistant response
        """
        # Extract SQL context from params if present
        if params and "original_query" in params:
            original_query = params.pop("original_query")

            self.select_targets = self.get_select_targets_from_sql(original_query)

        args = {}
        args.update(self.agent.params or {})
        args.update(params or {})

        data = None
        if stream:
            return self._get_completion_stream(messages, args)
        else:
            for message in self._get_completion_stream(messages, args):
                if message.get("type") == "end":
                    break
                elif message.get("type") == "error":
                    error_message = f"Agent failed with model error: {message.get('content')}"
                    raise RuntimeError(error_message)
                last_message = message

                # if last_message.get("type") == "sql":
                #     sql_query = last_message.get("content")

                if last_message.get("type") == "data":
                    if "text" in last_message:
                        data = pd.DataFrame([{"answer": last_message["text"]}])
                    else:
                        data = last_message.get("content")

            else:
                error_message = f"Agent failed with model error: {last_message.get('content')}"
                return self._create_error_response(error_message, return_context=params.get("return_context", True))

            # Validate select targets if specified

            if self.select_targets is not None:
                # Ensure all expected columns are present
                if data is None or (isinstance(data, pd.DataFrame) and data.empty):
                    # Create DataFrame with one row of nulls for all expected columns
                    data = pd.DataFrame({col: [None] for col in self.select_targets})
                else:
                    # Ensure all expected columns exist, add missing ones with null values
                    cols_map = {c.lower(): c for c in data.columns}

                    for col in self.select_targets:
                        if col not in data.columns:
                            # try to find case independent
                            if col.lower() in cols_map:
                                data[col] = data[cols_map[col.lower()]]
                            elif len(data.columns) == 1:
                                # Single-column result whose alias was stripped by the DB driver —
                                # use it as the value for the expected target column.
                                data[col] = data.iloc[:, 0]
                            else:
                                data[col] = None
                    # Reorder columns to match select_targets order
                    data = data[self.select_targets]

            return data

    @staticmethod
    def _to_db2_sql(sql: str) -> str:
        """Apply DB2-compatible transformations to a raw SQL string.

        Mirrors the regex pipeline in DB2JDBCHandler.query() so the returned SQL
        is ready to run directly against a DB2 database without further processing:
          - strip MindsDB connection-name prefix from table references
          - remove backtick quoting, uppercase identifiers
          - convert LIMIT N to FETCH FIRST N ROWS ONLY
        """
        import re

        # 1. Remove MindsDB connection-name prefix: `aar_db2`.`SCHEMA`.`TABLE` → `SCHEMA`.`TABLE`
        #    Pattern matches a lowercase_with_underscores name followed by a dot.
        sql = re.sub(r'`?[a-z_][a-z0-9_]*_connection[a-z0-9_]*`?\.', '', sql, flags=re.IGNORECASE)

        # 2. Replace backtick schema.table pairs → SCHEMA.TABLE (uppercase, no quotes)
        sql = re.sub(
            r'`([a-zA-Z_][a-zA-Z0-9_]*)`\.`([a-zA-Z_][a-zA-Z0-9_]*)`',
            lambda m: f"{m.group(1).upper()}.{m.group(2).upper()}",
            sql,
        )

        # 3. Replace remaining standalone backtick-quoted identifiers → UPPERCASE unquoted
        sql = re.sub(r'`([a-zA-Z_][a-zA-Z0-9_]*)`', lambda m: m.group(1).upper(), sql)

        # 4. Convert LIMIT N [OFFSET M] → FETCH FIRST N ROWS ONLY
        def _limit_to_fetch(m):
            n, offset = m.group(1), m.group(2)
            if offset:
                return f"OFFSET {offset} ROWS FETCH FIRST {n} ROWS ONLY"
            return f"FETCH FIRST {n} ROWS ONLY"

        sql = re.sub(
            r'\bLIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+))?',
            _limit_to_fetch,
            sql,
            flags=re.IGNORECASE,
        )
        return sql.strip()

    def _apply_sql_dialect(self, sql: str) -> str:
        """Transform *sql* into the target dialect specified by self.sql_dialect.

        Supported values:
          "db2"      – IBM DB2 (FETCH FIRST, uppercase identifiers, no backticks)
          "mindsdb"  – no transformation (return as-is)
          None       – no transformation (return as-is)

        Additional dialects ("mysql", "postgres", etc.) can be added here.
        """
        if self.sql_dialect == "db2":
            return self._to_db2_sql(sql)
        # "mindsdb", None, or any unknown value → return unchanged
        return sql

    def _create_error_response(self, error_message: str, return_context: bool = True) -> pd.DataFrame:
        """Create error response DataFrame"""
        response_data = {
            ASSISTANT_COLUMN: [error_message],
            TRACE_ID_COLUMN: [self.langfuse_client_wrapper.get_trace_id()],
        }
        if return_context:
            response_data[CONTEXT_COLUMN] = [json.dumps([])]
        return pd.DataFrame(response_data)

    def _fetch_kb_context(self, question: str) -> str:
        """Semantic pre-fetch from all KBs using the user's question.

        Returns relevant KB chunks (content only, truncated) for the planning prompt.
        Capped at 3000 chars total to avoid bloating the planning prompt.
        """
        _MAX_CHUNK_CHARS = 300  # truncate each chunk to keep snippets concise
        _MAX_TOTAL_CHARS = 3000  # hard cap on total injected KB context
        _cache_kb_bare = (self.sql_cache_kb or "").split(".")[-1].lower()
        parts = []
        for kb in self.sql_toolkit.get_usable_knowledge_base_names():
            if _cache_kb_bare and str(kb).split(".")[-1].lower() == _cache_kb_bare:
                continue  # sql_cache KB is not a schema source — skip pre-fetch
            kb_query = ast.Select(
                targets=[ast.Star()],
                from_table=kb,
                where=ast.BinaryOperation("LIKE", args=[ast.Identifier("content"), ast.Constant(question)]),
                limit=ast.Constant(5),
            )
            try:
                result = self.sql_toolkit.execute(kb_query)
                if result is not None and not result.empty:
                    # Only show the content column — skip id/metadata columns to save tokens
                    content_col = next(
                        (c for c in result.columns if c.lower() in ("chunk_content", "content")),
                        result.columns[0],
                    )
                    snippets = [str(v)[:_MAX_CHUNK_CHARS] for v in result[content_col] if v is not None]
                    parts.append(f"--- {kb} ---\n" + "\n\n".join(snippets))
            except Exception as e:
                logger.debug(f"KB pre-fetch failed for {kb}: {e}")
        full = "\n\n".join(parts)
        if len(full) > _MAX_TOTAL_CHARS:
            full = full[:_MAX_TOTAL_CHARS] + "\n... (truncated)"
        return full

    def _fetch_similar_sql_examples(self, question: str) -> str:
        """Retrieve semantically similar approved (question → SQL) pairs from the cache KB."""
        if not self.sql_cache_kb:
            return ""
        try:
            from mindsdb.utilities.context import context as ctx
            tenant_id = str(getattr(ctx, "company_id", "") or "")

            cache_query = ast.Select(
                targets=[ast.Star()],
                from_table=ast.Identifier(self.sql_cache_kb),
                where=ast.BinaryOperation(
                    "AND",
                    args=[
                        ast.BinaryOperation(
                            "AND",
                            args=[
                                ast.BinaryOperation(
                                    "LIKE",
                                    args=[ast.Identifier("content"), ast.Constant(question)]
                                ),
                                ast.BinaryOperation(
                                    ">=",
                                    args=[ast.Identifier("relevance"), ast.Constant(self.sql_cache_min_relevance)]
                                ),
                            ]
                        ),
                        ast.BinaryOperation(
                            "=",
                            args=[ast.Identifier("tenant_id"), ast.Constant(tenant_id)]
                        ),
                    ]
                ),
                limit=ast.Constant(self.sql_cache_max_examples),
            )
            result = self.sql_toolkit.execute(cache_query)
            if result is None or result.empty:
                return ""

            examples = []
            for _, row in result.iterrows():
                # KB stores embedded text as 'chunk_content'; 'content' is only a WHERE search trigger
                q = str(row.get("chunk_content", "") or row.get("content", "")).strip()
                sql = str(row.get("sql_query", "")).strip()
                if q and sql and sql.upper() != "NONE":
                    examples.append(f"Question: {q}\nSQL:\n{sql}")

            if not examples:
                return ""

            logger.info(f"SQL cache: {len(examples)} similar example(s) found for prompt injection")
            return (
                "=== Past Similar SQL Queries (REFERENCE ONLY — DO NOT COPY VERBATIM) ===\n"
                "Adapt these for the current question. Verify column names against the data catalog.\n"
                "If a past example contradicts the catalog, TRUST THE CATALOG.\n\n"
                + "\n\n---\n\n".join(examples)
            )

        except Exception as e:
            logger.warning(f"SQL cache fetch failed (non-critical): {type(e).__name__}: {e}")
            return ""

    def _stage_query_example(self, question: str, sql: str) -> None:
        """Write a (question → SQL) pair to the local staging JSONL file for manual review.

        Nothing is inserted into the KB automatically. The user must run the logged INSERT
        command to approve and promote an entry to the KB.
        Runs in a background daemon thread — zero latency to the user.
        """
        if not self.sql_cache_kb:
            return

        kb = self.sql_cache_kb
        dialect = self.sql_dialect or "mindsdb"

        def _do_stage():
            try:
                import json
                import os
                import uuid
                from datetime import datetime, timezone
                from mindsdb.utilities.context import context as ctx

                tenant_id = str(getattr(ctx, "company_id", "") or "")
                user_id = str(getattr(ctx, "user_id", "") or "")
                entry_id = str(uuid.uuid4())[:8]
                created_at = datetime.now(timezone.utc).isoformat()

                # Resolve MindsDB data root for staging file location
                try:
                    from mindsdb.utilities.config import Config
                    root = str(Config().paths.get("root", os.path.expanduser("~/.mindsdb")))
                except Exception:
                    root = os.path.expanduser("~/.mindsdb")
                os.makedirs(root, exist_ok=True)
                staging_path = os.path.join(root, "sql_cache_staging.jsonl")

                entry = {
                    "id": entry_id,
                    "question": question,
                    "sql_query": sql,
                    "dialect": dialect,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "created_at": created_at,
                }

                # One JSON object per line — safe for concurrent appends
                with open(staging_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")

                safe_q = question.replace("'", "''")
                safe_sql = sql.replace("'", "''")
                logger.info(
                    f"SQL cache staged [{entry_id}]: '{question[:60]}...'\n"
                    f"  Staging file: {staging_path}\n"
                    f"  To approve this entry, run:\n"
                    f"    INSERT INTO {kb} (content, sql_query, dialect, tenant_id, user_id, created_at)\n"
                    f"    VALUES ('{safe_q}', '{safe_sql}', '{dialect}',\n"
                    f"            '{tenant_id}', '{user_id}', '{created_at}');"
                )
            except Exception as e:
                # Bumped from debug → warning so failures are visible in default log level.
                logger.warning(f"SQL cache staging failed (non-critical): {type(e).__name__}: {e}")

        import threading
        logger.info(f"SQL cache: launching stage thread for kb={kb!r}, question_len={len(question)}, sql_len={len(sql)}")
        threading.Thread(target=_do_stage, daemon=True).start()

    @langfuse_traced_stream(trace_name="api-completion", span_name="run-completion")
    def _get_completion_stream(self, messages: List[dict], params) -> Iterable[Dict]:
        """
        Get completion as a stream of chunks.

        Args:
            messages: List of message dictionaries or DataFrame

        Returns:
            Iterator of chunk dictionaries
        """
        DEBUG_LOGGER(f"PydanticAIAgent._get_completion_stream: Messages: {messages}")

        # Extract current prompt and message history from messages
        # This handles multiple formats: list of dicts, DataFrame with role/content, or legacy DataFrame
        current_prompt, message_history = self._extract_current_prompt_and_history(messages, params)
        # Save the original user question — current_prompt is later overwritten with base_prompt
        # in the execution loop. Staging and the approve command need the original short question.
        original_question = current_prompt
        DEBUG_LOGGER(
            f"PydanticAIAgent._get_completion_stream: Extracted prompt and {len(message_history)} history messages"
        )

        yield self._add_chunk_metadata({"type": "status", "content": "Generating Data Catalog..."})

        if self.agent_mode == "text":
            agent_prompts = text_sql_mode
            AgentResponse = text_sql_mode.AgentResponse
        else:
            agent_prompts = sql_mode
            AgentResponse = sql_mode.AgentResponse

        # Skip MindsDB-specific SQL instructions when targeting a non-MindsDB dialect —
        # the dialect rules injected into base_prompt replace them entirely.
        _skip_mindsdb_instructions = self.return_sql_only and self.sql_dialect not in (None, "mindsdb")
        if _skip_mindsdb_instructions:
            sql_instructions = ""
        elif self.sql_toolkit.knowledge_bases:
            sql_instructions = f"{agent_prompts.sql_description}\n\n{agent_prompts.sql_with_kb_description}"
        else:
            sql_instructions = agent_prompts.sql_description

        # When KBs are present: use fewer sample rows (KB pre-fetch supplies focused column info).
        # Always include SHOW COLUMNS metadata — omitting it causes the LLM to hallucinate
        # column names for tables whose columns aren't semantically close to the question.
        _has_kbs = bool(self.sql_toolkit.knowledge_bases)
        _sample_rows = 3 if _has_kbs else 5
        _include_metadata = True
        import contextvars
        from concurrent.futures import ThreadPoolExecutor

        catalog_builder = DataCatalogBuilder(
            sql_toolkit=self.sql_toolkit,
            sample_rows=_sample_rows,
            include_metadata=_include_metadata,
            exclude_kbs=[self.sql_cache_kb] if self.sql_cache_kb else None,
        )

        if _has_kbs:
            # Catalog build and KB pre-fetch are independent — overlap them to save ~3-5 sec.
            # Each thread needs its own context copy — a single Context object cannot be entered
            # by two threads simultaneously.
            with ThreadPoolExecutor(max_workers=2) as _pool:
                _f_catalog = _pool.submit(contextvars.copy_context().run, catalog_builder.build_data_catalog)
                _f_kb = _pool.submit(contextvars.copy_context().run, self._fetch_kb_context, current_prompt)
            data_catalog = _f_catalog.result()
            _kb_prefetch = _f_kb.result()
        else:
            data_catalog = catalog_builder.build_data_catalog()
            _kb_prefetch = ""

        # Initialize counters and accumulators
        exploratory_query_count = 0
        exploratory_query_results = []
        MAX_EXPLORATORY_QUERIES = 20
        MAX_RETRIES = 3

        # Planning step: Create a plan before generating queries
        yield self._add_chunk_metadata({"type": "status", "content": "Creating execution plan..."})

        # Create planning agent
        planning_agent = Agent(self.model_instance, system_prompt=self.system_prompt, output_type=PlanResponse)

        # Build planning prompt
        planning_prompt_text = f"""Take into account the following Data Catalog:\n{data_catalog}\n\n{agent_prompts.planning_prompt}\n\nQuestion to answer: {current_prompt}"""

        # Inject KB context collected in parallel with the catalog build
        if _kb_prefetch:
            planning_prompt_text += f"\n\n=== Knowledge Base Context (schema / column info) ===\n{_kb_prefetch}"
            logger.info(f"KB pre-fetch injected {len(_kb_prefetch)} chars into planning prompt")
        _sql_examples = self._fetch_similar_sql_examples(current_prompt)
        if _sql_examples:
            planning_prompt_text += f"\n\n{_sql_examples}"
            logger.info(f"SQL cache: injected {len(_sql_examples)} chars of examples into planning prompt")
        DEBUG_LOGGER(f"PydanticAIAgent._get_completion_stream: Planning prompt text: {planning_prompt_text}")
        # Get select targets for planning context

        select_targets_str = None
        if self.select_targets is not None:
            select_targets_str = ", ".join(str(t) for t in self.select_targets)
            planning_prompt_text += f"\n\nFor the final query, the user expects to have a table such that this query is valid: SELECT {select_targets_str} FROM (<generated query>); when creating your plan, make sure to account for these expected columns."

        # Generate plan
        plan_result = planning_agent.run_sync(planning_prompt_text)
        _pu = plan_result.usage()
        logger.info(f"[TOKEN USAGE] planning: request={_pu.request_tokens}, response={_pu.response_tokens}, total={_pu.total_tokens}, details={_pu.details}")
        plan = plan_result.output
        # Validate plan steps don't exceed MAX_EXPLORATORY_QUERIES
        if plan.estimated_steps > MAX_EXPLORATORY_QUERIES:
            logger.warning(
                f"Plan estimated {plan.estimated_steps} steps, but maximum is {MAX_EXPLORATORY_QUERIES}. Adjusting plan."
            )
            plan.plan += (
                f"\n\nNote: The plan has been adjusted to ensure it does not exceed {MAX_EXPLORATORY_QUERIES} steps."
            )

        DEBUG_LOGGER(f"Generated plan with {plan.estimated_steps} estimated steps: {plan.plan}")

        # Yield the plan as a status message
        yield self._add_chunk_metadata(
            {
                "type": "status",
                "content": f"Proposed Execution Plan:\n{plan.plan}\n\nEstimated steps: {plan.estimated_steps}\n\n",
            }
        )

        # Build base prompt with plan included
        _sql_instr_block = f"MindsDB SQL instructions:\n{sql_instructions}\n\n" if sql_instructions else ""
        base_prompt = (
            f"\n\nTake into account the following Data Catalog:\n{data_catalog}\n"
            f"{_sql_instr_block}"
            f"Proposed Execution Plan:\n{plan.plan}\n\n"
            f"Estimated steps: {plan.estimated_steps} (maximum allowed: {MAX_EXPLORATORY_QUERIES})\n\n"
            f"Please follow this plan and write Mindsdb SQL queries to answer the question:\n{current_prompt}"
        )

        if self.return_sql_only:
            _dialect_instructions = {
                "db2": (
                    "This agent targets a DB2 database. Write ALL queries in standard MindsDB SQL "
                    "format — the system automatically converts the final answer to DB2 syntax "
                    "(FETCH FIRST, 2-part names, uppercase identifiers).\n"
                    "Rules:\n"
                    "  - For ALL queries (exploratory and final): use the EXACT 3-part table name "
                    "from the data catalog, including the connection name prefix "
                    "(e.g. aar_db2_minddb_connection_knl01pt_v1.coguser.RAR_PROJECT).\n"
                    "  - If a knowledge base result shows a 2-part table name like "
                    "'coguser.rar_proj_execution', find the matching 3-part name in the data "
                    "catalog and use that.\n"
                    "  - Use LIMIT N for row limits (not FETCH FIRST) — it will be converted automatically.\n"
                    "  - Use standard SQL JOIN syntax; no MindsDB-specific extensions."
                ),
                "mysql": (
                    "MySQL SQL rules you must follow:\n"
                    "  - Use LIMIT N for row limits\n"
                    "  - Backtick-quote identifiers that are reserved words\n"
                    "  - Use standard MySQL JOIN syntax; no MindsDB-specific extensions"
                ),
                "postgres": (
                    "PostgreSQL SQL rules you must follow:\n"
                    "  - Use LIMIT N for row limits\n"
                    "  - Double-quote identifiers only when necessary\n"
                    "  - Use standard PostgreSQL syntax; no MindsDB-specific extensions"
                ),
            }
            dialect_hint = _dialect_instructions.get(self.sql_dialect or "", "")
            base_prompt += (
                f"\n\nIMPORTANT: Your final answer MUST be a SQL query (type=final_query) "
                f"compatible with the target database ({self.sql_dialect or 'mindsdb'}). "
                + (f"\n{dialect_hint}\n" if dialect_hint else "")
                + "The SQL query itself IS the final answer — do not wrap it in text or explanation."
            )

        if select_targets_str is not None:
            base_prompt += f"\n\nFor the final query the user expects to have a table such that this query is valid: SELECT {select_targets_str} FROM (<generated query>); when generating the SQL query make sure to include those columns, do not fix grammar on columns. Keep them as the user wants them"

        if _has_kbs:
            base_prompt += (
                "\n\nKNOWLEDGE BASE QUERY RULES:\n"
                "- Column schema info is already in the data catalog above — do NOT query a knowledge base "
                "just to discover table structure or column names.\n"
                "- When you DO query a knowledge base, ALWAYS include a specific "
                "WHERE content LIKE '<search term>' filter AND add FETCH FIRST 10 ROWS ONLY "
                "(or LIMIT 10 for MindsDB-side queries) to avoid retrieving too many rows.\n"
                "- Knowledge base results may show 2-part table names (e.g. 'coguser.rar_proj_execution'). "
                "Always match these to the full 3-part name in the data catalog before using them in SQL."
            )

        DEBUG_LOGGER(
            f"PydanticAIAgent._get_completion_stream: Sending LLM request with Current prompt: {current_prompt}"
        )
        DEBUG_LOGGER(f"PydanticAIAgent._get_completion_stream: Message history: {message_history}")

        # Create agent
        agent = Agent(self.model_instance, system_prompt=self.system_prompt, output_type=AgentResponse)

        retry_count = 0

        try:
            while True:
                yield self._add_chunk_metadata({"type": "status", "content": "Generating agent response..."})

                current_prompt = base_prompt
                if exploratory_query_results:
                    current_prompt += "\n\nPrevious exploratory query results:\n" + "\n---\n".join(
                        exploratory_query_results
                    )

                if exploratory_query_count == MAX_EXPLORATORY_QUERIES:
                    current_prompt += f"\n\nIMPORTANT: You have reached the maximum number of exploratory queries ({MAX_EXPLORATORY_QUERIES}). The next query you generate MUST be a final_query or final_text."

                result = agent.run_sync(
                    current_prompt,
                    message_history=message_history if message_history else None,
                )
                _au = result.usage()
                logger.info(f"[TOKEN USAGE] loop#{exploratory_query_count}: request={_au.request_tokens}, response={_au.response_tokens}, total={_au.total_tokens}, details={_au.details}")

                # Extract output
                output = result.output

                # Yield description before SQL query
                if output.short_description:
                    yield self._add_chunk_metadata({"type": "context", "content": output.short_description})

                if output.type == ResponseType.FINAL_TEXT:
                    yield self._add_chunk_metadata({"type": "status", "content": "Returning text response"})

                    # return text to user and exit
                    yield self._add_chunk_metadata({"type": "data", "text": output.text})
                    yield self._add_chunk_metadata({"type": "end"})
                    return
                elif output.type == ResponseType.EXPLORATORY and exploratory_query_count == MAX_EXPLORATORY_QUERIES:
                    raise RuntimeError(
                        "Agent exceeded the maximum number of exploratory queries "
                        f"({MAX_EXPLORATORY_QUERIES}) but result still not returned. "
                        f"output.type='{output.type}', expected 'final_query' or 'final_text'."
                    )

                sql_query = output.sql_query
                DEBUG_LOGGER(
                    f"PydanticAIAgent._get_completion_stream: Received LLM response: sql: {sql_query}, query_type: {output.type}, description: {output.short_description}"
                )

                # return_sql_only mode: skip execution of the final query and return the SQL text as the answer
                if output.type == ResponseType.FINAL_QUERY and self.return_sql_only:
                    final_sql = self._apply_sql_dialect(sql_query)
                    logger.info(f"return_sql_only=True (dialect={self.sql_dialect!r}): returning SQL as answer: {final_sql}")
                    # If SQL cache is configured, append the approve command directly to the SQL response
                    # as a comment block so the user always sees it (UI may drop separate 'context' chunks).
                    response_text = final_sql
                    if self.sql_cache_kb:
                        from mindsdb.utilities.context import context as ctx
                        from datetime import datetime, timezone
                        _tenant_id = str(getattr(ctx, "company_id", "") or "")
                        _user_id = str(getattr(ctx, "user_id", "") or "")
                        _created_at = datetime.now(timezone.utc).isoformat()
                        _safe_q = original_question.replace("'", "''")
                        # INSERT stores MindsDB SQL (pre-dialect) so the cache entry works for
                        # both mode='sql' and mode='text'. The answer returned to the user
                        # (response_text) still uses final_sql (DB2 format).
                        _safe_sql = sql_query.replace("'", "''")
                        _dialect = "mindsdb"
                        # Runnable INSERT — copy/paste directly from the log to approve.
                        _runnable_insert = (
                            f"INSERT INTO {self.sql_cache_kb} "
                            f"(content, sql_query, dialect, tenant_id, user_id, created_at) "
                            f"VALUES ('{_safe_q}', '{_safe_sql}', '{_dialect}', "
                            f"'{_tenant_id}', '{_user_id}', '{_created_at}');"
                        )
                        logger.info(
                            "===== SQL CACHE: copy/paste the INSERT below to approve this query for future reuse =====\n"
                            f"{_runnable_insert}\n"
                            "===== END SQL CACHE INSERT ====="
                        )
                        # Also append a commented version to the SQL response (UI fallback).
                        _approve_cmd = (
                            f"\n\n-- ===== To save this query for future use, run the INSERT below: =====\n"
                            f"-- {_runnable_insert}"
                        )
                        response_text = final_sql + _approve_cmd
                        logger.info(f"SQL cache: appended approve command ({len(_approve_cmd)} chars) to response")
                    # Stage BEFORE the final yields — if the consumer closes the generator after
                    # receiving "end", any code after the last yield is skipped (GeneratorExit).
                    # The daemon thread launched by _stage_query_example runs independently afterwards.
                    self._stage_query_example(original_question, sql_query)  # MindsDB SQL, not DB2
                    yield self._add_chunk_metadata({"type": "data", "text": response_text})
                    yield self._add_chunk_metadata({"type": "end"})
                    return

                try:
                    query_type = "final" if output.type == ResponseType.FINAL_QUERY else "exploratory"
                    yield self._add_chunk_metadata(
                        {"type": "status", "content": f"Executing {query_type} SQL query: {sql_query}"}
                    )
                    query_data = self.sql_toolkit.execute_sql(sql_query, escape_identifiers=True)
                    logger.info(f"query_data columns={list(query_data.columns)}, rows={len(query_data)}, head=\n{query_data.head(2)}")
                except Exception as e:
                    # Extract error message - prefer db_error_msg for QueryError, otherwise use str(e)
                    query_error = str(e)

                    # Yield descriptive error message
                    error_message = f"Error executing SQL query: {query_error}"
                    yield self._add_chunk_metadata({"type": "status", "content": error_message})

                    retry_count += 1
                    if retry_count >= MAX_RETRIES:
                        DEBUG_LOGGER(
                            f"PydanticAIAgent._get_completion_stream: retry ({retry_count}/{MAX_RETRIES}) after error: {query_error}"
                        )
                        raise RuntimeError(
                            f"Failed to execute {query_type} SQL query after {retry_count} consecutive unsuccessful SQL queries. "
                            f"Last error: {query_error}\nSQL:\n{sql_query}"
                        )

                    query_result_str = f"Query: {sql_query}\nError: {query_error}"
                    exploratory_query_results.append(query_result_str)

                    continue

                DEBUG_LOGGER("PydanticAIAgent._get_completion_stream: Executed SQL query successfully")
                retry_count = 0

                if output.type == ResponseType.FINAL_QUERY:
                    # If the final query returned 0 rows and we still have exploratory budget,
                    # feed the empty result back so the LLM can revise its filters.
                    if query_data.empty and exploratory_query_count < MAX_EXPLORATORY_QUERIES:
                        logger.info("Final query returned 0 rows - feeding back as exploratory for filter revision")
                        exploratory_query_count += 1
                        yield self._add_chunk_metadata(
                            {"type": "status", "content": "Final query returned 0 rows - asking agent to revise filters"}
                        )
                        query_result_str = (
                            f"Query: {sql_query}\nDescription: {output.short_description}\n"
                            f"Result: EMPTY (0 rows returned)\n"
                            f"IMPORTANT: This query returned no results. Please re-examine the filter conditions. "
                            f"Hint: date surrogate key columns (e.g. COMPL_DATE_SKEY, END_DATE_SKEY) use "
                            f"20991231 as a sentinel meaning 'not yet completed / open record', not as a filter "
                            f"for future-dated items. To find open/incomplete records filter for = 20991231; "
                            f"to find completed records filter for < 20991231. Verify your filter logic and retry."
                        )
                        exploratory_query_results.append(query_result_str)
                        continue
                    # mode='text': sql_query is MindsDB-format SQL (3-part names, LIMIT N) — safe to cache.
                    # Stage BEFORE yields to avoid GeneratorExit silently skipping this call.
                    self._stage_query_example(original_question, sql_query)
                    # return response to user
                    yield self._add_chunk_metadata({"type": "data", "content": query_data})
                    yield self._add_chunk_metadata({"type": "end"})
                    return

                # is exploratory
                exploratory_query_count += 1
                debug_message = f"Exploratory query {exploratory_query_count}/{MAX_EXPLORATORY_QUERIES} succeeded"
                DEBUG_LOGGER(debug_message)
                yield self._add_chunk_metadata({"type": "status", "content": debug_message})

                # Format query result for prompt — cap at 3000 chars to prevent
                # wide/long results (e.g. SELECT * on a 100-column table) from permanently
                # bloating the context window across all subsequent loops.
                markdown_table = dataframe_to_markdown(query_data)
                _MAX_RESULT_CHARS = 3000
                if len(markdown_table) > _MAX_RESULT_CHARS:
                    markdown_table = (
                        markdown_table[:_MAX_RESULT_CHARS]
                        + f"\n... [truncated — {len(query_data)} row(s), {len(query_data.columns)} column(s) total; "
                        "use SELECT with specific columns to get a focused result]"
                    )
                query_result_str = (
                    f"Query: {sql_query}\nDescription: {output.short_description}\nResult:\n{markdown_table}"
                )
                yield self._add_chunk_metadata({"type": "status", "content": f"Query result: {markdown_table}"})
                exploratory_query_results.append(query_result_str)

        except Exception as e:
            # Suppress the "Event loop is closed" error from httpx cleanup
            # This is a known issue where async HTTP clients try to close after the event loop is closed
            error_msg = str(e)
            if "Event loop is closed" in error_msg:
                # This is a cleanup issue, not a critical error - log at debug level
                DEBUG_LOGGER(f"Async cleanup warning (non-critical): {error_msg}")
            else:
                # Extract error message - prefer db_error_msg for QueryError, otherwise use str(e)
                from mindsdb.utilities.exception import QueryError

                if isinstance(e, QueryError):
                    error_content = e.db_error_msg or str(e)
                    descriptive_error = f"Database query error: {error_content}"
                    if e.failed_query:
                        descriptive_error += f"\n\nFailed query: {e.failed_query}"
                else:
                    error_content = error_msg
                    descriptive_error = f"Agent streaming failed: {error_content}"

                logger.error(f"Agent streaming failed: {error_content}")
                error_chunk = self._add_chunk_metadata(
                    {
                        "type": "error",
                        "content": descriptive_error,
                    }
                )
                yield error_chunk

    def _add_chunk_metadata(self, chunk: Dict) -> Dict:
        """Add metadata to chunk"""
        chunk["trace_id"] = self.langfuse_client_wrapper.get_trace_id()
        return chunk
