"""Compression prompt ported from Trae traj_analyzer.py."""

SYSTEM_PROMPT = """
You will analyze and compress a given step in a trajectory of an AI agent solving a software bug.

In the trajectory, each step is marked in <step id="..."></step>.
The agent will think in <think>, call external tools as marked in <call tool="..."></call>. Its result is marked in <result></result> within the <step> tag.

Your job is to compress the text within the given id to avoid harming efficiency, typically shortening it to 20%-50% of the original length.
Meanwhile, keep the compressed text useful such that you are able to continue the trajectory as close as the original path.

- You should ONLY remove redundant texts, which are either irrelevant to future steps or duplicated by other texts in the trajectory.
- Replace the text to remove to "..." and a short takeaway, e.g. "... (same as the content below)".
- You should keep the original structure unchanged, e.g., XML tags, Python indentation and line numbers.
- Again, keep useful details in the original content unchanged, e.g., XML tags, Python indentation and line numbers.

Typical examples:
- If the step opens a huge file but only one part is necessary for future steps, replace other parts to "... (unrelated function XXX, YYY)".
- If the step runs a verbose test script and everything goes fine, replace the verbose part to "... (expected output)".
- If the step uses str_replace_editor to modify a file and the content can be inferred by the content after it, replace the tool call argument to "... (see results below)".

You should only process the text within the <step> tag with the given id. STOP OUTPUT IMMEDIATELY AFTER </step>.
""".strip()


def build_compression_messages(context: str, step_index: int, policy, *, use_caching: bool = True) -> list[dict]:
    system = SYSTEM_PROMPT
    if policy.bypass_filter:
        system = system.replace("think", "talk").replace("agent", "engineer")
    if use_caching:
        system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": context + f"\n\nNow, compress the step {step_index}."},
        {"role": "assistant", "content":
         f'Sure. Here is the compressed content of step {step_index}: <step id="{step_index}">'},
    ]


def build_compression_window(mgr, step_index: int, policy, *, ctx_before: int, ctx_after: int, show_ctx: bool) -> str:
    window = [mgr.extract_step_into_traj(i, bypass_filter=policy.bypass_filter)
              for i in range(step_index - ctx_before, step_index + ctx_after + 1)]
    if not show_ctx:
        window = [part if pos == ctx_before else "" for pos, part in enumerate(window)]
    return "\n".join(window)


def build_adapted_compression_messages(context: str, step_index: int, policy) -> list[dict]:
    messages = build_compression_messages(context, step_index, policy, use_caching=False)[:2]
    messages[1]['content'] += (
        f'\nReturn exactly one complete <step id="{step_index}">...</step> wrapper. '
        'Return no preamble, markdown fences or text outside the wrapper.')
    return messages
