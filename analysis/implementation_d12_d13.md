# Kết quả triển khai D12–D13

Ngày **05/10/2026**. Triển khai theo `fix_d12_d13.md`, giữ các thay đổi D01–D11 có sẵn. Các yêu cầu trong tài liệu là đặc tả để triển khai; câu mô tả phạm vi của lượt viết tài liệu cũ không thay thế yêu cầu triển khai hiện tại.

## Kết quả và phạm vi

**D12 và D13 đã triển khai**, nghiệm thu bằng frozen-source oracle, SDK thật `1.49.5`, HTTP mock và hai Docker image có sẵn. Không sửa frozen Trae source hoặc reference hashes. Live provider và sửa lỗi benchmark PHP/fmtlib bằng model thật: **not_run**.

- D12: capture bằng đúng helper nguồn; filter literal; accepted terminal snapshot quyết định patch submit; lỗi artifact và recovery WIP có trạng thái/origin riêng.
- D13: exact SDK tắt ambient discovery, hooks, condenser/critic, stuck detection và budget; dispatcher tuần tự; cap nguồn 50/100, SDK scheduling limit 51/101; một preflight kiểm tra controls/capabilities/role policy ở các entry points.
- D14 bổ sung manifest, run/case identity, retention và integrity, được mô tả riêng trong [implementation_d14.md](implementation_d14.md).

## D12: patch và validation boundary

`workflow/trae_patch.py` gọi frozen `tools/get_diff.py` trong subprocess, merged stdout/stderr. `openhands/trae_session.py` stage cùng helper trong container, gọi Docker exec REST API qua Unix socket. Cmd giữ `git --no-pager diff --ignore-submodules=all`: **không stage untracked files**, không dùng `git diff HEAD` hay baseline diff thay source. Extra newline từ `print` được giữ. Inner decode strict và outer decode replacement như source; stderr Git/caught diagnostics không bị đổi thành runtime failure. Runtime exec exception retry ba lần, sleep năm giây mỗi lần kể cả lần cuối, rồi trả chuỗi rỗng như nguồn. Nonzero helper exit không được coi là API exception.

`remove_patches_to_tests` giữ nguyên thuật toán substring/case-sensitive và parsing `diff --git` của nguồn. `.phpt`, false positives, quoted paths, rename, binary/mode/deletion đều được so sánh với AST nguồn; không thêm extension heuristic.

`TraeContractAgent` capture trước close khi `task_done` hợp lệ hoặc đạt cap. Multi-tool batch hoàn tất theo thứ tự; test-only/empty patch tại `task_done` vẫn tạo source feedback và tiếp tục. Capture có trigger, command, origin, raw/filtered hashes. Snapshot public schema v1 có run/case ID, profile, turns, generation status và patch origin. Raw snapshots kiểm tra filter relation; hash-only giữ byte hashes, emptiness và functional patch, không chứa raw original/filter/history.

Runner xác minh schema/profile/turns/IDs và `contract-patch.diff` bằng bytes. Khi snapshot hợp lệ, `patch.diff` lấy từ snapshot kể cả workspace bị đổi sau stop. Snapshot sai/stale/missing ở lượt thành công thành `contract_artifact_error`, không recapture rồi giả successful completion. Worker error hoặc harness abort có thể export `recovery-patch.diff`, origin `error_wip`/`harness_abort_wip`, và ledger `evaluate_error_or_abort_wip`; generation vẫn là lỗi/abort. Recovery không được ghi nhận là accepted source terminal patch.

Baseline từ input kiểm tra configured production `source_extensions` đã tracked trong Git index. Input source bị ignore/untracked làm baseline thất bại với thông báo sửa provisioning; không tự `git add -f` để mở rộng capture. Untracked file do agent tạo sau đó vẫn theo source semantics.

External evaluator apply submitted patch vào validation copy riêng. Test edits/untracked reproducer của agent không đi sang copy. Apply/build/test thất bại giữ nguyên submitted patch, không feedback vào repair; `resolved=False` độc lập với `task_done`.

## D13: effective SDK controls

`build_agent` exact dùng `tools=[]`, `include_default_tools=[]`, `agent_context=None`, `condenser=None`, `critic=None`, `tool_concurrency_limit=1`. Worker tạo `TraeLocalConversation` với `stuck_detection=False`, `hook_config=None`, `max_budget_per_run=None`, scheduling cap từ profile. Stock `Agent.step`/recovery không tham gia exact history; SDK event state phục vụ orchestration/persistence. API/parser/context/tokenizer/lingua fatal không làm thêm repair request, không alternate model hay fallback summary.

`validate_trae_run`/`validate_runtime_controls` được gọi từ runner preflight, build, process và worker; CLI/env→RunConfig roundtrip có cùng controls. Structural config validation từ chối unknown keys, monetary budget và override `raw_events_path`/`response_path`. Exact positive watchdog bị reject; `0` được CLI chuyển thành disabled. Exact runtime phải Docker với local Unix `DOCKER_HOST`. Generic vẫn có execution path và watchdog riêng.

Repair và compression dùng actual/reference policy riêng kể cả explicit `inherit` cùng LLM. Exact API-key chỉ chạy khi capabilities được khai báo đủ; subscription/Responses và streaming reference chưa được chứng minh nên preflight reject. Provider retry vẫn 12 attempts, inner retry 0, không routing/cache phụ. SDK version khác pin bị reject trước generation.

Worker luôn dùng conversation UUID mới, không đọc archive cũ để implicit resume. Raw SDK archive được export qua sanitizer ngoài mount; đó là evidence, không phải resumable authentication checkpoint. Test resume của internal `TraeLocalConversation` kiểm tra raw argument/history source contract; public worker không cung cấp checkpoint/resume workflow. Crash interval giữa request và persisted decision chưa có exactly-once guarantee.

## Mapping nghiệm thu

| IDs | Modules kiểm chứng | Nội dung |
|---|---|---|
| P12-01, P12-05, P12-06 | `test_trae_contract_patch` | Real Git oracle, empty/tracked/untracked/staged/partial-stage/committed/add-N, index không đổi |
| P12-02, P12-04, P12-07 | `test_trae_contract_patch` | Toàn bộ filter patterns, `.phpt`, quoted paths, rename/deletion/binary/mode |
| P12-03, P12-09, P12-10 | `test_trae_contract_turns`, `test_trae_contract_sdk` | Test-only done, empty/nonempty cap cả profiles, batch done, sync/async |
| P12-08, P12-14 | `test_trae_contract_workflow`, `test_trae_contract_audit` | Workspace đổi sau stop, submit accepted bytes, external fail không repair |
| P12-11 | `test_trae_contract_patch`, `test_trae_contract_container` | Git/decode diagnostics, merged exec frames, runtime retry/failure |
| P12-12 | `test_trae_contract_patch`, `test_audit_retention`, `test_trae_contract_audit` | Strict schema, malformed artifact, worker fatal, recovery origin |
| P12-13, P12-15 | `test_validation`, `test_patch` | Copy dưới parent repo, explicit apply rejection, patch giữ nguyên |
| C13-01, C13-02, C13-06 | `test_trae_contract_sdk_controls` | SOUL/AGENTS/skills/hooks/MCP/plugin fixtures, stock-step sentinel, budget bypass |
| C13-03, C13-04 | `test_trae_contract_turns`, `test_trae_contract_sdk`, `test_trae_contract_tools` | 50/100 calls, không nudge/stuck finish, sequential full batch |
| C13-05 | `test_trae_contract_sdk`, `test_trae_contract_diet_integration`, `test_trae_contract_diet_algorithm` | Fatal/context/parser/provider errors dừng, source cause giữ nguyên |
| C13-07, C13-08 | `test_trae_contract_compressor_payload`, `test_trae_contract_llm_policy`, `test_compressor` | Separate actual models, explicit shared inherit, independent reference policies |
| C13-09, C13-10 | `test_trae_contract_sdk_controls`, `test_trae_contract_llm_policy`, `test_trae_contract_subscription`, `test_cli` | Unsupported controls, implicit inherit/watchdog reject, API capabilities/subscription preflight |
| C13-11 | `test_trae_contract_llm_retry`, `test_trae_contract_audit` | 12 attempts, unchanged model/body, telemetry ngoài retry |
| C13-12 | `test_trae_contract_sdk`, `test_audit_retention` | Internal persistence history, fresh public worker UUID, no implicit archive resume |
| C13-13 | `test_trae_contract_sdk` | Async terminal/cap/cleanup bằng source sync oracle |
| C13-14 | `test_trae_contract_sdk` | SDK pin reject |
| C13-15 | `test_cli`, `test_trae_contract_sdk_controls`, `test_trae_contract_audit` | CLI/env/config/build/worker effective controls và manifest |

## Bằng chứng và giới hạn

Logs/bundles mới ở [d14_checks](d14_checks/), baseline worktree trước triển khai ở [d12_d13_checks](d12_d13_checks/). Chi tiết versions/reference hashes ở `d14_checks/versions.json`. Full suite **264 tests: 260 pass + 4 opt-in Docker skips**; chính bốn methods Docker được chạy/pass riêng trên cả PHP và fmtlib. Frozen hashes, `pip check`, `git diff --check` pass. Xem `d14_checks/verification-summary.json` và báo cáo D14 để có logs, image IDs và retained verifier output.

Domain reject được khai báo trong `compat/diet_conformance.json`. Helper diagnostics thuộc source domain và có thể được source xem như patch nonempty; external apply vẫn có thể reject chúng. Exact support hiện yêu cầu local Unix Docker endpoint; không giả parity cho remote TCP Docker, subscription, arbitrary controls hoặc unknown provider semantics. Không có OPENAI_API_KEY/OPENROUTER_API_KEY trong process tại kiểm tra, và endpoint/model capabilities cho live chưa xác minh. Không load hay dump toàn bộ environment/.env. Integration trên image thật dùng **synthetic prepared case + scripted HTTP replies**, không thay live benchmark.
