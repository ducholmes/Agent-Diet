"""Run prepared cases sequentially with OpenHands and Agent Diet validation."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path

from .config import RunConfig, resolve_output_root
from .events import emit
from .input_loader import CaseSpec, EnvironmentSpec, discover_cases, expand_target_commands, load_case
from .openhands.llm import build_llm
from .openhands.process import run_worker
from .openhands.prompts import DEFAULT_TASK_PROMPT, build_user_prompt
from .progress import log, stage
from .workflow.models import RunResult
from .workflow.runner import run_case


DEFAULT_PROMPT = DEFAULT_TASK_PROMPT


def build_parser() -> argparse.ArgumentParser:
    """Native-runner-style controls plus Agent Diet-specific knobs."""
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("input_path", nargs="?", type=Path, help="prepared input root")
    inputs.add_argument("--input", dest="input_option", type=Path, help="prepared input root")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case", nargs="+", action="extend", metavar="CASE",
                           help="one or more case IDs, relative IDs, or unambiguous prefixes; run sequentially")
    selection.add_argument("--all-cases", action="store_true",
                           help="run every prepared case sequentially in sorted order")
    parser.add_argument("--config", type=Path, help="JSON RunConfig; CLI options take precedence")
    parser.add_argument("--output", type=Path,
                        help="run subdirectory inside Agent-Diet/output, e.g. exp1")
    parser.add_argument("--keep-workspaces", "--keep-git", action="store_true")
    parser.add_argument("--verbose", action="store_true")

    agent = parser.add_argument_group("OpenHands agent")
    agent.add_argument("--model", required=True, help="OpenHands model for this run")
    agent.add_argument("--auth", choices=("subscription", "api-key"))
    agent.add_argument("--base-url", help="OpenAI-compatible API base URL")
    agent.add_argument("--api-key-env", help="e.g. OPENROUTER_API_KEY")
    agent.add_argument("--subscription-vendor", help="default: openai")
    agent.add_argument("--subscription-auth-method", choices=("browser", "device_code"))
    agent.add_argument("--force-login", action="store_true")
    agent.add_argument("--login-only", action="store_true")
    agent.add_argument("--reasoning-effort")
    agent.add_argument("--max-iterations", type=int)
    agent.add_argument("--openhands-timeout", "--agent-timeout", dest="agent_timeout", type=int)
    agent.add_argument("--command-timeout", type=int)
    agent.add_argument("--prompt")
    agent.add_argument("--prompt-file", type=Path)

    environment = parser.add_argument_group("Prepared runtime override")
    environment.add_argument("--runtime", help="override config.environment.runtime")
    environment.add_argument("--image", help="override config.environment.image")

    validation = parser.add_argument_group("Validation")
    validation.add_argument("--timeout", "--validation-timeout", dest="validation_timeout", type=int)
    agent.add_argument("--output-limit-bytes", type=int, help="Repair tool output character limit (default: 40000); validation keeps full output")

    diet = parser.add_argument_group("Agent Diet")
    diet.add_argument("--diet-mode", choices=("skip", "ours", "delete", "random", "lingua"))
    diet.add_argument("--disable-diet", action="store_true")
    diet.add_argument("--diet-threshold", type=int, metavar="TOKENS")
    diet.add_argument("--ctx-before", type=int)
    diet.add_argument("--ctx-after", type=int)
    diet.add_argument("--hide-context", action="store_true")
    diet.add_argument("--use-lz4", action="store_true")
    diet.add_argument("--lingua-ratio", type=float)
    diet.add_argument("--compressor-model")
    diet.add_argument("--min-reduction-tokens", type=int, help="Minimum token saving for mode ours only")
    diet.add_argument("--min-reduction-ratio", type=float, help="Minimum reduction ratio for mode ours only")
    diet.add_argument("--discard-raw-events", action="store_true")
    return parser


def _override(config: RunConfig, args: argparse.Namespace, input_root: Path | None) -> RunConfig:
    """Apply only explicit CLI values over JSON/environment defaults."""
    oh = config.openhands
    for field, value in {
        "model": args.model, "auth": args.auth, "base_url": args.base_url,
        "api_key_env": args.api_key_env, "subscription_vendor": args.subscription_vendor,
        "reasoning_effort": args.reasoning_effort, "max_iterations": args.max_iterations,
    }.items():
        if value is not None:
            oh = replace(oh, **{field: value})
    diet = config.agentdiet
    for field, value in {
        "mode": args.diet_mode, "threshold_tokens": args.diet_threshold,
        "ctx_before": args.ctx_before, "ctx_after": args.ctx_after,
        "lingua_ratio": args.lingua_ratio, "compressor_model": args.compressor_model,
        "minimum_reduction_tokens": args.min_reduction_tokens,
        "minimum_reduction_ratio": args.min_reduction_ratio,
    }.items():
        if value is not None:
            diet = replace(diet, **{field: value})
    if args.disable_diet: diet = replace(diet, enabled=False)
    if args.hide_context: diet = replace(diet, show_ctx=False)
    if args.use_lz4: diet = replace(diet, use_lz4=True)
    if args.discard_raw_events: diet = replace(diet, keep_raw_events=False)
    workflow = replace(
        config.workflow,
        input_root=input_root or config.workflow.input_root,
        output_root=resolve_output_root(args.output if args.output is not None else config.workflow.output_root),
        keep_workspaces=args.keep_workspaces or config.workflow.keep_workspaces,
        agent_timeout_seconds=args.agent_timeout or config.workflow.agent_timeout_seconds,
        command_timeout_seconds=args.command_timeout or config.workflow.command_timeout_seconds,
        validation_timeout_seconds=args.validation_timeout or config.workflow.validation_timeout_seconds,
        output_limit_bytes=args.output_limit_bytes or config.workflow.output_limit_bytes,
    )
    result = replace(config, openhands=oh, agentdiet=diet, workflow=workflow)
    result.validate()
    return result


def _prompt(args: argparse.Namespace) -> str:
    if args.prompt and args.prompt_file:
        raise ValueError("Set either --prompt or --prompt-file, not both")
    if args.prompt_file:
        return args.prompt_file.read_text(encoding="utf-8")
    return args.prompt or DEFAULT_PROMPT


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_root = args.input_option or args.input_path
    try:
        base = RunConfig.load(args.config) if args.config else RunConfig.from_env()
        config = _override(base, args, input_root.expanduser().resolve() if input_root else None)
        prompt = _prompt(args)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.subscription_auth_method:
        os.environ["OPENHANDS_SUBSCRIPTION_AUTH_METHOD"] = args.subscription_auth_method
    if args.force_login:
        os.environ["OPENHANDS_SUBSCRIPTION_FORCE_LOGIN"] = "1"
    if args.verbose:
        print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), file=os.sys.stderr)
    if args.login_only:
        build_llm(config.openhands)
        print("OpenHands authentication is ready")
        return 0
    if config.workflow.input_root is None:
        raise SystemExit("input root is required (positional path or --input)")
    try:
        if args.all_cases:
            cases = discover_cases(config.workflow.input_root)
        elif args.case:
            # Resolve all selectors before running and avoid rerunning duplicate matches.
            selected = {}
            for selector in args.case:
                case = load_case(config.workflow.input_root, selector)
                selected.setdefault(case.relative_id, case)
            cases = tuple(selected.values())
        else:
            cases = (load_case(config.workflow.input_root),)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    batch = args.all_cases or len(cases) > 1
    results = []
    all_resolved = True
    for index, case in enumerate(cases, 1):
        if batch:
            log(f"Case {index}/{len(cases)}: {case.relative_id}")
        try:
            result = _run_selected_case(case, config, args, prompt)
        except Exception as exc:
            if not batch:
                raise
            all_resolved = False
            log(f"Case {case.relative_id} failed: {type(exc).__name__}: {exc}")
            results.append({"case_id": case.case_id, "relative_id": case.relative_id,
                            "resolved": False, "error": {"type": type(exc).__name__, "message": str(exc)}})
            continue
        all_resolved = all_resolved and result.resolved
        results.append(result.to_dict())
    payload = {"results": results, "all_resolved": all_resolved} if batch else results[0]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all_resolved else 1


def _run_selected_case(case: CaseSpec, config: RunConfig, args: argparse.Namespace, prompt: str) -> RunResult:
    if args.runtime or args.image:
        case = replace(case, environment=EnvironmentSpec("image", args.runtime or case.environment.runtime, args.image or case.environment.image))
    stage(
        "run",
        "started",
        case=case.case_id,
        agent="OpenHands",
        model=config.openhands.model,
        auth=config.openhands.auth,
        compressor_mode=config.agentdiet.mode,
        compressor_model=config.openhands.model if config.agentdiet.compressor_model == "inherit" else config.agentdiet.compressor_model,
    )
    def agent_runner(workspace: Path, output: Path) -> str:
        execution_plan = {
            phase: [{"argv": list(spec.argv), "cwd": spec.cwd} for spec in commands]
            for phase, commands in (
                ("setup", case.setup_commands),
                ("build", case.build_commands),
                ("target_test", [spec for spec, _ in expand_target_commands(case)]),
                ("regression_test", case.regression_test_commands),
            )
        }
        result = run_worker(
            workspace, build_user_prompt(
                workspace.resolve(), problem_statement=case.problem_statement,
                instructions=prompt,
            ), output, image=case.environment.image,
            runtime=case.environment.runtime, execution_plan=execution_plan,
            openhands=config.openhands, diet=config.agentdiet, workflow=config.workflow,
        )
        emit(
            "agent_process",
            returncode=result.returncode,
            elapsed_seconds=round(result.elapsed_seconds, 3),
            timed_out=result.timed_out,
        )
        if result.timed_out:
            raise TimeoutError("OpenHands worker exceeded agent_timeout")
        if result.returncode:
            raise RuntimeError(f"OpenHands worker exited with code {result.returncode}")
        return result.response
    result = run_case(case, config.workflow, agent_runner=agent_runner)
    stage("run", "finished", case=case.case_id, result="resolved" if result.resolved else "unresolved", validation=result.validation, reason=result.reason)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
