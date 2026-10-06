# Subscription repair contract — 05/10/2026

`--auth subscription` hiện dùng cùng `TraeContractAgent`, `TraeLocalConversation`,
MessageManager, dispatcher và patch acceptance như `--auth api-key`, ở cả
`trae_verified` và `trae_multiswe`. Không cần chuyển sang `generic`.
OpenHands SDK giữ pin **1.49.5**, LiteLLM pin **1.103.2** để tái lập wire/SSE
behavior đã kiểm tra; không sửa package SDK cài sẵn.

## Thay đổi

- Bỏ guard API-key-only trước login trong `openhands/agent.py`.
- `SDKRawTransport` chọn Chat Completions hoặc Responses từ `llm.is_subscription`.
  Subscription tái sử dụng SDK provider routing, OAuth refresh, account/header
  và supported request options qua `_build_responses_call_kwargs`.
- Gửi exact system text qua `instructions`, giữ nguyên user text và assistant
  text. Tool calls/output được đổi wrapper theo Responses bằng `call_id`.
  Không thêm `Context (system prompt):`, default Codex prompt, summary hay
  security_risk. Tool descriptions/parameters/order và argument strings giữ
  nguyên; không parse hoặc sửa JSON trong transport.
- Đặt `strict: false` để tránh Responses tự chuẩn hóa schema optional thành
  strict. Đây là semantics tương ứng Chat Completions mặc định của source;
  xem [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)
  và [migration guide](https://developers.openai.com/api/docs/guides/migrate-to-responses).
- Response function-call `call_id` trở thành chat tool-call ID; không replay
  Responses item ID. `output_tokens` ánh xạ thành `completion_tokens`; usage
  thiếu vẫn là `None`, không tự tạo số token. Giữ SDK telemetry/token accounting.
- Drain stream trước khi trả assistant cho dispatcher. Thu `output_item.done`
  khi terminal `output=[]`; stream lỗi hoặc thiếu terminal gây lỗi rõ, không
  thực thi partial tool calls. Đóng iterator/HTTP response trong `finally`.
- Manifest ghi đúng `auth`, protocol Responses/Chat Completions và hash SDK
  auth/options code. Protected contract fields thắng cấu hình `extra_body`.

`is_error`/`agent_*` là metadata dispatcher ở logical state; Responses wire chỉ
gửi nội dung observation, ID và các field thuộc protocol. Metadata/error text
trong contract state vẫn được đối chiếu với source oracle.

## Kiểm tra

Suite tổng: **179 tests**, **177 pass**, 2 Docker tests skip. Hai Docker tests
đã pass ở đợt D01–D06 trước; đợt này không đổi container/tool runtime và không
chạy lại Docker. `pip check` không có dependency hỏng, `git diff --check` sạch.

- JSON HTTP thực sau LiteLLM serialization qua `HTTPHandler` +
  `httpx.MockTransport`: exact instructions/user/history, ordered non-strict
  schemas, batch calls, malformed raw arguments, ID pairing, OAuth headers,
  streaming/store=false và SDK token metrics.
- Refresh credentials giả bằng SDK, không đổi auth flow của production; test
  identity verification/JWKS được mock, không dùng OAuth của người dùng.
- Real SDK async run/close và AST `Expert.run` oracle độc lập: plain text,
  singleton task_done/task_failed, empty patch feedback, malformed/unknown tools,
  batch task_done, missing usage, cap 50/100 và compression-hook placement.
- Failure/incomplete stream cases và HTTP cleanup; API-key regression giữ nguyên.

Kết quả/lệnh và logs: [d01_d06_checks/subscription_results.json](d01_d06_checks/subscription_results.json),
[subscription_regression.log](d01_d06_checks/subscription_regression.log).

## Giới hạn còn lại

Chưa xác nhận endpoint subscription live chấp nhận exact `instructions`. SDK
stock có comment rằng instructions dài có thể bị từ chối; comment này không
phải bằng chứng endpoint hiện tại. Implementation không tự fallback sang user
prefix/generic nếu endpoint từ chối, để tránh âm thầm đổi contract.

Live smoke test hai lượt với OAuth hiện có bị automatic approval review từ
chối vì chưa có phê duyệt rõ việc gửi system prompt/schema và dữ liệu thử tới
OpenAI. Không thử lại hay đi vòng qua quyết định này. Bước chờ duyệt cụ thể:
gửi `SYS_PROMPT`/`TOOLS` với yêu cầu `think` một lần, replay `Continue.`, rồi nhận
`PROBE_OK`; không gọi bash/editor, không chạy benchmark hay gửi repository files.

Trong quá trình viết HTTP test, truyền nhầm OpenAI client cho Responses của
LiteLLM khiến một request với token giả trả 401. Đã đổi sang đúng `HTTPHandler`
và mock JWKS/identity; request này không dùng OAuth hiện có và không được tính
là live verification.

Subscription vẫn dùng policy options của SDK pin: không gửi temperature/n hay
max_output_tokens; SDK hiện không forward reasoning effort/include và bỏ replay
reasoning item ID khi store=false. Vì vậy contract repair D01–D06 được kiểm tra
theo phép đổi protocol, không tuyên bố sampling/model behavior hoặc parity toàn
hệ thống D07–D14 giống API-key. Compression vẫn theo implementation hiện tại.

## Cách chạy

```bash
PYTHONPATH=src .venv-openhands/bin/python -m openhands_adapter.cli \
  --auth subscription --model gpt-5.6-sol \
  --reference-profile trae_verified --diet-mode skip \
  --input <prepared-input-root> --case <case-id> --output subscription-check
```

Đổi profile sang `trae_multiswe` nếu muốn cap 100 turns, không budget reminders.
