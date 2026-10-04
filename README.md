# Agent Diet adapter for OpenHands

## Cài đặt OpenHands

Tạo một virtual environment riêng cho adapter (SDK được pin ở phiên bản đã
được kiểm tra) từ thư mục `Agent-Diet`:

```bash
./setup_openhands.sh
```

Mặc định script dùng `python3`; có thể truyền một Python cụ thể:

```bash
./setup_openhands.sh /usr/bin/python3.11
```

Môi trường được tạo ở `.venv-openhands/` và không làm thay đổi Python hay các
dependency của project khác.

## Xác thực mô hình

Copy mẫu biến môi trường rồi điền secret nếu dùng API key:

```bash
cp src/.env.example .env
set -a
source .env
set +a
```

Model không lưu trong `.env`; mỗi lần chạy phải truyền rõ bằng `--model`.

Runner truyền `reasoning_effort` vào SDK cho cả subscription và API key,
mặc định `low`; đổi bằng `--reasoning-effort` hoặc `OPENHANDS_REASONING_EFFORT`.
SDK đang pin không gửi tham số reasoning trong request subscription, nên giá trị
này trong SDK chưa đảm bảo endpoint subscription áp dụng mức effort tương ứng.

Output shell gửi vào agent mặc định giữ tối đa 40.000 ký tự cuối mỗi lệnh,
giống ContextSniper và Native-Agent, không thêm marker khi cắt. Giới hạn
áp dụng cả khi `--diet-mode skip`. Có thể đổi bằng `--output-limit-bytes`;
tên tùy chọn được giữ tương thích nhưng giới hạn thực tế tính theo ký tự,
không phải token. Validation giữ output đầy đủ để xét verdict và lưu log;
giới hạn này chỉ áp dụng cho shell tool của agent repair.

Với ChatGPT Plus/Pro (mặc định), đăng nhập OpenHands một lần:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --login-only --model gpt-5.6-luna
```

Để dùng API key, đặt secret trong `.env`, sau đó truyền auth và model lúc chạy:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --auth api-key --api-key-env OPENAI_API_KEY --model gpt-5.6-luna \
  --input /path/to/prepared-inputs --case <case-id> --output exp1
```

Không commit file `.env`.

## Chạy một case

```bash
set -a; source .env; set +a
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --model gpt-5.6-luna \
  --input /path/to/prepared-inputs \
  --case <case-id> \
  --output exp1
```

Để chạy nhiều case **lần lượt**, thay `--case <case-id>` bằng
`--case <case-1> <case-2>`, hoặc `--all-cases` để chạy toàn bộ prepared cases
trong input theo thứ tự đường dẫn. Có thể lặp lại `--case`; case trùng chỉ chạy
một lần. Case lỗi hoặc chưa sửa được không ngăn case tiếp theo chạy.
Exit code là `0` khi mọi case resolved, còn lại là `1`. Với batch, stdout là
JSON chứa `results` và `all_resolved`; mỗi case vẫn có artifacts riêng.
Chạy lại cùng case với cùng output sẽ thay artifacts cũ.

Nếu bỏ qua `--output`, runner ghi vào `Agent-Diet/output/<case-id>/`.
Với `--output exp1`, kết quả nằm trong `Agent-Diet/output/exp1/<case-id>/`.
Có thể dùng thư mục con nhiều cấp, ví dụ `--output experiments/ours`.
Đường dẫn luôn nằm dưới `Agent-Diet/output/`, độc lập với thư mục đang chạy;
`--output output/exp1` cũng tương đương `--output exp1`. Cấu hình JSON
`workflow.output_root` tuân theo cùng quy tắc.

Adapter cần Docker daemon đang chạy và image được khai báo trong prepared
input đã có sẵn cục bộ; nó không tự pull hoặc build image.

Agent chỉ có tool thao tác file (`list_files`, `read_file`, `search_text`,
`write_file`, `edit_file`, `inspect_workspace_diff`) và hai tool command:

- `list_configured_commands()` trả các command cùng `phase`, `index` (từ 0),
  `argv`, `cwd`.
- `run_configured_command(phase, index)` chạy đúng một command đã khai báo.
  Agent không truyền được command, argv, cwd hay thêm test filter.

Config thực thi được chuyển qua worker-config tạm bên ngoài repair workspace,
không mount vào container và không sinh `.agent-diet.repair-config.json` trong
workspace; file cũ cùng tên trong source cũng được loại khi copy workspace.
CLI truyền trực tiếp danh sách lệnh gồm `argv`/`cwd` từ case đã load cho worker.
Executor giữ bản command bất biến trong bộ nhớ. Target resolve
`{test_id}` giống evaluator; shell wrappers, cwd, filters và exclusions giữ nguyên.
Không còn tool shell tùy ý. Setup/build cũng dùng cùng cơ chế phase/index.
Các tool file chặn đường dẫn ngoài workspace, symlink, `.git`, artifact nội bộ,
và chặn sửa test/fixture theo tên thông dụng cùng các file được command tham
chiếu trực tiếp. `edit_file` yêu cầu một đoạn khớp đúng một lần; `write_file`
yêu cầu nội dung cũ chính xác khi ghi đè. Diff bao gồm cả source mới tạo.

Giới hạn này khóa command trực tiếp mà agent yêu cầu chạy. Nó không cô lập
hành vi bên trong source hay script được command gọi gián tiếp, và quy tắc tên
file không nhận diện được mọi layout test riêng của từng project. Validation
sau patch vẫn chạy độc lập trên bản workspace riêng bằng config gốc.
Với build/test dài, dùng `--command-timeout 600` (hoặc mức phù hợp với case)
để tránh timeout mặc định 120 giây khiến agent phải chạy lại build.

Loader nhận cả hai layout prepared input, không cần đổi tên hay copy metadata:

```text
# PHP / Defects4C
inputs/<case-id>/
inputs/<case-id>.debugging-framework.json
inputs/<case-id>.failure.log

# fmtlib / SWE-bench
fmtlib/<case-id>/config.json
fmtlib/<case-id>/failure.log
fmtlib/<case-id>/<case-id>/
```

Với layout thư mục, tên source lấy từ `config.project_id`, hoặc tên thư mục
chứa config nếu không khai báo `project_id`. `--input` có thể trỏ vào thư mục
chứa nhiều case hoặc trực tiếp thư mục prepared của một case. Chọn bằng
`--case <case-id>`, đường dẫn tương đối hoặc prefix duy nhất; dùng `--all-cases`
để chạy toàn bộ. Cùng source có cả hai bộ tên metadata chỉ được chạy một lần.

Adapter nối quy trình sửa bug từ system prompt Trae vào `AgentContext` của SDK,
giữ system prompt và hướng dẫn tool mặc định của OpenHands: hiểu lỗi → tìm code
→ reproduce trước khi sửa → xác định nguyên nhân → sửa tối thiểu → chạy lại
reproduce và test liên quan → tổng kết bằng `FinishTool`.
Chỉ sửa production code; dùng test có sẵn hoặc script tạm trong `/tmp` của
container, không thêm/sửa test trong repository.

Với `--input <inputs> --case <case-id>`, adapter resolve source có bug theo
layout tương ứng, copy vào repair workspace và mount workspace vào container.
Model dùng workspace tools để tìm, đọc và sửa source tại đường dẫn trong prompt.
Failure log của case được copy nguyên nội dung thành
`.agent-diet.failure.log` trong workspace. Prompt yêu cầu model đọc file này
trước khi chẩn đoán lỗi. `problem_statement` luôn là `None`, kể cả khi JSON có
trường này; nội dung log không được chèn vào prompt. `--prompt` / `--prompt-file`
thay phần task instructions; source path, hướng dẫn đọc log và ràng buộc sửa
code vẫn luôn được bổ sung.

Mode `ours` tự tạo callable nén qua LLM của OpenHands. Với
`--compressor-model inherit` (mặc định), compressor dùng chung LLM của agent.
Để dùng model nén riêng, truyền `--compressor-model <model>`; model này dùng
cùng cấu hình xác thực và endpoint với agent. Các mode khác và `--disable-diet`
không tạo LLM nén riêng. Nếu lời gọi nén lỗi hoặc trả về nội dung rỗng,
Agent-Diet giữ nguyên step và ghi nhận `compressor_error`.

Prompt của mode `ours` được chuyển từ `traj_analyzer.py` của Trae vào
`src/openhands_adapter/diet/prompts.py`, gồm quy tắc giữ cấu trúc và các ví dụ nén.
Compressor nhận ID step cụ thể, kiểm tra wrapper `<step id="...">...</step>`,
bóc wrapper và từ chối response bị cắt hoặc sai ID. Model hỗ trợ stop words
dùng `stop="</step>"`; Responses API dùng wrapper đầy đủ và kiểm tra status.
Assistant prefill có thể bật qua `build_compressor(..., assistant_prefill=True)`
cho completion endpoint tương thích; mặc định tắt để dùng được subscription.

`result.json` ghi `token_usage.repair`, `token_usage.compression` và
`token_usage.total`. Mỗi nhóm có input/output/total, cached input, cache write,
reasoning tokens, số lần gọi, số lần lỗi và số lần thiếu usage. Cache và reasoning
là thành phần của input/output tương ứng; không cộng thêm chúng vào total.
Không tính hoặc lưu chi phí tiền. `analysis_cost_tokens` được đổi tên thành
`compression_total_tokens`. Bỏ `--max-budget`/`OPENHANDS_MAX_BUDGET` vì không
còn tính chi phí tiền; giới hạn run bằng iterations và timeout.

`events.jsonl` ghi `llm_call_started` và `llm_call_finished` theo ID từng request:
vai trò sửa bug/nén, model, ID response, finish reason/status, usage và độ dài
context ước tính bằng encoding `gpt-4o`. `reminder_message_token_estimate` tính
cả message chứa reminder. Ước tính context gồm messages/instructions/input/tools,
không phải token usage chính xác do provider báo. Tổng chỉ cộng mỗi request một
lần, kể cả dùng chung LLM với `compressor_model=inherit`; response nén bị từ chối
vẫn được tính. Retry là lần gọi riêng. Không lưu toàn bộ payload LLM hay credentials.

Usage provider không trả được ghi `null`, không đổi thành 0. `known_tokens`
chứa phần đã biết; `complete=false` và `unknown_usage_calls` cho biết tổng còn
thiếu. Nếu worker bị ngắt, workflow vẫn tổng hợp từ các event đã ghi; request
chưa kết thúc có `pending_calls`. Chi tiết cache/reasoning không được trả cũng
là `null`, ngay cả khi input/output đã đầy đủ.

`diet_metrics.analysis_count` đếm candidate bắt đầu xử lý (gồm baseline),
`compression_llm_calls` đếm request nén LLM; `erase_count` đếm quyết định thay
step. `rejected` ghi số lần từ chối theo lý do. `application_checks` ghi kết quả
SDK áp dụng thay đổi: verified/view_mismatch/not_observed_before_run_end.
`analysis_prompt_tokens`, `analysis_completion_tokens`, `compression_total_tokens`
lấy từ tổng request nén, bao gồm retry và output bị từ chối.

`erase_in_tokens`, `erase_out_tokens`, `step_content_reduction_tokens` đo độ dài
step trước/sau. `reminder_token_estimate` đo riêng phần reminder thêm vào;
`view_reduction_token_estimate` là chênh lệch view dự kiến sau thay thế, đã tính
message reminder. Các trường này là ước tính độ giảm context tại thời điểm nén,
không phải tổng token tiết kiệm qua mọi lượt. Muốn đo tiết kiệm toàn run cần so
sánh input tokens giữa các lần chạy có/không nén với cùng cấu hình.

Threshold và metrics nén dùng `tiktoken` với encoding của `gpt-4o`, giống Trae:
đầu vào tính trên step đã serialize (gồm wrapper `<step id="...">`), đầu ra
tính trên nội dung compressor trả về. Mode `ours` trả nội dung không có wrapper.
Encoding được dùng cố định dù model
agent khác `gpt-4o`. Lần đầu sử dụng cần tải bảng encoding; các lần sau dùng
cache của `tiktoken`.

Khi bật `--use-lz4`, runner nén LZ4 trên context phía sau có và không có step
mục tiêu, rồi dùng chênh lệch dung lượng để ước tính số token dư thừa và so
với threshold, theo cách Trae tính. Dữ liệu dùng UTF-8 và các step đã serialize
được nối không thêm dấu phân cách. Với `--ctx-after 0`, context phía sau rỗng.

Baseline `random` xóa token `tiktoken` trên step đã serialize, chỉ chọn token
tự decode được UTF-8, rồi decode phần còn lại; không dùng `split()` hay nối
lại bằng dấu cách. Xuống dòng và indentation chỉ mất nếu token chứa chúng bị xóa.
Baseline `lingua` dùng model
`microsoft/llmlingua-2-xlm-roberta-large-meetingbank` với LLMLingua2 trên CPU,
cache compressor trong worker và giữ `\n`, `?` qua `force_tokens`.
Mode này cần dependency tùy chọn (`.venv-openhands/bin/python -m pip install llmlingua`)
và tải model ở lần dùng đầu. Nếu thiếu thư viện hoặc nén lỗi, runner giữ nguyên
step, ghi `compressor_error` và chi tiết trong `diet_compressor_error`; không
chuyển sang baseline random.

Điều kiện chấp nhận mức giảm chỉ áp dụng cho `ours`: mặc định giảm ít nhất
400 token hoặc đầu ra còn dưới 80% số token đầu vào. Hai tùy chọn
`--min-reduction-tokens` và `--min-reduction-ratio` chỉ điều chỉnh mode này.
Các baseline `delete`, `random`, `lingua` áp dụng trực tiếp sau khi qua threshold
và kiểm tra LZ4 (nếu bật); vẫn giữ nguyên step khi nén lỗi hoặc trả sai kiểu dữ liệu.

Sau nén, view của agent thay step bằng assistant message có
`(System reminder: compressed for better efficiency)` và nội dung nén. Mode
`delete` vẫn để lại `(System reminder: long content deleted for better efficiency)`.
Archive dùng event `Condensation` chuẩn của SDK; condenser chuyển summary sang
assistant message khi dựng view cho LLM, không sửa view được SDK cache.

Step của Agent Diet là một phản hồi model cùng toàn bộ kết quả tool của phản hồi
đó, theo Trae gốc. OpenHands gom action bằng `llm_response_id`, gắn kết quả bằng
`tool_call_id`/`action_id`; nhiều tool trong một phản hồi vẫn chỉ tính một step.
Step còn thiếu kết quả tool không được nén. Initial user prompt là context `-1`,
không phải target nén; system và event không xác định được quan hệ được giữ trong
SDK view nhưng không tăng bộ đếm step. User message bổ sung được gắn vào step trước.

Trajectory dùng `<think>` cho assistant content, `<call tool="...">` cho lời gọi
gốc và `<result>` cho output thực sự gửi tới LLM. ThinkTool giữ quy tắc Trae:
arguments vẫn nằm trong `<call>`, còn kết quả xác nhận được thay bằng argument
`thought` trong `<think>`. Reasoning riêng (`reasoning_content`, thinking blocks,
Responses reasoning items) không đưa vào compressor; không xóa các field đó khỏi
event gốc. Khi nén, toàn bộ step được thay bằng summary như Trae. Quy tắc này dùng
chung event SDK cho GPT subscription (Responses) và OpenRouter (Chat Completions).

Condenser giữ `original_steps` và ánh xạ `summary_to_step` trong RAM của worker.
Context cho compressor (kể cả kiểm tra LZ4) khôi phục nội dung gốc của các step
đã nén; agent chỉ nhận bản rút gọn. ID step ổn định và summary không được chọn
làm target nén lại. Kho gốc mất khi worker kết thúc; hiện chưa hỗ trợ khôi phục
kho này khi tiếp tục conversation trong worker mới.

Mỗi case ghi `result.json` (verdict, elapsed time, token usage và error nếu có)
và `events.jsonl` (các stage, action/observation của agent và metrics). Patch,
response và log lệnh validation vẫn được giữ để kiểm tra; runner không tạo
`baseline.json`, `validation.json` hay `diet-events.jsonl`.

Output mỗi case giữ `result.json`, `events.jsonl`, `patch.diff`, `response.txt`
(nếu giai đoạn tương ứng đã chạy). Log worker và command guard nằm trong
`logs/`, log validation trong `logs/validation/`; log rỗng và thư mục log rỗng
được dọn. Config worker và workspace mặc định nằm trong `.tmp/` của case,
được xóa sau khi chạy kể cả khi lỗi/timeout. Chỉ `--keep-workspaces`
mới giữ các bản sao workspace trong `workspaces/` của case. Log cũ không rỗng được
giữ để không mất dữ liệu. Khi chạy lại case, runner dọn kết quả/patch/response
cũ trước khi bắt đầu để không lẫn với output của lần mới; config worker cũ cũng
được dọn.

Để kiểm tra Agent-Diet đã thay đổi context nào, lọc `events.jsonl` theo
`diet_step_change`: mỗi bản ghi có mode, step index, event IDs, nội dung trước
và nội dung sau (hoặc `proposed_text` nếu bị từ chối). `diet_sdk_condensation`
ghi các event IDs gửi cho OpenHands; `diet_application_check` xác nhận ở lượt
ngưng tụ kế tiếp rằng View thực tế khớp với View dự kiến sau khi xóa/thay thế.
Trạng thái `not_observed_before_run_end` nghĩa là lần xóa cuối không có lượt
tiếp theo để đối chiếu.
