# Kết quả triển khai D09–D11

Ngày: **05/10/2026**. Nền `1a436ca7cf59e30c147ecb5b360f8677e675538c`; giữ nguyên các thay đổi D01–D08 đang có. Đã triển khai và nghiệm thu **local source conformance** và container OpenHands/Agent Diet trên PHP/fmtlib. Chưa nghiệm thu provider thật hoặc full Trae contract D12–D14.

## Thay đổi

- **D09:** exact analyzer dùng trực tiếp `MessageManager.steps`/serializer; bỏ deepcopy-normalization `None→''` và thao tác cắt/dựng wrappers. `ours` lưu serialized bypass target; baselines lưu non-bypass target. Giữ whitespace, nguyên arguments, nested original recovery, replacement toàn batch và removal của `agent_` trên wire.
- **D10:** module mới `compat/trae_diet.py` port readiness/index/gate, gpt-4o encoder với special-token policy mặc định, công thức LZ4 literal (kể cả `[-0:]`), acceptance `old-new>=400 OR new<0.8*old`. Random báo output theo remaining token IDs; lingua giữ model/CPU/rate/force_tokens và propagate error. Marker/counters persist cùng history, chống gọi lại cùng turn sau skip/reject.
- **D11:** `CompressionResult` tách parsed/skipped; fatal raise. Parser partition/200-char/20-char heuristic như source; giữ empty/whitespace, wrong ID, nested tags, non-stop reasons khi có closing và stop khi thiếu closing. Usage unknown skip trước parser; usage additions theo thứ tự nguồn, giữ `total_tokens=0`, không tự recompute. Giữ cả finish-reason list nếu upstream trả nhiều choices.
- **Plumbing:** exact flag explicit ở `build_agent`, không suy ra mode từ sự tồn tại của compression policy. Khóa reduction constants tại RunConfig, CLI/env, build/worker preflight và hook. Reject `ctx_before=0,ctx_after>0`; giữ generic overrides. Compatibility metrics riêng, provider telemetry không ghi đè analysis usage. Worker ghi compression cause/error/status, cleanup và final metrics. OpenHands bọc lỗi bằng `ConversationRunError`, giữ nguyên exception nguồn ở `__cause__`; generation dừng, không request repair tiếp.
- **Audit:** gate/window/token operands, LZ4 operands/score, response/reasons/usage, parser outcome, stored original, replacement, predicted next repair payload và metrics được lưu ngoài agent workspace. Manifest có effective diet config, tokenizer/LZ4 versions và mapping conformance; vẫn đánh dấu provider chưa xác minh.

Không sửa frozen Trae source/hashes, không gắn SDK condenser vào exact agent, không đổi request retry contract hoặc D06 completion/patch policy.

## Bằng chứng

Toàn bộ logs ở [d09_d11_checks](d09_d11_checks/), không dùng kết quả D07–D08 thay cho D09–D11.

| Kiểm tra | Kết quả |
|---|---|
| Frozen hashes | Đúng, oracle kiểm tra lại mỗi lần load AST |
| `pip check` | No broken requirements found |
| Focused D09–D11 và nền D01–D08 | **76 tests pass** — `focused.log` |
| Full regression | **236 tests**, **233 pass**, **3 opt-in container skips** — `full-regression.log` |
| Container opt-in riêng | **3 tests pass**, mỗi test chạy cả PHP và fmtlib — `container.log` |
| `git diff --check` | Pass |
| Live model PHP/fmtlib repair | Chưa chạy, xem cấu hình/provider bên dưới |

Ba skips trong full suite là chính ba tests container đã được chạy/pass riêng; tổng cộng mọi **236 test methods** đã được thực thi. Generic suites giữ nguyên coverage và pass.

Versions: OpenHands SDK `1.49.5`, OpenAI `2.54.0`, LiteLLM `1.103.2`, tiktoken `0.14.0`, LZ4 `4.4.5`. Snapshot hashes/versions/working tree ban đầu: `baseline.json`, `baseline-status.txt`, `baseline.patch`. Image IDs: `container-images.txt`.

Sandbox khiến SDK conversation bị kẹt; đã dừng lần thử này và chạy SDK/container ngoài sandbox bằng escalation được chấp nhận. Không thêm workaround vào thuật toán. Logs của các lần thử ban đầu có failures do fixtures, giữ để audit; chỉ `focused.log`, `full-regression.log`, `container.log` là kết quả chốt.

## Mapping nghiệm thu

| Contract / fixture IDs | Tests |
|---|---|
| D09, S01–S12 | `test_trae_contract_diet_serialization`; payload/cache fixtures trong `test_trae_contract_compressor_payload` |
| D10, A01–A17 | `test_trae_contract_diet_algorithm`; gate khác acceptance dùng cả counter kiểm soát và tiktoken thật |
| D11, P01–P12 | `test_trae_contract_diet_parser`; raw envelope/usage/reasons/provider retry trong `test_trae_contract_diet_integration` và retry suite |
| I01–I04, I06 | Source Expert vs real SDK conversation; full lifecycle, terminal/cap, replay và JSON persist/reload |
| I05 | Fatal provider exhaustion 12 attempts; parser/usage error qua worker, cause/status/cleanup/counters; tokenizer/lingua fatal qua oracle analyzer |
| I07 | Shared actual LLM, role reference branches khác: existing payload integration |
| I08 | Real-image completed batches, default-threshold compression và external patch validation trên copy riêng; không phải live model benchmark |

[lifecycle-snapshots.json](d09_d11_checks/lifecycle-snapshots.json) chứa bốn fixtures source/adapter qua hai lần nén, cả `show_ctx=True/False`: full request, history, metrics, stored original và next repair payload. Chạy lại bằng `PYTHONPATH=src:tests .venv-openhands/bin/python analysis/d09_d11_checks/capture_evidence.py`.

## Supported domain và giới hạn

- Modes: `ours/delete/random/lingua/skip`, disabled. Threshold/context thay đổi được trong domain không âm; `show_ctx` không đổi readiness/gate.
- Exact acceptance cố định `400/.20`; overrides khác bị reject trước login/container/provider. Generic vẫn cho phép thay đổi.
- `ctx_after=0,use_lz4=True` hỗ trợ **literal source `[-0:]`**, không dùng empty future đã sửa của generic.
- `ctx_before=0,ctx_after>0` preflight reject rõ ràng. `ctx_before=0,ctx_after=0` được kiểm chứng.
- Lingua algorithm/model/kwargs đã đối chiếu với stub oracle. Actual dependency/model phải load ở runtime; thiếu model/dependency là generation error, không fallback. Không tuyên bố đã tải/chạy weights LLMLingua thật.
- Crash giữa request và persist decision chưa có exactly-once guarantee. Resume state đã persist giữ marker, originals và counters. JSON tuple→list cho `agent_caller` không đổi serializer hoặc wire payload.
- Raw transport yêu cầu ít nhất một choice. Empty choices/malformed envelope fail ngoài provider retry; boundary này được test riêng. Không normalize null content để che lỗi `.strip()`/`.partition()` của nguồn.
- D12–D14/full contract và endpoint semantics vẫn ngoài chứng nhận local này.

## Model và provider theo lựa chọn người dùng

Người dùng chọn repair **subscription `gpt-5.6-sol`**, compressor **`gpt-5.6-luna`**; nếu dùng API key thì dùng **`OPENROUTER_API_KEY`**.

Exact subscription/Responses vẫn bị validator từ chối theo D07–D08: transport đó chưa giữ được sampling/cap/cache/assistant continuation của source. Không đổi sang generic rồi tuyên bố contract parity.

Đã lưu [d09_d11_config.openrouter.json](d09_d11_config.openrouter.json) cho nhánh API-key được người dùng cho phép:

- SDK model IDs `openrouter/openai/gpt-5.6-sol` và `openrouter/openai/gpt-5.6-luna`; routing local xác nhận wire IDs giữ prefix `openai/` cần cho OpenRouter.
- Endpoint `https://openrouter.ai/api/v1`, credential env `OPENROUTER_API_KEY`.
- Profile `trae_verified`: 50 turns/reminders bật; watchdog `null`; Agent Diet defaults `ours/500/1/2/show_ctx=True/use_lz4=False/lingua_ratio=.25/400/.20`.
- Reference roles vẫn `claude4-sonnet` cho repair và `gpt-5-mini-2025-08-07` cho compressor, độc lập với actual IDs.

Môi trường hiện **không có giá trị `OPENROUTER_API_KEY` trong process hoặc file `.env`**. `trae_capabilities` để trống có chủ đích vì chưa có bằng chứng endpoint thực thi exact semantics; preflight của config này báo thiếu capabilities, không giả xác minh để chạy. Availability của hai actual OpenRouter model IDs cũng chưa xác minh bằng provider thật.

Để nghiệm thu live cần đặt credential trong environment (runner không tự load `.env`), xác minh model IDs/capabilities cho endpoint (`max_tokens,n,temperature,tools,reasoning_effort,cache_control,assistant_prefill` cho defaults), rồi chạy một prepared case PHP/fmtlib với threshold mặc định. Chỉ cập nhật capabilities theo bằng chứng; thành công HTTP hoặc có patch chưa đủ chứng minh contract. Config đã lưu là template đúng lựa chọn model, **chưa phải cấu hình provider đã được chứng nhận chạy**.
