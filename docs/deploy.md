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
| 서버 | EC2 → 인스턴스 시작. 무료 범위면 t3.micro(1GB) | Server → 서버 생성. `s2-g3`(2vCPU/8GB) |
| 포트 | 보안 그룹 인바운드 규칙: 22(내 IP), 80·443(0.0.0.0/0) | ACG 규칙: 22(내 IP), 80·443(0.0.0.0/0) |
| 공인 IP | 탄력적 IP 할당 후 연결(재시작해도 IP 유지) | 공인 IP 신청 후 서버에 연결 |
| 접속 | `ssh -i 키.pem ubuntu@<IP>` | `ssh root@<IP>` (관리자 비밀번호는 콘솔에서 확인) |

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
```

`COOKIE_SECURE` 는 compose 가 `true` 로 덮어쓰므로 `.env` 에서 바꿀 필요 없다.

## 4. 실행

도메인이 없으면 `IP 의 점(.)을 대시(-)로 바꾼 값.sslip.io` 를 쓴다. 이 주소는 항상 그 IP 로 연결되고,
Caddy 가 이 주소로 HTTPS 인증서를 자동 발급받는다.

```bash
cd ~/hackathon-agent
export DOMAIN=3-35-10-7.sslip.io     # 예) IP 가 3.35.10.7 일 때
docker compose up -d --build
docker compose ps                    # app 이 healthy 가 되면 준비 완료(30초 정도)
curl -s https://$DOMAIN/health       # {"status":"ok"}
```

브라우저에서 `https://<DOMAIN>` 으로 접속한다. 관리자 화면은 `https://<DOMAIN>/admin`.

## 5. 업데이트·운영

```bash
git pull && docker compose up -d --build   # 새 코드 반영
docker compose logs -f app                 # 서버 로그(route_selected, ai_call_success 등)
docker compose restart app                 # .env 를 고쳤을 때
```

- RAG 문서는 로컬에서 `scripts/ingest.py` 로 넣는다(같은 Supabase 를 쓴다). 넣은 뒤 관리자 화면의
  "문서·프롬프트 다시 읽기"를 누르면 서버 재시작 없이 반영된다.
- 워커는 1개다. 같은 대화방 동시 실행 가드가 프로세스 안에서만 동작하기 때문이다.

## AWS → NCP 로 옮기기

1. NCP 에서 **1. 서버 만들기**를 한다.
2. 2~4번을 그대로 한다. `DOMAIN` 만 새 IP 로 바꾼다.
3. 확인되면 AWS 인스턴스를 중지한다(탄력적 IP 는 연결이 끊기면 과금되므로 해제).

팀원이 같은 NCP 안에 vLLM 을 띄우면 `.env` 의 `OPENAI_BASE_URL` 을 GPU 서버 **사설 IP** 주소
(예: `http://10.0.1.5:8000/v1`)로 바꾸고 `docker compose restart app` 한다.

## 문제 해결

| 증상 | 확인 |
|---|---|
| `https://` 접속이 안 됨 | 80·443 포트가 열려 있는지(Caddy 가 인증서를 받을 때 80 을 쓴다), `docker compose logs caddy` |
| 로그인 후 바로 로그아웃됨 | `http://` 로 접속했는지. Secure 쿠키라 HTTPS 에서만 유지된다 |
| app 이 unhealthy | `docker compose logs app` 에서 `.env` 값(DATABASE_URL 등) 오류 확인 |
| 빌드 중 멈춤·Killed | 메모리 부족. `free -h` 로 스왑이 있는지 확인하고 `server-setup.sh` 를 다시 실행 |
