#!/usr/bin/env bash
# 사설 PKI — (1) 관리 기기 인증 CA + 관리자 기기 인증서(mTLS) (2) 서버 인증서(도메인·공인 인증서가 없을 때)
#            (3) compose 운영 오버레이용: 내부 서비스 CA + DB 서버 인증서(앱↔DB TLS)
#
#   sudo bash deploy/pki/make-pki.sh /etc/nginx/tls [서버이름또는IP] [관리자이름]        # VM(호스트 nginx)
#   sudo bash deploy/pki/make-pki.sh compose deploy/tls [서버이름또는IP] [관리자이름]     # docker compose 운영 오버레이
#   → admin-ca.crt(nginx 가 신뢰) · server.crt/key(nginx) · clients/<관리자>.p12(관리자 브라우저에 가져오기)
#   → compose: edge/(nginx) · db/(db.crt·db.key, SAN=db) · trust/ca.crt(앱이 DB 인증서를 검증할 내부 CA)
#
# 원칙: 비밀키는 만든 곳에서 나가지 않는다(umask 077). 관리자 인증서는 1년, CA 는 3년. 분실·퇴사 시 CA 를 다시 만들거나
# (규모가 커지면) CRL 을 운영한다. 키 알고리즘은 ECDSA P-256(짧은 키·빠른 핸드셰이크, KCMVP 검증 대상 곡선).
set -euo pipefail
MODE=vm
if [[ "${1:-}" == "compose" ]]; then MODE=compose; shift; fi
OUT="${1:-./tls}"; NAME="${2:-$(hostname -I 2>/dev/null | awk '{print $1}')}"; ADMIN="${3:-admin}"
NAME="${NAME:-localhost}"
umask 077
EDGE="$OUT"; KEYS="$OUT"                       # KEYS: CA 비밀키·관리자 인증서 — compose 면 컨테이너에 붙는 폴더 밖
[[ "$MODE" == "compose" ]] && EDGE="$OUT/edge"
mkdir -p "$EDGE" "$KEYS/clients"
OUT="$(cd "$OUT" && pwd)"; EDGE="$(cd "$EDGE" && pwd)"; KEYS="$(cd "$KEYS" && pwd)"
cd "$EDGE"

if [[ ! -f "$KEYS/admin-ca.key" ]]; then
  openssl ecparam -name prime256v1 -genkey -noout -out "$KEYS/admin-ca.key"
  openssl req -x509 -new -key "$KEYS/admin-ca.key" -sha256 -days 1095 -out admin-ca.crt \
    -subj "/O=OUTOFSTOCK/OU=Admin Device CA/CN=OUTOFSTOCK Admin Device CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign"
  echo "관리 기기 CA 생성: $EDGE/admin-ca.crt"
fi

if [[ ! -f server.key ]]; then
  if [[ "$NAME" =~ ^[0-9.]+$ ]]; then SAN="IP:$NAME"; else SAN="DNS:$NAME"; fi
  openssl ecparam -name prime256v1 -genkey -noout -out server.key
  openssl req -x509 -new -key server.key -sha256 -days 397 -out server.crt -subj "/O=OUTOFSTOCK/CN=$NAME" \
    -addext "subjectAltName=$SAN" -addext "extendedKeyUsage=serverAuth" -addext "keyUsage=critical,digitalSignature"
  echo "서버 인증서(자체 서명) 생성: $EDGE/server.crt ($SAN) — 도메인이 있으면 공인 인증서로 교체 권장"
fi

C="$KEYS/clients/$ADMIN"
openssl ecparam -name prime256v1 -genkey -noout -out "$C.key"
openssl req -new -key "$C.key" -out "$C.csr" -subj "/O=OUTOFSTOCK/OU=Admin Device/CN=$ADMIN"
printf "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n" > "$C.ext"
openssl x509 -req -in "$C.csr" -CA admin-ca.crt -CAkey "$KEYS/admin-ca.key" -CAserial "$KEYS/admin-ca.srl" -CAcreateserial -sha256 -days 365 \
  -extfile "$C.ext" -out "$C.crt"
PASSOPT=(); [[ -n "${P12_PASS:-}" ]] && PASSOPT=(-passout env:P12_PASS)      # 비대화식: P12_PASS 환경변수
openssl pkcs12 -export -inkey "$C.key" -in "$C.crt" -certfile admin-ca.crt -name "OUTOFSTOCK admin ($ADMIN)" \
  "${PASSOPT[@]}" -out "$C.p12"
rm -f "$C.csr" "$C.ext"
chmod 644 admin-ca.crt server.crt
echo "관리자 기기 인증서: $C.p12 — 관리자 PC 브라우저에 가져오기(가져올 때 위에서 정한 암호 입력). 키 파일은 이 서버에서 지울 것."

[[ "$MODE" == "compose" ]] || exit 0

# ── compose: 컨테이너가 읽을 수 있게 소유자를 맞춘다(키는 그 서비스 사용자만) ──
#    nginx-unprivileged = uid 101 · 공식 postgres 이미지 = uid/gid 999
if [[ "$(id -u)" -eq 0 ]]; then
  chown 101:101 server.key && chmod 400 server.key
else
  echo "[!] root 가 아니라 소유자를 못 바꿨습니다 — sudo chown 101:101 $EDGE/server.key" >&2
fi

cd ..
mkdir -p db trust
# 내부 서비스 CA — 관리 기기 CA 와 분리(용도별 신뢰 분리). CA 비밀키는 컨테이너에 붙는 폴더(trust/) 밖에 둔다
if [[ ! -f service-ca.key ]]; then
  openssl ecparam -name prime256v1 -genkey -noout -out service-ca.key
  openssl req -x509 -new -key service-ca.key -sha256 -days 1095 -out trust/ca.crt \
    -subj "/O=OUTOFSTOCK/OU=Internal Service CA/CN=OUTOFSTOCK Internal Service CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign"
  echo "내부 서비스 CA 생성: $OUT/trust/ca.crt"
fi
if [[ ! -f db/db.key ]]; then
  openssl ecparam -name prime256v1 -genkey -noout -out db/db.key
  openssl req -new -key db/db.key -out db/db.csr -subj "/O=OUTOFSTOCK/CN=db"
  printf "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:db,DNS:localhost\n" > db/db.ext
  openssl x509 -req -in db/db.csr -CA trust/ca.crt -CAkey service-ca.key -CAserial service-ca.srl -CAcreateserial -sha256 -days 397 \
    -extfile db/db.ext -out db/db.crt
  rm -f db/db.csr db/db.ext
  echo "DB 서버 인증서: $OUT/db/db.crt (SAN: db) — 앱은 sslmode=verify-full 로 이 이름까지 확인한다"
fi
chmod 644 trust/ca.crt db/db.crt
if [[ "$(id -u)" -eq 0 ]]; then
  chown root:999 db/db.key && chmod 640 db/db.key            # PostgreSQL: root 소유 + DB 그룹 읽기(0640)를 허용한다
else
  echo "[!] root 가 아니라 소유자를 못 바꿨습니다 — sudo chown root:999 $OUT/db/db.key && sudo chmod 640 $OUT/db/db.key" >&2
fi
echo "compose 준비 완료 — .env.db 에 DB_TLS=on · DB_HBA_FILE=/etc/oos/pg_hba_tls.conf · DB_SSL_QUERY=?sslmode=verify-full&sslrootcert=/tls/ca.crt"
echo "                   그리고 .env 에 EDGE_MODE=tls · COOKIE_SECURE=true · SECURITY_HSTS=true"
