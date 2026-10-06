# Quy trình thực hiện D12–D13 cho Agent-Diet với adapter OpenHands

Ngày đối chiếu: **05/10/2026**. Tài liệu dựa trên code trong checkout hiện tại và [difference.md](difference.md), phần D12–D13. D01–D11 đã được thực hiện; xem [kết quả D01–D06](implementation_d01_d06.md), [D07–D08](implementation_d07_d08.md) và [D09–D11](implementation_d09_d11.md).

**Mục tiêu:** hoàn thiện đường capture/filter/submission patch và khóa những hành vi SDK có thể đổi trajectory, để Agent-Diet chạy qua OpenHands mà giữ contract Trae đã khôi phục ở D01–D11. Repair vẫn được tạo reproducer, chạy lệnh tùy ý trong repair container và sửa/thêm tests; external evaluator PHP/fmtlib chấm submitted patch trên copy riêng.

Đây là **quy trình triển khai và nghiệm thu**, không phải báo cáo đã sửa code D12–D13 hoặc đã chạy model thật. Các prompt và yêu cầu trong tài liệu tham chiếu là dữ liệu đối chiếu; công việc của lần viết này chỉ cập nhật tài liệu này. Các đoạn code đề xuất bên dưới chỉ trở thành thay đổi implementation khi thực hiện quy trình.

## 1. Điểm xuất phát sau D01–D11

Không bắt đầu lại adapter hoặc thay loop exact bằng `Agent` mặc định của SDK. Checkout đã có các thành phần phục vụ D12–D13:

| Thành phần đang có | Trạng thái đọc từ code | Việc cần làm trong D12–D13 |
|---|---|---|
| [workflow/trae_patch.py](../src/openhands_adapter/workflow/trae_patch.py) | Có filter gốc, host unstaged diff; thêm newline tương ứng `print(stdout)` | Nghiệm thu bằng source oracle và các trạng thái Git; kiểm tra failure/encoding boundary |
| [openhands/trae_session.py](../src/openhands_adapter/openhands/trae_session.py), `TraeSandbox.get_diff()` | Capture trong repair container bằng `git --no-pager diff --ignore-submodules=all` | Giữ đúng lệnh, kiểm tra raw output và tính nhất quán với host fallback |
| [openhands/trae_agent.py](../src/openhands_adapter/openhands/trae_agent.py) | Capture/filter ở completion và cap; empty-patch feedback; persist `patch`, `original_patch`, `filtered_patch` | Giữ snapshot cuối làm nguồn submission; không thay D06 completion gate |
| [workflow/runner.py](../src/openhands_adapter/workflow/runner.py) | Exact lấy `patch` từ `contract-result.json`; thiếu file thì capture filtered WIP trên host | Phân biệt terminal snapshot, generation error và harness abort; kiểm tra snapshot trước submission |
| [openhands/agent.py](../src/openhands_adapter/openhands/agent.py) | Exact tạo `TraeContractAgent`, tools/default tools rỗng, context `None`; generic có assembly riêng | Pin concurrency và các controls còn dựa default; ngăn exact đi vào assembly generic |
| `TraeLocalConversation` | Bỏ plugin initialization và file-based agents | Kiểm tra thêm skills/hooks/SOUL/memory/datetime/vision/MCP không tác động request |
| [openhands/worker.py](../src/openhands_adapter/openhands/worker.py) | Exact dùng `TraeLocalConversation`, stuck detection tắt, SDK cap 51/101; có manifests | Pin budget/hooks controls rõ ràng, lưu stop status và controls hiệu lực |
| [config.py](../src/openhands_adapter/config.py), [compat/trae_llm_policy.py](../src/openhands_adapter/compat/trae_llm_policy.py) | Có actual/reference role models; exact `ours` reject implicit `inherit`; watchdog mặc định `None` | Giữ hai role policies riêng, khóa unsupported overrides và watchdog exact |
| [openhands/llm.py](../src/openhands_adapter/openhands/llm.py), [trae_transport.py](../src/openhands_adapter/openhands/trae_transport.py) | Exact raw Chat transport, SDK auth/routing/telemetry; inner retries tắt | Chứng minh không fallback, không sửa payload, không thêm budget/recovery/cache |

Tài liệu `difference.md` mô tả các sai khác tại thời điểm đối chiếu ban đầu. Ví dụ watchdog 1800 giây và hidden default `inherit` đã được xử lý một phần trong code hiện tại; không coi tất cả mô tả cũ là lỗi vẫn còn nguyên. D12–D13 chủ yếu hoàn thiện boundary và tăng coverage cho những phần đã dựng.

Nguồn chuẩn để làm oracle:

- [tools/get_diff.py](../artifact/artifact/code/trae_agent/tools/get_diff.py): lệnh diff ở generation không truyền base commit.
- [utils/agent_util.py](../artifact/artifact/code/trae_agent/utils/agent_util.py): `remove_patches_to_tests()`.
- [agents/expert.py](../artifact/artifact/code/trae_agent/agents/expert.py): completion/cap/filter/order.
- [utils/sandbox.py](../artifact/artifact/code/trae_agent/utils/sandbox.py): wrapper lấy diff, decoding và error/retry behavior.
- [compat/reference_hashes.json](../src/openhands_adapter/compat/reference_hashes.json): frozen source hashes của nền `1a436ca7cf59e30c147ecb5b360f8677e675538c`.

Giữ OpenHands SDK **1.49.5** và dependency versions trong [requirements-openhands.lock](../requirements-openhands.lock). Phiên bản khác phải có đợt kiểm tra riêng; không bỏ `require_pinned_sdk()` để tiếp tục chạy.

## 2. Bất biến phải giữ khi triển khai

| Bất biến | Điều kiện kiểm tra |
|---|---|
| Quyền repair | Agent có thể sửa tests/fixtures và tạo reproducer; không chặn bằng protected-test policy |
| Diff | Unstaged tracked changes, dùng đúng lệnh Trae; không tự stage untracked hoặc lấy diff từ `HEAD` |
| Filter | Cùng raw string cho cùng output như hàm nguồn, gồm whitespace và edge cases |
| Completion | Chỉ một observation `task_done` kích hoạt completion check; filtered patch rỗng thì tiếp tục |
| Cap | `trae_verified`: 50 turns/reminders bật; `trae_multiswe`: 100 turns/reminders tắt; cap giữ filtered WIP |
| Submitted patch | Chính snapshot chốt ở terminal/cap; không recapture sau cleanup hoặc sau khi workspace đổi |
| Trajectory | Chỉ Trae manager dựng history; SDK events phục vụ archive/telemetry |
| Nén | Chỉ hook D09–D11 sau completed normal turn; không thêm SDK condenser/summarizer/recovery |
| Model roles | Repair và compression có actual model cùng reference policy riêng; shared actual model phải explicit |
| Stops | Không stuck/auto-finish/budget stop ngoài contract; harness abort được ghi riêng |
| Validation | Dùng config ngoài repair, apply submitted patch lên validation copy; không repair retry từ verdict |

Không đổi system/user/tool prompts, serializer, threshold, parser, reduction gate, retry policy hoặc repair turn counter đã nghiệm thu. Các khác biệt còn lại chỉ thuộc harness, actual model, input PHP/fmtlib hoặc external evaluation như đã xác định trong `difference.md`.

## 3. Bước chuẩn bị chung

Thực hiện từ thư mục `Agent-Diet`:

```bash
cd /home/anh_duc/projects/APR/Agent-Diet
git status --short
git diff --check
.venv-openhands/bin/python -m pip check
```

Checkout hiện có nhiều thay đổi D01–D11 chưa commit và các file untracked. Lưu snapshot working tree và danh sách file trước khi triển khai; không reset/clean để tạo baseline. Baseline của **adapter repo** khác baseline Git của **repair workspace**.

Tạo thư mục bằng chứng khi bắt đầu triển khai, ví dụ `analysis/d12_d13_checks/`, gồm `baseline-status.txt`, versions, logs và mapping fixtures. Đây là vị trí đề xuất, chưa được tạo bởi lần viết tài liệu này. D14 vẫn là công việc riêng; ở D12–D13 chỉ bổ sung bằng chứng cần để kiểm tra patch và controls, không xây lại hệ telemetry.

Giữ reference profile của lần chạy trước. Tài liệu dùng `trae_verified` trong ví dụ; bộ tests phải kiểm tra cả hai profile, không suy profile từ PHP hay fmtlib.

## 4. D12 — Chuẩn hóa capture và filter patch

### 4.1. Chốt ý nghĩa Git diff

Đường generation chuẩn:

```text
repair workspace có baseline sạch
  → agent reproduce/test/edit trong repair container
  → git --no-pager diff --ignore-submodules=all
  → raw string theo wrapper Trae
  → remove_patches_to_tests(raw string)
  → completion/cap decision
  → snapshot submitted patch
```

Lệnh không có `HEAD`, `--cached`, `--binary`, base commit hoặc thao tác `git add -N` của harness. Có thể dùng `git -C <workspace>` trên host, hay `docker exec --workdir <workspace>` trong container, miễn Git chạy ở cùng repo và cùng index/worktree state.

| Trạng thái do agent tạo | Kết quả theo source diff |
|---|---|
| Sửa tracked source, chưa stage | Có diff, sau đó đi qua filter |
| Sửa tracked tests, chưa stage | Có raw diff; block phù hợp filter bị loại |
| Tạo untracked reproducer/test/source | Không tự xuất hiện trong submission |
| `git add` toàn bộ thay đổi của một file | Phần staged không xuất hiện trong unstaged diff |
| Stage một phần, sau đó sửa tiếp | Chỉ phần worktree khác index xuất hiện |
| `git commit` thay đổi | Phần đã commit không xuất hiện nếu worktree sạch |
| Agent chủ động `git add -N` file mới | Có thể xuất hiện trong unstaged diff; harness không tự làm thao tác này |
| Submodule thay đổi | Giữ semantics `--ignore-submodules=all` |

Nếu agent tự stage/commit khiến patch rỗng, gửi empty-patch feedback như nguồn. Không đổi sang HEAD diff để lấy lại thay đổi. Không thêm lời nhắc Git mới vào prompt; điều đó sẽ đổi trajectory D01–D11.

### 4.2. Giữ baseline tracked và workspace tách biệt

`run_baseline()` hiện gọi `create_baseline(repair, case.failure_log)`, tạo commit và kiểm tra repair workspace sạch. Giữ bước này trước generation để tracked source đúng input ban đầu.

Kiểm tra các file production cần sửa thật sự nằm trong index baseline; `git add -A` vẫn tôn trọng `.gitignore`. Nếu source cần sửa bị ignore/untracked ngay từ input, sửa provisioning của case trước generation và ghi rõ input adaptation. Không khắc phục bằng automatic inclusion của untracked ở submission.

Validation copy phải giữ source ban đầu tương ứng baseline. Không copy nguyên repair directory sang validation: cách đó sẽ mang theo test edits, untracked files, build output hoặc state khác của agent. Chỉ apply patch đã chốt vào validation copy.

Baseline failure log hiện được stage cùng repair input. Không đưa evaluator-private config, gold patch hoặc hậu kiểm vào repair mount. `create_workspaces()` và host-side case config tiếp tục giữ boundary D04.

### 4.3. Giữ đúng raw string của wrapper

Trae `get_diff.py` dùng `stdout = check_output(...).decode()` rồi `print(stdout)`. Vì thế successful capture của adapter hiện thêm một newline sau decoded Git stdout:

```python
raw_patch = git_stdout.decode(...) + '\n'
filtered_patch = remove_patches_to_tests(raw_patch)
has_patch = bool(filtered_patch.strip())
```

Đây là mô tả successful path; phải kiểm tra decoding boundary riêng ở mục 4.6. Raw patch không phải lúc nào cũng kết thúc chỉ một newline. Empty diff qua wrapper cho `"\n"`; dùng `.strip()` để **quyết định có patch**, không strip/canonicalize string được lưu.

Giữ newline khi save/load JSON, `.diff` và khi chuyển UTF-8 string → bytes để apply. Không dùng `.strip() + '\n'`, sửa CRLF, sửa header hoặc chuẩn hóa trailing whitespace. Hash phải tính trên bytes thực sự dùng làm submission.

Capture/filter không mutate worktree/index. Có thể gom constants và successful capture logic để tránh drift giữa host/container, nhưng không ép hai đường thành một implementation nếu cách xử lý stdout/encoding khác source wrapper.

### 4.4. Port filter nguyên văn và nghiệm thu quirks

`workflow/trae_patch.py` đã có bản copy `remove_patches_to_tests()`. So sánh với frozen source, giữ thuật toán:

1. `splitlines(keepends=True)`.
2. Chỉ mở block khi dòng bắt đầu bằng `diff --git a/`.
3. Lấy `pieces = line.split()` rồi `to = pieces[-1]`.
4. Nếu `to.startswith('b/')` và chứa một substring bị lọc, bỏ cả block tới header kế tiếp.
5. Giữ các dòng còn lại nguyên văn và `''.join(filtered_lines)`.

Danh sách đúng theo nguồn:

```text
/test/ /tests/ /testing/ /test_
.tests. .test. _test_ _tests_ _test. _tests. .spec.ts
/tox.ini /Cargo.lock /package.json /package-lock.json /pom.xml
```

Không thay filter bằng `changed_paths()`, parser extension hoặc heuristic tên test. Các quirks cần có fixture oracle:

- `.phpt` không tự bị lọc chỉ do extension; `tests/x.phpt` bị lọc bởi `/tests/`, còn `ext/demo/sample.phpt` có thể được giữ.
- `tox.ini.bak` có thể bị lọc vì substring `/tox.ini`; không đổi thành exact basename match.
- `_test_` trong tên production cũng bị lọc theo source.
- Rename xem destination token theo code gốc; không tự xét cả source/destination để sửa policy.
- Header có đường dẫn chứa khoảng trắng/quoted name vẫn dùng `line.split()` như nguồn, dù cho kết quả khác parser Git chuẩn.
- Header không mở bằng exact prefix hoặc text trước header được xử lý nguyên như nguồn; không thêm validation ở filter.
- Binary marker, deletion, mode-only changes và empty/whitespace input phải có kết quả đúng hàm nguồn; không tự bổ sung binary payload.

Path-safety check khi **external apply** vẫn có thể giữ, nhưng phải ghi là evaluator/harness restriction. Nó không được âm thầm rewrite submitted patch hoặc ngăn agent sửa tests trong generation.

### 4.5. Tách generic capture khỏi exact capture

`workflow/patch.py:capture_patch()` hiện dùng `git diff HEAD --binary` và tự `git add -N` untracked. Giữ helper này cho `reference_profile='generic'`; không gọi nó ở exact, kể cả completion, cap, timeout hoặc error fallback.

Đặt tests spy để bất kỳ exact path nào gọi generic capture sẽ fail. Generic regression vẫn phải pass theo behavior generic đang có; không sửa generic thành exact chỉ để có một hàm capture chung.

### 4.6. Kiểm tra error/encoding boundary, không chỉ happy path

Hiện host/container capture dùng `subprocess.run(..., check=True)` và UTF-8 `errors='replace'`. Source `get_diff.py` lại `.decode()` strict, bắt lỗi bên trong rồi **in diagnostic text**; outer `Sandbox.get_diff_result()` retry ba lần với sleep 5 giây khi `exec_run` phát sinh exception và cuối cùng trả `''`. Nó không tương đương một blanket retry trên mọi Git return code.

Phân loại trước khi sửa:

| Tình huống | Hướng nghiệm thu |
|---|---|
| Git stdout UTF-8 hợp lệ, return code 0 | Hai capture paths phải cho cùng raw/filter string như source wrapper |
| Git command lỗi trong helper | Oracle ghi cả stdout diagnostic và return behavior; không gọi exception path hiện tại là literal parity |
| Byte stream không decode được trong helper | Kiểm tra strict decoding/diagnostic của nguồn, không coi replacement characters là tương đương |
| Container runtime/exec exception | Đối chiếu outer retry scope, số attempts và sleep bằng mock; không sleep thật trong tests |
| Container đã mất do harness abort | Host WIP capture được ghi riêng là recovery artifact, không giả terminal Trae capture |

Nếu mục tiêu là literal D12 trên toàn bộ failure domain, triển khai compatibility wrapper cho cả successful và failure semantics, kiểm tra source oracle. Nếu chỉ hỗ trợ successful UTF-8 Git capture, khai báo supported domain/error discrepancy trong báo cáo; D12 chưa được chứng nhận đầy đủ ở phần lỗi.

Diagnostic string của source có thể nonempty nhưng không phải valid unified diff. Không tự sửa hoặc biến nó thành patch hợp lệ; evaluator có thể reject apply. Bằng chứng cần giữ rõ `capture_error`, raw output, filtered output và validation failure.

Không đổi frozen source/hashes để làm test pass. Các retry này thuộc diff wrapper, khác retry LLM D07; không áp policy 12 attempts của provider cho Git.

## 5. D12 — Completion, snapshot và external submission

### 5.1. Giữ completion gate D06

Trong `TraeContractAgent.step()`:

- Chỉ response tạo **đúng một** observation `task_done` mới capture/check patch để kết thúc.
- `task_done` cùng `bash`/`think` trong batch không kết thúc ngay; tools vẫn chạy tuần tự theo source.
- Filtered patch rỗng: push observation và exact user feedback, sau đó xử lý normal turn/nén như nguồn.
- Filtered patch nonempty: push terminal assistant theo nguồn, lưu snapshot rồi kết thúc trước compression cuối.

Feedback phải giữ nguyên:

```text
ERROR! Your Patch is empty. Please provide a patch that fixes the problem.
```

Tại cap, agent capture/filter WIP mà không gọi repair LLM thêm. SDK scheduling limit **51/101** dành một scheduler step để xử lý cap, không phải thêm repair turn: `mgr.max_turn` vẫn **50/100**. Normal turn cuối ở cap vẫn có analysis hook như source; không thêm flush toàn bộ các bước chưa nén.

### 5.2. Dùng snapshot cuối làm nguồn duy nhất cho submission bình thường

Các artifacts hiện có:

| Artifact/field | Ý nghĩa |
|---|---|
| `contract-result.json:gen` | Generation status `task_done`, `turn_capped`, `task_failed` hoặc status lỗi được lưu riêng |
| `original_patch` | Raw diff ở lần capture tương ứng |
| `filtered_patch` | Output của filter, giữ representation gốc |
| `patch` | Submitted patch đã chốt; cap rỗng có thể được chuẩn hóa thành `''` theo implementation completion |
| `contract-patch.diff` | Snapshot submission do worker ghi |
| `patch.diff` | Bytes submission mà runner chuyển sang validation |

Giữ invariant tại successful terminal/cap có patch:

```text
remove_patches_to_tests(original_patch) == filtered_patch == patch
UTF8(patch) == bytes(contract-patch.diff) == bytes(patch.diff)
```

Với no-patch case, kiểm tra cùng `.strip()`-emptiness theo source và quy tắc lưu `patch`; không bắt empty raw diff `"\n"` phải giống byte string `''` ở mọi field.

Trước khi consume snapshot, bổ sung kiểm tra schema/kiểu dữ liệu, profile/turn range và tính nhất quán raw/filter/patch nếu các fields có đủ. Legacy fixtures chỉ ghi `gen/patch/turns` phải được cập nhật theo contract mới nếu áp validation nghiêm hơn. Đừng reject chỉ vì patch chứa paths mà external apply sẽ xử lý riêng.

Snapshot lỗi/malformed phải ghi `contract_artifact_error`; không silently recapture current workspace rồi coi là patch chốt của agent. Để tránh file JSON dở khi process bị ngắt, có thể ghi temporary file cùng directory rồi atomic replace. Hash/metadata chỉ là audit, không thêm content vào prompt.

### 5.3. Phân biệt generation error và harness abort

Worker hiện ghi `contract-result.json` sau khi `conversation.run()` thành công. Compression fatal có thể đã persist `gen='generation_error'` trong state nhưng chưa được export ra artifact; runner thiếu artifact sẽ recapture WIP.

Cần hoàn thiện status boundary:

1. Khi generation exception xảy ra, lưu state/stop reason đã có trong phần exception/finally nếu còn truy cập được. Giữ loại lỗi và `__cause__` do SDK bọc; không gọi thêm repair/compressor để recovery.
2. Snapshot lỗi không tự có nghĩa đã chốt patch. Nếu lưu thêm WIP, đánh dấu `patch_origin='error_wip'` hoặc tên tương đương rõ ràng.
3. Khi process bị kill/timeout, không có terminal snapshot thì giữ status harness abort và host-captured WIP riêng. Không gán `task_done`/`turn_capped` chỉ vì tìm thấy patch.
4. Nếu external policy cho phép chấm WIP sau abort/error, ghi policy đó trong result; không dùng verdict để biến generation thành thành công hay gọi repair lần hai.
5. Normal terminal/cap luôn ưu tiên snapshot chốt, kể cả workspace bị thay đổi sau stop.

Không cần thay thuật toán stop D11. Export status là công việc harness để kết quả không che lỗi generation. Với `task_failed`, giữ failure branch hiện có và submission behavior nguồn, không tự thêm nó thành tool thứ năm.

### 5.4. Apply và validation trên copy riêng

Giữ flow của runner:

```text
snapshot patch → patch.diff → safety/apply check
  → apply lên validation copy ban đầu
  → setup/build → target tests → regression tests
  → verdict PHP/fmtlib và resolved
```

`workflow/patch.py:apply_patch()` giữ check trước apply và `GIT_CEILING_DIRECTORIES` để tránh Git dùng nhầm parent repo nếu validation copy không có `.git`. Test copy không có local `.git` nhưng nằm trong parent repository vẫn cần pass.

Generation status và verdict là hai thông tin độc lập:

- `task_done` có thể external fail.
- `turn_capped` có nonempty WIP vẫn được chấm.
- Test-only sửa trong repair có thể bị filter hết và chưa đủ completion.
- Agent self-test pass không tự là `resolved=True`.

Nếu PHP/fmtlib evaluator cần filter khác, lưu `patch.diff` theo Trae trước, rồi lưu patch adaptation bằng artifact khác và ghi policy evaluator. Không overwrite patch chuẩn. Timeout validation 600 giây giữ thuộc external evaluator; không đưa timeout hoặc verdict đó vào repair loop.

## 6. D13 — Khóa SDK behavior ở assembly và conversation

### 6.1. Giữ architecture đang dùng

Đường exact phải đi qua:

```text
CLI/config → validate_trae_run
  → OpenHands LLM cho credential/routing/telemetry
  → TraeContractAgent + TraeLocalConversation
  → SDKRawTransport + Trae policies
  → MessageManager + parse_tool_response + repair container
  → AgentDiet after_normal_turn
```

Không tạo loop Python độc lập bỏ SDK để gọi là adapter OpenHands. Tests integration phải chạy `conversation.run()`/`arun()` của SDK thật với scripted transport. Đồng thời không quay lại stock `Agent.step()` để dùng recovery/FinishTool/context assembler của SDK.

`MessageManager` là nguồn lịch sử gửi model. `MessageEvent` của SDK có thể có text phục vụ archive, nhưng không được dựng lại repair request từ event list, nhầm logical turns thành event count hoặc dùng archive summary để thay history D09.

### 6.2. Pin controls thay vì trông chờ defaults

Các đề xuất sau bổ sung vào assembly/worker hiện có; chưa phải thay đổi đã áp dụng:

```python
# Nhánh exact trong build_agent(): giữ bind_runtime như hiện tại.
TraeContractAgent(
    llm=llm,
    reference_profile=openhands.reference_profile,
    tools=[],
    include_default_tools=[],
    agent_context=None,
    condenser=None,
    tool_concurrency_limit=1,
    critic=None,
)

# Nhánh exact trong worker; giữ callbacks phục vụ audit.
TraeLocalConversation(
    agent=agent,
    workspace=workspace,
    callbacks=[event_logger],
    visualizer=None,
    max_iteration_per_run=openhands.scheduling_limit,
    stuck_detection=False,
    max_budget_per_run=None,
    hook_config=None,
)
```

Các fields trên có trong SDK 1.49.5. Trước khi thêm kiểm tra constructor/runtime dưới interpreter đã pin; tránh phát minh kwargs từ phiên bản SDK khác. Với generic, giữ behavior generic hiện có.

`tool_concurrency_limit=1` làm effective config rõ ràng. Execution thực của exact vẫn do `parse_tool_response()` loop tuần tự điều khiển, không dùng SDK tool executor; phải kiểm tra thứ tự side effects và observations, không chỉ assert scalar bằng 1.

### 6.3. Ma trận control và bằng chứng runtime

| Hành vi cần khóa | Cách đang có / cần giữ | Bằng chứng cần bổ sung |
|---|---|---|
| Default tools, FinishTool/ThinkTool | `tools=[]`, `include_default_tools=[]`; advertised `TOOLS` lấy từ contract | Wire tools chỉ `str_replace_editor,bash,task_done,think`, cùng schema/thứ tự source |
| Ambient plugins/skills/MCP/vision | `supports_openhands_tools/mcp=False`, custom `_initialize`; conversation overrides | Đặt fixtures plugin/skill/vision/MCP trong workspace; không discover/execute hoặc thêm tools |
| SOUL/memory/datetime/secret guidance | `agent_context=None`, `dynamic_context=None`; raw manager dựng messages | First/next requests khớp source, không có marker context từ fixture |
| File-based agents/hooks | `_register_file_based_agents()` bỏ qua; `_ensure_plugins_loaded()` không setup hooks | `_hook_processor is None`; callbacks/hooks có side-effect fixture không bị chạy |
| Stuck detector/nudges | Worker exact `stuck_detection=False` | Repeated action/error vẫn tới Trae cap, không nudge/recovery user message |
| Auto final-message stop | `TraeContractAgent.step()` tự quản completion | Assistant text thường tiếp tục; chỉ terminal gate nguồn dừng |
| SDK/global condenser | `supports_condenser=False`, `condenser=None` | Stock condenser không gọi; chỉ after-normal-turn hook D09–D11 |
| Tool parallelism | Explicit 1 + source sequential dispatcher | Batch edit→read→think giữ order; response nhiều tools vẫn một turn |
| Monetary budget | `max_budget_per_run=None`, config không nhận `max_budget` | SDK budget checker không tạo stop dù usage/cost telemetry lớn |
| Generation token budget mới | Không thêm aggregate token ceiling | Chỉ cap output 8192/request D07 và turn limit nguồn tác động generation |
| Model fallback/routing rewrite | Raw exact transport dùng prepared model rồi gọi endpoint đã chọn | Inject failure: cùng actual wire ID qua mọi attempt, không alternate model/transport |
| Additional retries/cache | Reference retry 12; OpenAI `max_retries=0`, SDK retries 0 | Không thêm stock retry; repeated identical prompt vẫn gọi endpoint, không completed-response cache |
| Sampling/tool-choice rewrite | Wire body do `policy.params()` và exact raw messages/tools quyết định | Không có `top_p`, `seed`, `tool_choice`, extra_body fields ngoài source |

Controls là các bất biến ở **runtime và wire**, không chỉ flags trong JSON. Không chứng nhận một feature “tắt” chỉ vì default của SDK hôm nay là tắt.

### 6.4. Không sửa workspace content để vô hiệu hóa SDK

Đặt files `SOUL.md`, `.openhands` skills/plugins hoặc agent definition trong fixture nhằm chứng minh SDK không inject ambient context. Không xóa các files đó khỏi repository input chỉ để request nhìn sạch: như vậy đã đổi input và có thể ảnh hưởng repair.

Custom exact conversation hiện không gọi `disable_ambient_discovery()` global; generic worker mới gọi helper này. Giữ per-instance overrides cho exact và test chính các overrides đó. Nếu phát hiện đường SDK khác vẫn discover ambient state, chặn ở bridge/version-pinned hook cần thiết rồi thêm regression fixture; không áp mutation toàn process của host tùy tiện.

## 7. D13 — Model roles, transport và validation cấu hình

### 7.1. Khai báo actual model và reference model riêng

| Role | Actual model field | Reference protocol field |
|---|---|---|
| Repair | `openhands.model` / CLI `--model` | `openhands.repair_reference_model` |
| Compression `ours` | `agentdiet.compressor_model` / `--compressor-model` | `agentdiet.compressor_reference_model` |

Giữ reference defaults đã chọn ở D07–D11: repair `claude4-sonnet`, compression `gpt-5-mini-2025-08-07`. Actual IDs có thể đổi theo endpoint đã được xác minh; D13 không tự chọn model khác để vượt lỗi provider.

Hai actual models khác nhau phải build hai LLM objects. Nếu cố ý chọn actual model chung:

```json
"agentdiet": {
  "compressor_model": "inherit"
}
```

`AgentDietConfig.from_mapping()` hiện đánh dấu `compressor_model_explicit=True` khi field này hiện diện; CLI/env cũng có explicit handling. Shared LLM object vẫn có **hai transport policies**. Repair dùng repair policy; compressor dùng compression policy; không mutate shared LLM settings để chuyển role.

Với reference defaults, payload repair có `max_tokens=8192,n=1,temperature=0.0`; compressor có `max_tokens=8192,n=1,reasoning_effort='low'`, không tools. System variant, assistant prefill/cache theo D08 vẫn giữ. Nhánh reference được chọn bằng literal predicate trên reference model, không trên actual alias mới.

`inherit` implicit bị reject ở exact enabled `ours`. `skip`, disabled và baselines không gọi compression LLM; manifest ghi role này inactive, không giả đã có compressor request.

### 7.2. Khóa endpoint và fallback

`SDKRawTransport` dùng SDK để chuẩn bị auth/endpoint/wire model, rồi exact request được serialize trực tiếp qua OpenAI client. Đây là boundary cần giữ để SDK/LiteLLM không sửa sampling/cache/prefill theo actual model.

Cần kiểm tra:

1. Provider route cuối thuộc domain OpenAI-compatible mà validator cho phép; unsupported route phải fail trước request.
2. `litellm_extra_body`, API seed hoặc `api_version` override ngoài contract tiếp tục bị reject.
3. Model/endpoint lỗi không tự fallback qua alias khác, subscription, generic hoặc model repair dùng thay compressor.
4. Nếu pin thêm SDK `fallback_strategy`/fields, xem đúng API 1.49.5 và chứng minh đường raw không invoke nó. Không thêm unsupported config chỉ để có tên “disable_fallback”.
5. Mọi attempts cùng role giữ nguyên model/body; telemetry failure sau response không retry generation thành công.

Routing prefix là phần harness/provider plumbing: lưu cả SDK actual ID và prepared wire ID. Giữ mapping đã được test cho endpoint, không bỏ prefix dựa vào suy đoán.

### 7.3. Watchdog và giới hạn không thuộc Trae

Code hiện có `workflow.agent_timeout_seconds=None`; `--agent-timeout 0` chuyển thành `None`. Giữ default này. `command_timeout_seconds=120` là generic helper setting, không thay outer Bash 180 giây/inner 210 giây của tool exact D05; validation timeout 600 giây vẫn riêng.

Đề xuất validation D13:

- Run exact được chứng nhận dùng `agent_timeout_seconds=None`; reject positive watchdog trong profile này trước container/login/provider, hoặc cung cấp chế độ harness-abort riêng có nhãn rõ và không tuyên bố cùng Trae stop domain.
- Không đặt giới hạn tiền/tổng token mới; giữ rejection `max_budget` và `OPENHANDS_MAX_BUDGET` hiện có.
- CLI `--max-iterations` vẫn chỉ generic; JSON/env exact không được âm thầm đổi cap 50/100.
- Các unsupported keys như fallback model, custom recovery, parallel tools, extra summarizer nếu xuất hiện ở config phải bị reject rõ thay vì được parser bỏ qua. Chỉ nhận fields implementation thật sự hỗ trợ; kiểm tra cả nested config và worker roundtrip.

Đặt rule watchdog tại validator chung nhận được cả `openhands/diet/workflow`, gọi từ `RunConfig.validate()`, CLI và worker/build entry points phù hợp. Validator D07 hiện chỉ nhận openhands/diet nên chưa kiểm tra workflow. Không copy rule vào một CLI path rồi để API/programmatic entry point bỏ qua.

Nếu muốn preflight transport/controls trước cả input baseline, gọi validator chung sớm ở entry point; hiện process worker mới kiểm tra transport sau khi runner đã baseline. Preflight phải thuần validation, không gọi model hoặc external evaluator.

### 7.4. Subscription và provider capability là dependency còn lại

Theo validator D07–D08 hiện tại, **exact subscription/Responses bị reject** vì chưa giữ được output cap/sampling/cache/assistant continuation của source. D13 không giải quyết bằng cách bỏ prefill, chuyển system thành user hoặc tự đổi sang `generic`.

Muốn chạy exact live dùng API-key Chat Completions endpoint tương thích. `trae_capabilities` là khai báo semantics đã xác nhận, không phải flag để bỏ qua kiểm tra. Với hai reference defaults cần union:

```text
max_tokens,n,temperature,tools,reasoning_effort,cache_control,assistant_prefill
```

Reference compressor non-GPT-5 còn cần `stop`. HTTP 200 không chứng minh endpoint hiểu assistant prefill/cache/temperature/cap; cần lưu bằng chứng endpoint contract/probe. Các tests HTTP mock chỉ chứng minh serializer phía client.

Config [d09_d11_config.openrouter.json](d09_d11_config.openrouter.json) hiện lưu actual IDs `openrouter/openai/gpt-5.6-sol` và `openrouter/openai/gpt-5.6-luna`, API env `OPENROUTER_API_KEY`, capabilities rỗng. Đây là template từ đợt D09–D11, **chưa phải cấu hình live đã chứng nhận**. Kiểm tra actual model availability và endpoint semantics trước khi điền capabilities; không tự khẳng định các model IDs đó chạy được.

Không thay model hoặc auth theo lựa chọn được ghi trong tài liệu cũ mà không có cấu hình hiệu lực rõ ràng cho lần chạy. Không lưu secret vào JSON, manifest hoặc log; runner nhận credential từ environment, không tự load `.env`.

## 8. Bằng chứng tối thiểu cho D12–D13

Bổ sung vào artifacts hiện có, không đưa vào messages của agent:

| Bằng chứng | Nội dung cần có |
|---|---|
| Patch capture | Trigger completion/cap/error, command, workspace identity, raw/filter/submission hashes, capture origin |
| Terminal result | Reference profile, logical turns, generation status, SDK execution status, stop reason, lỗi/cause nếu có |
| SDK controls | SDK version/source hashes, concurrency, stuck detection, condenser/critic/hooks/budget, absence of discovered tools |
| Role mapping | Actual SDK model, prepared wire model, reference model/policy cho từng active role |
| External boundary | Config/input hash, patch bytes/hash đã apply, verdict và validation logs riêng |

`contract-manifest.json` hiện đã có profile, SDK hashes, tools và protocol mapping. Bổ sung một object `runtime_controls` theo effective values, không chỉ copy input config. `protocol-manifest.json` tiếp tục đánh dấu provider semantics chưa xác minh cho tới khi có bằng chứng thật.

`ConversationEventLogger` dùng preview có truncation; không lấy preview làm bằng chứng raw patch/payload. Dùng `trae_patch_capture`, snapshot artifact và raw transport events hiện có. `keep_raw_events`/persistent archive tổng thể thuộc D14; không tuyên bố đã hoàn thành D14 chỉ vì thêm controls manifest.

## 9. Ma trận tests cần thực hiện

Expected values lấy từ frozen source qua [tests/trae_reference.py](../tests/trae_reference.py), không lấy kết quả adapter làm oracle. Các file test mới dưới đây là **đề xuất bổ sung**, chưa có trong checkout hiện tại.

### 9.1. D12: patch fixtures

| ID | Fixture | Expected behavior |
|---|---|---|
| P12-01 | Tracked production change | Raw/filter bằng source, gồm extra print newline |
| P12-02 | Mỗi substring bị lọc + production block | Bỏ đúng block tương ứng, giữ production nguyên byte |
| P12-03 | Test-only sửa rồi `task_done` | Empty feedback, tiếp tục repair |
| P12-04 | `.phpt`, substring false positives, case-sensitive paths | Kết quả bằng source, không extension heuristic |
| P12-05 | Untracked source/reproducer/test | Capture không stage file, index unchanged |
| P12-06 | Staged, partial-stage, committed, intentional add-N | Giữ unstaged-diff semantics từng trạng thái |
| P12-07 | Rename, deletion, quoted/spaced paths, binary/mode-only | Literal filter như source |
| P12-08 | Successful done, sau đó workspace đổi | Runner vẫn submit accepted snapshot |
| P12-09 | Turn cap nonempty/empty/test-only | WIP filter đúng; không thêm repair request |
| P12-10 | Multi-tool có `task_done` | Chạy đủ batch, không premature finish |
| P12-11 | Git failure, decode failure, runtime exception | Oracle failure behavior/domain được ghi rõ |
| P12-12 | Snapshot malformed/missing, worker error/abort | Phân biệt artifact error và WIP origin; không giả task_done |
| P12-13 | Validation copy không local `.git` dưới parent repo | Apply đúng checkout, không silent skip |
| P12-14 | External fail sau valid patch | Không repair retry, `resolved=False` |
| P12-15 | Validation apply policy reject | Giữ submitted patch, báo external apply failure |

Coverage đang có: `test_trae_contract_turns`, `test_trae_contract_workflow`, `test_patch`, `test_workspace`, `test_trae_contract_container`. Có thể thêm `tests/test_trae_contract_patch.py` cho filter/capture/error-domain và mở rộng workflow fixtures cho snapshot consistency/abort. Khi chưa có file mới, không đưa tên nó vào lệnh chạy rồi báo pass.

### 9.2. D13: runtime controls fixtures

| ID | Fixture | Expected behavior |
|---|---|---|
| C13-01 | First request có ambient SOUL/memory/datetime fixture | Exact messages/tools bằng source |
| C13-02 | Workspace skills/plugins/MCP/file-based agent/hooks | Không discover hoặc chạy side effects |
| C13-03 | Repeated plain text/actions/errors tới cap | Không stuck/nudge/auto-finish, đúng 50/100 repair calls |
| C13-04 | Tool batch edit→read→think | Side effects và observations đúng thứ tự, một logical turn |
| C13-05 | Context overflow/API/parser failure | Không stock recovery/summarizer/fallback; source failure dừng |
| C13-06 | Usage/cost lớn | Không monetary/aggregate token stop phụ |
| C13-07 | Hai actual models khác nhau | Requests đúng role/model, policies độc lập |
| C13-08 | Explicit `inherit` cùng actual LLM | Policy/prompt của hai roles vẫn khác theo reference |
| C13-09 | Implicit inherit, unsupported controls, watchdog exact | Preflight reject theo policy đã chọn |
| C13-10 | API-key vs subscription exact | API-key có capabilities hợp lệ; subscription reject trước generation |
| C13-11 | Provider lỗi rồi retry | Không alternate model/route hoặc inner retry phụ |
| C13-12 | Resume đã persist history | History/reference profile không đổi, archive không inject context |
| C13-13 | Async SDK conversation | Cùng requests/order/terminal/cap như sync source |
| C13-14 | SDK version khác pin | Fail rõ ràng trước run |
| C13-15 | Build/worker/CLI/env roundtrip | Cùng effective controls và role mappings |

Coverage nền: `test_trae_contract_sdk`, `test_trae_contract_turns`, `test_openhands_runtime`, `test_trae_contract_llm_policy`, `test_trae_contract_llm_retry`, `test_trae_contract_diet_integration`. Có thể thêm `tests/test_trae_contract_sdk_controls.py` và config-control fixtures; dùng SDK thật với HTTP/scripted mocks, không cần trả phí model.

Đặt sentinel ở stock `Agent.step()`, default condenser, fallback hoặc plugin loader: nếu exact invoke thì test fail. Kết hợp assert effective controls và wire payload; một trong hai riêng lẻ chưa đủ.

## 10. Thứ tự triển khai và lệnh nghiệm thu

### 10.1. Thực hiện theo thứ tự

1. Lưu snapshot D01–D11, kiểm tra source hashes và môi trường đã pin.
2. Hoàn thiện D12 capture/filter tests; xử lý Git failure/encoding domain và baseline tracked source.
3. Hoàn thiện terminal snapshot/export/consume và artifact validation; test exact không gọi generic capture.
4. Pin D13 controls trong exact assembly/conversation; bổ sung validator chung cho unsupported overrides/watchdog theo policy đã chọn.
5. Kiểm tra actual/reference role mappings, no-fallback/no-extra-retry và mode inactive/shared LLM.
6. Chạy focused tests hiện có và tests mới sau khi tạo; sửa lỗi trong scope D12–D13.
7. Chạy full regression D01–D11/generic, container opt-in PHP/fmtlib, rồi mới chạy provider/live prepared cases đủ điều kiện.
8. Ghi báo cáo `analysis/implementation_d12_d13.md` và cập nhật trạng thái README/manifest đúng mức đã kiểm chứng.

Không cập nhật `difference.md` thành “mọi mục đã pass” nếu D14 hoặc provider vẫn chưa nghiệm thu.

### 10.2. Kiểm tra nền và focused suite đang có

Các lệnh sau dùng test modules đã tồn tại:

```bash
cd /home/anh_duc/projects/APR/Agent-Diet
PYTHONPATH=src:tests .venv-openhands/bin/python - <<'PY'
from trae_reference import assert_reference_hashes
assert_reference_hashes()
print('Frozen Trae source hashes: OK')
PY

PYTHONPATH=src:tests LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
  .venv-openhands/bin/python -m unittest \
  test_patch test_workspace test_trae_contract_workflow \
  test_trae_contract_turns test_trae_contract_sdk test_openhands_runtime \
  test_trae_contract_llm_policy test_trae_contract_llm_retry \
  test_trae_contract_diet_integration -v
```

Sau khi thêm tests mới, chạy thêm đúng module names tương ứng. Mocks retry phải thay sleep để kiểm tra schedule, không chờ backoff thật. Các env vars trên chỉ hỗ trợ môi trường test/log; chúng không thay Trae prompt/trajectory contract.

### 10.3. Full regression

```bash
PYTHONPATH=src:tests LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
  .venv-openhands/bin/python -m unittest discover -s tests -v

.venv-openhands/bin/python -m pip check
git diff --check
```

Ghi số tests pass/fail/skip thực tế sau thay đổi, không copy con số 236 của báo cáo D09–D11. Container tests skip trong suite thường phải được chạy riêng ở bước tiếp theo.

Nếu SDK test kẹt do sandbox/process infrastructure như đợt D09–D11, ghi failure môi trường và xử lý quyền thực thi phù hợp; không thêm workaround prompt, stuck stop hoặc timer vào algorithm để test kết thúc.

### 10.4. Container integration PHP/fmtlib

Tests hiện tại dùng hai images:

```bash
docker info
docker image inspect php-src/defect4c:latest
docker image inspect swebench/sweb.eval.x86_64.fmtlib_1776_fmt-3901:latest

AGENTDIET_CONTAINER_INTEGRATION=1 PYTHONPATH=src:tests \
  LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
  .venv-openhands/bin/python -m unittest test_trae_contract_container -v
```

Images phải có sẵn; adapter không tự pull/build. Những fixtures này chạy SDK/tools/container và Agent-Diet trên images thật bằng scripted responses; chúng **không phải** live model repair benchmark.

Bổ sung coverage D12–D13 vào container suite nếu cần: production+tests+untracked, accepted snapshot apply trên copy riêng, sequential batch, role policy và terminal/cap controls. Default nén `ours/500/1/2` phải có case thật sự kích hoạt analyzer; không dùng disabled diet hoặc threshold cực cao rồi kết luận Agent-Diet hoạt động.

## 11. Chạy thử với provider và prepared cases

### 11.1. Preflight không gọi provider

Chọn một prepared PHP case và một prepared fmtlib case. Giữ input layout/config mà loader hiện hỗ trợ; đọc [README phần input](../README.md) trước khi đặt selectors. Không có `Agent-Diet/input` mặc định trong checkout hiện tại, vì vậy phải truyền input root thực tế.

Để kiểm tra template cấu hình hiện có mà không login/container/API call:

```bash
PYTHONPATH=src .venv-openhands/bin/python - <<'PY'
from pathlib import Path
from openhands_adapter.config import RunConfig
from openhands_adapter.compat.trae_llm_policy import validate_trae_run
cfg = RunConfig.load(Path('analysis/d09_d11_config.openrouter.json'))
validate_trae_run(cfg.openhands, cfg.agentdiet)
print('Configuration preflight: OK')
PY
```

Với template capabilities rỗng hiện tại, expected là **ValueError thiếu capabilities**, không phải pass. Chỉ chạy tiếp sau khi đã xác minh endpoint và tạo config hiệu lực, ví dụ `analysis/d12_d13_config.verified.json`. File này là tên đề xuất, chưa được tạo. Ngoài capability gate cần credential environment và actual model availability đúng endpoint.

### 11.2. Chạy từng case khi đã đủ điều kiện

Mẫu dưới đây cần thay input path, case ID và actual model bằng cấu hình đã xác minh. CLI bắt buộc `--model` ngay cả khi JSON có field model; giá trị CLI phải đúng repair actual model trong config:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --config analysis/d12_d13_config.verified.json \
  --input /absolute/path/to/prepared-inputs \
  --case actual-php-case-id \
  --model verified-repair-model-id \
  --agent-timeout 0 --keep-workspaces \
  --output d12_d13_php

PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --config analysis/d12_d13_config.verified.json \
  --input /absolute/path/to/prepared-inputs \
  --case actual-fmtlib-case-id \
  --model verified-repair-model-id \
  --agent-timeout 0 --keep-workspaces \
  --output d12_d13_fmtlib
```

Giữ compressor model/reference fields trong verified JSON; không để CLI vô tình đổi repair model rồi `inherit` chạy model khác dự kiến. Dùng output riêng cho từng lần vì runner thay artifacts của case khi rerun.

CLI exit 0 khi tất cả cases `resolved=True`, exit 1 có thể là case unresolved sau generation/validation hợp lệ. Không kết luận adapter hỏng chỉ từ exit 1; đọc generation status, validation status và error phase.

### 11.3. Những gì phải xem sau run

- Manifests đúng SDK/profile/role models, watchdog tắt, no-extra controls; endpoint semantics có bằng chứng riêng.
- Raw patch/filter/submitted patch và apply bytes khớp; tests/untracked handling đúng D12.
- Logical repair calls không vượt 50/100, tools sequential, không extra stop/recovery/fallback.
- Khi output đủ dài, Agent-Diet thực sự có analysis request đúng role và decision đúng D09–D11.
- External setup/build/target/regression thực sự chạy trên validation copy bằng config gốc; không dùng model text hoặc self-test làm verdict.
- Generation error, harness abort, no patch, apply error và unresolved bug được phân biệt.

Live smoke chứng minh đường credential→model→tools→diet→patch→evaluator chạy; nó không chứng minh mọi bug sẽ được sửa. Local conformance chứng minh client/source contract; provider compatibility cần thêm evidence live, đặc biệt assistant prefill/cache/sampling.

## 12. Điều kiện hoàn thành và báo cáo kết quả

**D12 hoàn thành** khi capture/filter/submission khớp source trên domain được nghiệm thu, tests không bị cấm trong repair, không tự include untracked, snapshot terminal/cap không bị thay bởi recapture, và validation copy chỉ nhận đúng submitted patch. Failure/encoding differences chưa giải quyết phải được ghi rõ thay vì tuyên bố literal parity toàn bộ.

**D13 hoàn thành** khi exact run không có tools/context/hooks/stop/fallback/retry/budget/summarizer phụ, batch execution tuần tự, effective controls được pin và kiểm tra với SDK 1.49.5, hai role models/reference policies được khai báo rõ, unsupported configuration bị reject ở entry points cần thiết.

**Agent-Diet với OpenHands được nghiệm thu theo từng mức:**

| Mức | Bằng chứng bắt buộc | Kết luận được phép |
|---|---|---|
| Local source contract | Frozen oracle + focused/full tests | D12–D13 phía adapter đúng các fixtures/domain đã kiểm chứng |
| SDK/container integration | SDK run/arun + PHP/fmtlib images + actual tool/diet/patch/apply | Adapter và Agent-Diet vận hành qua OpenHands/container |
| Live provider + prepared cases | Credential/model/capabilities đã xác minh, run logs và artifacts đầy đủ | Đường chạy thật hoạt động với model/endpoint/cases cụ thể |

Trong `implementation_d12_d13.md`, ghi files thay đổi, behavior cuối, test counts/logs, runtime/model/endpoint, source/SDK hashes, các fixture IDs đã pass và việc còn thiếu. Tách local conformance khỏi provider verification; không đánh dấu D14 hoàn thành theo D12–D13.

Trạng thái của **lần viết tài liệu này**: đã đọc source/reference và code hiện tại để xây dựng quy trình; chỉ cập nhật `fix_d12_d13.md`, chưa triển khai code D12–D13, chưa chạy bộ conformance/container hoặc live model PHP/fmtlib cho D12–D13.
