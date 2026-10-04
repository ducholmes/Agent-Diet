# Agent Diet adapter for OpenHands

## 1. Muc tieu

Package `openhands_adapter` chay Agent Diet bang OpenHands SDK tren cac input da
duoc chuan bi san. Phien ban dau tien chi can chay mot case theo workflow:

1. Nap va kiem tra input.
2. Tao repair workspace va validation workspace rieng biet.
3. Chay baseline/preflight nhe de kiem tra input va workspace sach.
4. Cho OpenHands sua code trong repair workspace.
5. Lay patch tu repair workspace.
6. Ap patch vao validation workspace sach.
7. Chay validator de quyet dinh ket qua.
8. Luu patch, trajectory, metrics, log va ket qua.

Model co the chay test trong luc sua loi, nhung ket luan cua model khong duoc
dung lam verdict. Chi validation workflow moi duoc phep quyet dinh case da duoc
resolve hay chua.

## 2. Nguyen tac thiet ke

- `Agent-Diet/artifact/` la tai lieu tham chieu va duoc giu nguyen, khong sua.
- Khong import truc tiep `traj_analyzer.py`, `Expert`, `Sandbox` hoac runner Trae.
- Logic Agent Diet cho OpenHands duoc adapted tu artifact vao package moi.
- OpenHands quan ly agent loop; adapter khong viet lai `Expert.run()`.
- Repair va validation khong bao gio chay tren cung mot workspace da bi model sua.
- Input goc chi duoc doc va copy, khong bi sua.
- Baseline chi la preflight nhe; no khong chay full build/test suite.
- Raw events phai duoc luu day du du context cua model da bi reduce.
- MVP chi ho tro mot case, Docker runtime va execution tuan tu.

## 3. Kien truc tong quan

```text
Prepared input (read-only)
          |
          v
    Input loader
          |
          v
  Create isolated workspaces
          |
          v
 Lightweight baseline/preflight
          |
          +-------------------------------+
          |                               |
          v                               v
  Repair workspace                Validation workspace
          |                               |
          v                               |
OpenHands Agent                          |
+ guarded workspace-shell tool only      |
 + AgentDietCondenser                     |
          |                               |
          v                               |
      patch.diff -------------------------+
                                          |
                                    Apply patch
                                          |
                                    Run validation
                                          |
                                          v
                              Deterministic RunResult
```

Co hai boundary doc lap:

- **Agent boundary:** OpenHands chi nhin thay repair workspace.
- **Evaluation boundary:** validator chi danh gia patch tren mot validation
  workspace sach va khong doc final answer cua model.

Baseline khong phai la mot lan validation day du. Muc dich cua no chi la phat
hien som input sai, workspace ban hoac moi truong thieu truoc khi ton chi phi
goi repair model.

## 4. Cau truc thu muc du kien

```text
Agent-Diet/
├── artifact/                         # giu nguyen artifact Trae
├── src/
│   └── openhands_adapter/
│       ├── __init__.py
│       ├── adapter.md
│       ├── cli.py
│       ├── config.py
│       ├── input_loader.py
│       │
│       ├── diet/
│       │   ├── __init__.py
│       │   ├── core.py
│       │   ├── trajectory.py
│       │   ├── strategies.py
│       │   └── condenser.py
│       │
│       ├── openhands/
│       │   ├── __init__.py
│       │   ├── llm.py
│       │   ├── agent.py
│       │   ├── worker.py
│       │   ├── workspace_tool.py
│       │   ├── container.py
│       │   └── process.py
│       │
│       └── workflow/
│           ├── __init__.py
│           ├── models.py
│           ├── command.py
│           ├── environment.py
│           ├── outcome.py
│           ├── workspace.py
│           ├── patch.py
│           ├── validation.py
│           ├── artifacts.py
│           └── runner.py
│       │
│       └── security/
│           ├── __init__.py
│           └── guard.py
│
└── tests/
    ├── test_diet.py
    ├── test_input_loader.py
    ├── test_patch.py
    ├── test_validation.py
    ├── test_environment.py
    └── test_outcome.py
```

Day la cau truc dich. Khi bat dau MVP, mot so file co the duoc gop lai de giam
khoi luong code:

- `trajectory.py` va `strategies.py` co the tam nam trong `diet/core.py`.
- `patch.py` co the tam nam trong `workflow/workspace.py`.
- `models.py` va `artifacts.py` co the tam nam trong `workflow/runner.py`.

Sau khi workflow end-to-end chay on dinh moi tach cac file nay ra.

## 5. Trach nhiem cua tung phan

### 5.1. Top-level package

#### `config.py`

Chua ba nhom cau hinh:

- `OpenHandsConfig`: model, authentication, reasoning effort, max iterations va
  budget.
- `AgentDietConfig`: mode, threshold, context truoc/sau, tokenizer va compressor.
- `WorkflowConfig`: input/output path, timeout va tuy chon giu workspace.

Config chi mo ta du lieu va validate gia tri; config khong tao container, agent
hay workspace.

#### `input_loader.py`

Discover va nap mot prepared case thanh `CaseSpec`, bao gom:

- case ID;
- source project;
- failure log;
- Docker image/runtime;
- setup/build/target/regression commands;
- failing test IDs.

Input loader khong chay command va khong tao workspace.

#### `cli.py`

Parse command line, nap config va goi `workflow.runner.run_case()`. CLI khong
chua logic validation hoac OpenHands.

### 5.2. `diet/`

#### `core.py`

Chua thuat toan Agent Diet doc lap voi OpenHands SDK:

- chon step can reduce;
- `ctx_before` va `ctx_after`;
- token threshold;
- LZ4 heuristic;
- dieu kien chap nhan reduction;
- Agent Diet metrics.

Logic nay duoc adapted tu `artifact/.../traj_analyzer.py`, nhung khong import
artifact va khong su dung global environment configuration.

#### `trajectory.py`

Chuyen OpenHands events thanh logical steps, serialize step thanh representation
on dinh va giu mapping:

```text
logical step -> OpenHands event IDs
```

Mot tool call va observation cua no phai nam trong cung mot atomic step.

#### `strategies.py`

Implement cac mode:

- `skip`;
- `delete`;
- `random`;
- `ours`;
- `lingua`.

`ours` nhan compressor callable tu ngoai thay vi import `llm_polytool` cua
artifact.

#### `condenser.py`

Implement Agent Diet condenser theo contract OpenHands `CondenserBase`. Adapter
duoc truyen vao `Agent(condenser=...)`; `LocalConversation` chi nhan `agent` va
khong nhan condenser rieng. Moi lan OpenHands chuan bi mot agent step moi,
condenser:

1. Tao logical steps tu event view.
2. Goi Agent Diet core.
3. Chay strategy neu candidate du dieu kien.
4. Tra ve OpenHands `Condensation` voi event IDs can forget va summary thay the.

Neu compressor loi, output khong hop le hoac reduction khong du lon, condenser
giu nguyen view va ghi ly do vao metrics.

### 5.3. `openhands/`

#### `llm.py`

Tao repair LLM va compressor LLM tu config. Authentication details chi ton tai
trong module nay.

#### `container.py`

Tao va huy repair container. Container phai chi mount repair workspace va ap
dung policy hardening:

- `--network=none`;
- read-only root filesystem;
- non-root UID/GID;
- drop all capabilities va `no-new-privileges`;
- writable `/tmp` qua tmpfs;
- khong mount input goc, validation workspace, output artifacts, host home hay
  Docker socket.

Container luon bi `docker rm -f` trong `finally`, ke ca khi worker timeout hay
bi kill.

#### `workspace_tool.py`

Agent chi duoc cap cac tool file (`list_files`, `read_file`, `search_text`,
`write_file`, `edit_file`, `inspect_workspace_diff`) va command
(`list_configured_commands`, `run_configured_command`). Khong co shell tuy y.

Config command nam trong worker-config tam ngoai repair workspace va khong
mount vao container. Executor giu argv/cwd bat bien, agent chi chon phase va
index tu 0. Command chay nguyen argv qua Docker exec, voi timeout trong
container, host timeout va output limit. Shell wrapper chi duoc chay neu da
khai bao trong config goc. Config khong duoc sinh thanh file trong workspace.

Tool file kiem tra duong dan, tu choi symlink, `.git` va artifact noi bo; chan
sua test/fixture theo ten thong dung va file command tham chieu truc tiep.
Doc/tim/sua file dung thao tac Python co pham vi; diff dung argv Git co dinh,
tat external diff va textconv. Cac tool khong nhan command hay argv tu model.
Day la gioi han command truc tiep, khong phai sandbox cho hanh vi cua source
hay script duoc command goi gian tiep.

SDK duoc pin tai `requirements-openhands.lock` (`openhands-sdk==1.49.5`). Truoc
khi tao `LocalConversation`, worker tat ambient plugin/skill discovery va vision
profile discovery. Day la fail-closed boundary: version SDK khac version da pin
se lam worker dung thay vi co the nap them tool khong duoc review.

#### `process.py`

Launch worker trong subprocess/process group doc lap. Day la authority cho
`agent_timeout`:

1. Ghi worker config, stdout, stderr va event paths ngoai repair workspace.
2. Chay worker trong process group moi va theo doi bang monotonic clock.
3. Khi het `agent_timeout`, gui `SIGTERM` cho process group, cho grace period,
   sau do gui `SIGKILL` neu can.
4. Tra ve exit code, elapsed time va co `timed_out` ro rang; outer workflow van
   capture patch de audit nhung khong coi patch timeout la resolved.

#### `security/guard.py`

Guard command truoc tool execution. No deny va audit cac command co dau hieu:

- path escape khoi repair workspace;
- truy cap input goc, validation workspace, output/artifact root, host home,
  Docker socket hoac `.git` noi khac;
- benchmark/reference/gold-patch lookup bi cam boi benchmark policy.

Guard la defence-in-depth; container mount isolation moi la boundary chinh.

#### `agent.py`

Lap rap:

```text
LLM + WorkspaceTool + AgentDietCondenser -> OpenHands Agent
```

#### `worker.py`

Chay mot OpenHands conversation trong subprocess rieng:

- doc worker config va prompt;
- tao agent va `LocalConversation`;
- ap dung `max_iterations` neu OpenHands SDK version da dung ho tro API nay;
- khong tinh chi phi tien; gioi han run bang iterations va outer timeout;
- ghi events, response va token usage cua tung request;
- dong conversation va tra exit code.

Outer workflow enforce agent wall-clock timeout va kill worker neu can.

### 5.4. `workflow/`

#### `models.py`

Chua cac data model toi thieu:

- `CaseSpec`;
- `CommandSpec`;
- `RunLimits`;
- `BaselineResult`;
- `ValidationResult`;
- `RunResult`.

#### `environment.py`

Validate environment Docker fail-closed: runtime phai ton tai, `docker info`
thanh cong va prepared image phai `image inspect` duoc. Validator khong tu pull,
build image hay cai dependency. Command validation mount validation workspace
vao `/testbed` trong image va chay `--network=none`.

#### `command.py`

Mot command executor dung cho post-patch validation va cac preflight command
rat nhe neu can. No ghi command, cwd, exit code, elapsed time, stdout va stderr;
timeout duoc tinh bang monotonic clock. Voi validation image mode, command duoc
wrap thanh Docker command; moi invocation luu mot log doc lap gom command, cwd,
return code, elapsed, stdout va stderr.

#### `outcome.py`

Phan loai ket qua APR tu test IDs da verify truoc va sau patch:
`plausible`, `cleanfix`, `noisefix`, `nonefix`, `negfix` hoac `invalid`.
Khong duoc phan loai patch neu test execution/test ID khong xac minh duoc.

#### `workspace.py`

Quan ly hai workspace:

- copy source thanh repair workspace;
- copy source thanh validation workspace;
- dam bao hai workspace la hai duong dan doc lap;
- cleanup hoac giu workspace theo config.

#### `patch.py`

- Tao Git baseline trong repair workspace.
- Capture tracked va untracked changes thanh binary patch.
- Liet ke changed/created files.
- Kiem tra protected paths.
- Chay `git apply --check` va apply patch vao validation workspace.

#### `validation.py`

Chua hai entry point co muc dich khac nhau:

- `run_baseline()`: preflight nhe de kiem tra input va workspace san sang.
- `run_post_patch()`: chay setup/build/target/regression va tao verdict.

Baseline mac dinh chi kiem tra:

- source project, config va failure log ton tai va doc duoc;
- command specs parse duoc, `cwd` hop le va khong thoat khoi project;
- Docker runtime va image can thiet co san;
- reserved failure-log path chua ton tai trong source;
- repair va validation la hai copy rieng, khong trung voi input goc;
- Git baseline commit duoc tao trong repair workspace;
- `git status --porcelain` cua repair workspace rong sau khi stage failure log va
  tao baseline commit;
- validation workspace chua bi sua truoc khi repair bat dau.

Baseline khong chay setup, build, target test hay regression test trong MVP.

`run_post_patch()` chay deterministic suite theo thu tu:

1. setup va build fail-fast;
2. expand moi target command co `{test_id}` cho tung failing test ID;
3. chay tat ca target invocations de thu du evidence day du;
4. luon chay full `regression_test`, ke ca khi target van fail;
5. kiem tra return code, timeout, `evidence_pattern`, `failure_pattern`,
   command-not-found, zero-test output va bang chung test da thuc su chay;
6. parse test ID neu co the va reject verdict neu evidence/test IDs khong
   trustworthy;
7. so sanh post failures voi declared baseline failures de tao APR outcome.

`regression_test` khong duoc chua `{test_id}`. Setup/build failure, test khong
duoc execute, zero tests, timeout hay test runner unavailable deu la `invalid`,
khong phai mot test failure binh thuong. Verdict duoc tao tu evaluator, khong tu
model response.

#### `artifacts.py`

Ghi output theo mot schema on dinh.

#### `runner.py`

Orchestrate toan bo mot case. File nay chi goi cac component tren; no khong tu
implement compression, Git parsing hay test classification.

## 6. Workflow chi tiet

### Phase A: prepare va baseline

1. Load `CaseSpec`.
2. Tao temporary run root.
3. Copy input project vao `repair/` va `validation/`.
4. Kiem tra hai workspace doc lap va validation workspace chua bi sua.
5. Kiem tra config, command specs, Docker runtime/image va reserved paths.
6. Stage failure log vao repair workspace.
7. Tao Git baseline commit trong `repair/`.
8. Kiem tra repair workspace clean bang `git status --porcelain`.
9. Dung som neu bat ky baseline check nao khong hop le.

Phase nay khong chay setup, build hoac test. Vi baseline khong sua validation
workspace, khong can xoa va copy lai validation workspace truoc khi apply patch.

### Phase B: agent

1. Tao prompt chi dan model doc failure log va sua production code.
2. Chay OpenHands worker voi Agent Diet condenser.
3. Luu raw events va Agent Diet metrics.
4. Khi agent ket thuc hoac timeout, capture patch tu repair workspace.

### Phase C: validation

1. Reject empty patch hoac protected-file changes.
2. Chay `git apply --check` tren validation workspace sach.
3. Apply patch.
4. Chay setup/build neu case yeu cau.
5. Chay target tests.
6. Chay regression tests.
7. Tao verdict va ghi `result.json`.

### Phase D: cleanup

- Mac dinh xoa temporary workspaces va containers.
- `keep_workspaces=true` giu lai repair/validation de debug.
- Artifacts va logs luon nam ngoai temporary workspace.

## 7. Timeout va gioi han MVP

MVP co bon gioi han doc lap:

- `agent_timeout`: wall-clock timeout cua OpenHands worker.
- `max_iterations`: so agent iterations toi da.
- `command_timeout`: timeout cua moi shell command do agent goi.
- `validation_timeout`: timeout cua moi setup/build/test command.

Khong tinh chi phi tien; gioi han run bang iterations va timeout. Timeout
khong duoc chi dua vao model; outer runner phai co quyen terminate worker.

## 8. Output cua mot run

```text
output/<case-id>/
├── result.json
├── patch.diff
├── response.txt
├── events.jsonl
├── logs/
└── validation/
    └── *.log
```

`result.json` gom verdict, cac moc thoi gian, token usage cua OpenHands, Agent
Diet metrics, tom tat baseline/validation va loi neu co. `events.jsonl` la event stream duy
nhat cua run: stage cua workflow, action/observation cua agent, token usage va
Agent Diet metrics. Output khong tao `baseline.json`, `validation.json` hoac
`diet-events.jsonl`; noi dung can thiet cua chung nam trong `result.json` va
`events.jsonl`.

Phan verdict co dang toi thieu:

```json
{
  "case_id": "...",
  "baseline": "clean",
  "agent": "completed",
  "patch_generated": true,
  "patch_applied": true,
  "validation": "plausible",
  "resolved": true,
  "elapsed_seconds": 12.345,
  "timings": {"baseline": 0.2, "agent": 8.1, "validation": 3.9, "total": 12.3},
  "token_usage": {
    "repair": {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200, "calls": 2},
    "compression": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "calls": 1},
    "total": {"input_tokens": 1100, "output_tokens": 220, "total_tokens": 1320, "calls": 3}
  },
  "error": null
}
```

Ket qua loi can ghi ro phase va ly do, vi du `baseline_failed`, `agent_timeout`,
`no_patch`, `protected_change`, `patch_apply_failed` hoac `validation_failed`.

## 9. Dependency direction

```text
cli
 └── workflow.runner
      ├── input_loader
      ├── workflow.workspace
      ├── workflow.patch
      ├── workflow.validation
      └── openhands.worker
           ├── openhands.agent
           └── diet.condenser
                ├── diet.core
                ├── diet.trajectory
                └── diet.strategies
```

Bat buoc giu cac rang buoc:

- `workflow.validation` khong import OpenHands.
- `diet.core` khong import workflow hoac artifact.
- `openhands.worker` khong tu quyet dinh validation result.
- Khong co module nao trong `src/` sua hoac import runtime code tu `artifact/`.

## 10. Pham vi MVP

MVP se lam:

- mot prepared case moi lan chay;
- Docker runtime;
- OpenHands `Agent` va `LocalConversation`;
- Agent Diet modes `skip` va `ours` truoc;
- lightweight baseline/preflight va post-patch validation;
- repair/validation workspace rieng;
- raw events, patch, logs va result artifacts;
- agent, command va validation timeouts.

MVP chua lam:

- batch processing va multiprocessing;
- resume/retry scheduler;
- nhieu benchmark backend;
- host execution mode;
- dashboard hoac database;
- distributed workers;
- tu dong sua hoac import code Trae artifact.

## 11. Thu tu trien khai

1. Hoan thien `CaseSpec` va input loader.
2. Implement workspace va lightweight baseline/preflight checks.
3. Implement patch capture/apply va post-patch validation.
4. Chay workflow bang mot fake/fixed patch de kiem tra evaluator.
5. Implement OpenHands worker voi `skip` mode.
6. Adapt Agent Diet core va implement `ours` condenser.
7. Them metrics, artifacts va integration tests.

Workflow deterministic phai chay dung truoc khi them Agent Diet. Cach nay giup
phan biet loi cua agent/condenser voi loi cua evaluator.
