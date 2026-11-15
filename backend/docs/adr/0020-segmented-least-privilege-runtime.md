# ADR-0020 실행 환경도 최소 권한 — 망 분리 · 읽기 전용 컨테이너 · systemd 샌드박스 · 나가는 연결 3중 통제 · 설정은 코드로 검사

| 항목 | 내용 |
|---|---|
| 상태 | 채택 · 2026-10-21 |
| 범위 | 제로트러스트 — 네트워크·시스템 · 공급망 |
| 코드 | `docker-compose.prod.yml` · `Dockerfile` · `deploy/deploy.sh` · `deploy/vm.sh`(units·harden·EGRESS_PROXY) · `deploy/egress-proxy/` · `deploy/nginx*.conf` · `tools/egress.py`·`security_check.py`·`check_lock.py`·`make_installer.py` |
| 시험 | `tests/test_deploy_config.py`(27개) · CI image 작업 · 검증 보고서 N-·S-·D- |

## 맥락
기본 배포는 HTTP 80, 앱이 `0.0.0.0:8000` 으로 열려 있었고, systemd 격리 설정이 없었다(`systemd-analyze security` 9.0 UNSAFE).
compose 는 앱이 공식 postgres 이미지의 **슈퍼유저**로 붙을 수 있었다(RLS 무력화). 서버가 뚫리면 어디로든 나갈 수 있었다.

## 결정
- **compose** — 망 4개(outside·web·data·egress, 뒤 셋 internal). 공개 포트는 edge(nginx-unprivileged) 하나. 모든 컨테이너 비루트·
  루트 FS 읽기 전용·capability 전부 제거·권한 상승 금지·pids/메모리 상한. 앱은 `oos_app`(비밀번호 없으면 기동 거부),
  DB 비밀번호는 `.env.db` 에만(앱 컨테이너엔 자기 접속 문자열뿐). 앱↔DB TLS(verify-full, 평문 TCP 거부 hba) 선택.
- **VM(systemd)** — 서비스 샌드박스: 코드·시스템 읽기 전용, **홈 폴더 숨기고 코드 폴더만 읽기 전용으로 다시 보이게**(같은 계정의
  SSH 키를 못 읽게), 시스템 호출 묶음 제한, capability 0, 코어 덤프 금지. nginx 가 있으면 앱은 127.0.0.1(자동).
  `harden`: 방화벽 기본 거부·SSH 강화·자동 보안 업데이트·커널 설정·DB 접속 기록. 선택값은 서비스 파일에 남겨 update 때 유지.
- **나가는 연결 3중** — ① 앱 허용 목록(`egress.py`) ② squid 프록시(같은 목록에서 생성 + 내부 주소 거부 = DNS rebinding 차단)
  ③ (VM) `IPAddressDeny=any` — 프록시를 우회한 직접 연결은 커널이 막는다 / (compose) 앱은 인터넷 없는 내부망.
- **공급망** — 해시 고정 잠금 파일(`--require-hashes`), 실행 이미지에 pip 없음, CI 액션 커밋 SHA 고정, SBOM, 설치본 SHA-256·끝 표식.
- **설정을 코드로 검사** — 합쳐진 compose 설정·Dockerfile·생성된 systemd 파일·nginx 4종·hba·roles.sql·운영 견본·잠금 파일·설치본을
  시험 27개가 검사한다. 운영 중에는 `tools/security_check.py`(기준선 24항목, 근거 조항 표시).

## 검토한 대안
| 대안 | 버린 이유 |
|---|---|
| 방화벽만 | 서비스 하나가 뚫리면 같은 서버 안에서는 아무것도 막지 못한다 |
| 쿠버네티스 NetworkPolicy·PodSecurity | 학교 VM·단일 서버 규모에 과함 — compose 망 + systemd 로 같은 성질 |
| 전용 시스템 계정으로 VM 실행 | 이상적이지만 팀원이 본인 계정으로 배포·업데이트하는 흐름이 깨진다 → ProtectHome=tmpfs 로 같은 효과(홈 격리) |

## 결과(실측)
- 노출 점수 9.0 → **1.2**(postgresql·ssh 기본 9.6). 설치 54초 · 업데이트 8.1초 · 복원 6.8초(systemd 컨테이너).
- 2026-10-23 독립 점검: compose 의 앱이 0.0.0.0 에서 들어 data·egress 망의 컨테이너(특히 인터넷에 닿는 egress 프록시)가 edge 를 건너뛰어
  전달 헤더·인증서 확인 헤더를 위조할 수 있었다 → 앱은 web 망 별칭 `app-web` 에서만 듣는다(다른 망에서 연결 거부 실측). 이미지 기본값은
  `--forwarded-allow-ips` 를 열지 않는다(같은 컨테이너만 신뢰).
- 검증 중 발견해 고친 결함 18건(검증 보고서) — 예: update 가 옛 스크립트로 서비스 파일을 쓰던 문제, IPv6 없는 VM 에서 nginx 기동 실패,
  80·443 이 아닌 포트에서 nginx 가 Host 의 포트를 떼어 웹 로그인이 전부 403 이던 문제.
- 감수: 백업 서비스는 원격 보관 도구 때문에 홈을 '읽기 전용'까지만 숨긴다. 이미지 베이스는 태그 고정(다이제스트 고정은 다음 단계).

## 검증
`python -m pytest -q tests/test_deploy_config.py` · `bash deploy/vm.sh status` · `python tools/security_check.py`
