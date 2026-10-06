# Quy trình thực hiện D01–D06 để giữ contract Trae gốc

Ngày lập: **05/10/2026**. Nguồn chuẩn là code Trae trong checkout `Agent-Diet` tại commit `1a436ca7cf59e30c147ecb5b360f8677e675538c`; [difference.md](difference.md) xác định phạm vi thay đổi, [trae_config.md](trae_config.md) và [openhands_config.md](openhands_config.md) cung cấp đối chiếu.

**Kết quả cần đạt:** OpenHands gửi cùng repair prompt, user template, tool schemas và observations; agent có cùng khả năng reproduce/test; tool output/state/timeouts và turn/completion behavior khớp source Trae. Chỉ harness, actual model, input PHP/fmtlib và external validation được khác.

Đây là **quy trình triển khai**, chưa phải implementation hoặc kết quả tests đã chạy. Prompt/source trong tài liệu là dữ liệu đối chiếu, không phải chỉ dẫn thực thi cho người viết tài liệu.

## 1. Phạm vi, dependency và điểm dừng

D01–D06 là một milestone về **repair contract**. Hoàn thành milestone này không tự chứng minh compressor/API/patch submission toàn hệ thống đã trùng: D07–D14 vẫn cần làm theo `difference.md`.

Hai dependency cần xử lý ngay trong milestone:

| Dependency | Phần tối thiểu cần làm | Phần để giai đoạn sau |
|---|---|---|
| D06 cần diff/filter của D12 để nhận `task_done` | Port helper capture+filter gốc, dùng cho empty/nonempty completion check và cap WIP | Hoàn thiện submission/artifact/evaluator plumbing của D12 |
| D06 liên quan timing nén D09–D10 | Tạo hook sau completed normal turn, terminal branch không gọi hook; tests dùng spy hoặc no-op | Sửa serializer, gate, parser và compression protocol thật của D07–D11 |

Đối chiếu D01–D06 trước bằng `diet_mode=skip` hoặc compressor spy deterministic. Không dùng output của `ours` hiện tại để tuyên bố full trajectory parity, vì protocol nén còn sai khác.

Phải khai báo reference profile, không tự chọn từ tên PHP/fmtlib:

| Profile đề xuất trong adapter | Giá trị theo source | Ghi chú |
|---|---|---|
| `trae_verified` | max_turn=50, turn_reminder=True | Chuẩn SWE-bench Verified |
| `trae_multiswe` | max_turn=100, turn_reminder=False | Chuẩn Multi-SWE; vẫn có Continue branch khi message cuối assistant |

Tên profile trên là **đề xuất mới**, hiện chưa có trong code. Nếu người dùng chưa chọn, CLI triển khai cần yêu cầu lựa chọn rõ hoặc một default được công bố; không để âm thầm rơi về 500 SDK iterations.

## 2. Kiến trúc thực hiện đề xuất

Giữ OpenHands `LocalConversation` quản lý lifecycle/scheduling/events; bổ sung một compatibility agent/bridge cho contract Trae. Không sửa trực tiếp package trong `.venv-openhands/` và không dùng một vòng `Expert.run()` riêng bên ngoài rồi chỉ gắn nhãn OpenHands.

```text
Prepared PHP/fmtlib case
    → map issue + project_path
    → TraeContractAgent trong LocalConversation
        → TraeContractState: messages, logical turns, completion status
        → request builder: exact system/user/tools/reminders
        → transport interface: raw response, raw arguments, usage
        → TraeToolDispatcher: tools gốc + sandbox session
        → completion gate: source diff/filter
        → after-normal-turn hook: skip/spy trước, AgentDiet sau
    → filtered patch
    → external validation PHP/fmtlib trên workspace riêng
```

Các module mới **đề xuất**, chưa tồn tại:

| Module dưới `src/openhands_adapter/` | Trách nhiệm |
|---|---|
| `compat/trae_contract.py` | Exact constants/schema, reference profile, messages state và format_messages |
| `openhands/trae_agent.py` | SDK agent extension: một response → batch tools → transition; SDK lifecycle vẫn chạy bên ngoài |
| `openhands/trae_tools.py` | Raw tool-call dispatch, exact observations; không dùng WorkspaceTools policy hiện tại |
| `openhands/trae_session.py` | Container shell/tool wrappers, timeouts, session restart và editor history |
| `workflow/trae_patch.py` | Diff/filter helpers gốc cho D06 và sau đó D12 |
| `tests/trae_reference.py` | Oracle loader chỉ dùng trong tests, đọc AST source gốc |

Có thể gộp modules nếu implementation nhỏ, nhưng phải giữ các boundary trên. Production có thể dùng constants/logic đã port; oracle tests phải đọc source gốc độc lập, không import chính constants của adapter làm expected result.

Điểm extension SDK cần kiểm tra tại version đã pin `1.49.5`:

- [agent/base.py](../.venv-openhands/lib/python3.14/site-packages/openhands/sdk/agent/base.py): inline prompt/default tools và initialization.
- [agent/agent.py](../.venv-openhands/lib/python3.14/site-packages/openhands/sdk/agent/agent.py): `get_dynamic_context()`, **cả `step()` và `astep()`**, raw call dispatch, finish handling.
- [local_conversation.py](../.venv-openhands/lib/python3.14/site-packages/openhands/sdk/conversation/impl/local_conversation.py): hai đường `run()`/`arun()`, SDK iteration, stuck detection và lifecycle.
- [tool/tool.py](../.venv-openhands/lib/python3.14/site-packages/openhands/sdk/tool/tool.py) và [llm/llm.py](../.venv-openhands/lib/python3.14/site-packages/openhands/sdk/llm/llm.py): schema augmentation, `ToolDefinition` conversion và response parsing trước khi trả `raw_response`.

Không chỉ truyền kwargs vào `Agent(...)` rồi giả định contract đã đúng: source SDK hiện gọi LLM với `add_security_risk_prediction=True`, normalize/fix tool arguments và có FinishTool behavior riêng. Compatibility layer phải kiểm soát những đường này. Ưu tiên một implementation của `AgentBase` với contract transition riêng. Nếu subclass concrete `Agent`, phải xử lý cả sync/async paths: concrete `Agent.astep()` có implementation riêng, không tự gọi override `step()` của subclass.

### 2.1. Gate GSDK — chứng minh điểm tích hợp trước khi triển khai D01–D06

Đây là gate bắt buộc về khả năng thực hiện trên SDK đã cài, tách khỏi các gates so sánh Trae. Ghi cả SDK version và source hashes; version pin không thay thế việc kiểm tra code thực tế.

| Điểm chuyển đổi | Ràng buộc đã thấy trong source | Việc prototype phải chứng minh |
|---|---|---|
| SDK scheduling | Worker hiện gọi `conversation.run()` → `agent.step()`; `conversation.arun()` → `agent.astep()` | Custom agent được SDK chấp nhận, cả hai entry points dùng cùng contract transitions; không rơi vào stock Agent loop |
| State | `AgentBase` là frozen configuration model; `ConversationState.agent_state` dùng để giữ mutable state | Messages/turns/gen/patch ở contract state có persistence rõ; event archive không trở thành lịch sử thứ hai làm request lệch |
| Advertised schema | `LLM.generate/completion` nhận `Sequence[ToolDefinition]`, không trực tiếp nhận list dict Trae; `ToolDefinition._get_tool_schema()` luôn thêm `summary` | Override conversion hoặc chọn transport extension cụ thể; bắt payload sau serialization để chứng minh exact TOOLS, không chỉ tắt `security_risk` |
| Raw response | SDK tạo `Message` trước khi trả `LLMResponse.raw_response`; stock Agent còn normalize/fix/re-serialize arguments | Response có invalid JSON/unknown tool/raw whitespace đến dispatcher Trae nguyên vẹn; nếu conversion chặn trước đó phải đổi boundary nhận raw response |
| Terminal/cap | SDK chuyển sang ERROR khi chạm iteration limit mà agent chưa FINISHED | Terminal singleton không bị pending-action handling chạy lại; cap vẫn capture WIP và trả `turn_capped`, không thành `MaxIterationsReached` |

Prototype phải dùng **real LocalConversation**, fake model transport và scripted responses, không chỉ mock constructor. Chốt API/signature/schema conversion thực tế bằng code chạy được trước khi port toàn bộ tools. Không sửa trực tiếp installed SDK hay dùng global monkeypatch làm implementation production. Nếu đường `LLM.generate()` chuẩn không giữ exact schema/raw response, tài liệu implementation phải chỉ rõ extension/transport thay thế còn nằm dưới custom SDK agent, cùng dependency D07 của nó.

Một probe scheduling tối thiểu chỉ chứng minh lifecycle/cap; nó chưa chứng minh prompt, tool execution, raw arguments, terminal transcript hoặc sandbox. Các phần đó vẫn phải qua G0–G6 và scripted integration ở mục 10.

### 2.2. Bằng chứng rà lại khả năng chuyển đổi ngày 05/10/2026

Đã chạy **4 probes offline** bằng Python của `.venv-openhands`, real `LocalConversation.run()` và custom `AgentBase` scheduling stub, không gọi provider, không chạy repair tools. Stub tăng logical count/hook count cho mỗi normal step, rồi finalize `turn_capped` ở step kế tiếp.

| Logical turn cap | SDK iteration limit | Kết quả quan sát |
|---|---|---|
| 50 | 50 | SDK ERROR sau 50 normal steps; chưa chạy cap finalization |
| 50 | 51 | SDK FINISHED; contract stub turn_capped, 50 turns/hooks, 51 SDK steps |
| 100 | 100 | SDK ERROR sau 100 normal steps; chưa chạy cap finalization |
| 100 | 101 | SDK FINISHED; contract stub turn_capped, 100 turns/hooks, 101 SDK steps |

Cả 4 assertions khớp expected scheduling behavior. Đây là bằng chứng cho allowance ở mục 9.1, **không phải 4 conformance tests của Trae đã pass**: stub không capture patch, build prompts, parse responses hoặc chạy tools.

Một smoke thử `arun()` với stub nhỏ đã đạt bước đặt FINISHED, nhưng wrapper `asyncio.run()` chưa kết thúc cleanup trong timeout 15 giây; traceback dừng ở `asyncio.Runner.close()`. Chưa xác định nguyên nhân thuộc fixture/event-loop ownership hay SDK/dependency. Vì vậy **chưa đánh dấu async lifecycle/cleanup pass**. GSDK phải kiểm tra shutdown/close trên event-loop integration thực tế; không dùng timeout kill làm tiêu chuẩn completion. Worker hiện dùng sync `run()`.

**GSDK hiện chưa pass đầy đủ:** exact schema serialization/raw-response boundary, terminal event handling, persistence và sandbox integration vẫn chưa có compatibility implementation để kiểm chứng.

## 3. Bước 0 — Đóng băng reference và tạo oracle trước khi sửa

### Thực hiện

1. Ghi commit/source hashes, SDK version và effective profile. Không thay source trong `artifact/` để làm tests pass.
2. Tạo oracle loader AST cho [expert.py](../artifact/artifact/code/trae_agent/agents/expert.py): lấy `SYS_PROMPT`, `INIT_USER_PROMPT`, `TOOLS`, `MessageManager`, `parse_tool_response` và logic transition cần kiểm tra.
3. Lấy `remove_patches_to_tests()` từ [agent_util.py](../artifact/artifact/code/trae_agent/utils/agent_util.py), tool constants từ [claude_tools/](../artifact/artifact/code/trae_agent/tools/claude_tools/) và shell behavior từ [sandbox.py](../artifact/artifact/code/trae_agent/utils/sandbox.py).
4. Không import toàn `expert.py`/`traj_analyzer.py` trong tests: import có provider/config side effects và yêu cầu `TRAJ_ANALYSIS`. Compile các AST nodes cần thiết trong namespace tối thiểu; inject fake session/LLM/metrics cho oracle.
5. Dùng fixed project path `/testbed` và issue text cố định cho unit fixtures; dùng cùng path/input cho oracle và adapter. So sánh text nguyên vẹn, không `.strip()` cả hai phía để che lỗi whitespace.

Fixtures ban đầu cần có:

- Initial repair request ở cả hai profiles.
- Response chỉ text, một think, nhiều tool calls, malformed JSON, unknown tool.
- task_done với filtered patch rỗng/nonempty/test-only, task_done trong batch.
- Last turn/cap WIP; timeout rồi restart session.
- File >40k, editor >16k, Unicode/tabs, undo và shell state qua calls.

Có thể tham khảo cách AST-load `MessageManager` trong [test_turn_trajectory.py](../tests/test_turn_trajectory.py), nhưng bộ fixtures mới phải kiểm tra cả prompts/tools/stop decisions, không chỉ serializer.

### Gate G0

Oracle chạy offline, không login/call provider, không chạy benchmark mới; expected prompts/schema/transition được sinh từ source Trae. Reference hashes được kiểm tra trước mọi conformance test.

## 4. Bước 1 — D01: exact repair system prompt

### File sửa

[openhands/prompts.py](../src/openhands_adapter/openhands/prompts.py), [openhands/agent.py](../src/openhands_adapter/openhands/agent.py), [openhands/runtime.py](../src/openhands_adapter/openhands/runtime.py), compatibility agent/contract modules mới.

### Thực hiện

1. Port `expert.SYS_PROMPT` đúng giá trị runtime sau `.strip()`; giữ các khoảng trắng bên trong và câu chữ gốc.
2. Thay suffix hiện tại bằng inline system prompt. Không còn `AgentContext(system_message_suffix=...)` để ghép workflow mới vào default SDK prompt.
3. Bảo đảm dynamic system context trả rỗng trong compatibility agent, kể cả conversation có secret descriptions. Chỉ `agent_context=None` chưa đủ: SDK có đường tạo dynamic context từ secret registry.
4. Không resolve SOUL/memory/skills/datetime/model-specific sections cho profile này. Giữ discovery controls của runtime và chặn các đường SDK khác có thể gắn tools/prompts.
5. Request builder dùng exact system message từ contract state; không thêm `<SOUL>`, `<ROLE>`, `[Repair constraints]`, SDK recovery instructions hoặc cache marker mới vào initial system message.
6. Gắn capture stub tại LLM boundary để lấy messages thực sau SDK assembly; tạo thêm test transport serialization để phát hiện system text bị chuyển thành user prefix.

### Kiểm tra

| Test đề xuất | Assertion bắt buộc |
|---|---|
| `system_prompt_matches_source` | Text bằng oracle SYS_PROMPT từng ký tự |
| `no_dynamic_context_injection` | Không có datetime/SOUL/memory/skills/secret descriptions trong repair messages |
| `first_request_has_exact_system_message` | Một system message đúng source, không có system suffix phụ |
| `system_role_survives_transport` | Transport tương thích giữ role/content contract; không prefix `Context (system prompt):` |

### Gate G1

Payload messages đúng sau SDK assembly, không chỉ đúng constructor kwargs. Nếu transport subscription hiện chuyển system thành user prefix, G1 wire check chưa pass: dùng fake compatible transport để phát triển, ghi dependency D07, và chưa chạy live với nhãn exact Trae.

## 5. Bước 2 — D02: exact user template với input PHP/fmtlib

### File sửa

[input_loader.py](../src/openhands_adapter/input_loader.py), [openhands/prompts.py](../src/openhands_adapter/openhands/prompts.py), [cli.py](../src/openhands_adapter/cli.py).

### Thực hiện

1. Thêm input-mapping policy rõ ràng: `issue = problem_statement` nếu prepared case có mô tả lỗi hợp lệ; nếu không có, lấy text failure log đã chuẩn bị. Không tự lấy evaluator output sau generation.
2. Thay `_load_case_paths()` đang cố định `problem_statement=None`; ghi nguồn issue (`problem_statement` hoặc `failure_log`), encoding và hash vào case/run manifest.
3. Với input whitespace-only/missing text, reject hoặc báo lỗi input rõ ràng; không thay bằng `Diagnose and fix the reported failure.` để che thiếu input. Chỉ áp `.strip()` theo policy input đã khai báo; template formatting phải khớp source.
4. `build_user_prompt()` dùng `INIT_USER_PROMPT.format(project_path=..., issue=...)` như nguồn. Project path là path agent truy cập trong container; nếu container mount khác host path phải map đúng.
5. Bỏ builder các blocks `[Buggy source code]`, `[Task instructions]`, `[Repair constraints]` và câu yêu cầu đọc `.agent-diet.failure.log`.
6. Trong exact profile, reject `--prompt`/`--prompt-file` overrides tự do hoặc phân biệt chúng thành chế độ ngoài contract. Không silently ignore JSON/env prompt fields đã parse.
7. Source/build environment vẫn chuẩn bị từ input PHP/fmtlib; không expose evaluator-private config như tool mới, không thêm configured-command-only guidance vào prompt.

Template cần giữ:

```text
[Project root path]:
{project_path}

[Problem statement]: We're currently solving the following issue within our repository. Here's the issue text:
{issue}
```

### Kiểm tra

- PHP case chỉ có failure log: issue body đúng log theo mapping policy, phần bọc đúng oracle template.
- fmtlib case có problem statement: ưu tiên đúng nguồn được khai báo; không chèn cả log thành một section mới ngoài template.
- Input nhiều dòng/Unicode: không mất nội dung hay đổi indent/newlines ngoài policy đã ghi.
- Host/container paths khác nhau: agent prompt trỏ tới source thực truy cập được.
- Conflict prompt overrides: CLI/config báo rõ, không gửi request lệch trong khi manifest ghi exact profile.

### Gate G2

Initial user message bằng oracle khi truyền cùng `{project_path, issue}`. Khác biệt text chỉ nằm trong hai substitutions input đã cho phép.

## 6. Bước 3 — D03: exact tool schema và raw dispatch

### File sửa

[openhands/agent.py](../src/openhands_adapter/openhands/agent.py), [workspace_tool.py](../src/openhands_adapter/openhands/workspace_tool.py) tại assembly point; thêm contract tools/dispatcher bridge. Restricted WorkspaceTools có thể còn dùng cho generic mode, nhưng không được expose ở exact profile.

### Thực hiện

1. Port `expert.TOOLS` nguyên danh sách, descriptions, schemas và thứ tự: editor → bash → task_done → think.
2. Không dùng SDK FinishTool/ThinkTool thay tools gốc; đặt default tools rỗng và advertise đúng bốn schemas.
3. Loại schema augmentation `security_risk`, **`summary`** và các fields SDK thêm vào model-facing parameters. `add_security_risk_prediction=False` chỉ tắt risk, không tắt summary. Chọn cách conversion/transport đã được GSDK chứng minh; không truyền raw list dict vào `LLM.generate(tools=...)` rồi giả định SDK hỗ trợ. Nếu ToolDefinition tự serialize khác nguồn, bridge phải kiểm soát conversion để cung cấp exact schema, không chấp nhận schema “gần giống”.
4. Nhận raw tool calls trước SDK normalize/fix/JSON re-serialization. Giữ argument string gốc cho conversation/trajectory; parse một bản riêng để executor dùng.
5. Không sửa malformed JSON thành JSON hợp lệ trước dispatcher; không đổi tool alias sang tên đúng, không thêm strict validation khác source.
6. Tools được thực thi tuần tự đúng order trong response. Mỗi raw tool_call_id map một observation; IDs có thể khác giữa harness runs nhưng pairing phải đúng.
7. Port behavior `parse_tool_response()` cho invalid JSON, unknown tool, think, task_done và wrapper tool output. Lưu `agent_caller`/error metadata để contract logic dùng; format repair messages theo `_remove_internal_fields()` gốc.
8. Không expose `task_failed` thành tool thứ năm. Nếu response chứa tên này, branch xử lý bất thường phải khớp source, không bị SDK unknown-tool normalization thay behavior.

Observations đặc biệt phải đúng:

| Input/nhánh | Content theo nguồn |
|---|---|
| think | `Continue.` |
| task_done tại dispatcher | `Task done` |
| task_failed tại dispatcher | `Task failed` |
| Invalid JSON | `The argument given to {tool_name} is not a valid JSON. Please fix the problem and try again!` |
| Unknown tool | `The tool name you provided is not in the list!` |
| Tool output rỗng | `(no output)` |

Không thêm `exit_code=... timed_out=...` vào content. Có thể giữ code/timeout trong event metadata của harness, không đưa prefix mới vào repair observation.

`task_done` chỉ tạo kết quả dispatcher; **không tự finish SDK conversation tại tool executor**. Completion gate thuộc bước D06. Không dùng tên `finish` khiến SDK `_ActionBatch` cắt các tools phía sau một finish action.

### Kiểm tra

1. Deep equality advertised schemas với oracle TOOLS; object key order không đổi semantics, nhưng giữ list order và text descriptions nguyên vẹn. Kiểm tra cả wire schema, không chỉ schema constants.
2. `bash.command` schema không tự thêm required field; `task_done` không có message parameter; editor/think required lists khớp nguồn.
3. Raw arguments có spacing/JSON order khác nhau vẫn giữ original strings trong transcript.
4. Invalid JSON/unknown tool/extra argument cases trả cùng observations hoặc exception như source. Không viết test expected theo lỗi Pydantic của adapter hiện tại.
5. Một response có bash→editor→think: execution/result order như oracle, đúng tool_call_id pairing.
6. task_done trong batch không làm mất tools sau nó; không tạo SDK FinishTool message.

### Gate G3

Schemas/messages/dispatch decisions khớp oracle, SDK không âm thầm thêm fields hoặc sửa response. Action/event representation nội bộ có thể khác nhưng phải map được về contract transcript.

## 7. Bước 4 — D04: quyền reproduce/test và boundary evaluator

### File sửa

Tool/session bridge mới, [container.py](../src/openhands_adapter/openhands/container.py), input/workspace provisioning, assembly trong `agent.py`. Không sửa policy của external evaluator để bù cho repair tools khác.

### Thực hiện

1. Bash nhận command tự do trong repair container, không chỉ phase/index từ execution plan. Agent được chọn filters/tests và tạo reproducer/debug scripts.
2. Editor gốc được tạo/sửa source/tests/fixtures/manifests trong repair workspace; không áp test-name guard hoặc `protected_command_file` của WorkspaceTools hiện tại.
3. Bỏ expected_content overwrite contract hiện tại: giữ semantics của editor create/str_replace/insert/undo đúng source.
4. Chuẩn bị sandbox đủ writable paths/interpreter/build dependencies cho PHP/fmtlib. Container flags là harness implementation, nhưng không được làm tools trả `blocked=true` cho hành vi Trae cho phép.
5. Chỉ expose repair copy và tool runtime hỗ trợ cho agent. External config, validation copy và output-private files không mount vào repair container; credentials host không truyền vào shell tool.
6. Keep validation copy/config gốc bất biến trước apply submitted patch. Setup/test scripts repo có thể bị model sửa trong repair copy, nhưng điều đó không đổi evaluator commands/config ở host.
7. Khôi phục Git baseline tương ứng source input để diff/filter có nghĩa. Không bắt agent phải thực hiện một sequence mới như đọc command list trước khi được edit.

Chỉ dẫn “must reproduce trước sửa” trong prompt là **prompt requirement**, không phải runtime gate gốc đã validate. Không thêm một gate bắt buộc agent tạo reproducer mới vào runtime để làm prompt có vẻ được cưỡng chế hơn nguồn.

### Kiểm tra không dùng live model

| Fixture sandbox | Hành vi cần có |
|---|---|
| Bash chạy lệnh không nằm trong prepared config | Thực thi trong repair sandbox |
| Editor tạo `reproduce.py` | Thành công theo create semantics gốc |
| Editor sửa `tests/test_example.py`, fixture hoặc configured repo script | Không bị policy chặn theo tên như adapter cũ |
| Setup/build/test do agent chọn | Có thể chạy, không bắt phase/index |
| Sửa repair tests | Validation copy vẫn nguyên bytes và evaluator config không đổi |
| Liệt kê mounts/environment | Không có private validator workspace/config, host secrets hoặc Docker socket |

### Gate G4

Agent action space trong repair khớp tool gốc; external evaluator boundary vẫn độc lập. Không thay bằng “vẫn cấm repo tests nhưng cho script /tmp” vì đó không phải quyền gốc.

## 8. Bước 5 — D05: port tool wrappers, output và state

### Phương án triển khai ưu tiên

Vendor/copy nguyên các source tool wrappers gốc vào khu runtime riêng của container, giữ `artifact/` không sửa; bridge chỉ thích nghi container path/interpreter/process transport. Không viết lại editor từ WorkspaceTools rồi đoán error/output text tương đương.

Các source cần dùng:

- [execute_bash.py](../artifact/artifact/code/trae_agent/tools/claude_tools/execute_bash.py), [bash.py](../artifact/artifact/code/trae_agent/tools/claude_tools/bash.py).
- [execute_str_replace_editor.py](../artifact/artifact/code/trae_agent/tools/claude_tools/execute_str_replace_editor.py), [edit.py](../artifact/artifact/code/trae_agent/tools/claude_tools/edit.py), [run.py](../artifact/artifact/code/trae_agent/tools/claude_tools/run.py), [base.py](../artifact/artifact/code/trae_agent/tools/claude_tools/base.py).
- Outer session behavior trong `sandbox.py` và result parsing trong `expert.parse_tool_response()`.

### Thực hiện

1. Stage tool files/interpreter dưới path runtime cố định. Editor history/log.out dùng container-private storage, không đặt vào repository patch.
2. Port argument conversion/quoting của wrapper. User command chỉ đi vào sandbox tool; host runtime nhận structured argv để không thực thi command của model trên host. Nếu nguồn có hành vi conversion đặc biệt, test và giữ nó trong exact bridge.
3. Port outer Session execution: prompt/echo removal, `&& sleep 0.5` khi phù hợp nguồn, decode/escape handling, timeout output. Không thay bằng stdout+stderr cộng prefix mới nếu bytes khác oracle.
4. Port wrapper `Tool Call Status` handling: status line đầu phù hợp bị tách khỏi content, raw result còn lại như nguồn; lỗi/timeout đánh metadata tương ứng.
5. Loại hard truncation `output[-40000:]` khỏi đường repair request. Logger preview có thể truncate riêng, nhưng LLM observation và oracle comparison phải dùng full output.
6. Editor dùng `maybe_truncate`/raw content prefix 16.000 ký tự và exact `TRUNCATED_MESSAGE` trước expand tabs/line numbering. Không giới hạn full observation bằng 16k lần thứ hai sau formatting.
7. Editor `_file_history` sống qua calls như wrapper pickle gốc; undo phản ánh đúng lịch sử theo source.
8. Mỗi execute_bash wrapper tạo BashTool mới; không duy trì inner Bash process qua tool calls. Filesystem/container và outer session vẫn sống. `cd/export` trong inner tool call không tự sống sang call tiếp theo.
9. Timeout outer restart session theo `Expert.run()`; không recreate container/reset source hoặc mất editor history. Background jobs/processes giữ hay bị ảnh hưởng theo session lifecycle nguồn, không tự kill toàn bộ sandbox.

Timeout layers cần giữ:

| Layer | Giá trị gốc | Lưu ý triển khai |
|---|---:|---|
| Outer shell initial prompt | 10 giây | Cần nhận diện prompt khi start/restart |
| Outer `Session.execute()` | 180 giây | Có thể timeout trước inner Bash |
| Inner `_BashSession` | 210 giây | Poll delay 0.2 giây, sentinel gốc |
| Helper `run()` | 120 giây | Đường editor/helper tương ứng |

Outer prompt regex của source có host/user patterns cụ thể. Với PHP/fmtlib image, cấu hình prompt tương thích hoặc adaptation có kiểm tra output equality; không để regex không match rồi mọi command timeout. Không bật `privileged=True` chỉ theo tên “giống Trae”: flags thuộc harness, nhưng phải chứng minh tool capabilities/output không đổi bởi môi trường thay thế.

### Kiểm tra

| Test | Oracle và assertion |
|---|---|
| Bash 60k+ characters | Full observation bằng source, không mất đầu output hoặc giữ tail 40k |
| Editor long file | Exact clipped marker, prefix/raw-content order, numbered lines/tabs khớp |
| Unicode multiline | Không decode mất text, không đổi whitespace ngoài source behavior |
| No-output/error | Exact fallback/status stripping/error content |
| create/replace/insert/view/undo | File bytes và observation text bằng source wrappers |
| Filesystem state | File call trước còn ở call sau |
| Inner shell state | `cd`/`export` không persistent qua execute_bash subprocess riêng |
| Timeout/restart | Giữ partial output, đúng outer message, restart session nhưng không reset source |

Unit timeout tests dùng fake clock/session/process để kiểm tra constants/transition mà không chờ 180/210 giây. Chạy một số integration fixtures ngắn trong image local để kiểm tra TTY/prompt/output thật; không tăng test timeout tùy ý rồi suy ra parity đã pass.

### Gate G5

Tool observations và file state khớp oracle sau cùng sequence, không chỉ exit code bằng nhau. Cả mode skip và mode có condenser đều dùng cùng tool output policy.

## 9. Bước 6 — D06: count turns, messages, completion và cap

### File sửa

[config.py](../src/openhands_adapter/config.py), `cli.py`, `openhands/worker.py`, `openhands/process.py`, compatibility agent/contract state, helper `workflow/trae_patch.py` mới.

### 9.1. Contract state và SDK scheduling

1. Contract state giữ `steps = [[assistant_response, *stored_followups], ...]`, `user_message`, reference profile, gen status và filtered patch. Count turn là `len(steps)`.
2. SDK `step()`/`astep()` của compatibility agent thực hiện tối đa một repair response khi cần request mới; tools batch/observations thuộc response đó hoàn tất rồi mới append một logical step. Hai entry points phải dùng cùng transition semantics; worker sync hiện tại không có nghĩa async path được tự động tương thích.
3. Pending action execution, SDK housekeeping/event emission hoặc compression calls không tăng logical turns. Không dùng số ActionEvents hoặc callback invocations làm counter.
4. Dùng SDK run limit chỉ như scheduling guard, không làm nguồn truth của turn budget. Với thiết kế một SDK step = một repair response và cap check ở đầu step kế tiếp, cần scheduling limit **ít nhất `max_turn + 1`**: 51/101 cho profiles 50/100, nếu không có housekeeping steps khác. SDK step cuối chỉ finalize cap/capture WIP, không gọi model và không tăng logical turn. Nếu có housekeeping steps phải tính thêm scheduling allowance. Không đặt SDK limit 50/100 trong thiết kế này: source SDK sẽ trả ERROR sau response cuối, trước contract cap check. Một thiết kế finalize cap ngay sau normal-turn hook cuối chỉ được dùng nếu oracle chứng minh cùng thứ tự side effects và không finalize sớm hơn Trae.
5. Tắt stuck detection, FinishTool auto stop, tool aliases/argument repair, content-policy recovery prompts và context-error recovery làm thêm lượt/messages ngoài nguồn trong profile này. Không cấu hình SDK stop hook có thể từ chối FINISHED rồi chèn feedback/reopen generation; không nạp plugins/agents làm thêm hooks/tools. Transport errors thuộc D07 policy, không tự thành một user nudge mới.
6. `agent_timeout_seconds` hỗ trợ None/disabled cho exact profile; worker supervisor phải xử lý path này và vẫn cleanup trong finally. Nếu môi trường có watchdog bắt buộc, ghi harness abort riêng, không gọi đó là Trae completion/cap.

### 9.2. Port `MessageManager.format_messages()` đúng vị trí

Request gồm system gốc → initial user → stored steps bỏ các fields `agent_*`, rồi ephemeral messages/cache formatting như nguồn. Budget reminders và Continue là **request-time messages**, không tự persist vào `steps`.

`turn_left = max_turn - count_turn()`.

| Message cuối trước format | Verified 50/reminders=True | Multi 100/reminders=False |
|---|---|---|
| User/tool | Thêm user `ENVIRONMENT REMINDER: You have {turn_left} turns left to complete the task.` | Không thêm budget reminder |
| Assistant | Thêm user `Continue in standard tool call format. ENVIRONMENT REMINDER: You have {turn_left} turns left to complete the task.` | Thêm user `Continue in standard tool call format.` |

Initial request Verified cũng có reminder 50 turns, vì message cuối là initial user. Trước request thứ 50 còn 1 turn; sau push turn 50 thì cap trước request thứ 51. Đừng thêm reminder “0 turns” rồi gọi thêm model.

`USE_CACHING=True` là một phần message formatter nguồn: khi trajectory không rỗng và message cuối không phải assistant, content message trajectory cuối được cache-mark trước budget reminder; assistant-last branch không làm cache block đó. Port builder ở bước này để D06 fixture đúng, nhưng wire/provider support cache toàn phần vẫn cần D07–D08.

### 9.3. Port completion transitions bằng source oracle

Pseudocode sau mô tả trình tự, **không phải replacement code đầy đủ của Expert.run**:

```text
before next repair request:
    if count_turn >= max_turn:
        gen = turn_capped
        patch = capture_source_diff_then_original_filter()
        stop generation; expose nonempty WIP to external evaluation

receive one raw repair response:
    if source usage branch permits tool execution:
        parsed_results = execute tools sequentially as source
        if exactly one result is task_done:
            filtered_patch = capture_source_diff_then_original_filter()
            if filtered_patch is nonempty:
                push final response with source terminal followups
                gen = task_done
                stop before compression hook
            else:
                persist Task done result and exact empty-patch user feedback
        elif exactly one result is task_failed:
            push terminal response as source
            gen = task_failed
            stop before compression hook
        else:
            persist tool results; restart outer session if source timeout marker
    push one normal logical step
    call after-normal-turn hook once
```

Các details dễ sai phải kiểm tra trực tiếp source:

- Source chỉ vào tool-execution/completion branch khi `usage['completion_tokens'] is not None`. Missing usage không được SDK finish tool tự kết thúc conversation.
- Successful task_done và task_failed push response nhưng **không append `Task done`/`Task failed` observation vào stored followups** trong nhánh terminal gốc; dispatcher result và persisted terminal transcript là hai thứ khác nhau. Không “sửa” bằng thêm observation chỉ để làm terminal archive trông đầy đủ hơn; không có request kế tiếp cần pair nó.
- Empty task_done persist tool result và exact user feedback `ERROR! Your Patch is empty. Please provide a patch that fixes the problem.`. Feedback này thuộc stored step, khác request-only budget reminder.
- task_done cùng bash/think/editor trong một batch: không special finish, vẫn chạy hết batch theo source rồi push/compression hook.
- Plain assistant text không có tools không tự finish. Push response và formatter tạo Continue ở request sau.
- Normal response thứ max_turn vẫn push và gọi normal-turn hook như nguồn; cap chỉ được phát hiện ở đầu vòng/request tiếp theo. Terminal task_done thì không gọi hook cuối.
- Không khởi động attempt 2 dựa trên failed external validation; generation và external verdict lưu riêng.

### 9.4. Completion gate phải dùng filtered diff gốc

Port phần tối thiểu D12 ngay tại đây:

```text
git --no-pager diff --ignore-submodules=all
    → exact remove_patches_to_tests()
    → patch.strip() có rỗng không?
```

Không lấy `git diff HEAD --binary`/`add -N` hiện tại. Mỗi completion/cap check phải capture diff mới; không dùng cached diff từ trước response.

Fixtures: chỉ sửa tests → filtered patch rỗng → không accept task_done; chỉ tạo untracked source/reproducer → theo unstaged tracked diff gốc; sửa production tracked → nonempty; staged/committed changes phải cho đúng behavior của original capture. Tránh dùng fixture callback cố định “nonempty” cho tất cả tests rồi bỏ sót filter integration.

Nếu current runner sau SDK stop capture patch lại bằng helper cũ, contract đã lệch. Cần truyền filtered patch từ compatibility agent ra runner hoặc runner dùng chính helper nguồn; không quyết định stop bằng một patch rồi submit patch khác.

### 9.5. Kiểm tra transition và count turns

| Test | Assertion bắt buộc |
|---|---|
| One response, multiple tools | Count tăng 1, execution/results đúng thứ tự |
| Plain assistant text | Không finish; request sau có exact Continue branch |
| Empty/test-only task_done | Exact feedback, tiếp tục, counter tăng theo source |
| Nonempty task_done | Gen task_done; terminal transcript đúng source; hook nén không chạy |
| task_done trong batch | Không discard tools sau nó, không auto finish |
| task_failed bất thường | Không advertised schema nhưng transition/persistence đúng source |
| Missing usage | Cùng tool execution/stop decision như Expert source |
| Full 50/100 turns | Không request thứ 51/101; reminder counts/text đúng profile |
| SDK scheduling allowance | Cap-only SDK step không gọi model; không ERROR/MaxIterationsReached trước WIP capture; kiểm tra cả run/arun |
| Cap với WIP/empty patch | Status turn_capped, filtered patch đúng; external policy xử lý riêng |
| Compression/housekeeping calls | Không tăng repair count và không tạo budget reminders mới |
| Repeated actions/stuck pattern | Không bị SDK dừng sớm trước cap của reference |
| External fail | Không feedback/retry generation; một attempt |

### Gate G6

Với cùng scripted responses/tool outputs/patch states, adapter và `Expert.run` oracle có cùng request sequence, stored contract transcript, logical turn count, gen status và filtered patch. SDK-native events/IDs khác được phép, nhưng projection về contract phải bằng oracle.

## 10. Kiểm tra tích hợp và cập nhật tests hiện tại

### 10.1. Không giữ tests cũ đang khóa hành vi sai khác

Các tests hiện có cần phân loại khi triển khai:

| Test hiện có | Thay đổi cho exact profile |
|---|---|
| [test_agent.py](../tests/test_agent.py) | Assertion suffix/default Finish/Think/restricted-tools hiện tại phải chuyển sang exact system/schema checks; generic mode có thể giữ tests riêng |
| [test_cli.py](../tests/test_cli.py) | Cập nhật failure-log-only prompt, custom instructions, profile selection, optional worker timeout và issue mapping |
| [test_workspace_tool.py](../tests/test_workspace_tool.py) | Giữ nếu generic WorkspaceTools còn tồn tại; không dùng test-path blocking/configured-only/40k assertions làm tiêu chuẩn exact profile |
| [test_container.py](../tests/test_container.py) | Kiểm tra mount boundary/capabilities của profile thực tế; không buộc một flag hiện tại nếu nó làm tool gốc không chạy được |
| [test_turn_trajectory.py](../tests/test_turn_trajectory.py) | Giữ response batching/pairing tests; thêm request-only reminders/terminal cases và raw argument preservation |
| [test_history.py](../tests/test_history.py) | Giữ view/summary mapping có ích; không thay thế conformance oracle của message formatter và completion |

Không làm tests pass bằng cách đổi expected prompt/schema sang implementation mới mà không so nguồn. Những tests cho generic mode phải được đặt tên/profile rõ để không nhầm generic behavior với contract Trae.

### 10.2. Bộ tests mới đề xuất

```text
tests/trae_reference.py
tests/test_trae_contract_prompts.py
tests/test_trae_contract_tools.py
tests/test_trae_contract_runtime.py
tests/test_trae_contract_turns.py
tests/test_trae_contract_sdk.py
```

Lệnh dự kiến **sau khi đã tạo các test modules**, từ `Agent-Diet/`:

```bash
PYTHONPATH=src:tests LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
  .venv-openhands/bin/python -m unittest \
  test_trae_contract_prompts test_trae_contract_tools \
  test_trae_contract_runtime test_trae_contract_turns test_trae_contract_sdk
```

Nhóm offline dùng fake transport/session/clock; nhóm container integration chỉ chạy fixtures đã định nghĩa trên image local PHP/fmtlib. Không chạy một benchmark repair live để thay thế oracle tests.

### 10.3. Scripted integration scenario bắt buộc

Một scripted conversation qua **real LocalConversation + compatibility agent**, fake LLM, real hoặc deterministic sandbox:

1. First request đúng source system/user/tools, đúng initial reminder profile.
2. Model tạo reproducer, chạy Bash, đọc/sửa tests/source bằng editor; observations đúng wrapper nguồn.
3. Model gọi think; next request chứa `Continue.` từ think và request reminder đúng vị trí.
4. Model gọi task_done khi chỉ có test-only/untracked diff; nhận exact empty-patch feedback và tiếp tục.
5. Model sửa production tracked code, gọi task_done; capture/filter nonempty, dừng trước compression cuối.
6. External validation chạy trên copy/config gốc; kết quả không sinh thêm model request.

Một scenario riêng chạy tới cap ở cả 50/100 profiles; một scenario riêng kiểm tra Bash long output/editor clipping/session restart. Không ép tất cả hành vi vào một test khó chẩn đoán.

## 11. Các artifacts cần bàn giao khi hoàn thành triển khai

Lưu ngoài repair workspace:

- Effective profile, source hashes, SDK version, actual model/transport, input mapping/path/issue hash.
- Contract message/request snapshots và advertised tool schema đã resolve; normalized comparisons phải ghi rõ chỉ bỏ IDs/infrastructure fields được phép, không normalize nội dung text/argument strings để che drift.
- Trace response→tool_calls→observations→logical_step, turn count, request-only reminders và hook timing.
- Diff trước/sau filter tại completion/cap, gen status và external verdict tách biệt.
- Kết quả G0–G6, scripted SDK integration và sandbox fixtures; transport incompatibility hoặc dependencies D07–D14 còn mở.

Không cần ghi raw credentials; harness debug metadata không được chèn lại vào model messages. Callback preview truncation không được dùng thay full contract capture.

## 12. Checklist nghiệm thu D01–D06

- [ ] GSDK: custom SDK agent/lifecycle, schema conversion và raw-response boundary đã được prototype chạy thật; sync/async paths có cùng semantics, shutdown/cleanup kết thúc đúng.
- [ ] G0: reference source/oracle độc lập đã đóng băng, không import provider globals.
- [ ] G1: exact system text/role; không SDK dynamic injection trong request tương thích.
- [ ] G2: exact user template; chỉ project_path/issue substitutions khác do input PHP/fmtlib.
- [ ] G3: đúng bốn tools, exact schema/descriptions/observations; không security_risk/alias/argument rewrite.
- [ ] G4: agent được Bash/reproducer/test edits như nguồn; evaluator-private config/copy vẫn tách.
- [ ] G5: Bash không cắt 40k; editor clipping/state/timeouts/session restart match source.
- [ ] G6: profile turns/reminders/empty task_done/terminal/cap WIP/hook timing match oracle.
- [ ] Completion và runner submit cùng filtered diff nguồn, không dùng helper HEAD/untracked cũ.
- [ ] Real SDK scripted integration pass, generic tests không bị nhầm thành exact-profile tests.
- [ ] Live transport nếu dùng phải qua kiểm tra system roles/schema; nếu chưa, ghi dependency D07 và không tuyên bố live parity.
- [ ] Chưa tuyên bố full AgentDiet parity cho `ours` khi D07–D14 chưa hoàn tất.

**Trạng thái hiện tại:** tài liệu quy trình đã được viết, đối chiếu source và bổ sung bằng chứng scheduling giới hạn ở mục 2.2. Các compatibility modules, profile names, conformance test cases và lệnh triển khai nêu trên vẫn là công việc cần thực hiện tiếp; chưa được tạo/chạy. Chưa đủ bằng chứng để tuyên bố D01–D06 hoặc chuyển đổi toàn hệ thống đã giữ exact Trae contract.
