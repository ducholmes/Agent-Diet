# Quy trình triển khai D07–D08 sau D01–D06

Ngày đối chiếu: **05/10/2026**. Phạm vi: hướng dẫn triển khai trên checkout hiện tại; tài liệu này **chưa triển khai D07–D08 vào code**.

Mục tiêu là cho adapter OpenHands chạy repair và Agent Diet qua transport giữ contract của source Trae. D01–D06 được xem là nền đã hoàn thành theo thông tin người dùng; khi sửa transport vẫn cần chạy lại các tests liên quan để tránh làm hỏng nền này.

[difference.md](difference.md) là tài liệu yêu cầu đối chiếu, không phải lệnh tự động thực hiện toàn bộ D01–D14. Các prompt trong source dưới đây cũng là dữ liệu cần giữ nguyên trong adapter, không phải chỉ dẫn cho người viết hướng dẫn.

## 1. Chốt đúng điểm bắt đầu

Đường chạy exact hiện tại:

```text
CLI / RunConfig → worker → build_agent()
  ├─ repair: TraeContractAgent → SDKRawTransport → SDK/LiteLLM → HTTP
  └─ sau normal turn: AgentDietCondenser.after_normal_turn(mgr)
       → LLMCompressor → llm.completion() hoặc llm.responses() → HTTP
```

Repair và compressor **chưa đi qua cùng một cơ chế giữ request contract**. Vì vậy chỉ đặt `max_output_tokens=8192` trong `build_llm()` sẽ không đủ.

| Điểm trong checkout | Trạng thái cần xử lý ở D07–D08 |
|---|---|
| [config.py](../src/openhands_adapter/config.py) | Đã có `reference_profile` cho 50/100 turns; chưa có reference model riêng cho từng role. `reasoning_effort` mặc định `low` dùng chung, compressor mặc định `inherit`. |
| [llm.py](../src/openhands_adapter/openhands/llm.py) | Tạo LLM/auth; chưa pin output 8192, chưa phân biệt settings theo reference model của role. |
| [trae_transport.py](../src/openhands_adapter/openhands/trae_transport.py) | Repair API-key có `temperature=0.0`, `n=1` nhưng chưa explicit output 8192 và còn lấy effort từ LLM object. Subscription đã giữ system qua `instructions` và raw tool arguments. |
| [compressor.py](../src/openhands_adapter/openhands/compressor.py) | Còn thêm câu `Return <step ...>...`; prefill mặc định tắt; stop theo SDK capability; chưa có system cache block và model-derived variant. |
| [agent.py](../src/openhands_adapter/openhands/agent.py) | Cần truyền role/reference policy vào cả repair transport và compressor. |
| [diet/condenser.py](../src/openhands_adapter/diet/condenser.py) | Hook exact đã có; vẫn serialize không bypass trước khi gọi compressor. Cần nối bypass vào compression window, giữ gate không bypass. |
| Tests subscription hiện có | Chứng minh một phần repair contract; đang assert **không có** `max_output_tokens`. Không chứng minh output cap, effort hay compressor prefill. |

Đối với subscription, báo cáo cũ trong `difference.md` về việc system bị chuyển thành user prefix đã được đường repair riêng cải thiện. Không dùng nhận xét cũ đó để sửa lại D01; tập trung vào các khoảng trống D07–D08 còn thấy trong code hiện tại.

Nguồn làm oracle:

- [utils/llm_polytool.py](../artifact/artifact/code/trae_agent/utils/llm_polytool.py): wrapper, params, retry, NullCache.
- [agents/traj_analyzer.py](../artifact/artifact/code/trae_agent/agents/traj_analyzer.py): compressor defaults, messages, prefill, bypass, stop.
- [agents/expert.py](../artifact/artifact/code/trae_agent/agents/expert.py): repair caller, `MessageManager.format_messages()` và serializer.
- [tests/trae_reference.py](../tests/trae_reference.py): cơ chế AST oracle đã dùng cho D01–D06.

## 2. Quyết định protocol trước khi sửa transport

### 2.1. Tách ba thông tin độc lập

1. **Turn profile**: `trae_verified` = 50 turns + reminders; `trae_multiswe` = 100 turns + không budget reminders. Giữ phần D06.
2. **Reference protocol model**: tên model trong cấu hình Trae dùng để quyết định prompt/request branches.
3. **Actual model**: ID thật gửi endpoint. Có thể thay theo phạm vi cho phép, với điều kiện transport/model chấp nhận protocol đã chọn.

Nếu đang khớp cấu hình default trong `traj_analyzer.py`, reference repair là `claude4-sonnet`, reference compressor là `gpt-5-mini-2025-08-07`. Nếu đối chiếu một dòng thí nghiệm khác, lấy reference models từ dòng đó; không gắn mặc định hai tên này cho mọi thí nghiệm.

Đề xuất thêm các fields sau. **Đây là thiết kế cần triển khai, chưa phải config/CLI đang hoạt động**:

```text
openhands.repair_reference_model = "claude4-sonnet"
agentdiet.compressor_reference_model = "gpt-5-mini-2025-08-07"

# Fields actual model đã tồn tại:
openhands.model = <actual repair model>
agentdiet.compressor_model = <actual compressor model>
```

Cùng truyền fields mới qua `from_mapping()`, `from_env()` nếu expose env, `to_dict()/asdict`, worker-config round trip, và CLI overrides nếu expose flags. Tests phải chứng minh process worker nhận đúng reference models, không tự suy lại từ actual IDs.

`'gpt-5-' in 'gpt-5.6-sol'` là **False**. Không sửa predicate nguồn thành `startswith('gpt-5')`. Áp literal predicate lên **reference model** để giữ branch khi đổi actual model; ghi cả reference/actual vào manifest. Trong generic mode có thể chọn settings khác nhưng phải tách khỏi exact profile.

Không dùng actual repair model để quyết định bypass của compressor. `compressor_model='inherit'` chỉ có nghĩa dùng cùng actual model cho hai role; không có nghĩa dùng cùng reference policy. Với run exact dùng `ours`, nên yêu cầu actual compressor được khai báo rõ, hoặc người chạy chủ đích ghi `inherit` trong config thay vì nhận hidden default.

### 2.2. Chọn transport theo khả năng giữ contract

Đường khởi đầu dễ kiểm chứng là **API-key + endpoint Chat Completions tương thích cho cả hai role**. Đây là lựa chọn triển khai đề xuất, không phải khẳng định mọi endpoint kiểu này đều hỗ trợ prefill/cache.

| Đường chạy | Điều kiện được ghi nhận là exact D07–D08 |
|---|---|
| API-key / Chat Completions | Giữ exact message roles/content, trailing assistant continuation, params, tools/cache placement; kiểm tra HTTP sau provider wrapper. |
| Subscription / Responses hiện tại | Chưa đủ: SDK selector bỏ output cap và reasoning trong subscription; compressor mặc định bỏ prefill. Chỉ dùng cho exact khi triển khai và chứng minh được semantics còn thiếu. |
| Repair subscription + compressor API-key riêng | Có thể thiết kế credentials/endpoint theo role; vẫn phải chứng minh riêng repair cap/params. Đổi compressor transport không tự làm repair exact. |

Việc đặt một assistant message vào Responses input **không tự chứng minh** đó là prefill để model tiếp tục ngay sau `<step ...>`. Tương tự, cắt response ở client sau 8192 tokens không tương đương giới hạn generation ở provider.

Thêm validation trước generation: nếu transport/model không giữ được role policy thì báo lỗi có tên role và các yêu cầu không đáp ứng. Không tự tắt prefill, bỏ cap, chuyển system thành user, bỏ cache metadata, hoặc viết thêm câu wrapper để run tiếp trong exact profile. Nếu endpoint không hỗ trợ, chọn model/transport phù hợp hoặc chạy `reference_profile=generic` với manifest nói rõ khác biệt.

Các nhận định về subscription trên dựa vào **SDK 1.49.5 và code local đang cài**, không phải xác nhận live endpoint hiện tại. Không suy ra mọi phiên bản/provider tương lai có cùng giới hạn.

## 3. D07: pin request và retry cho cả hai role

### 3.1. Viết policy độc lập với SDK/model metadata

Đề xuất module `compat/trae_llm_policy.py` với immutable policy theo role. Không đặt policy vào LLM object dùng chung rồi mutate mỗi khi đổi role, vì `inherit` có thể chia sẻ object.

Bảng params **sau wrapper reference**:

| Field | Repair, non-GPT-5 reference | Repair, GPT-5 reference | Compressor, non-GPT-5 reference | Compressor, GPT-5 reference |
|---|---|---|---|---|
| `max_tokens` | 8192 | 8192 | 8192 | 8192 |
| `n` | 1 | 1 | 1 | 1 |
| `temperature` | 0.0 | Không có field | 0.0 | Không có field |
| `stop` | Không có field | Không có field | `</step>` | Không có field |
| `reasoning_effort` | Không có field | `low` | Không có field | `low` |
| `tools` | Exact Trae `TOOLS` | Exact Trae `TOOLS` | Không có field | Không có field |

GPT-5 reference ở đây là literal `'gpt-5-' in reference_model`. Ví dụ default: repair giữ non-GPT-5 column, compressor giữ GPT-5 column dù actual IDs được thay.

Không gửi `top_p`, API seed hay explicit `tool_choice`. Field **không có** khác với gửi `null`, `[]`, `auto` hoặc giá trị default. Compressor caller truyền `tools=[]` trong source nhưng wrapper xóa field đó trước gửi HTTP; adapter phải match kết quả sau wrapper.

Pseudocode cho phần semantic policy:

```python
def reference_params(role, reference_model):
    params = {"max_tokens": 8192, "n": 1, "temperature": 0.0}
    if role == "compression":
        params["stop"] = "</step>"
    if "gpt-5-" in reference_model:
        params.pop("temperature", None)
        params.pop("stop", None)
        params["reasoning_effort"] = "low"
    return params
```

Model ID wire vẫn là **actual model**. Hàm trên chỉ tạo params; repair tools và compressor messages được ghép riêng. Nếu hỗ trợ variant qwen trong reference, giữ caller rule `'qwen3-235b-a22b-instruct-2507' in reference_compressor_model` → `stream=True`, và test usage/finish reason qua streaming. Default GPT-5-mini không có caller stream setting này.

### 3.2. Nối policy vào transport đang chạy

Thứ tự thay đổi:

1. `build_llm()` nhận role để tạo LLM/auth với settings phù hợp; exact mode không dùng `config.reasoning_effort='low'` chung làm nguồn quyết định.
2. `SDKRawTransport` nhận role/reference policy. Giữ interface `transport(messages, tools)` của `TraeContractAgent`; thêm entry point cho compression hoặc transport cùng loại được bind role compression.
3. `LLMCompressor` exact gửi raw message dicts qua transport đã bind compression. Generic path có thể giữ SDK public methods riêng.
4. `build_agent()` tạo/bind policy riêng cho hai role, kể cả khi chúng dùng chung actual LLM.
5. Giữ output tuple/usage shape mà repair dispatcher và token tracking đang dùng. Thay transport không được thêm logical turns hoặc làm thay đổi batch execution.

Ở SDK local, `_transport_call()` đi thẳng tới `_prepare_transport_kwargs()` rồi LiteLLM, **không đi qua** `select_chat_options()` của public `completion()`. Do đó đặt `LLM(max_output_tokens=8192)` không bảo đảm raw repair transport gửi cap. Ngược lại public compressor path có selector inject sampling defaults/capability behavior. Hai đường phải được kiểm tra và khóa riêng hoặc hợp nhất ở exact transport.

Các điểm cần audit sau SDK/LiteLLM:

- `_prepare_transport_kwargs()` có thể thêm `seed` và dùng `drop_params`; kiểm tra **HTTP body** chứ không chỉ kwargs. Không cho silent parameter dropping che mất contract.
- `select_chat_options()` có thể thêm `top_p`, đổi token field, bỏ sampling theo actual model capabilities.
- `select_responses_options()` thêm `tool_choice='auto'`, bỏ cap/reasoning trong subscription. Transport exact phải kiểm soát fields này; hiện repair subscription chưa làm đầy đủ.
- `extra_body`, fallback/routing, automatic prompt cache processing không được override policy/messages.

Có thể đặt `max_output_tokens=8192`, `top_p=None`, `top_k=None`, `seed=None`, `reasoning_effort` theo role và tắt SDK prompt marker auto trong object để giảm drift; **wire policy vẫn là nơi quyết định cuối cùng**. Nếu endpoint cần `max_completion_tokens`/`max_output_tokens`, phải kiểm chứng mapping và semantics 8192; không gửi đồng thời hai token-cap fields hoặc bỏ cap vì SDK không hỗ trợ.

Khả năng actual model không nhận `temperature=0.0` của non-GPT-5 reference là lỗi tương thích cần xử lý theo mục 2.2. Model exception không cho phép SDK tự đổi sang policy khác.

### 3.3. Port đúng retry wrapper nguồn

Nguồn đang dùng `send_request_openai`, không phải policy retry riêng của Azure. Giữ:

- Tối đa **12 attempts tổng**, không phải một attempt đầu cộng 12 retries.
- Bắt `Exception`; `completion is None` cũng là failure của attempt.
- Chờ `2**retries` với `retries` từ 0 đến 11, **kể cả sau lỗi cuối**: `1, 2, 4, …, 2048` giây; tổng sleep khi tất cả fail là 4095 giây.
- Sau hết attempts, wrapper trả `None`; lớp tương đương `get_llm_response()` raise `RuntimeError('no response from api')`.
- Không thay messages/model/settings giữa attempts; không biến API retry thành một repair attempt mới.

Pseudocode:

```python
def send_with_reference_retry(one_attempt, sleep):
    for retries in range(12):
        try:
            completion = one_attempt()
            if completion is None:
                raise Exception("completion is None")
            return completion.model_dump()  # dict adapter: normalize tương đương
        except Exception:
            sleep(2 ** retries)
    return None

# Caller: nếu result is None → RuntimeError('no response from api').
```

Normalization của provider response nằm trong attempt như `.model_dump()` nguồn; parser compression/reduction decision không nằm trong retry. Không retry một kết quả nén bị gate từ chối, và không retry vì telemetry/logging lỗi.

Chọn **một lớp sở hữu retry**. Nếu triển khai wrapper trên, vô hiệu các request retry bên dưới ở SDK/LiteLLM/OpenAI client; số HTTP attempts cần test độc lập. `num_retries=12` trên SDK không tương đương policy này: SDK có loại exception/backoff khác, và raw transport không đi qua public retry decorator.

OpenAI library trong artifact gốc không pin version/default retries. Đối với conformance mới, pin versions, ghi rõ inner retries disabled là assumption, và chứng minh wrapper bằng oracle stub `OpenAI`. Không tuyên bố biết tổng HTTP retries của mọi run lịch sử.

Giữ local completed-response cache tắt như `NullCache`: hai request giống nhau vẫn gọi provider. Provider prompt cache của D08 là cơ chế khác. Watchdog harness ngắn hơn chuỗi retry có thể abort run; ghi harness abort, không ghi đã chạy đủ retry reference.

### 3.4. Điều kiện kết thúc D07

- Cả repair/compressor dùng role policy đúng sau process round trip.
- HTTP body match bảng params, actual model đúng, tools đúng và không có fields tự thêm.
- Test đủ 12 attempts, chuỗi sleep, `None` response, thành công giữa chuỗi; mock sleep để tests chạy nhanh.
- Bắt tầng HTTP chứng minh không có nested retries hay local response cache.
- Transport không tương thích bị reject trước repair; generic mode được ghi khác exact.

## 4. D08: phục hồi messages, prefill, cache và bypass

### 4.1. Tạo message builder thuần, giữ nguyên base prompt

Đề xuất `build_compression_messages(context, step_index, policy)` trong `diet/prompts.py` hoặc module compatibility riêng. Builder trả raw dicts, không phụ thuộc SDK capabilities và không sửa `context` đã serialize.

Base `SYSTEM_PROMPT` hiện trùng constant nguồn; thêm test literal equality với `traj_analyzer.SYS_PROMPT`. Không viết lại prompt cho model mới.

Nếu compressor reference có `'gpt-5-'`, effective system là:

```python
system = SYSTEM_PROMPT.replace("think", "talk").replace("agent", "engineer")
```

Đây là replace toàn chuỗi, không chỉ đổi câu đầu hoặc XML tag trong system. Không áp replace này lên repair system prompt.

Payload message của compressor với `USE_CACHING=True`:

```python
messages = [
    {
        "role": "system",
        "content": [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
    },
    {
        "role": "user",
        "content": context + f"\n\nNow, compress the step {idx}.",
    },
    {
        "role": "assistant",
        "content": (
            f"Sure. Here is the compressed content of step {idx}: "
            f'<step id="{idx}">'
        ),
    },
]
```

`idx` ở ví dụ là `step_index`. Kiểm tra exact roles, list/string content shape, hai newline trước suffix, dấu chấm cuối, và **không có newline/closing tag** sau prefill.

Trong `LLMCompressor` exact:

1. Bỏ câu `Return <step id=...> followed by...` hiện tự thêm.
2. Luôn có assistant prefill như source. Nếu giữ option `assistant_prefill`, exact profile phải bật/reject false; generic có thể có lựa chọn riêng.
3. Gửi qua compression transport D07 với field `tools` bị bỏ ở wire.
4. Đưa caller stop `</step>` vào reference policy; chỉ GPT-5 reference branch được xóa nó. Không dùng `_model_features().supports_stop_words` để tự chọn stop.
5. Với transport không có assistant continuation semantics, preflight reject; không biến prefill thành user instruction để vượt validation.

Prefill là **một phần input**, không phải đoạn adapter tự ghép vào response cho parser. Provider có thể trả chỉ body hoặc lặp opening; việc phân tích response thuộc D11. Không thay usage bằng token estimate của prefill.

### 4.2. Nối bypass vào window tại hook exact

Hiện `after_normal_turn()` gọi `extract_step_into_traj(index)` không có bypass rồi đưa `candidate.context` vào compressor. Đổi system sang talk/engineer mà context còn `<think>` vẫn chưa đạt D08.

Tạo hai representation riêng:

```text
gate representation:
  mgr.extract_step_into_traj(i, bypass_filter=False)
  → threshold / length / LZ4 như hiện được đối chiếu ở D10

compressor representation:
  mgr.extract_step_into_traj(i, bypass_filter=policy.bypass_filter)
  → compression messages D08
```

Chỉ đổi representation gửi compressor. Với target `idx`, window nguồn có thứ tự:

```python
window = [
    mgr.extract_step_into_traj(i, bypass_filter=policy.bypass_filter)
    for i in range(idx - ctx_before, idx + ctx_after + 1)
]
if not show_ctx:
    window = [part if pos == ctx_before else ""
              for pos, part in enumerate(window)]
context = "\n".join(window)
```

Đoạn này giả định candidate/indices đã hợp lệ như nguồn; phần chọn target và gate vẫn kiểm chứng ở D09–D10. Defaults sau ba normal turns chọn step 0, window là initial user `-1`, step 0, step 1, step 2.

Với `show_ctx=False`, vẫn giữ shape của window và join. Defaults cho chuỗi `"\n" + target + "\n\n"`; không chỉ gửi riêng `target.serialize()`. Việc này chạm whitespace của D09, cần có fixture khóa ngay khi nối D08.

Không thực hiện `context.replace('think', 'talk')`: cách đó sẽ đổi tên tool `think`, code/log/issue text và JSON arguments. Bypass phải xảy ra ở serializer bằng lựa chọn tag `<think>`/`<talk>`; `<call tool="think">` vẫn giữ nguyên.

Không sửa `<think>` literal nằm trong initial user issue; `extract_step_into_traj(-1, ...)` trả nguyên issue. Không đưa hidden reasoning metadata của provider vào window. Với neighbor đã nén, nguồn dùng `agent_erased` để khôi phục original; fixture exact cho representation/nested wrapper thuộc D09, không unwrap để làm payload đẹp hơn.

### 4.3. Giữ cache placement đúng role

Compressor system có một text block với ephemeral marker như ví dụ trên. Các user/prefill messages không được tự đánh marker thêm.

Repair cache placement trong `compat.trae_contract.MessageManager.format_messages()` hiện đã port logic nguồn:

- First repair request: không marker trên initial system/user.
- Khi trajectory có messages và message cuối không phải assistant: đánh marker tại content của **message trajectory cuối**, sau đó mới append user budget reminder.
- Khi message cuối là assistant: thêm Continue/reminder theo D06; không chèn marker mới vào assistant hoặc reminder.

Do đó D08 cần **giữ và test** logic này qua provider wrapper, không thêm cache placement lần nữa trong transport. Tắt SDK automatic marker processing ở exact path để tránh đánh initial system hay mọi message.

`cache_control` là metadata provider. Endpoint có cơ chế caching riêng không có nghĩa nó thực thi ephemeral placement như reference. Nếu API projection không thể giữ metadata này, lưu discrepancy và không ghi D08 literal wire conformance pass. Generic run có thể hoạt động với cache khác nhưng phải được mô tả đúng.

### 4.4. Truyền role và giữ accounting

Trong `build_agent()`:

```text
repair policy     ← repair_reference_model
compression policy ← compressor_reference_model

repair LLM/transport → TraeContractAgent
compression LLM/transport → LLMCompressor
compression policy → after_normal_turn window builder
```

Giữ `compression_call(step_index)` quanh call compressor để token telemetry vẫn nhận role `compression`, kể cả `inherit`. Nếu transport mới đi qua raw SDK path, bảo đảm nó phát telemetry success/error đúng, thay vì đi vòng làm token usage biến mất.

Usage callback của compressor chỉ ghi một lần; không vừa callback vừa ghi lại cùng usage trong transport vào Diet metrics. Phân biệt SDK billing telemetry với counters/gates của Diet. Repair turn counter chỉ tăng ở response repair đã xử lý theo D06, không tăng theo retry/compressor call.

### 4.5. Điều kiện kết thúc D08

- Exact base/effective system, user suffix, trailing assistant prefill và content shapes match oracle.
- Bypass dùng reference **compressor** branch ở cả prompt và serialization tags.
- Stop presence/absence đúng bảng D07, không theo SDK capability metadata.
- Cache markers đúng positions sau provider wrapper; không extra tools/prompt text.
- `show_ctx=False` giữ newline structure và initial issue giữ nguyên.
- Endpoint được xác nhận chấp nhận cap/effort/prefill/cache mà không silently drop; không chỉ được mock chấp nhận payload.

## 5. Trình tự sửa theo các mốc có thể kiểm tra

| Mốc | Công việc | Kết quả cần có trước khi sang mốc kế tiếp |
|---|---|---|
| A | Ghi reference profile/models, actual roles, protocol và versions; thêm hashes oracle | Configuration round trip không mất role/branch; biết rõ actual endpoint cần gì. |
| B | Viết D07 params policy và D08 message builder thuần | Tests độc lập từ source Trae pass, không dùng adapter output làm expected. |
| C | Nối exact repair transport D07; pin retry một lớp | Wire repair match, tests D01–D06 liên quan vẫn pass. |
| D | Nối compressor transport/prefill/cache, rồi bypass window | Wire compression match; `inherit` không gây settings lẫn role. |
| E | Bổ sung reject transport không tương thích, capture payload/attempts | Subscription/API-key khác nhau được ghi rõ; fail trước generation nếu thiếu semantics. |
| F | Chạy scripted integration repair → compression → repair | D06 hook timing/turns không trôi; next repair payload nhận replacement từ đúng target. |
| G | Provider smoke, rồi một PHP/fmtlib case nhỏ | Xác nhận transport chạy thật; audit fields và tách external verdict. |

File nên sửa: `config.py`, `cli.py` nếu expose mới, `openhands/llm.py`, `openhands/trae_transport.py`, `openhands/compressor.py`, `openhands/agent.py`, `diet/prompts.py`, và phần build window của `diet/condenser.py`. `worker.py` chỉ đổi khi cần nối preflight/persistence; không cần viết lại repair loop của D06.

Có thể chia thành hai commit logic: policy/transport/retry D07, rồi messages/bypass/cache D08. Nhưng lần nghiệm thu cuối phải kiểm tra cả hai roles cùng lúc vì D08 cần transport D07.

## 6. Bộ kiểm chứng bắt buộc

### 6.1. Oracle và fixtures

Mở rộng `compat/reference_hashes.json` với hash của `utils/llm_polytool.py` và `agents/traj_analyzer.py`; manifest hiện chưa có hai files này. Giữ hashes các files D01–D06.

Mở rộng AST loader trong `tests/trae_reference.py`:

- Load `send_request_openai` với stub `openai.OpenAI`, `NullCache`, `HashKey` và fake sleep để thu payload/retry thực sự từ wrapper nguồn.
- Load `SYS_PROMPT`/`perform_analysis_step_ours` và inject `MODEL`, `BYPASS_FILTER`, ctx settings, fake `get_llm_response`/token counter/manager phù hợp. Thu **messages và kwargs caller** từ source, sau đó áp source wrapper để có expected wire.
- Import oracle qua AST tránh chạy module-level env/provider setup trong artifact. Không sửa source reference cho phù hợp adapter.

Fixtures dùng credentials giả. Snapshot payload phải giữ whitespace/cache blocks/tool schemas đầy đủ; loại auth/header secrets khi lưu audit.

### 6.2. Ma trận tests

| Nhóm | Trường hợp | Assert cần có |
|---|---|---|
| Params | Hai roles × hai reference branches | Bảng D07; kiểm tra cả field absence. |
| Model mapping | Actual `gpt-5.6-sol`/alias khác, reference GPT-5-mini | Vẫn bypass/effort low/bỏ stop theo reference; actual ID wire đúng. |
| Role isolation | Repair non-GPT-5 ref, compressor GPT-5 ref, actual `inherit` | Không leak low/bypass/stop vào repair; không mutate shared policy/LLM theo call. |
| Configuration | JSON → config → worker-config → worker | Reference models và role settings giữ nguyên; conflicting exact overrides bị reject. |
| Wire | SDK + LiteLLM + HTTP mock serialization | Cap 8192; không seed/top_p/tool_choice mới; tools exact repair, tools absent compression. |
| Retry | Always fail; fail rồi success; provider trả None | 12 attempts và sleeps `2**0…2**11`; không sleep sau success; final `no response from api`. |
| Nested retry | HTTP 500/429/connection error được script | Actual HTTP attempts không bị nhân lên bởi inner clients. |
| NullCache | Hai request identical | Hai provider calls; không cached response local. |
| Messages | Base branch và bypass branch | Exact three-message list, suffix/prefill/system block, không Return sentence. |
| Bypass | Issue/code/arguments chứa chữ think/agent | Chỉ tag serializer đổi; issue/tool name/arguments không bị global replace. |
| Window | Step 0 với ctx 1/2, `show_ctx` true/false | Đúng -1/0/1/2 order; blank-neighbor newline shape. |
| Cache | First repair; tool-ending history; assistant-ending history; compression | Đúng positions trước reminder; không marker bổ sung. |
| Compatibility | Endpoint thiếu prefill/cap/effort/cache | Reject exact trước repair, không silently fallback. |
| Integration | Ba normal turns có target đủ dài, rồi task_done với patch | Một compression target; compressor không tăng repair turns; successful terminal không nén cuối. |
| Accounting | Role compression, shared actual model, API retry | Usage không double count, failures phân biệt attempts với logical turns. |

Không chỉ patch `LLM.completion()` rồi assert kwargs. Dùng HTTP mock như tests SDK/subscription đang làm để bắt payload **sau** các provider transforms. Mock wire chứng minh serialization local; provider smoke mới kiểm tra live endpoint chấp nhận/diễn giải protocol.

Tests hiện có trong `test_llm.py` đang yêu cầu effort low chung; một số tests `test_compressor.py` đang yêu cầu bỏ prefill, wrapper prompt và parser chặt. Khi thêm exact behavior, tách fixtures generic/exact; không giữ những expectations trái source làm chuẩn exact. Parser tests cần thay đúng trong D11, không xóa chỉ để suite pass.

Đề xuất test files mới: `test_trae_contract_llm_policy.py`, `test_trae_contract_llm_retry.py`, `test_trae_contract_compressor_payload.py`. **Các files này chưa được tạo trong công việc viết hướng dẫn**.

### 6.3. Lệnh chạy tests

Chạy từ thư mục `Agent-Diet`. Lệnh baseline hiện có:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
PYTHONPATH=src:tests .venv-openhands/bin/python -m unittest \
  test_trae_contract_prompts test_trae_contract_turns \
  test_trae_contract_sdk test_trae_contract_subscription \
  test_llm test_compressor -q
```

`LITELLM_LOCAL_MODEL_COST_MAP=True` chỉ tránh tải metadata qua mạng trong test offline; không làm provider payload đúng contract và không thay thế live smoke.

Sau khi tạo ba test modules đề xuất:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
PYTHONPATH=src:tests .venv-openhands/bin/python -m unittest \
  test_trae_contract_llm_policy test_trae_contract_llm_retry \
  test_trae_contract_compressor_payload -v
```

Cuối cùng chạy suite hiện có để phát hiện ảnh hưởng tới D01–D06/config/worker:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
PYTHONPATH=src:tests .venv-openhands/bin/python -m unittest discover -s tests -v
```

Các tests container thực có thể cần Docker/image đã chuẩn bị; ghi rõ skip/failure môi trường. Không coi các tests hiện có đều pass là bằng chứng D07–D08 nếu chưa thêm assertions trong ma trận trên.

## 7. Cách chạy smoke sau khi code được triển khai

### 7.1. Provider smoke độc lập, trước benchmark

Gửi hai request nhỏ bằng **chính transport đã nối vào adapter**:

1. Repair request với exact system/tools và issue tối giản, thu một response cùng usage.
2. Compression request với window fixture nhỏ, exact three messages/prefill và role params, thu text cùng usage.

Lưu canonical message snapshots, redacted HTTP body, actual/reference models, protocol, versions, attempt count. Kiểm tra provider không báo unsupported fields, không trả output rỗng do params không được hỗ trợ và không làm mất roles/prefill. Một HTTP 200 chưa đủ chứng minh prefill continuation hay enforced cap; cần bằng chứng endpoint contract hoặc probe thích hợp cho semantics đó. Không gửi secret vào fixtures/audit.

Nếu subscription không vượt preflight, dừng run exact với lỗi tương thích và dùng API-key endpoint/model đáp ứng được role contract. Không gọi một run đã bỏ prefill/cap/effort là exact D07–D08.

### 7.2. Smoke một prepared case PHP/fmtlib

Sau khi fields đề xuất ở mục 2 đã được triển khai, tạo JSON config ghi rõ reference models, actual models, `auth`, `base_url`, `api_key_env`, và Diet defaults. Chọn một case nhỏ đã có image/build dependencies. Với CLI hiện có, mẫu lệnh dùng config đó:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --config /path/to/d07-d08-config.json \
  --input /path/to/prepared-cases --case '<case-id>' \
  --reference-profile trae_verified \
  --auth api-key --model '<actual-repair-model>' \
  --compressor-model '<actual-compressor-model>' \
  --diet-mode ours --diet-threshold 500 --ctx-before 1 --ctx-after 2 \
  --agent-timeout 0 --output d07-d08-smoke --keep-workspaces
```

Các path/model/case ở trên là placeholders phải thay. API key đọc từ env đã cấu hình, không đặt key literal vào command hay config. Dùng `trae_multiswe` nếu đó là reference profile đã chọn; không tăng turns vì build lâu.

Kiểm tra artifacts:

- Manifest phân biệt reference branch/actual model, repair/compression roles và transport.
- First repair request và các request sau vẫn giữ D01–D06.
- Có ít nhất một compression call thực tế với payload D08. Case kết thúc sớm hoặc steps không vượt threshold thì chưa kiểm tra được compression; dùng scripted integration/fixture để bảo đảm bao phủ, không hạ threshold của run exact chỉ để tạo bằng chứng.
- `contract-result.json`/`contract-patch.diff` thể hiện gen status và patch; usage ghi đúng role.
- External evaluator chạy sau generation trên copy riêng, không feedback vào repair. Verdict PHP/fmtlib không thay thế kiểm chứng request contract.

## 8. Ranh giới nghiệm thu và phần còn lại

**D07 hoàn thành** khi params/retry/no-response-cache của cả hai role match source wrapper và HTTP serialization. **D08 hoàn thành** khi messages/prefill/cache/model branch match source compressor, và transport giữ được semantics đã cam kết.

Sau hai bước này có thể kiểm chứng đường repair → Agent Diet LLM → repair hoạt động. Chưa được tuyên bố toàn adapter identical với Trae nếu D09–D14 chưa nghiệm thu, đặc biệt:

| Phần còn lại | Vì sao vẫn cần |
|---|---|
| D09 | Serializer/original recovery và whitespace ở mọi cases, gồm neighbor đã nén. D08 chỉ khóa phần window/bypass cần cho request. |
| D10 | Gate dùng serialization không bypass; acceptance dùng target trong bypass window và bare parsed output. Code hiện còn dùng chung candidate count. |
| D11 | Parser source có heuristics/skip khác parser adapter; exception sau provider retries phải thoát generation. Hook exact hiện còn catch rồi tiếp tục. |
| D12–D14 | Nghiệm thu diff/filter, SDK controls còn lại và đủ audit bằng chứng; phần nào đã làm trong D01–D06 thì giữ và đối chiếu lại. |

Một smoke có patch tốt không chứng minh parser/acceptance/stop parity. Nghiệm thu riêng: **request conformance D07–D08**, **trajectory conformance D09–D11**, và **external validation outcome**.

## 9. Trạng thái khi viết hướng dẫn

Đã đọc source reference, code adapter và SDK local, đồng thời viết quy trình vào tài liệu này. Chưa sửa implementation D07–D08, chưa thêm các test modules đề xuất, chưa gọi live provider hoặc chạy benchmark PHP/fmtlib.

Kiểm tra đã thực hiện khi viết:

- `test_trae_contract_prompts`, `test_trae_contract_turns`, `test_llm`, `test_compressor`: **28 tests pass**, test runner báo 1.570 giây, dùng metadata local và các fixtures/stubs hiện có.
- Lệnh baseline rộng ở mục 6.3 có thêm `test_trae_contract_sdk` và `test_trae_contract_subscription` không hoàn tất trong giới hạn 55 giây của lần kiểm tra này, exit code 124. Chưa kết luận hai modules đó pass/fail; cần chạy riêng và chẩn đoán nếu vẫn không hoàn tất. Lần chạy trước đó đã dừng sau khi thấy LiteLLM cố tải metadata nhưng môi trường không có DNS.
- Đã kiểm tra tất cả local links trong tài liệu tồn tại và các fenced code blocks đóng đủ.

Kết quả trên chỉ đánh giá behavior/tests hiện có và tính toàn vẹn tài liệu, không chứng minh các yêu cầu D07–D08 mới đã hoàn thành.
