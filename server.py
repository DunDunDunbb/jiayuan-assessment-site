import csv
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse


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
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-before-deploy").strip() or "change-me-before-deploy"
SESSION_SECRET = os.environ.get("SESSION_SECRET", "local-dev-session-secret").encode("utf-8")
SESSION_COOKIE_NAME = "jiayuan_admin_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
PORT = int(os.environ.get("PORT", "4173"))


PREGNANCY_LABELS = {
    17: "未孕育过，也没有流产史",
    13: "有过一次怀孕，过程相对平稳",
    10: "有过两次及以上怀孕经历",
    4: "有妊娠经历，同时伴随流产史",
}


def ensure_db() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              name TEXT NOT NULL,
              phone TEXT NOT NULL,
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
              payload_json TEXT NOT NULL
            )
            """
        )


def db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


def save_submission(payload: dict, result: dict) -> None:
    created_at = datetime.now(timezone.utc).isoformat()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO submissions (
              created_at, name, phone, female_age, female_weight, pregnancy_history,
              estrogen, progesterone, hcg, male_age, male_weight, score_range,
              score_level, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                str(payload["femaleName"]).strip(),
                str(payload["femalePhone"]).strip(),
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
                json.dumps(payload, ensure_ascii=False),
            ),
        )


def list_submissions() -> list[dict]:
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, name, phone, female_age, female_weight, pregnancy_history,
                   male_age, male_weight, score_range, score_level
            FROM submissions
            ORDER BY id DESC
            """
        ).fetchall()
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
        }
        for row in rows
    ]


def delete_submission(submission_id: int) -> bool:
    with db_connection() as conn:
        cursor = conn.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))
        return cursor.rowcount > 0


def export_csv(handler: BaseHTTPRequestHandler) -> None:
    rows = list_submissions()
    from io import StringIO

    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["提交时间", "姓名", "联系电话", "结果区间", "女方年龄", "女方体重", "既往怀孕情况", "男方年龄", "男方体重"])
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
            ]
        )

    body = buffer.getvalue().encode("utf-8-sig")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition", 'attachment; filename="beijing-jiayuan-leads.csv"')
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def sign_session(username: str, expires_at: int) -> str:
    payload = f"{username}:{expires_at}".encode("utf-8")
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


def get_cookie_value(handler: BaseHTTPRequestHandler, name: str) -> str:
    raw_cookie = handler.headers.get("Cookie", "")
    if not raw_cookie:
        return ""
    cookie = SimpleCookie()
    cookie.load(raw_cookie)
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


def has_valid_session(handler: BaseHTTPRequestHandler) -> bool:
    token = get_cookie_value(handler, SESSION_COOKIE_NAME)
    if not token:
        return False
    try:
        username, expires_raw, signature = token.split(":", 2)
        expires_at = int(expires_raw)
    except ValueError:
        return False
    if username != ADMIN_USERNAME:
        return False
    if expires_at < int(datetime.now(timezone.utc).timestamp()):
        return False
    expected = sign_session(username, expires_at)
    return hmac.compare_digest(signature, expected)


def has_valid_admin_token(query: dict) -> bool:
    if not ADMIN_TOKEN:
        return False
    provided = ""
    if "token" in query and query["token"]:
        provided = query["token"][0]
    return secrets.compare_digest(provided, ADMIN_TOKEN)


def require_admin_auth(handler: BaseHTTPRequestHandler, query: Optional[dict] = None) -> bool:
    query = query or {}
    if has_valid_session(handler) or has_valid_admin_token(query):
        return True
    json_response(
        handler,
        HTTPStatus.UNAUTHORIZED,
        {"error": "请先登录后台。"},
        headers={"Cache-Control": "no-store"},
    )
    return False


def send_file(
    handler: BaseHTTPRequestHandler,
    file_path: Path,
    content_type: str,
    headers: Optional[dict[str, str]] = None,
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
    handler.wfile.write(body)


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
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
            authenticated = has_valid_session(self) or has_valid_admin_token(query)
            return json_response(
                self,
                HTTPStatus.OK,
                {
                    "authenticated": authenticated,
                    "username": ADMIN_USERNAME if authenticated else "",
                },
                headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/api/submissions":
            if not require_admin_auth(self, query):
                return
            return json_response(
                self,
                HTTPStatus.OK,
                {"items": list_submissions()},
                headers={"Cache-Control": "no-store"},
            )
        if parsed.path == "/api/export.csv":
            if not require_admin_auth(self, query):
                return
            return export_csv(self)
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

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/admin/login":
            payload = read_json(self)
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", "")).strip()
            if not secrets.compare_digest(username, ADMIN_USERNAME) or not secrets.compare_digest(password, ADMIN_PASSWORD):
                return json_response(
                    self,
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "账号或密码不正确。"},
                    headers={"Cache-Control": "no-store"},
                )
            return json_response(
                self,
                HTTPStatus.OK,
                {"ok": True, "username": ADMIN_USERNAME},
                headers={
                    "Set-Cookie": build_session_cookie(self, ADMIN_USERNAME),
                    "Cache-Control": "no-store",
                },
            )

        if parsed.path == "/api/admin/logout":
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

        payload = read_json(self)
        ok, message = validate_payload(payload)
        if not ok:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": message})

        result = calculate_result(payload)
        save_submission(payload, result)
        json_response(self, HTTPStatus.OK, {"result": result})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/submissions/"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        query = parse_qs(parsed.query)
        if not require_admin_auth(self, query):
            return

        tail = parsed.path.removeprefix("/api/submissions/").strip("/")
        try:
            submission_id = int(tail)
        except ValueError:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"error": "无效的记录编号。"})

        deleted = delete_submission(submission_id)
        if not deleted:
            return json_response(self, HTTPStatus.NOT_FOUND, {"error": "这条记录不存在或已经删除。"})

        json_response(self, HTTPStatus.OK, {"ok": True})

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    ensure_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AppHandler)
    print(f"Server running on http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
