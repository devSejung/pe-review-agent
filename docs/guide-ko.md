# Gerrit AI Reviewer 한국어 운영 가이드

이 문서는 Gerrit AI Reviewer를 처음 설치하거나 운영하는 사람이 **Gerrit 권한 요청부터 실제 Patch Set 리뷰 확인, 장애 대응, 프로젝트 범위 변경까지** 한 번에 따라갈 수 있도록 정리한 한국어 가이드입니다.

빠르게 설치만 하려면 `README.md`의 Production quick start를 봐도 되지만, 실제 팀 서비스로 운영할 때는 이 문서를 기준으로 보는 것을 권장합니다.

---

## 1. 이 서비스가 하는 일

Gerrit AI Reviewer는 Gerrit에 새로운 Patch Set이 올라오면 자동으로 다음 흐름을 수행합니다.

```text
Gerrit patchset-created
        ↓
receiver
        ↓
PostgreSQL durable job
        ↓
exact revision fetch
        ↓
Qwen / OpenAI-compatible API
        ↓
finding 검증
        ↓
Gerrit current revision 재확인
        ↓
Change-level summary + native inline/range comment
```

단순히 diff를 LLM에 던지고 댓글 한 번 다는 구조가 아닙니다.

운영 중 다음 상황을 고려해서 설계되어 있습니다.

- 같은 Gerrit event가 중복으로 들어오는 경우
- Qwen API가 느리거나 일시적으로 죽는 경우
- worker/container/host가 중간에 재시작되는 경우
- Gerrit POST는 성공했는데 응답만 유실되는 경우
- 리뷰 중에 더 최신 Patch Set이 올라오는 경우
- 삭제된 코드에 리뷰를 달아야 하는 경우
- 기존 repo에 open Change가 수천~수만 개 있는 경우
- 사내망이라 인터넷/CDN/PyPI 접근이 안 되는 경우

리뷰 결과는 Gerrit에 다음 두 형태로 올라갑니다.

1. Change 전체 요약 메시지
2. 실제 코드 라인 또는 범위를 가리키는 Gerrit native inline/range comment

삭제된 코드의 문제는 Gerrit `PARENT` side에 comment를 달 수 있습니다.

---

## 2. 주요 구성 요소

기본 Docker Compose 구성은 다음과 같습니다.

```text
postgres
  └─ durable jobs / review result / audit / admin settings

migrate
  └─ DB schema migration

receiver
  └─ Gerrit SSH stream-events의 patchset-created 수신

worker
  ├─ Gerrit revision fetch
  ├─ repository tool 실행
  ├─ Qwen review
  └─ Gerrit review publish

reconciler
  ├─ stream-events 누락 복구
  └─ ambiguous Gerrit publication 복구

admin
  └─ http://REVIEW_HOST:8080 운영 웹 UI
```

Admin Web은 기본적으로 **8080** 포트를 사용합니다.

worker health/metrics는 기본 **8081**입니다.

---

## 3. 운영 전에 준비할 것

최소 준비물은 다음과 같습니다.

- Docker가 동작하는 Linux 서버
- Gerrit bot/service account
- Gerrit SSH key
- Gerrit REST 인증 정보
- Gerrit `Stream Events` global capability
- 리뷰 대상 repo에 대한 `Read` 권한
- Qwen 또는 OpenAI-compatible LLM endpoint
- PostgreSQL password
- Admin Web password

기본 Compose는 PostgreSQL까지 포함하므로 별도의 PostgreSQL 서버는 필수가 아닙니다.

---

## 4. Gerrit 전용 bot 계정

운영 서비스는 개인 사번 계정보다 전용 service account를 권장합니다.

예:

```text
pe-review-agent
```

### 필요한 권한

필수:

- Gerrit SSH 접속
- 리뷰 대상 project의 `Read`
- Gerrit REST를 통한 일반 review message 작성
- Gerrit REST를 통한 inline/range comment 작성
- Global Capability의 `Stream Events`

필수가 아닌 권한:

- Gerrit Administrator
- Submit
- Code-Review +2
- 자동 merge 권한

이 bot은 기본적으로 **comment-only reviewer**입니다.

### 관리자에게 권한 요청할 때 예시

다음 수준으로 요청하면 됩니다.

```text
Gerrit AI code review bot용 service account 권한 요청드립니다.

대상 계정: pe-review-agent
대상 project: platform/dmc-fw, platform/ddrphy-fw 등 지정 project

필요 권한:
- 대상 project Read
- review message / inline comment 작성 권한
- Global Capability: Stream Events

불필요:
- Submit
- Code-Review +2
- Administrator
```

### Stream Events 권한 확인

```bash
timeout 5 ssh -p 29418 pe-review-agent@GERRIT_HOST \
  gerrit stream-events -s patchset-created
```

정상이면 에러 없이 대기합니다.

다음이 나오면:

```text
stream events not permitted
```

SSH 연결은 성공했지만 Gerrit의 global `Stream Events` capability가 없는 상태입니다.

---

## 5. 리뷰할 Gerrit project 이름 확인

Gerrit에서 보이는 repo 이름과 실제 project path를 혼동하지 않는 것이 중요합니다.

대상 repo에서:

```bash
git remote get-url origin
```

예:

```text
ssh://seungon.jung@gerrit.example.internal:29418/platform/dmc-fw
```

이 경우 project 이름은:

```text
platform/dmc-fw
```

입니다.

Admin Web의 Projects에도 이 값을 그대로 넣습니다.

`ssh://...` 전체 URL을 넣는 것이 아닙니다.

---

## 6. config.yaml 준비

배포 디렉터리에서 예제 설정을 복사합니다.

```bash
cp config.example.yaml config.yaml
```

또는 개발 repo 기준:

```bash
cp config/config.example.yaml config.yaml
```

핵심 설정은 다음과 같습니다.

```yaml
gerrit:
  ssh_host: "gerrit.example.internal"
  ssh_port: 29418
  ssh_user: "pe-review-agent"
  ssh_key_path: "/run/secrets/gerrit_ssh_key"
  known_hosts_path: "/run/secrets/gerrit_known_hosts"
  strict_host_key_checking: true

  rest_url: "https://gerrit.example.internal"
  rest_auth:
    mode: "basic"
    username: "pe-review-agent"
    password_env: "PE_REVIEW_GERRIT_HTTP_PASSWORD"

  # 웹에서 project를 관리한다면 비워 두는 것을 권장
  projects: []

llm:
  base_url: "https://qwen-api.example.internal/v1"
  model: "Qwen3.6-27B"
  api_key_env: "PE_REVIEW_LLM_API_KEY"
  request_timeout_seconds: 180
  concurrency: 2
  temperature: 0.1
  max_output_tokens: 6000

review:
  policy_version: "firmware-v1"
  output_language: "ko-KR"
  max_findings: 8
  min_confidence: 0.82

service:
  enabled: true
  worker_concurrency: 2
  claim_lease_seconds: 900
  poll_interval_seconds: 2
  reconcile_interval_seconds: 300
  reconcile_full_sweep_interval_seconds: 3600
  health_port: 8081

admin:
  host: "0.0.0.0"
  port: 8080
  auth_mode: "basic"
  username: "admin"
  password_env: "PE_REVIEW_ADMIN_PASSWORD"
```

---

## 7. Gerrit SSH와 REST는 각각 왜 필요한가

둘 다 필요합니다.

### SSH 29418

주 용도:

- `gerrit stream-events`
- Git revision fetch
- project read/fetch 권한 확인

### HTTPS REST

주 용도:

- Change current revision 확인
- open Change reconciliation
- Gerrit Set Review
- summary 게시
- inline/range comment 게시
- 게시 여부 복구 확인

즉 SSH만 연결되거나 REST만 연결되어서는 정상 운영이 안 됩니다.

---

## 8. Gerrit REST 인증

지원 모드는 다음 세 가지입니다.

### Basic

```yaml
rest_auth:
  mode: "basic"
  username: "pe-review-agent"
  password_env: "PE_REVIEW_GERRIT_HTTP_PASSWORD"
```

`.env`:

```dotenv
PE_REVIEW_GERRIT_HTTP_PASSWORD=...
```

### Bearer

```yaml
rest_auth:
  mode: "bearer"
  token_env: "PE_REVIEW_GERRIT_TOKEN"
```

`.env`:

```dotenv
PE_REVIEW_GERRIT_TOKEN=...
```

### none

인증 없는 사내 테스트 Gerrit 등에서만 사용합니다.

운영 환경에서는 회사 Gerrit 정책에 맞는 인증 방식을 사용하세요.

---

## 9. 사내 CA 인증서가 있는 경우

Gerrit 또는 Qwen endpoint가 사내 private CA를 쓰면 container에서 해당 CA를 신뢰할 수 있어야 합니다.

예:

```yaml
gerrit:
  ca_bundle_path: "/run/secrets/corporate_ca.pem"

llm:
  ca_bundle_path: "/run/secrets/corporate_ca.pem"
```

서버 인증 검증을 무작정 끄는 것보다 CA를 정상적으로 mount하는 방식을 권장합니다.

---

## 10. Qwen / vLLM 설정

이 서비스는 OpenAI-compatible chat completion API를 사용합니다.

사내 vLLM 예:

```yaml
llm:
  base_url: "http://qwen-server:8000/v1"
  model: "Qwen3.6-27B"
  api_key_env: "PE_REVIEW_LLM_API_KEY"
  request_timeout_seconds: 180
  concurrency: 2
  temperature: 0.1
  max_output_tokens: 6000
```

API key가 없는 내부 endpoint라면 key는 비워 둘 수 있습니다.

Admin Web의 **Connections -> Test /models**에서 endpoint와 model 목록 접근 여부를 확인할 수 있습니다.

---

## 11. 리뷰 언어 설정

기본값은 한국어입니다.

```yaml
review:
  output_language: "ko-KR"
```

Admin Web의:

```text
Settings
  -> Review language
```

에서 다음을 선택할 수 있습니다.

- 한국어 (`ko-KR`)
- English (`en-US`)

이 값은 candidate review와 verifier prompt 양쪽에 반영됩니다.

언어 선택은 **prompt 지시 방식**입니다. 모델 응답을 별도의 언어 판별기로 검사해서 강제로
재시도시키지는 않습니다. 따라서 일반적인 경우 선택한 언어로 출력되도록 유도하지만, 모델이
일부 기술 설명을 영어로 반환하는 것 자체를 후처리에서 실패로 간주하지는 않습니다.

한국어를 선택해도 다음과 같은 기술 식별자는 원문 그대로 유지하도록 prompt에서 지시합니다.

- 함수명
- 변수명
- type명
- macro
- register명
- 파일 path
- API 이름
- command
- literal
- error code

예:

```text
[P1] timeout 이후에도 training state가 진행됨

poll_done()이 -ETIMEDOUT을 반환하지만 호출부에서 반환값을 무시합니다.

Impact ...
Evidence ...
Suggested fix ...
```

JSON schema key, `P0/P1/P2`, `REVISION/PARENT` 같은 enum은 내부 처리 때문에 영어 값을 유지합니다.

---

## 12. secret 설정

password, token, private key는 Git이나 `config.yaml`에 직접 넣지 않습니다.

`.env` 예:

```dotenv
POSTGRES_PASSWORD=CHANGE_ME_TO_RANDOM_SECRET
REVIEWER_IMAGE=gerrit-ai-reviewer:local

PE_REVIEW_LLM_API_KEY=
PE_REVIEW_GERRIT_HTTP_PASSWORD=
PE_REVIEW_GERRIT_TOKEN=

PE_REVIEW_ADMIN_PASSWORD=CHANGE_ME_TO_ANOTHER_RANDOM_SECRET

ADMIN_BIND_ADDRESS=0.0.0.0
ADMIN_PORT=8080
WORKER_HEALTH_PORT=8081
```

SSH 파일:

```text
secrets/
├── gerrit_ssh_key
└── gerrit_known_hosts
```

`install.sh`는 기본적으로 이 파일들을 container UID 10001 기준으로 맞추고 mode `0600`을 요구합니다.

---

## 13. known_hosts 준비

운영 환경에서는 `StrictHostKeyChecking=yes`를 유지하는 것이 좋습니다.

회사 정책에 따라 Gerrit host key를 안전하게 확인한 후 `gerrit_known_hosts`에 넣으세요.

예:

```bash
ssh-keyscan -p 29418 GERRIT_HOST > secrets/gerrit_known_hosts
```

단, production에서는 `ssh-keyscan` 결과를 그대로 맹신하기보다 기존 신뢰 경로로 host fingerprint를 확인하는 것을 권장합니다.

---

## 14. 설치 전 설정 검증

```bash
pe-review-agent check-config
```

정상이면:

```text
configuration valid
```

처럼 통과합니다.

이 프로젝트는 unknown config key를 허용하지 않습니다.

예를 들어:

```yaml
service:
  enabld: false
```

처럼 오타가 있으면 실패합니다.

kill switch 오타가 조용히 무시되는 상황을 막기 위한 동작입니다.

---

## 15. 인터넷이 되는 곳에서 offline release 만들기

사내 서버가 인터넷이 안 되는 경우:

```bash
./deploy/build-release.sh 0.1.0
```

생성된 release archive를 사내 Linux host로 옮깁니다.

release에는 필요한 Docker image tarball이 포함되어 있으므로 target server가 PyPI나 public registry에 직접 연결될 필요가 없습니다.

---

## 16. 설치 및 실행

release 디렉터리에서:

```bash
./install.sh
```

정상 기동 시 주요 service는:

```text
postgres
migrate
receiver
worker
reconciler
admin
```

입니다.

---

## 17. Admin Web 접속

브라우저에서:

```text
http://REVIEW_HOST:8080/
```

기본 인증은 HTTP Basic입니다.

username:

```yaml
admin:
  username: "admin"
```

password:

```dotenv
PE_REVIEW_ADMIN_PASSWORD=...
```

### 보안 주의

HTTP Basic은 plain HTTP 자체를 암호화하지 않습니다.

여러 사용자가 쓰는 실제 사내 서비스라면 다음 구성을 권장합니다.

```text
Company HTTPS / SSO reverse proxy
            ↓
        Admin Web :8080
```

직접 8080을 회사 전체에 무방비로 공개하지 않는 것이 좋습니다.

---

## 18. Admin Web 화면 설명

### Dashboard

볼 수 있는 항목:

- 최근 24시간 job 수
- 최근 24시간 실패 수
- enabled project 수
- 현재 queue/active 수
- job state 분포
- 최근 job

### Projects

할 수 있는 작업:

- Gerrit project 추가
- project enable/disable
- Gerrit REST + Git/SSH read/fetch 테스트
- review 시작 범위 선택

### Connections

할 수 있는 작업:

- Gerrit SSH/REST metadata 설정
- Gerrit REST/SSH/Stream Events 테스트
- Qwen endpoint/model 설정
- Qwen `/models` 테스트

connection metadata 변경은 저장 후 receiver/worker/reconciler restart가 필요합니다.

### Jobs

볼 수 있는 항목:

- project/change/PS
- 현재 state
- attempt 수
- last error
- update 시각
- audit 상세

`FAILED_PERMANENT`은 Requeue할 수 있습니다.

### Audit

한 job에 대해 다음을 확인할 수 있습니다.

- 현재 state
- revision SHA
- policy version
- 전체 attempt timeline
- stage별 성공/실패
- 전체 error text
- Qwen summary
- finding 목록
- severity
- file/path
- `REVISION` / `PARENT`
- line/range
- confidence
- message
- impact
- evidence
- remediation
- exact Gerrit ReviewInput JSON
- Gerrit response
- publication status

즉 “AI가 실제로 어떤 리뷰를 달았는지” 감사 추적이 가능합니다.

### Logs

다음 component의 structured log를 볼 수 있습니다.

- receiver
- worker
- reconciler
- admin
- migrate

지원 기능:

- component filter
- DEBUG/INFO/WARNING/ERROR/CRITICAL filter
- 문자열 검색
- 5초 auto refresh
- exception/traceback 포함 full JSON 확인

### Settings

주요 기능:

- AI review service live pause/resume
- policy version
- review language
- max findings
- min confidence

---

## 19. 가장 중요한 설정: Project review 시작 범위

기존 Gerrit repo에는 open Change가 수천~수만 개 있을 수 있습니다.

그래서 새 project의 기본값은:

```text
From now on
```

입니다.

### From now on

의미:

```text
이 project를 reviewer에 등록/활성화한 시점 이후의 Patch Set부터 리뷰
```

장점:

- 기존 open Change 수만 건을 한꺼번에 review하지 않음
- Qwen API 폭주 방지
- DB queue 폭증 방지
- Gerrit REST/query 부하 방지

구현상 project별 cutoff timestamp를 PostgreSQL에 저장합니다.

reconciler도 Gerrit query 단계에서 cutoff를 사용합니다.

또한 Gerrit Change가 과거 Change인데 댓글 등으로 최근 update되었을 가능성을 고려해서 **현재 Patch Set의 실제 생성 시각도 다시 확인**합니다.

### Include current open Changes

이 옵션을 고르면 해당 project의 현재 open Change도 backfill 대상이 됩니다.

수천~수만 개 open Change가 있다면 매우 큰 작업이 될 수 있으므로 UI에서도 확인 경고를 띄웁니다.

운영에서는 특별한 이유가 없으면 `From now on`을 권장합니다.

### Backfill을 켰다가 다시 From now on으로 변경

아직 실행되지 않았고 Gerrit publish side effect도 없는 과거 backlog는:

```text
SKIPPED_SCOPE
```

로 종료합니다.

이미 Gerrit publication intent가 생긴 job은 함부로 버리지 않습니다.

POST가 실제 Gerrit에 반영됐는지 불확실할 수 있으므로 정상 publication reconciliation 흐름을 계속 탑니다.

### 다시 Include current open Changes로 변경

현재도 open 상태인 Change가 reconciler에서 다시 발견되면 과거 `SKIPPED_SCOPE` durable job은 다시 `RECEIVED`로 revive될 수 있습니다.

---

## 20. Project Disable / Enable 동작

Disable하면:

- 새 event ingest 중단
- reconciler 대상 제외
- PostgreSQL worker claim 대상 제외

즉 UI badge만 바뀌는 것이 아니라 실제 실행 경계에서 차단합니다.

`From now on` project를 다시 Enable하면 **재활성화 시각을 새 cutoff로 사용**합니다.

따라서 Disable되어 있던 동안 들어온 Patch Set이 나중에 한꺼번에 replay되지 않습니다.

---

## 21. 리뷰가 실제 Gerrit에 어떻게 표시되는가

`ko-KR` prompt를 따르는 일반적인 Change-level summary 예:

```text
검증된 수정 필요 결함 2건을 찾았습니다.
```

inline finding은 file/line/range에 native comment로 붙습니다.

예:

```text
fw/dram/train.c : 142-144

[P1] timeout 이후에도 training state가 진행됨

poll_done()이 -ETIMEDOUT을 반환하지만 반환값이 무시됩니다.
```

single-line finding은 `line`을 사용하고, 범위 finding은 Gerrit `range`를 사용합니다.

삭제된 코드에 대한 finding은:

```text
side = PARENT
```

를 사용합니다.

---

## 22. 첫 E2E 테스트는 반드시 test repo 하나로

처음부터 여러 production repo를 넣지 마세요.

권장 순서:

1. test repo 하나만 Projects에 추가
2. `From now on` 유지
3. Project Test 실행
4. Connections에서 Gerrit Test
5. Connections에서 LLM Test
6. 새 Patch Set 하나 업로드
7. Dashboard/Jobs 확인
8. Gerrit summary 확인
9. native inline comment 확인
10. Audit에서 exact ReviewInput 확인

---

## 23. 최초 E2E에서 확인할 항목

### 1) receiver

`patchset-created` event가 들어왔는지 확인합니다.

### 2) durable job

Jobs 화면에 해당 Change/Patch Set job이 생겼는지 확인합니다.

### 3) exact revision fetch

review 대상 SHA가 실제 Patch Set revision과 같은지 확인합니다.

### 4) Qwen review

REVIEWING 단계가 정상 완료되는지 확인합니다.

### 5) durable result

publication 전에 review result가 DB에 저장됩니다.

### 6) current revision guard

publish 직전에 Gerrit current revision을 다시 확인합니다.

### 7) Gerrit 결과

다음을 모두 확인합니다.

- Change summary
- inline comment
- range highlight
- 삭제 line이면 PARENT comment

### 8) duplicate event

같은 event를 재처리해도 duplicate comment가 생기지 않아야 합니다.

### 9) newer Patch Set

PS1 리뷰 중 PS2를 올렸을 때 PS1 stale review가 뒤늦게 publish되지 않는지 확인합니다.

---

## 24. Job state 이해하기

주요 state:

```text
RECEIVED
FETCHING
REVIEWING
VALIDATING
READY_TO_PUBLISH
PUBLISHING
RETRY_WAIT
FAILED_PERMANENT
SUPERSEDED
SKIPPED_SCOPE
DONE
```

### RECEIVED

event가 durable job으로 들어온 상태입니다.

### FETCHING

Gerrit revision / repository 준비 중입니다.

### REVIEWING

Qwen review를 수행하는 단계입니다.

### VALIDATING

finding 위치와 publish 가능성을 검증합니다.

### READY_TO_PUBLISH

review 결과가 DB에 durable하게 저장됐고 Gerrit publish를 기다리는 상태입니다.

### PUBLISHING

Gerrit review publication 흐름입니다.

### RETRY_WAIT

transient failure 후 backoff 대기 상태입니다.

### FAILED_PERMANENT

자동 retry budget을 소진했거나 permanent failure가 발생했습니다.

원인 해결 후 Admin Web에서 Requeue할 수 있습니다.

### SUPERSEDED

더 최신 Patch Set이 존재하여 이 job이 오래된 상태입니다.

### SKIPPED_SCOPE

Project의 `From now on` cutoff 범위 밖이라 의도적으로 실행하지 않은 job입니다.

### DONE

리뷰 publication이 정상 완료된 상태입니다.

---

## 25. 장애 발생 시 어디를 볼 것인가

우선 순서:

```text
Dashboard
  ↓
Jobs
  ↓
Job Audit
  ↓
Logs
```

### Dashboard

실패가 늘었는지, queue가 쌓였는지 봅니다.

### Jobs

문제 Change의 current state와 last error를 확인합니다.

### Job Audit

다음을 봅니다.

- 어느 stage에서 실패했는지
- attempt가 몇 번 발생했는지
- exact error text
- review result가 이미 만들어졌는지
- publication intent가 생겼는지
- Gerrit response가 있는지

### Logs

process-level stack trace와 전체 structured JSON을 확인합니다.

---

## 26. 주요 장애별 대응

### Qwen timeout / unavailable

증상:

- REVIEWING
- RETRY_WAIT
- LLM transport/timeout error

대응:

- Qwen endpoint 상태 확인
- `/models` test
- network/proxy/CA 확인
- timeout 확인

job은 PostgreSQL에 남아 있습니다.

### Gerrit 403

확인:

- REST account
- target project Read
- comment permission
- Stream Events capability

`stream events not permitted`은 Stream Events capability 문제입니다.

### Git fetch 실패

확인:

- SSH key
- known_hosts
- project Read 권한
- project 정확한 이름

Projects의 Test 버튼으로 REST Read + Git/SSH fetch를 같이 검사할 수 있습니다.

### Gerrit POST timeout

POST가 실제 Gerrit에는 적용됐지만 응답만 유실됐을 가능성이 있습니다.

이 경우 bot은 무작정 같은 review를 다시 POST하지 않습니다.

durable publication intent를 이용해서 Gerrit message를 먼저 조회하고 이미 게시되었는지 reconciliation합니다.

### worker crash

lease가 만료되면 다른 worker가 durable state를 기준으로 reclaim할 수 있습니다.

### newer Patch Set 발생

오래된 pending job은 stale publication이 되지 않도록 차단됩니다.

---

## 27. FAILED_PERMANENT Requeue

웹:

```text
Jobs
  -> 해당 FAILED_PERMANENT job
  -> Requeue
```

CLI:

```bash
docker compose run --rm worker \
  requeue --job-id <review-job-uuid>
```

Requeue는 기존 audit history를 지우지 않습니다.

review 결과가 이미 있으면 Qwen부터 다시 돌지 않고 가능한 durable 단계부터 이어서 진행합니다.

---

## 28. 전체 서비스 잠시 멈추기

평상시 운영 pause:

```text
Settings
  -> AI review service OFF
```

이 switch는 DB-backed live control입니다.

다음 경로에서 새 작업을 막습니다.

- receiver ingest
- worker claim
- reconciler

durable state는 삭제하지 않습니다.

### 하드 kill switch

```yaml
service:
  enabled: false
```

또는:

```bash
PE_REVIEW__SERVICE__ENABLED=false
```

이 bootstrap 값은 Admin Web보다 우선합니다.

`false`인 상태에서는 웹에서 ON으로 바꿔도 실제 service가 켜지지 않습니다.

---

## 29. Health / metrics

Admin Web:

```text
GET http://REVIEW_HOST:8080/healthz
GET http://REVIEW_HOST:8080/readyz
```

Worker:

```text
GET http://127.0.0.1:8081/healthz
GET http://127.0.0.1:8081/readyz
GET http://127.0.0.1:8081/metrics
```

8081은 기본 Compose에서 localhost-only 운영을 권장합니다.

---

## 30. 로그 파일 위치

기본:

```text
/var/lib/pe-review-agent/logs/
```

예:

```text
receiver.jsonl
worker.jsonl
reconciler.jsonl
admin.jsonl
migrate.jsonl
```

기본 rotation은 process별 약 10 MiB, backup 5개입니다.

stdout JSON logging도 유지됩니다.

---

## 31. Review policy

기본 review policy는 firmware correctness 중심입니다.

우선순위:

- 실제 runtime defect
- timeout/error handling
- race/concurrency 문제
- register/API 계약 위반
- memory/state corruption 가능성
- 잘못된 boundary/range
- Patch Set 변경으로 새로 생긴 문제

다음과 같은 낮은 신호 문제는 publish하지 않도록 검증 단계에서 걸러냅니다.

- naming
- formatting
- documentation only
- 단순 style

finding은 changed line에 anchor되어야 합니다.

LLM이 임의의 pre-existing line을 지목했다고 해서 그대로 Gerrit comment로 publish하지 않습니다.

---

## 32. P0 / P1 / P2

모델은 finding severity로 다음 값을 사용합니다.

```text
P0
P1
P2
```

정확한 severity 판단은 review policy와 repository context를 함께 사용합니다.

운영 초기에 false positive가 많으면 `min_confidence`를 높이는 방법도 있습니다.

예:

```yaml
review:
  min_confidence: 0.90
```

다만 너무 높이면 유효 finding까지 사라질 수 있으므로 실제 test repo 결과를 보고 조정하세요.

---

## 33. policy_version 의미

```yaml
review:
  policy_version: "firmware-v1"
```

job identity에는 policy version이 포함됩니다.

즉 동일 Patch Set이라도 review policy version이 바뀌면 별도 durable identity가 될 수 있습니다.

운영 중 policy를 의미 있게 바꿀 때 version도 같이 올리는 것이 좋습니다.

예:

```text
firmware-v1
firmware-v2
```

---

## 34. 대규모 repo 운영 권장값

처음에는 보수적으로 시작하세요.

예:

```yaml
service:
  worker_concurrency: 2

llm:
  concurrency: 2
```

그리고 다음을 관찰한 후 늘립니다.

- Qwen latency
- Qwen 동시 요청 허용량
- token usage
- repo fetch I/O
- Gerrit REST latency
- queue depth
- 서버 CPU/RAM/storage

repo를 많이 등록하더라도 `From now on` 기본값을 유지하면 최초 등록 시 수만 개 open Change가 몰리는 것을 막을 수 있습니다.

---

## 35. 기존 repo를 backfill하고 싶다면

Projects에서 해당 repo를:

```text
Include current open Changes
```

로 변경합니다.

주의:

- open Change가 수천~수만 개일 수 있음
- Qwen 비용/부하가 매우 커질 수 있음
- Gerrit query/REST 부하 증가
- DB queue 급증 가능

따라서 실제 production에서 backfill은 보통 다음처럼 제한된 시간에 수행하는 것이 좋습니다.

```text
1. worker concurrency 낮게 유지
2. 대상 repo 한 개만 backfill
3. queue/latency 관찰
4. 필요하면 From now on으로 복귀
```

---

## 36. DB 데이터는 왜 중요한가

PostgreSQL은 단순 cache가 아닙니다.

다음이 durable하게 저장됩니다.

- job identity
- state
- retry history
- attempt history
- review result
- finding
- finding lineage
- publication intent
- exact Gerrit ReviewInput
- Gerrit response
- Admin project settings
- review start cutoff

따라서 운영 PostgreSQL volume을 임의로 삭제하면 안 됩니다.

---

## 37. repo cache와 DB의 차이

repo mirror/worktree는 cache 성격입니다.

깨졌다면 Gerrit에서 다시 만들 수 있습니다.

반면 PostgreSQL은 durable operational state입니다.

즉 장애 복구 우선순위는:

```text
PostgreSQL 보존 > repo cache 보존
```

입니다.

---

## 38. 보안 체크리스트

운영 전 확인:

- 전용 Gerrit bot account 사용
- Submit 권한 없음
- Code-Review +2 없음
- SSH private key Git 미포함
- password/token Git 미포함
- StrictHostKeyChecking 유지
- private CA 정상 설정
- 8080 direct exposure 제한
- 가능하면 HTTPS/SSO proxy 사용
- PostgreSQL 5432 host 공개 안 함
- Admin password 강한 값 사용
- secret이 UI/API response로 다시 노출되지 않는지 유지

---

## 39. 첫 production enable 체크리스트

다음을 모두 확인한 후 여러 repo를 Enable하는 것을 권장합니다.

- [ ] Gerrit bot SSH login 성공
- [ ] `Stream Events` capability 성공
- [ ] test repo Read 성공
- [ ] Project Test 성공
- [ ] Gerrit REST test 성공
- [ ] Qwen `/models` test 성공
- [ ] Admin Web 8080 로그인 성공
- [ ] Review language 확인
- [ ] test repo가 `From now on`인지 확인
- [ ] 새 Patch Set event 수신
- [ ] exact revision fetch 성공
- [ ] Qwen review 성공
- [ ] summary Gerrit 게시 성공
- [ ] inline/range comment 성공
- [ ] 삭제 line PARENT comment 확인
- [ ] duplicate event 중복 comment 없음
- [ ] newer Patch Set stale publish 차단 확인
- [ ] Jobs Audit 확인
- [ ] Logs 확인
- [ ] pause/resume 확인
- [ ] FAILED_PERMANENT requeue 확인

---

## 40. 자주 묻는 질문

### Q. repo를 추가하면 기존 수만 개 Change를 전부 리뷰하나요?

아니요.

기본값은 `From now on`입니다.

등록/활성화 이후의 Patch Set 위주로 처리합니다.

기존 open Change까지 리뷰하려면 명시적으로 `Include current open Changes`를 선택해야 합니다.

### Q. Stream Events는 global 권한인데 모든 repo를 리뷰하나요?

아닙니다.

service 내부 project allowlist와 Admin Web managed projects를 기준으로 필터링합니다.

### Q. Qwen이 리뷰를 영어로 쓸 수 있나요?

`Settings -> Review language`에서 한국어/영어를 선택할 수 있습니다.

기본은 `ko-KR`입니다.

다만 현재는 모델 prompt로 언어를 지정하는 방식이며, 응답 언어를 후처리에서 강제 검증하지는
않습니다.

### Q. 코드 특정 줄에 댓글이 달리나요?

네.

Gerrit native `line` 또는 `range` comment를 사용합니다.

### Q. 삭제된 코드에도 달리나요?

네.

`PARENT` side를 사용합니다.

### Q. Gerrit POST 응답이 끊기면 duplicate review가 생기나요?

무작정 재POST하지 않습니다.

durable publication intent와 tagged Gerrit message를 이용해 게시 여부를 먼저 reconciliation합니다.

### Q. worker가 죽으면 job이 사라지나요?

아니요.

PostgreSQL durable state를 기준으로 lease 만료 후 reclaim합니다.

### Q. 설정 변경은 바로 반영되나요?

종류에 따라 다릅니다.

즉시 반영:

- project enable/disable
- project review scope
- global pause/resume

restart 필요:

- Gerrit connection metadata
- LLM endpoint/model
- review policy 일부
- review language

UI에서 restart 필요 여부를 표시합니다.

---

## 41. 운영자가 기억해야 할 핵심 8개

1. **새 repo 기본은 From now on**이다.
2. **기존 open Change backfill은 명시적으로만 켠다.**
3. Gerrit은 **SSH + REST 둘 다** 필요하다.
4. `Stream Events`는 Gerrit global capability다.
5. bot에는 Submit/+2 권한이 필요 없다.
6. 장애가 나면 **Jobs -> Audit -> Logs** 순서로 본다.
7. PostgreSQL은 cache가 아니라 **durable state**다.
8. 처음 production 적용은 **test repo 하나**로 E2E 검증 후 확대한다.

---

## 42. 관련 문서

- `README.md` — 전체 프로젝트 개요와 production quick start
- `docs/runbook.md` — 운영/장애 대응 runbook
- `docs/oss-engine-notes.md` — OSS reviewer 검토 기록
- `config/config.example.yaml` — 전체 설정 예제
- `deploy/env.example` — secret/environment 예제
