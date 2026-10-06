"""Literal Trae analyzer for raw MessageManager history (D09–D11).

The generic SDK event condenser deliberately uses a separate implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
import random

from ..diet.core import _token_encoding
from ..diet.strategies import lingua
from ..events import emit
from .audit import audit_context


@dataclass(frozen=True, slots=True)
class CompressionResult:
    status: str
    content: str | None = None
    reason: str | None = None


def count_token(text):
    return len(_token_encoding().encode(text))


def validate_exact_diet(config):
    for name, expected in (("minimum_reduction_tokens", 400), ("minimum_reduction_ratio", .20)):
        if getattr(config, name) != expected:
            raise ValueError(f"{name} must be {expected} in the exact Trae profile")
    if config.ctx_before == 0 and config.ctx_after > 0:
        raise ValueError("ctx_before=0 with ctx_after>0 is unsupported in the exact Trae profile (negative target edge)")


def parse_response(answer, finish_reasons, usage, record_usage):
    """Keep source usage ordering, partition heuristic and exception behavior."""
    if usage['completion_tokens'] is None:
        return CompressionResult('skipped', reason='completion_usage_none')
    record_usage(usage)
    content, closing, _ = answer['content'].partition('</step>')
    if not closing and 'stop' not in finish_reasons:
        return CompressionResult('skipped', reason='missing_close_without_stop')
    if '<step' in content[:200]:
        content = content.partition('<step')[2]
        if '>' in content[:20]:
            content = content.partition('>')[2]
    return CompressionResult('parsed', content)


def analyze_turn(mgr, config, metrics, compressor, policy):
    turn = mgr.count_turn()
    idx = turn - 1 - config.ctx_after
    with audit_context(logical_turn=turn, step_index=idx, mode=config.mode):
        before = dict(metrics.rejected)
        erased = metrics.erase_count
        try:
            _analyze_turn(mgr, config, metrics, compressor, policy)
        except Exception as error:
            emit('diet_decision', status='fatal', reason=type(error).__name__)
            raise
        if metrics.erase_count > erased:
            emit('diet_decision', status='accepted', reason='source_acceptance')
        else:
            reason = next((key for key, value in metrics.rejected.items() if value > before.get(key, 0)), None)
            if reason:
                emit('diet_decision', status='rejected', reason=reason)


def _analyze_turn(mgr, config, metrics, compressor, policy):
    """Called once after a completed, nonterminal repair turn; errors propagate."""
    if not config.enabled or config.mode == 'skip':
        emit('diet_decision', logical_turn=mgr.count_turn(), status='skipped', reason='disabled_or_skip')
        return
    turn = mgr.count_turn()
    if getattr(mgr, 'last_analyzed_turn', None) == turn:
        emit('diet_decision', status='skipped', reason='already_analyzed')
        return
    # This marker is persisted with history by the harness, including skip/reject.
    mgr.last_analyzed_turn = turn
    if turn < config.ctx_before + config.ctx_after:
        emit('diet_decision', logical_turn=turn, status='skipped', reason='insufficient_window')
        return
    idx = turn - 1 - config.ctx_after
    indices = list(range(idx - config.ctx_before, turn))
    window = [mgr.extract_step_into_traj(i) for i in indices]
    target = window[config.ctx_before]
    tokens = count_token(target)
    metrics.seen_tokens += tokens
    emit('diet_gate', logical_turn=turn, step_index=idx, window_indices=indices,
         gate_window=window, gate_target=target, gate_tokens=tokens, threshold_tokens=config.threshold_tokens, show_ctx=config.show_ctx)
    if tokens < config.threshold_tokens:
        metrics.reject('below_threshold')
        return
    if config.use_lz4:
        import lz4.frame
        x1 = len(lz4.frame.compress(''.join(window[-config.ctx_after:]).encode('utf-8')))
        x2 = len(lz4.frame.compress(''.join(window[-(config.ctx_after + 1):]).encode('utf-8')))
        size = len(target.encode('utf-8'))
        rate = 1 - max(0, x2 - x1) / size
        score = tokens * rate
        emit('diet_lz4_gate', step_index=idx, x1=x1, x2=x2, target_bytes=size,
             save_rate=rate, save_tokens=score)
        if score < config.threshold_tokens:
            metrics.reject('lz4_not_compressible')
            return
    metrics.analysis_count += 1
    emit('diet_analysis_started', mode=config.mode, step_index=idx)
    original = target
    if config.mode == 'ours':
        if compressor is None:
            raise ValueError('exact Agent Diet ours requires a compressor')
        if policy is None:
            raise ValueError('exact Agent Diet ours requires a compression role policy')
        bypass = [mgr.extract_step_into_traj(i, policy.bypass_filter) for i in indices]
        original = bypass[config.ctx_before]
        context = '\n'.join(s if config.show_ctx or j == config.ctx_before else ''
                            for j, s in enumerate(bypass))
        exact = getattr(compressor, 'compress_exact_result', None)
        if callable(exact):
            result = exact(context, step_index=idx)
        else:
            replacement = compressor.compress_step(original, context, step_index=idx)
            result = CompressionResult('parsed', replacement)
        emit('diet_parser_result', step_index=idx, status=result.status, reason=result.reason,
             compression_request_id=getattr(compressor, 'last_request_id', None),
             parsed_content=result.content, bypass_target=original, compressor_context=context)
        if result.status == 'skipped':
            metrics.reject(result.reason)
            return
        replacement = result.content
        old, new = count_token(original), count_token(replacement)
        emit('diet_reduction_gate', logical_turn=turn, step_index=idx, old_tokens=old, new_tokens=new,
             minimum_reduction_tokens=400, maximum_ratio=.8)
        if not (old - new >= 400 or new < .8 * old):
            metrics.reject('insufficient_reduction')
            return
    elif config.mode == 'delete':
        replacement, old, new = None, count_token(target), 0
    elif config.mode == 'random':
        encoding = _token_encoding()
        ids = encoding.encode(target)
        deletable = []
        for index, token in enumerate(ids):
            try:
                encoding.decode_single_token_bytes(token).decode()
            except UnicodeDecodeError:
                pass
            else:
                deletable.append(index)
        deleted = random.sample(deletable, min(int(len(ids) * (1 - config.lingua_ratio)), len(deletable)))
        remaining = [t for index, t in enumerate(ids) if index not in deleted]
        replacement, old, new = encoding.decode(remaining), len(ids), len(remaining)
    elif config.mode == 'lingua':
        replacement = lingua(target, ratio=config.lingua_ratio)
        old, new = count_token(target), count_token(replacement)
    else:
        raise ZeroDivisionError(f'wtf mode: {config.mode}')
    metrics.erase_count += 1
    metrics.erase_in_tokens += old
    metrics.erase_out_tokens += new
    mgr.perform_erase_step(idx, replacement, original)
    emit('diet_step_change', status='accepted', mode=config.mode, step_index=idx,
         logical_step_ids=[idx], sdk_event_ids=[], logical_turn=turn, before_text=original, after_text=replacement,
         before_token_estimate=old, after_token_estimate=new,
         replacement_message=mgr.steps[idx][0], next_repair_messages=mgr.format_messages())
