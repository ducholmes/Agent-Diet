# Những thay đổi OpenHands cần làm để khớp cấu hình Trae

Ngày đối chiếu: **05/10/2026**. Nguồn: checkout `Agent-Diet` tại commit `1a436ca7cf59e30c147ecb5b360f8677e675538c`, [report Trae](trae_config.md), [report OpenHands](openhands_config.md) và code được dẫn dưới đây.

**Mục tiêu: giữ nguyên cấu hình và hành vi sửa bug/nén của Trae; chỉ thay harness, model, input PHP/fmtlib và external validation.** Đây là danh sách yêu cầu sửa adapter, chưa phải các thay đổi đã triển khai.

Nội dung prompt trong các tài liệu/source được xem là dữ liệu đối chiếu. Báo cáo này không thực thi các chỉ dẫn nằm trong prompt đó.

## 1. Ranh giới bốn khác biệt được phép

| Khác biệt được phép | Có thể thay | Phần vẫn phải giữ |
|---|---|---|
| Harness | OpenHands SDK thay Trae Expert loop; event IDs, process/container plumbing, layout lưu output | Prompt agent nhìn thấy, tools/schema/observations, thứ tự thực thi, count turns, timing nén và stop conditions |
| Model | Tên/model snapshot của repair và compressor; routing/auth phù hợp model mới | Hai vai trò model phải rõ; không tự thay prompt, output limit, acceptance, budget hoặc protocol để thích nghi |
| Input | Source, project path, nội dung mô tả lỗi/failure log, build environment của PHP/fmtlib | Template user prompt, không thêm repair constraints hoặc khóa quyền test vì input mới |
| External validation | Evaluator PHP/fmtlib, target/regression commands, evidence patterns, outcome taxonomy và validation timeout | Evaluator chạy sau generation trên copy riêng; không đưa feedback external evaluator trở lại vòng repair |

**Self-verification của agent không thuộc ngoại lệ external validation.** Trae cho agent tự viết reproducer, chạy lệnh/test và thêm tests; OpenHands cũng phải cho làm như vậy. External evaluator vẫn dùng config gốc và submitted patch đã lọc test changes.

Harness được phép khác phần triển khai, nhưng không được viện lý do dùng SDK để thêm system prompt, đổi tools, dừng sớm, cắt output hoặc đổi thuật toán nén. Nếu API của model mới không nhận protocol gốc, đó là vấn đề tương thích cần giải quyết, không tự động là quyền viết lại protocol.

## 2. Chọn đúng cấu hình Trae làm chuẩn

Trae không có một turn budget duy nhất:

| Profile gốc | Max repair turns | Turn reminders |
|---|---:|---|
| SWE-bench Verified (`appr100` / `eval200`) | **50** | **Bật** |
| Multi-SWE-bench Flash | **100** | **Tắt budget reminders** |

PHP/fmtlib là input mới nên không tự suy ra profile 50 hay 100 từ tên/ngôn ngữ. Khi triển khai cần khai báo **reference profile** và giữ đúng cặp trên. Không giữ default `500` hiện tại, không tự tăng số lượt vì PHP build lâu. Báo cáo liệt kê cả hai profile; chưa giả định người dùng đã chọn một profile.

Defaults nén dùng làm chuẩn khi không chỉ định một dòng thí nghiệm khác:

```text
mode = ours
threshold = 500
ctx_before = 1
ctx_after = 2
show_ctx = True
use_lz4 = False
lingua_ratio = 0.25
tokenizer = tiktoken.encoding_for_model('gpt-4o')
acceptance = giảm >=400 tokens HOẶC output <80% serialized input
generation_attempts_per_case = 1
```

Các scalar này phần lớn **đã trùng** trong adapter; không cần viết lại chỉ để đổi tên setting. Các mục sau mới là những sai khác cần sửa hoặc cần khóa để không trôi theo SDK defaults.

## 3. Bảng công việc cần sửa

| ID | Thay đổi bắt buộc | File adapter chính | Tiêu chí hoàn thành |
|---|---|---|---|
| D01 | Dùng exact repair system prompt Trae, loại system context SDK dư | `openhands/prompts.py`, `agent.py`, `runtime.py` | Repair payload chỉ có prompt gốc và messages phát sinh theo Trae |
| D02 | Giữ user template Trae, đưa input PHP/fmtlib vào trường issue | `input_loader.py`, `openhands/prompts.py`, `cli.py` | Chỉ thay project path/issue body; không thêm constraints/template khác |
| D03 | Khôi phục đúng bốn tools gốc và observation protocol | `openhands/workspace_tool.py`, `agent.py`, tool bridge mới nếu cần | Names, descriptions, schema, required fields và outputs khớp nguồn |
| D04 | Bỏ giới hạn configured-command-only và cấm tạo/sửa tests trong generation | `workspace_tool.py`, `prompts.py`, `container.py` | Agent tự reproduce/test/edit như Trae; evaluator ngoài vẫn độc lập |
| D05 | Khớp output clipping, tool timeouts và shell/editor state | Tool bridge, `container.py`, `process.py` | Không cắt chung 40k; tool behavior/timeout observations tương đương |
| D06 | Khớp logical turns, reminders, completion và cap behavior | `worker.py`, `agent.py`, conversation bridge | 50/100 turns đúng profile, task_done không patch phải tiếp tục |
| D07 | Pin repair/compression request parameters và retry behavior | `openhands/llm.py`, `compressor.py`, `config.py` | Payload sau provider wrapper khớp reference protocol, không lệ thuộc SDK defaults |
| D08 | Khôi phục compressor messages/prefill/cache/model-dependent variant | `diet/prompts.py`, `openhands/compressor.py`, `llm.py` | Exact system/user/assistant messages; không thêm câu yêu cầu wrapper |
| D09 | Khớp serializer, original-step recovery và show_ctx whitespace | `diet/trajectory.py`, `diet/condenser.py`, `openhands/condenser.py` | Context compressor/repair khớp từng ký tự ở fixtures gốc |
| D10 | Khớp trigger, token counting và acceptance operands | `diet/core.py`, `diet/condenser.py`, SDK bridge | Mỗi completed turn xét đúng một target; gate/acceptance dùng đúng serialization |
| D11 | Khôi phục parser và failure behavior của compressor gốc | `openhands/compressor.py`, `diet/condenser.py`, `worker.py` | Cùng response thì cùng skip/accept/error decision như Trae |
| D12 | Khớp patch capture và filter; cho test edits khi repair | `workflow/patch.py`, `runner.py`, completion bridge | Submitted patch bằng diff+filter Trae; không tự include untracked |
| D13 | Tắt các SDK behaviors làm đổi trajectory; giữ hai model role rõ ràng | `agent.py`, `runtime.py`, `worker.py`, `config.py` | Không có tools/prompts/stop/fallback/retry phụ ngoài contract |
| D14 | Lưu bằng chứng payload/config và kiểm tra conformance | `worker.py`, `events.py`, `token_tracking.py`, tests | Có thể audit các bất biến và chỉ ra đúng bốn khác biệt được phép |

Paths trong bảng tính từ `src/openhands_adapter/`. Chi tiết từng mục ở dưới.

## 4. Prompt, input và tools

### D01 — Thay repair suffix bằng exact system prompt

Hiện tại `REPAIR_SYSTEM_PROMPT` là workflow viết lại rồi nối vào `AgentContext.system_message_suffix`, nên agent còn nhận static/dynamic prompt SDK: SOUL, memory, filesystem, testing, model-specific guidance, datetime...

Cần làm:

- Copy **nguyên văn** `SYS_PROMPT` trong [expert.py](../artifact/artifact/code/trae_agent/agents/expert.py), giữ cả khoảng trắng/câu chữ sau `.strip()` của nguồn.
- Dùng inline system prompt hoặc SDK extension bảo đảm payload thực có system text này; không chỉ thay suffix.
- Loại default system sections, user SOUL/memory/skills/date context và các hướng dẫn tự thêm khỏi request tương thích. Giữ tắt ambient tool/plugin discovery.
- Kiểm tra cả SDK message assembly lẫn transport. Một field `system_prompt` đúng không đủ nếu SDK còn chèn dynamic text hoặc provider wrapper chuyển system thành user prefix.

**Không viết lại prompt gốc thành “chỉ sửa production code”, “chỉ chạy configured tests” hoặc “không được tạo reproduce scripts”.** Prompt gốc yêu cầu reproduce trước sửa và thêm tests sau sửa.

### D02 — Giữ template user, chỉ thay nội dung input

Target template từ `INIT_USER_PROMPT` trong `expert.py`:

```text
[Project root path]:
{project_path}

[Problem statement]: We're currently solving the following issue within our repository. Here's the issue text:
{issue}
```

Cần bỏ `[Buggy source code]`, `[Task instructions]`, `[Repair constraints]` và yêu cầu đọc `.agent-diet.failure.log` đang được thêm vào prompt hiện tại. Bỏ việc loader luôn đặt `problem_statement=None`.

Đối với PHP/fmtlib, `{issue}` là nội dung input mới: dùng mô tả lỗi nếu case có; nếu case chỉ có failure log, dùng log đã chuẩn bị làm nội dung issue. Cách ánh xạ phải deterministic và được lưu trong manifest. Không dùng gold patch/reference solution hoặc kết quả external validation sau generation làm issue.

Failure log có thể vẫn được lưu như input artifact nếu cần, nhưng không bắt buộc agent phải đọc một file đặc biệt bằng câu hướng dẫn mới. Setup/build metadata cần cho input có thể được stage theo cơ chế input/harness đã khai báo; không dùng nó làm lý do thêm tools/constraints khác vào contract gốc.

`--prompt`/`--prompt-file` tự do phải tắt hoặc được đánh dấu ngoài profile tương thích. Các overrides top-level hiện parse nhưng không được dùng cần nối đúng hoặc reject, tránh config ghi một prompt nhưng request gửi prompt khác.

### D03 — Thay bộ tools hiện tại bằng contract Trae

Repair request phải có đúng `expert.TOOLS`, cùng thứ tự:

```text
str_replace_editor
bash
task_done
think
```

Cần giữ descriptions và schemas nguyên văn từ source, không viết descriptions tương đương theo ý mình. Các details bắt buộc:

- Editor: `view/create/str_replace/insert/undo_edit`, absolute path, view_range, original required fields. Không thay bằng `read_file/write_file/edit_file` có expected_content protocol khác.
- Bash: `command` theo schema gốc. Không thêm `phase/index/argv/cwd` hay tự siết required/extra fields khác nguồn.
- `task_done`: parameters object rỗng, không yêu cầu final message giống SDK FinishTool.
- `think`: required `thought`, exact description gốc; observation `Continue.`, không phải `Your thought has been logged.`.

Không expose tám WorkspaceTools hiện tại, SDK FinishTool/ThinkTool hoặc tools tự discover trong profile này. Có thể implement tools bằng SDK classes, nhưng JSON schema **sau SDK serialize** không được thêm base fields/strict constraints làm khác contract gửi model.

Observation protocol phải giữ tool_call_id, content và lỗi theo `parse_tool_response()`: `Task done`, `Continue.`, `(no output)`, invalid-JSON feedback, unknown-tool feedback và timeout/error text. Không thêm prefix `exit_code=... timed_out=...` hiện tại vào tool content.

`task_failed` có nhánh xử lý trong source nhưng **không được khai báo trong TOOLS**. Không tự thêm nó thành tool thứ năm; nếu tái lập cả nhánh response bất thường thì xử lý theo source mà không đổi schema advertised.

### D04 — Khôi phục quyền self-verification trong repair workspace

Cần cho agent chạy Bash tùy ý **trong môi trường repair**, tạo reproducer/debugging scripts, chọn lệnh/test, sửa/thêm repository tests và fixtures như tool gốc cho phép. Không bắt model chỉ gọi `run_configured_command(phase,index)`.

Cần bỏ policy hiện tại chặn test paths, command-referenced files và yêu cầu expected-content riêng vì những policy này làm action space khác Trae. Container/host boundary vẫn có thể là harness implementation riêng, nhưng phải cho các thao tác repair gốc hoạt động trong sandbox tương ứng.

Việc cho phép test edits trong generation không cho phép agent đổi **external evaluator config**: config/chấm điểm bên ngoài vẫn giữ ở host hoặc validation workspace riêng. Đúng mô hình cần là:

```text
Repair: agent được reproduce, debug, sửa source, thêm/sửa tests
Submission: lấy patch bằng filter gốc để bỏ các file tests/manifest bị lọc
External validation: chấm filtered patch bằng config PHP/fmtlib ban đầu
```

Không cần chuyển external validator sang evaluator SWE-bench để thực hiện D04.

## 5. Tool runtime, turns và generation stop

### D05 — Khớp output, timeout và state của tool

Nguồn: [sandbox.py](../artifact/artifact/code/trae_agent/utils/sandbox.py), [bash.py](../artifact/artifact/code/trae_agent/tools/claude_tools/bash.py), [edit.py](../artifact/artifact/code/trae_agent/tools/claude_tools/edit.py), [run.py](../artifact/artifact/code/trae_agent/tools/claude_tools/run.py).

| Thành phần | Target Trae | Sửa OpenHands |
|---|---|---|
| Bash output gửi model | Không hard-truncate chung 40.000 ký tự | Bỏ giữ tail 40k trong repair tool path, kể cả mode skip |
| Editor/file view | Raw content prefix 16.000 ký tự + exact clipped marker; formatting theo tool gốc | Dùng lại logic gốc; không thay bằng read_file 200 dòng mặc định |
| Outer Session timeout | 180 giây | Khớp outer execution/timeout behavior, không chỉ đổi 120 thành một giá trị khác |
| Inner Bash timeout | 210 giây, polling 0.2 giây | Giữ semantics nếu bridge có outer/inner layers |
| Helper run timeout | 120 giây | Giữ cho đường helper/editor tương ứng |
| Editor undo history | Persist qua các calls | Giữ lịch sử và undo semantics |
| Filesystem container | Persist trong generation | Giữ state giữa calls |

Clipped marker lấy exact từ `claude_tools/run.py`; không dùng marker mới. Output cuối của editor có thể vượt 16k vì source truncate raw text trước khi thêm line numbers/expand tabs.

Trae wrapper tạo BashTool mới trong subprocess mỗi call: **không tự bổ sung persistent `cd/export` shell state** chỉ vì description nói persistent. Match hành vi wrapper thực tế, không chỉ inner BashTool nếu dùng nó trực tiếp.

Timeout phải tạo cùng observation và restart outer session như nguồn; không âm thầm kill container rồi khởi tạo workspace sạch. Các khác biệt container flags/paths/Python bootstrap có thể thuộc harness/input, nhưng nếu read-only filesystem, UID, mạng tắt hoặc thiếu package làm lệnh gốc không chạy được thì cần xử lý môi trường; không dùng prompt mới để giảm quyền self-verification.

### D06 — Khớp turns, reminders và completion gate

Một turn = **một repair LLM response + tất cả follow-up/tool observations**. Nhiều tool calls trong một response vẫn là một turn; compression LLM calls không làm tăng repair turn count. Tools trong batch thực thi theo thứ tự Trae, không tự chạy song song.

Thay default 500 iterations bằng counter/limit tương ứng profile 50 hoặc 100. Nếu SDK iteration không bằng logical turn thì cần counter/bridge riêng; setting `max_iteration_per_run=50` chưa tự chứng minh đúng 50 Trae turns.

Khôi phục `format_messages()`:

```text
ENVIRONMENT REMINDER: You have {turn_left} turns left to complete the task.
```

Khi message cuối là assistant:

```text
Continue in standard tool call format. ENVIRONMENT REMINDER: You have {turn_left} turns left to complete the task.
```

Profile không bật turn reminders vẫn thêm `Continue in standard tool call format.` khi message cuối là assistant. `turn_left = max_turn - count_turn`; giữ đúng timing và vị trí user message, không chèn reminder vào serializer như một step mới.

Completion cần match:

- Chỉ response có **đúng một** tool observation là `task_done` mới đi vào completion check như nguồn; batch có task_done cùng tools khác không tự kết thúc ngay.
- `task_done` phải capture rồi filter patch. Filtered patch rỗng → tiếp tục bằng exact user feedback: `ERROR! Your Patch is empty. Please provide a patch that fixes the problem.`.
- Nonempty filtered patch → gen `task_done`, push final response theo nguồn rồi break **trước compression cuối**. Không thay bằng SDK FinishTool luôn kết thúc.
- Tới cap → gen `turn_capped`, lấy filtered WIP patch; nonempty WIP vẫn chuyển external validation. Không yêu cầu model đã tuyên bố xong mới chấm.
- Không có external-evaluator feedback repair retry; một generation attempt/case như `p2p_retry=1` gốc.

SDK stuck detector hoặc auto finish có thể dừng trajectory khác Trae: tắt trong profile tương thích. Giới hạn worker 1800 giây là budget generation bổ sung, cần hỗ trợ `None`/disabled; một infrastructure watchdog nếu bắt buộc phải được ghi là abort của harness, không giả làm Trae stop condition.

## 6. LLM và compressor protocol

### D07 — Pin request parameters, không dựa SDK defaults

Nguồn chuẩn: [llm_polytool.py](../artifact/artifact/code/trae_agent/utils/llm_polytool.py).

| Setting | Repair caller | Compressor caller | Sau wrapper Trae |
|---|---|---|---|
| Max output | 8192 | 8192 | Explicit `max_tokens=8192`, không dùng model maximum tự suy ra |
| `n` | 1 | 1 | Giữ một choice |
| `temperature` | 0.0 | 0.0 | Bỏ khi rule GPT-5 của reference áp dụng |
| `stop` | Không caller stop | `</step>` | Bỏ khi rule GPT-5 của reference áp dụng |
| `reasoning_effort` | Không caller setting riêng | Không caller setting riêng | GPT-5 branch đặt `low`; nhánh khác không tự thêm low |
| Tools | Exact TOOLS gốc | Không tools | Bỏ field `tools` rỗng ở compressor |
| `top_p`, API seed, explicit `tool_choice` | Không thêm | Không thêm | SDK không tự inject settings mới |

Cần kiểm tra **wire payload sau wrapper**, không chỉ constructor kwargs. API khác có thể cần field name tương ứng, nhưng phải giữ giới hạn/semantics tương đương; không được mất 8192/effort/prefill rồi coi đó là model exception.

Retry wrapper gốc `send_request_openai`: tối đa 12 attempts, bắt Exception, sleep `2**retries` cả sau lỗi cuối. Cần port policy này hoặc làm SDK retry path tương đương, không cộng thêm một lớp retry khiến số requests/backoff khác. Retries mặc định của thư viện OpenAI gốc chưa pin version, nên không tuyên bố phục hồi chính xác phần lịch sử chưa có nguồn; pin/ghi rõ assumptions.

Không thêm local completed-response cache: gốc dùng `NullCache`. Provider prompt caching có rules riêng ở D08, không đồng nghĩa bật một SDK response cache.

### D08 — Khôi phục exact compression messages và nhánh model

System constant hiện đã trùng base `traj_analyzer.SYS_PROMPT`, nhưng **effective prompt chưa trùng**.

Cần port nhánh `BYPASS_FILTER`: gốc dùng `'gpt-5-' in MODEL` với **model compressor**, thay toàn chuỗi `think → talk`, `agent → engineer` trong system prompt, và `<think> → <talk>` trong compressor serialization. Length/LZ4 gate vẫn dùng bản không bypass.

User suffix phải chỉ là:

```text
{serialized_context}

Now, compress the step {idx}.
```

Bỏ câu adapter thêm `Return <step id="..."> followed by the compressed content and </step>.`. Khôi phục **assistant prefill mặc định**:

```text
Sure. Here is the compressed content of step {idx}: <step id="{idx}">
```

Caller stop `</step>` được giữ/bỏ theo provider override gốc, không dùng SDK capability detection thay cho rule reference. Không thêm tools vào compression request.

Caching placement cần khớp `MessageManager.USE_CACHING=True`:

- Compressor system text là content block `cache_control={'type':'ephemeral'}`.
- Repair cache marker ở message trajectory cuối khi message cuối không phải assistant và trajectory không rỗng, trước user budget reminder.
- Không tự thêm cache marker vào initial repair system prompt hoặc chuyển marker sang mọi message.

**Điểm cần xử lý khi đổi model:** string `'gpt-5-'` không match `gpt-5.6-sol`/`gpt-5.6-luna`. Chỉ kiểm tra tên model mới có thể làm mất nhánh talk/engineer và làm khác default prompt Trae. Để yêu cầu “giữ cấu hình, chỉ đổi model” có nghĩa rõ ràng, cần tách **reference protocol model/branch** khỏi **actual model ID gửi endpoint**. Đây là field/profile mới cần triển khai, chưa có trong adapter.

Ví dụ reference defaults dùng repair `claude4-sonnet` branch và compressor `gpt-5-mini-2025-08-07` branch; actual repair/compressor IDs có thể thay. Reference branches giữ prompts/request rules của cấu hình được đối chiếu. Nếu muốn chạy đúng literal predicate trên tên model mới thay vì giữ reference branch, phải báo rằng effective model-derived prompt/settings đã đổi; không gọi đó là cùng exact default payload.

Subscription hiện chuyển system thành user prefix, không dùng prefill trên Responses và không gửi reasoning effort qua selector subscription. Vì vậy **profile exact Trae chưa thể bảo đảm với đường subscription hiện tại**. Cần chọn hoặc triển khai transport tương thích, kiểm tra endpoint chấp nhận messages/prefill/parameters gốc. Không khắc phục bằng cách tắt prefill hoặc thêm câu wrapper vào prompt. Đổi model là được phép; mất protocol không tự được miễn.

## 7. Serialization và thuật toán nén

### D09 — Khớp context từng ký tự, không chỉ cùng ý nghĩa

Nguồn chuẩn: `MessageManager.extract_step_into_traj()` và `perform_erase_step()` trong `expert.py`.

Cần giữ step IDs từ 0, initial user ở -1; order assistant text → tool calls → follow-ups theo source. Giữ original JSON argument string, spacing/newlines, tags và không tự XML-escape. Visible assistant content/ThinkTool thought được serialize; provider reasoning metadata không đưa vào compressor.

Với `show_ctx=False`, Trae blank các neighbor strings rồi vẫn `join('\n')`: input còn newline trước/sau target. Adapter hiện chỉ đưa `target.serialize()` nên **khác whitespace**; cần giữ đúng window shape nếu chọn setting này. Tắt show_ctx vẫn không đổi delay hoặc gates.

Giữ original text khi một neighbor đã nén. Gốc lưu `agent_erased` chứa serialized original, rồi serializer có thể thêm outer `<step>` nữa khi phục hồi, tạo nested wrappers. Nếu yêu cầu literal parity thì phải giữ representation này; không “sửa đẹp” bằng cách unwrap/normalize. Default cũng có thể gặp trường hợp neighbor đã nén này, không chỉ nondefault edge case.

Repair replacement giữ exact assistant reminder:

```text
(System reminder: compressed for better efficiency) {content.strip()}
(System reminder: long content deleted for better efficiency)
```

Toàn bộ action/observation batch của target được thay bằng một assistant message tại đúng vị trí. Internal originals không được lọt vào repair payload. Không bổ sung `Continue.`/ThinkTool text SDK khác vào serialization.

### D10 — Khớp timing, token counting và acceptance

Sequence cần giữ:

```text
repair response → execute all tools → push_step → maybe_perform_analysis_step
    → build next repair messages
```

Không chạy compression trước response đầu tiên, giữa tool observations chưa đủ, hoặc sau successful terminal task_done. Mỗi completed turn chỉ xét một target `turn - 1 - ctx_after`; không retry cùng target chỉ vì SDK gọi condenser lại/context-window recovery. Chưa đạt threshold/reduction thì giữ step và sang turn mới như gốc.

Length/LZ4 gate đếm serialized target **không bypass**. `ours` acceptance đếm serialized target **trong bypass window của compressor** và bare parsed output; hiện adapter dùng cùng candidate input count cho acceptance, nên cần tách hai operands nếu representation khác.

Acceptance phải cố định `old-new >= 400 OR new < 0.8*old` trong profile exact. `minimum_reduction_tokens/ratio` hiện có thể giữ ở generic mode, nhưng override khác 400/0.20 phải reject hoặc đánh dấu ngoài profile tương thích. Không áp reduction gate cho delete/random/lingua.

Token counting gốc dùng `encoding_for_model('gpt-4o').encode(text)` theo default special-token handling; adapter dùng `disallowed_special=()`. Nếu literal parity bao gồm strings trùng special-token spellings, cần khớp cả encode/error policy, không chỉ tên tokenizer. Gốc chưa pin tiktoken/LZ4 versions; chọn và ghi version chung cho conformance runs thay vì tuyên bố biết version lịch sử.

`use_lz4=False` mặc định đã trùng. Nếu cần hỗ trợ toàn design space kể cả `ctx_after=0/use_lz4=True`, source gốc có `[-0:]` lấy toàn window: profile literal phải match bug đó hoặc từ chối cấu hình này; không âm thầm dùng thuật toán đã sửa rồi tuyên bố identical. Giữ default LZ4 tắt không cần kích hoạt edge case này.

Baselines nếu đưa vào thí nghiệm cũng phải giữ random token-drop/LLMLingua model, ratio, force_tokens và exception policy. Random gốc báo erase_out theo số remaining token IDs; re-encode text rồi đếm lại không luôn cho cùng metric. Không tự thêm seed LLM/random baseline nếu cấu hình gốc không có.

### D11 — Khôi phục parser và compressor-error behavior

Parser adapter hiện chặt hơn Trae; nó reject sai step ID/nested wrappers, missing opening tag, whitespace-only text và nhiều finish reasons. Những reject rules này làm thay đổi context dù prompt/scalars trùng.

Cần port đúng `perform_analysis_step_ours()`:

1. `completion_tokens is None` → skip analysis result của lần đó như source.
2. Cộng usage theo nguồn trước parser/reduction decision.
3. Lấy choice đầu, `partition('</step>')`, bỏ trailing text.
4. Không có closing tag: tiếp tục nếu finish reasons chứa `'stop'`; không yêu cầu chính adapter đã gửi stop.
5. `<step` trong 200 ký tự đầu: bóc heuristic như nguồn, `>` trong 20 ký tự đầu còn lại thì bóc opening. Không verify ID hoặc áp full XML validator.
6. Dùng acceptance gốc; không tự reject empty text bằng một rule mới trước acceptance/reminder behavior.

Không giữ blanket try/except “compressor lỗi thì bảo toàn step và repair tiếp tục” trong exact profile. Exception sau provider retry phải thoát generation theo source và được outer harness ghi generation error; response invalid mà source skip vẫn skip. Hai tình huống này phải được phân biệt.

Ngoại lệ external validation cho phép evaluator riêng chấm patch theo policy ngoài, nhưng không cho generation âm thầm tiếp tục sau lỗi mà Trae đã dừng.

## 8. Patch, SDK controls và bằng chứng đối chiếu

### D12 — Khớp submitted patch, không dùng policy cấm test edits thay filter

Nguồn chuẩn: [get_diff.py](../artifact/artifact/code/trae_agent/tools/get_diff.py), [agent_util.py](../artifact/artifact/code/trae_agent/utils/agent_util.py).

Cần lấy diff theo đường generation Trae: `git --no-pager diff --ignore-submodules=all`, rồi exact `remove_patches_to_tests()`. Không dùng `git diff HEAD --binary` cộng `git add -N` untracked trong profile này. Untracked reproducer/test/source files không tự được đưa vào submitted patch nếu source gốc không lấy chúng.

Filter bỏ cả diff blocks theo substring list gốc:

```text
/test/ /tests/ /testing/ /test_
.tests. .test. _test_ _tests_ _test. _tests. .spec.ts
/tox.ini /Cargo.lock /package.json /package-lock.json /pom.xml
```

Không thay bằng parser extension/path heuristic của PHP/fmtlib, không mở rộng/sửa bugs trong filter khi đang yêu cầu literal parity. Ví dụ `.phpt` không nằm riêng trong danh sách gốc: đừng tự thêm nó chỉ vì input PHP. Nếu evaluator ngoài cần policy khác, lưu raw filtered patch như artifact chuẩn rồi phân biệt adaptation của external evaluation; không gọi patch đã đổi policy là patch Trae-identical.

Baseline Git của input mới phải sạch và tracked trạng thái source tương ứng trước generation để diff có nghĩa. Nếu commands agent chạy `git add/commit`, unstaged-diff semantics cũng phải được giữ như nguồn, không tự đổi sang HEAD-diff để “cứu” patch.

External validator vẫn có thể apply filtered patch vào validation copy, chạy configured PHP/fmtlib target/regression và classify như hiện tại. Cần lưu generation status tách external verdict: model/task_done/turn cap không phải bằng chứng `resolved`.

### D13 — Khóa SDK behavior ngoài contract và model-role config

Cần giữ hoặc cấu hình explicit:

- Không default tools, ambient plugins/skills/vision tools, memory/SOUL/datetime prompt injection trong profile exact.
- Tool batch concurrency = 1 và output order như source. SDK hiện default sequential; pin explicit và test để không trôi khi đổi SDK.
- Không stuck detection, automatic final-message stop, recovery prompt, extra condensation loop hoặc global summarizer làm khác Trae.
- Không SDK auto model fallback/routing rewrite, extra `tool_choice`/sampling/cache fields nếu không thuộc source contract.
- Không monetary/token budget generation mới; worker timeout default 1800 cần được vô hiệu hóa cho profile exact như D06.
- Khai báo repair model và compressor model rõ ràng. `inherit` chỉ dùng khi chủ đích chọn **cùng actual model cho hai role**, không giữ như một hidden default khi reference experiment dùng hai models.

Đây không phải yêu cầu giữ tên models cũ; người chạy được thay cả hai. Điều cần giữ là cấu hình hai roles/reference protocol minh bạch, không để SDK/model alias quyết định settings ngoài ý muốn.

### D14 — Lưu payload/config và test theo source gốc

Đây là công việc phục vụ **chứng minh parity**, không phải thêm một thuật toán repair. Telemetry/audit có thể khác định dạng Trae vì harness/model đã thay, nhưng không được tác động payload hoặc stop decisions.

Cần lưu effective reference profile, actual model IDs, input mapping, repair/compressor message snapshots hoặc hashes, advertised tool schemas, normalized wire parameters, logical step/turn mapping, submitted patch hash và các exceptions chỉ nằm trong bốn nhóm được phép. Không lưu credential values.

`keep_raw_events` hiện không điều khiển persistence, callback có truncation và LocalConversation không nhận persistence_dir: sửa để giữ đủ event/messages cần audit ngoài agent workspace. Không nhầm log preview với payload đầy đủ. Native event IDs/JSON format khác là khác biệt harness được phép.

Token usage của models mới và cách báo cost tiền có thể khác; không cần đổi telemetry đúng hiện tại thành phép cộng cache cũ để làm agent giống Trae. Nếu cần so sánh legacy metrics thì lưu thêm trường compatibility riêng và định nghĩa rõ. Các counters ảnh hưởng gate/acceptance/step reductions phải dùng cùng operands ở D10; provider billing usage giữ riêng.

## 9. Những phần không cần sửa về giống evaluator Trae

Theo phạm vi người dùng cho phép, **không cần**:

- Chuyển input PHP/fmtlib thành SWE-bench Verified/Multi-SWE records hoặc dùng dataset IDs cũ.
- Thay Docker images/build dependencies PHP/fmtlib bằng images benchmark Trae.
- Thay external validation target/regression commands bằng official SWE/Multi harness.
- Khôi phục external F2P/P2P/test_patch/`resolved_ids` oracle của benchmark cũ.
- Đổi validation timeout 600 giây mỗi command thành timeout official SWE 900 giây; đây là setting evaluator ngoài được phép khác.
- Đổi APR taxonomy `plausible/cleanfix/noisefix/nonefix/negfix/invalid` nếu đó là verdict của evaluator PHP/fmtlib được chọn.
- Chạy full baseline test suite trước generation chỉ để “khớp Trae”; preflight/input provisioning có thể thuộc harness. Đừng chèn baseline feedback mới vào repair prompt ngoài input mapping đã khai báo.
- Giữ nguyên host paths, Python bootstrap, IDs/events format hoặc số process chạy các cases nếu chúng chỉ thuộc harness và không đổi policy mỗi repair conversation. Nếu so sánh random baseline, vẫn phải ghi scheduling/RNG assumptions.

Có thể giữ isolated repair/validation copies, image availability preflight, external config bất biến, token telemetry và output root `Agent-Diet/output`. Điều kiện: các phần này không chèn thêm hướng dẫn, giới hạn generation hoặc thay contract agent ở các mục D01–D13.

## 10. Thứ tự triển khai và điều kiện hoàn thành

Ưu tiên thực hiện:

1. **D01–D06:** exact prompt/input template/tools, quyền reproduction/test, tool observations và turn/completion behavior.
2. **D07–D08:** transport tương thích, request params, compressor prefill/cache/model branch. Đây là điểm cần giải quyết trước khi chọn subscription làm đường chạy exact.
3. **D09–D11:** serializer, compression trigger/gates/acceptance/parser/error policy.
4. **D12–D13:** patch diff/filter, khóa SDK overrides/fallback/limits.
5. **D14:** lưu manifest và kiểm tra toàn chuỗi; chạy PHP/fmtlib external evaluation sau khi generation contract pass.

Các fixtures/tests conformance tối thiểu, dùng source Trae làm oracle thay vì lấy adapter hiện tại làm expected behavior:

| Kiểm tra | Kết quả cần có |
|---|---|
| First repair request | Exact SYS_PROMPT/TOOLS; user template chỉ khác project path và input body |
| Multi-tool response | Một turn; tools/results đúng thứ tự; think trả `Continue.` |
| Task completion | Empty filtered patch gây exact feedback rồi tiếp tục; nonempty dừng trước compression |
| Turn cap/reminders | 50+bật hoặc 100+tắt; remaining turns text và Continue branch khớp source |
| Step 0,1,2 | Target 0; user context -1; không đếm tool calls/compressor calls thành repair turns |
| Compressor payload | Exact system variant, neighbor originals, suffix/prefill/cache blocks; không có wrapper sentence thêm |
| show_ctx=False | Giữ blank-neighbor newline structure và delay/gates |
| Neighbor đã nén | Recovered serialization đúng nguồn, kể cả nested wrapper behavior |
| Threshold/acceptance | Gate không bypass; acceptance theo bypass input; strict <80% hoặc >=400 |
| Parser fixtures | Wrong ID/nested/empty/missing-close/stop cases ra cùng quyết định source |
| API/tool errors | Retry/timeout/error output/stop decisions tương ứng; không silently continue compressor exception |
| Long outputs | Bash không tail-40k; editor prefix/truncated marker/numbering đúng nguồn |
| Patch fixtures | Test/manifest blocks, unstaged/staged/untracked cases cho đúng filtered diff gốc |
| External evaluation boundary | Không expose evaluator-private artifacts/config, không feedback/retry repair từ evaluator |

**Hoàn thành khi mọi bất biến trên pass và phần diff còn lại chỉ là harness plumbing, actual model IDs, input PHP/fmtlib và external evaluator.** Chưa thể đạt mục tiêu chỉ bằng các flags `--ctx-before 1 --ctx-after 2 --diet-threshold 500`; cần sửa cả prompt/tool/message/stop/patch contracts.

## 11. Trạng thái tài liệu

Đã đối chiếu source và viết yêu cầu sửa; **chưa sửa implementation, chưa chạy parity tests hoặc benchmark PHP/fmtlib mới** trong công việc này. Reference profile 50/100 và actual model/transport cho lần triển khai phải được khai báo rõ. Version/package/provider behavior lịch sử không được pin trong artifact không thể suy ra chắc chắn; mục tiêu kiểm chứng là contract của source đã công bố, không phải cam kết mọi request lịch sử bit-for-bit.
