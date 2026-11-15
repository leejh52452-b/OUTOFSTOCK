# deploy/tls — compose 운영 오버레이가 쓰는 인증서 자리(파일은 git 에 넣지 않는다)

```bash
sudo bash deploy/pki/make-pki.sh compose deploy/tls oos.example.ac.kr   # 또는 서버 IP
```

| 폴더 | 마운트 | 내용 | 권한 |
|---|---|---|---|
| `edge/`  | edge(nginx) `/etc/nginx/tls` | `server.crt` `server.key` `admin-ca.crt` | 키는 nginx(uid 101)만 읽기 |
| `db/`    | db `/tls` | `db.crt` `db.key`(SAN: db) | 키는 root:999 0640(공식 postgres 이미지 그룹) |
| `trust/` | app·scheduler·realtime·backup `/tls` | `ca.crt`(DB 인증서를 검증할 내부 서비스 CA) | 공개 |

`.env.db` 에 `DB_TLS=on`, `DB_HBA_FILE=/etc/oos/pg_hba_tls.conf`,
`DB_SSL_QUERY=?sslmode=verify-full&sslrootcert=/tls/ca.crt` 을 넣으면 앱↔DB 도 TLS(평문 TCP 는 DB 가 거부).
공인 인증서(도메인)가 있으면 `edge/server.crt·server.key` 만 바꾸면 된다.
