# 배포 (AWS EC2 / NCP 공통)

앱은 Docker 이미지 하나로 돈다. 서버 앞에는 Caddy 가 HTTPS 를 붙인다.
DB 는 Supabase 를 그대로 쓰므로 서버를 옮겨도 데이터 이전이 없다.

```
브라우저 ─HTTPS→ Caddy(80·443, 인증서 자동 발급) ─HTTP→ app(uvicorn 8000) ─→ Supabase
```

클라우드마다 다른 것은 **1. 서버 만들기**뿐이다. 2번부터는 AWS·NCP 가 똑같다.

## 1. 서버 만들기

공통 조건: Ubuntu 22.04 또는 24.04, 공인 IP, 인바운드 포트 22(SSH)·80·443 열기.

| | AWS | NCP |
|---|---|---|
| 서버 | EC2 → 인스턴스 시작 | Server → 서버 생성. `s2-g3`(2vCPU/8GB) |
| 포트 | 보안 그룹 인바운드 규칙: 22(내 IP), 80·443(0.0.0.0/0) | ACG 규칙: 22(내 IP), 80·443(0.0.0.0/0) |
| 공인 IP | 탄력적 IP 할당 후 연결(재시작해도 IP 유지) | 공인 IP 신청 후 서버에 연결 |
| 접속 | `ssh -i 키.pem ubuntu@<IP>` | `ssh root@<IP>` (관리자 비밀번호는 콘솔에서 확인) |
| 사양 | 무료 티어 대상 중 m7i-flex.large(2vCPU/8GB)가 NCP s2-g3 와 같다 | |

메모리 1GB 서버는 `server-setup.sh` 가 스왑 2GB 를 만들어 빌드가 버티게 한다.

## 2. 서버 준비 (한 번만)

```bash
git clone https://github.com/ji-min0/hackathon-agent.git
cd hackathon-agent
bash scripts/server-setup.sh
exit   # docker 그룹 반영을 위해 다시 접속한다
```

## 3. 환경 변수 올리기

`.env` 는 저장소에 없다. 로컬에서 서버로 복사한다.

```bash
# 로컬에서
scp -i 키.pem .env ubuntu@<IP>:~/hackathon-agent/.env
ssh -i 키.pem ubuntu@<IP> chmod 600 hackathon-agent/.env
```

`COOKIE_SECURE` 는 compose 가 `true` 로 덮어쓰므로 `.env` 에서 바꿀 필요 없다.
`docker compose` 는 값 뒤의 주석(`KEY=40 # 설명`)을 지워서 읽지만, `docker run --env-file` 은 주석까지
값으로 읽는다. 주석은 값과 다른 줄에 두는 것이 안전하다.

## 4. 실행

서버 `.env` 끝에 접속 주소를 넣는다. `docker compose` 가 같은 폴더의 `.env` 를 읽어 Caddy 에 넘긴다
(앱은 모르는 키를 무시한다).

```bash
cd ~/hackathon-agent
printf "DOMAIN=52.78.121.248\nACME_PROFILE=shortlived\n" >> .env   # 공인 IP 로 접속할 때
make deploy                                # = docker compose up -d --build
docker compose ps                          # app 이 healthy 가 되면 준비 완료(30초 정도)
curl -s https://52.78.121.248/health       # {"status":"ok"}
```

주소 방식은 셋 중 하나를 고른다. 바꿀 때는 `.env` 를 고치고 `docker compose up -d` 한다.

| 방식 | `.env` | 비고 |
|---|---|---|
| **공인 IP (현재)** | `DOMAIN=52.78.121.248`, `ACME_PROFILE=shortlived` | Let's Encrypt IP 인증서(2026-01 정식). 6일짜리라 Caddy 가 이틀쯤마다 자동 갱신. 서버가 6일 넘게 꺼져 있으면 만료되지만 다시 켜면 곧 새로 받는다 |
| 도메인 | `DOMAIN=chat.example.com` | DNS A 레코드를 서버 IP 로. 90일 인증서. 발표·심사용으로 가장 안정적 |
| sslip.io | `DOMAIN=52-78-121-248.sslip.io` | 도메인 없이 이름이 필요할 때. 외부 무료 DNS(sslip.io)에 의존 |

## 5. 업데이트·운영

```bash
git pull && docker compose up -d --build   # 새 코드 반영
docker compose logs -f app                 # 서버 로그(route_selected, ai_call_success 등)
docker compose restart app                 # .env 를 고쳤을 때
```

- RAG 문서는 로컬에서 `scripts/ingest.py` 로 넣는다(같은 Supabase 를 쓴다). 넣은 뒤 관리자 화면의
  "문서·프롬프트 다시 읽기"를 누르면 서버 재시작 없이 반영된다.
- 워커는 1개다. 같은 대화방 동시 실행 가드가 프로세스 안에서만 동작하기 때문이다.

## CI/CD (GitHub Actions)

`.github/workflows/ci-cd.yml`

- **CI**: 모든 push·PR 에서 `make check`(ruff 린트·포맷, pytest)를 돌린다. 배포는 하지 않는다.
- **CD**: main 에 push 되고 CI 가 통과하면 서버에 SSH 로 접속해 `git reset --hard origin/main` →
  `docker compose up -d --build` → `https://<DEPLOY_HOST>/health` 확인. 서버 `.env` 는 그대로 둔다.

### 처음 한 번 설정

배포 전용 SSH 키를 따로 만든다(사람이 쓰는 키와 분리해서, 문제가 생기면 이 키만 지운다).

```bash
# 1) 로컬에서 배포 키 만들기
ssh-keygen -t ed25519 -N "" -C github-actions-deploy -f ~/.ssh/hackathon-deploy

# 2) 서버에 공개 키 등록
ssh -i ~/.ssh/hackathon-agent-key.pem ubuntu@52.78.121.248 \
  "cat >> ~/.ssh/authorized_keys" < ~/.ssh/hackathon-deploy.pub

# 3) GitHub Secrets 등록 (gh CLI)
gh secret set DEPLOY_HOST --body 52.78.121.248
gh secret set DEPLOY_USER --body ubuntu
gh secret set DEPLOY_SSH_KEY < ~/.ssh/hackathon-deploy
ssh-keyscan -t ed25519 52.78.121.248 2>/dev/null | gh secret set DEPLOY_KNOWN_HOSTS
```

4) 방화벽: GitHub Actions 러너는 IP 가 매번 바뀌므로 22번을 전체 허용한다. 비밀번호 로그인은
꺼져 있고(Ubuntu 기본값) 등록한 키로만 들어올 수 있다.

```bash
aws ec2 authorize-security-group-ingress --region ap-northeast-2 --group-name hackathon-agent-sg \
  --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=0.0.0.0/0,Description=github-actions}]"
```

### NCP 로 옮기면

1. 새 서버의 ACG 에서 22번을 전체 허용한다.
2. 새 서버 계정(root)의 `~/.ssh/authorized_keys` 에 `~/.ssh/hackathon-deploy.pub` 을 추가한다.
3. `DEPLOY_HOST`(새 IP), `DEPLOY_USER`(root), `DEPLOY_KNOWN_HOSTS`(새 서버 ssh-keyscan) 를 다시 등록한다.
   `DEPLOY_SSH_KEY` 는 그대로 쓴다. 워크플로 파일은 고치지 않는다.

## AWS → NCP 로 옮기기

1. NCP 에서 **1. 서버 만들기**를 한다.
2. 2~4번을 그대로 한다. `.env` 의 `DOMAIN` 만 새 IP 로 바꾼다(새 IP 로 인증서를 자동으로 다시 받는다).
3. 확인되면 아래 명령으로 AWS 리소스를 정리한다. 탄력적 IP 는 남겨 두면 계속 과금된다.

```bash
export AWS_REGION=ap-northeast-2
IID=$(aws ec2 describe-instances --filters Name=tag:Name,Values=hackathon-agent \
  Name=instance-state-name,Values=running,stopped --query "Reservations[].Instances[].InstanceId" --output text)
ALLOC=$(aws ec2 describe-addresses --filters Name=tag:Name,Values=hackathon-agent --query "Addresses[0].AllocationId" --output text)
aws ec2 disassociate-address --association-id $(aws ec2 describe-addresses --allocation-ids $ALLOC --query "Addresses[0].AssociationId" --output text)
aws ec2 release-address --allocation-id $ALLOC
aws ec2 terminate-instances --instance-ids $IID && aws ec2 wait instance-terminated --instance-ids $IID
aws ec2 delete-security-group --group-name hackathon-agent-sg
aws ec2 delete-key-pair --key-name hackathon-agent-key && rm ~/.ssh/hackathon-agent-key.pem
```

### 현재 AWS 배포 (2026-10-26)

| 항목 | 값 |
|---|---|
| 주소 | https://52.78.121.248 (관리자: `/admin`) — Let's Encrypt IP 인증서(shortlived) |
| 인스턴스 | `hackathon-agent`, m7i-flex.large(2vCPU/8GB, 무료 티어 대상), Ubuntu 24.04, 30GB gp3, 서울 |
| 접속 | `ssh -i ~/.ssh/hackathon-agent-key.pem ubuntu@52.78.121.248` (22번은 등록한 IP 만 허용) |
| CLI 권한 | IAM 사용자 `hackathon-deployer`(서울 EC2 작업만 허용 + `aws login`용 권한) |

팀원이 같은 NCP 안에 vLLM 을 띄우면 `.env` 의 `OPENAI_BASE_URL` 을 GPU 서버 **사설 IP** 주소
(예: `http://10.0.1.5:8000/v1`)로 바꾸고 `docker compose restart app` 한다.

## 문제 해결

| 증상 | 확인 |
|---|---|
| `https://` 접속이 안 됨 | 80·443 포트가 열려 있는지(Caddy 가 인증서를 받을 때 80 을 쓴다), `docker compose logs caddy` |
| IP 접속 시 인증서 경고 | `.env` 에 `ACME_PROFILE=shortlived` 가 있는지. 없으면 IP 인증서를 받지 못한다 |
| 로그인 후 바로 로그아웃됨 | `http://` 로 접속했는지. Secure 쿠키라 HTTPS 에서만 유지된다 |
| app 이 unhealthy | `docker compose logs app` 에서 `.env` 값(DATABASE_URL 등) 오류 확인 |
| 빌드 중 멈춤·Killed | 메모리 부족. `free -h` 로 스왑이 있는지 확인하고 `server-setup.sh` 를 다시 실행 |
