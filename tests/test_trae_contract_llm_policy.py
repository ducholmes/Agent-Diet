from dataclasses import asdict
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch
from openhands_adapter.config import OpenHandsConfig, AgentDietConfig, RunConfig
from openhands_adapter.compat.trae_contract import TOOLS, SYS_PROMPT
from openhands_adapter.compat.trae_llm_policy import TraeLLMPolicy, validate_trae_run
from openhands_adapter.openhands.llm import build_llm
from trae_reference import wrapper_oracle
from trae_transport_fixture import CAPABILITIES, capture_transport


class PolicyTests(unittest.TestCase):
    def test_two_roles_two_branches_match_source_wrapper_at_http(self):
        messages = [{'role': 'system', 'content': SYS_PROMPT}, {'role': 'user', 'content': 'issue'}]
        for role in ('repair', 'compression'):
            for reference in ('claude4-sonnet', 'gpt-5-mini-2025-08-07'):
                with self.subTest(role=role, reference=reference):
                    tools = TOOLS if role == 'repair' else []
                    expected = []
                    wrapper = wrapper_oracle(lambda **kw: expected.append(kw) or SimpleNamespace(model_dump=lambda: {}), lambda n: None)
                    params = {'temperature': 0.0, 'n': 1}
                    if role == 'compression': params['stop'] = '</step>'
                    wrapper(reference, messages, tools, params)
                    expected[0]['model'] = 'gpt-5.6-sol'
                    with capture_transport(role, reference) as (transport, seen, sleeps, clients):
                        transport(messages, tools)
                    self.assertEqual(seen, expected)
                    self.assertEqual(sleeps, [])
                    self.assertEqual(clients[0]['max_retries'], 0)
                    for key in ('seed', 'top_p', 'tool_choice', 'max_output_tokens'):
                        self.assertNotIn(key, seen[0])

    def test_reference_predicate_is_literal_and_policies_are_immutable(self):
        self.assertFalse(TraeLLMPolicy('compression', 'gpt-5.6-sol').bypass_filter)
        policy = TraeLLMPolicy('repair', 'claude4-sonnet')
        policy.params()['reasoning_effort'] = 'high'
        self.assertNotIn('reasoning_effort', policy.params())
        with self.assertRaises(AttributeError): policy.reference_model = 'changed'

    def test_process_roundtrip_preserves_explicit_inherit_and_roles(self):
        config = RunConfig.from_mapping({'openhands': {'auth': 'api-key', 'repair_reference_model': 'other-repair',
            'trae_capabilities': list(CAPABILITIES)}, 'agentdiet': {'compressor_model': 'inherit',
            'compressor_reference_model': 'gpt-5-mini-custom'}})
        worker_oh = OpenHandsConfig.from_mapping(asdict(config.openhands))
        worker_diet = AgentDietConfig.from_mapping(asdict(config.agentdiet))
        repair, compression = validate_trae_run(worker_oh, worker_diet)
        self.assertEqual(repair.reference_model, 'other-repair')
        self.assertEqual(compression.reference_model, 'gpt-5-mini-custom')
        self.assertTrue(worker_diet.compressor_model_explicit)
        self.assertEqual(RunConfig.from_mapping(config.to_dict()).to_dict(), config.to_dict())

    def test_environment_role_models(self):
        with patch.dict(os.environ, {'OPENHANDS_REPAIR_REFERENCE_MODEL': 'repair-ref',
             'AGENTDIET_COMPRESSOR_REFERENCE_MODEL': 'compress-ref', 'AGENTDIET_COMPRESSOR_MODEL': 'inherit',
             'OPENHANDS_TRAE_CAPABILITIES': ','.join(CAPABILITIES)}, clear=True):
            self.assertEqual(OpenHandsConfig.from_env().repair_reference_model, 'repair-ref')
            self.assertEqual(OpenHandsConfig.from_env().trae_capabilities, CAPABILITIES)
            self.assertEqual(AgentDietConfig.from_env().compressor_reference_model, 'compress-ref')
            self.assertTrue(AgentDietConfig.from_env().compressor_model_explicit)

    def test_cli_role_reference_overrides_roundtrip(self):
        from openhands_adapter.cli import build_parser, _override
        args = build_parser().parse_args(['--model', 'openai/gpt-5.6-sol', '--auth', 'api-key',
            '--repair-reference-model', 'claude4-sonnet-custom', '--compressor-model', 'inherit',
            '--compressor-reference-model', 'gpt-5-mini-custom', '--trae-capabilities', ','.join(CAPABILITIES)])
        config = _override(RunConfig(), args, None)
        worker = RunConfig.from_mapping(config.to_dict())
        repair, compression = validate_trae_run(worker.openhands, worker.agentdiet)
        self.assertEqual(repair.reference_model, 'claude4-sonnet-custom')
        self.assertEqual(compression.reference_model, 'gpt-5-mini-custom')
        self.assertEqual(worker.openhands.model, 'openai/gpt-5.6-sol')
        self.assertTrue(worker.agentdiet.compressor_model_explicit)

    def test_preflight_rejects_unsupported_semantics_and_implicit_inherit(self):
        with self.assertRaisesRegex(ValueError, 'repair: subscription'):
            validate_trae_run(OpenHandsConfig(), AgentDietConfig(mode='skip'))
        for missing in CAPABILITIES:
            config = OpenHandsConfig(auth='api-key', trae_capabilities=tuple(c for c in CAPABILITIES if c != missing))
            # Different reference branches collectively require every capability.
            reference = 'claude4-sonnet' if missing == 'stop' else 'gpt-5-mini-2025-08-07'
            diet = AgentDietConfig(compressor_model_explicit=True, compressor_reference_model=reference)
            if missing == 'reasoning_effort': config.repair_reference_model = reference
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, missing):
                validate_trae_run(config, diet)
        with self.assertRaisesRegex(ValueError, 'declare compressor_model'):
            validate_trae_run(OpenHandsConfig(auth='api-key', trae_capabilities=CAPABILITIES), AgentDietConfig())
        self.assertEqual(validate_trae_run(OpenHandsConfig(reference_profile='generic'), AgentDietConfig()), (None, None))
        with self.assertRaisesRegex(ValueError, 'generic'):
            OpenHandsConfig(reasoning_effort='high').validate()
        with self.assertRaisesRegex(ValueError, 'Qwen streaming'):
            TraeLLMPolicy('compression', 'qwen3-235b-a22b-instruct-2507')

    def test_llm_pins_exact_settings_per_role(self):
        for role, reference, effort in [('repair', 'claude4-sonnet', None), ('compression', 'gpt-5-mini-2025-08-07', 'low')]:
            policy = TraeLLMPolicy(role, reference)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}), patch('openhands_adapter.openhands.llm._load_sdk_llm') as factory:
                build_llm(OpenHandsConfig(auth='api-key', trae_capabilities=CAPABILITIES), policy=policy)
                kw = factory.return_value.call_args.kwargs
            self.assertEqual(kw['reasoning_effort'], effort)
            self.assertEqual(kw['max_output_tokens'], 8192)
            self.assertEqual(kw['num_retries'], 0)
            self.assertFalse(kw['drop_params'])
            self.assertFalse(kw['caching_prompt'])
