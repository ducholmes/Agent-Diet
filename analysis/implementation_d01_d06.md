# Kết quả triển khai D01–D06 — 05/10/2026

Cập nhật subscription: [implementation_subscription_contract.md](implementation_subscription_contract.md)
thay thế giới hạn API-key-only trong báo cáo ban đầu này. Hai profile Trae hiện
có Responses transport riêng, đã kiểm tra offline; chưa xác nhận endpoint live.

Đã triển khai repair contract Trae trong custom `TraeContractAgent(AgentBase)` và
`TraeLocalConversation(LocalConversation)` trên OpenHands SDK **1.49.5**.
Source `artifact/` giữ nguyên tại commit `1a436ca7cf59e30c147ecb5b360f8677e675538c`.
Các hash nguồn nằm trong `src/openhands_adapter/compat/reference_hashes.json`;
hash SDK và kết quả kiểm tra nằm trong [d01_d06_checks/results.json](d01_d06_checks/results.json).

## Kết quả

- Suite tổng: **173 tests**, **171 pass**, 2 Docker tests được skip trong suite tổng.
- Hai Docker integration tests đã chạy riêng và **đều pass**, mỗi test trên cả
  `php-src/defect4c:latest` và `swebench/sweb.eval.x86_64.fmtlib_1776_fmt-3901:latest`.
- Oracle AST độc lập đọc source gốc; không import provider/config globals.
- Không gọi model/provider thật, không chạy benchmark repair live.
- `pip check`: không có dependency hỏng; `git diff --check` sạch.

| Gate | Bằng chứng đã pass |
|---|---|
| GSDK | Real SDK sync/async lifecycle, FINISHED terminal/cap, clean shutdown ngoài sandbox; persisted state/resume; serialized HTTP schema và malformed raw arguments |
| G0 | Source hashes checked trước tests; oracle compile AST độc lập, gồm `Expert.run`, `MessageManager`, dispatcher, filter và tools |
| G1 | Exact system text/role ở request và HTTP JSON; secret descriptions/dynamic context không chèn vào messages |
| G2 | Exact user template; problem_statement ưu tiên trước UTF-8 failure log; multiline/Unicode/whitespace giữ nguyên; reject thiếu input/overrides |
| G3 | Editor → Bash → task_done → think, exact schemas; không thêm summary/security_risk; không alias/repair JSON; pairing/batch/observations theo source |
| G4 | Bash/reproducer/test edits hoạt động trong hai image; external validation chạy trên copy riêng, verdict fail không tạo request/retry generation |
| G5 | 60k output đầy đủ; editor clip raw content 16k trước numbering/tabs; state/undo, outer timeout/restart và inner polling timeout; wrapper observations/file bytes đối chiếu source |
| G6 | Expert oracle và SDK có cùng requests/steps/turns/gen/patch cho plain text, invalid/unknown tools, missing usage, batch task_done, empty/test-only completion, singleton terminal và cap 50/100 |

Gates trên áp dụng cho offline/scripted compatible chat transport với
`diet_mode=skip` hoặc hook spy. HTTP test dùng OpenAI client + httpx MockTransport
để bắt JSON thực sau serialization của SDK/LiteLLM, không chỉ constructor kwargs.
SDK events là archive; mutable contract nằm trong `ConversationState.agent_state`.
Request builder không đọc ngược SDK event archive để tạo lịch sử thứ hai.

## Thay đổi chính

- Profile mặc định được công khai là `trae_verified`: 50 turns, reminders bật;
  `trae_multiswe`: 100 turns, reminders tắt. SDK scheduling guard lần lượt 51/101;
  step cuối chỉ capture WIP, không gọi model hoặc hook nữa.
- Prompt/schema/MessageManager/dispatcher/filter port từ source. Tools chạy tuần tự,
  giữ nguyên argument strings. Terminal singleton không persist tool observation
  và không chạy compression hook; batch task_done vẫn chạy hết batch.
- Source wrappers được stage ở container-private `/home/swe-bench/tools/claude_tools`;
  editor history/log không đi vào repository patch. Bash inner shell mới mỗi call;
  outer session/container/filesystem sống qua calls.
- Capture dùng `git --no-pager diff --ignore-submodules=all`, rồi filter gốc;
  giữ behavior newline của `get_diff.py`. Runner dùng patch được agent chấp nhận,
  không recapture bằng HEAD/binary/add-N. Generation và external verdict tách nhau.
- Per-instance SDK conversation tắt ambient plugins/agents/hooks; không sửa SDK
  installed package hay dùng global monkeypatch cho implementation exact profile.
- Agent Diet chạy qua after-normal-turn hook trên contract state. Compression
  logic hiện tại được giữ hoạt động nhưng chưa được xác nhận parity D07–D11.
- Các tests restricted-tools/suffix/compressor cũ được ghi rõ là generic mode.

## Lỗi/bất thường phát hiện và xử lý

1. SDK ToolDefinition luôn thêm `summary`, và SDK conversion/stock agent có thể
   chặn/sửa JSON trước dispatcher. `SDKRawTransport` dùng boundary version-pinned
   `LLM._transport_call` dưới custom agent; test wire xác nhận schema/raw arguments.
2. Hai image có Python 3.10, thiếu `asyncio.timeout`. Runtime Bash dùng shim polling
   timeout riêng, giữ 210s/0.2s/sentinel gốc. Fallback phải raise
   **`asyncio.TimeoutError`**, vì Python 3.10 chưa alias nó với built-in TimeoutError.
   Python 3.11+ vẫn dùng native timeout. Shim được kiểm tra timeout/cancellation,
   và chạy thật qua wrappers trên cả hai image.
3. Git từ root container không tin checkout do host uid khác. Stage thêm đúng
   workspace vào container-private `safe.directory`; không thay source/evaluator.
4. Root-created files/build dirs gây lỗi host giữ/dọn workspace. Khi bỏ container,
   trả ownership của repair bind mount về uid/gid host; container luôn được remove
   trong finally. Không thay mode/file content.
5. Cap với filtered diff chỉ có whitespace: source trả final patch rỗng. Đã giữ
   đúng behavior này; original/filtered diff vẫn được archive riêng.
6. Async cleanup treo **trong sandbox execution hiện tại**. Probe bare
   `asyncio.run(asyncio.to_thread(...))` cũng treo; cùng probe và real SDK sync/async
   chạy ngoài sandbox đều exit sạch. Không sửa SDK/Python hay dùng timeout-kill
   làm bằng chứng pass. Các probe timeout chỉ dùng để chẩn đoán, không được ghi nhận pass.
7. Interactive shell gốc xử lý literal tabs qua readline trước editor wrapper.
   Tests whole-session so đúng source behavior này, không normalize output/tab
   để che drift. Pure editor tests kiểm tra riêng raw clipping/tab expansion.

## Cách dùng

Từ `Agent-Diet/`, API-key transport tương thích:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --model <model> --auth api-key --api-key-env OPENAI_API_KEY \
  --reference-profile trae_verified --diet-mode skip \
  --input <prepared-input-root> --case <case-id> --output d01-d06
```

Đổi sang `--reference-profile trae_multiswe` nếu cần reference Multi-SWE.
`--agent-timeout N` là watchdog harness; mặc định disabled, `0` tắt. Abort harness
không được gắn nhãn Trae completion. Prompt overrides và max-iterations chỉ dùng
ở `generic`; issue mapping bảo toàn UTF-8 decoded text, không `.strip()` nội dung.

Ở bản triển khai ban đầu, subscription cần `--reference-profile generic` do SDK
đổi system text thành user prefix. Giới hạn này đã được gỡ bằng Responses
transport riêng; xem báo cáo cập nhật ở đầu file để biết phạm vi kiểm tra.

Run artifacts nằm ngoài repair workspace: `contract-manifest.json` ghi profile,
SDK/source hashes, model/transport, input mapping/path/hash và schemas;
`contract-result.json`, `contract-patch.diff` ghi final state/patch; `events.jsonl`
chứa full `trae_request`, `trae_response`, `trae_tool_results`, state/hook/diff trace.
Preview logger được phép clip riêng; raw contract trace không clip observations.

## Lệnh kiểm tra

```bash
PYTHONPATH=src:tests LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
  .venv-openhands/bin/python -m unittest discover -s tests

PYTHONPATH=src:tests AGENTDIET_CONTAINER_INTEGRATION=1 \
  LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
  .venv-openhands/bin/python -m unittest test_trae_contract_container
```

Cần Docker daemon/image local cho lệnh thứ hai. Nếu chạy trong sandbox chặn async
executor wakeups, cần chạy test bên ngoài sandbox như phiên xác minh này.
Logs đã lưu: [regression](d01_d06_checks/regression.log),
[container](d01_d06_checks/container.log), [SDK probe](d01_d06_checks/sdk_probe.log),
[runtime probe](d01_d06_checks/runtime_probe.log).

**Phạm vi còn mở:** chưa xác nhận live provider wire behavior ngoài mock,
subscription exact, full compression/API/caching/submission parity D07–D14.
Không tuyên bố full AgentDiet parity cho mode `ours`.
