"""Conservative capability evidence for the pinned subscription serializer.

This is local serializer evidence, never a claim about live server semantics.
"""
from importlib.metadata import version


def capability_report(policy, model, *, reasoning_effort=None):
    capabilities = {}
    fields = policy.required_capabilities | ({'reasoning_effort'} if reasoning_effort else set())
    for field in sorted(fields):
        status = 'unverified'
        reason = 'No evidence of equivalent endpoint semantics'
        if field == 'reasoning_effort' and reasoning_effort:
            reason = 'Adapter explicitly serializes reasoning.effort; live endpoint semantics unverified'
        elif field in {'max_tokens', 'temperature', 'reasoning_effort'}:
            status = 'unsupported'
            reason = 'Pinned subscription options omit this parameter'
        elif field == 'stop':
            status = 'unsupported'
            reason = 'Adapted compression uses a complete step wrapper without server stop'
        elif field == 'cache_control':
            status = 'unsupported'
            reason = 'Chat cache blocks are removed when projecting Responses content'
        elif field == 'assistant_prefill':
            status = 'unsupported'
            reason = 'Adapted compression requests a wrapper instead of assistant continuation'
        capabilities[field] = {'status': status, 'reason': reason}
    return {'role': policy.role, 'actual_model': model,
            'reference_model': policy.reference_model,
            'evidence_scope': 'pinned_serializer; live endpoint not verified',
            'sdk_version': version('openhands-sdk'),
            'capabilities': capabilities,
            'deviations': [field for field, item in capabilities.items()
                           if item['status'] != 'supported']}


def project_messages(messages):
    """Convert only known text blocks, explicitly dropping chat cache metadata."""
    from copy import deepcopy
    result = deepcopy(messages)
    for message in result:
        content = message.get('content')
        if isinstance(content, list):
            if any(block.get('type') != 'text' or not isinstance(block.get('text'), str)
                   for block in content):
                raise ValueError('Trae adapted Responses requires text-only content blocks')
            message['content'] = ''.join(block['text'] for block in content)
    return result
