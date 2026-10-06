"""Independent AST oracle: no provider/config imports from the Trae runner."""
from __future__ import annotations
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shlex
import time
from types import SimpleNamespace
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "artifact/artifact/code/trae_agent"


def assert_reference_hashes():
    manifest = json.loads((ROOT / "src/openhands_adapter/compat/reference_hashes.json").read_text())
    for relative, expected in manifest["sha256"].items():
        actual = hashlib.sha256((REFERENCE / relative).read_bytes()).hexdigest()
        assert actual == expected, f"Frozen Trae source changed: {relative}"
    return manifest


def load_nodes(relative, names, namespace=None):
    assert_reference_hashes()
    source = (REFERENCE / relative).read_text()
    nodes = []
    for node in ast.parse(source).body:
        name = getattr(node, "name", None)
        if name in names or isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            nodes.append(node)
    ns = {"json": json, "shlex": shlex, "time": time, **(namespace or {})}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(REFERENCE / relative), "exec"), ns)
    return ns


def oracle():
    ns = load_nodes("utils/agent_util.py", {"remove_patches_to_tests", "TIME_OUT_LABEL"})
    return load_nodes("agents/expert.py", {"TOOLS", "SYS_PROMPT", "INIT_USER_PROMPT", "MessageManager", "parse_tool_response", "Expert"}, ns)


def run_expert(responses, sandbox, *, profile="trae_verified", issue="Lỗi\n  preserved\ttext", hook=None):
    ns = oracle()
    requests = []
    script = iter(deepcopy(responses))
    def fake_llm(model, messages, tools, options):
        requests.append((deepcopy(messages), deepcopy(tools)))
        answer, reason, usage = next(script)
        return [answer], [reason], usage
    ns.update(FIX_MODEL="fake", get_llm_response=fake_llm, maybe_perform_analysis_step=hook or (lambda mgr: None))
    expert = ns["Expert"](sandbox)
    turns, reminder = {"trae_verified": (50, True), "trae_multiswe": (100, False)}[profile]
    trajectory = {"metrics": {"tot_step": 0, "cost_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}, "result": {}}
    _, patch, _ = expert.run("/testbed", {"problem_statement": issue, "max_turn": turns, "turn_reminder": reminder}, trajectory)
    return requests, expert.mgr.steps, trajectory["result"]["gen"], patch


def response(*calls, content=None, usage=1):
    answer = {"role": "assistant", "content": content}
    if calls:
        answer["tool_calls"] = [{"id": f"call-{i}", "type": "function", "function": {"name": name, "arguments": arguments}}
                                for i, (name, arguments) in enumerate(calls)]
    return answer, "tool_calls" if calls else "stop", {"completion_tokens": usage}


def wrapper_oracle(create, sleep):
    """Run the actual frozen OpenAI wrapper against a stub provider client."""
    ns = load_nodes("utils/llm_polytool.py", {"HashKey", "NullCache", "send_request_openai"}, {
        "hashlib": hashlib, "Optional": Optional,
        "openai": SimpleNamespace(OpenAI=lambda **kw: SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))),
        "time": SimpleNamespace(sleep=sleep), "print": lambda *a: None,
    })
    ns["llm_cache_chat"] = ns["NullCache"]()
    return ns["send_request_openai"]("https://mock.invalid/v1", "fake")


def compression_oracle(mgr, reference_model, *, show_ctx=True, ctx_before=1, ctx_after=2):
    """Capture caller messages/kwargs from perform_analysis_step_ours via AST."""
    calls = []
    def capture(model, messages, tools, kwargs):
        calls.append((model, deepcopy(messages), deepcopy(tools), deepcopy(kwargs)))
        # Source returns before acceptance/parser when usage is unknown.
        return [{"content": "unused"}], ["stop"], {"completion_tokens": None}
    ns = load_nodes("agents/traj_analyzer.py", {"SYS_PROMPT", "perform_analysis_step_ours"}, {
        "MessageManager": type(mgr), "MODEL": reference_model, "BYPASS_FILTER": "gpt-5-" in reference_model,
        "SHOW_CTX": show_ctx, "N_CTX_BEFORE": ctx_before, "N_CTX_AFTER": ctx_after,
        "get_llm_response": capture, "print": lambda *a: None,
    })
    ns["perform_analysis_step_ours"](mgr)
    return ns["SYS_PROMPT"], calls[0]


def diet_oracle(config, *, answers=None, reference_model='gpt-5-mini-2025-08-07',
                encoding=None, rng=None, lingua=None):
    """Frozen analyzer functions executed independently, with provider-only stubs."""
    import tiktoken
    import lz4.frame
    import random
    calls = []
    def fake_llm(model, messages, tools, options):
        calls.append((model, deepcopy(messages), deepcopy(tools), deepcopy(options)))
        if isinstance(answers, BaseException):
            raise answers
        return deepcopy(answers or ([{'content': 'short</step>'}], ['stop'],
                       {'total_tokens': 13, 'prompt_tokens': 10, 'completion_tokens': 3}))
    names = {'SYS_PROMPT', 'count_token', 'count_comp', 'should_perform_analysis',
             'maybe_perform_analysis_step', 'perform_analysis_step_ours',
             'perform_analysis_step_delete', 'perform_analysis_step_random_drop',
             'perform_analysis_step_llmlingua'}
    ns = load_nodes('agents/traj_analyzer.py', names, {
        'MessageManager': oracle()['MessageManager'], 'MODE': config.mode,
        'MODEL': reference_model, 'BYPASS_FILTER': 'gpt-5-' in reference_model,
        'N_CTX_BEFORE': config.ctx_before, 'N_CTX_AFTER': config.ctx_after,
        'SHOW_CTX': config.show_ctx, 'USE_LZ4': config.use_lz4,
        'THRESHOLD_TOKENS': config.threshold_tokens, 'LINGUA_RATIO': config.lingua_ratio,
        '_token_encoding': encoding or tiktoken.encoding_for_model('gpt-4o'),
        'lz4': lz4, 'random': rng or random, 'get_llm_response': fake_llm,
        'get_lingua': lambda: lingua, 'print': lambda *args: None,
    })
    return ns, calls


def diet_metrics():
    return dict.fromkeys(('seen_tokens', 'analysis_count', 'analysis_cost_tokens',
                         'analysis_prompt_tokens', 'analysis_completion_tokens',
                         'erase_tot_count', 'erase_in_tokens', 'erase_out_tokens'), 0)
