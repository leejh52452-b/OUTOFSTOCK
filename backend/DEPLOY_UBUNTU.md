# OUTOFSTOCK — 우분투 VM 배포 가이드

로컬(내 PC)에서만 돌리면 팀원이 못 붙으니, **학교 VM/서버에 올려 팀원이 브라우저로 접속**하게 하는 절차다.
접속 목표: `http://<VM_IP>:8000/`(웹 화면 — nginx·TLS 를 깔면 `https://<VM_IP>/`, 예전 주소 `/app` 은 `/` 로 넘어감) · `/health`(상태). API 문서(`/docs`)는 운영에서 숨긴다.

경로는 세 가지.

| | 경로 A · Docker Compose | 경로 B · systemd + venv | 경로 C · 학교 VM(tmux) |
|---|---|---|---|
| 추천 | Docker 되는 서버 | Docker 금지 + 상시 운영 | 지금 학교 VM 방식(설치 스크립트) |
| 필요 | docker, docker compose | python3.11+, postgresql | python3-venv, postgresql, tmux |
| 배포 | `./deploy/deploy.sh`(.env + .env.db) | **`bash deploy/vm.sh install`**(★) 또는 수동 단계 | WinSCP 로 스크립트 1개 → tmux 창 3개 |
| DB | 컨테이너(postgres:16) | OS 의 PostgreSQL | OS 의 PostgreSQL |

> 준비물: 우분투 22.04/24.04 VM, `sudo` 권한, VM의 IP(또는 도메인), 학교 방화벽에서 열 수 있는 포트(8000 또는 80).

---

## ★ 가장 쉬운 길 — `deploy/vm.sh` (경로 B 자동화 — 2026-10-22 판)

Docker 없이 venv + PostgreSQL + systemd. **본인 계정으로 실행**(sudo 를 앞에 붙이지 말 것 — 필요한 단계만 비밀번호를 묻는다).

```bash
# 처음 한 번 — WinSCP 로 install_outofstock_full.sh 하나를 홈에 올린 뒤
mkdir -p ~/stock-backend && cd ~/stock-backend && bash ~/install_outofstock_full.sh
bash deploy/vm.sh install          # 패키지·DB·.venv·.env(600)·스키마·서비스 3개(systemd 샌드박스)·매일 04:30 백업
nano .env && bash deploy/vm.sh restart     # 키(KRX·DART·KIS…) 넣고 → .venv/bin/python run.py real (또는 23:00 자동)
bash deploy/vm.sh harden           # 서버 기본 보안: 방화벽(기본 거부·SSH 시도 제한)·SSH 강화·자동 보안 업데이트·커널·DB 접속 기록

# 새 버전 — 새 install_outofstock_full.sh 를 홈에 올린 뒤
bash deploy/vm.sh update ~/install_outofstock_full.sh
#   설치본 검증(SHA-256·파일 수 — 잘리거나 바뀐 파일 거부) → **변경관리 확인**(지금 배포된 코드가 새 설치본의 구문·적용된 마이그레이션·
#   정책 형식을 점검, 승인·배포 금지 시간 — 2026-11 ADR-0026) → 소유자 점검 → 암호화 백업 → 이전 코드 보관 → 교체(.env·.venv 유지,
#   지운 파일 정리) → 패키지 → 마이그레이션 → '새' 스크립트로 서비스 파일·프록시 목록 → 재시작 → 헬스 → 변경 기록(적용·기준선 번호).
#   안 뜨면: bash deploy/vm.sh rollback(되돌린 파일로 무결성 기준선을 다시 기록 + 변경 기록 '되돌림')
#   긴급 배포(배포 금지 시간·enforce 에서 승인 전): CHANGE_REASON='무엇이 왜 급한지' bash deploy/vm.sh update … → 3일 안 다른 관리자 사후 검토

bash deploy/vm.sh status           # 서비스·바인딩·샌드박스 노출 점수·egress·헬스·디스크·백업·훈련·보안 설정 점검 요약
bash deploy/vm.sh logs api         # 실시간 로그(scheduler·realtime 도)
bash deploy/vm.sh backup | backups # 지금 백업(age 암호화 + manifest) · 목록
AGE_IDENTITY=~/금고/vault.key bash deploy/vm.sh restore <백업>   # 새 DB 에 복원 → 검증 8항목 → 통과하면 교체(실패하면 운영 DB 그대로)
DRILL_ADMIN_URL=… bash deploy/vm.sh drill                        # 복구 훈련(운영 DB 안 건드림) — RTO/RPO 보고서
```

설치본 만들기(개발 PC): `python tools/make_installer.py` → `dist/install_outofstock_full.sh`(.env·키·인증서·백업 공개키는 들어가지 않는다).

### 2026-10-22 업데이트 뒤 한 번만 할 일

1. **서비스 파일 다시 쓰기** — 지금 VM 에 있는 vm.sh 는 옛 판이라 `update` 가 옛 방식으로 서비스 파일을 쓴다. 업데이트가 끝나면 한 번:
   ```bash
   bash deploy/vm.sh units          # 샌드박스·상태 폴더·API 쿼리 시간 제한이 들어간 서비스 파일 + 재시작 + 헬스
   bash deploy/vm.sh status         # '샌드박스: 켬 · 노출 점수 1.x OK' 확인
   ```
   (다음 업데이트부터는 update 가 새 스크립트로 이어서 실행하므로 따로 할 일 없음.) 문제가 의심되면 `HARDEN=0 bash deploy/vm.sh units` 로 비교.
2. **로그인이 한 번 풀린다** — 24시간 토큰 → 10분 접근 토큰 + 회전 갱신 토큰(HttpOnly 쿠키)으로 바뀌었다. 다시 로그인하면 된다.
3. **키 넣기**(없으면) — `.venv/bin/python tools/security_check.py` 가 빠진 것을 FAIL 로 보여 준다.
   ```bash
   python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"   # 두 번 실행 — APP_SECRET_KEY · DATA_ENCRYPTION_KEY(서로 다르게)
   nano .env    # APP_SECRET_KEY=… · DATA_ENCRYPTION_KEY=… · AUDIT_WITNESS_FILE=/var/lib/outofstock/audit-witness.jsonl · LOG_FORMAT=json
   bash deploy/vm.sh restart
   ```
   기존 비밀번호 해시는 그대로 동작한다(다음 로그인 때 페퍼 적용으로 자동 상향). 키 사본은 오프라인 금고에.
4. **백업 암호화** — 공개키만 서버에(복호화 키는 금고). 절차: `docs/dr/README.md` '처음 설정'.
   `/etc/outofstock/backup.recipients` 에 공개키 2개 → `.env` 에 `BACKUP_AGE_RECIPIENTS=/etc/outofstock/backup.recipients`.
5. **관리자** — `.env` 에 `ADMIN_USERS=내아이디` → `.venv/bin/python run.py admin-bootstrap`(자격) → TOTP 등록(API — 화면은 스크린샷 기능만:
   `frontend/outofstock.http` '관리자' 절 — 추가 인증 → `/auth/mfa/setup` → OTP 앱 → `/auth/mfa/confirm`) →
   관리 기능을 쓸 때마다 OTP 로 확인된 세션에서 `POST /admin/elevate`(사유 필수, 최대 30분).
6. (권장) **DB 계정 분리** — `docs/security/db_roles.md`(oos_app·oos_ro·oos_owner·oos_backup). 그 뒤 업데이트는
   `MIGRATION_DATABASE_URL=…oos_owner… bash deploy/vm.sh update …`.
7. (선택) **HTTPS + 관리 기기 인증서** — 아래 'nginx' 절 → `bash deploy/vm.sh units`(앱이 자동으로 127.0.0.1 로) → `bash deploy/vm.sh harden`(80/443 만).
8. (선택) **나가는 연결 잠그기** — `EGRESS_PROXY=1 bash deploy/vm.sh units`: 같은 서버에 squid(허용 목록)를 깔고 서비스의 직접 연결은 커널이 막는다.
   DB 가 다른 서버면 `EGRESS_ALLOW_IPS='<DB IP>/32'`, 메일 알림을 쓰면 메일 서버 주소도.
9. (이미 nginx 를 쓰는 VM) **nginx 설정 교체** — update 는 `/etc/nginx` 를 건드리지 않는다. 새 판에는 Host 포트 전달(80·443 이 아닌 포트에서
   웹 로그인 403 수정)·`/events` 버퍼링 끔·전달 헤더 덮어쓰기가 들어 있다:
   `sudo cp deploy/nginx.conf /etc/nginx/sites-available/outofstock`(HTTP 판이면 `nginx-http-only.conf`) `&& sudo nginx -t && sudo systemctl reload nginx`.

### 2026-11-09 업데이트 — 팀 변경 반영: 모의투자·양봉이 주간 추천(할 일 없음 · 확인만)

`bash deploy/vm.sh update ~/install_outofstock_full_1109.sh` 한 번이면 된다(ADR-0028). 새 표 5개(`yangbong_basket`·`vmarket_*`)와
행 수준 보안(V019)·초기 데이터(팀 백테스트 247주·모의투자 시즌 1~10)는 서버가 켜질 때 자동으로 들어간다. `.env` 그대로.
변경관리 게이트는 이번 판을 '기능·스키마 추가'로 분류한다 — 기본 기록 모드는 통과.
- **모의투자(새 화면 `/invest.html`)** — 시즌은 월~금(시즌 1 = 11-02 주, 오늘 11-09 은 시즌 2), 평일 09:00~15:00 30분마다 봉 하나.
  참가금은 가상머니 지갑에서 빠지고 주말 정산 금액이 돌아온다(스케줄러 매일 00:05 — 토요일에 지난 시즌 정산).
  **시즌 데이터는 시즌 10(2027-01-04 주)까지** 들어 있다. 시즌 11부터는 팀 생성기(`stockGeneator`)가 만든 파일을 넣는다:
  `.venv/bin/python run.py vmarket-import season_11.json season_12.json …` → `run.py vmarket-status`.
  다음 시즌 파일이 없으면 금요일 18:00 에 운영 경보가 가고 `/admin/health` 에 `vmarket_next_season_missing` 이 뜬다.
- **양봉이 주간 추천(홈 카드)** — 스케줄러가 매일 08:55·23:55 에 '끝난 주'를 찾아 5종목을 고르고 30일 지난 바구니를 채점한다.
  분석 계층(KRX 전 종목 시세 — `run.py platform-load`)이 있어야 새 주를 고른다. 없거나 일부만 적재됐으면(후보 150 미만) 고르지 않고
  팀 백테스트 마지막 주(2026-09-23)를 그대로 보여 준다(카드 아래 기준일 표시). 바로 돌려 보기: `.venv/bin/python run.py yangbong`.
  첫 실행은 그동안 빠진 주(최근 26주까지)를 한꺼번에 채운다.
- 확인: `run.py vmarket-status`(이번·다음 시즌 준비·정산 대기) · `run.py yangbong-status` · 브라우저 `/invest.html`·홈 추천 카드.

### 2026-11-03 업데이트 — DB 정리(서비스 1~3분 멈춤 · 자동, 할 일 없음)

`bash deploy/vm.sh update ~/install_outofstock_full_1103.sh` 한 번이면 된다(ADR-0027). 변경관리 게이트가 이번 판을 **스키마 변경(위험 높음)**으로
분류한다 — 기본 기록 모드는 막지 않는다. `.env` 에 `CHANGE_GATE=enforce` 를 켰다면 평일 08:30~16:00(배포 금지 시간)을 피해, 정식 절차
(`run.py change-request` → 다른 관리자 승인 — 근거로 `docs/perf/db_space_2026-11-03.md`)나 `CHANGE_REASON='사유'` 로.
- **DB 저장 형식이 바뀐다** — 날짜·시각 문자열 → date·timestamp, 원화 가격 → 정수, 기사 주소 인덱스 → md5 키, 겹치는 인덱스 7개 제거.
  표를 한 번씩 다시 쓰므로 데이터가 많을수록 오래 걸린다(1년 규모 실험에서 약 70초 — VM 은 2~3배까지). update 가 서비스를 멈춘 채 하고,
  화면·API 의 값 모양은 그대로다. 형식이 깨진 값은 `data_quality_issue` 에 원값을 남기고 비운다(바뀐 뒤 행 수가 안 맞으면 전체를 되돌린다).
  update 는 시작 전에 자동 백업한다 — 디스크 여유가 DB 크기만큼은 있어야 한다(`df -h` · `bash deploy/vm.sh status` 의 DB 크기).
- **DB 서버 설정 자동 적용** — 서버 메모리·CPU 에 맞는 값(공유 버퍼·WAL 압축·자동 VACUUM·느린 쿼리 통계 등)을 `ALTER SYSTEM` 으로 넣고
  PostgreSQL 을 한 번 재시작한다(sudo 비밀번호를 물을 수 있다). 미리 보기 `bash deploy/vm.sh db-tune --plan` · 되돌리기 `--reset` ·
  끄기 `.env` 에 `DB_AUTOTUNE=off`.
- **수명주기** — 뉴스·검색 기록은 13개월(400일)이 지난 달부터 `var/archive` 에 압축 보관(SHA-256 검증) 뒤 DB 에서 정리(매월 2일 04:10).
  `.env` 에 `BACKUP_OFFSITE_CMD` 가 있으면 보관 파일도 같은 명령으로 내보내고, 그 복사가 성공한 달만 지운다. 지금 데이터는 1년이 안 돼 아직 대상 없음.
- **되돌리기 주의** — 이 판 전 코드로 `vm.sh rollback` 하면 막힌다(이전 코드는 새 저장 형식을 못 읽는다). update 직전 백업으로
  `bash deploy/vm.sh restore <백업>` 한 뒤 `FORCE=1 bash deploy/vm.sh rollback`.
- 확인: `.venv/bin/python run.py db-report`(크기·판정·설정) · `run.py ilm-status` · `GET /admin/db` · 백업은 이제 zstd(빠르고 작다).

### 2026-11-02(3차) 업데이트 — IT 내부통제(따로 할 일 없음, 선택만)

`bash deploy/vm.sh update ~/install_outofstock_full_1102c.sh` 한 번이면 된다. 새 표(`change_record`·`incident`·`uptime_check`)·권한(V017)은 자동.
- **이번 업데이트는 변경관리 확인을 건너뛴다** — 지금 VM 에 있는 코드에는 아직 게이트가 없다. 다음 업데이트부터 `update` 가 서비스를 멈추기 전에 확인한다.
  기본은 기록 모드(막지 않고 '미승인 배포'로 기록) — 정식 절차를 쓰려면 배포 전에
  `.venv/bin/python run.py change-request ~/새설치본.sh --by 내아이디 --title … --reason … --evidence '시험 결과'` → **다른 관리자**가
  `POST /admin/changes/{번호}/approve`. 금융 운영처럼 막으려면 `.env` 에 `CHANGE_GATE=enforce`.
- **가용성 감시** — 스케줄러가 1분마다 API 헬스를 부른다(systemd 면 vm.sh 가 실제 포트로). 10분 실패면 장애 사건·경보, 30분이면 보고 대상.
  tmux 로 돌리면 기본값(`http://127.0.0.1:8000/health`)으로 동작. 끄려면 `.env` 에 `UPTIME_URL=off`.
- **경보 확인** — 오류 경보를 받으면 `POST /admin/alerts/{번호}/ack`(15분 안 — 넘으면 한 번 더 온다). 목록은 `GET /admin/alerts`.
- 확인: `.venv/bin/python run.py change-list` · `.venv/bin/python run.py uptime-check` · (선택) `.venv/bin/python run.py pentest --base http://127.0.0.1:8000 --register`.

### 2026-10-24 업데이트 — 따로 할 일 없음(확인만)

`bash deploy/vm.sh update ~/install_outofstock_full.sh` 한 번이면 된다(새 열 `fds_decision.ref` 는 시작할 때 자동 추가). 바뀐 동작:
- **원장 일 마감**이 '어제'만이 아니라 **마감 안 한 날을 어제까지 날짜순으로** 메운다(서버가 꺼져 있던 날 포함) — 다음 00:20 에 자동, 지금 바로는
  `.venv/bin/python run.py ledger-close`. 확인: `.venv/bin/python run.py ledger-verify` → 깨진 날·빠진 날 없음·앵커 일치·시산표 0.
  예전 판이 이미 건너뛴 날이 있으면 '빠진 날'로 보인다(사슬 중간에는 끼울 수 없어 기록으로 남김 — 오류가 아니라 사실 보고).
- **송금 FDS**는 송금 트랜잭션 안(지갑 잠금 뒤)에서 채점한다 — 화면·API 는 그대로. `run.py dq` 에 `wallet.transfer_unscreened`(판정 없는 송금 0) 추가.
- 운영 기준선이 24항목(PAY-1: 결제 검증이 stub 이면 INFO 로 표시 — 데모 충전용 기본값).

### 알아 둘 것
- **선택값은 기억된다** — `PORT`·`WORKERS`·`BIND_HOST`·`HARDEN`·`EGRESS_PROXY` 는 서비스 파일에 기록돼 update 때 유지된다.
  `BIND_HOST=auto`(기본): nginx 사이트(`/etc/nginx/sites-enabled/outofstock`)가 있으면 127.0.0.1, 없으면 0.0.0.0.
- **워커 2개 이상**이면 `.env` 에 `RATE_LIMIT_BACKEND=postgres`(시도 제한을 워커끼리 공유).
- **샌드박스 안에서 서비스가 보는 것**: 홈 폴더 중 코드 폴더만(읽기 전용) · 쓰기는 `var/`·`/var/lib/outofstock`·`/var/cache/outofstock` 만 ·
  `/tmp` 는 서비스 전용. 그래서 `.env` 의 `AUDIT_WITNESS_FILE` 은 `/var/lib/outofstock/…` 로. 백업 서비스는 백업 폴더에도 쓴다.
  원격 보관 도구가 홈의 다른 폴더에 써야 하면 `BACKUP_RW_PATHS='/home/나/.config/rclone'`.
- **IPv6 를 끈 VM**: nginx 설정에 `listen [::]:80` 을 넣지 말 것(기본 설정은 넣지 않음 — 넣으면 nginx 가 못 뜬다).
- **tmux 로 돌리던 VM**: 각 창(uvicorn·scheduler·realtime)을 `Ctrl+C` 로 끈 다음 `install` — 포트가 잡혀 있으면 스크립트가 멈추고 알려 준다.
- **학교 VPN 으로 여러 명이 같은 IP** 면 가입 제한(IP 당 1시간 20번)에 걸릴 수 있다 → `.env` 에 `REGISTER_MAX_PER_IP=200` 후 `restart`.
- **서버 시간대**: VM 이 UTC 여도 기록 시각·'오늘' 판정은 한국시간(`app/clock.py`) — `timedatectl` 을 바꿀 필요 없다.
- **nginx 없이 바로 공개**하면 앱이 직접 막는다: 본문 1MB 초과 413(`MAX_BODY_BYTES`), 무거운 계산을 새로 돌리는 요청은 IP 당 1분 12번
  (`HEAVY_MISS_PER_MIN`), 로그인·가입 시도 제한. 관리 기기 인증서(mTLS)는 nginx 가 있어야 쓸 수 있다.
- 백업 파일에는 계정·지갑 기록이 들어 있다 — 암호화하고(공개키), 폴더 700·파일 600. 평문 백업은 관리자 헬스에 경고가 뜬다.

<details><summary>지난 업데이트 메모(10-10 · 6차→7차)</summary>

- 2026-10-10(2차): 새 패키지·SQL 마이그레이션 V001~V006 자동, 분석 계층 백필은 `run.py platform-load --years 10 --budget 5000`(끊기면 다시 실행 = 이어 받기).
- 6차 → 7차: 6차 vm.sh 가 실행 중에 자기 파일이 바뀌어 마지막에 `syntax error` 한 줄이 보일 수 있었다 — 그 위에 '업데이트 완료'가 찍혔으면 끝난 것.
</details>

---

## 0. 코드 VM에 올리기 (공통)

**설치 스크립트(가장 간단)** — `install_outofstock_full.sh` 하나를 WinSCP/scp 로 홈에 올리고 **빈 폴더**에서 실행:

```bash
mkdir -p ~/stock-backend && cd ~/stock-backend && bash ~/install_outofstock_full.sh
```

**git 저장소**가 있으면 폴더 이름까지 지정해서 받는다(아래 서비스 파일들이 `…/stock-backend` 경로를 쓴다):

```bash
sudo mkdir -p /opt/outofstock && sudo chown "$USER" /opt/outofstock
cd /opt/outofstock
git clone <저장소_URL> stock-backend      # 결과: /opt/outofstock/stock-backend
cd /opt/outofstock/stock-backend
```

이후 모든 명령은 `stock-backend` 폴더 안에서 실행한다.

---

## 경로 A — Docker Compose

### A-1. Docker 설치 (최초 1회)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"      # sudo 없이 docker 쓰기
newgrp docker                        # 그룹 즉시 적용(또는 재로그인)
docker compose version               # v2 확인
```

### A-2. 환경파일 준비 — 두 개(둘 다 600)

```bash
install -m 600 env.prod.example .env      # 앱 설정·API 키 — 앱 컨테이너로 들어간다
install -m 600 env.db.example .env.db     # DB 비밀번호 — compose 가 접속 문자열을 만들 때만(앱 컨테이너엔 자기 것만)
nano .env.db     # POSTGRES_PASSWORD · OOS_OWNER/APP/RO/BACKUP_PASSWORD 각각 openssl rand -hex 24(영문·숫자만)
nano .env        # 키(KRX·DART·KIS·…) · APP_SECRET_KEY · DATA_ENCRYPTION_KEY — 견본은 보안 기준선을 통과한 값(tools/security_check.py)
cp deploy/backup/recipients.example.txt deploy/backup/recipients.txt && nano deploy/backup/recipients.txt   # 백업 공개키(age1…) 2개
```

- 앱은 **공식 postgres 이미지의 슈퍼유저로 붙지 않는다**(그러면 RLS 가 통째로 우회된다) — `OOS_APP_PASSWORD` 가 없으면 기동 거부.
- `.env` 에 DB 비밀번호가 있으면 deploy.sh 가 경고한다(앱 컨테이너 환경에 그대로 들어가므로 `.env.db` 로).

### A-3. 배포 · 업데이트

```bash
./deploy/deploy.sh             # 처음·업데이트 모두
./deploy/deploy.sh backup      # 지금 백업(oos_backup 계정 + age 암호화) — 호스트 타이머로 매일
./deploy/deploy.sh status      # 컨테이너·헬스·최근 백업
```

순서: egress 허용 목록을 코드와 맞춤(`deploy/egress-proxy/squid.conf`) → DB 기동·healthy → **최소 권한 역할 맞추기**(`roles.sql`, 멱등) →
**마이그레이션은 소유자 계정의 일회성 작업 컨테이너로**(서비스엔 소유자 비밀번호 없음) → 서비스 기동 → 앱 healthy → 빈 DB 면 시드(`SEED_CMD`, 기본 demo).

- 실데이터로 첫 시드: `SEED_CMD=real ./deploy/deploy.sh`. ⚠️ `demo` 는 빈 DB 용(실데이터가 있으면 거부, 정말 필요하면 `--force`).

### A-4. 망·포트 — 공개는 edge(nginx) 하나

| 망 | 누가 | 바깥 |
|---|---|---|
| outside | edge · egress-proxy | 인터넷 |
| web(내부) | edge → app | 없음 |
| data(내부) | app·scheduler·realtime·migrate·backup → db | 없음 |
| egress(내부) | app·scheduler·realtime → egress-proxy → 허용 목록 호스트만 | 프록시 경유만 |

- 공개 포트: `HTTP_PORT`(80)·`HTTPS_PORT`(443) → edge 하나. 앱·DB 포트는 열지 않는다. Docker 가 연 포트는 ufw 를 우회하므로 '열지 않는 것'이 유일하게 확실하다.
  학교 방화벽이 8000 만 열어 주면 `HTTP_PORT=8000` 도 된다(edge 가 Host 를 포트째 넘긴다 — 2026-10-22 수정 전엔 이 경우 웹 로그인이 403).
  TLS 판의 http→https 자동 이동은 443 으로 가므로 `HTTPS_PORT` 를 바꿨다면 `https://주소:포트` 로 바로 접속한다.
- TLS: `sudo bash deploy/pki/make-pki.sh compose deploy/tls <서버이름>` → `.env` 에 `EDGE_MODE=tls`·`COOKIE_SECURE=true`·`SECURITY_HSTS=true`
  (+ 앱↔DB TLS 는 `.env.db` 의 `DB_TLS`·`DB_HBA_FILE`·`DB_SSL_QUERY` 세 줄).
- 컨테이너는 전부 루트 아님·루트 FS 읽기 전용·capability 0·권한 상승 금지·자원 상한(`docker-compose.prod.yml` 의 x-hardening).

### A-5. 접속 확인

- `http://<VM_IP>/`(TLS 면 `https://`) → 웹 화면(팀 확정 디자인 다섯 장) · `/health` → `{"status": …}`(DB 문제면 503) · `/docs` → 404(운영에서 숨김)

### A-6. 운영 명령 모음

```bash
DC="docker compose --env-file .env --env-file .env.db -f docker-compose.yml -f docker-compose.prod.yml --profile full"
$DC ps
$DC logs -f app | scheduler | realtime | egress-proxy | edge
$DC --profile ops run --rm migrate python run.py real        # 지금 실데이터 수집(소유자 계정 작업 컨테이너)
$DC --profile ops run --rm backup                            # 백업
$DC exec -T app python tools/security_check.py --no-db       # 설정 기준선 점검
```

`restart: unless-stopped` 라 **VM이 재부팅돼도 컨테이너가 자동으로 다시 뜬다.**

---

## 경로 B — Docker 없이 (systemd + venv + PostgreSQL)

### B-1. 패키지 설치

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv postgresql
```

(우분투 22.04라 python3.12가 없으면 3.11 로 — `python3.11 -m venv` 도 동작.)

### B-2. PostgreSQL 사용자·DB 생성

```bash
PW=$(openssl rand -hex 24); echo "$PW"      # 영문·숫자 비밀번호(아래 .env 에 넣는다)
sudo -u postgres psql <<SQL
CREATE USER oos WITH PASSWORD '$PW';
CREATE DATABASE outofstock OWNER oos;
CREATE DATABASE outofstock_test OWNER oos;
SQL
```

### B-3. 전용 계정 · 가상환경 · 의존성

```bash
sudo useradd -r -s /usr/sbin/nologin outofstock 2>/dev/null || true
cd /opt/outofstock/stock-backend
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes --no-deps -r requirements.lock    # CI 가 시험한 판 그대로(해시가 다르면 설치 실패)
```
(잠금 파일이 그 파이썬 판에 맞지 않아 실패하면 `.venv/bin/pip install -r requirements.txt` — 범위 안 최신판.)

### B-4. 환경파일 (권한 600, 서비스 계정 소유)

```bash
sudo install -o outofstock -g outofstock -m 600 env.prod.example .env
sudo nano .env
```

compose 가 없으므로 **`DATABASE_URL` 을 직접** 넣는다:

```
DATABASE_URL=postgresql+psycopg://oos:<B-2 비밀번호>@localhost:5432/outofstock
```
그다음 `sudo -u outofstock .venv/bin/python tools/security_check.py` — 키·보관 기간·쿠키 등 기준선과 다른 것을 근거 조항과 함께 보여 준다.

### B-5. 스키마 + 초기 데이터

```bash
sudo chown -R outofstock:outofstock /opt/outofstock
sudo -u outofstock .venv/bin/python run.py initdb
sudo -u outofstock .venv/bin/python run.py real      # 키가 없으면 표본 + 경고 배너(빈 DB 미리보기는 demo)
```

### B-6. systemd 서비스 등록

```bash
sudo cp deploy/outofstock-api.service deploy/outofstock-scheduler.service deploy/outofstock-realtime.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now outofstock-api outofstock-scheduler outofstock-realtime
```

> 견본 유닛은 **샌드박스**(코드·시스템 읽기 전용, 홈 숨김, capability 0, 시스템 호출 제한)와 **`--host 127.0.0.1`**(nginx 뒤)로 되어 있다.
> nginx 없이 바로 열 거면 `--host 0.0.0.0` 으로. 쓰기 위치는 `var/`·`/var/lib/outofstock` 뿐 — `mkdir -p var && chown outofstock: var`.
> 유닛 파일의 `User` / `WorkingDirectory` / 경로가 실제 배포 위치와 다르면 먼저 수정할 것. **같은 내용을 계정·경로에 맞춰 자동으로 만들어 주는 것이
> `bash deploy/vm.sh install`(★)** 이다 — 백업 타이머·egress 프록시·선택값 기억까지.
> 실시간 수집기는 키가 하나도 없으면 바로 끝나며(정상 종료라 재시작 안 함), 수집기가 예외로 죽으면 프로세스 안에서 그 수집기만
> 잠시 뒤 다시 띄우고(30초→…최대 5분), 설정·인증 문제로 스스로 멈추면 종료 코드 1 → systemd 가 다시 띄운다.

### B-7. 방화벽 + 확인

```bash
sudo ufw allow 8000/tcp && sudo ufw reload       # nginx 뒤(127.0.0.1)면 8000 대신 80·443 — 또는 bash deploy/vm.sh harden
systemctl status outofstock-api --no-pager
systemd-analyze security outofstock-api          # 노출 점수(샌드박스 켜면 1.x OK, 끄면 9.x UNSAFE)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health    # 200 (DB 문제면 503)
```

### B-8. 실시간 시세 수집기 (국내 KIS + 해외 Finnhub) · 스케줄러

| 대상 | 필요한 키(.env) | 흐르는 시간(한국시간) |
|---|---|---|
| 국내 (KIS 웹소켓 H0STCNT0 + REST 일자별) | `KIS_APP_KEY`, `KIS_APP_SECRET` (모의계좌면 `KIS_PAPER=true`) | 평일 09:00~15:30(휴장일 제외) |
| 해외 (Finnhub 웹소켓 + /quote) | `FINNHUB_API_KEY` | 22:30~05:00(서머타임) / 23:30~06:00 |

장이 닫혀 있을 때는 **직전 체결가(=그날 종가)** 가 보인다. 국내는 수집기가 시작할 때·장중 10분마다·15:40 이후에
KIS REST 로 '오늘 봉'을 받아 둔다. 끊긴 웹소켓의 마지막 틱보다 **같은 날 공식 봉(종가)이 우선**한다.
휴장일(설·추석·NYSE 공휴일 등)은 화면에 `휴장일 · M/D 종가` 로 표시된다(`holidays` 패키지).

### B-8-1. 매일 23:00 `python run.py real` 자동 실행 (스케줄러)

`scheduler.py` 가 **한국시간 매일 23:00** 에 `run.py real` 전체 파이프라인을 돌린다
(KRX 시세·지수 → KIS 오늘 종가 → 실습 구간 → DART 재무 → 뉴스 → 해외 일봉·지수·환율). 서버 시간대와 무관(Asia/Seoul 고정).
보조 작업: 평일 08:40 KRX 확정치 · 15:45 KIS 오늘 종가 · 화~토 07:30 해외 확정 종가. 뉴스는 23:00 에서 하루 한 번만.

- **단계별 독립**: 한 소스(KIS 토큰·DART 키·뉴스 등)가 실패해도 나머지 단계는 돈다 → 실행 기록은 `PARTIAL`,
  `/health` 에 `last_pipeline_partial`, 실패한 단계 이름은 관리자 헬스(`/admin/health` 의 `last_run.note`)·`bash deploy/vm.sh logs scheduler`.
- **시각 바꾸기**: `.env` 에 `REAL_PIPELINE_TIME=23:00` · `REAL_PIPELINE_DAYS=mon-sun`.
- **겹침 방지**: 23:00 실행 중 수동 `run.py real` 은 "이미 실행 중 — 건너뜀"(DB advisory lock).
- **놓친 실행 보충**: 스케줄러가 꺼져 있다 켜졌는데 마지막 실행이 26시간보다 오래됐으면 1분 뒤 1회(`REAL_PIPELINE_CATCHUP=false` 로 끔).
- 23:00 은 미국 장중이라 **진행 중인 미국 오늘 봉은 저장하지 않는다**(확정 종가는 07:30, 장중 가격은 Finnhub 실시간).
- 실습 시나리오 과거 구간은 **한 번 실데이터로 받으면 다시 받지 않는다**(바뀌지 않는 과거). 다시 받으려면 `run.py fetch-practice --force`.

> cron 으로 하고 싶다면(스케줄러 대신, **둘 중 하나만**): `crontab -e` 에
> `0 23 * * * cd /opt/outofstock/stock-backend && .venv/bin/python run.py real >> logs/real.log 2>&1` (`mkdir -p logs` 먼저,
> cron 은 서버 시간대를 쓰므로 `sudo timedatectl set-timezone Asia/Seoul`).

### B-9. 새 버전으로 업데이트할 때

폴더 소유자가 `outofstock` 이므로 명령도 그 계정으로 실행한다(로그인 계정으로 하면 권한 오류, `sudo git pull` 은 git 의
'dubious ownership' 검사에 걸린다).

```bash
cd /opt/outofstock/stock-backend
sudo -u outofstock git pull
sudo -u outofstock .venv/bin/pip install --require-hashes --no-deps -r requirements.lock
sudo -u outofstock .venv/bin/python run.py initdb       # 새 테이블·컬럼·인덱스(기존 데이터 유지, 여러 번 실행해도 안전)
sudo systemctl restart outofstock-api outofstock-scheduler outofstock-realtime
```

설치 스크립트로 받는 경우엔 `git pull` 대신 `sudo -u outofstock bash ~/install_outofstock_full.sh`
(스크립트를 `/tmp` 등 outofstock 계정이 읽을 수 있는 곳에 두고 실행).

---

## 경로 C — 학교 VM (tmux · 설치 스크립트)

### C-1. 처음 설치

```bash
sudo apt update && sudo apt install -y python3-venv python3-dev postgresql tmux
mkdir -p ~/stock-backend && cd ~/stock-backend && bash ~/install_outofstock_full.sh
sudo -u postgres psql <<'SQL'
CREATE ROLE oos WITH LOGIN PASSWORD 'oos';
ALTER ROLE oos WITH LOGIN PASSWORD 'oos';
CREATE DATABASE outofstock OWNER oos;
CREATE DATABASE outofstock_test OWNER oos;
SQL
python3 -m venv .venv && source .venv/bin/activate && pip install -U pip && pip install -r requirements.txt
install -m 600 .env.example .env && nano .env     # 키: KRX/DART/KIS/FINNHUB/SEC_USER_AGENT
python run.py initdb
python run.py real                                 # 빈 DB 미리보기만 할 거면 demo
# tmux 창 3개
uvicorn app.api:app --host 0.0.0.0 --port 8000     # 창 1
python scheduler.py                                # 창 2
python run.py realtime                             # 창 3(선택)
```

### C-2. 폴더를 비우고 새 버전으로 다시 설치(데이터·키 유지)

DB 는 폴더 밖(PostgreSQL)에 있으므로 폴더를 지워도 시세·계정·지갑은 그대로다. **`.env`(API 키)만 먼저 빼 둔다.**

```bash
# 0) tmux 의 uvicorn / scheduler / realtime 창에서 Ctrl+C 로 모두 멈춘다
cp ~/stock-backend/.env ~/oos.env.bak                      # 키 백업
rm -rf ~/stock-backend && mkdir -p ~/stock-backend && cd ~/stock-backend
bash ~/install_outofstock_full.sh                          # 새 install_outofstock_full.sh 를 홈에 먼저 올려 둘 것
install -m 600 ~/oos.env.bak .env                          # 키 복원(권한 600)
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install -r requirements.txt      # 새 패키지(holidays) 포함
python run.py initdb                                       # 스키마·데이터 정리(한 번만 도는 정리 작업 포함)
python run.py real                                         # 선택 — 바로 새 규칙으로 수집(아니면 23:00 에 자동)
# 다시 tmux 창 3개: uvicorn … / python scheduler.py / python run.py realtime
```

---

## (선택) nginx — TLS 경계 · 관리 기기 인증서(mTLS) · 포트 없이 접속

```bash
sudo apt install -y nginx
sudo P12_PASS='관리자 인증서 암호' bash deploy/pki/make-pki.sh /etc/nginx/tls <VM_IP 또는 도메인> admin
#   → 서버 인증서(자체 서명 — 도메인이 있으면 certbot 로 교체) · 관리 기기 CA · clients/admin.p12(관리자 PC 브라우저에 가져오기)
sudo cp deploy/nginx.conf /etc/nginx/sites-available/outofstock     # TLS 판. 인증서 없이 시작하려면 deploy/nginx-http-only.conf
sudo ln -sf /etc/nginx/sites-available/outofstock /etc/nginx/sites-enabled/ && sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
bash deploy/vm.sh units      # 앱을 127.0.0.1 로 다시 묶는다(BIND_HOST=auto 가 nginx 사이트를 감지) — nginx 를 건너뛴 직접 접속 차단
bash deploy/vm.sh harden     # 방화벽: 80/443 만(앱 포트 8000 은 닫음)
```
`.env`: `COOKIE_SECURE=true` · `SECURITY_HSTS=true` · `DOCS_ENABLED=false` · (관리 기기 인증서를 강제하려면) `ADMIN_REQUIRE_CLIENT_CERT=true` → `bash deploy/vm.sh restart`.

nginx 가 하는 일: TLS 1.2/1.3(ECDHE + AEAD)·HSTS · 요청률(로그인 계열 분당 10회)·본문 1MB·연결 수 · `/admin/` 은 관리 기기 인증서가 없으면
앱까지 가지도 않고 403 · `/docs` 404 · 숨김 파일 차단 · 실시간 알림(`/events`)은 버퍼링 없이.

> **전달 헤더는 덮어쓴다(덧붙이지 않는다)** — `X-Forwarded-For $remote_addr`(덧붙이면 가짜 IP 로 시도 제한을 피한다), 인증서 확인 결과
> `X-Client-Cert-Verified` 도 nginx 가 덮어쓴다(HTTP 판은 `"NONE"`) — 클라이언트가 보낸 값으로 관리 기기 확인을 통과할 수 없다.
> 그래서 `ADMIN_REQUIRE_CLIENT_CERT=true` 는 앱이 127.0.0.1(또는 compose 내부망)에만 있을 때만 의미가 있다 — vm.sh 가 어긋난 구성을 경고한다.
> **IPv6 를 끈 VM** 이면 `listen [::]…` 줄을 넣지 말 것(nginx 가 못 뜬다 — 기본 설정엔 없음).

(선택) **공인 도메인이 있을 때만** 공인 인증서: `sudo apt install -y certbot python3-certbot-nginx && sudo certbot --nginx -d <도메인>`
(VPN 안에서만 접속되는 VM 은 인증서 발급 검증이 안 된다 — 자체 서명 + 브라우저 예외로 시연).

---

## 보안 체크리스트 (운영 전환 시) — `python tools/security_check.py` 가 대부분을 자동으로 본다

- [ ] **키** — `APP_SECRET_KEY`·`DATA_ENCRYPTION_KEY` 를 서로 다르게(점검 KEY-1~3), 사본은 오프라인 금고.
- [ ] **`.env` 600**(OPS-2) · compose 면 DB 비밀번호는 `.env.db` 에만.
- [ ] **DB 계정 분리** — `roles.sql`(oos_app·oos_ro·oos_owner·oos_backup) → `tools/db_roles_check.py --strict` 0 건.
- [ ] **백업 암호화** — 공개키 2개(DR-1), 분기 복구 훈련(`vm.sh drill` — 92일 넘으면 관리자 헬스 경보).
- [ ] **경계** — nginx TLS + `bash deploy/vm.sh units`(앱 127.0.0.1) · `DOCS_ENABLED=false` · `COOKIE_SECURE=true` · `SECURITY_HSTS=true`.
- [ ] **관리자** — `ADMIN_USERS` 는 자격만, TOTP 등록 · 관리 기기 인증서(`ADMIN_REQUIRE_CLIENT_CERT=true`).
- [ ] **서버** — `bash deploy/vm.sh harden`(키 등록 뒤 `SSH_KEYS_ONLY=1`, 가능하면 `ADMIN_CIDR`) · `status` 의 샌드박스 노출 점수 1.x.
- [ ] **나가는 연결** — (선택) `EGRESS_PROXY=1 bash deploy/vm.sh units`.
- [ ] **로그** — `LOG_FORMAT=json`(LOG-2) · `AUDIT_WITNESS_FILE=/var/lib/outofstock/audit-witness.jsonl`(LOG-3), 증인 파일은 주기적으로 서버 밖에도.
- [ ] **내부 관리계획** — `docs/security/internal_management_plan.md` 의 이름·승인란 채우기, 월 1회 열람 점검(`/admin/pii-access`).
- [x] 항상 켜짐: 모든 경로 정책 집행(기본 거부) · 10분 접근 토큰 + 회전 갱신 토큰(HttpOnly·SameSite=Strict) · CSRF · 5회 잠금 ·
      개인 데이터 FORCE RLS · 봉투 암호화 · 감사 해시 체인 · CSP nonce + Trusted Types · 보안 이벤트 자동 대응 · API 키 가림 · 사칭 차단.

---

## 팀원 PC 로 실데이터 옮기기(스냅샷)

```bash
# VM 에서 — snapshots/outofstock-snapshot-YYYYMMDD-HHMM.jsonl.gz 생성
.venv/bin/python run.py snapshot-export
# 팀원 PC 에서(파일을 받은 뒤)
python run.py initdb
python run.py snapshot-import outofstock-snapshot-20260930-2330.jsonl.gz --yes
```

- 들어가는 것: 종목·일봉·실습 구간·지수·실시간 마지막 값·재무·랭킹·추천·테마·뉴스(+종목 태그·감성)·해외 일봉/시세/재무,
  출처 표시·실데이터 시작일 메타.
- **들어가지 않는 것**: 계정·비밀번호 해시·로그인 토큰·지갑·결제·채팅·친구·학습/접속 로그·KIS 접근 토큰·배치 오류 메시지.
- 들이기는 한 트랜잭션 — 파일이 잘렸거나 변조됐으면 아무것도 바뀌지 않는다.

---

## 트러블슈팅

- **팀원이 접속이 안 됨** → ① 보안그룹/방화벽 ② VM 안에서 `curl 127.0.0.1:8000/health` ③ `<VM_IP>` 가 학교망/VPN 전용인지.
- **`/health` 가 503** → DB 접속 불가. 경로 A `$DC logs db`, 경로 B/C `sudo service postgresql status`.
- **화면은 뜨는데 데이터가 비어 있음** → 시드 안 함. 경로 A `./deploy/deploy.sh`(빈 DB 면 자동 시드), 경로 B/C `run.py real`.
- **`run.py demo` 가 거부됨** → 실데이터가 있는 DB. 정상 보호. 감성·테마만 다시: `run.py recompute`.
- **`POSTGRES_PASSWORD`·`OOS_APP_PASSWORD` … 를 설정하세요** → `.env.db` 가 없거나 값 미설정(compose 운영 오버레이는 DB 비밀번호를 `.env.db` 에서 읽는다).
- **DB 비밀번호를 바꾸고 싶다** → PostgreSQL 은 최초 생성 때만 비번을 잡는다. **볼륨을 지우지 말고**(`down -v` 는 데이터 삭제)
  `$DC exec db psql -U oos -d outofstock -c "ALTER USER oos PASSWORD '<새 값>';"` 후 `.env` 수정 → `./deploy/deploy.sh`.
  경로 B/C 는 `sudo -u postgres psql -c "ALTER USER oos PASSWORD '...';"`.
- **시드가 'could not translate host name' 로 실패** → 비밀번호에 `@ : / %` 가 있음 → hex 비밀번호로 바꾸기(위).
- **nginx 502 Bad Gateway** → 앱이 안 떠 있거나 포트 불일치.
- **화면은 뜨는데 로그인·저장이 403 '허용되지 않은 출처의 요청이에요'** → ① 2026-10-22 이전 nginx 설정(Host 의 포트를 떼는 `$host`)을
  80·443 이 아닌 포트로 쓰는 중 → 새 `deploy/nginx*.conf`(또는 compose 는 새 판으로 `./deploy/deploy.sh`)로 바꾸고 `sudo nginx -t && sudo systemctl reload nginx`
  ② 화면과 API 의 주소(도메인·포트)가 다름 → 같은 주소로 열거나 `.env` 의 `TRUSTED_ORIGINS` 에 화면 출처(개발 서버만).
- **`docker: permission denied`** → `sudo usermod -aG docker $USER` 후 재로그인.
- **로그인하면 429 "시도가 너무 많아요"** → 10분 안에 너무 많이 틀림 — 기다린다(`RATE_LIMIT_BACKEND=postgres` 면 재시작해도 유지).
  계정이 잠겼으면(연속 5회) `.venv/bin/python run.py user-unlock <아이디>` 또는 관리자 `POST /admin/users/{아이디}/unlock`.
- **로그인이 자꾸 풀림·'세션이 종료되었어요'** → 다른 브라우저 계열에서 같은 세션을 쓰면(기기 결속) 세션이 폐기된다 · 유휴 30분 · 절대 12시간.
- **서비스가 샌드박스 때문에 안 뜨는 것 같다** → `bash deploy/vm.sh logs api` 의 Permission denied·Read-only file system 확인 →
  `HARDEN=0 bash deploy/vm.sh units` 로 비교(쓰기 위치는 `var/`·`/var/lib/outofstock` 뿐 — `.env` 의 파일 경로를 거기로).
- **`egress 프록시 확인 실패`**(EGRESS_PROXY=1) → `sudo journalctl -u squid -n 30` · 외부 데이터가 안 들어오면 squid 로그(`/var/log/squid/access.log`)의
  `TCP_DENIED` 호스트를 보고, 필요한 원천이면 `.env` 의 `EGRESS_EXTRA_HOSTS` 에 추가 → `bash deploy/vm.sh units`.
- **`/health` 가 `last_pipeline_partial`** → 23:00 배치 중 일부 소스만 실패. `bash deploy/vm.sh logs scheduler` 또는 관리자 헬스의 `last_run.note` 에서 단계 이름 확인(키·한도).
- **`/health` 가 `news_collection_stopped`** → 뉴스 수집이 차단(429/503)이나 연속 실패로 멈춤. 다음 날 다시 시도.
- **홈 '오늘의 추천 종목'이 비어 있음** → `cd ~/stock-backend && .venv/bin/python tools/reco_check.py`(읽기 전용).
  화면 출력과 같은 내용이 `reco_check_result.txt` 로 저장된다(WinSCP 로 내려받아 공유). 결론 줄이 '안정형 기준에서 모두 빠짐'이면
  데이터 문제가 아니다(카드의 '성향 무관 Top 5 보기'). '데이터 부족(25거래일 미만)'이면 같은 결과 파일의 '실패:' 줄과
  '4. 외부 연결 확인'(KRX·DART 각 1회 조회)을 본다.
- **KRX 시세 단계가 `value too long for type character varying(2000)` 로 실패** → 10-10 이후 판으로 `vm.sh update`
  (initdb 가 `app_meta.value` 를 TEXT 로 넓힘) → `.venv/bin/python run.py fetch-market`(약 5~10분, 시총 상위 150종목 180거래일).

---

## 파일 안내

```
DEPLOY_UBUNTU.md              ← 이 문서
docker-compose.prod.yml       운영 오버레이 — 망 4개·최소 권한 계정·읽기 전용 컨테이너·egress 프록시·edge
env.prod.example · env.db.example   운영 환경파일 견본(보안 기준선 통과) → install -m 600 으로 .env · .env.db
Dockerfile                    runtime(비루트·pip 없음·해시 고정) · backup(pg_dump 16 + age)
deploy/
  vm.sh                       VM(systemd): install·update·units·harden·status·backup·backups·restore·drill·rollback·logs
  deploy.sh                   compose: 배포·업데이트·backup·status
  nginx.conf · nginx-http-only.conf   VM 경계(TLS·mTLS / HTTP 판)
  nginx/compose-http.conf · compose-tls.conf   compose edge
  egress-proxy/               squid 이미지 + 설정(코드의 허용 목록에서 생성 — tools/egress.py)
  sql/roles.sql · pg_hba_tls.conf     DB 계정 분리 · 앱↔DB TLS 접속 규칙
  pki/make-pki.sh             사설 CA·서버·관리 기기 인증서(VM·compose)
  backup/                     PITR(WAL 보관·복원·훈련) · recipients.example.txt(백업 공개키 예시)
  tls/                        compose 인증서 자리(키는 저장소에 넣지 않음)
  outofstock-*.service        (경로 B 수동) 샌드박스 견본 — 보통은 vm.sh install 이 만든다
tools/make_installer.py       VM 설치본(install_outofstock_full.sh) 만들기
tools/security_check.py       운영 설정 기준선 점검
```
