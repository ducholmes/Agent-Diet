# Đề xuất adapt subscription cho Trae contract

## Kết luận

Có thể xây dựng adapter để subscription chạy **luồng điều khiển Trae và thuật toán Agent Diet giống reference**, thay vì generic. Tuy nhiên, chưa có đủ bằng chứng để khẳng định subscription có thể đạt **exact contract toàn bộ**, bao gồm semantics của request tới model.

Đề xuất triển khai một chế độ tương thích có giới hạn, kiểm chứng từng ràng buộc, và chỉ cho phép exact khi mọi ràng buộc bắt buộc được chứng minh. Không bỏ guard subscription rồi mặc nhiên coi Responses tương đương Chat Completions.

Phần thiết kế dưới đây là kế hoạch ban đầu. Implementation đã được bổ sung; xem mục trạng thái triển khai cuối file để biết kiểm chứng thực tế và giới hạn còn lại.

## Hiện trạng trong repository

- `src/openhands_adapter/config.py`: mặc định `reference_profile=trae_verified`; `exact_trae` được suy ra từ `reference_profile != generic`, độc lập với auth.
- `compat/trae_llm_policy.py`: exact subscription bị chặn trước login/container. Policy mặc định yêu cầu `max_tokens=8192`, `n=1`, `temperature=0.0`; compression thêm `stop=</step>`. Nhánh reference có chuỗi `gpt-5-` bỏ temperature/stop và dùng `reasoning_effort=low`. Cả hai nhánh vẫn yêu cầu cache semantics và compression vẫn yêu cầu assistant prefill.
- `openhands/trae_transport.py`: đã có chuyển history/tools sang Responses và chuyển response về envelope nội bộ. Mapping giữ chuỗi tool arguments, call ID và đặt `strict=False`, nhưng đây chưa phải bằng chứng tương đương toàn bộ semantics.
- `SDKRawTransport.__call__`: có policy thì luôn gọi `_exact_chat`; subscription chỉ đi `_subscription` khi không có policy. Vì vậy sửa validation thôi chưa đủ.
- `openhands/compressor.py`: exact compressor yêu cầu assistant prefill và transport cùng role policy.
- `compat/audit.py`: generic được báo `unsupported` về Trae conformance. Verifier hiện dựa vào frozen reference và request Chat Completions, cần mở rộng có chủ đích cho Responses.
- `tests/test_trae_contract_subscription.py`: đã có kiểm tra serialization/history, subscription credentials và exact rejection; có thể tái sử dụng fixtures khi triển khai.

## Hai mức tương thích cần phân biệt

1. **Trae workflow với subscription**: giữ prompt reference, tools, history, execution/stop rules, 50/100 repair turns, reminders, retry và Diet algorithm; công khai các khác biệt ở transport/model request.
2. **Exact toàn bộ**: thêm bằng chứng rằng mọi field và semantics bắt buộc của reference được endpoint subscription giữ nguyên hoặc có phép chuyển tương đương được kiểm chứng.

Mức 1 có thể là mục tiêu triển khai trước. Nếu endpoint không hỗ trợ một semantics bắt buộc thì mức 2 phải tiếp tục bị chặn. Kết quả mức 1 không được xuất nhãn exact hay gộp với kết quả exact trong đánh giá.

## Các điểm cần chứng minh

| Ràng buộc | Hướng adapt | Điều kiện để công nhận exact |
| --- | --- | --- |
| Prompt, history, tools | Dùng history Trae; system chuyển sang instructions; tool calls/results giữ ID, argument bytes, thứ tự; không để SDK sửa schema | So sánh với oracle cả text, ordering, nullable content và multi-tool history; đánh giá semantics của phép chuyển system → instructions |
| `max_tokens=8192` | Xem xét mapping sang output limit của Responses | Chứng minh endpoint nhận và thực thi cùng semantics, bao gồm reasoning tokens; chỉ đổi tên field chưa đủ |
| Sampling và `n=1` | Áp policy theo reference model, độc lập actual model | Endpoint hỗ trợ/giữ semantics tương ứng; output đơn không tự chứng minh `n=1` tương đương |
| `reasoning_effort=low` | Truyền theo policy của từng role | Kiểm tra request cuối sau serializer và khả năng endpoint/model; không suy ra từ tên model |
| `cache_control` | Bảo toàn vị trí và semantics cache của reference nếu có mapping được hỗ trợ | Automatic caching hoặc một cache key không tự tương đương cache-control blocks |
| Compression `stop=</step>` | Truyền server-side stop nếu được hỗ trợ | Cắt text ở client không tương đương server stop về generation và usage |
| Assistant prefill | Kiểm tra endpoint có continuation từ prefix assistant thật sự hay không | Một assistant message trong history hoặc yêu cầu model lặp prefix không tự tương đương prefill |
| Response/usage | Drain SSE, normalize text/tool calls/status/usage | Không mất refusal, incomplete reason, output ordering hoặc token fields; missing usage phải xử lý theo reference |
| Retry | Dùng 12 total attempts và backoff reference, gồm sleep sau lần lỗi cuối | Tắt retry ngầm ở SDK/LiteLLM; chỉ retry phần call/normalization thuộc reference, không retry parser/telemetry |

Không nên đổi `repair_reference_model` hoặc `compressor_reference_model` chỉ để né unsupported fields: đó là chọn một contract reference khác. Nếu cần đánh giá nhánh GPT, phải công khai lựa chọn đó và vẫn kiểm tra các ràng buộc còn lại.

## Thiết kế đề xuất

### 1. Tách workflow profile khỏi transport conformance

Giữ `reference_profile` để chọn `generic`, `trae_verified`, `trae_multiswe`. Thêm cấu hình đề xuất `--transport-conformance exact|adapted`, mặc định `exact` để giữ hành vi hiện tại.

Khi chọn Trae + `adapted`, vẫn chạy workflow Trae nhưng không khẳng định transport exact. Cần thay cách dùng `exact_trae`: thêm thuộc tính như `uses_trae_workflow` và `requires_exact_transport`, rà soát mọi nhánh để workflow, prompts, container, Diet, compressor không vô tình rơi về generic.

CLI minh họa sau khi implementation hoàn tất:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --input ../defects4c/out_tmp_dirs/debugging_framework/php/inputs \
  --case CVE-2016-9936__b2af4e886872 CVE-2016-10159__ca46d0acbce5 \
         CVE-2016-10162__8d2539fa0faf CVE-2017-7272__bab0b99f376d \
  --output inspect-subscription-trae-adapted \
  --model gpt-5.6-sol \
  --auth subscription \
  --reference-profile trae_verified \
  --transport-conformance adapted \
  --diet-mode ours \
  --compressor-model gpt-5.6-luna
```

Flag `--transport-conformance` đã được triển khai. Tên output làm rõ đây là Trae adapted, tránh nhầm với exact. `--diet-mode ours` là nén Diet; tên thư mục `inspect-skip` không làm mode chuyển thành skip.

### 2. Thêm role-aware subscription transport

- Dispatch theo cả auth và role policy; xây nhánh Responses dành cho Trae, không tái sử dụng `_exact_chat` với OAuth.
- Dùng SDK cho OAuth refresh/routing/headers, giữ request serialization tại một boundary có thể audit. Kiểm tra payload cuối sau LiteLLM/SDK, không chỉ payload trước conversion.
- Tạo capability report riêng cho repair và compression, gắn với endpoint, actual model, phiên bản SDK/serializer và thời điểm kiểm chứng. CLI capability declaration đơn thuần không đủ làm bằng chứng endpoint thực thi semantics.
- Exact thiếu capability thì fail trước repair. Adapted ghi rõ supported/unsupported/unverified và mọi phép biến đổi đã dùng; không silently drop params.
- Reuse retry reference, giữ SSE terminal handling và correlation giữa request, response, usage và Diet decision.

### 3. Adapter cho compressor

Giữ selection, context windows, threshold, parser và acceptance/rejection của exact Diet khi có thể. Tách cách tạo request để compression role dùng Responses.

Nếu prefill/stop không khả dụng, adapted có thể yêu cầu model trả wrapper `<step id="...">...</step>` rồi parse/validate theo một protocol được khai báo riêng. Cần ghi rõ prompt và protocol đã đổi; không gọi đó là exact compression. Không sửa prefix hoặc cắt output rồi đánh dấu exact pass.

Nếu yêu cầu của người dùng là exact toàn bộ với `ours`, chưa chứng minh được prefill hoặc semantics khác thì phải chặn trước run. Có thể dùng `skip` để kiểm chứng repair trước, nhưng không tự đổi `ours` thành `skip`.

### 4. Audit và manifest

Manifest ghi workflow profile, transport conformance, protocol, reference/actual model cho từng role, capability evidence, request hashes và danh sách deviations. Không lưu OAuth token/headers bí mật vào artifacts.

Verifier cần phân biệt integrity pass, workflow conformance và transport conformance. Exact chỉ pass khi đủ bằng chứng bắt buộc; adapted báo partial/unsupported cho phần chưa tương đương. Không tái sử dụng nhãn `SDK-auth/OpenAI-chat-raw` hoặc verifier Chat Completions cho Responses.

## Lộ trình và kiểm thử

1. **Capability audit trước:** đọc serializer/SDK đã pin; kiểm tra endpoint/model được phép sử dụng bằng probes nhỏ, tách repair và compression. Ghi phân biệt field bị reject, bị drop, được nhận và được thực thi. Không chạy bốn CVE dài trước khi xác định blockers.
2. **Workflow adapted:** tách workflow/conformance, triển khai role-aware Responses, giữ rejection của exact chưa đủ bằng chứng. Kiểm chứng repair với Diet skip trước, sau đó mới triển khai ours.
3. **Differential fixtures:** replay deterministic responses qua frozen Trae oracle và adapter; so prompt/tools/history, reminders, turn limits, stop rules, retry count và Diet decisions. Không dùng việc hai model sinh cùng patch làm bằng chứng contract exact.
4. **Transport tests:** payload sau serialization, credentials refresh không đổi payload, hidden retries, SSE thiếu terminal, partial output, tool calls, refusal, incomplete, malformed/missing usage. Parser/telemetry errors không gây gọi model lại.
5. **Compression tests:** prefix/wrapper, stop, wrong step ID, nested/multiple steps, incomplete output và acceptance decision; xác nhận deviations của adapted được ghi vào manifest.
6. **Verifier regression:** giữ API-key exact và generic hiện tại; adapted không thể được nhận nhầm là exact. Sau khi fixtures pass, smoke một CVE rồi chạy đủ bốn case với output mới.
7. **Mở exact có điều kiện:** chỉ khi từng role đạt toàn bộ capability evidence và verifier đã kiểm chứng mapping; nếu blocker thuộc endpoint không giải quyết được thì giữ adapted và ghi rõ exact subscription chưa hỗ trợ.

## Tiêu chí hoàn thành

- Subscription chạy được workflow Trae đã chọn với deviations rõ ràng, không silently fallback generic.
- `ours` dùng đúng Diet algorithm; khác biệt compression protocol được công khai và kiểm thử.
- Không làm giảm guard/verifier của exact hiện tại.
- Có artifacts để phân biệt lỗi adapter với hạn chế endpoint và so sánh workflow với frozen reference.
- Chỉ tuyên bố exact toàn bộ khi đã chứng minh cả workflow và transport; nếu không, kết luận dừng ở Trae workflow adapted.

## Quy trình thực hiện cụ thể

Các bước dưới đây mô tả quy trình implementation; trạng thái thực hiện được ghi ở cuối file. Chạy lệnh từ thư mục `Agent-Diet`. Các script/probe và flag mới phải được tạo ở bước tương ứng trước khi sử dụng.

### Bước 0 — Chốt baseline và phạm vi

Mục tiêu phiên bản đầu: subscription + `trae_verified` + `adapted` + `ours`, giữ actual models `gpt-5.6-sol` và `gpt-5.6-luna`. Giữ reference model hiện tại; không đổi reference để làm tests dễ pass.

Ghi git revision, phiên bản dependency và kết quả baseline vào `analysis/subscription_checks/`. Trước khi sửa, chạy:

```bash
PYTHONPATH=src:tests .venv-openhands/bin/python -m unittest discover \
  -s tests -p 'test_trae_contract_*.py'
```

Đọc `requirements-openhands.lock` và serializer của SDK đã cài, tập trung vào `llm/auth/openai.py`, `llm/options/responses_options.py`, `_build_responses_call_kwargs` và nhánh Responses của LiteLLM. Repository đang pin OpenHands SDK `1.49.5`; không nâng dependency trong cùng thay đổi này.

**Đầu ra:** baseline log, danh sách serializer hooks và mapping fields thực tế. Baseline lỗi phải được phân loại trước khi dùng kết quả regression.

### Bước 1 — Làm capability probe độc lập với agent

Tạo script đề xuất `scripts/probe_subscription_contract.py`, gọi transport bằng SDK đang pin và auth hiện có, không khởi động Docker hoặc chạy CVE. Script có hai chế độ: mock để kiểm tra request cuối tại HTTP boundary; live để kiểm tra endpoint. Live là phép kiểm chứng riêng, không nằm trong unit tests mặc định.

Quy trình mỗi role/model:

1. Gửi request text tối thiểu làm control; lưu payload đã loại bỏ secrets, status, usage và SDK/model identity.
2. Thử từng field riêng: output cap, temperature, reasoning effort, n, stop, cache metadata và assistant prefix. Chỉ thử fields thuộc policy role đang chọn; ghi rõ các field không áp dụng.
3. Với output cap, dùng giới hạn nhỏ và prompt yêu cầu output dài, kiểm tra terminal reason/usage. Đây chỉ là smoke về enforcement, chưa chứng minh token-budget semantics tương đương reference.
4. Với prefill, kiểm tra hỗ trợ continuation trong serialization và endpoint; việc model sinh câu đúng prefix chỉ là hành vi quan sát, không đủ chứng minh continuation.
5. Với sampling/cache/stop, kết hợp tài liệu endpoint hoặc bằng chứng implementation với wire capture. HTTP 200 chỉ chứng minh request được nhận, không chứng minh field được thực thi. Nếu không có bằng chứng chắc chắn, giữ `unverified`.
6. Thử repair tools bằng tool schema reference, raw JSON arguments và tool result round-trip; thử compression không có tools.

Report JSON đề xuất có các trường: `role`, `actual_model`, `reference_model`, `sdk_versions`, `endpoint_identity`, `checked_at`, `capabilities`, `evidence`, `deviations`. Mỗi capability có `status=supported|unsupported|unverified|not_applicable`, `serialization_preserved`, `endpoint_accepted`, `semantic_evidence` và lý do. Không dùng một boolean chung cho toàn model.

**Điều kiện chuyển bước:** đã biết request hợp lệ tối thiểu của cả repair và compression. Nếu exact còn blocker, tiếp tục adapted và giữ blocker trong report; không cần dừng toàn bộ implementation.

### Bước 2 — Tách config và validation trước khi sửa transport

Sửa theo thứ tự:

| File/nhóm | Thay đổi cụ thể |
| --- | --- |
| `config.py`, `cli.py` | Thêm `transport_conformance`; đọc từ CLI/mapping/env theo convention hiện tại; mặc định exact cho Trae. Generic giữ behavior hiện tại và từ chối explicit lựa chọn conformance không phù hợp |
| `config.py` | Thêm `uses_trae_workflow = reference_profile != generic`; `requires_exact_transport = uses_trae_workflow and transport_conformance == exact`. Giữ thuộc tính cũ trong giai đoạn migration nếu cần, nhưng không dùng nó quyết định cả hai chiều |
| `compat/trae_llm_policy.py` | Tách dựng role policy khỏi kiểm tra transport. Adapted vẫn nhận repair/compression policies; guard exact subscription vẫn giữ cho tới khi có validation bằng evidence |
| `openhands/agent.py`, `worker.py` | Chọn `TraeContractAgent`, `TraeLocalConversation`, tools, turn scheduling, stuck detection và callbacks theo workflow; chọn transport/labels theo auth và conformance |
| `cli.py`, `openhands/process.py`, `container.py`, `prompts.py`, `workflow/runner.py` | Giữ prompt restrictions, Docker/source exec, patch capture, timeout và stop rules theo workflow Trae. Không bỏ ràng buộc workflow chỉ vì transport adapted |
| `diet/condenser.py`, `compat/trae_diet.py` | Chọn reference Diet algorithm theo workflow; phân biệt algorithm với compression request protocol |

Rà soát bằng `rg -n 'exact_trae|reference_profile' src/openhands_adapter`. Phân loại từng call site là workflow, transport hoặc audit rồi sửa từng nhóm; tránh replace toàn cục.

**Tests bắt buộc:** ma trận generic/API-key, generic/subscription, Trae/API-key exact, Trae/subscription exact rejected, Trae/subscription adapted. Kiểm tra lỗi xảy ra trước login/container khi cấu hình không hợp lệ.

### Bước 3 — Implement transport repair

Đề xuất dispatch:

```python
if not uses_trae_workflow:
    return existing_generic_path(...)
if llm.is_subscription:
    return trae_responses_path(policy, conformance, ...)
return existing_exact_chat_path(...)
```

Phiên bản đầu chỉ mở `trae_responses_path` cho adapted. API-key + adapted có thể từ chối rõ ràng nếu chưa có nhu cầu hỗ trợ; không cho nó âm thầm chạy một nhánh chưa kiểm thử.

Tái sử dụng `responses_payload`, `_drain_response` và `responses_answer`, nhưng bổ sung kiểm tra message grouping/order, usage và terminal errors bằng fixtures. Xây request từ role policy cộng capability report: mọi field bị omit/convert đều tạo deviation, không để SDK tự drop mà không audit. Giữ full history; không gửi `previous_response_id` để nối state ngoài history reference.

Tách ba lớp: tạo payload → một provider attempt gồm drain/normalize → xử lý downstream. Retry reference bọc đúng provider attempt; telemetry final và parser Diet ở ngoài retry. Kiểm tra HTTP mock để chắc chắn `12 attempts` ở adapter không bị nhân bởi retries ngầm. Giữ logic refresh credentials của SDK; mỗi lần retry dùng payload mới và credentials hợp lệ.

**Đầu ra:** repair Trae adapted chạy được bằng scripted Responses; reference prompts/tools/turn trajectory khớp oracle. Chưa cần gọi model thật để chứng minh workflow.

### Bước 4 — Implement compression adapted và nối Diet

Thêm compression protocol rõ ràng, ví dụ `trae-prefill` cho exact hiện tại và `responses-step-wrapper-v1` cho adapted thiếu prefill. Đưa protocol vào config hiệu lực/manifest, không suy ra ngầm từ tên model.

Với wrapper protocol, giữ phần context/target step của reference và thêm yêu cầu trả một wrapper hoàn chỉnh. Parser yêu cầu đúng step ID, một step duy nhất, nội dung không rỗng, output hoàn tất và usage hợp lệ. Chỉ bỏ wrapper theo protocol đã khai báo; output malformed đi vào failure behavior của Diet reference, không gọi lại model do parser lỗi và không áp summary lỗi.

Giữ nguyên code chọn step, context windows, threshold và quyết định chấp nhận compression. Với scripted compression text và usage giống nhau, so kết quả Diet với oracle. Với model thật, text/usage có thể khác do protocol adapted; báo khác biệt, không yêu cầu hai lần sinh phải giống nhau.

**Đầu ra:** một transcript scripted kích hoạt compression, có request/response usage, Diet decision và history sau compression; parser edge cases được kiểm thử.

### Bước 5 — Nâng audit trước khi chạy CVE thật

Sửa `compat/audit.py`, manifest trong `worker.py` và bundle verification trong `workflow/runner.py` đồng thời. Nếu runner hiện coi mọi profile Trae là exact, phải sửa phân loại đó trước khi cho adapted chạy end-to-end.

Report đề xuất tách `integrity_status`, `workflow_status`, `transport_status` và `overall_conformance`. Adapted có thể integrity/workflow pass nhưng overall là partial; absence of provider evidence không được biến thành exact pass. Schema cần version mới và xử lý bundle cũ theo schema cũ.

Thêm negative tests: sửa manifest adapted thành exact phải fail nếu thiếu evidence; đổi request sau khi hash phải fail; thiếu compression artifacts khi có compression phải được phát hiện. Capability evidence chỉ được công nhận theo mức nó chứng minh, không vì JSON ghi `supported`.

### Bước 6 — Chạy kiểm thử và smoke theo thứ tự

Sau khi thêm tests vào các file tương ứng, chạy nhóm tập trung:

```bash
PYTHONPATH=src:tests .venv-openhands/bin/python -m unittest \
  test_cli test_trae_contract_llm_policy test_trae_contract_llm_retry \
  test_trae_contract_subscription test_trae_contract_compressor_payload \
  test_trae_contract_diet_algorithm test_trae_contract_diet_parser \
  test_trae_contract_turns test_trae_contract_workflow test_trae_contract_audit
```

Tiếp theo chạy suite repository:

```bash
PYTHONPATH=src:tests .venv-openhands/bin/python -m unittest discover -s tests
```

Live smoke sau khi tests pass và flag mới tồn tại:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --input ../defects4c/out_tmp_dirs/debugging_framework/php/inputs \
  --case CVE-2016-9936__b2af4e886872 \
  --output inspect-subscription-trae-adapted-skip \
  --model gpt-5.6-sol --auth subscription \
  --reference-profile trae_verified --transport-conformance adapted \
  --diet-mode skip
```

Sau đó chạy lại một case với `--diet-mode ours --compressor-model gpt-5.6-luna` và output khác. Kiểm tra manifest/deviations, prompt/tools, limits/reminders, patch artifacts, validation result và token usage. Nếu smoke không kích hoạt compression, dùng fixture bước 4 để xác minh nhánh này; không coi smoke đó là bằng chứng đã chạy compressor.

Chỉ sau khi smoke hợp lệ mới chạy lệnh bốn CVE phía trên. Mỗi run dùng output riêng để tránh lẫn artifacts. Verification conformance và test suite của CVE là hai kết quả độc lập: patch pass không chứng minh exact.

### Bước 7 — Quyết định có mở exact subscription hay không

Đối chiếu evidence từng role với `TraeLLMPolicy.required_capabilities`. Chỉ thay guard cứng bằng validator có điều kiện khi tất cả yêu cầu đã được chứng minh và verifier nhận diện đúng mapping đó. Không để adapted flag, capability tự khai hay một thành công smoke vượt guard exact.

Nếu cache/prefill/sampling/cap semantics vẫn unsupported hoặc unverified, giữ exact subscription bị chặn. Deliverable vẫn hoàn thành ở mức subscription chạy workflow Trae adapted với Diet và audit đầy đủ; ghi cụ thể blocker để phân biệt giới hạn endpoint với việc implementation còn thiếu.

## Chia thay đổi để review

1. Capability probe/report và baseline fixtures; chưa đổi runtime.
2. Config/workflow split và tests ma trận; giữ guard exact.
3. Repair Responses adapted, retry và wire audit.
4. Compression wrapper adapted và differential Diet tests.
5. Manifest/verifier, CLI documentation và smoke artifacts.
6. Mở exact có điều kiện nếu có đủ evidence; đây là bước tùy kết quả kiểm chứng, không phải cam kết chắc chắn thực hiện được.

Mỗi thay đổi phải giữ tests exact API-key đang có. Không sửa frozen reference hoặc expected outputs của oracle để hợp thức hóa deviations của subscription.

## Trạng thái implementation (2026-10-05)

Đã triển khai `--transport-conformance adapted` và env `OPENHANDS_TRANSPORT_CONFORMANCE`. Config tách `uses_trae_workflow` khỏi `requires_exact_transport`; các nhánh prompts, runtime, turn scheduling, sandbox, patch capture và Diet chọn theo workflow. Generic và API-key exact giữ đường chạy hiện tại. API-key/generic + adapted bị từ chối; exact subscription vẫn bị chặn trước login/generation.

Subscription adapted có role policy, Responses history/tool projection, full history không dùng previous-response state, 12 attempts/backoff reference, inner retries tắt, correlation request/token usage và capability/deviation report. Các extra-body/seed/api-version chưa review bị từ chối. Request audit ghi đúng boundary `adapter_body_object`; HTTP serialization được kiểm chứng độc lập bằng mock tests và live probes.

Compressor adapted dùng `responses-step-wrapper-v1`; parser kiểm tra terminal status, usage, đúng step ID, single/nonempty wrapper và không có preamble/trailing text. Wrapper lỗi được bỏ qua, không retry parser và không áp summary lỗi. Diet selection/context/threshold/reduction vẫn dùng reference algorithm. Đây là compression protocol adapted, không phải prefill exact.

Verifier phân biệt adapted bằng manifest/effective config, role reports và request evidence. Bundle adapted hợp lệ trả `partial`, integrity pass, transport unsupported; workflow vẫn là structural/incomplete evidence, không tự nhận semantic pass. Đổi nhãn adapted thành exact bị phát hiện. Manifest schema v1 được giữ cho thay đổi additive/backward-compatible; chưa mở bất kỳ exact Responses schema nào. Không sửa frozen reference.

### Kết quả kiểm chứng

- Suite cuối: **270 tests, OK, 4 skipped**; log tại `analysis/subscription_checks/regression-final.log`. Các test async cũ cần chạy ngoài sandbox do hạn chế event-loop wakeup của môi trường; không cần sửa scheduler production.
- Nhóm regression tập trung: 24 tests OK, log `analysis/subscription_checks/targeted.log`.
- Fixtures adapted raw và hash-only chạy agent/worker/runner/verifier, kích hoạt một compression, kiểm tra Diet history và phát hiện đổi nhãn exact.
- HTTP mock lỗi: đúng 12 requests, backoff `[1, 2, ..., 2048]`, không hidden retries và payload giữ nguyên.
- Live subscription control thành công cho `gpt-5.6-sol` và `gpt-5.6-luna`: `repair-live.json`, `compression-live.json` trong `analysis/subscription_checks/`.
- Live compression wrapper hợp lệ: `compression-wrapper-live.json` có `wrapper_valid=true`. Các probes này chứng minh request được thực hiện/protocol wrapper hoạt động; không chứng minh exact endpoint semantics.
- Smoke CVE thật được khởi động riêng cho `skip` và `ours` tại `output/inspect-subscription-trae-adapted-skip/` và `output/inspect-subscription-trae-adapted-ours/`; kết quả cuối phải đọc `result.json` và `audit/conformance-report.json` sau khi run kết thúc. Không coi run đang chạy là benchmark pass.

Chưa mở exact subscription: SDK đã pin omits output cap, sampling/effort và không có bằng chứng cache/prefill tương đương. Không dùng capability tự khai hoặc wrapper pass để vượt guard. Batch bốn CVE chỉ thực hiện sau khi xem smoke; implementation đã có CLI để chạy batch theo lệnh phía trên.
