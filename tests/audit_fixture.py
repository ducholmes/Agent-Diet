"""End-to-end deterministic SDK/HTTP fixture, independent of provider availability."""
from copy import deepcopy
import json
import shlex
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
import httpx
from openai import OpenAI
from openhands.sdk import LLM
from openhands_adapter.config import RunConfig, OpenHandsConfig, AgentDietConfig, WorkflowConfig
from openhands_adapter.openhands.container import RepairContainer
from openhands_adapter.openhands.worker import run_worker
from openhands_adapter.openhands.prompts import build_user_prompt
from openhands_adapter.workflow.runner import run_case
from openhands_adapter.workflow.models import BaselineResult, ValidationResult
from openhands_adapter.workflow.patch import create_baseline
from openhands_adapter.workflow.trae_patch import capture_source_diff
from openhands_adapter.input_loader import load_case
from test_input_loader import _write_case
from trae_transport_fixture import CAPABILITIES, completion
from trae_reference import response


class FixtureSandbox:
    def __init__(self,container,secret=None):self.container=container;self.secret=secret;self.closed=0;self.last_output="Tool Call Status: 0\nok\n"
    def get_session(self):return self
    def close(self):self.closed+=1
    def execute(self,cmd):
        root=self.container.workspace
        if cmd.startswith('cat '):return self.last_output
        if 'prepare-fixture' in cmd:
            (root/'tests/test_a').write_text('agent test'+(' '+self.secret if self.secret else '')+'\n');(root/'reproducer.py').write_text('print("untracked")\n')
            self.last_output='Tool Call Status: 0\n'+('observed Unicode lỗi\n'*2500)
            return ''
        if 'fix-fixture' in cmd:
            (root/'source.c').write_text('new\n')
        self.last_output='Tool Call Status: 0\nok\n'
        return ''
    def get_diff(self):return capture_source_diff(self.container.workspace)


def exercise(root, *, raw=True, secret=None, script=None, fatal=False, audit_fault=False, real_image=None, adapted=False):
    root.mkdir(parents=True,exist_ok=True);_write_case(root,'case')
    (root/'case.failure.log').write_text('issue')
    if real_image:
        config_path=root/'case.debugging-framework.json'
        data=json.loads(config_path.read_text())
        data.update(setup=[['sh','-c','test "$(cat source.c)" = new && test "$(cat tests/test_a)" = "original test" && test ! -e reproducer.py']],
            build=[['true']], target_test=[{'command':['sh','-c','printf "FAILED {test_id}\\n"; exit 1'],
                'evidence_pattern':'^FAILED', 'failure_pattern':'^FAILED'}],
            regression_test=[['sh','-c','printf "1 passed\\n"']])
        data['environment']['image']=real_image
        config_path.write_text(json.dumps(data))
    case=load_case(root)
    output=root/'out'
    cfg=RunConfig(openhands=OpenHandsConfig(auth='api-key',model='openai/test',
        base_url='https://mock.invalid/v1',api_key_env='AUDIT_FIXTURE_KEY',trae_capabilities=CAPABILITIES),
        agentdiet=AgentDietConfig(compressor_model='inherit',compressor_model_explicit=True,keep_raw_events=raw),
        workflow=WorkflowConfig(output_root=output))
    if adapted:
        cfg.openhands = OpenHandsConfig(auth='subscription', transport_conformance='adapted')
    seen=[];compressions=[];sandboxes=[];current_workspace=None
    scripts=iter(deepcopy(script or [
        response(('bash','{"command":"prepare-fixture"}'),('think',' { "thought" : "keep raw" } '),
                 content='visible talk\n'+(secret or '')),
        response(('bash','{"command":"fix-fixture"}'),content=''),
        response(('think','{"thought":"done soon"}'),content=''),
        response(('task_done','{}'),content='done')]))
    def handler(request):
        data=json.loads(request.content)
        if data.get('tools'):
            seen.append(data);answer,reason,usage=next(scripts)
            if real_image:
                for call in answer.get('tool_calls',[]):
                    args=json.loads(call['function']['arguments'])
                    if args.get('command') == 'prepare-fixture':
                        args['command']=(f"printf 'agent test\\n' > {shlex.quote(str(current_workspace/'tests/test_a'))}; "
                            f"printf 'print(1)\\n' > {shlex.quote(str(current_workspace/'reproducer.py'))}; "
                            "python3 -c " + shlex.quote('print("observed Unicode lỗi\\n"*2500)'))
                    elif args.get('command') == 'fix-fixture':
                        args['command']=f"printf 'new\\n' > {shlex.quote(str(current_workspace/'source.c'))}"
                    call['function']['arguments']=json.dumps(args,ensure_ascii=False)
            raw_response=completion();raw_response['choices'][0].update(message=answer,finish_reason=reason)
        else:
            compressions.append(data);raw_response=completion()
            if fatal:raw_response['choices'][0]['message']['content']=None
        if adapted:
            from test_trae_contract_subscription import provider_response, sse_events
            if data.get('tools'):
                raw_response = provider_response((answer, reason, usage))
            else:
                raw_response = provider_response(response(content=None if fatal else '<step id="0">kept</step>'))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=''.join('data: '+json.dumps(e)+'\n\n' for e in sse_events(raw_response)))
        return httpx.Response(200,json=raw_response)
    def factory(**kwargs):return OpenAI(**kwargs,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    def make_llm(config,**kwargs):
        if adapted:
            from test_trae_contract_subscription import subscription_llm
            return subscription_llm()
        return LLM(model=config.model,api_key=secret or 'fixture-key',base_url=config.base_url,num_retries=0)
    def sandbox(container):
        value=FixtureSandbox(container,secret);sandboxes.append(value);return value
    def baseline(case,repair,validation,**kwargs):
        for path in (repair,validation):
            (path/'source.c').write_text('old\n');(path/'tests').mkdir(exist_ok=True)
            (path/'tests/test_a').write_text('original test\n')
        create_baseline(repair);return BaselineResult(True)
    def external(case,workspace,**kwargs):
        assert (workspace/'source.c').read_text()=='new\n'
        assert (workspace/'tests/test_a').read_text()=='original test\n'
        assert not (workspace/'reproducer.py').exists()
        if real_image:
            from openhands_adapter.workflow.validation import run_post_patch
            return run_post_patch(case,workspace,**kwargs)
        return ValidationResult(False,'target',reason='external_fail',status='failed')
    def worker(workspace,artifacts):
        nonlocal current_workspace
        current_workspace=workspace
        container=RepairContainer('fixture',workspace,'fixture-image')
        if real_image:
            from openhands_adapter.openhands.container import start
            from openhands_adapter.events import emit
            container=start(workspace,image=real_image,exact_trae=True)
            emit('repair_runtime', image=container.image, resolved_image_id=container.image_id, runtime=container.runtime)
        try:
            return run_worker(workspace,build_user_prompt(workspace if real_image else Path('/testbed'),problem_statement='issue'),artifacts,execution_plan={},
                container=container,openhands=cfg.openhands,diet=cfg.agentdiet,workflow=cfg.workflow)
        finally:
            if real_image:
                from openhands_adapter.openhands.container import remove
                remove(container)
    patches=[patch('openhands_adapter.workflow.runner.run_baseline',side_effect=baseline),
        patch('openhands_adapter.workflow.runner.run_post_patch',side_effect=external),
        patch('openhands_adapter.openhands.agent.build_llm',side_effect=make_llm),
        patch('openai.OpenAI',side_effect=factory)]
    if not real_image:
        patches.extend([patch('openhands_adapter.openhands.trae_session.stage_tools'),
                        patch('openhands_adapter.openhands.trae_session.TraeSandbox',side_effect=sandbox)])
    if adapted:
        import litellm
        from litellm.llms.custom_httpx.http_handler import HTTPHandler
        original = litellm.responses
        client = httpx.Client(transport=httpx.MockTransport(handler))
        http_handler = HTTPHandler(client=client)
        patches.append(patch('litellm.responses', side_effect=lambda **kw: original(**kw, client=http_handler)))
    from contextlib import ExitStack
    with ExitStack() as stack:
        if adapted: stack.callback(client.close)
        for item in patches:stack.enter_context(item)
        if audit_fault:
            stack.enter_context(patch('openhands_adapter.events._write_bytes',side_effect=OSError('fixture disk error')))
        result=run_case(case,cfg.workflow,agent_runner=worker,reference_profile='trae_verified',run_config=cfg)
    return result,output/'case',seen,compressions,sandboxes
