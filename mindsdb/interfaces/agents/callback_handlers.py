import io
import time
import logging
import contextlib
from typing import Any, Dict, List, Union, Callable

from langchain_core.agents import AgentAction, AgentFinish
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages.base import BaseMessage
from langchain_core.outputs import LLMResult
from langchain_core.callbacks import StdOutCallbackHandler


class TokenTrackingCallbackHandler(BaseCallbackHandler):
    """Tracks LLM calls, token usage, and tool invocations for each agent request."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.llm_call_count = 0
        self.tool_call_count = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.calls: List[Dict[str, Any]] = []
        self.tools: List[Dict[str, Any]] = []
        self._current_llm_start_time = None

    def on_llm_start(self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any) -> Any:
        self._current_llm_start_time = time.time()

    def on_chat_model_start(self, serialized: Dict[str, Any], messages: List[List[BaseMessage]], **kwargs: Any) -> Any:
        self._current_llm_start_time = time.time()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> Any:
        self.llm_call_count += 1
        elapsed = time.time() - self._current_llm_start_time if self._current_llm_start_time else 0

        # Extract token usage from response
        prompt_tokens = 0
        completion_tokens = 0
        call_total_tokens = 0

        # Try llm_output (OpenAI-style)
        if response.llm_output and isinstance(response.llm_output, dict):
            usage = response.llm_output.get("token_usage", response.llm_output.get("usage", {}))
            if isinstance(usage, dict):
                prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
                completion_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
                call_total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens) or 0

        # Try usage_metadata on generations (Google Gemini / newer LangChain)
        if call_total_tokens == 0 and response.generations:
            for gen_list in response.generations:
                for gen in gen_list:
                    if hasattr(gen, 'message') and hasattr(gen.message, 'usage_metadata'):
                        um = gen.message.usage_metadata
                        if um and isinstance(um, dict):
                            prompt_tokens = um.get('input_tokens', 0) or 0
                            completion_tokens = um.get('output_tokens', 0) or 0
                            call_total_tokens = um.get('total_tokens', 0) or 0
                            if call_total_tokens == 0:
                                call_total_tokens = prompt_tokens + completion_tokens
                        elif um:
                            # Object with attributes (e.g., pydantic model)
                            prompt_tokens = getattr(um, 'input_tokens', 0) or 0
                            completion_tokens = getattr(um, 'output_tokens', 0) or 0
                            call_total_tokens = getattr(um, 'total_tokens', 0) or 0
                            if call_total_tokens == 0:
                                call_total_tokens = prompt_tokens + completion_tokens
                    if call_total_tokens > 0:
                        break
                if call_total_tokens > 0:
                    break

        # Try response_metadata (another path for token info)
        if call_total_tokens == 0 and response.generations:
            for gen_list in response.generations:
                for gen in gen_list:
                    if hasattr(gen, 'message') and hasattr(gen.message, 'response_metadata'):
                        rm = gen.message.response_metadata or {}
                        usage = rm.get('usage_metadata', rm.get('token_usage', {}))
                        if isinstance(usage, dict):
                            prompt_tokens = usage.get('prompt_token_count', usage.get('input_tokens', 0)) or 0
                            completion_tokens = usage.get('candidates_token_count', usage.get('output_tokens', 0)) or 0
                            call_total_tokens = usage.get('total_token_count', usage.get('total_tokens', 0)) or 0
                            if call_total_tokens == 0:
                                call_total_tokens = prompt_tokens + completion_tokens
                    if call_total_tokens > 0:
                        break
                if call_total_tokens > 0:
                    break

        # Debug: when all paths return 0, log the raw response structure to help diagnose
        if call_total_tokens == 0:
            try:
                debug_parts = [f"llm_output={response.llm_output}"]
                if response.generations:
                    gen = response.generations[0][0] if response.generations[0] else None
                    if gen:
                        debug_parts.append(f"gen_type={type(gen).__name__}")
                        debug_parts.append(f"gen_attrs={[a for a in dir(gen) if not a.startswith('_')]}")
                        if hasattr(gen, 'message'):
                            msg = gen.message
                            debug_parts.append(f"msg_type={type(msg).__name__}")
                            debug_parts.append(f"usage_metadata={getattr(msg, 'usage_metadata', 'N/A')}")
                            rm = getattr(msg, 'response_metadata', None)
                            debug_parts.append(f"response_metadata={rm}")
                            # dump all non-private msg attributes
                            debug_parts.append(f"msg_attrs={[(a, getattr(msg, a, None)) for a in dir(msg) if not a.startswith('_') and a not in ('copy', 'dict', 'json', 'schema', 'validate')]}")
                self.logger.info(f">>> TOKEN DEBUG (tokens=0): {' | '.join(debug_parts)}")
            except Exception as e:
                self.logger.info(f">>> TOKEN DEBUG failed: {e}")

        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_tokens += call_total_tokens

        call_info = {
            "call_number": self.llm_call_count,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": call_total_tokens,
            "elapsed_seconds": round(elapsed, 2),
        }
        self.calls.append(call_info)

        token_str = f"{call_total_tokens}" if call_total_tokens > 0 else "N/A"
        self.logger.info(
            f">>> LLM CALL #{self.llm_call_count} | "
            f"Tokens: {token_str} (prompt={prompt_tokens}, completion={completion_tokens}) | "
            f"Time: {elapsed:.2f}s | "
            f"Cumulative: {self.total_tokens} tokens in {self.llm_call_count} calls"
        )

    def on_llm_error(self, error: Union[Exception, KeyboardInterrupt], **kwargs: Any) -> Any:
        self.logger.error(f">>> LLM ERROR on call #{self.llm_call_count + 1}: {str(error)}")

    def on_tool_start(self, serialized: Dict[str, Any], input_str: str, **kwargs: Any) -> Any:
        self.tool_call_count += 1
        tool_name = serialized.get("name", "unknown")
        self.logger.info(f">>> TOOL #{self.tool_call_count}: {tool_name} | Input: {str(input_str)[:200]}")
        self.tools.append({"tool_number": self.tool_call_count, "name": tool_name, "input": str(input_str)[:500]})

    def on_agent_finish(self, finish: AgentFinish, **kwargs: Any) -> Any:
        self.logger.info(
            f">>> AGENT SUMMARY | "
            f"LLM calls: {self.llm_call_count} | "
            f"Tool calls: {self.tool_call_count} | "
            f"Total tokens: {self.total_tokens} "
            f"(prompt={self.total_prompt_tokens}, completion={self.total_completion_tokens})"
        )

    def get_summary(self) -> Dict[str, Any]:
        return {
            "llm_calls": self.llm_call_count,
            "tool_calls": self.tool_call_count,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "call_details": self.calls,
            "tool_details": self.tools,
        }


class ContextCaptureCallback(BaseCallbackHandler):
    def __init__(self):
        self.context = None

    def on_retriever_end(self, documents: List[Any], *, run_id: str, parent_run_id: Union[str, None] = None, **kwargs: Any) -> Any:
        self.context = [{
            'page_content': doc.page_content,
            'metadata': doc.metadata
        } for doc in documents]

    def get_contexts(self):
        return self.context


class VerboseLogCallbackHandler(StdOutCallbackHandler):
    def __init__(self, logger: logging.Logger, verbose: bool):
        self.logger = logger
        self.verbose = verbose
        super().__init__()

    def __call(self, method: Callable, *args: List[Any], **kwargs: Any) -> Any:
        if self.verbose is False:
            return
        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            method(*args, **kwargs)
        output = f.getvalue()
        self.logger.info(output)

    def on_chain_start(self, *args: List[Any], **kwargs: Any) -> None:
        self.__call(super().on_chain_start, *args, **kwargs)

    def on_chain_end(self, *args: List[Any], **kwargs: Any) -> None:
        self.__call(super().on_chain_end, *args, **kwargs)

    def on_agent_action(self, *args: List[Any], **kwargs: Any) -> None:
        self.__call(super().on_agent_action, *args, **kwargs)

    def on_tool_end(self, *args: List[Any], **kwargs: Any) -> None:
        self.__call(super().on_tool_end, *args, **kwargs)

    def on_text(self, *args: List[Any], **kwargs: Any) -> None:
        self.__call(super().on_text, *args, **kwargs)

    def on_agent_finish(self, *args: List[Any], **kwargs: Any) -> None:
        self.__call(super().on_agent_finish, *args, **kwargs)


class LogCallbackHandler(BaseCallbackHandler):
    '''Langchain callback handler that logs agent and chain executions.'''

    def __init__(self, logger: logging.Logger, verbose: bool = True):
        logger.setLevel('DEBUG')
        self.logger = logger
        self._num_running_chains = 0
        self.generated_sql = None
        self.verbose_log_handler = VerboseLogCallbackHandler(logger, verbose)

    def on_llm_start(
        self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any
    ) -> Any:
        '''Run when LLM starts running.'''
        self.logger.debug('LLM started with prompts:')
        for prompt in prompts:
            self.logger.debug(prompt[:50])
        self.verbose_log_handler.on_llm_start(serialized, prompts, **kwargs)

    def on_chat_model_start(
            self,
            serialized: Dict[str, Any],
            messages: List[List[BaseMessage]], **kwargs: Any
    ) -> Any:
        '''Run when Chat Model starts running.'''
        self.logger.debug('Chat model started with messages:')
        for message_list in messages:
            for message in message_list:
                self.logger.debug(message.pretty_repr())

    def on_llm_new_token(self, token: str, **kwargs: Any) -> Any:
        '''Run on new LLM token. Only available when streaming is enabled.'''
        pass

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> Any:
        '''Run when LLM ends running.'''
        self.logger.debug('LLM ended with response:')
        self.logger.debug(str(response.llm_output))

    def on_llm_error(
        self, error: Union[Exception, KeyboardInterrupt], **kwargs: Any
    ) -> Any:
        '''Run when LLM errors.'''
        self.logger.debug(f'LLM encountered an error: {str(error)}')

    def on_chain_start(
        self, serialized: Dict[str, Any], inputs: Dict[str, Any], **kwargs: Any
    ) -> Any:
        '''Run when chain starts running.'''
        self._num_running_chains += 1
        self.logger.info('Entering new LLM chain ({} total)'.format(
            self._num_running_chains))
        self.logger.debug('Inputs: {}'.format(inputs))

        self.verbose_log_handler.on_chain_start(serialized=serialized, inputs=inputs, **kwargs)

    def on_chain_end(self, outputs: Dict[str, Any], **kwargs: Any) -> Any:
        '''Run when chain ends running.'''
        self._num_running_chains -= 1
        self.logger.info('Ended LLM chain ({} total)'.format(
            self._num_running_chains))
        self.logger.debug('Outputs: {}'.format(outputs))

        self.verbose_log_handler.on_chain_end(outputs=outputs, **kwargs)

    def on_chain_error(
        self, error: Union[Exception, KeyboardInterrupt], **kwargs: Any
    ) -> Any:
        '''Run when chain errors.'''
        self._num_running_chains -= 1
        self.logger.error(
            'LLM chain encountered an error ({} running): {}'.format(
                self._num_running_chains, error))

    def on_tool_start(
        self, serialized: Dict[str, Any], input_str: str, **kwargs: Any
    ) -> Any:
        '''Run when tool starts running.'''
        pass

    def on_tool_end(self, output: str, **kwargs: Any) -> Any:
        '''Run when tool ends running.'''
        self.verbose_log_handler.on_tool_end(output=output, **kwargs)

    def on_tool_error(
        self, error: Union[Exception, KeyboardInterrupt], **kwargs: Any
    ) -> Any:
        '''Run when tool errors.'''
        pass

    def on_text(self, text: str, **kwargs: Any) -> Any:
        '''Run on arbitrary text.'''
        self.verbose_log_handler.on_text(text=text, **kwargs)

    def on_agent_action(self, action: AgentAction, **kwargs: Any) -> Any:
        '''Run on agent action.'''
        self.logger.debug(f'Running tool {action.tool} with input:')
        self.logger.debug(action.tool_input)

        stop_block = 'Observation: '
        if stop_block in action.tool_input:
            action.tool_input = action.tool_input[: action.tool_input.find(stop_block)]

        if action.tool.startswith("sql_db_query"):
            # Save the generated SQL query
            self.generated_sql = action.tool_input

        # fix for mistral
        action.tool = action.tool.replace('\\', '')

        self.verbose_log_handler.on_agent_action(action=action, **kwargs)

    def on_agent_finish(self, finish: AgentFinish, **kwargs: Any) -> Any:
        '''Run on agent end.'''
        self.logger.debug('Agent finished with return values:')
        self.logger.debug(str(finish.return_values))
        self.verbose_log_handler.on_agent_finish(finish=finish, **kwargs)
