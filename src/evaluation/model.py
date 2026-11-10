import json
from time import perf_counter

import httpx
from langchain_core.messages import AIMessage, convert_to_openai_messages
from langchain_core.utils.function_calling import convert_to_openai_tool

from src.config import settings
from src.domain.exceptions import AppError
from src.evaluation.budget import BudgetStop


class RecordedModel:
    def __init__(self, model, traces, secrets=()):
        self.model, self.traces, self.secrets = model, traces, tuple(value for value in secrets if value)

    def bind_tools(self, tools):
        return RecordedModel(self.model.bind_tools(tools), self.traces, self.secrets)

    async def ainvoke(self, messages):
        started = perf_counter()
        try:
            response = await self.model.ainvoke(messages)
        except Exception:
            self.traces.append(dict(duration_ms=(perf_counter() - started) * 1000, error='MODEL_REQUEST_FAILED'))
            raise
        value = dict(content=response.content, tool_calls=response.tool_calls, usage=response.usage_metadata,
                     duration_ms=(perf_counter() - started) * 1000,
                     finish_reason=response.response_metadata.get('finish_reason'))
        triage_stage = any('Classify the customer' in str(message.content) for message in messages)
        if response.tool_calls and triage_stage:
            value['schema_valid'] = False
        elif not response.tool_calls:
            from src.agent.nodes import _json
            from src.domain.proposals import Triage, ModelDraft
            schema = Triage if triage_stage else ModelDraft
            try:
                parsed = schema.model_validate(_json(response.content))
                value['schema_valid'] = True
                if schema is Triage:
                    value['triage'] = parsed.model_dump()
            except (TypeError, ValueError):
                value['schema_valid'] = False
        serialized = json.dumps(value, ensure_ascii=False)
        for secret in self.secrets:
            serialized = serialized.replace(secret, '[REDACTED]')
        self.traces.append(json.loads(serialized))
        return response


class BudgetedModel:
    """Exactly one HTTP request per invocation; no SDK retry/redirect/model fallback."""
    def __init__(self, budget, split, tools=None):
        self.budget, self.split, self.tools = budget, split, tools

    def bind_tools(self, tools):
        return BudgetedModel(self.budget, self.split, [convert_to_openai_tool(tool) for tool in tools])

    async def ainvoke(self, messages):
        maximum = settings.evaluation_output_tokens
        payload = dict(model=settings.openai_model, temperature=settings.temperature,
            messages=convert_to_openai_messages(messages), max_tokens=maximum, stream=False)
        from src.agent.llm import thinking_options
        payload.update(thinking_options())
        if self.tools:
            payload['tools'] = self.tools
        # UTF-8 byte upper reservation plus per-message/tool overhead; measured usage is authoritative.
        bound = len(json.dumps(payload, ensure_ascii=False).encode()) + 1024 + 64 * len(messages)
        try:
            identity = self.budget.reserve(self.split, bound, maximum)
        except BudgetStop:
            raise AppError('EVALUATION_BUDGET_STOP', 'Evaluation budget stopped', 503) from None
        usage = None
        response_data = None
        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds, follow_redirects=False, trust_env=False) as client:
                response = await client.post(settings.openai_base_url.rstrip('/') + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + settings.openai_api_key}, json=payload)
                response.raise_for_status()
                response_data = response.json()
                raw_usage = response_data.get('usage') or {}
                if type(raw_usage.get('prompt_tokens')) is int and type(raw_usage.get('completion_tokens')) is int:
                    usage = dict(input_tokens=raw_usage['prompt_tokens'], output_tokens=raw_usage['completion_tokens'])
        finally:
            try:
                self.budget.settle(identity, usage)
            except BudgetStop:
                raise AppError('EVALUATION_BUDGET_STOP', 'Usage unavailable or over reservation; evaluation stopped', 503) from None
        if usage is None:
            raise AppError('MODEL_USAGE_UNKNOWN','Provider usage unavailable; reservation retained',503)
        message = response_data['choices'][0]['message']
        if usage['output_tokens'] > maximum:
            raise AppError('EVALUATION_BUDGET_STOP', 'Provider did not honor output limit', 503)
        calls = [dict(id=item['id'], name=item['function']['name'], args=json.loads(item['function']['arguments']))
                 for item in message.get('tool_calls', [])]
        return AIMessage(content=message.get('content') or '', tool_calls=calls,
            response_metadata={'finish_reason': response_data['choices'][0].get('finish_reason')},
            usage_metadata={**usage, 'total_tokens': usage['input_tokens'] + usage['output_tokens']})
