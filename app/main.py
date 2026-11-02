"""Private relationship archive API.

Small, deliberately readable FastAPI application. All user data is private to
one account; records are stored as a JSON document in MySQL and image bytes in
an S3-compatible MinIO bucket. See README.md before exposing it to the internet.
"""
import collections
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.client import Config
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("relationship_archive")


class RedactGeocodeQuery(logging.Filter):
    """주소는 민감 정보이므로 접속 로그에 검색어를 남기지 않는다."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and str(args[2]).startswith("/api/geocode"):
            record.args = (*args[:2], "/api/geocode", *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(RedactGeocodeQuery())

DATABASE_URL = os.environ["DATABASE_URL"]
ARCHIVE_PASSWORD = os.environ["ARCHIVE_PASSWORD"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
MINIO_ACCESS_KEY = os.environ["MINIO_ROOT_USER"]
MINIO_SECRET_KEY = os.environ["MINIO_ROOT_PASSWORD"]
BUCKET = os.getenv("MINIO_BUCKET", "archive-photos")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)
photo_store = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
    config=Config(signature_version="s3v4"),
    region_name="us-east-1",
)

app = FastAPI(title="Private Relationship Archive", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="strict",
    https_only=COOKIE_SECURE,
    max_age=60 * 60 * 12,
    session_cookie="archive_session",
)


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class RecordsBody(BaseModel):
    records: list[dict[str, Any]]


class ArchiveSettingsBody(BaseModel):
    relationshipStart: str = Field(default="", max_length=10)
    headerPhrase: str = Field(default="", max_length=120)


class CategoryBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class PlaceCategoryBody(BaseModel):
    # 길이/형식 검사는 place_category_fields()에서 한 번에 하고 422로 돌려준다.
    name: str
    color: str


class AnniversaryBody(BaseModel):
    # 이름/날짜 검사는 anniversary_fields()에서 하고 422로 돌려준다. repeatYearly는 진짜 bool만 받는다.
    name: str
    date: str
    repeatYearly: StrictBool = True


ANNIVERSARY_LIMIT = 200


# 장소 전용 카테고리(사람용 archive_categories와 별개).
PLACE_CATEGORY_DEFAULTS = (
    ("음식점", "#e4572e"),
    ("데이트 코스", "#d6409f"),
    ("놀이공원", "#2e86de"),
    ("전시회", "#2a9d8f"),
)
PLACE_CATEGORY_SEED_KEY = "place_categories_seeded"
PLACE_CATEGORY_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def seed_place_categories(connection) -> bool:
    """기본 장소 카테고리를 단 한 번만 넣는다(표준 SQL이라 SQLite 테스트에서도 동작).

    표시(seeded) 키가 있으면 사용자가 지운 기본값이 있어도 다시 만들지 않는다.
    """
    seeded = connection.execute(
        text("SELECT setting_value FROM archive_settings WHERE setting_key=:key"),
        {"key": PLACE_CATEGORY_SEED_KEY},
    ).scalar_one_or_none()
    if seeded is not None:
        return False
    now = int(time.time() * 1000)
    for order, (name, color) in enumerate(PLACE_CATEGORY_DEFAULTS):
        connection.execute(
            text("INSERT INTO place_categories (name, color, sort_order, created_at) VALUES (:name, :color, :sort_order, :created_at)"),
            {"name": name, "color": color, "sort_order": order, "created_at": now},
        )
    connection.execute(
        text("INSERT INTO archive_settings (setting_key, setting_value) VALUES (:key, '1')"),
        {"key": PLACE_CATEGORY_SEED_KEY},
    )
    return True


@app.on_event("startup")
def startup() -> None:
    """Create the records table and private photo bucket if needed."""
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS archive_settings (
                setting_key VARCHAR(100) NOT NULL PRIMARY KEY,
                setting_value VARCHAR(500) NOT NULL DEFAULT ''
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS records (
                record_id VARCHAR(100) NOT NULL PRIMARY KEY,
                record_type VARCHAR(30) NOT NULL DEFAULT 'diary',
                record_date VARCHAR(10) NULL,
                title VARCHAR(500) NOT NULL,
                body MEDIUMTEXT NULL,
                category VARCHAR(100) NULL,
                certainty VARCHAR(100) NULL,
                tags JSON NOT NULL,
                place VARCHAR(500) NULL,
                image VARCHAR(1000) NULL,
                media_type VARCHAR(20) NULL,
                images JSON NULL,
                created_at BIGINT NULL,
                updated_at BIGINT NULL,
                INDEX idx_records_date (record_date),
                INDEX idx_records_type_date (record_type, record_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """))
        for column_sql in (
            "ALTER TABLE records ADD COLUMN media_type VARCHAR(20) NULL",
            "ALTER TABLE records ADD COLUMN images JSON NULL",
            "ALTER TABLE records ADD COLUMN place_kind VARCHAR(20) NULL",
            "ALTER TABLE records ADD COLUMN address VARCHAR(500) NULL",
            "ALTER TABLE records ADD COLUMN latitude DECIMAL(10,7) NULL",
            "ALTER TABLE records ADD COLUMN longitude DECIMAL(10,7) NULL",
            "ALTER TABLE records ADD COLUMN place_category_id BIGINT NULL",
        ):
            try:
                connection.execute(text(column_sql))
            except Exception as exc:
                if "duplicate column" not in str(exc).lower() and "1060" not in str(exc):
                    raise
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS archive_categories (
                category_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL UNIQUE,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                sort_order INT NOT NULL DEFAULT 100,
                created_at BIGINT NOT NULL,
                INDEX idx_archive_categories_order (sort_order, category_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """))
        seeded = connection.execute(text("SELECT setting_value FROM archive_settings WHERE setting_key='archive_categories_seeded'")).scalar_one_or_none()
        if seeded is None:
            defaults = ["좋아하는 것", "싫어하는 것", "두려워하는 것", "꿈과 목표", "가치관", "습관과 특징", "대화와 에피소드", "음식", "장소", "기억", "기타"]
            for order, name in enumerate(defaults):
                connection.execute(text("""
                    INSERT IGNORE INTO archive_categories (name, is_default, sort_order, created_at)
                    VALUES (:name, TRUE, :sort_order, :created_at)
                """), {"name": name, "sort_order": order, "created_at": int(time.time() * 1000)})
            # Preserve category names already used by existing records.
            connection.execute(text("""
                INSERT IGNORE INTO archive_categories (name, is_default, sort_order, created_at)
                SELECT DISTINCT category, FALSE, 100, :created_at FROM records
                WHERE category IS NOT NULL AND TRIM(category) <> ''
            """), {"created_at": int(time.time() * 1000)})
            connection.execute(text("INSERT INTO archive_settings (setting_key, setting_value) VALUES ('archive_categories_seeded', '1')"))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS place_categories (
                place_category_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(50) NOT NULL UNIQUE,
                color CHAR(7) NOT NULL,
                sort_order INT NOT NULL DEFAULT 100,
                created_at BIGINT NOT NULL,
                INDEX idx_place_categories_order (sort_order, place_category_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """))
        seed_place_categories(connection)
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS anniversaries (
                anniversary_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(50) NOT NULL,
                anniversary_date CHAR(10) NOT NULL,
                repeat_yearly BOOLEAN NOT NULL DEFAULT TRUE,
                created_at BIGINT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """))

    try:
        photo_store.head_bucket(Bucket=BUCKET)
    except Exception:
        photo_store.create_bucket(Bucket=BUCKET)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    return response


def require_auth(request: Request) -> None:
    if request.session.get("authenticated") is not True:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")


@app.get("/api/health")
def health():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/version")
def version():
    return {"sha": os.getenv("APP_BUILD_SHA", "unknown")}


@app.get("/api/session")
def session_status(request: Request):
    return {"authenticated": request.session.get("authenticated") is True}


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    # Single-account private service. Use a long, unique password in .env.
    if not secrets.compare_digest(body.password, ARCHIVE_PASSWORD):
        time.sleep(0.8)
        raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.")
    request.session.clear()
    request.session["authenticated"] = True
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/settings")
def get_settings(request: Request):
    require_auth(request)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT setting_key, setting_value FROM archive_settings WHERE setting_key IN ('relationship_start', 'header_phrase')")).all()
    settings = {row[0]: row[1] for row in rows}
    return {"relationshipStart": settings.get('relationship_start', ''), "headerPhrase": settings.get('header_phrase', '')}


@app.put("/api/settings")
def put_settings(body: ArchiveSettingsBody, request: Request):
    require_auth(request)
    value = body.relationshipStart.strip()
    if value:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="연애 시작일은 YYYY-MM-DD 형식이어야 합니다.")
        if parsed.isoformat() != value:
            raise HTTPException(status_code=422, detail="연애 시작일을 확인해 주세요.")
        if parsed > datetime.now().date():
            raise HTTPException(status_code=422, detail="연애 시작일은 오늘 이후일 수 없습니다.")
    phrase = body.headerPhrase.strip()
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO archive_settings (setting_key, setting_value)
            VALUES ('relationship_start', :value)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
        """), {"value": value})
        connection.execute(text("""
            INSERT INTO archive_settings (setting_key, setting_value)
            VALUES ('header_phrase', :phrase)
            ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
        """), {"phrase": phrase})
    return {"relationshipStart": value, "headerPhrase": phrase}


@app.get("/api/categories")
def get_categories(request: Request):
    require_auth(request)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT category_id AS id, name, is_default AS isDefault, sort_order AS sortOrder FROM archive_categories ORDER BY sort_order, name")).mappings().all()
    return {"categories": [dict(row) for row in rows]}


@app.post("/api/categories")
def create_category(body: CategoryBody, request: Request):
    require_auth(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="카테고리 이름을 입력해 주세요.")
    try:
        with engine.begin() as connection:
            next_order = connection.execute(text("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM archive_categories")).scalar_one()
            result = connection.execute(text("INSERT INTO archive_categories (name, is_default, sort_order, created_at) VALUES (:name, FALSE, :sort_order, :created_at)"), {"name": name, "sort_order": next_order, "created_at": int(time.time() * 1000)})
            category_id = result.lastrowid
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "1062" in str(exc):
            raise HTTPException(status_code=409, detail="이미 있는 카테고리 이름이에요.")
        raise
    return {"id": category_id, "name": name, "isDefault": False, "sortOrder": next_order}


@app.put("/api/categories/{category_id}")
def rename_category(category_id: int, body: CategoryBody, request: Request):
    require_auth(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="카테고리 이름을 입력해 주세요.")
    try:
        with engine.begin() as connection:
            old = connection.execute(text("SELECT name FROM archive_categories WHERE category_id=:id"), {"id": category_id}).scalar_one_or_none()
            if old is None:
                raise HTTPException(status_code=404, detail="카테고리를 찾을 수 없어요.")
            if old != name:
                connection.execute(text("UPDATE records SET category=:new_name WHERE category=:old_name"), {"new_name": name, "old_name": old})
                connection.execute(text("UPDATE archive_categories SET name=:name WHERE category_id=:id"), {"name": name, "id": category_id})
    except HTTPException:
        raise
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "1062" in str(exc):
            raise HTTPException(status_code=409, detail="이미 있는 카테고리 이름이에요.")
        raise
    return {"id": category_id, "name": name}


@app.delete("/api/categories/{category_id}")
def delete_category(category_id: int, request: Request):
    require_auth(request)
    with engine.begin() as connection:
        row = connection.execute(text("SELECT name FROM archive_categories WHERE category_id=:id"), {"id": category_id}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="카테고리를 찾을 수 없어요.")
        used = connection.execute(text("SELECT COUNT(*) FROM records WHERE category=:name"), {"name": row["name"]}).scalar_one()
        if used:
            raise HTTPException(status_code=409, detail=f"이 카테고리를 사용하는 기록이 {used}개 있어요. 기록을 다른 카테고리로 옮긴 뒤 삭제해 주세요.")
        connection.execute(text("DELETE FROM archive_categories WHERE category_id=:id"), {"id": category_id})
    return {"ok": True}


PLACE_KINDS = ("visit", "home")


def place_category_fields(body: "PlaceCategoryBody") -> tuple[str, str]:
    name = body.name.strip()
    if not 1 <= len(name) <= 50:
        raise HTTPException(status_code=422, detail="카테고리 이름은 1~50자로 입력해 주세요.")
    # fullmatch: 끝에 줄바꿈이 붙은 값이나 CSS 주입 문자열을 막는다.
    if not PLACE_CATEGORY_COLOR_RE.fullmatch(body.color):
        raise HTTPException(status_code=422, detail="색은 #RRGGBB 형식이어야 해요.")
    return name, body.color


def _duplicate_place_category() -> HTTPException:
    return HTTPException(status_code=409, detail="이미 있는 장소 카테고리 이름이에요.")


@app.get("/api/place-categories")
def get_place_categories(request: Request):
    require_auth(request)
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT place_category_id AS id, name, color, sort_order AS sortOrder FROM place_categories ORDER BY sort_order, place_category_id"
        )).mappings().all()
    return {"categories": [dict(row) for row in rows]}


@app.post("/api/place-categories")
def create_place_category(body: PlaceCategoryBody, request: Request):
    require_auth(request)
    name, color = place_category_fields(body)
    try:
        with engine.begin() as connection:
            if connection.execute(text("SELECT 1 FROM place_categories WHERE name=:name"), {"name": name}).first():
                raise _duplicate_place_category()
            next_order = connection.execute(text("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM place_categories")).scalar_one()
            result = connection.execute(
                text("INSERT INTO place_categories (name, color, sort_order, created_at) VALUES (:name, :color, :sort_order, :created_at)"),
                {"name": name, "color": color, "sort_order": next_order, "created_at": int(time.time() * 1000)},
            )
            category_id = result.lastrowid
    except IntegrityError:
        raise _duplicate_place_category()
    return {"id": category_id, "name": name, "color": color, "sortOrder": next_order}


@app.put("/api/place-categories/{category_id}")
def update_place_category(category_id: int, body: PlaceCategoryBody, request: Request):
    require_auth(request)
    name, color = place_category_fields(body)
    try:
        with engine.begin() as connection:
            if connection.execute(text("SELECT 1 FROM place_categories WHERE place_category_id=:id"), {"id": category_id}).first() is None:
                raise HTTPException(status_code=404, detail="장소 카테고리를 찾을 수 없어요.")
            if connection.execute(text("SELECT 1 FROM place_categories WHERE name=:name AND place_category_id<>:id"), {"name": name, "id": category_id}).first():
                raise _duplicate_place_category()
            connection.execute(
                text("UPDATE place_categories SET name=:name, color=:color WHERE place_category_id=:id"),
                {"name": name, "color": color, "id": category_id},
            )
    except IntegrityError:
        raise _duplicate_place_category()
    return {"id": category_id, "name": name, "color": color}


@app.delete("/api/place-categories/{category_id}")
def delete_place_category(category_id: int, request: Request):
    require_auth(request)
    # 장소 기록은 지우지 않고 미분류(NULL)로 돌린 뒤 카테고리만 삭제한다(한 트랜잭션).
    with engine.begin() as connection:
        if connection.execute(text("SELECT 1 FROM place_categories WHERE place_category_id=:id"), {"id": category_id}).first() is None:
            raise HTTPException(status_code=404, detail="장소 카테고리를 찾을 수 없어요.")
        moved = connection.execute(text("UPDATE records SET place_category_id=NULL WHERE place_category_id=:id"), {"id": category_id}).rowcount
        connection.execute(text("DELETE FROM place_categories WHERE place_category_id=:id"), {"id": category_id})
    return {"ok": True, "uncategorized": moved}


def anniversary_fields(body: "AnniversaryBody") -> tuple[str, str]:
    name = body.name.strip()
    if not 1 <= len(name) <= 50:
        raise HTTPException(status_code=422, detail="기념일 이름은 1~50자로 입력해 주세요.")
    try:
        parsed = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="기념일 날짜는 YYYY-MM-DD 형식이어야 합니다.")
    # strptime은 2026-2-3처럼 자릿수가 모자란 값도 받으므로 다시 만든 문자열과 같은지 확인한다.
    if parsed.isoformat() != body.date:
        raise HTTPException(status_code=422, detail="기념일 날짜를 확인해 주세요.")
    return name, body.date


def anniversary_json(row) -> dict:
    return {"id": row["id"], "name": row["name"], "date": row["date"], "repeatYearly": bool(row["repeatYearly"])}


@app.get("/api/anniversaries")
def get_anniversaries(request: Request):
    require_auth(request)
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT anniversary_id AS id, name, anniversary_date AS date, repeat_yearly AS repeatYearly FROM anniversaries ORDER BY anniversary_date, anniversary_id"
        )).mappings().all()
    return {"anniversaries": [anniversary_json(row) for row in rows]}


@app.post("/api/anniversaries")
def create_anniversary(body: AnniversaryBody, request: Request):
    require_auth(request)
    name, date = anniversary_fields(body)
    with engine.begin() as connection:
        if connection.execute(text("SELECT COUNT(*) FROM anniversaries")).scalar_one() >= ANNIVERSARY_LIMIT:
            raise HTTPException(status_code=409, detail=f"기념일은 최대 {ANNIVERSARY_LIMIT}개까지 등록할 수 있어요.")
        result = connection.execute(
            text("INSERT INTO anniversaries (name, anniversary_date, repeat_yearly, created_at) VALUES (:name, :date, :repeat, :created_at)"),
            {"name": name, "date": date, "repeat": body.repeatYearly, "created_at": int(time.time() * 1000)},
        )
        anniversary_id = result.lastrowid
    return {"id": anniversary_id, "name": name, "date": date, "repeatYearly": body.repeatYearly}


@app.put("/api/anniversaries/{anniversary_id}")
def update_anniversary(anniversary_id: int, body: AnniversaryBody, request: Request):
    require_auth(request)
    name, date = anniversary_fields(body)
    with engine.begin() as connection:
        if connection.execute(text("SELECT 1 FROM anniversaries WHERE anniversary_id=:id"), {"id": anniversary_id}).first() is None:
            raise HTTPException(status_code=404, detail="기념일을 찾을 수 없어요.")
        connection.execute(
            text("UPDATE anniversaries SET name=:name, anniversary_date=:date, repeat_yearly=:repeat WHERE anniversary_id=:id"),
            {"name": name, "date": date, "repeat": body.repeatYearly, "id": anniversary_id},
        )
    return {"id": anniversary_id, "name": name, "date": date, "repeatYearly": body.repeatYearly}


@app.delete("/api/anniversaries/{anniversary_id}")
def delete_anniversary(anniversary_id: int, request: Request):
    require_auth(request)
    with engine.begin() as connection:
        deleted = connection.execute(text("DELETE FROM anniversaries WHERE anniversary_id=:id"), {"id": anniversary_id}).rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="기념일을 찾을 수 없어요.")
    return {"ok": True}


def parse_place_category_id(value: Any) -> int | None:
    """정수 id만 통과. 그 외(빈 값, bool, 문자열, 실수)는 None으로 저장한다."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_coordinate(value: Any, label: str, limit: float) -> float | None:
    """Return a finite coordinate within +/-limit, or None when left empty."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise HTTPException(status_code=422, detail=f"{label} 값이 올바르지 않습니다.")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=422, detail=f"{label} 값이 올바르지 않습니다.")
    if not math.isfinite(number) or abs(number) > limit:
        raise HTTPException(status_code=422, detail=f"{label} 값이 범위를 벗어났습니다.")
    return number


# --- Address search (geocoding) -------------------------------------------
# Only the fixed hosts below are ever contacted; the user's query is just
# URL-encoded data, never a host or path. Addresses are private, so neither the
# query nor the API key is written to the logs or echoed in any response.
GEOCODE_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
GEOCODE_KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
GEOCODE_USER_AGENT = "NFA-private-archive/1.0"  # identifies this app (Nominatim usage policy)
GEOCODE_TIMEOUT = 8
GEOCODE_MAX_BYTES = 1024 * 1024
GEOCODE_MAX_RESULTS = 5
GEOCODE_CACHE_SIZE = 200
GEOCODE_MAX_WAIT = 5.0  # longest we queue for Nominatim's 1 request/second slot

_geocode_cache: "collections.OrderedDict[tuple, list]" = collections.OrderedDict()
_geocode_cache_lock = threading.Lock()
_nominatim_lock = threading.Lock()
_nominatim_next_slot = 0.0
_geocode_clock = time.monotonic
_geocode_sleep = time.sleep


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a redirect becomes an HTTPError instead of being followed


_GEOCODE_OPENER = urllib.request.build_opener(_NoRedirect)


def _geocode_error() -> HTTPException:
    return HTTPException(status_code=502, detail="주소 검색 서비스에 연결하지 못했어요. 잠시 후 다시 시도하거나 좌표를 직접 입력해 주세요.")


def _geocode_get_json(url: str, headers: dict) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _GEOCODE_OPENER.open(request, timeout=GEOCODE_TIMEOUT) as response:
            raw = response.read(GEOCODE_MAX_BYTES + 1)
        if len(raw) > GEOCODE_MAX_BYTES:
            raise ValueError("response too large")
        return json.loads(raw.decode("utf-8"))
    except Exception as error:
        # Log only the exception class: messages can contain the URL (= the address).
        log.warning("geocode upstream request failed: %s", type(error).__name__)
        raise _geocode_error()


def _reserve_nominatim_slot() -> None:
    """Allow at most one Nominatim request per second for the whole process."""
    global _nominatim_next_slot
    with _nominatim_lock:
        now = _geocode_clock()
        slot = max(now, _nominatim_next_slot)
        wait = slot - now
        if wait > GEOCODE_MAX_WAIT:
            raise HTTPException(status_code=429, detail="주소 검색 요청이 많아요. 잠시 후 다시 시도해 주세요.")
        _nominatim_next_slot = slot + 1.0
    if wait > 0:
        _geocode_sleep(wait)


def _geocode_item(label: Any, address: Any, lat: Any, lng: Any) -> dict | None:
    try:
        latitude = parse_coordinate(lat, "위도", 90)
        longitude = parse_coordinate(lng, "경도", 180)
    except HTTPException:
        return None
    if latitude is None or longitude is None:
        return None
    address_text = str(address or "").strip()[:500]
    label_text = str(label or "").strip()[:200] or address_text[:200]
    if not label_text:
        return None
    return {"label": label_text, "address": address_text or label_text, "latitude": latitude, "longitude": longitude}


def _geocode_nominatim(q: str) -> list:
    params = urllib.parse.urlencode({"format": "jsonv2", "limit": GEOCODE_MAX_RESULTS, "accept-language": "ko", "countrycodes": "kr", "q": q})
    _reserve_nominatim_slot()
    data = _geocode_get_json(f"{GEOCODE_NOMINATIM_URL}?{params}", {"User-Agent": GEOCODE_USER_AGENT, "Accept": "application/json"})
    if not isinstance(data, list):
        raise _geocode_error()
    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        display = str(item.get("display_name") or "")
        found = _geocode_item(item.get("name") or display.split(",")[0], display, item.get("lat"), item.get("lon"))
        if found:
            results.append(found)
    return results[:GEOCODE_MAX_RESULTS]


def _geocode_kakao(q: str, api_key: str) -> list:
    headers = {"Authorization": f"KakaoAK {api_key}", "Accept": "application/json"}
    params = urllib.parse.urlencode({"query": q, "size": GEOCODE_MAX_RESULTS})

    def fetch(url: str) -> list:
        data = _geocode_get_json(f"{url}?{params}", headers)
        documents = data.get("documents") if isinstance(data, dict) else None
        if not isinstance(documents, list):
            raise _geocode_error()
        return [doc for doc in documents if isinstance(doc, dict)]

    results = []
    for doc in fetch(GEOCODE_KAKAO_ADDRESS_URL):
        road = doc.get("road_address") if isinstance(doc.get("road_address"), dict) else {}
        address = road.get("address_name") or doc.get("address_name")
        # Kakao: x is longitude, y is latitude.
        found = _geocode_item(doc.get("address_name"), address, doc.get("y"), doc.get("x"))
        if found:
            results.append(found)
    if not results:
        for doc in fetch(GEOCODE_KAKAO_KEYWORD_URL):
            found = _geocode_item(doc.get("place_name"), doc.get("road_address_name") or doc.get("address_name"), doc.get("y"), doc.get("x"))
            if found:
                results.append(found)
    return results[:GEOCODE_MAX_RESULTS]


@app.get("/api/geocode")
def geocode(request: Request, q: str = ""):
    require_auth(request)
    query = q.strip()
    if not 2 <= len(query) <= 200:
        raise HTTPException(status_code=422, detail="검색할 주소를 2자 이상 200자 이하로 입력해 주세요.")
    api_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    provider = "kakao" if api_key else "nominatim"
    cache_key = (provider, query.casefold())
    with _geocode_cache_lock:
        cached = _geocode_cache.get(cache_key)
        if cached is not None:
            _geocode_cache.move_to_end(cache_key)
    if cached is None:
        # Failures raise before this point, so they are never cached; empty results are.
        cached = _geocode_kakao(query, api_key) if api_key else _geocode_nominatim(query)
        with _geocode_cache_lock:
            _geocode_cache[cache_key] = cached
            _geocode_cache.move_to_end(cache_key)
            while len(_geocode_cache) > GEOCODE_CACHE_SIZE:
                _geocode_cache.popitem(last=False)
    return {"results": [dict(item) for item in cached], "provider": provider}


def parse_place_kind(value: Any) -> str:
    # Unknown kinds are rejected instead of silently saved as "visit", so a typo
    # never turns the special home marker into an ordinary place.
    if value is None or value == "":
        return "visit"
    if not isinstance(value, str) or value not in PLACE_KINDS:
        raise HTTPException(status_code=422, detail="장소 유형이 올바르지 않습니다.")
    return value


@app.get("/api/records")
def get_records(request: Request):
    require_auth(request)
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT record_id, record_type, record_date, title, body, category,
                   certainty, tags, place, place_kind, place_category_id, address, latitude, longitude,
                   image, media_type, images, created_at, updated_at
            FROM records
            ORDER BY record_date DESC, updated_at DESC
        """)).mappings().all()

    records = []
    for row in rows:
        item = dict(row)
        item["id"] = item.pop("record_id")
        item["type"] = item.pop("record_type")
        item["date"] = item.pop("record_date")
        item["createdAt"] = item.pop("created_at")
        item["updatedAt"] = item.pop("updated_at")
        item["placeKind"] = item.pop("place_kind") or "visit"
        item["placeCategoryId"] = item.pop("place_category_id")
        item["address"] = item.pop("address") or ""
        item["latitude"] = float(item["latitude"]) if item.get("latitude") is not None else None
        item["longitude"] = float(item["longitude"]) if item.get("longitude") is not None else None
        item["mediaType"] = item.pop("media_type") or ("image" if item.get("image") else None)
        raw_images = item.pop("images", None)
        if isinstance(raw_images, list):
            item["images"] = [
                media for media in raw_images
                if isinstance(media, dict) and isinstance(media.get("url"), str) and media["url"]
            ]
        elif isinstance(raw_images, str):
            try:
                parsed_images = json.loads(raw_images)
            except (TypeError, json.JSONDecodeError):
                parsed_images = []
            item["images"] = [
                media for media in parsed_images
                if isinstance(media, dict) and isinstance(media.get("url"), str) and media["url"]
            ] if isinstance(parsed_images, list) else []
        else:
            item["images"] = []
        if not item["images"] and item.get("image"):
            item["images"] = [{"url": item["image"], "mediaType": item.get("mediaType") or "image"}]
        if item["images"]:
            item["image"] = item["images"][0]["url"]
            item["mediaType"] = item["images"][0].get("mediaType") or "image"
        item["tags"] = item["tags"] if isinstance(item["tags"], list) else json.loads(item["tags"] or "[]")
        records.append(item)
    return {"records": records}


@app.put("/api/records")
def put_records(body: RecordsBody, request: Request):
    require_auth(request)
    if len(body.records) > 10000:
        raise HTTPException(status_code=413, detail="기록이 너무 많습니다.")

    rows = []
    for record in body.records:
        if not isinstance(record.get("id"), (str, int)) or not record.get("title"):
            raise HTTPException(status_code=422, detail="기록의 ID와 제목은 필수입니다.")
        if len(str(record.get("title", ""))) > 500 or len(str(record.get("body", ""))) > 100000:
            raise HTTPException(status_code=422, detail="제목 또는 본문이 너무 깁니다.")
        raw_images = record.get("images")
        if isinstance(raw_images, list):
            media_items = [
                {"url": str(media.get("url", ""))[:1000], "mediaType": str(media.get("mediaType") or "image")[:20]}
                for media in raw_images
                if isinstance(media, dict) and isinstance(media.get("url"), str) and media.get("url")
            ]
        else:
            media_items = []
        if not media_items and record.get("image"):
            media_items = [{
                "url": str(record.get("image") or "")[:1000],
                "mediaType": str(record.get("mediaType") or "image")[:20],
            }]
        primary_media = media_items[0] if media_items else {"url": "", "mediaType": ""}
        rows.append({
            "record_id": str(record["id"]),
            "record_type": str(record.get("type", "diary"))[:30],
            "record_date": record.get("date") or None,
            "title": str(record["title"]),
            "body": str(record.get("body") or ""),
            "category": str(record.get("category") or "")[:100],
            "certainty": str(record.get("certainty") or "")[:100],
            "tags": json.dumps(record.get("tags") if isinstance(record.get("tags"), list) else [], ensure_ascii=False),
            "place": str(record.get("place") or "")[:500],
            "place_kind": parse_place_kind(record.get("placeKind")),
            "place_category_id": parse_place_category_id(record.get("placeCategoryId")),
            "address": str(record.get("address") or "")[:500],
            "latitude": parse_coordinate(record.get("latitude"), "위도", 90),
            "longitude": parse_coordinate(record.get("longitude"), "경도", 180),
            "image": primary_media["url"],
            "media_type": primary_media["mediaType"],
            "images": json.dumps(media_items, ensure_ascii=False),
            "created_at": int(record.get("createdAt") or 0),
            "updated_at": int(record.get("updatedAt") or 0),
        })

    # 우리 집은 하나뿐이다. DELETE 전에 검증해서 실패 시 기존 데이터를 그대로 둔다.
    if sum(1 for row in rows if row["record_type"] == "place" and row["place_kind"] == "home") > 1:
        raise HTTPException(status_code=422, detail="우리 집으로 지정한 장소는 하나만 둘 수 있어요.")

    # This endpoint replaces the current snapshot, matching the existing UI.
    # The transaction means a failed import/save won't leave a half-written list.
    with engine.begin() as connection:
        # 없는 카테고리 id(오래된 탭 등)는 NULL로 저장해 저장 전체가 막히지 않게 한다.
        known_ids = {row[0] for row in connection.execute(text("SELECT place_category_id FROM place_categories"))}
        for row in rows:
            if row["place_category_id"] not in known_ids:
                row["place_category_id"] = None
        connection.execute(text("DELETE FROM records"))
        if rows:
            connection.execute(text("""
                INSERT INTO records
                    (record_id, record_type, record_date, title, body, category,
                     certainty, tags, place, place_kind, place_category_id, address, latitude, longitude, image, media_type, images, created_at, updated_at)
                VALUES
                    (:record_id, :record_type, :record_date, :title, :body, :category,
                     :certainty, CAST(:tags AS JSON), :place, :place_kind, :place_category_id, :address, :latitude, :longitude, :image, :media_type, CAST(:images AS JSON), :created_at, :updated_at)
            """), rows)
    return {"ok": True, "count": len(rows)}


@app.post("/api/photos")
async def upload_photo(request: Request, file: UploadFile = File(...)):
    require_auth(request)
    allowed_types = {
        "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
        "image/gif": "gif", "image/heic": "heic", "image/heif": "heif",
        "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
    }
    content_type = (file.content_type or "").lower()
    extension = allowed_types.get(content_type)
    if not extension:
        raise HTTPException(status_code=415, detail="JPG, PNG, WebP, GIF, HEIC, MP4, MOV, WEBM 형식만 지원합니다.")
    is_video = content_type.startswith("video/")
    max_size = 200 * 1024 * 1024 if is_video else 15 * 1024 * 1024
    data = await file.read(max_size + 1)
    if len(data) > max_size:
        limit_label = "동영상은 200MB 이하로 올려주세요." if is_video else "사진은 15MB 이하로 올려주세요."
        raise HTTPException(status_code=413, detail=limit_label)
    key = f"{uuid.uuid4().hex}.{extension}"
    photo_store.put_object(
        Bucket=BUCKET, Key=key, Body=data, ContentType=content_type,
        Metadata={"uploaded-at": datetime.now(timezone.utc).isoformat()},
    )
    return {"url": f"/api/photos/{key}", "mediaType": "video" if is_video else "image"}

@app.get("/api/photos/{key}")
def get_photo(key: str, request: Request):
    require_auth(request)
    if "/" in key or ".." in key:
        raise HTTPException(status_code=400, detail="잘못된 파일 키입니다.")
    try:
        stored = photo_store.get_object(Bucket=BUCKET, Key=key)
        return StreamingResponse(
            stored["Body"],
            media_type=stored.get("ContentType", "application/octet-stream"),
            headers={"Content-Length": str(stored["ContentLength"]), "Cache-Control": "private, no-store"},
        )
    except Exception:
        raise HTTPException(status_code=404, detail="사진을 찾을 수 없습니다.")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")
