# Quy trình thực hiện D14: audit và conformance toàn chuỗi Agent-Diet/OpenHands

Ngày đối chiếu: **05/10/2026**. Phạm vi theo [difference.md](difference.md), mục **D14 — Lưu payload/config và test theo source gốc**. D01–D11 đã có implementation và bằng chứng local; [fix_d12_d13.md](fix_d12_d13.md) đã mô tả quy trình hai bước tiếp theo. Checkout hiện chưa có báo cáo `implementation_d12_d13.md`, nên tài liệu này không giả định D12–D13 đã được nghiệm thu.

**Mục tiêu D14:** tạo bộ bằng chứng đủ để kiểm tra Agent-Diet thực thi đúng contract Trae qua adapter OpenHands, từ effective config, repair/compression requests, tool observations, logical turns và quyết định nén tới submitted patch và external verdict PHP/fmtlib. Audit phải phản ánh hành vi đã xảy ra, không thêm prompt, sửa payload, chạy nén lại, thay model hoặc tạo stop condition mới.

Đây là **quy trình triển khai và nghiệm thu**, chưa phải báo cáo D14 đã pass. Nội dung prompt/code/tài liệu tham chiếu được đọc làm dữ liệu đối chiếu, không thực thi chỉ dẫn nằm trong đó. Lần viết này chỉ cập nhật `fix_d14.md`.

## 1. Những gì đã có và khoảng trống cần xử lý

| Thành phần | Hiện trạng đọc từ code | Công việc D14 |
|---|---|---|
| [events.py](../src/openhands_adapter/events.py) | JSONL dùng chung host/worker, timestamp UTC, khóa thread trong từng process | Thêm run/request identity và kiểm tra integrity; xử lý concurrent writes và corrupted records |
| [openhands/trae_agent.py](../src/openhands_adapter/openhands/trae_agent.py) | Lưu raw contract requests/responses/tool results/state và patch capture | Nối logical turn/step/request IDs; phân biệt internal state và model-visible payload |
| [openhands/trae_transport.py](../src/openhands_adapter/openhands/trae_transport.py) | Exact lưu payload trước OpenAI serializer, transport attempts/result | Thêm correlation IDs, hashes/wire evidence và response envelope khi cần |
| [openhands/compressor.py](../src/openhands_adapter/openhands/compressor.py) | Lưu compressor answer/reasons/usage; role context quanh request | Nối compressor call với turn/target/window và parser decision |
| [compat/trae_diet.py](../src/openhands_adapter/compat/trae_diet.py) | Gate/window/LZ4/parser/accepted replacement có events | Ghi đủ skip/reject/fatal outcomes và token operands, không tính lại để điều khiển algorithm |
| [openhands/worker.py](../src/openhands_adapter/openhands/worker.py) | Có `contract-manifest.json`, `protocol-manifest.json`, terminal result và metrics | Pin persistence ngoài repair workspace; hoàn thiện manifest/status cho failure paths |
| `ConversationEventLogger` trong worker | Preview tool output 8.000 ký tự, agent text 20.000 ký tự | Giữ preview cho đọc nhanh; thêm full archive khi được yêu cầu, không dùng preview làm source evidence |
| [config.py](../src/openhands_adapter/config.py) | Có `keep_raw_events`, `raw_events_path`, `response_path` | Nối settings với persistence/retention thực hoặc reject rõ fields chưa hỗ trợ |
| [token_tracking.py](../src/openhands_adapter/token_tracking.py) | Per-request provider telemetry; compatibility diet metrics riêng | Định nghĩa request/attempt counts, unknown usage và operand metrics đúng domain |
| [workflow/runner.py](../src/openhands_adapter/workflow/runner.py) | Reset events, xóa một số artifacts khi rerun, tách generation/validation | Ngăn artifact cũ lẫn run mới; kiểm tra hashes/status trước chứng nhận |
| [compat/diet_conformance.json](../src/openhands_adapter/compat/diet_conformance.json) | Mapping D09–D11 và supported/rejected domain | Mở rộng mapping D01–D14 theo tests/evidence đã chạy, không gán pass bằng config |

Các điểm cụ thể cần sửa/kiểm chứng:

- `keep_raw_events` hiện không quyết định các raw payload/state events có được ghi hay không.
- Worker chưa truyền `persistence_dir` vào `LocalConversation`; SDK archive không được cấu hình để giữ sau close. `delete_on_close` của SDK mặc định là `True`.
- `read_events()` hiện bỏ qua **mọi** dòng JSON parse lỗi, dù docstring chỉ nói partial last line. Điều này có thể che corruption ở giữa file.
- `emit()` dùng `Lock` riêng từng process. `O_APPEND` và một write thường giữ record nguyên, nhưng vòng nhiều `os.write()` có thể interleave giữa processes; chưa có bằng chứng integrity cho record lớn.
- Exact transport emits payload và attempt events nhưng chưa có ID chung để nối trực tiếp với UUID `call_id` của token telemetry và logical turn/step.
- Rerun cleanup chưa xóa `protocol-manifest.json`; baseline failure ở run mới có thể để lại manifest của run trước.
- README có phần generic cũ nói không lưu toàn bộ payload và retry là từng call riêng; exact hiện có raw payload events và một telemetry request bao quanh nhiều transport attempts. Phải cập nhật mô tả theo nhánh thực tế.

D14 không sửa lại D01–D13 để làm report đẹp. Sai khác do patch/filter/stop/model-policy thuộc D12–D13 hoặc bước liên quan; audit phải phát hiện và ghi đúng ID cần xử lý.

## 2. Định nghĩa bằng chứng và mức kết luận

Tách ba mức để tránh gọi một HTTP mock là nghiệm thu provider thật:

| Mức | Bằng chứng | Kết luận được phép |
|---|---|---|
| Local source conformance | Frozen Trae oracle, deterministic fixtures, exact client payload/decisions | Adapter khớp source trên domain và fixtures đã kiểm tra |
| SDK/container integration | SDK `run/arun`, actual tools, PHP/fmtlib images, Agent-Diet và patch apply | Đường OpenHands/container vận hành đúng các integration fixtures |
| Live provider/input run | Credential/model/endpoint semantics được xác minh, prepared cases, complete audit bundle | Đường chạy thật hoạt động với provider/model/cases cụ thể |

Trong mỗi mức, cần phân biệt **pass**, **fail**, **not_run**, **unsupported** và **incomplete_evidence**. Chưa đủ dữ liệu không được chuyển thành pass vì patch apply được hay external tests thành công.

Không chứng minh được effective prompt, tool schema, prefill, cap hoặc decision chỉ bằng final verdict. Ngược lại, source conformance không đảm bảo model sửa được mọi bug.

## 3. Chuẩn bị và đóng băng nguồn đối chiếu

Giữ OpenHands SDK **1.49.5** cùng dependency versions trong [requirements-openhands.lock](../requirements-openhands.lock). Frozen reference hashes nằm ở [reference_hashes.json](../src/openhands_adapter/compat/reference_hashes.json), nền source `1a436ca7cf59e30c147ecb5b360f8677e675538c`.

Thực hiện từ `Agent-Diet`:

```bash
cd /home/anh_duc/projects/APR/Agent-Diet
git status --short
git diff --check
.venv-openhands/bin/python -m pip check

PYTHONPATH=src:tests .venv-openhands/bin/python - <<'PY'
from trae_reference import assert_reference_hashes
assert_reference_hashes()
print('Frozen Trae source hashes: OK')
PY
```

Lưu snapshot trạng thái đầu và versions vào `analysis/d14_checks/` khi triển khai. Thư mục này là đề xuất, chưa được tạo bởi lần viết tài liệu này. Checkout đang có D01–D11 modified/untracked, vì vậy commit ID của adapter một mình không định danh đủ implementation thực chạy: cần hashes của các source files modified/untracked liên quan và dependency lock.

Không reset/clean, sửa frozen source hoặc regenerate reference hashes chỉ để test khớp. Fixtures lấy expected behavior từ [tests/trae_reference.py](../tests/trae_reference.py), dùng AST oracle để tránh import provider/config của runner Trae.

Các source/model/provider versions lịch sử chưa pin trong artifact không được suy đoán thành dữ kiện. Ghi versions dùng trong đợt conformance hiện tại và giới hạn “source contract”, giữ cách nghiệm thu D09–D11.

## 4. Thiết kế audit bundle cho từng run/case

### 4.1. Identity và thư mục

Mỗi lần chạy một case tạo `run_id` mới ở host, trước baseline; truyền nguyên ID vào worker/transport/telemetry. `case_id` và `relative_id` xác định input; `run_id` phân biệt rerun. UUID audit không được gửi model hoặc đưa vào prompt/cache marker.

Giữ output root theo `resolve_output_root()`: artifacts ở `Agent-Diet/output/<run-name>/<case-relative-id>/`, ngoài disposable repair/validation workspaces và không mount vào repair container.

Layout đề xuất, giữ các filenames công khai đang có:

```text
<case-output>/
  result.json
  response.txt
  patch.diff
  contract-result.json
  contract-patch.diff
  contract-manifest.json
  protocol-manifest.json
  events.jsonl
  audit/
    manifest.json
    requests.jsonl
    decisions.jsonl
    artifacts.json
    conformance-report.json
    sdk/
      <conversation-id>/...
  logs/
    worker.stderr.log
    worker.stdout.jsonl
    validation/...
  workspaces/...                 # Chỉ khi keep_workspaces=True.
```

`audit/*` là artifacts **cần bổ sung**, không phải files hiện đã có. Có thể dùng fewer files nếu vẫn giữ semantics/identity/integrity rõ; không bắt buộc mọi request phải tồn tại cả ở events lẫn archive như hai bản raw trùng nhau.

### 4.2. Effective manifest

Manifest phải phản ánh giá trị **sau config/env/CLI overrides và routing**, không chỉ copy JSON đầu vào:

| Nhóm | Nội dung bắt buộc |
|---|---|
| Identity | Schema version, run/case/relative IDs, SDK conversation ID nếu tạo được, start/end/status |
| Reference | Profile 50+bật reminders hoặc 100+tắt, frozen hashes, reference role models |
| Implementation | Adapter source hashes, dirty/untracked status, lock hash, SDK/dependency versions và SDK hashes đang dùng |
| Actual roles | Repair/compression actual SDK ID, prepared wire ID, auth mode, endpoint đã bỏ secret, active/inactive/shared model status |
| Request policy | Effective max output, `n`, temperature/effort/stop presence, advertised tools/hash, cache placement, prefill, retry/no-fallback controls |
| Agent-Diet | Effective mode/enabled/threshold/context/show_ctx/LZ4/ratio/reduction constants, tokenizer và special-token policy |
| SDK controls | Effective tools/context/hooks/stuck/concurrency/condenser/critic/budgets/watchdog theo D13 |
| Input | Config hash, issue source/encoding/hash, exact initial user prompt hash, source baseline identity, runtime/image identity |
| Validation | Original configured commands/evidence patterns/timeout/hash; isolation and submitted patch hash |
| Audit | Retention mode, hash definitions, records completeness, missing/corrupt artifacts, audit errors |
| Verification | Local/SDK-container/live levels riêng; supported/rejected domain; approved differences và evidence references |

Image tag có thể trỏ sang image khác theo thời gian; lưu resolved image ID/digest của run. Không ghi “image đã xác minh” chỉ từ tên tag trong input.

D07–D11 đã tách actual/reference model. Manifest phải giữ điều này: `inherit` actual chung không có nghĩa hai role policies giống nhau; inactive compressor không giả có wire request.

Viết manifest đầu run với trạng thái `running/not_verified`, cập nhật atomically sau finish/abort. Baseline/transport preflight failure trước conversation vẫn cần có run identity, stage/error và các metadata đã biết; fields chưa có giữ `null`/`not_created`.

### 4.3. Chống stale artifacts khi rerun

Mở rộng ownership/cleanup list của runner cho `protocol-manifest.json`, `audit/` và SDK archive của run trước. Chỉ quản lý artifacts của case output hiện tại; không xóa thư mục source hoặc output của case khác.

Nếu chọn giữ nhiều runs, dùng subdirectory/run ID rõ và chỉ có một pointer tới current run. Nếu giữ behavior replace hiện tại, xóa tất cả generated artifacts cũ trước baseline rồi tạo manifest run mới. Không trộn hai policies.

Test quan trọng: run A success có manifests/raw/archive, run B cùng output baseline fail; files còn lại phải thuộc run B hoặc bị đánh dấu archive run A riêng. Một manifest cũ không có run ID matching phải bị verifier reject.

## 5. Nối logical turns, requests, attempts và SDK events

### 5.1. Correlation fields

Đề xuất envelope chung:

```json
{
  "schema_version": 1,
  "run_id": "fixture-run-001",
  "case_id": "fixture-case",
  "producer": "worker",
  "producer_seq": 17,
  "type": "request_prepared",
  "role": "compression",
  "request_id": "fixture-request-004",
  "logical_turn": 3,
  "step_index": 0,
  "attempt": null,
  "sdk_event_ids": [],
  "payload_sha256": "HASH_COMPUTED_FROM_THE_DEFINED_CANONICAL_PAYLOAD"
}
```

Giá trị trong JSON là ví dụ schema, không phải evidence thật. Giữ event names hiện có hoặc thêm normalized audit records; tránh đổi raw contract messages chỉ để gắn IDs.

- `logical_turn` của repair request bắt đầu từ **1**; step được push từ response đó bắt đầu từ **0**.
- Initial user step ở **-1**, không là repair turn.
- Một repair response có nhiều tool calls vẫn một turn, observations giữ order/call IDs nguồn.
- Compression request gắn completed turn và target step; không tăng repair turn count.
- SDK scheduler step ở cap 51/101 không đồng nghĩa repair request mới.
- UUID SDK event khác step/request ID; lưu explicit mapping, không suy bằng cast/int hoặc lấy `event_ids=[str(idx)]` làm SDK event thật.

Trong exact, `diet_step_change.event_ids` hiện là `[str(idx)]` để audit step, không phải SDK event IDs. D14 nên thêm field `logical_step_ids` hoặc map riêng và ghi semantics rõ; không thay manager bằng event projector generic.

### 5.2. Request và retry attempt khác nhau

Exact transport hiện gọi telemetry `on_request()` một lần trước reference retry loop, có tối đa 12 `trae_transport_attempt` cho request đó, và một terminal telemetry result. Vì vậy:

```text
logical request count != HTTP transport attempt count
repair turn count != compression request count != SDK event count
```

Giữ counter definitions:

- `repair_requests`: số repair requests thực được chuẩn bị/gọi; fixture bình thường mỗi completed turn có một request.
- `compression_requests`: số requests analyzer LLM; gate skip không tự tạo request.
- `transport_attempts`: attempts bên trong reference retry, cùng request ID/body/model.
- `responses_received`: phân biệt response đã nhận và downstream parsing/telemetry error.
- `pending_requests`: started nhưng chưa có terminal record; usage chưa biết.

Để nối UUID `llm_call_started/finished` với raw transport record, tạo request context dùng chung hoặc truyền correlation metadata qua telemetry-only context. Không thêm `request_id` vào body gửi provider, prompt, tools, temperature hoặc cache settings.

Shared actual LLM phải phân role bằng request context, không bằng tên model. Kiểm tra role context qua SDK thread/async path bằng tests; không giả định mọi `ContextVar` tự truyền qua mọi thread bridge.

### 5.3. Causal order

Timestamps để đọc thời gian, không dùng làm bằng chứng order độc nhất giữa processes. Ghi `producer_seq` tăng đơn điệu theo producer cùng dependencies (`request_id`, response/tool IDs, completed turn).

Verifier dựng chuỗi:

```text
repair request → transport attempts → response → ordered tools/results
  → push logical step → normal-turn hook → gate/analysis/decision
  → next repair request hoặc terminal/cap snapshot
```

Successful `task_done` không có compression cuối; cap chỉ capture WIP, không có repair request mới; provider/parser fatal không có repair continuation. Order này kiểm tra theo source, không theo thứ tự archive preview xuất hiện trên UI.

## 6. Payload snapshots và hash definitions

### 6.1. Capture ở đúng boundary

| Boundary | Cần lưu/kiểm tra |
|---|---|
| Manager repair formatting | Exact system/messages/reminders/cache blocks và raw tools |
| Transport effective body | Prepared wire model, exact params, messages, tools và absence of extra fields |
| HTTP serializer | Body bytes thật trong HTTP mock; live nếu capture được qua instrumentation đã kiểm chứng |
| Compressor | Exact system variant, suffix, assistant prefill/cache, serialized context và reference params |
| Provider response | Parsed JSON envelope/raw arguments/finish reasons/usage trước analyzer decision |
| Replacement | Accepted bare content, stored original, reminder message và next actual repair history |

`trae_llm_request.payload` hiện là **client body object trước serializer**, không phải raw HTTP bytes. Không đặt tên field là `wire_bytes_hash` nếu chỉ hash dict. Chứng minh object→wire bằng HTTP mock tests D07–D08; ghi capture boundary trong manifest.

Không lấy output `_preview()` của callback làm raw response. SDK `MessageEvent` exact chủ yếu là archive assistant text; raw tool arguments/results/state đã nằm ở contract records. Giữ full source evidence tại đúng boundary đó.

### 6.2. Hai loại hashes

Định nghĩa và pin canonical JSON để hash structured payload:

```python
import hashlib
import json

def canonical_json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False,
    ).encode('utf-8')

def payload_hash(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

def byte_hash(data):
    return hashlib.sha256(data).hexdigest()
```

Canonicalization chỉ dành cho **bản audit copy**. Nó giữ thứ tự arrays, từng ký tự trong string content/tool arguments và distinction `None`/absent; không parse argument JSON string, strip whitespace, sort tool calls hoặc chuẩn hóa tags.

`payload_hash` của dict là structural evidence. `byte_hash` của file/wire/patch là byte evidence. Hai hash có thể khác dù biểu diễn cùng nội dung JSON; không so chéo hoặc gọi chúng cùng một loại.

Hash snapshot text/serialized step dùng UTF-8 nguyên văn. Nếu normalization path/model là cần để so sánh permitted differences, giữ raw hash trước rồi tạo comparison projection riêng, ghi chính xác fields được thay. Không `replace()` toàn bộ body: model/path có thể xuất hiện trong issue/code mà không phải field được phép đổi.

### 6.3. Payload lưu raw hay chỉ hash

- Raw mode: lưu full allowlisted message/response/tool/state data phục vụ byte/string comparison và original recovery audit.
- Hash mode: lưu hashes + lengths + schemas/config/decisions/correlation, không lưu full arbitrary content. Chỉ hash không đủ reconstruct source request nếu input/raw oracle đã mất.
- Redacted mode: đánh dấu artifact đã redact và reason; không chứng nhận full snapshot parity bằng bản đã thay text.

Fixtures local conformance dùng synthetic inputs để có thể giữ full evidence không credentials. Live hash-only run chỉ được kết luận những bất biến có đủ evidence, không tự nâng thành same coverage của raw local oracle.

## 7. Làm `keep_raw_events` và SDK persistence có hiệu lực

### 7.1. Retention contract

| Setting | Nội dung giữ | Nội dung không giữ |
|---|---|---|
| `keep_raw_events=True` | Full contract payload/response/tool/step data và SDK archive đã xử lý credential, manifests, hashes, metrics/status | Credential values và transport auth headers |
| `keep_raw_events=False` / `--discard-raw-events` | Effective config, tool schemas, hashes/lengths, decisions/counters, patch/result và đủ identity để audit mức được công bố | Full arbitrary payload/tool/history, SDK raw archive và originals không cần cho mức hash-only |

`patch.diff`/result là output chức năng vẫn giữ; flag raw-events không có nghĩa xóa submitted patch. Quy tắc retention áp dụng **mọi nơi ghi**: `trae_request`, `trae_response`, `trae_tool_results`, `trae_contract_state`, `diet_gate`, `diet_parser_result`, `diet_step_change`, compressor response, stdout và SDK archive. Không chỉ tắt callback rồi để events khác chứa full originals.

Terminal `contract-result.json` hiện gồm `steps`, `user_message`, originals; raw=false cần bản public terminal summary tối thiểu đủ runner consume `gen/patch/turns` và những fields D12 dùng kiểm tra. Working state in-memory của manager vẫn giữ originals cần D09; không strip internal state đang chạy để đáp ứng retention.

Không làm raw flag đổi payload hash, request order, seed, gates hoặc decisions. Programmatic config, CLI và worker serialized roundtrip phải cho cùng policy.

### 7.2. SDK persistence ngoài workspace

Đề xuất worker constructor khi raw mode bật, kết hợp controls D13 đang dùng:

```python
sdk_archive_root = output_dir / 'audit' / 'sdk'
conversation = TraeLocalConversation(
    agent=agent,
    workspace=workspace,
    callbacks=[event_logger],
    visualizer=None,
    max_iteration_per_run=openhands.scheduling_limit,
    stuck_detection=False,
    persistence_dir=sdk_archive_root,
    delete_on_close=False,
)
```

Đoạn trên là đề xuất bổ sung, chưa áp dụng. SDK 1.49.5 nhận persistence base directory rồi tạo conversation-specific layout; lấy path hiệu lực từ state thay vì đoán tên file. Kiểm tra archive còn sau `conversation.close()` và `spaces.cleanup()`.

Khi raw=false, không bật persistent SDK raw archive hoặc loại bỏ archive temporary do audit tạo theo policy rõ ràng. Không lưu persistent archive trong repair mount, `.tmp` của disposable workspace hoặc thư mục SDK global khó truy vết theo run.

Persistence phục vụ audit không tự cho phép auto-resume/retry sau crash. Mỗi run mới dùng conversation ID mới; không để SDK load lại tools/state của archive cũ. Nếu triển khai resume riêng, giữ run identity và thêm resume-segment identity, kiểm tra reference/effective contract trước khi tiếp tục. Crash giữa API response và checkpoint chưa có exactly-once guarantee theo D09–D11; D14 phải ghi uncertain/missing interval, không tự gọi lại rồi tuyên bố không có duplicate request.

SDK state có thể serialize agent/LLM config. Kiểm tra serializer thực tế và archive files để bảo đảm không có API key/token values trước khi giữ archive. Nếu stock persistence không đáp ứng credential exclusion, dùng reviewed persistence/export mechanism phù hợp hoặc chỉ giữ sanitized explicit archive, ghi mức evidence thực tế; không bật raw persistence rồi giả định `SecretStr` luôn đủ.

### 7.3. Paths chưa nối với CLI

`RunConfig.raw_events_path/response_path` được parse nhưng đường CLI/worker hiện quyết định artifacts từ case output. D14 cần chọn một contract rõ:

1. Nối override vào run/case-safe paths dưới output, ngoài repair workspace; batch có layout tránh các cases ghi đè nhau.
2. Hoặc reject fields chưa support và chỉ dùng canonical case-output paths.

Không để JSON ghi một destination nhưng output thực ở nơi khác. Validation path phải xảy ra trước persistence setup; không nhận output path trỏ vào workspace agent.

## 8. Events integrity, crash và audit không đổi trajectory

### 8.1. Ghi JSONL an toàn giữa processes

Với record rất lớn, thread lock không thay process lock. Chọn một giải pháp rồi test:

- Mỗi producer ghi JSONL riêng (`host`, `worker`), merge offline bằng identity/dependencies; hoặc
- Process-safe locking cho record writes vào shared file, bảo đảm toàn bộ record được ghi liền và file descriptor đóng ở mọi path.

Không suy ra total order chỉ bằng merge timestamps. Pin UTF-8, newline framing và schema; audit writer không dùng `default=str` để âm thầm chuyển unsupported structured values thành string rồi gọi đó là exact snapshot. Với schema không encode được, báo audit error rõ.

Có thể reuse [workflow/artifacts.py](../src/openhands_adapter/workflow/artifacts.py) cho atomic JSON artifacts. `write_json()` hiện dùng temp→replace; `write_text()` vẫn trực tiếp write. Atomic rename tránh readers thấy JSON dở, chưa phải guarantee fsync durability sau power loss; chỉ công bố durability đã triển khai/test.

### 8.2. Strict audit reader và completeness

Giữ tolerant `read_events()` cho telemetry phục hồi nếu cần, nhưng thêm strict reader/verifier cho conformance:

- Partial last line sau crash có thể bỏ qua có nhãn `truncated_tail/incomplete_evidence`.
- Invalid JSON ở giữa file phải là integrity failure, không silently skip.
- Duplicate record IDs cùng content được deduplicate có ghi nhận; duplicate cùng ID khác content là conflict.
- Producer sequence gaps, wrong run ID, missing required start/terminal records và absent artifacts được báo rõ.
- Unknown extra schema/version không được silently interpret thành pass.

Verifier không cần từ chối external result chỉ vì audit incomplete, nhưng phải tách `resolved` với `audit_complete/conformance_status`. Tái dựng metrics từ valid prefix không có nghĩa audit đã đầy đủ.

### 8.3. Audit không được điều khiển repair/nén

Không chạy model để giải thích log, chạy serializer/parser gốc lại trên manager sống hoặc tokenize raw payload để quyết định tiếp tục/dừng. Capture copies tại boundaries đã có; verifier/so sánh thực hiện offline.

Đặc biệt:

- Không thay input/ref profile/model để làm hash khớp.
- Không reanalyze target bị skip/reject chỉ vì thiếu audit event.
- Không flush compressor sau terminal/cap để hoàn thiện log.
- Không đưa audit summary/source evidence/provider capability probe vào repair history.
- Counter metrics audit không override operands/counters đã nghiệm thu D10.

Phân biệt errors:

| Lỗi | Cách xử lý |
|---|---|
| Provider/tool/compressor/source failure | Giữ đúng behavior D05–D11; propagate/skip theo source, không audit-recovery |
| Secondary audit serialization/write failure có thể cô lập | Giữ generation behavior, mark audit incomplete và lưu error nếu còn sink khả dụng |
| Infrastructure/persistence failure làm SDK không thể chạy | Ghi harness failure, không giả Trae terminal status hoặc conformance pass |
| Verifier phát hiện mismatch sau run | Báo failed conformance offline; không sửa patch/history hoặc gọi repair lại |

Đừng blanket catch algorithm exception cùng audit error. Test fault injection để bảo đảm audit-only lỗi không vô tình nuốt lỗi nguồn hoặc khiến successful API response bị gọi lại. Existing `emit()`/telemetry hooks có thể raise; cần xử lý audit boundary cụ thể, không chỉ viết cam kết trong manifest.

### 8.4. Chứng minh non-interference

Chạy cùng deterministic scripted source fixture với full audit và hash-only audit. So sánh repair/compressor requests, tools/observations/order, logical steps, skip/accept/error decisions, status và submitted patch. Loại audit metadata ra khỏi comparison bằng projection có allowlist, không bỏ bất kỳ agent-visible text nào.

Với random baseline, dùng fixture RNG controllable giống source oracle; không thêm global seed vào production algorithm để phục vụ D14. Với live model, không yêu cầu hai lần run cho cùng patch vì sampling/provider không deterministic; dùng local scripted comparison cho chứng minh audit không can thiệp.

## 9. Provider usage và compatibility metrics

Giữ ba nhóm dữ liệu riêng:

| Nhóm | Ý nghĩa và nguồn |
|---|---|
| `token_usage` | Provider-reported input/output/total/cache/reasoning qua SDK telemetry |
| `reference_diet_metrics` | Metrics/counters compatibility source, gồm analysis usage và erase operands D10 |
| Audit estimates | Context/reminder serialized size estimates, definition/encoding ghi rõ |

`token_tracking.response_tokens()` giữ unknown là `None`, zero hợp lệ là `0`, `known_tokens` là phần đã biết. Cache/reasoning là detail của input/output, không cộng lại thành billing total. Không đổi telemetry đúng hiện tại thành phép cộng cache legacy chỉ để trùng Trae metrics.

Exact gate dùng encoder **default special-token policy** trong `compat/trae_diet.py`; generic/audit estimate `diet.core.count_tokens()` dùng `disallowed_special=()`. Đây là hai domain khác nhau; đừng lấy `context_token_estimate` làm gate/acceptance operand.

D14 cần ghi/kiểm tra:

- Gate target serialized non-bypass/token count; threshold và LZ4 operands nếu bật.
- Acceptance serialized bypass target và bare parsed output; `old-new>=400 OR new<0.8*old`.
- Provider usage trước parser/reduction decision, skipped/rejected responses vẫn có usage theo source rules.
- `delete/random/lingua` không áp reduction gate của `ours`; random output metric là remaining token IDs theo D10.
- `compression_llm_calls` definition thực tế cùng request/attempt counts; không đồng nhất nó với số erase accepted.
- `reference_diet_metrics` snapshot không bị provider aggregate ghi đè; fail/pending run vẫn recover phần đã biết.

Code hiện `summarize_diet()` đã ưu tiên compatibility snapshot khi có; D14 giữ behavior này và mở rộng fixtures. README/result field descriptions phải tách exact/generic và báo đúng metric source, không gọi “tiết kiệm provider tokens” chỉ từ `erase_in_tokens-erase_out_tokens`.

## 10. Credentials và phân loại các khác biệt được phép

### 10.1. Chỉ lưu metadata cần thiết

Theo yêu cầu D14, **không lưu credential values**: API keys, OAuth/access/refresh tokens, cookies, Authorization headers hoặc signed credential-bearing URL. Có thể lưu tên env như `OPENROUTER_API_KEY`, auth mode/vendor và endpoint đã bỏ credential query/userinfo.

Dùng allowlist khi export config/SDK state/transport metadata. Exception strings, request body/tool stdout hoặc source input có thể chứa secret text; retention sanitizer xử lý **bản audit copy**, không mutate request/observation thực. Đánh dấu redaction trong evidence; không hash redacted text rồi gọi là hash của original payload.

Tránh đọc/dump toàn bộ environment để tìm credential. D14 local tests dùng fake sentinel secrets rồi scan bundle đảm bảo chúng không xuất hiện ở manifest/events/SDK archive/stdout/errors. Nếu không có content record đủ kiểm chứng do redaction, report đúng coverage còn lại.

### 10.2. Allowed-difference ledger

Mọi sai khác phải có category, scope, lý do, evidence và tác động trên agent-visible contract. Chỉ bốn nhóm đã được người dùng cho phép trong `difference.md`:

| Category | Ví dụ hợp lệ | Ranh giới |
|---|---|---|
| `harness` | SDK IDs, process/container plumbing, output/audit format, path routing | Không tự đổi tools/prompts/turns/order/stop/retries/nén |
| `model` | Actual repair/compressor model IDs, routing/auth tương thích | Reference branch/prefill/cap/settings không tự đổi theo alias mới |
| `input` | PHP/fmtlib source/path/issue/build environment đã khai báo | Không thêm repair constraints hoặc gold/evaluator feedback |
| `external_validation` | Commands, evidence patterns, verdict taxonomy và validation timeout | Chấm sau generation trên copy riêng; không feedback/retry repair |

Fields không thuộc bốn nhóm mà khác source phải failed conformance hoặc unsupported domain, không thêm category “SDK default” để cho qua.

Ví dụ: UUID event khác là harness; SDK chèn SOUL prompt là mismatch D01/D13. `.phpt` policy mở rộng là evaluator adaptation nếu lưu riêng, nhưng không là exact D12 filtered patch. Subscription thiếu prefill là protocol incompatibility, không được miễn vì đã đổi model.

## 11. Conformance verifier và mapping D01–D14

### 11.1. Verifier offline

Đề xuất module mới `src/openhands_adapter/compat/audit.py` và entry point/script `analysis/d14_checks/verify_audit.py`. Đây là tên đề xuất, **chưa tồn tại** và chưa có lệnh verifier chạy được trong checkout.

Verifier chỉ đọc bundle/source fixtures, không login, gọi provider, execute tool, apply patch lên source hoặc resume SDK conversation. Output cần có:

```text
run/case identity + manifest/schema/hash checks
  → evidence completeness/integrity
  → field/string/byte comparisons theo contract fixture
  → timing/turn/decision/patch/validation boundary checks
  → allowed-difference ledger
  → per-Dxx status + local/container/live scope + limitations
```

Mismatch record ghi contract ID, request/turn/step, expected/actual artifact path và first differing field/character offset. Nội dung diff chỉ dùng dữ liệu đã qua retention policy, không dump secrets để giải thích failure.

Fixture conformance dùng source oracle với cùng scripted responses/usage/tool results để kiểm tra quyết định adapter. Audit của live run không có oracle cho các model responses khác nhau; kiểm tra bất biến cấu trúc/timing/operands theo dữ liệu ghi, và ghi provider semantics evidence riêng. Không yêu cầu actual models tái tạo lời giải của Trae.

### 11.2. Mapping bộ tests hiện có

| Contract | Modules hiện có để lấy evidence nền |
|---|---|
| D01–D02 prompts/input | `test_trae_contract_prompts`, `test_input_loader`, `test_cli` |
| D03–D05 tools/container/runtime | `test_trae_contract_tools`, `test_trae_contract_runtime`, `test_trae_contract_container` |
| D06 turns/completion | `test_trae_contract_turns`, `test_trae_contract_sdk`, `test_trae_contract_workflow` |
| D07 request/retry policy | `test_trae_contract_llm_policy`, `test_trae_contract_llm_retry` |
| D08 compressor payload | `test_trae_contract_compressor_payload` |
| D09 serialization | `test_trae_contract_diet_serialization` |
| D10 gates/acceptance/token operands | `test_trae_contract_diet_algorithm` |
| D11 parser/fatal behavior | `test_trae_contract_diet_parser`, `test_trae_contract_diet_integration` |
| D12 nền patch/submission | `test_patch`, `test_workspace`, `test_trae_contract_workflow`, `test_trae_contract_turns` |
| D13 nền SDK/models/controls | `test_openhands_runtime`, `test_trae_contract_sdk`, `test_trae_contract_llm_policy` |
| D14 nền logs/metrics/cleanup | `test_events`, `test_token_tracking`, `test_token_counting`, `test_output_cleanup` |

Mapping “có test nền” không đồng nghĩa Dxx đã hoàn thành. Coverage D12–D13 bổ sung theo tài liệu trước và D14-specific fixtures ở mục 12 phải có kết quả thực tế.

Mở rộng conformance manifest với test names, fixture IDs, evidence paths/hashes, supported/rejected domain và run date. Đừng sửa `status` thành verified chỉ vì test file tồn tại. Những historical reports vẫn có giá trị cho đợt đó; report mới phải ghi tests đã chạy lại trên implementation mới.

## 12. Ma trận tests D14 cần bổ sung

Có thể thêm `tests/test_trae_contract_audit.py`, `tests/test_audit_integrity.py` và `tests/test_audit_retention.py`. Các tên này là đề xuất, chưa phải modules có sẵn.

| ID | Fixture | Expected behavior |
|---|---|---|
| A14-01 | Effective JSON/env/CLI→worker config | Manifest đúng settings/model/profile cuối, không chỉ input defaults |
| A14-02 | Exact payload dài >40k, Unicode/newlines/raw arguments | Full snapshots/hash đúng, preview truncation không mất source evidence |
| A14-03 | Raw true/false cùng deterministic repair+compression | Requests/steps/decisions/status/patch identical; retention khác đúng policy |
| A14-04 | SDK persistence + close + workspace cleanup | Archive ngoài mount còn khi raw=true, không còn full raw khi false |
| A14-05 | Shared LLM explicit inherit, threads/async | Correlation/role chính xác, không tính compression thành repair |
| A14-06 | Retry 12 attempts, success/exhaustion/telemetry error | Một logical request nhiều attempts; model/body giữ nguyên; không extra generation |
| A14-07 | Batch multi-tool, normal turn, done và cả cap profiles | Mapping/order/count đúng, không terminal compressor/extra repair |
| A14-08 | Gate skip, LZ4 reject, usage-none/parser skip, reduction reject | Mỗi turn/target có decision/reason đúng, không missing event bị coi accepted |
| A14-09 | Accepted empty/whitespace/nested originals/show_ctx=False | Before/after/storage/next actual request đúng oracle từng ký tự |
| A14-10 | Fatal provider/parser/tokenizer/lingua | Error/cause preserved; audit không nuốt lỗi, không repair tiếp |
| A14-11 | Unknown/zero/cache/reasoning usage + pending request | Provider totals/known_tokens và reference metrics đúng domain |
| A14-12 | Accepted snapshot, workspace đổi sau stop, external fail | Patch hashes nhất quán; status khác verdict; không feedback repair |
| A14-13 | Concurrent writers với large records | JSONL không interleave; sequences/correlation đủ verify |
| A14-14 | Partial tail và corrupt middle | Tail được báo incomplete; corrupt middle failed integrity |
| A14-15 | Duplicate ID same/different content, sequence gap | Dedup có ghi nhận hoặc conflict/missing evidence rõ |
| A14-16 | Rerun success→baseline fail→worker fail | Không stale manifest/archive/events hoặc cross-run evidence |
| A14-17 | Audit write/serialization failure | Audit incomplete riêng; không thêm LLM call, không che algorithm error |
| A14-18 | Sentinel credentials ở config/header/error/SDK state/content | Retention export không lộ values; payload thực không bị sanitizer sửa |
| A14-19 | Tampered patch/config/payload/schema/reference/SDK hash | Verifier fail với reason và contract ID đúng |
| A14-20 | Allowed differences projection | Chỉ whitelist fields/category hợp lệ; prompt/tool/stop change fail |
| A14-21 | Hash-only hoặc redacted evidence thiếu reconstruct data | Coverage limitations/incomplete status, không giả full parity |
| A14-22 | Full audit bundle PHP/fmtlib scripted containers | SDK/tools/diet/patch/evaluator boundary có complete evidence |
| A14-23 | New run cạnh archive cũ, checkpoint/resume nếu support | Không implicit resume/discover tools cũ; resumed lineage rõ, uncertain crash interval không giả exactly-once |

Không viết test chỉ mirror schema implementation. Fixtures cần chứng minh failure mà audit hiện dễ che, hoặc đối chiếu source decisions/boundaries có ảnh hưởng kết luận.

## 13. Thứ tự triển khai và các lệnh kiểm tra

### 13.1. Triển khai theo dependencies

1. Kiểm tra D01–D11 evidence và D12–D13 implementation/domain; giữ các mục chưa nghiệm thu là pending.
2. Định nghĩa schema/identity/hash/retention/usage/attempt semantics trước khi thêm event fields.
3. Nối run/request context, manifest hiệu lực và capture boundaries; không inject metadata vào wire body.
4. Nối raw flag/persistence/paths, xử lý credential export và stale artifacts.
5. Thêm integrity reader/process-safe write và failure/completeness reporting.
6. Hoàn thiện gate/parser/reject/error/cap mapping, provider/reference metrics và patch lineage.
7. Viết offline verifier và source fixtures; chứng minh audit non-interference.
8. Chạy focused/full regressions, container fixtures rồi provider/live runs khi đủ điều kiện.
9. Viết `analysis/implementation_d14.md`, update README và conformance manifest theo kết quả thực tế.

Không bắt buộc làm lại toàn D12–D13 trong D14; phát hiện lỗi thì ghi đúng contract và sửa ở module liên quan trước khi chứng nhận toàn chuỗi.

### 13.2. Focused modules đang có

```bash
cd /home/anh_duc/projects/APR/Agent-Diet
PYTHONPATH=src:tests LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
  .venv-openhands/bin/python -m unittest \
  test_events test_token_tracking test_token_counting test_output_cleanup \
  test_trae_contract_sdk test_trae_contract_workflow test_trae_contract_turns \
  test_trae_contract_llm_policy test_trae_contract_llm_retry \
  test_trae_contract_compressor_payload test_trae_contract_diet_integration -v
```

Sau khi tạo D14 test files, chạy modules mới theo tên thực tế. Không đưa tên đề xuất vào command hiện có rồi coi suite skipped/missing module là success.

### 13.3. Full regression

```bash
PYTHONPATH=src:tests LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
  .venv-openhands/bin/python -m unittest discover -s tests -v

.venv-openhands/bin/python -m pip check
git diff --check
```

Ghi test counts pass/fail/skip thực tế. Giữ D01–D11 và generic suites pass, không copy con số 236 của đợt D09–D11 thành kết quả D14.

Báo cáo trước có SDK sandbox `socketpair` failure; nếu lặp lại, ghi infrastructure failure và xử lý môi trường thực thi phù hợp, không thêm timer/stop/prompt workaround vào algorithm để test kết thúc.

### 13.4. SDK/container integration

```bash
docker info
docker image inspect php-src/defect4c:latest
docker image inspect swebench/sweb.eval.x86_64.fmtlib_1776_fmt-3901:latest

AGENTDIET_CONTAINER_INTEGRATION=1 PYTHONPATH=src:tests \
  LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 \
  .venv-openhands/bin/python -m unittest test_trae_contract_container -v
```

Images phải có sẵn; adapter không tự pull/build. Bổ sung A14-22 vào container fixtures hoặc module integration riêng sau khi implement audit bundle. Tests scripted images không thay live model benchmark.

Coverage phải có actual compression với defaults `ours/500/ctx_before=1/ctx_after=2/show_ctx=True/use_lz4=False`, cùng tests/untracked patch filtering và validation copy. Run disabled diet hoặc threshold quá cao không đủ chứng minh full Agent-Diet path.

### 13.5. Kiểm tra bundle offline sau triển khai

Verifier đề xuất chưa tồn tại; khi tạo, ghi command và flags chính xác vào README/report. Tối thiểu có option chỉ định case output, evidence level, strict integrity và expected reference profile/domain. Missing raw ở hash-only run phải cho report coverage rõ, không silently pass strict full-snapshot mode.

Tạo tampered bundle copy trong test temporary directory, không sửa evidence gốc. Verifier reject thay đổi một ký tự payload/patch hoặc manifest ID/profile/hash; intact bundle phải pass ở đúng level được fixture chứng minh.

## 14. Live provider và prepared PHP/fmtlib cases

Giữ ranh giới hiện có: exact subscription/Responses vẫn bị D07 validator reject; D14 không bỏ assistant prefill/cache/cap hoặc đổi sang generic để gọi là exact parity. Dùng compatible API-key Chat Completions endpoint cho exact live nghiệm thu.

[d09_d11_config.openrouter.json](d09_d11_config.openrouter.json) là template actual IDs từ đợt trước, capabilities rỗng và chưa được live chứng nhận. Không suy availability/capability từ tên model hoặc local mock. Với default reference roles cần xác nhận `max_tokens,n,temperature,tools,reasoning_effort,cache_control,assistant_prefill`; compressor reference non-GPT-5 cần thêm `stop`.

Sau khi có endpoint/model/credential và verified config, mẫu chạy từng case:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --config /absolute/path/to/verified-run-config.json \
  --model verified-repair-model-id \
  --input /absolute/path/to/prepared-inputs \
  --case actual-php-case-id \
  --agent-timeout 0 --keep-workspaces \
  --output d14_php

PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --config /absolute/path/to/verified-run-config.json \
  --model verified-repair-model-id \
  --input /absolute/path/to/prepared-inputs \
  --case actual-fmtlib-case-id \
  --agent-timeout 0 --keep-workspaces \
  --output d14_fmtlib
```

Placeholders phải thay bằng paths/selectors/models đã xác minh. CLI yêu cầu `--model` dù JSON có model; compressor actual/reference fields ở JSON vẫn giữ rõ. Raw audit dùng `agentdiet.keep_raw_events=true`; hash-only dùng `--discard-raw-events` sau khi flag đã được nối đúng D14.

Runner không tự load `.env`; đặt credential vào environment theo config mà không ghi values vào bundle. Output riêng mỗi run tránh mất evidence; nếu cần rerun cùng output, nghiệm thu cleanup/identity policy trước.

Sau run, kiểm tra:

1. Manifest/profile/actual-reference models/controls đúng, endpoint semantics có evidence riêng.
2. Requests, attempts, tool results, normal-turn gates/decisions và next actual payload nối được bằng IDs.
3. SDK archive ngoài repair còn sau cleanup khi raw=true, không có credentials/stale run data.
4. Raw/filter/submitted/apply patch lineage đúng D12; validation commands/config/hash đúng input ban đầu.
5. Provider usage/reference metrics/unknown pending states đúng definitions.
6. Offline report có completeness/domain/allowed differences/local-vs-live coverage chính xác.

CLI exit 1 có thể chỉ là bug unresolved sau run hợp lệ. D14 report phải phân biệt unresolved bug với adapter failure, provider incompatibility, generation fatal, harness abort và audit incomplete.

## 15. Điều kiện hoàn thành D14 và toàn bộ D01–D14

**D14 hoàn thành ở local/SDK scope** khi:

- Effective config, roles, input mapping, tool schemas, payload hashes/snapshots, request/turn/step lineage và submitted patch được lưu đủ cho domain đã chọn.
- Raw flag, persistence/paths và rerun cleanup có hiệu lực; archive ngoài repair, không credentials.
- Event corruption/missing/stale/tampered evidence được phát hiện; failure/pending runs không bị chứng nhận pass.
- Metrics dùng đúng source/operands, provider billing tách compatibility metrics, attempts không bị nhầm với logical requests.
- Audit non-interference pass với source fixtures; không thêm prompt/tools/stop/nén/retry/fallback.
- Verifier và mapping D01–D14 có evidence thực tế cùng supported/rejected domains, SDK/dependency/source hashes.

**Nghiệm thu toàn chuỗi exact D01–D14** chỉ được công bố sau khi D12–D13 và tất cả source invariants tương ứng đã pass. Nếu live provider còn thiếu credential/model/capability evidence, ghi rõ “local source + SDK/container verified, live provider chưa nghiệm thu”; không dùng status toàn cục verified che phần đó.

**Nghiệm thu chạy thật** cần thêm prepared PHP và fmtlib run artifacts có complete audit, external tests chạy trên copy riêng và provider semantics đã xác minh. Không cần đổi evaluator sang SWE-bench/Multi-SWE hoặc sửa taxonomy PHP/fmtlib để đạt D14.

Báo cáo `implementation_d14.md` cần nêu behavior cuối, files đã sửa, tests/logs/counts, fixture IDs, bundle/verifier evidence, versions/hashes, allowed differences và giới hạn còn lại. README phải thống nhất exact/generic, raw retention/persistence, request-vs-attempt metrics và mức verification hiện tại.

Trạng thái của **lần viết tài liệu này**: đã đọc `difference.md`, quy trình D12–D13 và code audit/telemetry/persistence hiện có; chỉ viết `fix_d14.md`. Chưa triển khai D14, chưa chạy conformance/container/live model PHP/fmtlib cho D14, chưa chứng nhận D12–D14 hoàn thành.
