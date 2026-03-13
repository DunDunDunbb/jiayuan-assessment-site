import csv
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import threading
import traceback
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent


def load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


load_local_env()

DB_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = DB_DIR / "submissions.db"
INDEX_PATH = BASE_DIR / "index.html"
ADMIN_PATH = BASE_DIR / "admin.html"
ASSETS_DIR = BASE_DIR / "assets"
LOG_DIR = DB_DIR / "logs"
BACKUP_DIR = DB_DIR / "backups"
ALERTS_LOG_PATH = LOG_DIR / "alerts.log"
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-before-deploy").strip() or "change-me-before-deploy"
SESSION_SECRET = os.environ.get("SESSION_SECRET", "local-dev-session-secret").encode("utf-8")
SESSION_COOKIE_NAME = "jiayuan_admin_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
PORT = int(os.environ.get("PORT", "4173"))
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "").strip()
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "").strip()
APP_ENV = os.environ.get("APP_ENV", "").strip().lower()
IS_PRODUCTION = APP_ENV == "production" or any(
    os.environ.get(key)
    for key in ("RENDER_EXTERNAL_HOSTNAME", "RENDER_SERVICE_ID", "RENDER_INSTANCE_ID")
)
DEFAULT_ADMIN_PASSWORD = "change-me-before-deploy"
DEFAULT_SESSION_SECRET = "local-dev-session-secret".encode("utf-8")
BACKUP_INTERVAL_SECONDS = 60 * 60 * 6
MAX_BACKUP_FILES = 20
DUPLICATE_PHONE_WINDOW_SECONDS = 60 * 60 * 12
ADMIN_PAGE_SIZE = 50
SUSPICIOUS_IP_WINDOW_SECONDS = 60 * 30
SUSPICIOUS_IP_THRESHOLD = 3
SUSPICIOUS_NAME_WINDOW_SECONDS = 60 * 60 * 24
SUSPICIOUS_NAME_PHONE_THRESHOLD = 1
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_ENABLED = bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY)

RATE_LIMITS = {
    "login": {"limit": 5, "window": 60 * 10},
    "assess": {"limit": 15, "window": 60 * 5},
}
rate_limit_store: dict[str, list[float]] = {}
rate_limit_lock = threading.Lock()


PREGNANCY_LABELS = {
    17: "未孕育过，也没有流产史",
    13: "有过一次怀孕，过程相对平稳",
    10: "有过两次及以上怀孕经历",
    4: "有妊娠经历，同时伴随流产史",
}


def ensure_db() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              name TEXT NOT NULL,
              phone TEXT NOT NULL,
              ip_address TEXT NOT NULL DEFAULT '',
              user_agent TEXT NOT NULL DEFAULT '',
              female_age INTEGER NOT NULL,
              female_weight REAL NOT NULL,
              pregnancy_history INTEGER NOT NULL,
              estrogen REAL,
              progesterone REAL,
              hcg REAL,
              male_age INTEGER NOT NULL,
              male_weight REAL NOT NULL,
              score_range TEXT NOT NULL,
              score_level TEXT NOT NULL,
              is_suspicious INTEGER NOT NULL DEFAULT 0,
              suspicion_reason TEXT NOT NULL DEFAULT '',
              payload_json TEXT NOT NULL
            )
            """
        )
        existing_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(submissions)").fetchall()
        }
        for column_name, definition in (
            ("ip_address", "TEXT NOT NULL DEFAULT ''"),
            ("user_agent", "TEXT NOT NULL DEFAULT ''"),
            ("is_suspicious", "INTEGER NOT NULL DEFAULT 0"),
            ("suspicion_reason", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column_name not in existing_columns:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {column_name} {definition}")


def db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return now_utc().isoformat()


def log_event(level: str, event: str, **details) -> None:
    try:
        payload = {
            "timestamp": utc_iso(),
            "level": level,
            "event": event,
            "details": details,
        }
        with (LOG_DIR / "app.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # Logging should never break the request lifecycle.
        return


def log_alert(event: str, **details) -> None:
    try:
        payload = {
            "timestamp": utc_iso(),
            "event": event,
            "details": details,
        }
        with ALERTS_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        return


def latest_backup_age_seconds() -> Optional[float]:
    backups = sorted(BACKUP_DIR.glob("submissions-*.db"))
    if not backups:
        return None
    latest = backups[-1]
    return now_utc().timestamp() - latest.stat().st_mtime


def prune_old_backups() -> None:
    backups = sorted(BACKUP_DIR.glob("submissions-*.db"))
    for stale in backups[:-MAX_BACKUP_FILES]:
        stale.unlink(missing_ok=True)


def maybe_backup_db(reason: str) -> None:
    if not DB_PATH.exists():
        return
    age = latest_backup_age_seconds()
    if age is not None and age < BACKUP_INTERVAL_SECONDS:
        return
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"submissions-{stamp}.db"
    shutil.copy2(DB_PATH, backup_path)
    prune_old_backups()
    log_event("info", "db_backup_created", reason=reason, backup_path=str(backup_path))


def validate_runtime_config() -> None:
    issues = []
    if not ADMIN_USERNAME:
        issues.append("ADMIN_USERNAME 不能为空")
    if IS_PRODUCTION and ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD:
        issues.append("生产环境必须设置安全的 ADMIN_PASSWORD")
    if IS_PRODUCTION and SESSION_SECRET == DEFAULT_SESSION_SECRET:
        issues.append("生产环境必须设置安全的 SESSION_SECRET")
    if bool(TURNSTILE_SITE_KEY) != bool(TURNSTILE_SECRET_KEY):
        issues.append("TURNSTILE_SITE_KEY 和 TURNSTILE_SECRET_KEY 必须同时配置或同时留空")
    if issues:
        message = "；".join(issues)
        raise RuntimeError(message)


def json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: dict,
    headers: Optional[dict[str, str]] = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def validate_phone(phone: str) -> bool:
    return len(phone) == 11 and phone.isdigit() and phone.startswith("1") and phone[1] in "3456789"


def public_config() -> dict:
    return {
        "turnstileEnabled": TURNSTILE_ENABLED,
        "turnstileSiteKey": TURNSTILE_SITE_KEY if TURNSTILE_ENABLED else "",
    }


def get_client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.client_address[0]


def check_rate_limit(handler: BaseHTTPRequestHandler, scope: str) -> tuple[bool, int]:
    config = RATE_LIMITS[scope]
    now_ts = now_utc().timestamp()
    key = f"{scope}:{get_client_ip(handler)}"
    with rate_limit_lock:
        recent = [ts for ts in rate_limit_store.get(key, []) if now_ts - ts < config["window"]]
        if len(recent) >= config["limit"]:
            retry_after = max(1, int(config["window"] - (now_ts - recent[0])))
            rate_limit_store[key] = recent
            return False, retry_after
        recent.append(now_ts)
        rate_limit_store[key] = recent
    return True, 0


def validate_turnstile(token: str, remote_ip: str) -> tuple[bool, str]:
    if not TURNSTILE_ENABLED:
        return True, ""
    token = token.strip()
    if not token:
        return False, "请先完成人机验证。"
    payload = urlencode(
        {
            "secret": TURNSTILE_SECRET_KEY,
            "response": token,
            "remoteip": remote_ip,
        }
    ).encode("utf-8")
    request = Request(
        TURNSTILE_VERIFY_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log_alert("turnstile_verify_failed", error=str(exc), ip=remote_ip)
        return False, "验证服务暂时不可用，请稍后再试。"
    if data.get("success"):
        return True, ""
    error_codes = ",".join(data.get("error-codes", []))
    log_alert("turnstile_rejected", ip=remote_ip, error_codes=error_codes or "unknown")
    return False, "请先完成人机验证。"


def parse_optional_number(value):
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_payload(payload: dict) -> tuple[bool, str]:
    name = str(payload.get("femaleName", "")).strip()
    phone = str(payload.get("femalePhone", "")).strip()
    if not name:
        return False, "请填写姓名。"
    if not validate_phone(phone):
        return False, "请输入有效的 11 位手机号。"

    ranged = [
        ("femaleAge", 20, 55),
        ("femaleWeight", 35, 100),
        ("maleAge", 22, 60),
        ("maleWeight", 45, 150),
    ]
    for field, minimum, maximum in ranged:
        try:
            value = float(payload.get(field))
        except (TypeError, ValueError):
            return False, "表单存在未完成项，请检查后重试。"
        if value < minimum or value > maximum:
            return False, "表单存在未完成项，请检查后重试。"

    try:
        pregnancy_history = int(payload.get("pregnancyHistory"))
    except (TypeError, ValueError):
        return False, "请选择既往怀孕情况。"

    if pregnancy_history not in PREGNANCY_LABELS:
        return False, "请选择既往怀孕情况。"

    return True, ""


def calculate_result(payload: dict) -> dict:
    female_age = int(float(payload["femaleAge"]))
    female_weight = float(payload["femaleWeight"])
    pregnancy_history = int(payload["pregnancyHistory"])
    male_age = int(float(payload["maleAge"]))
    male_weight = float(payload["maleWeight"])
    estrogen = parse_optional_number(payload.get("estrogen"))
    progesterone = parse_optional_number(payload.get("progesterone"))
    hcg = parse_optional_number(payload.get("hcg"))

    score = 24

    if female_age <= 30:
        score += 18
    elif female_age <= 34:
        score += 14
    elif female_age <= 37:
        score += 10
    elif female_age <= 40:
        score += 6
    elif female_age <= 45:
        score += 3
    else:
        score += 1

    if 45 <= female_weight <= 62:
        score += 8
    elif (35 <= female_weight < 45) or (62 < female_weight <= 78):
        score += 5
    else:
        score += 2

    score += pregnancy_history

    if male_age <= 35:
        score += 7
    elif male_age <= 42:
        score += 5
    elif male_age <= 50:
        score += 3
    else:
        score += 1

    if 55 <= male_weight <= 82:
        score += 5
    elif (45 <= male_weight < 55) or (82 < male_weight <= 98):
        score += 3
    else:
        score += 1

    if estrogen is not None:
        score += 3 if 40 <= estrogen <= 60 else -1
    if progesterone is not None:
        score += 3 if 18 <= progesterone <= 35 else -1
    if hcg is not None:
        score += 2 if 5 <= hcg <= 15 else -1

    middle = max(28, min(68, round(score)))
    minimum = max(25, middle - 5)
    maximum = min(72, middle + 5)

    level = "当前属于平稳观察区间"
    advice = "建议把结果作为沟通参考，再结合具体检查结果和医生意见综合判断。"

    if middle >= 58:
        level = "当前属于相对理想区间"
        advice = "整体参考表现较积极，但仍建议按专业节奏推进，不要因为结果偏高而忽略复查。"
    elif middle <= 38:
        level = "当前属于需要重点关注区间"
        advice = "建议优先补充完整检查项，并把目前数据交给医生做更细致的评估。"

    basis = ["双方年龄", "双方体重", "女方既往怀孕情况"]
    if estrogen is not None or progesterone is not None or hcg is not None:
        basis.append("激素/基础指标")

    return {
        "range": f"{minimum}%-{maximum}%",
        "level": level,
        "basis": "、".join(basis),
        "advice": advice,
    }


def submission_metadata_from_request(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return {
        "ip_address": get_client_ip(handler),
        "user_agent": handler.headers.get("User-Agent", "")[:300],
    }


def recent_ip_submission_count(ip_address: str, window_seconds: int = SUSPICIOUS_IP_WINDOW_SECONDS) -> int:
    if not ip_address:
        return 0
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT created_at
            FROM submissions
            WHERE ip_address = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (ip_address,),
        ).fetchall()
    count = 0
    current = now_utc()
    for row in rows:
        try:
            created_at = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            continue
        if (current - created_at).total_seconds() < window_seconds:
            count += 1
    return count


def recent_distinct_phone_count_for_name(name: str, window_seconds: int = SUSPICIOUS_NAME_WINDOW_SECONDS) -> int:
    if not name:
        return 0
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT phone, created_at
            FROM submissions
            WHERE name = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (name,),
        ).fetchall()
    current = now_utc()
    phones: set[str] = set()
    for row in rows:
        try:
            created_at = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            continue
        if (current - created_at).total_seconds() < window_seconds:
            phones.add(str(row["phone"]))
    return len(phones)


def assess_submission_risk(payload: dict, metadata: dict[str, str]) -> tuple[bool, str]:
    reasons: list[str] = []
    ip_address = metadata.get("ip_address", "")
    if recent_ip_submission_count(ip_address) >= SUSPICIOUS_IP_THRESHOLD:
        reasons.append("同一网络短时间提交过多")

    name = str(payload.get("femaleName", "")).strip()
    phone = str(payload.get("femalePhone", "")).strip()
    distinct_phone_count = recent_distinct_phone_count_for_name(name)
    if distinct_phone_count >= SUSPICIOUS_NAME_PHONE_THRESHOLD and not recent_submission_exists(phone):
        reasons.append("同名对应多个手机号")

    return bool(reasons), "；".join(reasons)


def save_submission(payload: dict, result: dict, metadata: dict[str, str], is_suspicious: bool, suspicion_reason: str) -> None:
    created_at = utc_iso()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO submissions (
              created_at, name, phone, ip_address, user_agent, female_age, female_weight, pregnancy_history,
              estrogen, progesterone, hcg, male_age, male_weight, score_range,
              score_level, is_suspicious, suspicion_reason, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                str(payload["femaleName"]).strip(),
                str(payload["femalePhone"]).strip(),
                metadata.get("ip_address", ""),
                metadata.get("user_agent", ""),
                int(float(payload["femaleAge"])),
                float(payload["femaleWeight"]),
                int(payload["pregnancyHistory"]),
                parse_optional_number(payload.get("estrogen")),
                parse_optional_number(payload.get("progesterone")),
                parse_optional_number(payload.get("hcg")),
                int(float(payload["maleAge"])),
                float(payload["maleWeight"]),
                result["range"],
                result["level"],
                1 if is_suspicious else 0,
                suspicion_reason,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
    maybe_backup_db("submission_saved")


def count_all_submissions(suspicious_only: bool = False) -> int:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM submissions WHERE is_suspicious = ?",
            (1 if suspicious_only else 0,),
        ).fetchone()
    return int(row["count"]) if row else 0


def count_today_submissions(suspicious_only: bool = False) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM submissions WHERE substr(created_at, 1, 10) = ? AND is_suspicious = ?",
            (today, 1 if suspicious_only else 0),
        ).fetchone()
    return int(row["count"]) if row else 0


def list_submissions(page: int = 1, page_size: int = ADMIN_PAGE_SIZE, suspicious_only: bool = False) -> list[dict]:
    page = max(1, page)
    page_size = max(1, min(page_size, ADMIN_PAGE_SIZE))
    offset = (page - 1) * page_size
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, name, phone, female_age, female_weight, pregnancy_history,
                   male_age, male_weight, score_range, score_level, is_suspicious, suspicion_reason
            FROM submissions
            WHERE is_suspicious = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """
        , (1 if suspicious_only else 0, page_size, offset)).fetchall()
    return [
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "name": row["name"],
            "phone": row["phone"],
            "female_age": row["female_age"],
            "female_weight": row["female_weight"],
            "pregnancy_history_label": PREGNANCY_LABELS.get(row["pregnancy_history"], "未知"),
            "male_age": row["male_age"],
            "male_weight": row["male_weight"],
            "score_range": row["score_range"],
            "score_level": row["score_level"],
            "is_suspicious": bool(row["is_suspicious"]),
            "suspicion_reason": row["suspicion_reason"] or "",
        }
        for row in rows
    ]


def delete_submission(submission_id: int) -> bool:
    with db_connection() as conn:
        cursor = conn.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))
        deleted = cursor.rowcount > 0
    if deleted:
        maybe_backup_db("submission_deleted")
    return deleted


def recent_submission_exists(phone: str, window_seconds: int = DUPLICATE_PHONE_WINDOW_SECONDS) -> bool:
    with db_connection() as conn:
        row = conn.execute(
            """
            SELECT created_at
            FROM submissions
            WHERE phone = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (phone,),
        ).fetchone()
    if not row:
        return False
    try:
        created_at = datetime.fromisoformat(row["created_at"])
    except (TypeError, ValueError):
        return False
    return (now_utc() - created_at).total_seconds() < window_seconds


def export_csv(handler: BaseHTTPRequestHandler, suspicious_only: bool = False) -> None:
    rows = list_submissions(suspicious_only=suspicious_only, page_size=5000)
    from io import StringIO

    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["提交时间", "姓名", "联系电话", "结果区间", "女方年龄", "女方体重", "既往怀孕情况", "男方年龄", "男方体重", "备注"])
    for row in rows:
        writer.writerow(
            [
                row["created_at"],
                row["name"],
                row["phone"],
                row["score_range"],
                row["female_age"],
                row["female_weight"],
                row["pregnancy_history_label"],
                row["male_age"],
                row["male_weight"],
                row["suspicion_reason"],
            ]
        )

    body = buffer.getvalue().encode("utf-8-sig")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    filename = "beijing-jiayuan-suspicious-leads.csv" if suspicious_only else "beijing-jiayuan-leads.csv"
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def sign_session(username: str, expires_at: int) -> str:
    payload = f"{username}:{expires_at}".encode("utf-8")
    return hmac.new(SESSION_SECRET, payload, hashlib.sha256).hexdigest()


def sign_csrf(username: str, expires_at: int) -> str:
    payload = f"csrf:{username}:{expires_at}".encode("utf-8")
    return hmac.new(SESSION_SECRET, payload, hashlib.sha256).hexdigest()


def build_session_cookie(handler: BaseHTTPRequestHandler, username: str, clear: bool = False) -> str:
    parts = [
        f"{SESSION_COOKIE_NAME}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if clear:
        parts.extend(["Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"])
    else:
        expires_at = int(datetime.now(timezone.utc).timestamp()) + SESSION_TTL_SECONDS
        signature = sign_session(username, expires_at)
        token = f"{username}:{expires_at}:{signature}"
        parts[0] = f"{SESSION_COOKIE_NAME}={token}"
        parts.append(f"Max-Age={SESSION_TTL_SECONDS}")
    if handler.headers.get("X-Forwarded-Proto", "").lower() == "https":
        parts.append("Secure")
    return "; ".join(parts)


def build_session_artifacts(handler: BaseHTTPRequestHandler, username: str) -> tuple[str, str]:
    expires_at = int(datetime.now(timezone.utc).timestamp()) + SESSION_TTL_SECONDS
    signature = sign_session(username, expires_at)
    csrf_token = sign_csrf(username, expires_at)
    token = f"{username}:{expires_at}:{signature}"
    parts = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={SESSION_TTL_SECONDS}",
    ]
    if handler.headers.get("X-Forwarded-Proto", "").lower() == "https":
        parts.append("Secure")
    return "; ".join(parts), csrf_token


def get_cookie_value(handler: BaseHTTPRequestHandler, name: str) -> str:
    raw_cookie = handler.headers.get("Cookie", "")
    if not raw_cookie:
        return ""
    cookie = SimpleCookie()
    cookie.load(raw_cookie)
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


def parse_session_token(handler: BaseHTTPRequestHandler) -> Optional[tuple[str, int, str]]:
    token = get_cookie_value(handler, SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        username, expires_raw, signature = token.split(":", 2)
        expires_at = int(expires_raw)
    except ValueError:
        return None
    return username, expires_at, signature


def current_csrf_token(handler: BaseHTTPRequestHandler) -> str:
    parsed = parse_session_token(handler)
    if not parsed:
        return ""
    username, expires_at, signature = parsed
    if not hmac.compare_digest(signature, sign_session(username, expires_at)):
        return ""
    return sign_csrf(username, expires_at)


def has_valid_session(handler: BaseHTTPRequestHandler) -> bool:
    parsed = parse_session_token(handler)
    if not parsed:
        return False
    username, expires_at, signature = parsed
    if username != ADMIN_USERNAME:
        return False
    if expires_at < int(datetime.now(timezone.utc).timestamp()):
        return False
    expected = sign_session(username, expires_at)
    return hmac.compare_digest(signature, expected)


def require_admin_auth(handler: BaseHTTPRequestHandler) -> bool:
    if has_valid_session(handler):
        return True
    json_response(
        handler,
        HTTPStatus.UNAUTHORIZED,
        {"error": "请先登录后台。"},
        headers={"Cache-Control": "no-store"},
    )
    return False


def same_origin_request(handler: BaseHTTPRequestHandler) -> bool:
    proto = handler.headers.get("X-Forwarded-Proto", "") or "http"
    host = handler.headers.get("Host", "")
    target_origin = f"{proto}://{host}" if host else ""
    for header_name in ("Origin", "Referer"):
        value = handler.headers.get(header_name, "")
        if not value:
            continue
        return value.startswith(target_origin)
    return True


def require_csrf(handler: BaseHTTPRequestHandler) -> bool:
    if not same_origin_request(handler):
        log_alert("csrf_origin_blocked", ip=get_client_ip(handler), path=handler.path)
        json_response(
            handler,
            HTTPStatus.FORBIDDEN,
            {"error": "请求来源无效。"},
            headers={"Cache-Control": "no-store"},
        )
        return False
    expected = current_csrf_token(handler)
    provided = handler.headers.get("X-CSRF-Token", "").strip()
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        log_alert("csrf_token_blocked", ip=get_client_ip(handler), path=handler.path)
        json_response(
            handler,
            HTTPStatus.FORBIDDEN,
            {"error": "安全校验失败，请刷新后台后重试。"},
            headers={"Cache-Control": "no-store"},
        )
        return False
    return True


def send_file(
    handler: BaseHTTPRequestHandler,
    file_path: Path,
    content_type: str,
    headers: Optional[dict[str, str]] = None,
    include_body: bool = True,
) -> None:
    if not file_path.exists() or not file_path.is_file():
        handler.send_error(HTTPStatus.NOT_FOUND, "File not found")
        return
    body = file_path.read_bytes()
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    if include_body:
        handler.wfile.write(body)


class AppHandler(BaseHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        super().end_headers()

    def _respond_rate_limited(self, retry_after: int) -> None:
        json_response(
            self,
            HTTPStatus.TOO_MANY_REQUESTS,
            {"error": "操作太频繁了，请稍后再试。"},
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )

    def _run_safely(self, method_name: str, handler_fn) -> None:
        try:
            handler_fn()
        except BrokenPipeError:
            log_event("warning", "client_disconnected", method=method_name, path=self.path)
        except Exception as exc:
            log_event(
                "error",
                "unhandled_exception",
                method=method_name,
                path=self.path,
                error=str(exc),
                traceback=traceback.format_exc()[-4000:],
            )
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Server error")

    def do_GET(self) -> None:
        self._run_safely("GET", self._handle_get)

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            return send_file(self, INDEX_PATH, "text/html; charset=utf-8")
        if parsed.path in ("/admin", "/admin.html"):
            return send_file(
                self,
                ADMIN_PATH,
                "text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/api/admin/session":
            authenticated = has_valid_session(self)
            return json_response(
                self,
                HTTPStatus.OK,
                {
                    "authenticated": authenticated,
                    "username": ADMIN_USERNAME if authenticated else "",
                    "csrfToken": current_csrf_token(self) if authenticated else "",
                },
                headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/api/public-config":
            return json_response(
                self,
                HTTPStatus.OK,
                public_config(),
                headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/api/submissions":
            if not require_admin_auth(self):
                return
            page = 1
            page_size = ADMIN_PAGE_SIZE
            suspicious_only = (query.get("suspicious") or ["0"])[0] == "1"
            try:
                page = int((query.get("page") or ["1"])[0])
            except (ValueError, IndexError):
                page = 1
            total = count_all_submissions(suspicious_only=suspicious_only)
            return json_response(
                self,
                HTTPStatus.OK,
                {
                    "items": list_submissions(page=page, page_size=page_size, suspicious_only=suspicious_only),
                    "page": max(1, page),
                    "pageSize": page_size,
                    "total": total,
                    "todayCount": count_today_submissions(suspicious_only=suspicious_only),
                    "hasNext": total > max(1, page) * page_size,
                    "suspicious": suspicious_only,
                    "normalCount": count_all_submissions(suspicious_only=False),
                    "suspiciousCount": count_all_submissions(suspicious_only=True),
                },
                headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/api/export.csv":
            if not require_admin_auth(self):
                return
            if not require_csrf(self):
                return
            suspicious_only = (query.get("suspicious") or ["0"])[0] == "1"
            return export_csv(self, suspicious_only=suspicious_only)
        if parsed.path.startswith("/assets/"):
            asset_path = (ASSETS_DIR / parsed.path.removeprefix("/assets/")).resolve()
            if ASSETS_DIR not in asset_path.parents and asset_path != ASSETS_DIR:
                return self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            content_type = "application/octet-stream"
            if asset_path.suffix.lower() in {".svg"}:
                content_type = "image/svg+xml"
            elif asset_path.suffix.lower() in {".jpg", ".jpeg"}:
                content_type = "image/jpeg"
            elif asset_path.suffix.lower() == ".png":
                content_type = "image/png"
            return send_file(self, asset_path, content_type)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_HEAD(self) -> None:
        self._run_safely("HEAD", self._handle_head)

    def _handle_head(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            return send_file(self, INDEX_PATH, "text/html; charset=utf-8", include_body=False)
        if parsed.path in ("/admin", "/admin.html"):
            return send_file(
                self,
                ADMIN_PATH,
                "text/html; charset=utf-8",
                headers={"Cache-Control": "no-store"},
                include_body=False,
            )

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        self._run_safely("POST", self._handle_post)

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/admin/login":
            allowed, retry_after = check_rate_limit(self, "login")
            if not allowed:
                log_event("warning", "login_rate_limited", ip=get_client_ip(self))
                log_alert("login_rate_limited", ip=get_client_ip(self))
                return self._respond_rate_limited(retry_after)
            payload = read_json(self)
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", "")).strip()
            if not secrets.compare_digest(username, ADMIN_USERNAME) or not secrets.compare_digest(password, ADMIN_PASSWORD):
                log_event("warning", "login_failed", ip=get_client_ip(self), username=username[:64])
                log_alert("login_failed", ip=get_client_ip(self), username=username[:64])
                return json_response(
                    self,
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "账号或密码不正确。"},
                    headers={"Cache-Control": "no-store"},
                )
            session_cookie, csrf_token = build_session_artifacts(self, ADMIN_USERNAME)
            return json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "username": ADMIN_USERNAME,
                    "csrfToken": csrf_token,
                },
                headers={
                    "Set-Cookie": session_cookie,
                    "Cache-Control": "no-store",
                },
            )

        if parsed.path == "/api/admin/logout":
            if not require_admin_auth(self):
                return
            if not require_csrf(self):
                return
            return json_response(
                self,
                HTTPStatus.OK,
                {"ok": True},
                headers={
                    "Set-Cookie": build_session_cookie(self, "", clear=True),
                    "Cache-Control": "no-store",
                },
            )

        if parsed.path != "/api/assess":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        allowed, retry_after = check_rate_limit(self, "assess")
        if not allowed:
            log_event("warning", "assessment_rate_limited", ip=get_client_ip(self))
            return self._respond_rate_limited(retry_after)

        payload = read_json(self)
        ok, message = validate_payload(payload)
        if not ok:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": message})
        turnstile_ok, turnstile_message = validate_turnstile(
            str(payload.get("turnstileToken", "")),
            get_client_ip(self),
        )
        if not turnstile_ok:
            return json_response(
                self,
                HTTPStatus.FORBIDDEN,
                {"error": turnstile_message},
                headers={"Cache-Control": "no-store"},
            )

        phone = str(payload.get("femalePhone", "")).strip()
        if recent_submission_exists(phone):
            log_event("info", "duplicate_submission_blocked", ip=get_client_ip(self))
            return json_response(
                self,
                HTTPStatus.CONFLICT,
                {"error": "这个手机号近期已经提交过了，请不要重复提交，专业医生会尽快联系你。"},
                headers={"Cache-Control": "no-store"},
            )

        result = calculate_result(payload)
        metadata = submission_metadata_from_request(self)
        is_suspicious, suspicion_reason = assess_submission_risk(payload, metadata)
        if is_suspicious:
            log_alert("suspicious_submission_isolated", ip=metadata.get("ip_address", ""), reason=suspicion_reason)
        save_submission(payload, result, metadata, is_suspicious, suspicion_reason)
        log_event("info", "assessment_saved", ip=get_client_ip(self))
        json_response(self, HTTPStatus.OK, {"result": result})

    def do_DELETE(self) -> None:
        self._run_safely("DELETE", self._handle_delete)

    def _handle_delete(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/submissions/"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        if not require_admin_auth(self):
            return
        if not require_csrf(self):
            return

        tail = parsed.path.removeprefix("/api/submissions/").strip("/")
        try:
            submission_id = int(tail)
        except ValueError:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "无效的记录编号。"})

        deleted = delete_submission(submission_id)
        if not deleted:
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "这条记录不存在或已经删除。"})

        log_event("info", "submission_deleted", ip=get_client_ip(self), submission_id=submission_id)
        json_response(self, HTTPStatus.OK, {"ok": True})

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    ensure_db()
    validate_runtime_config()
    maybe_backup_db("startup")
    log_event("info", "server_started", port=PORT, app_env=APP_ENV or "development", production=IS_PRODUCTION)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AppHandler)
    print(f"Server running on http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
