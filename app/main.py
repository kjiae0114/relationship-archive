"""Private relationship archive API.

Small, deliberately readable FastAPI application. All user data is private to
one account; records are stored as a JSON document in MySQL and image bytes in
an S3-compatible MinIO bucket. See README.md before exposing it to the internet.
"""
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.client import Config
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from starlette.middleware.sessions import SessionMiddleware

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("relationship_archive")

DATABASE_URL = os.environ["DATABASE_URL"]
ARCHIVE_PASSWORD = os.environ["ARCHIVE_PASSWORD"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
APP_BUILD_SHA = os.getenv("APP_BUILD_SHA", "unknown")
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
    return {"sha": APP_BUILD_SHA}


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


@app.get("/api/records")
def get_records(request: Request):
    require_auth(request)
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT record_id, record_type, record_date, title, body, category,
                   certainty, tags, place, image, media_type, images, created_at, updated_at
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
            "image": primary_media["url"],
            "media_type": primary_media["mediaType"],
            "images": json.dumps(media_items, ensure_ascii=False),
            "created_at": int(record.get("createdAt") or 0),
            "updated_at": int(record.get("updatedAt") or 0),
        })

    # This endpoint replaces the current snapshot, matching the existing UI.
    # The transaction means a failed import/save won't leave a half-written list.
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM records"))
        if rows:
            connection.execute(text("""
                INSERT INTO records
                    (record_id, record_type, record_date, title, body, category,
                     certainty, tags, place, image, media_type, images, created_at, updated_at)
                VALUES
                    (:record_id, :record_type, :record_date, :title, :body, :category,
                     :certainty, CAST(:tags AS JSON), :place, :image, :media_type, CAST(:images AS JSON), :created_at, :updated_at)
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
