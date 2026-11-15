# 보안 문서 — 어디서부터 읽나

이 서비스는 전자금융업자가 아니다. 금융권(공기업) 기준을 **설계 기준**으로 삼았고, 모든 '했다'에는 코드·시험·실측을 붙였다.

| 읽는 목적 | 문서 |
|---|---|
| **한 장 요약 — 금융보안원 「금융보안 수준진단 프레임워크」 45개 항목 자가진단(성숙도·CSF 2.0·개선 전후·레이더, 근거를 기계가 확인)** | [`self_assessment.md`](self_assessment.md) |
| 자산 목록·중요도(C·I·A)·위협 38개의 가능성×영향(고유·잔여)·처리·담당·히트맵 | [`risk_register.md`](risk_register.md) |
| **IT 내부통제 — 변경관리 배포 게이트·장애 자동 판정·사고보고 초안(시행세칙 제7조의4)·경보 확인·상향·외부 관제 연동** | [`../adr/0026-it-internal-control.md`](../adr/0026-it-internal-control.md) · [`compliance_mapping.md`](compliance_mapping.md) §2-3 |
| 취약점 자가 점검(모의 침투 — 금융보안원 침투테스트 가이드라인 4단계·ATT&CK·KISA 항목, 첫 실행에서 IDOR 1건 발견·수정) | [`pentest/`](pentest/) |
| 전체 그림 — 제로트러스트를 어떻게 코드로 옮겼고 어디까지 왔나(KISA 2.0 성숙도 · 금융보안원 21개 항목 자가 점검) | [`zero_trust.md`](zero_trust.md) |
| 무엇을 무엇으로부터 지키나 — 자산·신뢰 경계·STRIDE 38개 위협·남은 위험 | [`threat_model.md`](threat_model.md) |
| 규정 조항별로 — 개인정보 안전성 확보조치(2025.10.31 개정)·전자금융감독규정(제13조 ①1~11호)·금감원 IT 안정성 가이드라인·사고 대응·**자율보안(수준진단·CSF 2.0)**·**IT 내부통제·사고보고(시행세칙 제7조의4)·취약점 점검(제37조의2)**·N²SF·BCBS 239 | [`compliance_mapping.md`](compliance_mapping.md) |
| 외부 연동(데이터 원천·결제·알림·공급망)의 중요도·끊김 영향·대체 경로 — 제3자 IT 리스크 | [`third_party_register.md`](third_party_register.md) |
| 코드 수준 — 행안부 개발보안 설계 20 · 구현 49 | [`secure_coding_69.md`](secure_coding_69.md) |
| OWASP Top 10 별 대응과 시험 이름 | [`owasp_checklist.md`](owasp_checklist.md) |
| 사람이 하는 일 — 개인정보 내부 관리계획(견본: 권한·키·접속기록 점검 주기·사고 대응) | [`internal_management_plan.md`](internal_management_plan.md) |
| 실제로 띄워 확인한 것 — 공격 시나리오·배포 경로·독립 점검·정밀 점검(내부 처리)·발견하고 고친 결함 33건(33번째는 취약점 자가 점검이 찾은 IDOR) | [`verification_report_2026-10-21.md`](verification_report_2026-10-21.md) |
| DB 계정 분리 | [`db_roles.md`](db_roles.md) |
| 정적 분석·의존성 점검 결과 | [`scan_report.md`](scan_report.md) |
| 결정의 이유(ADR 0011~0027 — 세션·정책·RLS·암호화·원장·FDS·SSE·CSP·DR·실행 환경·화면 연동·꾸미기·운영 복원력·기기 결속·자율보안·IT 내부통제·DB 저장·수명주기) | [`../adr/README.md`](../adr/README.md) |
| **DB 저장 표준·데이터 수명주기·DB 진단**(용량 PR.IR-04·수명주기 ID.AM-08 — 보관 파일 SHA-256·보관 기록 수정 불가 V018) | [`../adr/0027-db-storage-and-lifecycle.md`](../adr/0027-db-storage-and-lifecycle.md) · [`../db/standards.md`](../db/standards.md) |
| 사고 대응 모의훈련 보고서(원장 변조·이상 송금·대입 공격·쿠키 탈취·파일 변조·서비스 장애 판정·깨진 배포 — 탐지 → 차단 → 복구 시간) | [`../incidents/drills/`](../incidents/drills/) |
| 백업·복구 운영 절차와 훈련 보고서 | [`../dr/README.md`](../dr/README.md) |

## 바로 돌려 보는 점검

```bash
python tools/security_check.py            # 운영 설정 기준선 24항목(근거 조항 표시, FAIL 이면 종료 코드 1)
python tools/db_roles_check.py --strict   # DB 계정 권한
bash tools/security_scan.sh               # 의존성 취약점·bandit·숨은 문자
bash deploy/vm.sh status                  # (VM) 샌드박스 노출 점수·egress·백업·훈련·설정 점검 요약
python run.py self-assess                 # 금융보안 수준진단(45개 항목 — 운영 점검 포함, 결과 저장·감사 체인) — --static 은 근거만
python run.py integrity-check             # 배포 기준선 대비 바뀐·사라진·새 파일(웹셸 의심)
python run.py incident-drill --target-db postgresql+psycopg://…/outofstock_drill   # 사고 대응 모의훈련(격리 DB)
python -m pytest -q tests/test_zero_trust.py tests/test_rls.py tests/test_web_security.py tests/test_deploy_config.py
```
