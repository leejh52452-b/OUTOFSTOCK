"""데이터 사전·계보 문서가 코드와 같은지(드리프트 방지) + 가명정보 내보내기 k-익명성."""
import csv
from collections import Counter

import pytest

from app import db
from app.services import auth, datadocs, pseudonymize


def test_generated_docs_match_committed_files():
    for name, fn in (("data_dictionary.md", datadocs.dictionary_md), ("lineage.md", datadocs.lineage_md)):
        committed = (datadocs.ROOT / "docs" / "data" / name).read_text(encoding="utf-8")
        assert committed == fn(), f"docs/data/{name} 가 코드와 다르다 — python run.py data-docs 로 다시 생성"


def test_every_table_is_documented():
    md = datadocs.dictionary_md()
    for t in db.metadata.sorted_tables:
        assert f"## {t.name}\n" in md


def test_lineage_edges_point_to_known_tables():
    tables = {t.name for t in db.metadata.sorted_tables}
    for up, down, _via in datadocs.EDGES:
        for x in (up, down):
            assert x in datadocs.SOURCES or x in tables or x.startswith("analytics."), x


def test_pseudonymized_export_is_k_anonymous_and_unlinkable(tmp_path):
    for i in range(40):
        u = f"pz-user{i:02d}"
        auth.register(u, "Pz-pass-2026")
        db.execute("UPDATE app_user SET created_at=:c WHERE username=:u", {"c": f"2026-0{1 + i % 3}-15T10:00:00", "u": u})
        if i % 4:
            db.execute("INSERT INTO survey_result(user_id, type, score, created_at) VALUES(:u, :t, 10, '2026-10-01T09:00:00')",
                       {"u": u, "t": ["안정형", "중립형", "공격형"][i % 3]})
    db.execute("INSERT INTO survey_result(user_id, type, score, created_at) VALUES('pz-user00', '희귀형', 1, '2026-10-01T09:00:00')")
    rows, rep = pseudonymize.build(k=5)
    groups = Counter(tuple(r[q] for q in pseudonymize.QI) for r in rows)
    assert min(groups.values()) >= 5 and rep["min_group"] >= 5
    assert rep["records_in"] == 40 and rep["records_out"] + rep["suppressed"] == 40
    assert all("pz-user" not in str(r) for r in rows)                        # 아이디 없음
    rows2, _ = pseudonymize.build(k=5)
    assert {r["pid"] for r in rows}.isdisjoint({r["pid"] for r in rows2})   # 내보낼 때마다 다른 가명(연결 불가)
    with pytest.raises(ValueError):
        pseudonymize.export(str(tmp_path / "x.csv"), purpose="")
    out = pseudonymize.export(str(tmp_path / "x.csv"), k=5, purpose="성향별 학습 행태 분석(캡스톤 보고서)")
    with open(out["path"], encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == out["records_out"]
    assert db.query("SELECT 1 FROM audit_log WHERE action='privacy.pseudonymized_export'")
