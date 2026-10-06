# Triển khai adapter OpenHands D07–D08

Ngày kiểm tra: **05/10/2026**. Thực hiện theo [kế hoạch](fix_d07_d08.md).

Đã nối repair → Agent Diet LLM → repair với policies riêng theo reference
models. Request serialization local và luồng tích hợp đã được kiểm chứng;
chưa nghiệm thu semantics của provider thật hoặc toàn bộ D09–D14.

## Thay đổi

- `compat/trae_llm_policy.py`: immutable role policy, literal `'gpt-5-' in
  reference_model`, cap 8192, `n=1`, temperature/stop/effort theo source.
  Hai actual models có thể giống nhau mà reference policies vẫn độc lập.
- Exact `SDKRawTransport` dùng SDK auth/routing và telemetry, rồi pinned OpenAI
  Chat serializer trực tiếp. Đây là thay đổi chủ đích so với đề xuất đi tiếp qua
  LiteLLM: tránh projection theo actual model làm đổi params/cache/prefill.
  Đường legacy subscription và generic SDK vẫn có tests riêng; chúng không
  được xem là exact D07–D08.
- Retry ở đúng một lớp: 12 attempts tổng, normalize provider response trong
  attempt, sleep `1,2,...,2048` kể cả failure cuối; inner OpenAI retries = 0.
  Không completed-response cache. Parser/usage callbacks/telemetry không nằm
  trong retry. Hết attempts raise `RuntimeError('no response from api')` và
  exact hook cho lỗi này thoát generation.
- Compressor exact gửi đúng system cache text block, user suffix và assistant
  prefill. Bỏ wrapper instruction tự thêm; stop không dựa vào SDK capabilities.
  Base prompt được so literal với source bằng AST oracle.
- Gate giữ serializer không bypass; compressor window dựng riêng từ manager,
  chỉ đổi tags tại serializer theo compressor reference. Giữ issue/tool name/
  arguments/code literals, thứ tự `-1/0/1/2`, newline shape khi `show_ctx=False`,
  và nested original recovery của source. Không global replace context.
- Config/env/CLI/worker roundtrip giữ `repair_reference_model`,
  `compressor_reference_model`, actual models và endpoint capabilities.
  Exact `ours` yêu cầu actual compressor rõ ràng; `inherit` phải được khai báo
  chủ đích. Global effort overrides khác low bị reject ở exact.
- Preflight trước container/provider reject subscription/Responses, thiếu
  capabilities, incompatible provider routing, extra-body/seed/api-version
  overrides. Exact Qwen streaming reference bị reject: OpenAI wrapper source
  không drain được iterator; chưa có supported streaming variant.
- `protocol-manifest.json` ghi roles/reference/actual/wire models, params,
  versions, retry assumption và trạng thái provider verification. Generic ghi
  discrepancy. Exact `contract-manifest.json` chứa protocol manifest và frozen
  oracle hashes; bổ sung hashes `llm_polytool.py` và `traj_analyzer.py`.
  `trae_llm_request` lưu payload không auth, `trae_transport_attempt/result`
  ghi attempt counts; compression telemetry giữ đúng role và usage một lần.
- Pin OpenAI **2.54.0**, httpx **0.28.1**, giữ SDK **1.49.5** và LiteLLM **1.103.2**.

## Kiểm chứng

- Full suite: **196 tests, OK, 2 skipped** (9.942 giây). Hai tests skipped là
  Docker opt-in, đã chạy riêng: **2/2 pass** (69.905 giây), mỗi test chạy trên
  cả image PHP và fmtlib cục bộ. Tổng cộng mọi 196 test cases đã được thực thi
  thành công khi kết hợp hai lần chạy. Logs: [unittest](d07_d08_checks/unittest.log),
  [container](d07_d08_checks/container.log).
- HTTP MockTransport bắt body sau OpenAI serialization: hai roles × hai
  reference branches match frozen wrapper, actual `gpt-5.6-sol` không làm đổi
  branch; field absence/tools/cache blocks/raw arguments được giữ.
- HTTP 429/500/connection failures: đúng 12 attempts, không nested retries;
  `None`, normalization failure, intermediate success và hai requests giống
  nhau được đối chiếu AST wrapper với fake sleep.
- Scripted integration dùng chính `build_agent`, SDK LocalConversation, exact
  transports và Agent Diet: ba normal turns → một compression step 0 → repair
  thứ tư task_done. Next request có summary, turns không tăng vì compression,
  một usage callback; telemetry 4 repair calls + 1 compression call.
- Docker tests kiểm tra real wrappers/editor/shell/restart và scripted repair
  lifecycle/patch filtering/external verdict separation. Đây không phải model
  repair trên benchmark thật hoặc bằng chứng live compression.
- `pip check`, CLI help, config JSON và `git diff --check` pass.

Lệnh full suite (từ `Agent-Diet`):

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
PYTHONPATH=src:tests .venv-openhands/bin/python -m unittest discover -s tests -v
```

Lệnh container fixtures với local images đã có:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
AGENTDIET_CONTAINER_INTEGRATION=1 PYTHONPATH=src:tests \
.venv-openhands/bin/python -m unittest test_trae_contract_container -v
```

Lần baseline trong sandbox bị treo vì `socketpair.send()` bị chặn với EPERM,
không phải model/network metadata hoặc adapter deadlock. Đã xác minh bằng probe
socketpair và chạy offline tests ngoài sandbox; không thêm workaround vào code.

## Chạy exact và phần còn lại

Copy [config mẫu](d07_d08_config.example.json), điền actual models và endpoint.
`trae_capabilities` mẫu để trống có chủ đích: chỉ khai báo những semantics đã
xác nhận bằng endpoint contract/probe. Defaults cần
`max_tokens,n,temperature,tools,reasoning_effort,cache_control,assistant_prefill`;
compressor reference non-GPT-5 cần thêm `stop`. Metadata này là xác nhận cấu
hình, không phải adapter tự chứng minh endpoint thực thi các semantics đó.

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --config /path/to/completed-d07-d08-config.json \
  --model 'openai/ACTUAL_REPAIR_MODEL' \
  --input /path/to/prepared-cases --case CASE_ID \
  --output d07-d08-smoke --keep-workspaces
```

API key đọc từ env theo config. Môi trường kiểm tra chưa có API key được cấu
hình, nên chưa gọi provider smoke hoặc model repair trên prepared PHP/fmtlib
case. HTTP 200/mock không chứng minh enforced cap, assistant continuation hoặc
provider cache semantics. Subscription chạy được ở `reference_profile=generic`,
đường đó được manifest mô tả khác exact.

D09–D14 vẫn còn: full serializer parity, acceptance/gates, parser heuristics và
controls/audit. Giữ strict parser hiện có cho tới D11; không tuyên bố adapter
identical với Trae chỉ dựa trên request tests hay patch outcome.
