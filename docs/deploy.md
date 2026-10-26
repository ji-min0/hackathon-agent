# 배포

앱은 Docker 이미지 하나로 돈다. 서버 앞에는 Caddy(웹 서버)가 HTTPS 를 붙인다.
DB 는 Supabase 를 그대로 쓰므로 서버를 바꿔도 데이터 이전이 없다.

```
브라우저 ─HTTPS→ Caddy(80·443, 인증서 자동 발급) ─HTTP→ app(uvicorn 8000) ─→ Supabase
```

- **AWS(현재)**: CloudFormation 스택 하나로 서버·IP·방화벽·CD 권한을 만들고, main 에 push 하면
  GitHub Actions 가 자동 배포한다.
- **NCP**: 서버를 손으로 만들고 같은 Docker Compose 로 띄운다(아래 "NCP 로 옮기기").

## 현재 배포

| 항목 | 값 |
|---|---|
| 주소 | https://43.203.94.29 (관리자: `/admin`) — Let's Encrypt IP 인증서 |
| 스택 | CloudFormation `hackathon-agent` (서울), 템플릿 `deploy/aws/ec2-oidc-cfn.yml` |
| 서버 | m7i-flex.large(2vCPU/8GB, 무료 티어 대상), Ubuntu 24.04, 30GB |
| SSH | `ssh -i ~/.ssh/hackathon-agent-key.pem ubuntu@43.203.94.29` (22번은 등록한 IP 만) |
| CD 권한 | IAM 역할 `hackathon-agent-github-deploy`(GitHub OIDC, main 브랜치만) |

## AWS: 처음부터 만들기

### 1. 스택 만들기 (콘솔)

CloudFormation → 스택 생성 → 템플릿 파일 업로드 → `deploy/aws/ec2-oidc-cfn.yml`

| 파라미터 | 값 |
|---|---|
| `AppName` | `hackathon-agent` (서버 폴더·태그·역할 이름에 쓰인다) |
| `SSHAllowedCidr` | 내 IP + `/32` (예: `121.135.181.35/32`) |
| `CreateOIDCProvider` | `true`. 계정에 GitHub OIDC 공급자가 이미 있으면 `false` |
| 나머지 | 기본값 |

마지막 화면에서 "사용자 지정 이름으로 IAM 리소스를 생성할 수 있음"을 체크하고 제출한다.
스택이 만드는 것:

- EC2 + 탄력적 IP + 보안 그룹(22: 내 IP, 80·443: 전체)
- 첫 부팅 때 Docker·make 설치, 레포를 `/home/ubuntu/<AppName>` 으로 clone
  (끝나면 `~/<AppName>.bootstrap-done` 이 생긴다. 로그: `/var/log/cloud-init-output.log`)
- EC2 용 SSM 역할 `<AppName>-ec2`, GitHub Actions 용 역할 `<AppName>-github-deploy`, OIDC 공급자

출력 탭에서 `PublicIp`, `DeployRoleArn` 을 확인한다.

### 2. GitHub Secret

```bash
gh secret set AWS_ROLE_ARN -b <출력의 DeployRoleArn>
```

### 3. 서버에 `.env` 올리기

`.env` 는 저장소에 없으므로 로컬에서 복사하고, 접속 주소 두 줄을 붙인다.

```bash
scp -i ~/.ssh/hackathon-agent-key.pem .env ubuntu@<IP>:hackathon-agent/.env
ssh -i ~/.ssh/hackathon-agent-key.pem ubuntu@<IP> \
  "chmod 600 hackathon-agent/.env && printf 'DOMAIN=<IP>\nACME_PROFILE=shortlived\n' >> hackathon-agent/.env"
```

- `docker compose` 는 같은 폴더의 `.env` 에서 `DOMAIN`·`ACME_PROFILE` 을 읽어 Caddy 에 넘긴다
  (앱은 모르는 키를 무시한다). `COOKIE_SECURE` 는 compose 가 `true` 로 덮어쓴다.
- `docker compose` 는 값 뒤 주석(`KEY=40 # 설명`)을 지워서 읽지만 `docker run --env-file` 은
  주석까지 값으로 읽는다. 주석은 값과 다른 줄에 둔다.

### 4. 배포

main 에 push 하면 CD 가 배포한다. 손으로 하려면 Actions → CI/CD → Run workflow, 또는 서버에서
`cd ~/hackathon-agent && make deploy`.

### 지우기

CloudFormation 에서 스택을 삭제하면 서버·IP·역할·OIDC 공급자가 모두 지워진다.

## CI/CD (GitHub Actions)

`.github/workflows/ci-cd.yml`

- **CI**: 모든 push·PR 에서 `make check`(ruff 린트·포맷, pytest). 배포는 하지 않는다.
- **CD**: main push + CI 통과 시 배포한다. **SSH 를 쓰지 않는다.**

```
GitHub Actions ──OIDC(임시 권한)──▶ IAM 역할 <AppName>-github-deploy
               ──SSM Run Command──▶ EC2 안의 SSM 에이전트
                                     └ git reset --hard origin/main → docker compose up -d --build --wait
               ──https://<IP>/health 확인
```

- GitHub 에 저장된 AWS 키·SSH 키가 없다. OIDC 는 이 레포의 main 브랜치 작업에만 1시간짜리 권한을 준다.
- 배포 역할은 이 스택의 인스턴스에만 명령을 보낼 수 있다.
- 배포 대상은 `Name=<APP_NAME>` 태그로 찾고 공인 IP 도 거기서 읽는다.
- OIDC 신뢰 조건은 `sub = repo:<owner>/<repo>:ref:refs/heads/main` 이다. 워크플로 job 에
  `environment:` 를 넣으면 `sub` 가 `...:environment:<이름>` 으로 바뀌어 거절된다.

## 다른 레포 배포하기

같은 템플릿으로 앱마다 스택을 하나씩 만든다.

1. 스택 파라미터: `AppName`(예: `other-app`), `GitHubRepo`(예: `ji-min0/other-app`),
   `CreateOIDCProvider=false`(공급자는 계정에 하나뿐).
2. 그 레포의 워크플로 `env.APP_NAME` 을 `AppName` 과 같게, Secret `AWS_ROLE_ARN` 을 출력의
   `DeployRoleArn` 으로 둔다.
3. 그 레포에 `Dockerfile`, `docker-compose.yml`(app + Caddy), `deploy/Caddyfile`, `/health` 가 있어야 한다.

## 운영

```bash
docker compose ps                     # app 이 healthy 인지
docker compose logs -f app            # 서버 로그(route_selected, ai_call_success 등)
docker compose restart app            # .env 를 고쳤을 때
```

- RAG 문서는 로컬에서 `scripts/ingest.py` 로 넣는다(같은 Supabase). 넣은 뒤 관리자 화면의
  "문서·프롬프트 다시 읽기"를 누르면 서버 재시작 없이 반영된다.
- 워커는 1개다. 같은 대화방 동시 실행 가드가 프로세스 안에서만 동작하기 때문이다.

### 주소 방식

`.env` 의 `DOMAIN`·`ACME_PROFILE` 을 고치고 `docker compose up -d` 한다.

| 방식 | `.env` | 비고 |
|---|---|---|
| **공인 IP (현재)** | `DOMAIN=<IP>`, `ACME_PROFILE=shortlived` | Let's Encrypt IP 인증서(2026-01 정식). 6일짜리라 Caddy 가 이틀쯤마다 자동 갱신. 서버가 6일 넘게 꺼져 있으면 만료되지만 다시 켜면 곧 새로 받는다 |
| 도메인 | `DOMAIN=chat.example.com` | DNS A 레코드를 서버 IP 로. 90일 인증서. 발표·심사용으로 가장 안정적 |
| sslip.io | `DOMAIN=<IP 의 . 을 - 로>.sslip.io` | 도메인 없이 이름이 필요할 때. 외부 무료 DNS 에 의존 |

IP 로 접속하면 브라우저가 SNI 를 보내지 않으므로 `deploy/Caddyfile` 의 `default_sni` 가 필요하다.

## NCP 로 옮기기

NCP 에는 CloudFormation·SSM 이 없으므로 서버를 손으로 만들고, CD 는 SSH 방식으로 바꾼다.

1. Server → 서버 생성: Ubuntu 24.04, `s2-g3`(2vCPU/8GB), 공인 IP 연결, ACG 에서 22(내 IP)·80·443 허용.
2. 서버에서:

   ```bash
   git clone https://github.com/ji-min0/hackathon-agent.git && cd hackathon-agent
   bash scripts/server-setup.sh   # Docker·make 설치, 메모리가 작으면 스왑
   ```

3. 위 "3. 서버에 `.env` 올리기"를 새 IP 로 하고 `make deploy`.
4. CD: git 이력의 `802de0d` 커밋에 SSH 방식 워크플로가 있다(배포 키를 서버 `authorized_keys` 에
   등록하고 `DEPLOY_*` Secrets 를 넣는 방식). 22번을 전체 허용해야 한다.
5. 팀원이 같은 NCP 안에 vLLM 을 띄우면 `.env` 의 `OPENAI_BASE_URL` 을 GPU 서버 **사설 IP**
   (예: `http://10.0.1.5:8000/v1`)로 바꾸고 `docker compose restart app`.
6. 확인되면 AWS 스택을 삭제한다.

## 문제 해결

| 증상 | 확인 |
|---|---|
| `https://` 접속이 안 됨 | 80·443 이 열려 있는지(인증서 발급에 80 을 쓴다), `docker compose logs caddy` |
| IP 접속 시 인증서 경고 | `.env` 에 `ACME_PROFILE=shortlived` 가 있는지 |
| 로그인 후 바로 로그아웃됨 | `http://` 로 접속했는지. Secure 쿠키라 HTTPS 에서만 유지된다 |
| app 이 unhealthy | `docker compose logs app` 에서 `.env` 값 오류 확인 |
| CD: `Not authorized to perform sts:AssumeRoleWithWebIdentity` | `AWS_ROLE_ARN` 이 출력의 `DeployRoleArn` 과 같은지, 워크플로 job 에 `environment:` 가 없는지 |
| CD: SSM 명령이 `Undeliverable`·대기만 함 | EC2 에 `<AppName>-ec2` 역할이 붙었는지, 서버에서 `sudo snap restart amazon-ssm-agent` |
