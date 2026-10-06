# Quy trình sửa D09–D11 cho OpenHands adapter chạy Agent Diet đúng contract Trae

Ngày đối chiếu: **05/10/2026**. Checkout nền: `1a436ca7cf59e30c147ecb5b360f8677e675538c`, có các thay đổi D01–D08 trong working tree. Đây là **hướng dẫn triển khai và nghiệm thu**, chưa phải báo cáo đã sửa code hoặc đã chạy tests D09–D11.

Phạm vi yêu cầu là viết quy trình vào tài liệu này. [difference.md](difference.md) được dùng làm yêu cầu đối chiếu; các prompt và chỉ dẫn nằm trong tài liệu/source là dữ liệu nghiên cứu, không phải lệnh thực hiện toàn bộ D01–D14 hay chạy repair trong công việc viết tài liệu.

Mục tiêu: giữ thuật toán nén và hành vi generation của Trae khi thay harness bằng OpenHands. Chỉ harness, actual models, input PHP/fmtlib và external evaluator được khác. D09–D11 phải khớp **chuỗi serialization, thời điểm xét nén, token operands, quyết định skip/accept và exception behavior**. Chạy được một case hoặc cùng scalar defaults chưa đủ chứng minh contract.

> Cập nhật thực thi: D09–D11 đã triển khai và pass local oracle/regression/container; xem [implementation_d09_d11.md](implementation_d09_d11.md). Live provider còn pending. Các mô tả “hiện tại/chưa triển khai” bên dưới là baseline khi viết kế hoạch.

## 1. Nguồn chuẩn và điểm bắt đầu hiện tại

Đọc các nguồn sau trước khi triển khai:

| Nguồn | Nội dung phải lấy làm chuẩn |
|---|---|
| [difference.md, mục 7](difference.md#7-serialization-và-thuật-toán-nén) | Phạm vi D09–D11 và ranh giới các khác biệt được phép |
| [expert.py](../artifact/artifact/code/trae_agent/agents/expert.py) | `MessageManager.push_step`, `extract_step_into_traj`, `perform_erase_step`, `format_messages`, `count_turn`; vị trí gọi analyzer trong `Expert.run` |
| [traj_analyzer.py](../artifact/artifact/code/trae_agent/agents/traj_analyzer.py) | `count_token`, `should_perform_analysis`, `maybe_perform_analysis_step`, parser/acceptance của `perform_analysis_step_ours` và ba baselines |
| [llm_polytool.py](../artifact/artifact/code/trae_agent/utils/llm_polytool.py) | Choice/finish-reason lists, usage, retry/provider boundary và lỗi `no response from api` |
| [implementation_d07_d08.md](implementation_d07_d08.md) | Nền D07–D08 đã triển khai; giới hạn transport/provider chưa được nghiệm thu |
| [tests/trae_reference.py](../tests/trae_reference.py) và [reference_hashes.json](../src/openhands_adapter/compat/reference_hashes.json) | AST oracle độc lập, frozen source hashes; không import toàn Trae runner để chạy test |

Đường exact **thực sự đang chạy**:

```text
CLI / RunConfig
  → worker.run_worker → build_agent
  → TraeContractAgent.step
      → MessageManager.format_messages → SDKRawTransport (repair)
      → parse_tool_response → chạy toàn bộ tool batch → mgr.push_step
      → nếu chưa terminal: AgentDietCondenser.after_normal_turn(mgr)
          → gate → LLMCompressor._compress_exact
          → SDKRawTransport (compression) → parse → acceptance → erase
      → persist manager → request repair tiếp theo
```

`TraeContractAgent.supports_condenser=False`: exact mode không dùng condenser mặc định của SDK. SDK events ở đường này là archive; `mgr.steps` mới là dữ liệu tạo repair payload. `AgentDietCondenser.condense(events)` và `build_sdk_condenser()` phục vụ đường **generic**, cần kiểm tra regression riêng nhưng không lấy chúng làm bằng chứng exact mode.

### 1.1. Những phần đã có, cần giữ

- [compat/trae_contract.py](../src/openhands_adapter/compat/trae_contract.py) đã port `MessageManager` từ source: serializer, nested original recovery, erase reminder, loại fields `agent_` khỏi repair payload và turn formatting đã tồn tại.
- [diet/prompts.py](../src/openhands_adapter/diet/prompts.py) đã có `build_compression_window()`: gọi serializer theo `compression_policy.bypass_filter`, blank neighbor rồi `join('\n')` khi `show_ctx=False`.
- D07–D08 đã tách reference model của từng role khỏi actual model; đã có cache/prefill/request params và raw exact transport. Không đổi predicate thành `startswith('gpt-5')`, không global replace nội dung trajectory.
- [test_trae_contract_compressor_payload.py](../tests/test_trae_contract_compressor_payload.py) đã đối chiếu payload, whitespace và nested-window builder với source. Tuy nhiên test helper có trường hợp lưu original không bypass, và integration happy path không chứng minh toàn bộ vòng đời D09–D11.

### 1.2. Khoảng trống phải sửa

| File/điểm hiện tại | Sai khác hoặc phần chưa chứng minh | Công việc |
|---|---|---|
| [diet/trajectory.py](../src/openhands_adapter/diet/trajectory.py) | SDK event reconstruction có thể lặp visible thought, chỉ có `<think>`, lưu original body thay vì raw messages/serialized `agent_erased` | Exact lấy manager làm authority; nếu đưa SDK projection vào exact sau này phải chứng minh cùng chuỗi, thứ tự và batch ownership |
| [diet/condenser.py](../src/openhands_adapter/diet/condenser.py), `after_normal_turn` | Deepcopy rồi đổi `content=None` thành `''`; round trip serialize → cắt wrapper → `LogicalStep` | Bỏ normalization không có trong source ở exact; thao tác trực tiếp với serializer của manager |
| Cùng hook | Compressor gửi bypass window nhưng acceptance vẫn dùng `candidate.input_tokens` của gate; original lưu lúc erase cũng không bypass | Tách gate target khỏi acceptance/original target; `ours` lưu đúng serialized bypass target |
| `condense(events)` | `show_ctx=False` chỉ gửi target; recovery giữ original body, không tái lập nested wrappers | Giữ generic tách biệt; nếu dùng để phục vụ exact thì phải sửa và kiểm chứng thêm |
| [diet/core.py](../src/openhands_adapter/diet/core.py) | Token count dùng `disallowed_special=()`; reduction thresholds có thể override; LZ4 đã sửa `[-0:]` và denominator | Exact dùng encoder/error policy và công thức literal của source; khóa acceptance constants |
| Hook/condenser | Có thể xét lại target bị skip/reject khi gọi lại; guard đã nén chưa xử lý tất cả cases | Một analysis decision cho mỗi completed logical turn; lưu trạng thái chống replay nếu harness cần |
| [openhands/compressor.py](../src/openhands_adapter/openhands/compressor.py), `_compress_exact` | Usage phải là int; parser reject nhiều finish reasons, wrong ID, nested tags, empty output; missing close phụ thuộc request stop | Port đúng skip/parser semantics; bỏ các rules bổ sung ở exact |
| Hook | Nuốt hầu hết exceptions; chỉ rethrow một `RuntimeError` có message nhất định | Tất cả exception mà source phát sinh phải thoát generation; skip hợp lệ dùng outcome riêng |
| [worker.py](../src/openhands_adapter/openhands/worker.py), [token_tracking.py](../src/openhands_adapter/token_tracking.py) | Final telemetry có thể ghi đè analysis usage; manifest còn ghi D09–D11 pending | Lưu compatibility counters riêng; chỉ cập nhật trạng thái đã xác minh sau conformance |
| [config.py](../src/openhands_adapter/config.py), [compat/trae_llm_policy.py](../src/openhands_adapter/compat/trae_llm_policy.py) | Exact chưa reject acceptance overrides/các cấu hình edge chưa support | Thêm validation theo exact profile, giữ generic có thể tùy chỉnh |

## 2. Chốt cấu hình và giới hạn nghiệm thu

Defaults dùng để nghiệm thu:

```text
enabled = True
mode = ours
threshold_tokens = 500
ctx_before = 1
ctx_after = 2
show_ctx = True
use_lz4 = False
lingua_ratio = 0.25
tokenizer = tiktoken.encoding_for_model('gpt-4o')
acceptance = old_tokens - new_tokens >= 400 OR new_tokens < 0.8 * old_tokens
```

Giữ profile được chọn: `trae_verified` = 50 repair turns/reminders bật; `trae_multiswe` = 100/reminders tắt. D09–D11 không chọn lại profile theo PHP/fmtlib và không đổi completion/patch policy D06/D12.

Reference compressor mặc định `gpt-5-mini-2025-08-07` khiến `bypass_filter=True`; actual compressor có thể khác. `inherit` chỉ dùng actual model chung, vẫn giữ policy riêng của hai role.

Thêm exact validation tại `validate_trae_run()` hoặc validator chung được mọi entry point gọi trước generation:

1. `minimum_reduction_tokens` phải là `400`, `minimum_reduction_ratio` phải là `0.20`. CLI/config/env overrides khác phải bị reject với tên field; không âm thầm bỏ qua giá trị đã khai báo. Generic vẫn có thể override.
2. `show_ctx=False` là cấu hình hợp lệ phải giữ đúng whitespace, delay và gate. Không dùng nó để bỏ future-context requirement.
3. `use_lz4=True` chỉ công bố supported khi pass oracle cho công thức literal; `ctx_after=0` phải match `[-0:]` của source hoặc reject cấu hình này trong exact trước khi chạy.
4. `ctx_before=0, ctx_after>0` có edge riêng: source có thể xét target `-1` ở turn đầu đủ delay, tức user prompt, rồi erase bằng Python negative index. Khuyến nghị reject tổ hợp này ở exact cho tới khi chủ đích hỗ trợ literal behavior; không tự thêm guard rồi gọi là parity. `ctx_before=0, ctx_after=0` không có edge này ở hook sau turn đầu.
5. `skip`/disabled có thể được cung cấp, nhưng phải khai báo rõ mode và không gọi analyzer. Với exact enabled, `skip` tương ứng nhánh source `MODE=='skip'`.
6. Baselines `delete/random/lingua` chỉ ghi supported sau khi có oracle tests riêng. Thiếu dependency/model LLMLingua phải là preflight error hoặc generation error, không fallback sang algorithm khác.

Đây là validation **cần bổ sung**, không phải khẳng định checkout hiện đã có. Các dòng thí nghiệm thay threshold/context/ratio phải ghi effective config và supported domain; không dùng default nghiệm thu để che thay đổi config.

## 3. D09 — Serializer, original recovery và repair replacement

### 3.1. Dùng một dữ liệu gốc cho exact trajectory

Giữ `MessageManager.steps` là list các step `[assistant_msg, *followup_msgs]`. Step repair bắt đầu từ `0`; initial user riêng ở `-1`, không được cộng vào `count_turn()`. Compression không thêm step mới.

Exact không cần rebuild trajectory từ SDK events. Hook nên gọi trực tiếp `mgr.extract_step_into_traj(i, bypass_filter=...)`. Không cắt wrapper bằng `split/rsplit` rồi dựng lại, vì empty step của source là `'<step id="i">\n</step>'`, khác empty `LogicalStep.serialize()` hiện tại có thêm newline.

Giữ visible assistant content; bỏ provider reasoning metadata khỏi compressor trajectory. Giữ tool-call ID/arguments ở raw messages để format repair history; serializer không đưa IDs vào `<call>` nếu nguồn không đưa.

### 3.2. Quy tắc serialize từng ký tự

Với index `-1`: trả **nguyên** initial user string, không bọc `<step>`. Với index `i>=0`:

1. Mở `'<step id="i">'` trong list output.
2. Duyệt messages đúng thứ tự đã push; không sort theo tool ID hoặc timestamp.
3. User follow-up dùng `<user>`. Assistant dùng `<think>` nếu không bypass, `<talk>` nếu bypass.
4. Assistant có `agent_erased` truthy: append nguyên serialized original rồi `continue`; không emit reminder, không render tool calls của message đó.
5. Tool bình thường dùng `<result>` với content nguyên văn. Tool có `agent_caller[0]=='think'` lấy `thought` từ parsed caller arguments, tạo `f'\n{thought.strip()}\n'`, dùng `<think>`/`<talk>` theo bypass. `Continue.` vẫn nằm trong raw repair messages nhưng không thay thế thought trong serializer.
6. Chỉ emit content tag khi `content.strip()` truthy; **content bên trong tag không bị strip**. Thought có rule strip riêng như bước 5.
7. Sau content của mỗi assistant, emit toàn bộ calls theo advertised/raw response order: `'<call tool="NAME">{arguments.strip()}</call>'`. Giữ nguyên original JSON string ở giữa, chỉ `.strip()` hai đầu theo source; không `json.loads` rồi dump lại, không sort keys, đổi spacing hoặc XML-escape.
8. Append `'</step>'`; `return '\n'.join(out)`.

Các chuỗi `<think>`/`agent` nằm trong issue, code, tool names hoặc arguments phải giữ nguyên. Bypass đổi **tag tại serializer**, không thay chữ trên cả context. Một assistant response có nhiều calls vẫn chỉ có một visible assistant text, rồi tất cả calls, rồi follow-ups theo source.

Không đổi `content=None` thành `''` trong exact analyzer để che lỗi nguồn. Source có thể phát sinh exception ở `.strip()`; kiểm chứng cả loại lỗi/thời điểm đó. Nếu muốn hỗ trợ một provider luôn trả null content, cần thiết kế adaptation và ghi khác biệt riêng; không công bố literal parity cho đường đã normalization. Defaults hợp lệ và fixtures phải có string content khi source cần string.

### 3.3. Window có hai representation

Đừng gom gate window và compressor window thành một string duy nhất:

```python
# Pseudocode: dùng serializer của manager, không global replace.
gate_window = [mgr.extract_step_into_traj(i, False) for i in window_indices]
bypass_window = [mgr.extract_step_into_traj(i, policy.bypass_filter)
                 for i in window_indices]
target_original = bypass_window[ctx_before]  # ours: acceptance + agent_erased
if not show_ctx:
    bypass_window = [s if j == ctx_before else ''
                     for j, s in enumerate(bypass_window)]
compressor_context = '\n'.join(bypass_window)
```

Có thể mở rộng `build_compression_window()` để trả thêm list/target, hoặc thêm builder dữ liệu bên cạnh. Không tách target ra bằng cách tìm tag trong chuỗi đã join: source có nested/literal tags.

Với `ctx_before=1, ctx_after=2`, `show_ctx=False` phải cho đúng:

```text
'\n' + serialized_bypass_target + '\n\n'
```

Khi builder D08 nối suffix, user payload là `compressor_context + f'\n\nNow, compress the step {idx}.'`; giữ nguyên cả newline của context lẫn suffix. Initial user và neighbors bị blank nhưng **số phần tử window không đổi**.

### 3.4. Lưu original đúng nhánh và phục hồi nested wrappers

Khi `ours` accept, gọi `mgr.perform_erase_step(idx, parsed_content, target_original)` với `target_original` là **serialized target theo bypass dùng trong request vừa thực hiện**. Hiện hook lấy lại original không bypass, cần sửa.

`delete/random/lingua` dùng serialized non-bypass target theo hàm baseline nguồn. Không dùng một original chung cho mọi mode.

Source lưu serialized original gồm cả `<step>...</step>` trong `agent_erased`. Lần sau serialize neighbor đã nén sẽ thêm outer `<step>` rồi append original nguyên văn:

```text
<step id="0">
<step id="0">
<talk>...</talk>
...
</step>
</step>
```

Giữ shape này, không unwrap, parse XML, canonicalize hay thay original bằng summary. Khi original đã lưu `<talk>`, lần recovery với `bypass_filter=False` vẫn giữ `<talk>` bên trong original; không đổi nó trở lại `<think>`. Đây là hệ quả của source, cần fixture vòng đời qua hai lần compression liên tiếp, không chỉ fixture tự dựng `agent_erased`.

### 3.5. Thay toàn bộ target batch trong repair history

`perform_erase_step()` thay đúng một step bằng một assistant message; mọi calls và observations cũ của target biến mất khỏi next repair request. Giữ exact reminder và truthiness:

```python
content = (
    f'(System reminder: compressed for better efficiency) {parsed_content.strip()}'
    if parsed_content else
    '(System reminder: long content deleted for better efficiency)'
)
```

- Parsed `''` đi vào deleted reminder; whitespace-only như `'   '` truthy nên đi vào compressed reminder với suffix rỗng sau strip.
- Token acceptance đếm **trước** strip/reminder. Không đo acceptance trên repair replacement message.
- `agent_erased` và tất cả fields `agent_` chỉ ở internal state/audit, bị `_remove_internal_fields()` loại khỏi repair wire payload.
- Khi erase target cuối với `ctx_after=0`, next repair formatting có thể cần user `Continue...`; giữ `format_messages()` D06/D08 và cache placement như source.

## 4. D10 — Trigger, gates, token operands và metrics

### 4.1. Timing và target index

Giữ sequence:

```text
repair response → tất cả tool executions/observations → push_step
  → maybe analysis đúng một lần → persist → format next repair messages
```

Không chạy trước response đầu, giữa batch thiếu observations, theo số tool events hoặc theo request context-window của SDK. Compression request không tăng repair turns.

Đặt `turn=mgr.count_turn()`. Source:

```python
if turn < ctx_before + ctx_after:
    return
idx = turn - 1 - ctx_after
window_indices = range(idx - ctx_before, idx + ctx_after + 1)
```

Với defaults:

| Sau push | Completed steps | Target xét | Window |
|---|---|---|---|
| Turn 1 | 0 | Chưa xét | — |
| Turn 2 | 0, 1 | Chưa xét | — |
| Turn 3 | 0, 1, 2 | 0 | -1, 0, 1, 2 |
| Turn 4 | 0, 1, 2, 3 | 1 | 0, 1, 2, 3 |
| Turn 5 | 0…4 | 2 | 1, 2, 3, 4 |

Với `ctx_before>1`, source có thể bắt đầu từ target lớn hơn 0; không tự backfill những targets đầu bị bỏ qua. Điều kiện readiness không phải `ctx_before+ctx_after+1` vì initial user có thể cung cấp context trước.

`task_done` có filtered patch không rỗng, hoặc terminal `task_failed` theo source, push final response rồi kết thúc **trước analysis cuối**. Empty-patch task_done là normal turn có observations/user feedback và vẫn xét analysis. Turn cuối tại cap nếu là normal turn vẫn xét analysis; bước scheduler tiếp theo ghi `turn_capped`, không thêm analysis lần nữa.

Hook hiện chỉ được gọi sau normal turn; giữ điểm gọi này. Nếu có replay/resume, thêm dấu `last_analyzed_turn` hoặc trạng thái tương đương ở persisted agent state, để repeated call cùng turn không làm tăng `seen_tokens`, không retry target skip/reject. Đây là plumbing để giữ lịch gọi source, không thêm thuật toán tìm candidate khác. Khởi tạo/khôi phục marker cùng steps/metrics; không đánh dấu fatal error rồi cho repair tiếp tục. Nếu resume sau crash chưa chứng minh được, ghi giới hạn thay vì hứa exactly-once cho mọi crash window.

### 4.2. Encoder và threshold gate

Exact counter phải dùng:

```python
encoding = tiktoken.encoding_for_model('gpt-4o')
tokens = len(encoding.encode(text))
```

Không truyền `disallowed_special=()`. Chuỗi special-token spelling như `<|endoftext|>` phải có cùng encode/error behavior với source. Không dùng tokenizer của actual model, SDK estimate, character count hoặc API usage làm operand cho gate/acceptance.

Checkout lock đang pin `tiktoken==0.14.0`, `lz4==4.4.5`. Chạy cả oracle và adapter trên cùng versions, lưu versions vào bằng chứng; source lịch sử không pin đủ versions nên không khẳng định biết mọi token count của run lịch sử.

Sau readiness, serialize full gate window **không bypass** rồi lấy `gate_target=gate_window[ctx_before]`:

1. `gate_tokens=count_token(gate_target)`.
2. Cộng `seen_tokens += gate_tokens` đúng một lần, kể cả target dưới threshold.
3. `gate_tokens < threshold_tokens` thì skip; bằng threshold thì đi tiếp.
4. Nếu `use_lz4=False`, gate đạt. `show_ctx` không tác động các bước trên.

Gate đếm full serialized target, gồm wrappers/tags và raw arguments/observations. Không đếm riêng tool output, toàn context hoặc summary hiện đang hiển thị cho repair.

### 4.3. LZ4 gate literal

Với gate window `W` và `tokens=gate_tokens`, port đúng source:

```python
x1 = len(lz4.frame.compress(''.join(W[-ctx_after:]).encode('utf-8')))
x2 = len(lz4.frame.compress(''.join(W[-(ctx_after + 1):]).encode('utf-8')))
save_rate = 1 - max(0, x2 - x1) / len(W[ctx_before].encode('utf-8'))
save_tokens = tokens * save_rate
passed = save_tokens >= threshold_tokens
```

Không thêm separators vào chuỗi nén; denominator là **UTF-8 bytes**, không phải Unicode chars hay tokens. Không clamp `save_rate` về `[0,1]`, thay denominator bằng `max(1,...)` hoặc bật LZ4 khi flag tắt. Threshold token gate chạy trước LZ4.

Với `ctx_after>0`, `x1` là future neighbors, `x2` là target + future. Với `ctx_after=0`, `W[-0:]` là toàn window; adapter `_compressible()` hiện dùng empty future đã sửa khác source. Chọn port literal hoặc preflight reject như mục 2. Test `test_zero_future_context_compresses_empty_baseline` hiện có chỉ mô tả generic behavior, không thể dùng làm expected exact.

### 4.4. Acceptance phải có operand riêng

Với `ours`:

```python
old_tokens = count_token(serialized_bypass_target)
new_tokens = count_token(parsed_content)  # bare string, chưa strip/reminder
accepted = old_tokens - new_tokens >= 400 or new_tokens < 0.8 * old_tokens
```

`serialized_bypass_target` là target slot của compressor window trước khi join; `show_ctx=False` không thay target slot. Gate target và acceptance target có thể khác số tokens do tags/representation; không dùng lại `candidate.input_tokens` của gate.

Không tính tokens của full compressor user request/system/prefill/neighbor context làm `old_tokens`. Không thêm repair reminder hoặc wrapper do adapter tự dựng vào `new_tokens`. Giữ dấu `<` strict ở mốc 80%, `>=` ở 400; không đổi thành `<=`, làm tròn percentage hay yêu cầu cả hai điều kiện cùng đúng.

Baselines vẫn đi qua readiness/threshold/LZ4, nhưng **không có reduction acceptance gate**:

| Mode | Input/thuật toán | Erase metrics/original |
|---|---|---|
| `delete` | Serialized non-bypass target; `content=None` khi erase theo source | `erase_in=count_token(target)`, `erase_out=0`; lưu full original target |
| `random` | Encode target theo default special-token policy; chỉ deletable token có `decode_single_token_bytes(t).decode()` không lỗi Unicode; `random.sample(..., min(int(len(tokens)*(1-ratio)), len(deletable)))` | `erase_out=len(remaining_token_ids)`, **không re-encode decoded text**; không tự seed runtime; giữ random global/scheduling assumptions |
| `lingua` | `microsoft/llmlingua-2-xlm-roberta-large-meetingbank`, `use_llmlingua2=True`, CPU, `rate=lingua_ratio`, `force_tokens=['\n','?']` | Đếm output `compressed_prompt`; giữ original target; exception truyền ra ngoài |

Nếu cần structured baseline result để giữ `remaining_token_ids` count, bổ sung ở exact path; generic API `random_drop()->str` có thể giữ riêng. Controlled RNG trong fixture được phép để tái lập oracle, không thêm seed vào thí nghiệm thật khi reference không có.

### 4.5. Counters phải theo cùng nhánh quyết định

| Counter tương thích source | Thời điểm cập nhật |
|---|---|
| `seen_tokens` | Sau readiness, trước threshold/LZ4 decision |
| `analysis_count` | Gate đạt, trước dispatch strategy/request; gồm cả response sau đó bị skip, rejected hoặc fatal |
| `analysis_cost_tokens`, `analysis_prompt_tokens`, `analysis_completion_tokens` | Chỉ `ours` có completion usage khác `None`; cộng raw source fields trước parser/acceptance |
| `erase_tot_count` / adapter alias `erase_count` | Chỉ khi erase thực sự được chấp nhận hoặc baseline erase |
| `erase_in_tokens`, `erase_out_tokens` | Acceptance operands của `ours`; baseline operands theo bảng trên |

Không tính tool calls/provider retries thành `analysis_count`. Không double-count qua `on_usage`, hook và telemetry. Adapter có thể thêm rejected-reason counters, request-attempt counters và billing telemetry, miễn không làm đổi quyết định.

`worker` và `summarize_diet()` hiện lấy aggregate provider usage ghi đè fields analysis. Nên lưu một nhóm `reference_diet_metrics` riêng hoặc các fields compatibility rõ nghĩa, giữ billing usage tách biệt. `step_content_reduction_tokens` nếu công bố parity phải dùng đúng `erase_in-erase_out`, không nhầm với giảm full repair view sau thêm reminders.

## 5. D11 — Parser và failure behavior của compressor

### 5.1. Tách ba kết quả, không dùng exception làm skip

Exact compressor cần phân biệt:

- **Parsed**: đã có content, kể cả `''` hoặc whitespace; chuyển sang acceptance.
- **Skipped**: source bỏ response vì `completion_tokens is None` hoặc thiếu closing tag và không có finish reason `'stop'`; không erase, turn tiếp theo xét target mới.
- **Raised**: provider/retry exhaustion, malformed envelope/usage hoặc runtime lỗi mà source không catch; abort generation.

Có thể thêm kiểu result `status`, `content`, `reason` cho exact hoặc method exact riêng. Không lấy `''`, `None` hoặc `ValueError` làm tín hiệu chung cho cả ba tình huống. Không buộc generic callable API đổi nếu không cần. Acceptance rejected là quyết định của analyzer, khác parser skipped.

### 5.2. Port parser theo đúng thứ tự source

Pseudocode dưới đây mô tả thứ tự cần giữ, chưa phải code đã triển khai:

```python
answers, finish_reasons, usage = get_reference_shaped_response()
if usage['completion_tokens'] is None:
    return skipped('completion_usage_none')

# Cộng source usage trước parser; không tự fallback total hoặc cộng cache lần nữa.
metrics['analysis_cost_tokens'] += usage['total_tokens']
metrics['analysis_prompt_tokens'] += usage['prompt_tokens']
metrics['analysis_completion_tokens'] += usage['completion_tokens']

answer = answers[0]
content, closing, trailing = answer['content'].partition('</step>')
if not closing and 'stop' not in finish_reasons:
    return skipped('missing_close_without_stop')

if '<step' in content[:200]:
    content = content.partition('<step')[2]
    if '>' in content[:20]:
        content = content.partition('>')[2]
return parsed(content)
```

Các chi tiết không được bỏ:

1. Lấy choice đầu; dùng exact list membership `'stop' in finish_reason_list`, không substring trên một reason string. Current raw transport trả một choice/reason; vì `n=1`, có thể dùng `[answer]`, `[reason]` với envelope được bảo đảm một choice. Nếu muốn hỗ trợ response nhiều choices như source, cần giữ full reasons list ở transport result; không giả vờ first reason luôn tương đương cả list.
2. `completion_tokens=None` return **trước** usage addition, choice/content parser và token count output. Adapter hiện raise missing usage cần thay ở exact.
3. Source không có blanket validation `isinstance(prompt/completion, int)`. Missing/wrong-type các usage fields khác có thể gây `KeyError`/`TypeError` trong phép cộng; giữ failure tương ứng. `total_tokens=0` không được tự thay bằng `prompt+completion`.
4. `partition('</step>')` lấy lần đầu, bỏ toàn bộ trailing text. Không thêm prefill vào output rồi parse lại; parse response content đúng như source.
5. Nếu có closing tag, các finish reasons như `length`, `content_filter`, `tool_calls`, `error` **không tự bị reject**. Source không kiểm tra `answer.tool_calls` để loại text. Acceptance vẫn quyết định theo parsed content.
6. Nếu thiếu closing nhưng reasons có `'stop'`, vẫn parse/accept cho cả GPT-5 reference bỏ request stop. Không yêu cầu `stop` tồn tại trong kwargs/policy.
7. Heuristic `<step` chỉ kiểm tra 200 chars đầu; `>` chỉ kiểm tra 20 chars đầu sau khi bỏ prefix. Không verify ID, quotes, XML nesting hoặc opening-tag grammar. Prefix có thể bị cắt tại chuỗi `<step` literal trong code: giữ behavior đó khi yêu cầu exact.
8. Không có opening tag vẫn hợp lệ. Wrong step ID và nested tags không tạo một rejection rule riêng. Nội dung cuối có thể giữ nested opening tags do chỉ bóc một prefix; không chạy validator lần hai.
9. Không strip toàn output trước parser/counting. Output rỗng/whitespace có thể qua acceptance và tạo reminder theo truthiness ở mục 3.5.
10. `answer['content']=None`, thiếu choice/content hoặc envelope sai có thể raise từ source. Không chuyển thành parsed empty hoặc parser skipped, không retry parser exception qua provider layer.

### 5.3. Exception phải dừng generation

Trong exact `after_normal_turn`, bỏ blanket catch-and-return cho compressor/parser/tokenizer/strategy errors. Nếu cần emit audit ở `except`, emit rồi **raise lại mọi exception**; không chỉ matching message `no response from api`.

Giữ provider retry D07: 12 total attempts, backoff gốc, inner retries tắt. Không thêm parser retries, không gọi compressor lần hai vì output skipped/insufficient reduction. Telemetry/parser/usage exceptions nằm ngoài provider retry như transport hiện tại.

Luồng lỗi cần kiểm chứng:

```text
Fatal trong compression
  → hook raise → TraeContractAgent.step / conversation.run raise
  → worker emit lỗi + finally lưu metrics/cleanup → worker exit lỗi
  → process/CLI giữ error → outer workflow ghi generation failure
```

Source không repair tiếp sau lỗi compressor. [workflow/runner.py](../src/openhands_adapter/workflow/runner.py) có thể giữ filtered WIP và external evaluator chấm trên copy riêng theo policy ngoài; đó không phải successful generation và không phải lý do gửi repair request tiếp. Lưu riêng generation status/error và external verdict, không ghi nhầm thành `task_done`/`turn_capped`, không dùng evaluator feedback để retry generation.

`worker` hiện gom `ImportError/AttributeError` thành lỗi SDK API. Nếu parser/compressor phát sinh loại lỗi này, giữ nguyên error type/cause và phase compression để audit, hoặc thu hẹp catch SDK vào phần khởi tạo; đừng làm người vận hành hiểu nhầm lỗi content là SDK version mismatch. Cleanup/final metrics không được che exception gốc.

## 6. Cách tổ chức implementation để tránh lệch hai đường chạy

Đề xuất thêm module pure `src/openhands_adapter/compat/trae_diet.py` cho exact analyzer: readiness/index/window, gate, acceptance và orchestration metrics/erase. **Tên module và API này là đề xuất mới**, chưa tồn tại.

Phân công trách nhiệm:

| Thành phần | Trách nhiệm sau sửa |
|---|---|
| `compat/trae_contract.MessageManager` | Raw steps, serializer, erase và repair formatting giữ nguyên nguồn |
| Pure exact analyzer đề xuất | Port source analyzer; giữ gate và bypass operands riêng; dispatch baseline; quyết định skip/reject/erase |
| `diet/prompts` | Window data/context + exact messages D08, không parse output |
| `openhands/compressor` | Gửi exact request; usage trước parser; result phân biệt parsed/skipped; exception propagate |
| `diet/condenser.after_normal_turn` | Delegate exact analyzer; audit, metrics alias; không rebuild `LogicalStep` cho exact |
| `TraeContractAgent` | Turn/completion gates; gọi hook một lần; persist state sau nén; không SDK condensation ở exact |
| `worker/token_tracking` | Lưu status, compatibility metrics, provider usage và manifest tách biệt |
| `diet/core/trajectory`, `openhands/condenser` | Giữ generic regression; không tự coi generic policy là exact |

Có thể triển khai cùng invariants ngay trong hook thay vì module mới nếu code rõ và test độc lập. Không import/execute toàn `traj_analyzer.py` trong production vì nó đọc env, tạo dependencies/global state và routing gốc. Port phần cần thiết vào adapter; AST execution của source chỉ dùng ở **tests làm oracle**.

Không gắn lại SDK condenser vào exact agent để giải quyết thiếu context. Không bật SDK context-window recovery/auto summarizer/retry target khác; nếu provider không nhận được request, giữ lỗi transport/harness như contract đã chọn.

## 7. Các bước triển khai theo thứ tự

1. **Ghi baseline.** Lưu git status/diff của working tree hiện có, hashes source, lock versions và effective config. Chỉ sửa files cần D09–D11, giữ các thay đổi D01–D08 đang có; không reset source/oracle để test pass.
2. **Mở rộng oracle.** Trong `tests/trae_reference.py`, dùng `load_nodes()` lấy `count_token`, `count_comp`, `should_perform_analysis`, `maybe_perform_analysis_step`, `perform_analysis_step_ours` và baseline functions từ frozen analyzer. Inject `MODE`, context/threshold flags, encoder/LZ4/RNG, fake `get_llm_response` và lingua stub. Khởi tạo manager/metrics đầy đủ theo nguồn. Không lấy expected từ hàm adapter vừa sửa.
3. **Định nghĩa exact result và config guard.** Chốt parsed/skipped/fatal API; khóa reduction constants; xử lý unsupported context/LZ4 combinations trước container/provider. Các tests `from_mapping`, CLI overrides, worker round trip phải giữ fields và validation.
4. **Làm D09 trước.** Dùng raw manager serializer; expose window target riêng; bỏ normalization/unwrap; lưu bypass original khi `ours` erase. Kiểm tra full lifecycle và next repair request cùng oracle.
5. **Làm D10.** Port readiness/index, default encoder semantics, threshold/LZ4 gates và acceptance operands/constants. Hoàn thiện metrics/baselines nếu công bố supported; chống replay cùng turn khi harness có thể gọi lại.
6. **Làm D11.** Thay strict exact parser bằng heuristic nguồn; usage order; missing completion usage → skipped; fatal → raise. Giữ generic parser/soft-failure semantics riêng nếu generic vẫn chủ đích có chúng.
7. **Nối tích hợp.** `build_agent` truyền compression policy/exact marker đúng cả enabled/skip/baseline modes; không xác định exact solely bằng `compression_policy is not None` vì baselines/skip không có compression LLM policy. Hook, agent state, worker audit và token summary dùng cùng state/counters.
8. **Chạy tests tập trung rồi full regression.** Sửa test expectations chỉ ở scope exact; không xóa generic coverage để làm suite xanh. Test D07–D08 đang dùng reduction overrides `0/0` để force erase phải sửa thành target dài, threshold fixture hợp lệ, constants `400/0.20` khi test build exact run.
9. **Container + live smoke PHP/fmtlib.** Chỉ sau local conformance, xác nhận tool/harness không phá trajectory; rồi endpoint capabilities/model protocol và một case mỗi project. Không dùng benchmark outcome để thay expected parser/gate.
10. **Chốt bằng chứng.** Lưu logs/fixture IDs, manifest versions và mapping D09–D11 → tests; ghi verified domain/unsupported settings và provider verification status. Không xóa “pending” khỏi manifest nếu chỉ mới pass happy path.

## 8. Ma trận kiểm chứng bắt buộc

Tên files mới dưới đây là **đề xuất để tạo khi triển khai**, chưa phải files đã có:

- `tests/test_trae_contract_diet_serialization.py` — D09.
- `tests/test_trae_contract_diet_algorithm.py` — D10, baselines/config.
- `tests/test_trae_contract_diet_parser.py` — D11.
- `tests/test_trae_contract_diet_integration.py` — full lifecycle/error/status.

Mỗi fixture so chuỗi bằng equality trực tiếp hoặc UTF-8 bytes, state/metrics bằng values, exceptions bằng type và stage; không `.strip()`, normalize whitespace hoặc bỏ nested tags trước assert.

| ID | Fixture cần có | Kết quả chuẩn |
|---|---|---|
| S01 | Initial user, step IDs 0/1/2, empty assistant | User ở -1; count_turn chỉ steps; empty wrapper đúng source, không thêm blank line |
| S02 | Một assistant text + nhiều calls + results/user follow-up | Text → calls → follow-ups đúng raw order; response là một step; không lặp text |
| S03 | JSON arguments có key order, spaces, newline, Unicode, `<>&` | Chỉ strip hai đầu arguments, giữ bytes còn lại; không reserialize/escape |
| S04 | Think caller thought có indentation/blank lines, tool observation `Continue.` | Serializer lấy thought theo source; raw repair history vẫn giữ observation |
| S05 | Provider reasoning metadata, visible assistant text | Chỉ visible text vào trajectory; metadata không lọt vào context |
| S06 | Bypass True/False, issue/code/tool literals chứa `think/agent` | Chỉ tag serializer thay; original literals không bị replace |
| S07 | show_ctx True/False, nhiều ctx_before/after hợp lệ | Window đúng slot/order/newlines; hide context không đổi delay/gates |
| S08 | `ours` erase step 0 rồi turn kế nén step 1 | Original step 0 lưu bypass; neighbor recovery nested nguyên văn giống source |
| S09 | Baseline erase rồi dùng làm neighbor | Original non-bypass, outer wrapper và stored content khớp oracle |
| S10 | Replacement thường, `''`, whitespace-only | Reminder/truthiness đúng source; toàn old batch bị thay; no `agent_` in repair payload |
| S11 | `ctx_after=0`, next repair ends assistant sau erase | `Continue...`/budget text và cache placement giữ D06/D08 |
| S12 | Null/invalid content và malformed caller theo nguồn | Cùng exception behavior; không auto-normalize hoặc skip lỗi |
| A01 | Turn 1/2/3/4 defaults; ctx_before>1 | Không xét sớm; target 0 ở turn 3; source readiness/backfill behavior |
| A02 | Partial tool batch, multi-tool complete, repeated hook call | Chỉ completed turn được analysis; một decision/turn; no duplicate seen/request |
| A03 | Threshold `N-1/N/N+1` theo source token count | Equality qua gate; `seen_tokens` cộng kể cả below threshold |
| A04 | Gate tags và bypass tags có token counts khác | Gate dùng non-bypass, acceptance/erase_in dùng bypass target |
| A05 | Code/Unicode/special-token spelling | Same encoder counts hoặc same ValueError, không permissive fallback |
| A06 | LZ4 default/off và on với future trùng/khác, UTF-8 | Same bytes/formula/decision; threshold trước LZ4; không tính prior context vào future với ca>0 |
| A07 | LZ4 marginal size âm, score tại threshold; ca=0 | Clamp chỉ marginal max(0,...); equality pass; ca=0 literal hoặc preflight reject |
| A08 | Output savings 399/400 với ratio branch không đạt | 399 reject, 400 accept; OR branch độc lập |
| A09 | Old=1000, new=799/800/801, saving đều <400 | 799 accept; 800/801 reject; strict 80% boundary |
| A10 | Leading/trailing whitespace, reminder/wrapper overhead | Count bare parsed output trước strip; không count full context/reminder |
| A11 | Below threshold, invalid result, insufficient reduction, accepted | Same analysis/seen/erase counters theo từng nhánh |
| A12 | skip/disabled, thiếu compressor/dependency khi enabled | Skip không gọi gate/request; lỗi cấu hình/dependency không silently repair tiếp |
| A13 | Config/CLI reduction overrides khác chuẩn | Exact reject, generic được giữ riêng; round trip không đổi settings |
| A14 | delete/random/lingua outputs dài bằng/lớn hơn input | Không reduction gate; vẫn readiness/threshold/LZ4 |
| A15 | random Unicode nondeletable tokens, re-encode count khác remaining IDs | Same sample/decoded output; erase_out dùng remaining IDs; không seed runtime |
| A16 | lingua model/kwargs/error | Exact model, CPU/rate/force_tokens; lỗi propagate, không baseline fallback |
| A17 | ctx_before=0/ctx_after>0, unsupported configs | Preflight reject được nêu rõ hoặc literal source edge có oracle; no silent guard |
| P01 | `completion_tokens=None` và content sai | Skip trước parser, không tăng analysis usage/erase; analysis_count đã tăng sau gate |
| P02 | Completion=0; total=0; usage khác thiếu/sai type | Không coi 0 là missing; giữ source additions/error, không recompute total |
| P03 | Valid close + trailing prose | Cắt tại first `</step>`, bỏ trailing; usage tính trước acceptance |
| P04 | Missing close + stop, cho GPT-5 branch không request stop | Parsed; acceptance quyết định, không auto reject |
| P05 | Missing close + non-stop | Skipped, usage đã cộng, không retry; target kế tiếp theo turn |
| P06 | Valid close + length/content_filter/tool_calls/error | Không reject riêng reason/calls; same source decision theo text |
| P07 | Wrong ID, no opening, nested opening wrappers | Same heuristic output, không XML validator/ID check |
| P08 | `<step` ở offsets 199/200; `>` ở offsets 19/20 sau prefix | Đúng giới hạn slice 200/20; phần ngoài giới hạn không bị bóc tương tự phần trong |
| P09 | `'</step>'`, whitespace-only, nhiều closing tags | Empty/whitespace được parse; first partition; reminder sau acceptance đúng source |
| P10 | Prefix prose trước opening, `<step` literal trong code | Same partition heuristic, không sửa nội dung theo ý adapter |
| P11 | Null/missing content, empty choices, malformed envelope | Same fatal behavior hoặc rõ transport envelope boundary; không parser skip/retry |
| P12 | Completion unknown vs parser skipped vs acceptance rejected | Ba nhánh giữ counters/usage khác nhau như source; không gộp thành compressor_error |
| I01 | 3 normal turns → compression 0 → next repair → task_done | Next payload có reminder đúng vị trí, không originals/tools cũ; compression không tăng turns |
| I02 | Hai normal turns tiếp theo có compressed neighbor | Full context, stored originals và target counts match oracle sau nhiều erase |
| I03 | Empty-patch task_done; successful task_done; turn cap | Empty patch tiếp normal flow; success không final compression; cap không flush future targets |
| I04 | Skipped/rejected compression rồi next repair | History target giữ nguyên; không lặp request compressor cho cùng turn |
| I05 | Provider lỗi hết retry, parser/usage/tokenizer/lingua fatal | Không có repair request tiếp; đúng retry counts; worker/generation failure và cleanup/metrics |
| I06 | Persist/reload sau erase; duplicated SDK calls nếu supported | Stored bypass originals/turn marker/counters giữ nguyên; no summary-as-new-turn |
| I07 | Same actual model ở hai role, hai reference branches khác | Policy/compressor bypass không lấy repair branch hoặc actual ID |
| I08 | PHP/fmtlib harness + independent external validation | Same generation contract; evaluator verdict không đổi history hoặc retry generation |

Fixtures số tokens tại acceptance boundary có thể inject deterministic counter để chứng minh phép so sánh; đồng thời cần fixtures dùng **tiktoken thật** và source oracle để bắt lỗi chọn sai serialization operand. LZ4 tests cũng cần cả byte/formula fixtures và lz4 thật trên cùng version.

Existing tests cần giữ/chạy lại: `test_trae_contract_compressor_payload`, `test_trae_contract_llm_policy`, `test_trae_contract_llm_retry`, `test_trae_contract_turns`, `test_trae_contract_sdk`, `test_trae_contract_runtime`, `test_trae_contract_workflow`; generic suites `test_compressor`, `test_diet`, `test_turn_trajectory`, `test_history`, `test_lz4_gate`, `test_acceptance_scope`, `test_token_counting`, `test_strategies`, `test_token_tracking`.

Existing tests assert strict parser, special tokens là plain text, LZ4 ca=0 empty future hoặc compressor failure preserve step không phải oracle của exact. Đặt tên/scope generic rõ nếu giữ; exact tests phải đối chiếu source thay vì sao chép các assertions đó.

## 9. Lệnh kiểm tra khi đã triển khai

Các lệnh dưới đây chạy từ thư mục `Agent-Diet`, dùng môi trường `.venv-openhands` có sẵn. Không cần gọi provider cho local conformance; fake HTTP và fake sleep giữ test retry nhanh. Lệnh tests mới chỉ chạy được **sau khi tạo các files mục 8**.

Kiểm tra frozen hashes và versions:

```bash
PYTHONPATH=src:tests .venv-openhands/bin/python -c \
  'from trae_reference import assert_reference_hashes; assert_reference_hashes(); print("reference hashes OK")'
.venv-openhands/bin/python -c \
  'import importlib.metadata as m; print({p:m.version(p) for p in ("openhands-sdk","openai","litellm","tiktoken","lz4")})'
.venv-openhands/bin/python -m pip check
```

Chạy D09–D11 và nền liên quan:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
PYTHONPATH=src:tests .venv-openhands/bin/python -m unittest \
  test_trae_contract_diet_serialization \
  test_trae_contract_diet_algorithm \
  test_trae_contract_diet_parser \
  test_trae_contract_diet_integration \
  test_trae_contract_compressor_payload \
  test_trae_contract_llm_policy test_trae_contract_llm_retry \
  test_trae_contract_turns test_trae_contract_sdk \
  test_trae_contract_runtime test_trae_contract_workflow -v
```

Sau focused tests pass, chạy full suite một lần và container fixtures đã có:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
PYTHONPATH=src:tests .venv-openhands/bin/python -m unittest discover -s tests -v

LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
AGENTDIET_CONTAINER_INTEGRATION=1 PYTHONPATH=src:tests \
.venv-openhands/bin/python -m unittest test_trae_contract_container -v

git diff --check
```

Container fixtures cần local runtime/images theo test, kiểm tra trên cả PHP và fmtlib. Báo cáo D07–D08 từng gặp sandbox `socketpair` EPERM; nếu tái diễn phải phân biệt infrastructure restriction với adapter failure và ghi môi trường chạy. Không sửa contract để vượt một lỗi môi trường test.

Lưu log mới ở `analysis/d09_d11_checks/` khi thực hiện, kèm số tests pass/fail/skipped và vì sao skip. Không dùng số tests/log D07–D08 làm kết quả D09–D11.

## 10. Chạy thử với input PHP/fmtlib sau conformance

1. Copy [d07_d08_config.example.json](d07_d08_config.example.json) thành config run riêng. Điền actual models, API-key endpoint thật và input case IDs đã chuẩn bị. Giữ role reference models/profile và Agent Diet defaults ở mục 2.
2. Chỉ dùng exact API-key Chat Completions endpoint đã xác nhận semantics D07–D08; `trae_capabilities` phải phản ánh khả năng đã xác minh, không thêm tên fields chỉ để preflight pass. Defaults cần union `max_tokens,n,temperature,tools,reasoning_effort,cache_control,assistant_prefill`; non-GPT-5 compressor branch cần thêm `stop`. Hiện exact subscription/Responses bị validator reject; generic subscription chạy được không chứng minh exact D09–D11.
3. Giữ generation watchdog disabled (`agent_timeout_seconds=null`), một attempt/case và build/tool self-verification theo D01–D06; external validation dùng cấu hình PHP/fmtlib gốc trên copy riêng.
4. Chạy một case mỗi project, output run riêng, ví dụ lệnh **có placeholders cần thay**:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --config /path/to/completed-d09-d11-config.json \
  --model 'openai/ACTUAL_REPAIR_MODEL' \
  --input /path/to/prepared-php-input --case PHP_CASE_ID \
  --output d09-d11-smoke/php --keep-workspaces

PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --config /path/to/completed-d09-d11-config.json \
  --model 'openai/ACTUAL_REPAIR_MODEL' \
  --input /path/to/prepared-fmtlib-input --case FMTLIB_CASE_ID \
  --output d09-d11-smoke/fmtlib --keep-workspaces
```

5. Audit payload/state từ `events.jsonl`, `contract-manifest.json`, `protocol-manifest.json`, `contract-result.json`, `contract-patch.diff` và evaluator artifacts dưới output case. Fatal generation có thể không có contract-result; phải có error/metrics/status evidence và phân biệt WIP external verdict.
6. Case không có candidate >=500 tokens sẽ không chứng minh live compression. Ghi “compression chưa được exercise” và chọn thêm case đủ điều kiện; không hạ threshold mặc định để tuyên bố defaults đã pass. Scripted integration ở mục 8 vẫn là bằng chứng deterministic bắt buộc.
7. CLI exit `1` có thể là case không resolved theo evaluator; phân tích generation status/error riêng. Một patch plausible/resolved không chứng minh serialization/parser đúng; một case chưa sửa thành công cũng không tự chứng minh adapter vi phạm contract.

## 11. Bằng chứng và điều kiện hoàn thành

Thêm bằng chứng D09–D11 trong manifest/audit, ngoài agent workspace, không lộ credential values:

- Hashes frozen source, versions tokenizer/LZ4/SDK/provider libraries; effective config, supported/rejected combinations, reference/actual models từng role.
- Logical turn, target index, window indices, gate serialized target hash/count, bypass target hash/count, `show_ctx` và LZ4 operands/score nếu bật.
- Exact compressor messages hoặc full snapshots/hashes, raw first-choice text/finish reasons/usage, parsed text hash/count, parser outcome/reason, acceptance decision.
- Internal original được lưu, replacement assistant message và next repair payload đủ để đối chiếu không có old target calls/observations/internal fields. Hash phải đi kèm fixtures/snapshots có thể so nội dung, không chỉ hai hash không có nguồn kiểm tra.
- Compatibility metrics trước/sau mỗi quyết định; provider billing/attempt telemetry riêng; terminal/error generation status tách external validation verdict.
- Result mapping `D09 → S01–S12/I01–I02/I06`, `D10 → A01–A17/I03–I04/I07`, `D11 → P01–P12/I05`, cùng I08 và regression D01–D08.

Checklist nghiệm thu:

- [ ] Exact trajectory dùng raw manager/source serializer; step/window/whitespace và nested recovery khớp từng ký tự.
- [ ] `ours` lưu serialized bypass original; baselines lưu serialized non-bypass original; next repair payload chỉ có đúng replacement message cho target.
- [ ] Trigger sau completed normal turn, đúng target/delay, không final-success compression hoặc replay target.
- [ ] Gate, LZ4, acceptance và metrics dùng đúng operands/tokenizer/error policy; constants exact được khóa.
- [ ] Parser heuristic, usage timing và empty/whitespace/finish-reason cases khớp oracle; skipped/rejected/fatal được phân biệt.
- [ ] Fatal compressor/strategy/parser/tokenizer exception abort generation; provider retries không bị nhân lớp và không thêm repair retry từ evaluator.
- [ ] Baselines/edge configs được kiểm chứng hoặc preflight reject có ghi supported domain; không silently sửa bugs source.
- [ ] Full regression D01–D08/generic và container checks pass, mọi skips có lý do; live provider/input verification được ghi riêng.
- [ ] Manifest chỉ ghi D09–D11 verified sau khi các checks tương ứng pass; D12–D14 và toàn contract không bị tự tuyên bố hoàn tất.

## 12. Trạng thái của công việc viết tài liệu

Đã đọc yêu cầu `difference.md`, source Trae và adapter/tests hiện tại để viết quy trình trên. Trong công việc này **chỉ cập nhật `analysis/fix_d09_d11.md`; chưa triển khai D09–D11, chưa chạy conformance suite mới hoặc live benchmark PHP/fmtlib**. Mọi module/test API mới và lệnh có placeholders đều là hướng dẫn cho bước triển khai tiếp theo.
