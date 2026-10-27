# Never Fade away — 비공개 연애 아카이브

기존 시제품의 다크 테마와 화면/기능을 유지하면서 저장 방식을 브라우저 `localStorage`에서 서버로 옮긴 Docker 기반 프로젝트입니다.

## 기술 구성

- **Python 3.12 + FastAPI**: 로그인, 기록 API, 사진 업로드/조회
- **MySQL 8.4**: 기록 하나당 행 하나로 저장하는 `records` 테이블을 사용합니다. 날짜/종류 인덱스와 태그 JSON 컬럼을 두어 SQL로 읽기 쉽도록 구성했습니다.
- **MinIO**: 사진 원본을 별도 저장합니다(S3 호환 API).
- **Docker Compose**: 앱, DB, 사진 저장소를 격리된 내부 네트워크에서 실행합니다.
- **백업 스크립트**: MySQL 덤프와 사진 저장 볼륨을 별도 파일로 내보냅니다.

현재는 1인용 단일 계정입니다. 로그인 암호는 `.env`에 저장하고, 세션 쿠키는 `HttpOnly`와 `SameSite=Strict`를 적용합니다. 이것은 개인 프로젝트용 출발점이며, 인터넷에 공개하기 전에는 아래 보안·백업 절차를 따라주세요.

## 1. 준비

Docker Desktop(Windows/macOS) 또는 Docker Engine + Compose 플러그인이 필요합니다. 프로젝트 폴더에서 실행합니다.

```bash
cp .env.example .env
```

`.env`의 **모든** `REPLACE_...` 값을 각각 다른 강력한 값으로 바꾸세요. `ARCHIVE_PASSWORD`는 로그인 비밀번호이고, `SESSION_SECRET`, `MYSQL_PASSWORD`, `MYSQL_ROOT_PASSWORD`, `MINIO_ROOT_PASSWORD`는 서로 달라야 합니다. 비밀번호에 `@`, `:`, `/`, `#` 등 URL에 특별한 의미가 있는 문자를 쓰면 `DATABASE_URL` 해석에 문제가 생길 수 있으므로 영문·숫자·밑줄 위주의 무작위 값을 권장합니다. `.env`는 GitHub에 올리지 마세요.

무작위 값 예시를 만들려면 터미널에서 `python -c "import secrets; print(secrets.token_urlsafe(36))"`를 실행할 수 있습니다. 각 비밀값마다 새로 실행하세요.

## 2. 로컬 실행

```bash
docker compose up -d --build
docker compose ps
```

브라우저에서 `http://localhost:8000`을 열고 `.env`에 설정한 `ARCHIVE_PASSWORD`로 로그인합니다. 상태 확인은 `http://localhost:8000/api/health`입니다. 사진과 기록은 브라우저가 아니라 MySQL/MinIO 볼륨에 저장됩니다.

로그 확인:

```bash
docker compose logs -f app
docker compose logs -f db
```

중지하려면 `docker compose down`을 실행합니다. **데이터를 삭제하는 `docker compose down -v`는 실행하지 마세요.** `-v`는 DB와 사진 볼륨까지 지웁니다.

## 3. 무료 임시 공개 주소

Docker를 실행하는 컴퓨터가 계속 켜져 있고 인터넷에 연결되어 있어야 합니다. 무료 Quick Tunnel을 시도하려면:

1. `.env`에서 `COOKIE_SECURE=true`로 변경합니다.
2. `docker compose up -d --build`로 앱을 실행합니다.
3. `docker compose --profile tunnel up -d tunnel`을 실행합니다.
4. `docker compose logs -f tunnel`에서 `https://....trycloudflare.com` 주소를 찾습니다.

Quick Tunnel 주소는 **임시 주소**이며 재시작 시 바뀔 수 있고 가용성 보장이 없습니다. 개인 기록을 안정적으로 장기간 쓰려면 나중에 도메인과 고정 Cloudflare Tunnel을 설정하거나, 접근을 VPN으로 제한하는 편이 좋습니다. 포트 8000, 3306, 9000, 9001을 공유기에서 직접 포트포워딩하지 마세요. DB와 MinIO 콘솔은 인터넷에 노출하면 안 됩니다.

테스트만 할 때는 `COOKIE_SECURE=false`를 유지하고 `http://localhost:8000`으로 접속하세요. HTTPS 공개 터널에서는 `COOKIE_SECURE=true`를 사용하세요.

## 4. 백업

```bash
./backup.sh
```

Windows PowerShell에서는 WSL/Git Bash에서 실행하거나 `backup.sh` 안의 두 Docker 명령을 순서대로 실행할 수 있습니다. `backups/`에 MySQL SQL 덤프와 사진 볼륨 tar.gz 파일이 생성됩니다.

**같은 컴퓨터의 `backups/`만으로는 백업이 충분하지 않습니다.** 생성된 두 파일을 다른 물리 장치 또는 암호화된 별도 저장소에 복사하세요. 기록 DB와 사진 파일은 각각 백업되어야 합니다. 주기적으로 복구 테스트도 하세요.

MySQL 복원 예시(기존 DB 내용을 덮어쓸 수 있으므로 실행 전 현재 백업을 별도로 보관):

```bash
cat backups/mysql-YYYYMMDD-HHMMSS.sql | docker compose exec -T db sh -c 'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" archive'
```

사진 볼륨 복원은 서비스 중지 후 `photodata` 볼륨에 아카이브를 풀어야 합니다. 사진 백업을 복원할 때는 해당 시점의 DB 백업도 함께 복원해야 기록과 사진이 일치합니다.

## 5. 기존 시제품에서 가져오기

기존 HTML 시제품이 브라우저 `localStorage`에 저장한 기록은 이 서버에 자동으로 이동하지 않습니다. 시제품의 **백업과 설정 → JSON 내보내기**로 JSON 파일을 내려받은 다음, 새 서비스에서 **JSON 가져오기**를 사용하세요. 사진은 시제품 JSON에 data URL로 포함되어 있을 수 있습니다. 파일이 크면 브라우저 메모리 한계가 생길 수 있으니 사진이 많은 경우 기록을 나눠 이전하거나 사진을 따로 보관하세요.

## 6. 데이터 구조와 수정 방법

```sql
USE archive;
SHOW TABLES;
SELECT record_type, COUNT(*) AS record_count FROM records GROUP BY record_type;
SELECT record_id, record_date, title, category, place FROM records ORDER BY record_date DESC LIMIT 20;
```

기록은 `records` 테이블에 한 행씩 저장하고, 태그 목록만 JSON 컬럼으로 보관합니다. 현재 UI와의 호환을 위해 저장 API는 기록 목록 전체를 트랜잭션으로 교체합니다. 1인용 초기 버전에 단순하지만, 나중에 동시 편집이 필요해지면 개별 생성/수정/삭제 API로 나눌 수 있습니다.

## 주의 사항

- 로그인은 단일 비밀번호 계정입니다. 비밀번호를 길고 유일하게 설정하고 외부에 공유하지 마세요.
- 현재 API는 기본적인 로그인 지연과 세션 확인을 갖췄지만, 정교한 로그인 시도 제한·2단계 인증·감사 로그는 아직 없습니다. 민감한 실제 기록을 공개 인터넷에서 장기간 사용하기 전에는 추가 보안 강화가 필요합니다.
- 삭제한 사진의 파일은 현재 자동으로 지워지지 않아 MinIO에 고아 파일이 남을 수 있습니다. 저장 공간을 정리할 때는 DB 참조와 백업을 확인하고 수동으로 진행하세요.
- Docker 이미지와 패키지를 업데이트하기 전 백업을 먼저 수행하세요.
