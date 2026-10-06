"""Raw transport extension underneath the custom SDK agent (SDK 1.49.5).

Bypass ToolDefinition/Message conversion, argument repair and stock recovery.
Subscription preserves the model-visible contract in Responses protocol, without the
SDK's system-to-user rewrite or its ToolDefinition schema augmentation.
"""
from __future__ import annotations
from copy import deepcopy
from ..compat.trae_llm_policy import TraeLLMPolicy, send_with_reference_retry
from ..events import emit
from ..compat.audit import request_context, safe_payload_hash, register_secrets, sanitize


def responses_payload(messages, tools):
    """Project the Trae chat contract without parsing arguments or trimming text."""
    if not messages or messages[0]["role"] != "system":
        raise ValueError("Trae Responses transport requires a leading system message")
    instructions = messages[0]["content"]
    items = []
    for message in messages[1:]:
        role = message["role"]
        content = message.get("content")
        if role in {"user", "assistant"}:
            if content is not None:
                items.append({"role": role, "content": content})
            for call in message.get("tool_calls") or []:
                function = call["function"]
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": function["name"], "arguments": function["arguments"]})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": content})
        else:
            raise ValueError(f"Unsupported Trae Responses message role: {role}")
    # Responses defaults can normalize optional fields into a strict schema.
    # Explicit non-strict mode matches the source Chat Completions semantics.
    resp_tools = [{"type": "function", **deepcopy(tool["function"]), "strict": False}
                  for tool in tools]
    return instructions, items, resp_tools


def _dict(value):
    return value if isinstance(value, dict) else value.model_dump(exclude_none=True)


def _drain_response(raw):
    """Drain required SSE, retaining item.done output when completed.output=[]."""
    if isinstance(raw, dict) or hasattr(raw, "output"):
        return raw
    final = None
    items = {}
    try:
        for event in raw:
            if event is None:
                continue
            event = _dict(event)
            kind = event.get("type")
            if kind == "response.output_item.done":
                items[event["output_index"]] = event["item"]
            elif kind in {"response.completed", "response.incomplete"}:
                final = event["response"]
            elif kind in {"response.failed", "response.cancelled", "error"}:
                raise RuntimeError(f"Subscription Responses stream ended with {kind}")
        cached = getattr(raw, "completed_response", None)
        if final is None and cached is not None:
            cached = _dict(cached)
            final = cached.get("response", cached)
        if final is None:
            raise RuntimeError("Subscription Responses stream ended without a terminal response")
        final = deepcopy(_dict(final))
        if not final.get("output"):
            final["output"] = [items[index] for index in sorted(items)]
        return final
    finally:
        close = getattr(raw, "close", None)
        if not callable(close):
            # LiteLLM's SSE iterator exposes its httpx.Response, not close().
            close = getattr(getattr(raw, "response", None), "close", None)
        if callable(close):
            close()


def responses_answer(raw):
    """Return the same raw assistant/finish/usage shape as chat-completion mode."""
    raw = _dict(raw)
    status = raw.get("status")
    if status not in {"completed", "incomplete"}:
        raise RuntimeError(f"Subscription Responses returned status {status!r}")
    if raw.get("error"):
        raise RuntimeError("Subscription Responses returned an error")
    texts, refusals, calls = [], [], []
    for item in raw.get("output") or []:
        item = _dict(item)
        if item["type"] == "message":
            for part in item.get("content") or []:
                part = _dict(part)
                if part["type"] == "output_text":
                    texts.append(part["text"])
                elif part["type"] == "refusal":
                    refusals.append(part["refusal"])
        elif item["type"] == "function_call":
            if not all(isinstance(item.get(key), str) for key in ("call_id", "name", "arguments")):
                raise RuntimeError("Subscription Responses returned an invalid function call envelope")
            calls.append({"id": item["call_id"], "type": "function", "function": {
                "name": item["name"], "arguments": item["arguments"]}})
    answer = {"role": "assistant", "content": "".join(texts) if texts else None}
    if calls:
        answer["tool_calls"] = calls
    if refusals:
        answer["refusal"] = "".join(refusals)
    reason = "tool_calls" if calls else "stop"
    if status == "incomplete":
        detail = (raw.get("incomplete_details") or {}).get("reason")
        if detail not in {"max_output_tokens", "content_filter"}:
            raise RuntimeError(f"Subscription Responses incomplete: {detail!r}")
        reason = "length" if detail == "max_output_tokens" else "content_filter"
    usage = deepcopy(_dict(raw["usage"])) if raw.get("usage") is not None else {}
    usage["prompt_tokens"] = usage.get("input_tokens")
    usage["completion_tokens"] = usage.get("output_tokens")
    return answer, reason, usage


class SDKRawTransport:
    def __init__(self, llm, *, policy: TraeLLMPolicy | None = None, capabilities=(), sleep=None,
                 conformance="exact"):
        self.llm = llm
        self.policy, self.sleep = policy, sleep
        self.conformance = conformance
        if conformance not in {'exact', 'adapted'}:
            raise ValueError('unknown transport conformance')
        if conformance == 'adapted' and (policy is None or not llm.is_subscription):
            raise ValueError('adapted transport requires subscription and a Trae role policy')
        if conformance == 'adapted' and (
                set(llm.litellm_extra_body or {}) - {'store'} or llm.seed is not None or llm.api_version is not None):
            raise ValueError('adapted subscription transport does not accept unreviewed extra_body, seed or api_version')
        if policy is not None and conformance == 'exact':
            policy.validate_transport(subscription=llm.is_subscription, capabilities=capabilities)
            if llm.litellm_extra_body or llm.seed is not None or llm.api_version is not None:
                raise ValueError(f"{policy.role}: extra_body, seed and api_version are outside the exact transport contract")
            if llm._provider_info.name not in {None, "openai", "openrouter", "custom_openai"}:
                raise ValueError(f"{policy.role}: exact transport requires an OpenAI-compatible endpoint")

    def _send_with_retry(self, one_attempt, exhausted_message):
        import sys

        last_error = None

        def report_error(exc, attempt, delay):
            nonlocal last_error
            last_error = exc
            message = sanitize(str(exc))
            emit('trae_transport_error', role=self.policy.role, attempt=attempt,
                 error_type=type(exc).__name__, message=message,
                 retry_delay_seconds=delay, status_code=getattr(exc, 'status_code', None))
            print(f'[agent-diet] model request failed role={self.policy.role} '
                  f'attempt={attempt}/12 retry_in={delay}s '
                  f'{type(exc).__name__}: {message}', file=sys.stderr, flush=True)

        options = {'sleep': self.sleep} if self.sleep is not None else {}
        result = send_with_reference_retry(one_attempt, on_error=report_error, **options)
        if result is None:
            raise RuntimeError(exhausted_message) from last_error
        return result

    @property
    def protocol(self):
        return "responses" if self.llm.is_subscription else "chat-completions"

    def __call__(self, messages, tools):
        if self.policy is not None:
            with request_context(self.policy.role, exact_transport=self.conformance == 'exact',
                                 adapted_transport=self.conformance == 'adapted') as request_id:
                self.last_request_id = request_id
                if self.conformance == 'adapted':
                    return self._trae_subscription(messages, tools)
                return self._exact_chat(messages, tools)
        if self.llm.is_subscription:
            return self._subscription(messages, tools)
        kwargs = {"tools": deepcopy(tools), "temperature": 0.0, "n": 1}
        if self.llm.reasoning_effort:
            kwargs["reasoning_effort"] = self.llm.reasoning_effort
        telemetry = self.llm.telemetry
        telemetry.on_request({"messages": messages, "tools": tools})
        try:
            # Version-pinned extension: raw dicts reach SDK provider serialization.
            raw = self.llm._transport_call(messages=deepcopy(messages), **kwargs)
            telemetry.on_response(raw, provider_info=self.llm._provider_info)
        except Exception as exc:
            telemetry.on_error(exc)
            raise
        choice = raw.choices[0]
        answer = choice.message.model_dump(exclude_none=True)
        answer.setdefault("content", None)
        usage = raw.usage.model_dump() if raw.usage is not None else {}
        usage.setdefault("completion_tokens", None)
        return answer, choice.finish_reason, usage

    def _exact_chat(self, messages, tools):
        """Use SDK auth/routing and telemetry, with an unprojected OpenAI body.

        LiteLLM changes params and cache blocks based on the *actual* model.
        Exact mode therefore uses the pinned OpenAI serializer directly. This
        is a deliberate transport boundary, shared by repair and compression.
        """
        from openai import OpenAI
        from litellm.types.utils import ModelResponse

        prepared = self.llm._prepare_transport_kwargs(messages=deepcopy(messages), enable_streaming=False)
        provider = prepared.get("custom_llm_provider")
        if provider not in {None, "openai", "openrouter", "custom_openai"}:
            raise ValueError(f"{self.policy.role}: exact transport requires an OpenAI-compatible endpoint, got {provider!r}")
        register_secrets(prepared.get("api_key"))
        data = {"model": prepared["model"], "messages": deepcopy(messages), **self.policy.params()}
        if self.policy.role == "repair" and tools:
            data["tools"] = deepcopy(tools)
        elif tools:
            raise ValueError("compression: tools must be absent")
        body_hash = safe_payload_hash(data)
        emit("trae_llm_request", role=self.policy.role, capture_boundary="client_body_object", actual_model=data["model"],
             reference_model=self.policy.reference_model, payload=data)
        telemetry = self.llm.telemetry
        telemetry.on_request(data)
        attempts = 0
        try:
            with OpenAI(api_key=prepared.get("api_key"), base_url=prepared.get("api_base"),
                        timeout=prepared.get("timeout"), max_retries=0) as client:
                def one_attempt():
                    nonlocal attempts
                    attempts += 1
                    emit("trae_transport_attempt", role=self.policy.role, attempt=attempts, body_sha256=body_hash,
                         actual_model=data["model"], reference_model=self.policy.reference_model)
                    # Always create fresh input so a provider client cannot
                    # mutate the request reused by the next attempt.
                    completion = client.chat.completions.create(**deepcopy(data))
                    if completion is None:
                        return None
                    return completion.model_dump()
                raw = self._send_with_retry(one_attempt, 'no response from api')
            if not raw:
                raise RuntimeError("no response from api")
        except Exception as exc:
            telemetry.on_error(exc)
            emit("trae_transport_result", role=self.policy.role, actual_model=data["model"],
                 reference_model=self.policy.reference_model, attempts=attempts, status="error")
            raise
        # Telemetry and downstream parsing deliberately live outside retry.
        emit("trae_provider_response", response=raw)
        emit("trae_transport_result", role=self.policy.role, actual_model=data["model"],
             reference_model=self.policy.reference_model, attempts=attempts, status="responded")
        from ..token_tracking import provider_token_response
        with provider_token_response(raw):
            telemetry.on_response(ModelResponse(**raw), provider_info=self.llm._provider_info)
        self.last_finish_reasons = [choice["finish_reason"] for choice in raw["choices"]]
        choice = raw["choices"][0]
        answer = choice["message"]
        # Preserve malformed/missing usage: source parsing raises rather than
        # converting an invalid envelope into an unknown-usage skip.
        usage = raw["usage"]
        return answer, choice["finish_reason"], usage

    def _trae_subscription(self, messages, tools):
        from litellm import responses
        from litellm.types.llms.openai import ResponsesAPIResponse
        from openhands.sdk.llm.options.responses_options import select_responses_options
        from ..compat.subscription_contract import project_messages, capability_report

        if self.policy.role == 'compression' and tools:
            raise ValueError('compression: tools must be absent')
        instructions, items, resp_tools = responses_payload(project_messages(messages), tools)
        payload = {'instructions': instructions, 'input': items, 'tools': resp_tools,
                   'stream': True, 'store': False}
        if self.llm.reasoning_effort:
            payload['reasoning'] = {'effort': self.llm.reasoning_effort}
        body_hash = safe_payload_hash(payload)
        wire_model = self.llm._provider_info.model
        emit('trae_llm_request', role=self.policy.role, actual_model=wire_model,
             reference_model=self.policy.reference_model, protocol='responses',
             transport_conformance='adapted', capture_boundary='adapter_body_object',
             capability_report=capability_report(self.policy, wire_model,
                                                 reasoning_effort=self.llm.reasoning_effort), payload=payload)
        telemetry = self.llm.telemetry
        telemetry.on_request(payload)
        attempts = 0
        try:
            def one_attempt():
                nonlocal attempts
                attempts += 1
                options = select_responses_options(self.llm, {'stream': True}, include=None, store=False)
                # Allow only reviewed SDK subscription fields. All reference
                # omissions are explicit in capability_report, not drop_params.
                extra = options.get('extra_body') or {}
                options['extra_body'] = {key: deepcopy(value) for key, value in extra.items()
                                         if key in {'store'}}
                for key in ('previous_response_id', 'max_output_tokens', 'temperature',
                            'stop', 'n', 'reasoning', 'include', 'prompt_cache_retention'):
                    options.pop(key, None)
                options.update(stream=True, store=False)
                if 'reasoning' in payload:
                    options['reasoning'] = deepcopy(payload['reasoning'])
                kwargs = self.llm._build_responses_call_kwargs(
                    deepcopy(items), deepcopy(instructions), deepcopy(resp_tools), options)
                kwargs.update(num_retries=0, max_retries=0)
                # Secrets are registered after SDK credential refresh, before
                # errors can enter event artifacts. Headers are never captured.
                register_secrets(kwargs.get('api_key'))
                credentials = getattr(self.llm, '_subscription_credentials', None)
                if credentials is not None:
                    register_secrets(credentials.access_token, credentials.refresh_token)
                emit('trae_transport_attempt', role=self.policy.role, attempt=attempts,
                     body_sha256=body_hash, actual_model=wire_model,
                     protocol='responses', transport_conformance='adapted')
                raw = _drain_response(responses(**kwargs))
                normalized = responses_answer(raw)
                # Responses tool-only turns have no text item. Trae's history
                # serializer requires text to be a string even on tool turns.
                # Normalize only adapted repair history; retain raw evidence
                # and the exact/compression response contracts.
                answer = normalized[0]
                if (self.policy.role == 'repair' and answer.get('tool_calls')
                        and answer.get('content') is None):
                    answer['content'] = ''
                return raw, normalized
            result = self._send_with_retry(one_attempt, 'no response from subscription api')
            if result is None:
                raise RuntimeError('no response from subscription api')
            raw, normalized = result
        except Exception as exc:
            telemetry.on_error(exc)
            emit('trae_transport_result', role=self.policy.role, attempts=attempts, status='error',
                 transport_conformance='adapted')
            raise
        emit('trae_provider_response', response=raw)
        emit('trae_transport_result', role=self.policy.role, attempts=attempts, status='responded',
             transport_conformance='adapted')
        from ..token_tracking import provider_token_response
        with provider_token_response(raw):
            telemetry.on_response(ResponsesAPIResponse(**raw) if isinstance(raw, dict) else raw,
                                  provider_info=self.llm._provider_info)
        self.last_finish_reasons = [normalized[1]]
        return normalized

    def _subscription(self, messages, tools):
        from litellm import responses
        from litellm.types.llms.openai import ResponsesAPIResponse
        from openhands.sdk.llm.options.responses_options import select_responses_options

        instructions, items, resp_tools = responses_payload(messages, tools)
        telemetry = self.llm.telemetry
        telemetry.on_request({"instructions": instructions, "input": items, "tools": resp_tools})
        try:
            options = select_responses_options(self.llm, {"stream": True}, include=None, store=False)
            # Contract fields must win over optional SDK extra_body defaults.
            extra = deepcopy(options.get("extra_body") or {})
            for key in ("instructions", "input", "tools", "previous_response_id", "stream", "store"):
                options.pop(key, None)
                extra.pop(key, None)
            options.update(stream=True, store=False, extra_body=extra)
            # Reuse SDK OAuth refresh, provider routing and subscription headers.
            kwargs = self.llm._build_responses_call_kwargs(items, instructions, resp_tools, options)
            raw = _drain_response(responses(**kwargs))
            result = responses_answer(raw)
            if isinstance(raw, dict):
                raw = ResponsesAPIResponse(**raw)
            telemetry.on_response(raw, provider_info=self.llm._provider_info)
            return result
        except Exception as exc:
            telemetry.on_error(exc)
            raise
