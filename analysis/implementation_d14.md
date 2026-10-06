# Kết quả triển khai D14

Ngày **05/10/2026**. D14 đã triển khai audit ngoài repair, retention raw/hash-only, correlation, integrity và verifier offline. Nghiệm thu local source fixtures và SDK/Docker scripted integration; live model/provider **not_run**. Phạm vi D12/D13 xem [báo cáo riêng](implementation_d12_d13.md).

## Bundle và vận hành

Mỗi case tạo `run_id` mới, giữ `case_id`, `relative_id`, SDK conversation UUID và profile. Manifest được khởi tạo trước preflight/baseline để failed runs cũng có identity. Rerun thay artifacts/logs/audit của case, không dùng lại protocol/contract/archive cũ. Cần output/run-name khác nếu muốn giữ các lượt lịch sử.

Bundle gồm:

- `audit/manifest.json`: effective JSON/env/CLI→worker settings, config/input/validation hashes, actual/reference/wire model theo role, role active/shared, capabilities, runtime controls, source/SDK/dependency hashes, baseline/image evidence, retention và audit completeness.
- `events.jsonl`: schema, run/case ID, producer UUID/sequence và record ID; logical turn, request/call IDs, parent compression request, step targets, actual SDK event IDs.
- `contract-manifest.json`, `protocol-manifest.json`, `contract-result.json`, `contract-patch.diff`, `patch.diff`, `result.json`, `response.txt` và validation/worker logs nếu stage đã chạy.
- `audit/sdk/` khi raw bật: SDK JSON/JSONL export sau close, đã sanitize credentials. Staging ngoài mount, dọn sau export; archive còn sau repair cleanup. Raw=false không lưu archive/history/stdout raw.
- `audit/conformance-report.json`: integrity/structural checks, từng contract và coverage limitations. Submitted patch là functional output giữ nguyên bytes cả khi raw=false.

Manifest hash mọi file top-level, audit tree (trừ chính `audit/manifest.json`) và logs tree, gồm nested SDK `manifest.json`. Verifier yêu cầu đủ implementation/SDK hash coverage, đối chiếu dependency lock và frozen source files thực tế. Schema phải là integer, không chấp nhận boolean bằng số 1; producer/record identity sai kiểu hoặc không khớp sequence bị báo integrity issue. Không hash transient `.tmp` hoặc retained workspaces. `pending_requests`, `pending_calls`, missing run/worker boundaries, partial/corrupt JSONL, sidecar audit errors làm `audit_complete=False`. Generation failure có thể được audit đầy đủ; audit đầy đủ không chứng nhận generation success hay source conformance.

## Capture, correlation và metrics

Exact payload được snapshot tại **client body object** ngay trước OpenAI create, không gọi đó là raw HTTP wire bytes. Payload đầy đủ, Unicode/newline/raw argument strings và full raw provider response được giữ/hash; previews không thay evidence. Canonical structural hash: JSON UTF-8, sorted keys, separators `(',', ':')`, `allow_nan=False`. Patch/source/file hash: nguyên bytes.

Một logical request có UUID dùng chung telemetry và transport; 12 retry attempts có cùng request ID/model/body hash. Provider response và transport result được ghi trước parsing, telemetry/parsing ở ngoài retry. Compression context có parent repair request, role và step target. ContextVar giữ role/call state cho shared LLM qua threads/async contexts; không inject metadata audit vào wire body.

Logical batch/SDK event mapping được ghi riêng từ source step IDs. After-normal hooks và gate/decision/skip/rejection/fatal events chứa target, context/hash, token/LZ4 operands, parser raw/usage/reasons, original storage, replacement và actual next repair payload. Accepted change không suy ra từ việc thiếu rejection event. Source `MessageManager.steps` vẫn quyết định request; audit copies không mutate live history.

Provider token telemetry tách source-reference metrics. Unknown/missing/null không thành zero; trường hợp LiteLLM normalize null usage thành zero được khắc phục bằng context riêng đọc original OpenAI response dump. Reported zero vẫn zero. Cache/reasoning details không cộng lại vào total; `known_tokens`, unknown/pending/failed call counts và completeness riêng. Không tính monetary cost/budget cho exact. Source compression usage addition/parser failure semantics giữ nguyên.

## Integrity, credential và failure policy

JSONL writer dùng thread lock + `flock` cho toàn record và xử lý partial writes. Strict reader nhận diện partial tail, corrupt middle, duplicate same-content/conflict, sequence gap, unsupported schema; duplicate identical được dedup nhưng vẫn ghi integrity issue. Recovery API cho metrics không giả corrupt evidence thành complete.

Audit sink/serialization/hash/estimate/archive failure có marker riêng và không retry model, không đổi accepted output/source exception. SDK `ConversationRunError` giữ cause nguồn. Source/provider/parser/tokenizer/lingua fatal dừng generation; không feedback external evaluator vào repair.

Sanitizer chỉ áp dụng copies export/log/report, không sửa request, live steps hoặc patch filter. Chỉ đọc credentials được config/transport lựa chọn; mask api_key/token/authorization/cookie/password/client_secret và known secret values; metadata URL bỏ userinfo/query. Sentinel tests kiểm tra config/error/history/SDK state/stdout/external logs, đồng thời actual body vẫn có nguyên original content. Raw diff chứa credential trong test edits chuyển riêng terminal capture evidence sang hash-only, giữ submitted filtered patch đúng bytes. **Credential nằm trong functional submitted patch chính nó nằm ngoài credential-free fixture domain**: patch vẫn giữ source bytes; không hứa sanitizer sửa patch mà vẫn exact. Redacted evidence không được full-parity certification.

SDK export có thể xem lại, không phải authentication checkpoint để resume. Public worker không implicit discover/resume; internal conversation persistence tests không mở public crash-resume guarantee. Locking/atomic JSON writes không tương đương fsync/power-loss durability hoặc exactly-once giữa request và persisted decision.

## Verifier offline và levels

`compat/audit.py` kiểm tra artifacts/events/schema/IDs, hashes, config/reference/SDK/source pin, effective controls/role mappings, policies/tools/system prompt, retry count/body, turn sequence, diet reduction/context decisions, terminal capture/submission bytes và reconstruction khi raw còn đầy đủ. Generic profile trả unsupported cho Trae conformance. Hash-only có integrity/structural checks nhưng thiếu full reconstruct; `--full` fail và ghi limitations. Tampered/redacted/incomplete evidence fail có reason/contract ID.

`--oracle` so sánh repair/compression requests và steps với independent fixture từ frozen source AST; báo field và character offset đầu tiên khác, không echo secret text. Allowed-differences chỉ chấp nhận field/category whitelist có reason/evidence. Không replace substring trong prompt/tools/stop/history để tạo pass.

| Category | Ledger/phạm vi được phép | Giới hạn |
|---|---|---|
| harness | Runtime/workspace/profile controls được khai báo và kiểm tra | Không miễn ambient discovery, extra turn hay recovery |
| model | Explicit actual/reference role mapping; oracle fixture chỉ project trường `model` | Prompt/tools/options/history không bị đổi |
| input | Case source path/issue encoding/hash ghi trong manifest | Không âm thầm normalize nội dung issue |
| external_validation | Policy/verdict/apply origin độc lập generation | Không repair feedback/extra generation |

Không tự thêm allowed differences vào mọi run: mặc định ledger rỗng; fixture cần khác model khai báo cụ thể trong independent oracle. Integrity/structural pass không phải global D01–D14 parity. CLI báo source oracle `not_run` nếu không có independent expected history. Provider semantics không được xác minh chỉ bằng HTTP body fixture.

## Mapping A14-01…23

| IDs | Modules kiểm chứng | Evidence/phạm vi |
|---|---|---|
| A14-01 | `test_cli`, `test_trae_contract_sdk_controls`, `test_trae_contract_audit` | Effective configs/controls/role manifests |
| A14-02 | `test_trae_contract_audit`, `test_audit_integrity` | >40k Unicode/raw strings, structural/byte hashes không dựa previews |
| A14-03 | `test_trae_contract_audit` | Raw/hash deterministic bodies/steps/metrics/status/patch identical |
| A14-04 | `test_trae_contract_audit`, `test_trae_contract_audit_container` | Sanitized SDK archive còn sau cleanup; hash-only không giữ raw |
| A14-05 | `test_trae_contract_audit`, `test_trae_contract_compressor_payload`, `test_trae_contract_sdk` | Explicit shared LLM policies, concurrent ContextVars, async orchestration |
| A14-06 | `test_trae_contract_audit`, `test_trae_contract_llm_retry` | One request/12 attempts, body/model stable, telemetry error ngoài retry |
| A14-07 | `test_trae_contract_audit`, `test_trae_contract_turns`, `test_trae_contract_sdk` | Batch/event IDs/after-normal, terminal và cả caps |
| A14-08 | `test_trae_contract_audit`, `test_trae_contract_diet_algorithm`, `test_trae_contract_diet_parser` | Disabled/window/threshold/LZ4/usage/parser/reduction source decisions |
| A14-09 | `test_trae_contract_diet_serialization`, `test_trae_contract_diet_algorithm`, `test_trae_contract_diet_integration`, `test_trae_contract_audit` | Empty/whitespace/nested originals/show_ctx false, source history và actual next request |
| A14-10 | `test_trae_contract_audit`, `test_trae_contract_diet_integration`, `test_trae_contract_diet_algorithm` | Provider/parser/tokenizer/lingua fatal, cause và no next repair |
| A14-11 | `test_token_tracking`, `test_trae_contract_audit`, `test_trae_contract_diet_integration` | Unknown/zero/cache/reasoning/pending totals; null usage không normalize zero |
| A14-12 | `test_trae_contract_workflow`, `test_trae_contract_audit` | Accepted snapshot bất biến, external fail và patch hashes |
| A14-13 | `test_audit_integrity` | 4 processes × 8 large Unicode records, không interleave |
| A14-14, A14-15 | `test_audit_integrity` | Partial tail/middle corruption/duplicates/conflict/gaps/schema |
| A14-16 | `test_audit_retention` | Same case success→baseline fail→worker fatal, UUID/artifacts không stale |
| A14-17 | `test_audit_integrity`, `test_trae_contract_audit` | Disk/serialization failure tách audit, generation không thêm call |
| A14-18 | `test_trae_contract_audit`, `test_audit_integrity`, `test_audit_retention` | Credential sentinel across exports/SDK/streams/logs, no live payload mutation |
| A14-19 | `test_trae_contract_audit`, `test_audit_integrity`, `test_trae_contract_sdk` | Patch/manifest/payload/schema/reference/SDK pin/hash checks; nested SDK manifest + logs tamper fail |
| A14-20 | `test_trae_contract_audit` | Independent source history + model-only projection, forbidden messages exemption reject, char mismatch |
| A14-21 | `test_trae_contract_audit` | Hash-only/redacted full check fail, limitations explicit |
| A14-22 | `test_trae_contract_audit_container` | PHP/fmtlib SDK + actual tools + default ours compression + isolated external evaluator |
| A14-23 | `test_audit_retention`, `test_trae_contract_sdk`, `test_trae_contract_audit` | Fresh public run, internal persist history, pending/missing boundaries incomplete; crash-resume outside domain |

## Retained bằng chứng

`analysis/d14_checks/capture_evidence.py --containers` dùng pinned SDK, HTTP MockTransport và images có sẵn; không gọi paid/live model. Chạy với `PYTHONPATH=src:tests LITELLM_LOCAL_MODEL_COST_MAP=True OPENHANDS_SUPPRESS_BANNER=1 .venv-openhands/bin/python`. Script từ chối overwrite existing evidence directory. `bundle-results.json` mô tả từng level.

| Bundle | Generation / external | Verifier |
|---|---|---|
| `bundles/local-raw/out/case` | task_done / failed | Full structural + independent frozen-source oracle pass |
| `bundles/local-hash/out/case` | task_done / failed | Integrity/structural pass; full-unavailable report fail đúng policy |
| `bundles/php/out/case` | task_done / nonefix | Full structural pass, tools/SDK/diet/evaluator trên PHP image |
| `bundles/fmtlib/out/case` | task_done / nonefix | Full structural pass, tools/SDK/diet/evaluator trên fmtlib image |

Hai local bundles có actual repair/compression bodies identical. Cả bốn có 4 repair requests, 1 compression request, 1 accepted compression, submitted source patch; external failure chủ ý để kiểm tra generation/verdict separation. Container fixtures dùng synthetic `source.c`/tests case, không sửa benchmark thật. Retained source oracle độc lập có Git helper output dựng trong temp repo riêng, sau đó frozen Expert/analyzer AST dựng expected requests/steps; không lấy adapter histories làm expected.

CLI đọc lại bằng chứng:

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.compat.audit \
  analysis/d14_checks/bundles/local-raw/out/case --expected-profile trae_verified --full \
  --oracle analysis/d14_checks/bundles/local-raw/out/case/audit/source-oracle.json

PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.compat.audit \
  analysis/d14_checks/bundles/local-hash/out/case --expected-profile trae_verified
```

Kết quả CLI lưu `verifier-cli-raw.json`, `verifier-cli-hash.json`. Full suite **264 methods**, **260 pass + 4 opt-in skips**; chính **4 Docker methods pass riêng**, mỗi method chạy PHP/fmtlib subtests. Logs chốt: `full-regression.log`, `container-final.log`; audit container được chạy lại sau lần siết verifier cuối và pass ở `audit-container-final.log`. Phiên bản và reference hashes: `versions.json`; tổng hợp: `verification-summary.json`. Các logs lần thử cũ không thay kết quả chốt. Frozen hashes, pip check, diff check pass.

OpenHands SDK `1.49.5`; LiteLLM `1.103.2`; OpenAI `2.54.0`; httpx `0.28.1`; tiktoken `0.14.0`; LZ4 `4.4.5`; pexpect `4.9.0`; ptyprocess `0.7.0`. Sandbox không hỗ trợ socketpair cần cho SDK; local SDK/Docker checks chạy qua escalation được chấp nhận, không workaround thuật toán.

Live provider **not_run**: OPENAI_API_KEY/OPENROUTER_API_KEY không có trong process lúc kiểm tra, capabilities/model availability của endpoint live chưa xác minh. Không load/dump toàn environment/.env. Conformance manifest phân biệt local source, SDK/container và live. Bundle source hashes ràng buộc checkout/SDK hiện tại; cần cùng implementation để verify lại sau các thay đổi tương lai.
