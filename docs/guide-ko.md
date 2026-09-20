# Gerrit AI Reviewer 한국어 운영 가이드

이 문서는 Gerrit AI Reviewer를 처음 설치하거나 운영하는 사람이 **Gerrit 권한 요청부터 실제 Patch Set 리뷰 확인, 장애 대응, 프로젝트 범위 변경까지** 한 번에 따라갈 수 있도록 정리한 한국어 가이드입니다.

빠르게 설치만 하려면 `README.md`의 Production quick start를 봐도 되지만, 실제 팀 서비스로 운영할 때는 이 문서를 기준으로 보는 것을 권장합니다.

## 처음 설치하는 사람은 여기만 위에서부터 그대로 따라하세요

### 먼저 기억할 운영 명령

이제 Docker Compose 명령을 직접 외울 필요가 없습니다. `deploy` 디렉터리에서 다음 스크립트만
사용하면 됩니다.

```bash
./start.sh             # 서버 켜기
./stop.sh              # 서버 끄기 (DB/volume 삭제 안 함)
./restart.sh           # 서버 재시작 + .env/config 변경 재반영
./status.sh            # 현재 상태 보기
./logs.sh              # 전체 최근 로그
./logs.sh worker       # worker 로그만 보기
./doctor.sh            # 파일/이미지/포트/Docker 상태 한 번에 진단
```

최초 설치만 `./install.sh`를 사용합니다. 첫 설치 때 `8080` 또는 worker health 포트가 이미 다른
프로세스에 의해 사용 중이면 `install.sh`가 빈 포트를 찾아 `.env`에 자동 반영하고 최종 Admin Web
주소를 출력합니다. 이미 운영 중인 서비스의 주소가 몰래 바뀌면 안 되므로 `start.sh`와
`restart.sh`는 포트를 자동 변경하지 않습니다.

사내망처럼 Docker registry CA, PyPI mirror, Debian apt mirror가 모두 필요한 환경은 한 번만
`corporate.env.example`을 `corporate.env`로 복사해 회사 값을 채운 뒤 다음 두 스크립트를 사용합니다.

```bash
./configure-corporate-host.sh   # Docker mirror + 회사 CA chain 등록
./build-local.sh                # PyPI/Debian mirror를 사용해 reviewer/Postgres image 준비
```

`corporate.env`는 Git에 올라가지 않습니다. 회사 내부 주소와 CA 파일 위치를 한 번 적어 두면 매번
긴 `docker build --build-arg ...` 명령을 다시 입력할 필요가 없습니다.

애플리케이션 설정도 직접 여러 파일을 만들기 싫다면 `./configure.sh`를 사용할 수 있습니다. 이
스크립트는 `.env`, `config.yaml`, SSH key/known_hosts의 **배포용 복사본**을 만들고 DB/Admin Web
비밀번호를 자동 생성합니다. `~/.ssh`의 원본 파일은 수정하지 않습니다.

이 절은 **Docker는 이미 설치되어 있고**, Linux/Git/Docker에 익숙하지 않은 사람이 새 서버에서 처음
설치한다는 기준으로 작성했습니다. 중간 단계를 건너뛰지 말고 위에서부터 순서대로 진행하세요.

이 절을 따라가는 동안에는 다음 두 가지를 하지 않습니다.

- `bootstrap-host.sh`를 실행하지 않습니다. 이미 사용할 Linux 계정이 있다면 필요 없습니다.
- host의 `~/.ssh/id_ed25519_gerrit` 소유자/권한을 임의로 `10001`로 바꾸지 않습니다.

container 내부 UID 처리는 나중에 `sudo ./install.sh`가 배포용 SSH key **복사본만** 알아서 처리합니다.

### 1단계. 홈 디렉터리로 이동

```bash
cd ~
pwd
```

예를 들어 다음처럼 나오면 정상입니다.

```text
/home/pe-review-agent
```

### 2단계. 소스 clone

처음 설치하는 경우:

```bash
cd ~
git clone https://github.com/devSejung/pe-review-agent.git
cd pe-review-agent
```

이미 clone한 적이 있어서 `pe-review-agent` 폴더가 있다면 `git clone`을 다시 하지 말고 다음만 실행합니다.

```bash
cd ~/pe-review-agent
git switch main
git pull --ff-only
```

현재 위치 확인:

```bash
pwd
```

정상 예:

```text
/home/pe-review-agent/pe-review-agent
```

### 3단계. Docker가 실제로 동작하는지 확인

Docker가 설치되어 있다는 것과 daemon이 실제로 동작하는 것은 별개입니다. 다음 두 명령을 실행합니다.

```bash
sudo docker ps
sudo docker compose version
```

`sudo docker ps`가 표를 출력하고, `sudo docker compose version`이 버전을 출력하면 다음 단계로 갑니다.

Docker daemon 연결 오류가 날 때만 다음을 실행합니다.

```bash
sudo systemctl start docker
sudo systemctl enable docker
sudo docker ps
```

### 4단계. reviewer Docker image 만들기

repo root에서 다음을 실행합니다.

public PyPI에 직접 접근 가능한 환경이면:

```bash
cd ~/pe-review-agent
sudo docker build -t gerrit-ai-reviewer:local .
```

사내망에서 public PyPI가 차단되어 있고 사내 PyPI mirror를 사용해야 한다면 아래처럼 build arg를
추가합니다. `PYPI_MIRROR_URL`과 `PYPI_MIRROR_HOST`는 회사에서 안내한 실제 값으로 바꾸세요.

```bash
cd ~/pe-review-agent

sudo docker build \
  --build-arg PIP_INDEX_URL="PYPI_MIRROR_URL" \
  --build-arg PIP_TRUSTED_HOST="PYPI_MIRROR_HOST" \
  -t gerrit-ai-reviewer:local .
```

예를 들어 mirror URL이 `https://repository.example.internal/repository/pypi/simple`이라면 host 값은
`repository.example.internal`입니다.

이 두 값은 Docker image runtime 설정이 아니라 **image를 만드는 동안 pip가 어느 package index를
사용할지** 정하는 값입니다. `PIP_INDEX_URL`을 지정하지 않으면 기존과 동일하게 pip 기본 index를
사용합니다.

사내망에서 build가 그 다음 `apt-get update` 단계에서 `deb.debian.org` 연결 실패로 멈춘다면
Debian apt mirror도 같이 지정해야 합니다. 주의할 점은 **host가 Ubuntu여도 이 Docker image는
Debian trixie 기반**이라는 것입니다. host의 Ubuntu `resolute` sources.list를 Docker image에 그대로
넣으면 안 됩니다. 회사에서 제공하는 **Debian용** mirror URL 두 개를 사용하세요.

```bash
cd ~/pe-review-agent

sudo docker build \
  --build-arg PIP_INDEX_URL="PYPI_MIRROR_URL" \
  --build-arg PIP_TRUSTED_HOST="PYPI_MIRROR_HOST" \
  --build-arg APT_DEBIAN_MIRROR_URL="DEBIAN_MIRROR_URL" \
  --build-arg APT_DEBIAN_SECURITY_MIRROR_URL="DEBIAN_SECURITY_MIRROR_URL" \
  -t gerrit-ai-reviewer:local .
```

예를 들어 `DEBIAN_MIRROR_URL`은 upstream `http://deb.debian.org/debian`을 proxy하는 사내
repository이고, `DEBIAN_SECURITY_MIRROR_URL`은 upstream
`http://deb.debian.org/debian-security`를 proxy하는 사내 repository여야 합니다.

완료 후 image가 생겼는지 확인합니다.

```bash
sudo docker image ls gerrit-ai-reviewer
```

`gerrit-ai-reviewer`와 `local` tag가 보이면 정상입니다.

> Docker registry는 사내 mirror로 연결됐는데 build 중 `pip ... /simple/...`에서 connection reset이
> 난다면 Docker 문제가 아니라 pip가 public PyPI로 나가고 있는 경우가 많습니다. 이때 위의
> `PIP_INDEX_URL` / `PIP_TRUSTED_HOST` build arg를 사용하세요. 사내 mirror 자체가 없으면 아래의
> **offline release 설치** 절을 사용하세요.

### 5단계. PostgreSQL image 준비

기본 Compose는 `pe-review-postgres:16.15`라는 local image tag를 사용합니다.

```bash
sudo docker pull \
  postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94

sudo docker tag \
  postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94 \
  pe-review-postgres:16.15
```

확인:

```bash
sudo docker image ls pe-review-postgres
```

`pe-review-postgres`와 `16.15`가 보이면 정상입니다.

### 6단계. 배포 디렉터리로 이동하고 설정 파일 복사

```bash
cd ~/pe-review-agent/deploy

cp env.example .env
cp ../config/config.example.yaml config.yaml
```

확인:

```bash
pwd
ls -al .env config.yaml docker-compose.yml install.sh
```

현재 위치는 다음이어야 합니다.

```text
/home/pe-review-agent/pe-review-agent/deploy
```

### 7단계. `.env` 작성

`.env`는 **현재 `deploy` 디렉터리 안에 그대로 둡니다.** 다른 위치로 옮기지 않습니다.

```bash
nano .env
```

기본 내용은 다음 형태입니다.

```dotenv
POSTGRES_PASSWORD=여기에_DB용_비밀번호
REVIEWER_IMAGE=gerrit-ai-reviewer:local

PE_REVIEW_LLM_API_KEY=
PE_REVIEW_GERRIT_HTTP_PASSWORD=여기에_Gerrit_REST용_비밀번호
PE_REVIEW_GERRIT_TOKEN=

PE_REVIEW_ADMIN_PASSWORD=여기에_Admin웹_비밀번호

ADMIN_BIND_ADDRESS=0.0.0.0
ADMIN_PORT=8080
WORKER_HEALTH_PORT=8081
```

의미는 다음과 같습니다.

- `POSTGRES_PASSWORD`: 이 서비스 내부 PostgreSQL용 비밀번호입니다. 새로 정해서 넣으면 됩니다.
- `PE_REVIEW_ADMIN_PASSWORD`: `http://서버IP:8080` Admin Web 로그인 비밀번호입니다. 새로 정합니다.
- `PE_REVIEW_GERRIT_HTTP_PASSWORD`: Gerrit REST Basic 인증에 쓰는 비밀번호/HTTP credential입니다.
- `PE_REVIEW_LLM_API_KEY`: 사내 Qwen/vLLM이 API key를 요구할 때만 넣습니다. 필요 없으면 비워둡니다.
- `PE_REVIEW_GERRIT_TOKEN`: Bearer token 방식을 쓸 때만 넣습니다. Basic을 쓰면 비워둡니다.

저장 후 권한을 줄입니다.

```bash
chmod 600 .env
```

### 8단계. `config.yaml` 작성

```bash
nano config.yaml
```

처음에는 예제 전체를 지우지 말고, 아래 값만 실제 환경에 맞게 수정하는 것이 안전합니다.

```yaml
gerrit:
  ssh_host: "GERRIT_HOST"
  ssh_port: 29418
  ssh_user: "GERRIT_USER"
  ssh_key_path: "/run/secrets/gerrit_ssh_key"
  known_hosts_path: "/run/secrets/gerrit_known_hosts"
  strict_host_key_checking: true
  rest_url: "http://GERRIT_WEB_HOST:PORT"
  rest_auth:
    mode: "basic"
    username: "GERRIT_USER"
    password_env: "PE_REVIEW_GERRIT_HTTP_PASSWORD"
  projects: []

llm:
  base_url: "http://QWEN_HOST:PORT/v1"
  model: "실제_서빙_모델명"
  api_key_env: "PE_REVIEW_LLM_API_KEY"

review:
  output_language: "ko-KR"
```

나머지 `database`, `repos`, `retry`, `service`, `admin` 값은 첫 설치에서는 예제 기본값을 그대로
유지해도 됩니다.

`projects: []`로 두면 리뷰 대상 project는 나중에 Admin Web에서 추가할 수 있습니다.

### 9단계. Gerrit SSH key를 배포용으로 복사

host에 이미 다음 파일이 있다고 가정합니다.

```text
~/.ssh/id_ed25519_gerrit
~/.ssh/known_hosts
```

배포용 복사본을 만듭니다.

```bash
cd ~/pe-review-agent/deploy
mkdir -p secrets

cp ~/.ssh/id_ed25519_gerrit secrets/gerrit_ssh_key
cp ~/.ssh/known_hosts secrets/gerrit_known_hosts
```

여기서 `chown 10001` 같은 명령은 직접 실행하지 않습니다.

확인만 합니다.

```bash
ls -l secrets/gerrit_ssh_key secrets/gerrit_known_hosts
```

### 10단계. Docker를 띄우기 전에 Gerrit SSH 연결 확인

아래에서 `GERRIT_USER`, `GERRIT_HOST`만 실제 값으로 바꿉니다.

```bash
ssh -T \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile="$HOME/.ssh/known_hosts" \
  -i "$HOME/.ssh/id_ed25519_gerrit" \
  -p 29418 \
  GERRIT_USER@GERRIT_HOST \
  gerrit version
```

Gerrit 버전이 출력되면 SSH key/user/host/port는 정상입니다.

그 다음 `Stream Events` 권한을 확인합니다.

```bash
timeout 5 ssh -T \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile="$HOME/.ssh/known_hosts" \
  -i "$HOME/.ssh/id_ed25519_gerrit" \
  -p 29418 \
  GERRIT_USER@GERRIT_HOST \
  gerrit stream-events -s patchset-created
```

5초 동안 별도 오류 없이 기다리다 `timeout`으로 끝나면 정상입니다.

`stream events not permitted`가 나오면 SSH 설정 문제가 아니라 Gerrit의 global `Stream Events`
권한이 없는 것입니다. 관리자에게 권한을 요청한 뒤 계속 진행하세요.

### 11단계. 서비스 설치 및 Docker Compose 실행

이제 처음으로 실제 서비스를 띄웁니다.

```bash
cd ~/pe-review-agent/deploy
sudo ./install.sh
```

이 명령 하나가 다음을 처리합니다.

```text
배포용 SSH key 권한 정리
DB migration
PostgreSQL 기동
receiver 기동
worker 기동
reconciler 기동
Admin Web 기동
```

host Linux 사용자의 UID가 1002여도 그대로 두면 됩니다. `install.sh`가 container가 읽는
`deploy/secrets/` 복사본만 필요한 권한으로 맞춥니다.

### 12단계. container 상태 확인

```bash
cd ~/pe-review-agent/deploy
sudo docker compose ps -a
```

정상적인 상태는 대략 다음과 같습니다.

```text
postgres      Up / healthy
migrate       Exited (0)
receiver      Up
worker        Up
reconciler    Up
admin         Up
```

`migrate`가 `Exited (0)`인 것은 실패가 아니라 정상입니다.

### 13단계. Admin Web 접속

브라우저에서 다음 주소를 엽니다.

```text
http://리뷰봇서버IP:8080/
```

로그인 정보:

```text
username: config.yaml의 admin.username (기본 admin)
password: .env의 PE_REVIEW_ADMIN_PASSWORD
```

### 14단계. Admin Web에서 연결 확인

Admin Web의 **Connections** 화면에서 다음을 순서대로 확인합니다.

```text
Gerrit REST
Gerrit SSH
Stream Events
LLM
```

하나라도 실패하면 바로 project를 활성화하지 말고 해당 연결부터 고칩니다.

### 15단계. 첫 리뷰 project 하나만 추가

리뷰 대상 repo에서 다음 명령으로 Gerrit remote를 확인합니다.

```bash
git remote get-url origin
```

예를 들어:

```text
ssh://developer@gerrit.example.internal:29418/sw_product/example/repo
```

라면 Admin Web의 **Projects**에 넣을 값은 전체 URL이 아니라 다음 project path만입니다.

```text
sw_product/example/repo
```

처음에는 다음처럼 설정하는 것을 권장합니다.

```text
Scope: From now on
Enabled: Yes
```

`From now on`은 이미 열려 있던 수천~수만 개의 Change를 한꺼번에 리뷰하지 않고, 활성화 이후의
새 Patch Set부터 리뷰합니다.

### 16단계. 첫 Patch Set으로 실제 동작 확인

테스트용 Change에 새 Patch Set 하나를 올린 뒤 Admin Web의 **Jobs**에서 상태가 진행되는지 확인합니다.

정상 흐름은 대략 다음과 같습니다.

```text
RECEIVED
→ FETCHING / REVIEWING
→ READY_TO_PUBLISH / PUBLISHING
→ PUBLISHED
```

Gerrit Change 화면에서도 다음 두 가지가 보여야 합니다.

```text
Change-level review summary
코드 라인에 붙은 native inline/range comment
```

### 17단계. 안 되면 먼저 이 로그만 확인

```bash
cd ~/pe-review-agent/deploy

sudo docker compose logs --tail=200 admin
sudo docker compose logs --tail=200 receiver
sudo docker compose logs --tail=200 worker
sudo docker compose logs --tail=200 reconciler
```

전체 로그를 실시간으로 보고 싶을 때만 다음을 사용합니다.

```bash
sudo docker compose logs -f
```

### 설치가 끝난 뒤 기억할 명령 4개

상태 보기:

```bash
cd ~/pe-review-agent/deploy
sudo docker compose ps -a
```

로그 보기:

```bash
sudo docker compose logs --tail=200
```

서비스 재시작:

```bash
sudo docker compose restart
```

서비스 내리기:

```bash
sudo docker compose down
```

`docker compose down`은 container를 내리지만 named volume의 PostgreSQL 데이터는 기본적으로 삭제하지
않습니다. 데이터까지 삭제하는 `docker compose down -v`는 운영 중에는 사용하지 마세요.

---

## 0. 제일 먼저: 이 서비스는 Docker Compose로 실행합니다

이 프로젝트는 기본적으로 bare Python 프로세스를 직접 띄우는 방식이 아니라 **Docker Compose**로
실행합니다. 즉 운영 서버에는 최소한 다음 두 명령이 동작해야 합니다.

```bash
docker --version
docker compose version
```

둘 다 버전이 출력되면 준비된 상태입니다.

Docker가 설치되어 있지 않다면 회사 패키지 미러/서버 정책에 맞춰 **Docker Engine + Compose v2**를
먼저 설치해야 합니다. Ubuntu 계열에서 흔히 사용하는 패키지는 `docker.io`와 Compose v2
plugin이지만, 사내 apt mirror에 따라 정확한 패키지 이름은 다를 수 있습니다.

설치 후에는 daemon 상태도 확인합니다.

```bash
sudo systemctl enable --now docker
sudo docker info
```

### 이 프로젝트에서 Docker가 하는 일

`docker-compose.yml`은 다음 container를 같이 띄웁니다.

```text
postgres     PostgreSQL durable DB
migrate      DB migration을 한 번 수행하고 종료
receiver     Gerrit stream-events 수신
worker       repo fetch + Qwen review + Gerrit publish
reconciler   누락 event / ambiguous publish 복구
admin        :8080 Web UI
```

`migrate`가 `Exited (0)`으로 보이는 것은 정상입니다. migration을 한 번 끝내고 종료하는 one-shot
container입니다.

### 이미 `pe-review-agent` Linux 계정이 있는 서버라면

`bootstrap-host.sh`는 **완전히 새 서버에서 UID 10001의 host 계정까지 새로 만드는 경우만** 위한
선택 스크립트입니다. 이미 `pe-review-agent` 계정이 존재하고 UID가 1002 같은 다른 값이라면
`bootstrap-host.sh`를 실행하지 마세요.

host 사용자 UID와 container 내부 UID는 같을 필요가 없습니다.

```text
host Linux account
pe-review-agent (예: UID 1002)

Docker container 내부 reviewer
pe-review-agent (UID 10001)
```

현재 배포에서 UID 10001이 중요한 곳은 container가 읽는 **배포용 SSH key 복사본**입니다.
host의 `~/.ssh/id_ed25519_gerrit` 원본 ownership을 10001로 바꾸는 것이 아닙니다.

### 가장 단순한 실행 순서

repo를 직접 clone해서 서버에서 build할 수 있는 환경이라면 전체 흐름은 다음입니다.

```text
1. Docker / docker compose 확인
2. reviewer Docker image 준비
3. PostgreSQL Docker image 준비
4. deploy/.env 작성
5. deploy/config.yaml 작성
6. deploy/secrets/에 SSH key 복사
7. sudo ./install.sh
8. docker compose ps 확인
9. http://서버IP:8080 접속
```

사내 서버가 인터넷에 연결되지 않는다면 2~3번을 target server에서 하지 않고, 인터넷 가능한
machine에서 release tarball을 만들어 옮기는 방식이 권장됩니다. 아래 15~16장에서 두 방식을 모두
설명합니다.

### 기존 `pe-review-agent` 계정이 UID 1002인 서버에서 그대로 따라하기

이미 Linux 계정 `pe-review-agent`가 있고 `id -u` 결과가 `1002`라면 **그 값은 정상이며 바꾸지
않습니다.** `bootstrap-host.sh`도 실행하지 않습니다. 아래 순서대로만 진행하면 됩니다.

먼저 source checkout이 이미 있다면 해당 repo로 이동합니다. 없다면 GitHub에 접근 가능한 환경에서
clone합니다.

```bash
cd ~
git clone https://github.com/devSejung/pe-review-agent.git
cd pe-review-agent
git checkout main
git pull
```

이미 clone되어 있다면 `git clone`은 생략하고 기존 repo로 들어가면 됩니다.

그 다음 배포 파일을 준비합니다.

```bash
cd deploy
cp env.example .env
cp ../config/config.example.yaml config.yaml

mkdir -p secrets
cp ~/.ssh/id_ed25519_gerrit secrets/gerrit_ssh_key
cp ~/.ssh/known_hosts secrets/gerrit_known_hosts
```

여기까지 한 직후에는 다음 결과가 나와도 정상입니다.

```text
host user pe-review-agent: UID 1002
deploy/secrets/gerrit_ssh_key owner: UID 1002
```

**이 단계에서 직접 `chown 10001`을 하지 않습니다.** 홈의 원본 SSH key도 절대 변경하지 않습니다.
나중에 `sudo ./install.sh`를 실행할 때 `deploy/secrets/`의 **복사본만** container UID 10001에 맞게
조정됩니다.

즉 다음 명령으로 현재 상태만 확인합니다.

```bash
pwd
ls -al
ls -ln secrets
```

이 세 명령 결과를 확인한 다음 `.env`와 `config.yaml`을 채우고 Docker image를 준비하는 다음
단계로 진행합니다.

처음 설정하는 동안에는 한 번에 모든 단계를 수행하지 말고, 위 세 명령의 출력부터 확인한 뒤
다음 단계로 넘어갑니다.

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
ssh://developer@gerrit.example.internal:29418/platform/dmc-fw
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
  # 기본 8. 실제 repo 탐색량이 많으면 16 전후부터 조정하고, 무작정 크게 올리지 않습니다.
  max_tool_rounds: 8
  # 불필요한 탐색을 제한하는 운영 예산. 장애 재실행 비용을 포함한 과금 hard cap은 아님
  max_candidate_chunks: 12
  max_llm_calls_per_job: 30
  max_tool_calls_per_job: 50
  verifier_budget_fraction: 0.3333333333333333
  # 토큰은 참고 정보만 표시. 기존 max_input_tokens_per_job 설정은 허용하지만 무시함
  # DONE/SUPERSEDED job의 candidate/verifier progress 보존 기간
  chunk_checkpoint_retention_days: 30

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

`max_candidate_chunks`, `max_llm_calls_per_job`, `max_tool_calls_per_job`은 과도한 코드 탐색과
불필요한 반복을 제한하는 **운영 예산**입니다. 전체 비용을 영구적으로 묶는 과금 상한은 아닙니다.
정상적으로 예산에 도달하면 job을 무작정 retry하지 않고, 검증된 결과만 게시하며 미검토/미검증
범위와 제한 사유를 Gerrit summary 및 Job Audit에 표시합니다.

`verifier_budget_fraction`은 verifier를 위해 보호하는 LLM/tool 예산 비율입니다. 초기값은 1/3이며
실제 FW CR로 조정해야 합니다. 예를 들어 LLM 한도 30이면 candidate가 사용할 수 있는 범위는 20회,
verifier 보호분은 10회입니다. candidate가 8회에 끝나면 verifier는 남은 22회를 사용할 수 있습니다.
tool 한도 50에서는 올림하여 17회를 보호하고 candidate에 최대 33회를 허용합니다.

각 chunk에 같은 횟수를 고정 배정하지 않습니다. candidate와 verifier 각각 내부에서 순환 실행하며
한 번에 LLM 요청 하나 또는 tool 실행 하나를 진행합니다. 쉬운 chunk가 빨리 끝나면 나머지에 예산이
돌아갑니다. 새 탐색 session의 첫 호출과 최종 응답 여력을 확보할 수 있는 범위만 시작합니다.
마지막 한 번만 가능하면 도구 없는 제한된 검토를 수행하며, 시작조차 불가능한 범위는 미검토입니다.
각 session의 마지막 호출은 처음부터 tools 없이 최종 JSON을 요청합니다. 필요한 탐색이 제한된
결과는 JSON이 유효해도 부분 검토 상태를 유지하고 이전 finding의 해결 판정에 사용하지 않습니다.

토큰 사전 계산, 토큰 예약, 누적 토큰에 의한 호출 차단은 사용하지 않습니다. 기존
`max_input_tokens_per_job` 키는 업그레이드 호환 목적으로 읽지만 동작에는 영향을 주지 않습니다.
API가 반환한 토큰 수는 Audit 참고 정보이며, 응답을 못 받은 요청의 토큰 수를 0으로 확정하지 않습니다.
모델의 출력 길이 제한과 context 초과 시 분할은 별개의 안전장치로 유지됩니다.

Admin Web의 **Settings → Review budget**에서도 같은 값을 저장할 수 있습니다. Review policy/budget은
worker가 시작될 때 읽으므로 저장 직후에는 기존 process에 적용되지 않으며, 화면의
**Restart required** 안내대로 `receiver` / `worker` / `reconciler`를 재시작해야 적용됩니다.

candidate chunk와 verifier batch 모두 완료 결과를 PostgreSQL의 compact progress에 저장합니다.
결과, 경로/hash, 사용량, 제한 사유, context-limit 분할 결정과 검증 대상 후보 집합을 저장하며 **전체
diff, prompt, tool result 전문, reasoning transcript를 중복 저장하지 않습니다.** 모델/정책/근거 context
등이 동일할 때만 재사용합니다. verifier에 진입하기 전에 후보 집합과 candidate 범위를 확정하므로,
검증 중 장애 후 복원하면서 candidate 탐색을 다시 열어 검증 대상을 바꾸지 않습니다.

재시작 계산 예시는 `완료 A 5회 + 완료 B 4회 + 미완료 C 6회 후 장애`입니다. A/B는 결과를 재사용하고
9회를 계속 차감합니다. C는 처음부터 다시 수행하므로 폐기된 6회는 재실행 예산에서 제외합니다.
전체 한도 30에서 재실행에 남는 몫은 21회이며, 이 안에서도 verifier 보호분을 유지합니다. 이미 쓴
C의 6회는 실제 호출 감사 기록에 그대로 남습니다. 정상적인 예산/round 제한 종료는 장애로 취급해
환급하지 않습니다. 반복 timeout/잘못된 응답도 `retry.review_attempts`로 제한합니다.

최종 응답에서도 유효한 JSON을 못 받거나 도구를 다시 요청하면 성공/문제없음으로 처리하지 않습니다.
그 미완료 작업은 재시도 대상이며, 재시도도 소진하면 실패가 표시됩니다. 도구 실행 금지는 코드로
강제하지만 JSON 의미나 FW 결함 판정의 정확성까지 보장하는 것은 아닙니다.

Job Audit의 **Review progress / checkpoints**는 완료/분할/제한 상태를, **Actual invocation audit**는
실패를 포함한 lifetime 요청 시도를 보여줍니다. 요청 전 intent를 저장하므로 `started`/unconfirmed는
진행 중이거나 종료되어 결과 확인이 안 된 요청입니다. 실제 전송 직전 중단됐을 수도 있습니다.
실패 재실행이 있으면 실제 총 요청 시도는 운영 예산보다 클 수 있습니다. 표시된 최신 100개를 넘어선
기록도 DB에는 남고 합계에는 포함됩니다.

업그레이드는 구 worker를 중지하고 migration 후 새 worker를 시작하십시오. 구 #16의 v1 checkpoint에는
모델/context와 제한 상태 증명이 없으므로 새 엔진이 그 결과를 완전한 검토로 추정해 재사용하지 않습니다.
기존 행과 알려진 사용량은 Audit에 보존하지만, 결과 자체를 재사용하지 않으므로 새 operational budget에서
차감하지 않습니다. 즉 새 엔진은 해당 범위를 다시 검토하고, 새 v2 결과/제한 상태로 완료 여부를 판단합니다.
예전 집계에 섞인 실패 비용은 정확히 분리할 수 없습니다. 기존에 게시 완료된 review를 업데이트 때문에
자동 재게시하지 않습니다.

progress는 장애복구용 cache입니다. 기본 `chunk_checkpoint_retention_days: 30`으로 DONE/SUPERSEDED
job의 오래된 cache를 reconciler가 정리합니다. 실제 호출 audit은 job 생명주기 동안 보존합니다.
`FAILED_PERMANENT`는 수동 requeue 가능성이 있어 cache 자동 정리에서 제외합니다. 정상 예산 종료 후
게시까지 완료된 `DONE`은 기존 실패-job requeue 대상이 아니며, 예산을 올렸다고 자동 재검토하지 않습니다.

### 향후 선택 가능한 방향: Jenkins build metadata 기반 C semantic evidence

현재 reviewer는 각 FW repo의 build command를 필수로 알지 않아도 동작합니다. 이 원칙은 유지합니다.
향후 실제 DMC/FW replay에서 검출력 향상이 충분히 확인될 경우에만, **선택 기능**으로 compiler/build-aware
C semantic evidence를 추가하는 방향을 고려할 수 있습니다. 이 기능이 없어도 지금의 diff + repository tool
+ Qwen candidate/verifier 리뷰는 그대로 동작해야 하며, 새 project를 enable할 때 build command 입력을
필수 onboarding 절차로 만들지 않습니다.

현실적인 연결점은 기존 Jenkins입니다. Jenkins가 reviewer와 다른 서버에서 실행되어도 상관없으며,
성공 build가 다음과 같은 작은 artifact를 남기면 reviewer가 필요할 때 가져와 사용할 수 있습니다.

```text
metadata.json
  project / branch / revision SHA / target / build number

compile_commands.json
  source file별 실제 compiler / -D / -I / target option

generated headers (선택)
  semantic 분석에 정말 필요한 경우에만 추가
```

우선순위는 `동일 revision artifact > 호환 가능한 동일 branch의 최근 성공 build > semantic evidence 없음`
순으로 생각합니다. 정확히 일치하지 않는 baseline artifact를 사용할 때는 provenance를 명확히 남겨야 하며,
현재 Patch Set의 compile option이 달라졌을 가능성을 무시해서는 안 됩니다. 아무 artifact도 없으면 semantic
분석만 생략하고 기존 reviewer로 정상 진행합니다.

이 방향의 목적은 Jenkins나 전체 FW toolchain을 review 서버로 옮기는 것이 아닙니다. Jenkins가 이미 알고
있는 build 정보를 재사용해 type/macro/configuration/call/data-flow 같은 C 의미 근거를 reviewer에 공급하고,
Qwen이 그 근거를 diff와 domain context에 맞춰 triage/검증하는 구조입니다. Static analyzer 경고를 그대로
Gerrit에 게시하는 구조는 피합니다.

초기 적용 대상은 DMC가 될 수 있지만 core에 DMC build command를 박지 않습니다. 다른 FW repo와 branch로
확장 가능하도록 optional adapter/profile 형태를 전제로 하며, 실제 도입 여부와 투자 범위는 historical DMC
CR/버그 replay에서 recall, precision, false-positive, latency 개선폭을 확인한 뒤 결정합니다.

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

`.env`는 **`docker-compose.yml`과 같은 배포 디렉터리**에 둡니다.

source checkout 기준:

```text
gerrit-ai-reviewer/
└── deploy/
    ├── docker-compose.yml
    ├── install.sh
    ├── config.yaml
    ├── .env                  <- 여기
    └── secrets/
        ├── gerrit_ssh_key
        └── gerrit_known_hosts
```

release tarball을 사용하는 경우에도 동일하게 **압축을 푼 디렉터리의 `install.sh` 옆**에
`.env`, `config.yaml`, `secrets/`가 위치합니다.

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

이미 host의 `~/.ssh`에 Gerrit key가 있다면 **원본은 그대로 두고 배포용 복사본을 만듭니다.**

예:

```bash
cd deploy
mkdir -p secrets

cp ~/.ssh/id_ed25519_gerrit secrets/gerrit_ssh_key
cp ~/.ssh/known_hosts secrets/gerrit_known_hosts
```

그 후 `sudo ./install.sh`를 실행하면 현재 install script가 배포용 복사본의 ownership을 container
UID 10001로 맞춥니다. `~/.ssh/id_ed25519_gerrit` 원본 ownership은 변경하지 않습니다.

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

## 15. Docker image 준비 방법

설치 방식은 두 가지입니다.

### 방법 A. 서버에서 직접 source를 build할 수 있는 경우

target server가 public registry/PyPI에 접근 가능하거나 필요한 package/image가 이미 mirror에 있다면
repo root에서 reviewer image를 직접 build할 수 있습니다.

```bash
cd ~/gerrit-ai-reviewer
sudo docker build -t gerrit-ai-reviewer:local .
```

사내 PyPI mirror가 필요한 경우:

```bash
sudo docker build \
  --build-arg PIP_INDEX_URL="PYPI_MIRROR_URL" \
  --build-arg PIP_TRUSTED_HOST="PYPI_MIRROR_HOST" \
  -t gerrit-ai-reviewer:local .
```

사내망에서 Debian apt도 mirror를 사용해야 한다면:

```bash
sudo docker build \
  --build-arg PIP_INDEX_URL="PYPI_MIRROR_URL" \
  --build-arg PIP_TRUSTED_HOST="PYPI_MIRROR_HOST" \
  --build-arg APT_DEBIAN_MIRROR_URL="DEBIAN_MIRROR_URL" \
  --build-arg APT_DEBIAN_SECURITY_MIRROR_URL="DEBIAN_SECURITY_MIRROR_URL" \
  -t gerrit-ai-reviewer:local .
```

Docker base image는 Debian이므로 host의 Ubuntu codename(`resolute` 등)을 이 값에 사용하지 않습니다.

Compose는 PostgreSQL image를 `pe-review-postgres:16.15`라는 local tag로 사용합니다. 현재 release
builder와 동일한 pinned PostgreSQL image를 준비하려면:

```bash
sudo docker pull \
  postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94

sudo docker tag \
  postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94 \
  pe-review-postgres:16.15
```

이미지가 준비됐는지 확인:

```bash
sudo docker images | grep -E 'gerrit-ai-reviewer|pe-review-postgres'
```

최소 다음 두 image가 보여야 합니다.

```text
gerrit-ai-reviewer   local
pe-review-postgres   16.15
```

### 방법 B. 사내 서버가 인터넷이 안 되는 경우 - 권장

인터넷 가능한 machine에서 repo root 기준으로:

```bash
./deploy/build-release.sh 0.1.0
```

release를 만드는 machine도 public PyPI 대신 사내 PyPI mirror를 써야 한다면:

```bash
PIP_INDEX_URL="PYPI_MIRROR_URL" \
PIP_TRUSTED_HOST="PYPI_MIRROR_HOST" \
./deploy/build-release.sh 0.1.0
```

release build에서도 Debian apt mirror가 필요한 경우 같은 환경변수를 함께 넘길 수 있습니다.

```bash
PIP_INDEX_URL="PYPI_MIRROR_URL" \
PIP_TRUSTED_HOST="PYPI_MIRROR_HOST" \
APT_DEBIAN_MIRROR_URL="DEBIAN_MIRROR_URL" \
APT_DEBIAN_SECURITY_MIRROR_URL="DEBIAN_SECURITY_MIRROR_URL" \
./deploy/build-release.sh 0.1.0
```

이 script가 자동으로:

1. reviewer Docker image build
2. pinned PostgreSQL image pull/tag
3. 두 image를 tar로 저장
4. Compose/install/config 예제 포함
5. SHA256SUMS 생성
6. release tar.gz 생성

까지 수행합니다.

결과 예:

```text
release/gerrit-ai-reviewer-0.1.0.tar.gz
```

이 파일만 사내 Linux host로 옮깁니다.

사내 서버에서:

```bash
tar xzf gerrit-ai-reviewer-0.1.0.tar.gz
cd gerrit-ai-reviewer-0.1.0
```

release 내부는 대략 다음 형태입니다.

```text
gerrit-ai-reviewer-0.1.0/
├── docker-compose.yml
├── install.sh
├── .env.example
├── config.example.yaml
├── docker-images/
│   ├── gerrit-ai-reviewer.tar
│   └── postgres-16.tar
└── secrets/
```

target server는 PyPI/public Docker registry에 접속할 필요가 없습니다. `install.sh`가 image tar를
`docker load`합니다.

---

## 16. 실제 설치 및 실행

### A. source checkout에서 직접 실행하는 경우

repo root에서 image 준비를 끝낸 뒤:

```bash
cd deploy

cp env.example .env
cp ../config/config.example.yaml config.yaml

mkdir -p secrets
cp ~/.ssh/id_ed25519_gerrit secrets/gerrit_ssh_key
cp ~/.ssh/known_hosts secrets/gerrit_known_hosts
```

`.env`와 `config.yaml`을 실제 환경에 맞게 수정합니다.

그 다음:

```bash
sudo ./install.sh
```

### B. offline release에서 실행하는 경우

압축을 푼 release 디렉터리에서:

```bash
cp .env.example .env
cp config.example.yaml config.yaml

mkdir -p secrets
cp ~/.ssh/id_ed25519_gerrit secrets/gerrit_ssh_key
cp ~/.ssh/known_hosts secrets/gerrit_known_hosts
```

`.env`, `config.yaml` 수정 후:

```bash
sudo ./install.sh
```

`install.sh`가 수행하는 실제 작업은 다음입니다.

```text
1. release라면 SHA256SUMS 검증
2. release라면 Docker image tar를 docker load
3. config.yaml / .env / SSH secret 존재 확인
4. 배포용 SSH key ownership/mode 정리
5. docker compose up -d
```

즉 별도로 `docker compose up -d`를 다시 칠 필요는 없습니다.

### 기동 상태 확인

반드시 `docker-compose.yml`이 있는 디렉터리에서:

```bash
sudo docker compose ps
```

정상적인 모습은 대략 다음과 같습니다.

```text
postgres      Up / healthy
migrate       Exited (0)
receiver      Up
worker        Up
reconciler    Up
admin         Up
```

`migrate`의 `Exited (0)`은 정상입니다.

### 처음 기동했는데 문제가 있으면

```bash
sudo docker compose logs --tail=200 migrate
sudo docker compose logs --tail=200 admin
sudo docker compose logs --tail=200 receiver
sudo docker compose logs --tail=200 worker
sudo docker compose logs --tail=200 reconciler
```

전체를 실시간으로 보려면:

```bash
sudo docker compose logs -f
```

Admin health 확인:

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/readyz
```

둘 다 정상이라면 브라우저에서 `http://SERVER_IP:8080/`으로 접속하면 됩니다.

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

- **Needs attention**: 영구 실패, 지연된 retry, 만료/누락 lease, 게시 대기 및 ambiguous publication
- receiver / worker / reconciler / admin 실제 heartbeat와 Healthy/Stale/Down 상태
- 각 process의 실행 version, Git SHA, 시작 시각, 마지막 heartbeat
- 현재 config generation과 process별 적용 generation
- 부분 재시작으로 인한 mixed revision / settings fingerprint 경고
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
- 현재 연결값이 `config.yaml`인지 saved Admin override인지 확인
- **Use config.yaml values**로 Gerrit/LLM Admin override 제거
- REST auth가 필요한 경우 실제 secret 값이 아니라 필요한 env 이름과 configured/missing 상태 확인

connection metadata 변경은 저장 후 receiver/worker/reconciler restart가 필요합니다.
Admin Web에서 직접 저장한 항목만 DB override가 되며, 저장하지 않은 connection 값은 계속
`config.yaml`을 따릅니다. `Use config.yaml values`를 누른 뒤에도 long-lived client에는 restart가
필요합니다.

restart가 필요한 저장/reset은 PostgreSQL의 `config generation`을 증가시킵니다. 각 process가 자신이
실제로 읽은 generation과 secret을 제외한 settings fingerprint를 heartbeat에 기록하므로, 단순 안내
문구가 아니라 **어느 process가 아직 이전 설정인지** Dashboard 상단 경고에서 확인할 수 있습니다.
receiver/worker/reconciler가 모두 현재 generation을 보고할 때까지 경고는 사라지지 않습니다.

settings fingerprint는 각 process가 **이미 읽은 값**을 뜻하며 file watcher는 아닙니다. 모든 process가
계속 실행 중인 동안 `config.yaml`만 직접 수정하면 즉시 감지할 수 없고, 하나 이상의 process가
재시작해 새 fingerprint를 보고한 시점부터 drift가 표시됩니다. Admin Web 저장은 즉시 generation을
올리므로 이 경우에는 재시작 전부터 pending process가 표시됩니다.

이전 버전에서 업그레이드한 DB에는 과거의 전체 config snapshot이 남아 있을 수 있습니다.
현재 `config.yaml`과 완전히 같은 legacy section은 시작 시 자동 정리합니다. 값이 다른 snapshot은
과거 Admin 수정일 수도 있으므로 임의 삭제하지 않고 Connections에 경고를 표시합니다. 이 경우
현재 파일을 확인한 뒤 YAML을 기준으로 쓸 것이 맞으면 **Use config.yaml values**를 한 번 눌러
Gerrit/LLM snapshot을 명시적으로 제거합니다.

같은 방식으로 legacy `review` snapshot이 남아 있으면 **Settings** 상단에 경고와
**Use config.yaml values** 버튼이 표시됩니다. 이 버튼은 review-policy override만 제거합니다.

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
- stage별 성공/실패/running/abandoned
- 전체 error text
- REVIEW attempt의 repository tool trace(round/phase/tool/args/result preview)
- duplicate tool suppression 및 forced finalization 사유
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

Change-level summary는 **변경 요약**과 **리뷰 결과**를 분리합니다. finding이 0건이어도
변경 요약은 유지됩니다.

`ko-KR`의 0-finding 예:

```text
변경 요약
- LPDDR PHY register init table과 training sequence를 갱신합니다.
- 관련 register mapping을 함께 수정합니다.

리뷰 결과
- 추가로 조치가 필요한 펌웨어 동작상 문제는 발견되지 않았습니다.
```

리뷰 모델에는 Gerrit Change `subject`, target `branch`, 현재 Patch Set의 bounded commit message도
전달합니다. 이 metadata는 작성자 의도를 이해하기 위한 **참고 정보**일 뿐이며, 오래되거나
부정확할 수 있으므로 실제 변경 내용은 Patch Set diff를 authoritative source로 사용합니다.
commit message와 Change metadata는 prompt instruction이 아닌 untrusted data로 취급합니다.

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

가장 먼저 **Needs attention**을 봅니다. 실패뿐 아니라 다음을 자동으로 구분합니다.

- `FAILED_PERMANENT`
- 예정 시각을 넘긴 `RETRY_WAIT`
- lease가 만료되거나 사라진 FETCHING/REVIEWING/VALIDATING/PUBLISHING
- 오래 기다리는 RECEIVED/READY_TO_PUBLISH
- 장시간 해소되지 않은 ambiguous Gerrit publication
- receiver/worker/reconciler/admin heartbeat stale/down
- config generation 미적용 및 서로 다른 Git SHA 실행

의도적으로 global pause했거나 project를 disable한 경우 일반 queue/lease 지연은 장애로 표시하지
않습니다. 다만 영구 실패와 외부 게시 불확실성은 계속 표시합니다.

### Jobs

문제 Change의 current state와 last error를 확인합니다.

### Job Audit

다음을 봅니다.

- 어느 stage에서 실패했는지
- attempt가 몇 번 발생했는지
- exact error text
- review result가 이미 만들어졌는지
- candidate chunk checkpoint가 몇 개 저장/재사용됐는지
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

### `repository tool-call rounds` 반복 / tool loop

과거처럼 `max_tool_rounds`만 계속 키우는 방식으로 대응하지 않습니다.

- Job Audit의 **Repository tool trace**에서 실제 `read_file`, `search_text`, `list_files` 호출을 확인
- 동일 tool + 동일 args 반복은 두 번째부터 자동 suppress됨
- 한 round 전체가 duplicate이면 즉시 tool 사용을 중단하고 final JSON 생성을 강제
- round budget을 다 써도 리뷰 전체를 transient failure로 버리지 않고, tools를 제거한 마지막
  LLM 호출에서 지금까지 확보한 근거만으로 final JSON을 생성
- 운영값은 8~16 정도에서 시작해 실제 trace를 보고 조정하고, 설정상 최대값은 64

따라서 작은 테스트 commit에서 8/16 round를 반복 소진한다면 단순히 64로 올리기보다 먼저
Job Audit trace에서 어떤 탐색을 반복했는지 확인합니다.

큰 register dump / CSV도 `read_file`에서 파일 전체를 모델 context로 올리지 않습니다.
`search_text`로 register/symbol 위치를 찾은 뒤 필요한 line range만 스트리밍해서 읽고,
한 번의 tool 결과는 `review.max_tool_output_bytes`(기본 256 KB)로 제한됩니다. 따라서 수 MB급
텍스트 파일이라고 해서 파일 전체 크기만으로 `file exceeds context size limit` 처리하지 않습니다.
`max_context_file_bytes`는 repository policy/architecture 같은 정적 review guidance를 읽을 때의
per-file 제한으로 계속 사용됩니다.

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

Dashboard의 process 상태는 Docker socket을 읽는 방식이 아니라 각 process가 PostgreSQL에 15초마다
남기는 heartbeat를 사용합니다. 60초 이상 갱신되지 않으면 stale로 표시합니다. heartbeat에는
component/instance/version/Git SHA/start/last-seen/applied config generation이 포함되며 secret 값은
포함하지 않습니다. 30일보다 오래된 heartbeat 이력은 service 시작 시 정리됩니다.

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

UI는 restart 필요 여부뿐 아니라 config generation을 표시하고, receiver/worker/reconciler 중 실제로
새 설정을 읽지 않은 process 이름을 계속 보여줍니다.

---

## 41. 운영자가 기억해야 할 핵심 8개

1. **새 repo 기본은 From now on**이다.
2. **기존 open Change backfill은 명시적으로만 켠다.**
3. Gerrit은 **SSH + REST 둘 다** 필요하다.
4. `Stream Events`는 Gerrit global capability다.
5. bot에는 Submit/+2 권한이 필요 없다.
6. 장애가 나면 **Dashboard Needs attention -> Jobs Audit -> Logs** 순서로 본다.
7. PostgreSQL은 cache가 아니라 **durable state**다.
8. 처음 production 적용은 **test repo 하나**로 E2E 검증 후 확대한다.

---

## 42. 관련 문서

- `README.md` — 전체 프로젝트 개요와 production quick start
- `docs/runbook.md` — 운영/장애 대응 runbook
- `docs/oss-engine-notes.md` — OSS reviewer 검토 기록
- `config/config.example.yaml` — 전체 설정 예제
- `deploy/env.example` — secret/environment 예제
