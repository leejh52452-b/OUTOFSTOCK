# 백업·복구(DR) 운영 절차

> 백업은 '복구해 봐야' 백업이다. 이 문서는 무엇을·어떻게 백업하고, 사고 때 어떤 순서로 되살리며, 그것이 실제로 되는지
> 어떻게 정기적으로 확인하는지를 적는다. 훈련 기록은 `docs/dr/drills/`.

## 1. 목표

| 지표 | 목표 | 실측(2026-10-20 훈련, 개발 규모 0.8MB 덤프 · 105MB 클러스터) |
|---|---|---|
| RPO(잃어도 되는 데이터) | 논리 백업만: 24시간 → **WAL 보관 켜면 1분 이하** | PITR: 사고 1초 전 시점으로 복구, 손실 0 |
| RTO(서비스 복구 시간) | 1시간 이내(이 서비스 규모 기준 내부 목표) | 논리 복원·검증 2.3초 · PITR(베이스 백업 포함) 7.8초 · **VM 실제 systemd 에서 `vm.sh restore`(새 DB 복원 → 검증 → 이름 교체 → 재시작) 6.8초**(2026-10-22) |
| 훈련 주기 | 분기 1회(관리자 헬스가 92일 넘으면 경고) + 큰 변경(스키마·키·배포 구조) 뒤 | — |
| 규정 상한(설계 기준) | 전자금융감독규정 제23조 — 핵심업무 복구목표시간 **3시간 이내**·재해복구 전환훈련 연 1회(2026.2.5 시행) → `DR_RTO_TARGET_MIN=180`: 훈련의 실측 RTO 가 넘으면 훈련이 성공해도 관리자 헬스 `dr_rto_exceeded`·수준진단 점검 실패 | 위 실측(수 초)으로 여유 큼 |
| 참고(지급결제) | 한국은행이 감시하는 지급결제 FMI 의 국제 기준 PFMI 원칙 17 — 중요 IT 시스템은 장애 후 **2시간 안** 재개 | 이 서비스는 FMI 가 아니라 참고 목표 — 내부 목표 1시간이 그보다 엄격 |

참고 기준 — 전자금융감독규정 제23조(업무지속성 확보: 백업·재해복구계획·모의훈련, 대책의 연 1회 이상 점검, 핵심업무 복구목표시간),
개인정보의 안전성 확보조치 기준 제11조(위기대응 매뉴얼과 정기 점검, 개인정보처리시스템 백업·복구 계획) · 제7조(암호화).
이 서비스는 모의투자 캡스톤이라 규정 적용 대상은 아니지만, 같은 요구를 '실행 가능한 절차 + 실측 증적'으로 갖추는 것을 목표로 했다.

## 2. 구성

| 층 | 무엇 | 어디서 | 주기 |
|---|---|---|---|
| 논리 백업 | `tools/dr.py backup` — 일관 스냅샷 덤프 + manifest + age 암호화 | VM: systemd 타이머(`vm.sh backup`, 매일 04:30 KST, 샌드박스 — 2-3) · compose: `backup` 작업 컨테이너 | 매일 |
| WAL 보관 | `deploy/backup/pitr_setup.sh` — 1분마다 WAL 조각을 gzip + age 로 보관 | PostgreSQL `archive_command` | 연속 |
| 베이스 백업 | `pg_basebackup` (PITR 의 출발점) | 운영: pgBackRest/WAL-G 권장 | 주 1회 권장 |
| 소산(원격 사본) | `BACKUP_OFFSITE_CMD`(예: `rclone copy {file} remote:oos-backups/`) | 다른 장소·다른 계정 | 백업마다 |
| 감시 | 관리자 헬스(`/admin/health` → `dr`) + 운영 알림 | 앱 | 상시 |

### 2-1. 논리 백업이 하는 일(`tools/dr.py backup`)
1. **일관 스냅샷** — REPEATABLE READ 트랜잭션에서 `pg_export_snapshot()` 을 내보내고, 같은 스냅샷으로 표별 정확한 행 수·감사 체인 머리·원장 마감 머리·분개 합·보안 통제(FORCE RLS 표·정책·트리거)·스키마 버전·데이터 계약 결과를 잰 뒤 `pg_dump --snapshot` 으로 덤프한다. manifest 의 숫자와 덤프 내용이 같은 순간을 가리킨다 — 복구 검증의 기준값.
2. **RLS 표도 빠짐없이** — 개인 데이터 표는 FORCE RLS 라 일반 계정의 `pg_dump` 는 실패하고, `--enable-row-security` 만 켜면 **오류 없이 0행**이 된다(가장 위험한 실패: 백업은 '성공'인데 비어 있음). 백업 전용 계정 `oos_backup`(pg_read_all_data + BYPASSRLS, 쓰기 없음, 접속 2개)을 쓰거나, 없으면 시스템 문맥(`app.rls=off`)으로 덤프한다. 복구 훈련의 행 수 대조가 '빈 백업'을 잡는다(`tests/test_dr.py` 로 고정).
3. **평문을 디스크에 쓰지 않는다** — `pg_dump` 출력 → SHA-256 → `age`(공개키) → 파일. 서버에는 공개키만 있다.
4. **보관** — 최근 14개 + 최근 12개월 각 달의 첫 백업(GFS). 파일·manifest 는 0600, 폴더는 0700.
5. **기록** — `app_meta.backup_last`(관리자 헬스의 '백업 경과 시간') + 감사 로그 `backup.create`. 실패하면 운영 알림.
6. 덤프에 넣는 확장은 `plpgsql` 만(`BACKUP_EXTENSIONS`) — `pg_stat_statements` 같은 운영 확장이 들어가면 슈퍼유저가 아닌 계정으로 복원할 수 없다.

### 2-2. 키 관리
- 키 2개: **운영 키**(담당자 보관) + **금고 키**(오프라인, 다른 사람 보관). 백업은 두 공개키 모두로 암호화 → 한쪽을 잃어도 복구된다.
- 서버에는 `BACKUP_AGE_RECIPIENTS`(공개키 파일)만. 복호화 키(identity)는 복구·훈련할 때만 가져온다(`AGE_IDENTITY`).
- 서버가 털려도 지난 백업은 열리지 않는다. 키를 바꿀 때는 새 공개키를 수신자 파일에 넣고, 예전 키는 '그 키로 잠근 백업의 보관 기간'이 끝날 때까지 금고에 둔다.
- 애플리케이션 데이터 키(`DATA_ENCRYPTION_KEY`, 봉투 암호화 KEK)는 백업 키와 별개다. 복구된 DB 의 암호화 열을 읽으려면 그 키도 있어야 하므로, 훈련이 **암호화 열 전부 복호화**를 확인한다(키 교체 때 예전 키를 빠뜨린 사고를 잡는다).

```bash
age-keygen -o ops.key        # 운영 키 — 출력의 'Public key: age1…' 를 수신자 파일에
age-keygen -o vault.key      # 금고 키 — 인쇄·USB 로 오프라인 보관, 서버에 두지 않는다
{ age-keygen -y ops.key; age-keygen -y vault.key; } > backup.recipients    # 공개키만 서버로
sudo install -d -m 755 /etc/outofstock && sudo install -m 644 backup.recipients /etc/outofstock/backup.recipients   # (VM)
```

### 2-3. VM 에서의 격리(2026-10-22)
- 백업 폴더 기본값 `~/outofstock-backups`(`BACKUP_DIR`, 0700). API·스케줄러·실시간 서비스는 홈을 빈 tmpfs 로 보는 샌드박스라
  **백업 폴더를 볼 수 없다**(웹 취약점으로 서버가 털려도 백업 파일에 닿지 않음). 홈 밖에 두면 `InaccessiblePaths` 로 명시 차단.
- 백업 서비스(`outofstock-backup.service`)는 쓰기를 **백업 폴더만** 연다(`ReadWritePaths`). 외부 보관 도구가 다른 폴더에 써야 하면
  `BACKUP_RW_PATHS`(설치·업데이트 때 서비스 정의에 기록). 홈은 읽기 전용(rclone 설정·키를 읽을 수 있게).
- 나가는 연결: 다른 서비스는 프록시 경유 + 커널 직접 연결 차단이지만, 백업 서비스는 소산 명령(`BACKUP_OFFSITE_CMD`)이 있을 수 있어
  막지 않는다 — 내보내는 것은 암호문뿐이다.
- 수신자(공개키) 파일: VM 은 `BACKUP_AGE_RECIPIENTS=/etc/outofstock/backup.recipients` — `sudo install -m 644` 로 root 소유
  (공개키라 비밀은 아니지만, 서비스 계정이 수신자를 바꿔치기할 수 없게). compose 는 `deploy/backup/recipients.txt`(git·설치본 제외 —
  없으면 `deploy.sh` 가 `recipients.example.txt` 로 만들어 두고, 공개키를 넣기 전까지 백업은 '수신자 없음'으로 실패한다 — 평문 백업을
  조용히 만들지 않는다. 예전엔 저장소에 실제 파일이 있어 업데이트가 운영 키를 덮어쓸 수 있었다).

## 3. 처음 설정(VM)

```bash
sudo apt install age postgresql-client
# 백업 전용 계정(선택이지만 권장) — roles.sql 에 backup_pw 를 주면 만든다
sudo -u postgres psql -d outofstock -v owner_pw="'…'" -v app_pw="'…'" -v ro_pw="'…'" -v backup_pw="'…'" -f deploy/sql/roles.sql
# .env
BACKUP_AGE_RECIPIENTS=/etc/outofstock/backup.recipients
BACKUP_DATABASE_URL=postgresql+psycopg://oos_backup:…@localhost:5432/outofstock
BACKUP_OFFSITE_CMD=rclone copy {file} offsite:oos-backups/        # 선택
# WAL 보관(PITR) — PostgreSQL 이 한 번 재시작된다
sudo RECIPIENTS=/etc/outofstock/backup.recipients bash deploy/backup/pitr_setup.sh
bash deploy/vm.sh backup && bash deploy/vm.sh backups              # 첫 백업 확인
```

## 4. 정기 훈련

| 훈련 | 명령 | 확인하는 것 |
|---|---|---|
| 논리 복원 훈련(분기) | `AGE_IDENTITY=… DRILL_ADMIN_URL=… bash deploy/vm.sh drill` (= `tools/dr.py drill`) | 아래 10가지 |
| 시점 복구 훈련(반기) | `sudo AGE_IDENTITY=… bash deploy/backup/pitr_drill.sh` | 베이스 백업 검증 → WAL 재생 → 사고 1초 전 상태 |
| 빠른 점검(매주, 자동화 가능) | `python tools/dr.py verify --identity …` | 암호문·덤프 해시, 복호화, 목록 — 복원 없이 수 초 |

논리 복원 훈련은 운영 DB 를 건드리지 않고 **새 DB**(가능하면 다른 인스턴스)에 복원해 확인한다.
① 암호문 SHA-256 = manifest ② 복원 대상에 GRANT 대상 역할이 있는가 ③ 복호화 → `pg_restore`(단일 트랜잭션, 오류 시 중단) + 덤프 SHA-256
④ 표별 행 수 = manifest ⑤ 감사 로그 해시 체인 재계산 + 머리 해시 ⑥ 원장 마감 해시 사슬·시산표 0·머리 해시
⑦ 데이터 계약(critical) — 복원으로 생긴 새 위반 없음 ⑧ 암호화 열 전부 복호화 ⑨ FORCE RLS·정책·트리거·스키마 버전
⑩ 복원 DB 로 앱 기동 — 스키마 변경 0건, `/health`·`/indices`, 가입 → 로그인 → 지갑(분개)까지.
결과는 MD·JSON 보고서 + `app_meta.dr_drill_last` + 감사 로그 `dr.drill`. 실패하면 운영 알림.

훈련 계정(`DRILL_ADMIN_URL`) — 데이터베이스를 만들 수 있는 계정. 운영과 다른 인스턴스의 DBA 계정이 이상적이다.
같은 인스턴스라면 `CREATE ROLE oos_drill LOGIN CREATEDB PASSWORD '…'` 정도로 충분하다(복원은 `--no-owner`).

## 5. 사고별 대응

| 상황 | 순서 | 예상 손실 |
|---|---|---|
| **실수로 지운 데이터·잘못 돈 배치** | ① 사고 시각 확인(감사 로그·앱 로그) ② `pitr_drill.sh` 와 같은 절차로 별도 인스턴스에 '사고 직전'까지 복구 ③ 필요한 행만 옮기거나, 전체를 되돌릴 땐 아래 '전체 복원' | 0 (WAL 보관 중이면) |
| **DB 디스크 손상·서버 소실** | ① 새 서버에 PostgreSQL + `roles.sql` ② 최신 논리 백업 복원(`vm.sh restore`) 또는 베이스 백업 + WAL 로 최신 시점까지 ③ 앱 기동·헬스 | ≤ 1분(WAL) / ≤ 24시간(논리만) |
| **서버 탈취·랜섬웨어** | ① 격리 ② 새 서버 구축(이미지 재사용 금지) ③ 소산 사본에서 복원 — 백업은 오프라인 키로만 열려 탈취범이 바꿔치기·열람 못 함 ④ 모든 비밀값·세션 폐기(`/admin/users/*/sessions/revoke`, `APP_SECRET_KEY` 교체) | 마지막 소산 사본 시점 |
| **운영 키 분실** | 금고 키로 복구 → 새 키 쌍 발급 → 수신자 파일 교체 | 없음 |
| **백업이 매일 실패** | 관리자 헬스 `backup_failed`/`backup_stale` + 알림 → `bash deploy/vm.sh backup` 으로 오류 확인 | — |

### 전체 복원(VM) — `bash deploy/vm.sh restore <파일>`
1. 해시·복호화·목록 확인(복원 없이) → 2. 지금 상태를 한 번 더 백업 → 3. **새 DB 에 복원**(서비스는 계속 동작, 한 트랜잭션)
→ 4. manifest 대비 검증(읽기만) → 5. 서비스 정지 → 6. **두 DB 이름을 한 트랜잭션으로 맞바꿈** → 7. 서비스 시작·헬스체크.
이전 DB 는 `<이름>_pre_<시각>` 으로 남는다(확인 뒤 삭제). 어느 단계에서 실패해도 운영 DB 는 그대로다.

## 6. 감시(관리자 헬스 `dr`)

| 이슈 코드 | 뜻 |
|---|---|
| `backup_failed` | 마지막 백업 시도가 실패 |
| `backup_stale` | 마지막 성공 백업이 26시간(`BACKUP_MAX_AGE_HOURS`)보다 오래됨 |
| `backup_unencrypted` | 마지막 백업이 평문(암호화 키 미설정) |
| `dr_drill_failed` | 마지막 복구 훈련 실패 |
| `dr_drill_overdue` | 92일(`DR_DRILL_MAX_AGE_DAYS`) 넘게 훈련하지 않음 — 또는 백업은 있는데 복구해 본 적이 없음 |

공개 `/health` 에는 이 항목들을 싣지 않는다(백업 상태는 공격자에게 줄 정보가 아니다).

## 7. 훈련이 찾아낸 문제(2026-10-20)

| # | 발견 | 원인 | 조치 |
|---|---|---|---|
| 1 | **기존 일일 백업이 매일 실패하고 있었다** — 아무도 몰랐다 | V010 FORCE RLS 이후 일반 계정 `pg_dump` 가 개인 데이터 표에서 오류 | `tools/dr.py`(백업 전용 계정·시스템 문맥) + 실패 알림 + 관리자 헬스 |
| 2 | `--enable-row-security` 만 켜면 오류 없이 **빈 표** | 정책이 주체 없는 세션에 0행을 보여 줌 | manifest 행 수 대조로 탐지, 테스트로 고정 |
| 3 | 덮어쓰기 복원(`pg_restore --clean`)이 **분할 표에서 실패** | 상속된 PK 제약을 먼저 지우려 함 | 새 DB 에 복원 → 검증 → 이름 맞바꿈(원자적 전환) |
| 4 | `pg_stat_statements` 가 덤프에 포함 → 일반 계정 복원 실패 | 신뢰되지 않은 확장 | `--extension=plpgsql` |
| 5 | PITR 인스턴스가 기동하자마자 중단 | 복구 중엔 `max_connections` 등이 원본 이상이어야 함 | 원본 값 복사 |
| 6 | PITR 인스턴스가 **운영 WAL 보관소에 쓸 뻔** | 베이스 백업에 딸려 온 `postgresql.auto.conf`(archive_mode=on) | 훈련 인스턴스는 보관 끔(뒤에서 덮어씀) |
| 7 | WAL 보관 용량 — 16MB 조각을 1분마다 | `archive_timeout` 강제 전환 | gzip -1 (16MB → 약 0.1~1MB) |

## 8. 파일

| 파일 | 역할 |
|---|---|
| `tools/dr.py` | backup · list · verify · drill (+ 복원 검증 `_verify`) |
| `deploy/vm.sh` | `backup` · `backups` · `restore`(새 DB → 검증 → 전환) · `drill` |
| `deploy/backup/pitr_setup.sh` | WAL 보관 켜기 |
| `deploy/backup/wal-archive.sh`, `wal-restore.sh` | 보관/복원 명령(gzip + age, pipefail) |
| `deploy/backup/pitr_drill.sh` | 시점 복구 훈련 |
| `deploy/sql/roles.sql` | `oos_backup`(읽기 전용 + BYPASSRLS) |
| `tests/test_dr.py` | RLS 표 덤프 · 빈 백업 위험 · 변조·잘못된 키 · 키 링 · 보관 주기 · 헬스 · (선택) 전체 훈련 |
| `docs/dr/drills/` | 훈련 보고서 |
