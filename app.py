import os
import sys
import json
import sqlite3
import hashlib
import secrets
import time
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, g, session, redirect, url_for, render_template_string, send_from_directory
from werkzeug.utils import secure_filename

DB_PATH = os.environ.get("MAFIN_DB_PATH", "mafin_launcher.db")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_secret_key():
    env_key = os.environ.get("MAFIN_SECRET_KEY")
    if env_key:
        return env_key
    path = os.path.join(BASE_DIR, ".secret_key")
    try:
        with open(path, "r") as fh:
            key = fh.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key

SECRET_KEY = _load_secret_key()

ADMIN_SESSION_DAYS = 30
ADMIN_CODE_TTL_SECONDS = 300
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

UPDATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "updates")
os.makedirs(UPDATES_DIR, exist_ok=True)
ALLOWED_UPDATE_EXT = {".exe", ".zip"}
MAX_UPDATE_SIZE_BYTES = 500 * 1024 * 1024

ALLOWED_EXTRA_FILE_EXT = {".dll", ".zip"}

GIFTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gift_images")
os.makedirs(GIFTS_DIR, exist_ok=True)
ALLOWED_GIFT_IMAGE_EXT = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_GIFT_IMAGE_SIZE_BYTES = 15 * 1024 * 1024

try:
    from zoneinfo import ZoneInfo
    YEKB_TZ = ZoneInfo("Asia/Yekaterinburg")
except Exception:
    YEKB_TZ = None

ONLINE_THRESHOLD_SECONDS = 90

TYPING_THRESHOLD_SECONDS = 6

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 60
_login_attempts_lock = threading.Lock()
_login_attempts = {}

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=ADMIN_SESSION_DAYS)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("MAFIN_HTTPS") == "1"
app.config['MAX_CONTENT_LENGTH'] = MAX_UPDATE_SIZE_BYTES + 1024 * 1024

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            token TEXT UNIQUE,
            is_admin INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT,
            total_playtime_minutes REAL DEFAULT 0,
            coins REAL DEFAULT 0,
            total_launches INTEGER DEFAULT 0,
            versions_played TEXT DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_login TEXT,
            last_seen TEXT
        );
        
        CREATE TABLE IF NOT EXISTS play_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            version TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration_minutes REAL DEFAULT 0,
            coins_earned REAL DEFAULT 0
        );
        
        CREATE TABLE IF NOT EXISTS server_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            nickname TEXT,
            details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL,
            filename TEXT NOT NULL,
            changelog TEXT,
            is_active INTEGER DEFAULT 0,
            uploaded_by TEXT,
            file_size INTEGER DEFAULT 0,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            achievement_id TEXT NOT NULL,
            unlocked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(nickname, achievement_id)
        );

        CREATE TABLE IF NOT EXISTS achievement_counters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            counter_name TEXT NOT NULL,
            value REAL NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(nickname, counter_name)
        );

        CREATE TABLE IF NOT EXISTS friendships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester TEXT NOT NULL,
            addressee TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(requester, addressee)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            text TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            author TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS login_days (
            nickname TEXT NOT NULL,
            day TEXT NOT NULL,
            UNIQUE(nickname, day)
        );

        CREATE TABLE IF NOT EXISTS quests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            target_value REAL NOT NULL,
            extra_param TEXT,
            reward_coins REAL NOT NULL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS quest_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quest_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            claimed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(quest_id, nickname)
        );

        CREATE TABLE IF NOT EXISTS gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            image_filename TEXT NOT NULL,
            quantity INTEGER DEFAULT -1,
            sale_until TEXT,
            is_active INTEGER DEFAULT 1,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS admin_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS gift_sends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gift_id INTEGER NOT NULL,
            gift_name TEXT NOT NULL,
            gift_image_filename TEXT NOT NULL,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            note TEXT,
            price_paid REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    existing_cols = [row[1] for row in db.execute("PRAGMA table_info(profiles)").fetchall()]
    if "last_seen" not in existing_cols:
        db.execute("ALTER TABLE profiles ADD COLUMN last_seen TEXT")
        db.commit()
    if "bio" not in existing_cols:
        db.execute("ALTER TABLE profiles ADD COLUMN bio TEXT DEFAULT ''")
        db.commit()
    if "gifts_visible" not in existing_cols:
        db.execute("ALTER TABLE profiles ADD COLUMN gifts_visible INTEGER DEFAULT 1")
        db.commit()

    existing_gift_cols = [row[1] for row in db.execute("PRAGMA table_info(gifts)").fetchall()]
    gift_col_defs = {
        "name": "TEXT NOT NULL DEFAULT ''",
        "price": "REAL NOT NULL DEFAULT 0",
        "image_filename": "TEXT NOT NULL DEFAULT ''",
        "quantity": "INTEGER DEFAULT -1",
        "sale_until": "TEXT",
        "is_active": "INTEGER DEFAULT 1",
        "created_by": "TEXT",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    }
    for col, col_def in gift_col_defs.items():
        if col not in existing_gift_cols:
            db.execute(f"ALTER TABLE gifts ADD COLUMN {col} {col_def}")
            db.commit()

    existing_update_cols = [row[1] for row in db.execute("PRAGMA table_info(updates)").fetchall()]
    if "extra_filename" not in existing_update_cols:
        db.execute("ALTER TABLE updates ADD COLUMN extra_filename TEXT")
        db.commit()
    if "extra_file_size" not in existing_update_cols:
        db.execute("ALTER TABLE updates ADD COLUMN extra_file_size INTEGER DEFAULT 0")
        db.commit()

    existing_msg_cols = [row[1] for row in db.execute("PRAGMA table_info(messages)").fetchall()]
    if "message_type" not in existing_msg_cols:
        db.execute("ALTER TABLE messages ADD COLUMN message_type TEXT DEFAULT 'text'")
        db.commit()
    if "gift_send_id" not in existing_msg_cols:
        db.execute("ALTER TABLE messages ADD COLUMN gift_send_id INTEGER")
        db.commit()

    existing_news_cols = [row[1] for row in db.execute("PRAGMA table_info(news)").fetchall()]
    if "edited" not in existing_news_cols:
        db.execute("ALTER TABLE news ADD COLUMN edited INTEGER DEFAULT 0")
        db.commit()
    if "edited_at" not in existing_news_cols:
        db.execute("ALTER TABLE news ADD COLUMN edited_at TEXT")
        db.commit()

    has_admin = db.execute("SELECT 1 FROM profiles WHERE is_admin = 1 LIMIT 1").fetchone()
    if not has_admin:
        print("⚠️  В базе нет ни одного админа. Создай: python app.py admin-code НИК --create")

    db.close()
    print(f"✅ База данных инициализирована: {DB_PATH}")

PBKDF2_ITERATIONS = 100_000

def hash_password(password, salt):
    raw = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${raw.hex()}"

def _legacy_hash_password(password, salt):
    return hashlib.sha256((password + salt).encode()).hexdigest()

def verify_password(password, salt, stored_hash):
    if stored_hash.startswith("pbkdf2$"):
        try:
            _, iterations_str, hex_hash = stored_hash.split("$", 2)
            iterations = int(iterations_str)
        except (ValueError, IndexError):
            return False, False
        raw = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
        return secrets.compare_digest(raw.hex(), hex_hash), False
    matches = secrets.compare_digest(_legacy_hash_password(password, salt), stored_hash)
    return matches, matches

def generate_token():
    return secrets.token_hex(32)

def yekb_input_to_system_naive(dt_str):
    if not dt_str:
        return None
    naive = datetime.fromisoformat(dt_str)
    if YEKB_TZ is not None:
        aware_yekb = naive.replace(tzinfo=YEKB_TZ)
    else:
        aware_yekb = naive.replace(tzinfo=timezone(timedelta(hours=5)))
    aware_utc = aware_yekb.astimezone(timezone.utc)
    system_tzinfo = datetime.now().astimezone().tzinfo
    aware_system = aware_utc.astimezone(system_tzinfo)
    return aware_system.replace(tzinfo=None)

def system_naive_to_yekb_input(dt_str):
    if not dt_str:
        return ""
    try:
        naive = datetime.fromisoformat(dt_str)
    except Exception:
        return ""
    system_tzinfo = datetime.now().astimezone().tzinfo
    aware_system = naive.replace(tzinfo=system_tzinfo)
    aware_utc = aware_system.astimezone(timezone.utc)
    if YEKB_TZ is not None:
        aware_yekb = aware_utc.astimezone(YEKB_TZ)
    else:
        aware_yekb = aware_utc.astimezone(timezone(timedelta(hours=5)))
    return aware_yekb.strftime("%Y-%m-%dT%H:%M")

def gift_is_on_sale(gift_row):
    if not gift_row["is_active"]:
        return False
    qty = gift_row["quantity"]
    if qty is not None and qty >= 0 and qty <= 0:
        return False
    sale_until = gift_row["sale_until"]
    if sale_until:
        try:
            if datetime.now() > datetime.fromisoformat(sale_until):
                return False
        except Exception:
            pass
    return True

def touch_last_seen(db, nickname):
    try:
        db.execute(
            "UPDATE profiles SET last_seen = ? WHERE nickname = ?",
            (datetime.now().isoformat(), nickname)
        )
        db.commit()
    except Exception as e:
        print(f"Ошибка обновления last_seen: {e}")

def is_online(last_seen_value):
    if not last_seen_value:
        return False
    try:
        last_seen_dt = datetime.fromisoformat(last_seen_value)
    except (ValueError, TypeError):
        return False
    return (datetime.now() - last_seen_dt).total_seconds() < ONLINE_THRESHOLD_SECONDS

_login_day_cache = {}
_login_day_cache_lock = threading.Lock()

def record_login_day(db, nickname):
    today = datetime.now().strftime("%Y-%m-%d")
    with _login_day_cache_lock:
        if _login_day_cache.get(nickname) == today:
            return
        _login_day_cache[nickname] = today
    try:
        db.execute("INSERT OR IGNORE INTO login_days (nickname, day) VALUES (?, ?)", (nickname, today))
        db.commit()
    except Exception as e:
        print(f"Ошибка записи login_days: {e}")

def log_action(action, nickname=None, details=None):
    try:
        db = get_db()
        db.execute(
            "INSERT INTO server_logs (action, nickname, details) VALUES (?, ?, ?)",
            (action, nickname, details)
        )
        db.commit()
    except Exception as e:
        print(f"Ошибка логирования: {e}")

def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({"error": "Требуется авторизация"}), 401
        
        db = get_db()
        profile = db.execute("SELECT * FROM profiles WHERE token = ?", (token,)).fetchone()
        
        if not profile:
            return jsonify({"error": "Недействительный токен"}), 401
        
        if profile['is_banned']:
            return jsonify({"error": f"Профиль заблокирован: {profile['ban_reason']}"}), 403
        
        g.current_profile = dict(profile)
        touch_last_seen(db, profile['nickname'])
        record_login_day(db, profile['nickname'])
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    @auth_required
    def decorated(*args, **kwargs):
        if not g.current_profile.get('is_admin'):
            return jsonify({"error": "Требуется права администратора"}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"success": True, "status": "ok"})

@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат данных"}), 400
    
    nickname = data.get('nickname', '').strip()
    password = data.get('password', '').strip()
    
    if not nickname or not password:
        return jsonify({"error": "Ник и пароль обязательны"}), 400
    
    if len(nickname) < 3 or len(nickname) > 20:
        return jsonify({"error": "Ник должен быть от 3 до 20 символов"}), 400
    
    if len(password) < 6:
        return jsonify({"error": "Пароль должен быть минимум 6 символов"}), 400
    
    if not nickname.replace('_', '').replace('-', '').isalnum():
        return jsonify({"error": "Ник может содержать только буквы, цифры, _ и -"}), 400
    
    db = get_db()
    existing = db.execute("SELECT id FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if existing:
        return jsonify({"error": "Ник уже занят"}), 409
    
    salt = secrets.token_hex(16)
    password_hash = hash_password(password, salt)
    token = generate_token()
    
    db.execute(
        "INSERT INTO profiles (nickname, password_hash, salt, token, last_login) VALUES (?, ?, ?, ?, ?)",
        (nickname, password_hash, salt, token, datetime.now().isoformat())
    )
    db.commit()
    
    log_action("register", nickname)
    
    return jsonify({
        "success": True,
        "token": token,
        "nickname": nickname,
        "message": "Регистрация успешна"
    }), 201

def _check_login_lockout(nickname):
    with _login_attempts_lock:
        entry = _login_attempts.get(nickname)
        if not entry:
            return 0
        remaining = entry["locked_until"] - time.time()
        return max(0, remaining)

def _register_login_failure(nickname):
    with _login_attempts_lock:
        entry = _login_attempts.setdefault(nickname, {"count": 0, "locked_until": 0})
        entry["count"] += 1
        if entry["count"] >= LOGIN_MAX_ATTEMPTS:
            entry["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
            entry["count"] = 0

def _clear_login_failures(nickname):
    with _login_attempts_lock:
        _login_attempts.pop(nickname, None)

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат данных"}), 400
    
    nickname = data.get('nickname', '').strip()
    password = data.get('password', '').strip()

    if not nickname or not password:
        return jsonify({"error": "Ник и пароль обязательны"}), 400

    lockout_remaining = _check_login_lockout(nickname)
    if lockout_remaining > 0:
        return jsonify({
            "error": f"Слишком много неудачных попыток. Повторите через {int(lockout_remaining)} сек."
        }), 429
    
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    
    if not profile:
        _register_login_failure(nickname)
        return jsonify({"error": "Неверный ник или пароль"}), 404
    
    if profile['is_banned']:
        return jsonify({"error": f"Профиль заблокирован: {profile['ban_reason']}"}), 403
    
    ok, needs_upgrade = verify_password(password, profile['salt'], profile['password_hash'])
    if not ok:
        _register_login_failure(nickname)
        return jsonify({"error": "Неверный ник или пароль"}), 401

    _clear_login_failures(nickname)

    token = generate_token()
    now_iso = datetime.now().isoformat()
    if needs_upgrade:
        new_hash = hash_password(password, profile['salt'])
        db.execute(
            "UPDATE profiles SET token = ?, last_login = ?, last_seen = ?, password_hash = ? WHERE id = ?",
            (token, now_iso, now_iso, new_hash, profile['id'])
        )
    else:
        db.execute(
            "UPDATE profiles SET token = ?, last_login = ?, last_seen = ? WHERE id = ?",
            (token, now_iso, now_iso, profile['id'])
        )
    db.commit()
    
    log_action("login", nickname)
    
    return jsonify({
        "success": True,
        "token": token,
        "nickname": nickname,
        "is_admin": bool(profile['is_admin']),
        "coins": profile['coins'],
        "playtime": profile['total_playtime_minutes']
    })

@app.route('/api/profile/<nickname>', methods=['GET'])
def get_profile(nickname):
    db = get_db()
    profile = db.execute(
        "SELECT nickname, coins, total_playtime_minutes, total_launches, created_at, "
        "last_login, last_seen, is_admin, bio, gifts_visible FROM profiles WHERE nickname = ?",
        (nickname,)
    ).fetchone()

    if not profile:
        return jsonify({"error": "Профиль не найден"}), 404

    result = dict(profile)
    result["is_online"] = is_online(profile["last_seen"])
    ach_count = db.execute(
        "SELECT COUNT(*) AS c FROM achievements WHERE nickname = ?", (nickname,)
    ).fetchone()["c"]
    result["achievements_count"] = ach_count

    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    viewer = None
    if token:
        viewer_row = db.execute("SELECT nickname FROM profiles WHERE token = ?", (token,)).fetchone()
        if viewer_row:
            viewer = viewer_row["nickname"]
    if viewer and viewer != nickname:
        rel = db.execute(
            "SELECT status, requester FROM friendships WHERE "
            "(requester = ? AND addressee = ?) OR (requester = ? AND addressee = ?)",
            (viewer, nickname, nickname, viewer)
        ).fetchone()
        if not rel:
            result["friendship_status"] = "none"
        elif rel["status"] == "accepted":
            result["friendship_status"] = "friends"
        elif rel["requester"] == viewer:
            result["friendship_status"] = "request_sent"
        else:
            result["friendship_status"] = "request_received"
    else:
        result["friendship_status"] = "self" if viewer == nickname else None

    return jsonify(result)


@app.route('/api/users/search', methods=['GET'])
@auth_required
def search_users():
    query = (request.args.get('q') or '').strip()
    if len(query) < 2:
        return jsonify({"error": "Введите минимум 2 символа для поиска"}), 400
    me = g.current_profile['nickname']
    db = get_db()
    rows = db.execute(
        "SELECT nickname, last_seen FROM profiles WHERE nickname LIKE ? AND nickname != ? "
        "ORDER BY nickname LIMIT 25",
        (f"%{query}%", me)
    ).fetchall()
    friend_rows = db.execute(
        "SELECT requester, addressee, status FROM friendships WHERE requester = ? OR addressee = ?",
        (me, me)
    ).fetchall()
    friend_status = {}
    for r in friend_rows:
        other = r['addressee'] if r['requester'] == me else r['requester']
        friend_status[other] = 'friends' if r['status'] == 'accepted' else 'pending'
    results = []
    for r in rows:
        results.append({
            "nickname": r["nickname"],
            "is_online": is_online(r["last_seen"]),
            "friendship_status": friend_status.get(r["nickname"], "none"),
        })
    return jsonify({"results": results})


@app.route('/api/profile/me', methods=['PUT'])
@auth_required
def update_my_profile():
    data = request.get_json(force=True, silent=True) or {}
    bio = data.get('bio')
    db = get_db()
    me = g.current_profile['nickname']
    if bio is not None:
        bio = str(bio).strip()
        if len(bio) > 300:
            return jsonify({"error": "Описание слишком длинное (максимум 300 символов)"}), 400
        db.execute("UPDATE profiles SET bio = ? WHERE nickname = ?", (bio, me))
        db.commit()
        log_action("profile_edit", me, "bio")
    return jsonify({"success": True})


@app.route('/api/profile/gifts_visibility', methods=['POST'])
@auth_required
def set_gifts_visibility():
    data = request.get_json(force=True, silent=True) or {}
    visible = 1 if data.get('visible') else 0
    db = get_db()
    me = g.current_profile['nickname']
    db.execute("UPDATE profiles SET gifts_visible = ? WHERE nickname = ?", (visible, me))
    db.commit()
    log_action("profile_edit", me, f"gifts_visible={visible}")
    return jsonify({"success": True, "gifts_visible": bool(visible)})

    result = dict(profile)
    result["is_online"] = is_online(result.get("last_seen"))
    return jsonify(result)

@app.route('/api/me', methods=['GET'])
@auth_required
def get_me():
    profile = dict(g.current_profile)
    profile["is_online"] = True
    return jsonify(profile)

@app.route('/api/logout', methods=['POST'])
@auth_required
def logout():
    db = get_db()
    nickname = g.current_profile['nickname']
    db.execute(
        "UPDATE profiles SET token = NULL, last_seen = NULL WHERE id = ?",
        (g.current_profile['id'],)
    )
    db.commit()
    log_action("logout", nickname)
    return jsonify({"success": True})

@app.route('/api/heartbeat', methods=['POST'])
@auth_required
def heartbeat():
    db = get_db()
    touch_last_seen(db, g.current_profile['nickname'])
    return jsonify({"success": True})

@app.route('/api/online/<nickname>', methods=['GET'])
def get_online_status(nickname):
    db = get_db()
    row = db.execute("SELECT last_seen FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not row:
        return jsonify({"error": "Профиль не найден"}), 404
    return jsonify({"nickname": nickname, "is_online": is_online(row["last_seen"])})

@app.route('/api/launch', methods=['POST'])
@auth_required
def report_launch():
    data = request.get_json(silent=True) or {}
    version = str(data.get('version', 'unknown'))[:64]

    db = get_db()
    db.execute(
        "UPDATE profiles SET total_launches = total_launches + 1 WHERE id = ?",
        (g.current_profile['id'],)
    )
    db.commit()
    row = db.execute(
        "SELECT total_launches FROM profiles WHERE id = ?", (g.current_profile['id'],)
    ).fetchone()

    log_action("launch", g.current_profile['nickname'], version)
    return jsonify({"success": True, "total_launches": row['total_launches']})

@app.route('/api/playtime', methods=['POST'])
@auth_required
def update_playtime():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Неверный формат"}), 400

    try:
        minutes = float(data.get('minutes', 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректное значение времени"}), 400
    version = str(data.get('version', 'unknown'))[:64]

    if minutes <= 0:
        return jsonify({"error": "Время должно быть положительным"}), 400

    launch_inc = 0 if data.get('launch_counted') else 1

    coins_earned = minutes * 0.5

    db = get_db()
    db.execute(
        "UPDATE profiles SET total_playtime_minutes = total_playtime_minutes + ?, coins = coins + ?, "
        "total_launches = total_launches + ? WHERE id = ?",
        (minutes, coins_earned, launch_inc, g.current_profile['id'])
    )

    db.execute(
        "INSERT INTO play_sessions (nickname, version, start_time, duration_minutes, coins_earned) VALUES (?, ?, ?, ?, ?)",
        (g.current_profile['nickname'], version, datetime.now().isoformat(), minutes, coins_earned)
    )
    db.commit()

    log_action("playtime", g.current_profile['nickname'], f"{minutes} мин, {version}")

    return jsonify({
        "success": True,
        "coins_earned": coins_earned,
        "total_coins": g.current_profile['coins'] + coins_earned,
        "total_playtime": g.current_profile['total_playtime_minutes'] + minutes,
        "total_launches": g.current_profile['total_launches'] + launch_inc
    })

@app.route('/api/top', methods=['GET'])
def get_top():
    limit = request.args.get('limit', 10, type=int)
    limit = min(max(limit, 1), 100)
    
    db = get_db()
    rows = db.execute(
        "SELECT nickname, coins, total_playtime_minutes, total_launches, last_seen FROM profiles WHERE is_banned = 0 ORDER BY coins DESC LIMIT ?",
        (limit,)
    ).fetchall()
    
    top = []
    for i, row in enumerate(rows, 1):
        top.append({
            "rank": i,
            "nickname": row['nickname'],
            "coins": row['coins'],
            "playtime": row['total_playtime_minutes'],
            "launches": row['total_launches'],
            "is_online": is_online(row['last_seen'])
        })
    
    return jsonify({"top": top})

@app.route('/api/achievements/sync', methods=['POST'])
@auth_required
def sync_achievements():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400

    ids = data.get('ids', [])
    if not isinstance(ids, list):
        return jsonify({"error": "ids должен быть списком"}), 400

    db = get_db()
    nickname = g.current_profile['nickname']
    synced = []
    for ach_id in ids:
        ach_id = str(ach_id).strip()
        if not ach_id:
            continue
        try:
            db.execute(
                "INSERT OR IGNORE INTO achievements (nickname, achievement_id) VALUES (?, ?)",
                (nickname, ach_id)
            )
            synced.append(ach_id)
        except Exception as e:
            print(f"Ошибка синхронизации ачивки {ach_id}: {e}")
    db.commit()
    if synced:
        log_action("achievements_sync", nickname, ", ".join(synced))
    return jsonify({"success": True, "synced": synced})


@app.route('/api/achievements/mine', methods=['GET'])
@auth_required
def my_achievements():
    db = get_db()
    rows = db.execute(
        "SELECT achievement_id, unlocked_at FROM achievements WHERE nickname = ? ORDER BY unlocked_at",
        (g.current_profile['nickname'],)
    ).fetchall()
    return jsonify({"achievements": [dict(r) for r in rows]})


@app.route('/api/achievements/<nickname>', methods=['GET'])
def achievements_of(nickname):
    db = get_db()
    profile = db.execute("SELECT id FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not profile:
        return jsonify({"error": "Профиль не найден"}), 404
    rows = db.execute(
        "SELECT achievement_id, unlocked_at FROM achievements WHERE nickname = ? ORDER BY unlocked_at",
        (nickname,)
    ).fetchall()
    return jsonify({"achievements": [dict(r) for r in rows]})


@app.route('/api/achievement_counters/bump', methods=['POST'])
@auth_required
def bump_achievement_counters():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400

    deltas = data.get('deltas', {})
    sets = data.get('set', {})
    if not isinstance(deltas, dict) or not isinstance(sets, dict):
        return jsonify({"error": "deltas и set должны быть объектами"}), 400

    db = get_db()
    nickname = g.current_profile['nickname']
    for name, amount in deltas.items():
        name = str(name).strip()[:64]
        if not name:
            continue
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        db.execute(
            "INSERT INTO achievement_counters (nickname, counter_name, value, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(nickname, counter_name) DO UPDATE SET "
            "value = value + excluded.value, updated_at = CURRENT_TIMESTAMP",
            (nickname, name, amount)
        )
    for name, value in sets.items():
        name = str(name).strip()[:64]
        if not name:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        db.execute(
            "INSERT INTO achievement_counters (nickname, counter_name, value, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(nickname, counter_name) DO UPDATE SET "
            "value = excluded.value, updated_at = CURRENT_TIMESTAMP",
            (nickname, name, value)
        )
    db.commit()
    return jsonify({"success": True})


@app.route('/api/achievement_counters/mine', methods=['GET'])
@auth_required
def my_achievement_counters():
    db = get_db()
    rows = db.execute(
        "SELECT counter_name, value FROM achievement_counters WHERE nickname = ?",
        (g.current_profile['nickname'],)
    ).fetchall()
    return jsonify({"counters": {r['counter_name']: r['value'] for r in rows}})


@app.route('/api/friends/request', methods=['POST'])
@auth_required
def friend_request():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    target = data.get('nickname', '').strip()
    me = g.current_profile['nickname']
    if not target:
        return jsonify({"error": "nickname обязателен"}), 400
    if target == me:
        return jsonify({"error": "Нельзя добавить себя в друзья"}), 400

    db = get_db()
    target_profile = db.execute("SELECT id FROM profiles WHERE nickname = ?", (target,)).fetchone()
    if not target_profile:
        return jsonify({"error": "Такого игрока не существует"}), 404

    existing = db.execute(
        "SELECT * FROM friendships WHERE (requester = ? AND addressee = ?) OR (requester = ? AND addressee = ?)",
        (me, target, target, me)
    ).fetchone()
    if existing:
        if existing['status'] == 'accepted':
            return jsonify({"error": "Вы уже друзья"}), 409
        return jsonify({"error": "Заявка уже отправлена и ожидает ответа"}), 409

    db.execute(
        "INSERT INTO friendships (requester, addressee, status) VALUES (?, ?, 'pending')",
        (me, target)
    )
    db.commit()
    log_action("friend_request", me, target)
    return jsonify({"success": True})


@app.route('/api/friends/respond', methods=['POST'])
@auth_required
def friend_respond():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    requester = data.get('nickname', '').strip()
    accept = bool(data.get('accept'))
    me = g.current_profile['nickname']

    db = get_db()
    row = db.execute(
        "SELECT * FROM friendships WHERE requester = ? AND addressee = ? AND status = 'pending'",
        (requester, me)
    ).fetchone()
    if not row:
        return jsonify({"error": "Заявка не найдена"}), 404

    if accept:
        db.execute(
            "UPDATE friendships SET status = 'accepted' WHERE id = ?", (row['id'],)
        )
        log_action("friend_accept", me, requester)
    else:
        db.execute("DELETE FROM friendships WHERE id = ?", (row['id'],))
        log_action("friend_decline", me, requester)
    db.commit()
    return jsonify({"success": True})


@app.route('/api/friends/remove', methods=['POST'])
@auth_required
def friend_remove():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    target = data.get('nickname', '').strip()
    me = g.current_profile['nickname']

    db = get_db()
    db.execute(
        "DELETE FROM friendships WHERE (requester = ? AND addressee = ?) OR (requester = ? AND addressee = ?)",
        (me, target, target, me)
    )
    db.commit()
    log_action("friend_remove", me, target)
    return jsonify({"success": True})


@app.route('/api/friends/list', methods=['GET'])
@auth_required
def friends_list():
    me = g.current_profile['nickname']
    db = get_db()
    accepted = db.execute(
        "SELECT requester, addressee, created_at FROM friendships "
        "WHERE status = 'accepted' AND (requester = ? OR addressee = ?)",
        (me, me)
    ).fetchall()
    incoming = db.execute(
        "SELECT requester, created_at FROM friendships WHERE status = 'pending' AND addressee = ?",
        (me,)
    ).fetchall()
    outgoing = db.execute(
        "SELECT addressee, created_at FROM friendships WHERE status = 'pending' AND requester = ?",
        (me,)
    ).fetchall()

    friends = []
    for r in accepted:
        other = r['addressee'] if r['requester'] == me else r['requester']
        other_row = db.execute("SELECT last_seen FROM profiles WHERE nickname = ?", (other,)).fetchone()
        online = is_online(other_row["last_seen"]) if other_row else False
        friends.append({"nickname": other, "since": r['created_at'], "is_online": online})

    return jsonify({
        "friends": friends,
        "incoming_requests": [dict(r) for r in incoming],
        "outgoing_requests": [dict(r) for r in outgoing],
    })


def _are_friends(db, a, b):
    if a == b:
        return True
    row = db.execute(
        "SELECT id FROM friendships WHERE status = 'accepted' AND "
        "((requester = ? AND addressee = ?) OR (requester = ? AND addressee = ?))",
        (a, b, b, a)
    ).fetchone()
    return row is not None


@app.route('/api/messages/send', methods=['POST'])
@auth_required
def send_message():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    to = data.get('to', '').strip()
    text = data.get('text', '').strip()
    me = g.current_profile['nickname']

    if not to or not text:
        return jsonify({"error": "to и text обязательны"}), 400
    if len(text) > 2000:
        return jsonify({"error": "Слишком длинное сообщение (макс. 2000 символов)"}), 400

    db = get_db()
    if not _are_friends(db, me, to):
        return jsonify({"error": "Переписываться можно только с друзьями"}), 403

    db.execute(
        "INSERT INTO messages (sender, receiver, text) VALUES (?, ?, ?)",
        (me, to, text)
    )
    db.commit()
    return jsonify({"success": True})


@app.route('/api/messages/with/<nickname>', methods=['GET'])
@auth_required
def get_messages_with(nickname):
    me = g.current_profile['nickname']
    after_id = request.args.get('after_id', 0, type=int)

    db = get_db()
    rows = db.execute(
        "SELECT m.id, m.sender, m.receiver, m.text, m.created_at, m.message_type, "
        "gs.gift_name, gs.gift_image_filename, gs.price_paid AS gift_price, gs.note AS gift_note "
        "FROM messages m LEFT JOIN gift_sends gs ON gs.id = m.gift_send_id "
        "WHERE ((m.sender = ? AND m.receiver = ?) OR (m.sender = ? AND m.receiver = ?)) AND m.id > ? "
        "ORDER BY m.id ASC LIMIT 200",
        (me, nickname, nickname, me, after_id)
    ).fetchall()

    db.execute(
        "UPDATE messages SET is_read = 1 WHERE sender = ? AND receiver = ?",
        (nickname, me)
    )
    db.commit()

    messages = []
    for r in rows:
        item = dict(r)
        if item.get("gift_image_filename"):
            item["gift_image_url"] = f"/api/gifts/image/{item['gift_image_filename']}"
        messages.append(item)
    return jsonify({"messages": messages})


@app.route('/api/messages/unread_count', methods=['GET'])
@auth_required
def unread_count():
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) FROM messages WHERE receiver = ? AND is_read = 0",
        (g.current_profile['nickname'],)
    ).fetchone()
    return jsonify({"count": row[0]})


_typing_lock = threading.Lock()
_typing_status = {}

@app.route('/api/messages/typing', methods=['POST'])
@auth_required
def set_typing():
    data = request.get_json() or {}
    to = str(data.get('to', '')).strip()
    me = g.current_profile['nickname']
    if not to:
        return jsonify({"error": "to обязателен"}), 400
    db = get_db()
    if not _are_friends(db, me, to):
        return jsonify({"error": "Переписываться можно только с друзьями"}), 403
    with _typing_lock:
        _typing_status[(me, to)] = time.time()
    return jsonify({"success": True})

@app.route('/api/messages/typing/<nickname>', methods=['GET'])
@auth_required
def get_typing(nickname):
    me = g.current_profile['nickname']
    with _typing_lock:
        ts = _typing_status.get((nickname, me))
    is_typing = bool(ts) and (time.time() - ts) < TYPING_THRESHOLD_SECONDS
    return jsonify({"nickname": nickname, "is_typing": is_typing})


def _gift_to_dict(row, include_admin_fields=False):
    qty = row["quantity"]
    result = {
        "id": row["id"],
        "name": row["name"],
        "price": row["price"],
        "image_url": f"/api/gifts/image/{row['image_filename']}",
        "quantity": None if (qty is None or qty < 0) else qty,
        "sale_until": row["sale_until"],
        "on_sale": gift_is_on_sale(row),
    }
    if include_admin_fields:
        result["is_active"] = bool(row["is_active"])
        result["created_by"] = row["created_by"]
        result["created_at"] = row["created_at"]
        result["sale_until_yekb"] = system_naive_to_yekb_input(row["sale_until"])
    return result


@app.route('/api/gifts', methods=['GET'])
@auth_required
def list_gifts():
    db = get_db()
    rows = db.execute("SELECT * FROM gifts ORDER BY id DESC").fetchall()
    gifts = [_gift_to_dict(r) for r in rows if gift_is_on_sale(r)]
    return jsonify({"gifts": gifts})


@app.route('/api/gifts/image/<path:filename>', methods=['GET'])
def gift_image(filename):
    filename = secure_filename(filename)
    ext = os.path.splitext(filename)[1].lower()
    mimetypes_by_ext = {
        ".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".webp": "image/webp", ".bmp": "image/bmp",
    }
    return send_from_directory(GIFTS_DIR, filename, mimetype=mimetypes_by_ext.get(ext))


@app.route('/api/gifts/send', methods=['POST'])
@auth_required
def send_gift():
    data = request.get_json(force=True, silent=True) or {}
    gift_id = data.get('gift_id')
    to = (data.get('to') or '').strip()
    note = (data.get('message') or '').strip()
    me = g.current_profile['nickname']

    if not gift_id or not to:
        return jsonify({"error": "gift_id и to обязательны"}), 400
    if len(note) > 500:
        return jsonify({"error": "Слишком длинное сообщение к подарку (макс. 500 символов)"}), 400

    db = get_db()
    receiver_row = db.execute("SELECT nickname, is_banned FROM profiles WHERE nickname = ?", (to,)).fetchone()
    if not receiver_row:
        return jsonify({"error": "Такого пользователя нет"}), 404
    if receiver_row['is_banned']:
        return jsonify({"error": "Пользователь заблокирован"}), 403

    gift = db.execute("SELECT * FROM gifts WHERE id = ?", (gift_id,)).fetchone()
    if not gift:
        return jsonify({"error": "Подарок не найден"}), 404
    if not gift_is_on_sale(gift):
        return jsonify({"error": "Этот подарок сейчас недоступен для покупки"}), 409

    price = gift['price']
    my_coins = g.current_profile['coins']
    if my_coins < price:
        return jsonify({"error": "Недостаточно монет"}), 402

    if gift['quantity'] is not None and gift['quantity'] >= 0:
        cur = db.execute(
            "UPDATE gifts SET quantity = quantity - 1 WHERE id = ? AND quantity > 0",
            (gift_id,)
        )
        if cur.rowcount == 0:
            db.commit()
            return jsonify({"error": "Подарок только что закончился"}), 409

    cur = db.execute(
        "UPDATE profiles SET coins = coins - ? WHERE id = ? AND coins >= ?",
        (price, g.current_profile['id'], price)
    )
    if cur.rowcount == 0:
        db.rollback()
        return jsonify({"error": "Недостаточно монет"}), 402

    cursor = db.execute(
        "INSERT INTO gift_sends (gift_id, gift_name, gift_image_filename, sender, receiver, note, price_paid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (gift['id'], gift['name'], gift['image_filename'], me, to, note, price)
    )
    gift_send_id = cursor.lastrowid

    if _are_friends(db, me, to):
        chat_text = note if note else f"🎁 Подарок: {gift['name']}"
        db.execute(
            "INSERT INTO messages (sender, receiver, text, message_type, gift_send_id) VALUES (?, ?, ?, 'gift', ?)",
            (me, to, chat_text, gift_send_id)
        )

    db.commit()
    log_action("gift_send", me, f"-> {to}: {gift['name']} (-{price} монет)")
    new_coins = db.execute("SELECT coins FROM profiles WHERE nickname = ?", (me,)).fetchone()['coins']
    return jsonify({"success": True, "total_coins": new_coins, "gift_send_id": gift_send_id})


@app.route('/api/gifts/received/<nickname>', methods=['GET'])
def gifts_received(nickname):
    db = get_db()
    profile = db.execute("SELECT gifts_visible FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not profile:
        return jsonify({"error": "Профиль не найден"}), 404
    if not profile['gifts_visible']:
        return jsonify({"gifts": [], "hidden": True})
    rows = db.execute(
        "SELECT gift_name, gift_image_filename, sender, note, created_at FROM gift_sends "
        "WHERE receiver = ? ORDER BY id DESC LIMIT 100",
        (nickname,)
    ).fetchall()
    gifts = [{
        "name": r["gift_name"],
        "image_url": f"/api/gifts/image/{r['gift_image_filename']}",
        "from": r["sender"],
        "note": r["note"],
        "created_at": r["created_at"],
    } for r in rows]
    return jsonify({"gifts": gifts, "hidden": False})


@app.route('/api/news', methods=['GET'])
def get_news():
    db = get_db()
    rows = db.execute(
        "SELECT id, title, body, author, created_at, edited, edited_at FROM news ORDER BY id DESC"
    ).fetchall()
    return jsonify({"news": [dict(r) for r in rows]})


@app.route('/api/news/latest_id', methods=['GET'])
def news_latest_id():
    db = get_db()
    row = db.execute("SELECT MAX(id) AS max_id FROM news").fetchone()
    return jsonify({"latest_id": row["max_id"] or 0})


@app.route('/api/news', methods=['POST'])
@admin_required
def publish_news():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    title = data.get('title', '').strip()
    body = data.get('body', '').strip()
    if not title or not body:
        return jsonify({"error": "title и body обязательны"}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO news (title, body, author) VALUES (?, ?, ?)",
        (title, body, g.current_profile['nickname'])
    )
    db.commit()
    log_action("news_publish", g.current_profile['nickname'], title)
    return jsonify({"success": True, "id": cur.lastrowid}), 201


@app.route('/api/news/<int:news_id>', methods=['PUT'])
@admin_required
def edit_news(news_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    title = data.get('title', '').strip()
    body = data.get('body', '').strip()
    if not title or not body:
        return jsonify({"error": "title и body обязательны"}), 400

    db = get_db()
    row = db.execute("SELECT id FROM news WHERE id = ?", (news_id,)).fetchone()
    if not row:
        return jsonify({"error": "Новость не найдена"}), 404

    db.execute(
        "UPDATE news SET title = ?, body = ?, edited = 1, edited_at = ? WHERE id = ?",
        (title, body, datetime.now().isoformat(timespec="seconds"), news_id)
    )
    db.commit()
    log_action("news_edit", g.current_profile['nickname'], title)
    return jsonify({"success": True})


@app.route('/api/news/<int:news_id>', methods=['DELETE'])
@admin_required
def delete_news(news_id):
    db = get_db()
    db.execute("DELETE FROM news WHERE id = ?", (news_id,))
    db.commit()
    log_action("news_delete", g.current_profile['nickname'], str(news_id))
    return jsonify({"success": True})


def cascade_delete_profile(db, nickname):
    db.execute("DELETE FROM profiles WHERE nickname = ?", (nickname,))
    db.execute("DELETE FROM play_sessions WHERE nickname = ?", (nickname,))
    db.execute("DELETE FROM achievements WHERE nickname = ?", (nickname,))
    db.execute(
        "DELETE FROM friendships WHERE requester = ? OR addressee = ?", (nickname, nickname)
    )
    db.execute(
        "DELETE FROM messages WHERE sender = ? OR receiver = ?", (nickname, nickname)
    )
    db.commit()


@app.route('/api/admin/delete_account', methods=['POST'])
@admin_required
def api_delete_account():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    nickname = data.get('nickname', '').strip()
    if not nickname:
        return jsonify({"error": "nickname обязателен"}), 400
    if nickname == g.current_profile['nickname']:
        return jsonify({"error": "Нельзя удалить самого себя"}), 400

    db = get_db()
    profile = db.execute("SELECT id FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not profile:
        return jsonify({"error": "Профиль не найден"}), 404

    cascade_delete_profile(db, nickname)
    log_action("delete_account", g.current_profile['nickname'], nickname)
    return jsonify({"success": True})


LOGIN_TEMPLATE = """
<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Mafin Launcher — Админ-панель</title>
<style>
body{background:#1a1d23;color:#e1e4e8;font-family:'Segoe UI',Arial,sans-serif;
     display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#242830;padding:40px;border-radius:12px;width:340px;box-shadow:0 8px 24px rgba(0,0,0,.5)}
h1{color:#43b581;font-size:19px;margin:0 0 20px}
input{width:100%;padding:10px;margin:6px 0 14px;border-radius:6px;border:1px solid #2d323c;
      background:#2d323c;color:#fff;box-sizing:border-box;font-size:14px}
button{width:100%;padding:11px;background:#43b581;color:#fff;border:none;border-radius:6px;
       font-weight:bold;cursor:pointer;font-size:14px}
button:hover{background:#379768}
.err{background:#3a2020;border:1px solid #f04747;color:#ff9b9b;padding:10px 12px;
     border-radius:6px;margin-bottom:14px;white-space:pre-wrap;font-size:13px}
</style></head>
<body><div class="card">
<h1>🛡️ Mafin Launcher — Админ-панель</h1>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post" action="/admin/login">
<input type="text" name="nickname" placeholder="Никнейм" required autofocus>
<input type="text" name="code" placeholder="Одноразовый код (из SSH)" autocomplete="off" required>
<div style="font-size:12px;color:#8b949e;margin:-6px 0 14px">Получить код: на сервере <code>python app.py admin-code ВАШ_НИК</code></div>
<button type="submit">Войти</button>
</form>
</div></body></html>
"""

DASHBOARD_TEMPLATE = """
<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Mafin Launcher — Админ-панель</title>
<style>
* {box-sizing:border-box}
body{background:#1a1d23;color:#e1e4e8;font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:0}
header{background:#242830;padding:16px 28px;display:flex;justify-content:space-between;align-items:center;
       border-bottom:1px solid #2d323c}
header h1{font-size:18px;margin:0;color:#43b581}
header a{color:#7a8599;text-decoration:none;font-size:13px}
header a:hover{color:#fff}
main{padding:24px 28px;max-width:1100px;margin:0 auto}
.stats{display:flex;gap:14px;margin-bottom:22px;flex-wrap:wrap}
.stat{background:#242830;padding:14px 18px;border-radius:8px;flex:1;min-width:140px}
.stat .num{font-size:22px;font-weight:bold;color:#43b581}
.stat .label{font-size:12px;color:#7a8599}
section{background:#242830;border-radius:10px;padding:20px;margin-bottom:24px}
section h2{margin-top:0;font-size:15px;color:#43b581}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #2d323c}
th{color:#7a8599;font-weight:600}
tr:hover{background:#2d323c}
.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold}
.badge.admin{background:#43b581;color:#fff}
.badge.banned{background:#f04747;color:#fff}
.badge.ok{background:#2d323c;color:#7a8599}
.actions form{display:inline}
.actions button{background:#2d323c;color:#e1e4e8;border:none;padding:5px 10px;border-radius:5px;
                 font-size:11px;cursor:pointer;margin:1px}
.actions button:hover{background:#3a3f4a}
.actions button.danger:hover{background:#f04747}
.actions button.success:hover{background:#43b581}
.upload-form input[type=text], .upload-form textarea{width:100%;padding:9px;border-radius:6px;
    border:1px solid #2d323c;background:#2d323c;color:#fff;margin:5px 0 12px;font-family:inherit}
.upload-form input[type=file]{margin:8px 0 14px}
.upload-form button{background:#43b581;color:#fff;border:none;padding:10px 18px;border-radius:6px;
                     font-weight:bold;cursor:pointer}
.upload-form button:hover{background:#379768}
.active-pill{background:#43b581;color:#fff;padding:2px 8px;border-radius:10px;font-size:11px}
.muted{color:#7a8599}
.okmsg{background:#1f3a2a;border:1px solid #43b581;color:#a6f0c6;padding:10px 12px;border-radius:6px;font-size:13px}
.err{background:#3a2020;border:1px solid #f04747;color:#ff9b9b;padding:10px 12px;
     border-radius:6px;font-size:13px;white-space:pre-wrap}
</style></head>
<body>
<header>
  <h1>🛡️ Mafin Launcher — Админ-панель</h1>
  <div>Вы вошли как <b>{{ me.nickname }}</b> &nbsp;|&nbsp; <a href="/admin/quests">📋 Задания</a> &nbsp;|&nbsp; <a href="/admin/gifts">🎁 Подарки</a> &nbsp;|&nbsp; <a href="/admin/news">📰 Новости</a> &nbsp;|&nbsp; <a href="/admin/logout">Выйти</a></div>
</header>
<main>
  <div class="stats">
    <div class="stat"><div class="num">{{ stats.total_profiles }}</div><div class="label">Пользователей</div></div>
    <div class="stat"><div class="num">{{ stats.total_banned }}</div><div class="label">Забанено</div></div>
    <div class="stat"><div class="num">{{ stats.total_admins }}</div><div class="label">Админов</div></div>
  </div>

  <section>
    <h2>🔑 Пароль для лаунчера ({{ me.nickname }})</h2>
    {% if pw_msg %}<div class="{{ 'err' if pw_msg[0] == 'err' else 'okmsg' }}" style="margin-bottom:14px">{{ pw_msg[1] }}</div>{% endif %}
    {% if not has_password %}<div class="muted" style="margin-bottom:12px">Пароль ещё не задан — в лаунчер по этому нику пока войти нельзя. Придумай пароль (от 10 символов).</div>{% endif %}
    <form class="upload-form" method="post" action="/admin/set_password" autocomplete="off">
      {% if has_password %}<input type="password" name="current_password" placeholder="Текущий пароль" required>{% endif %}
      <input type="password" name="new_password" placeholder="Новый пароль (от 10 символов)" minlength="10" required>
      <input type="password" name="new_password2" placeholder="Новый пароль ещё раз" minlength="10" required>
      <button type="submit">{{ 'Сменить пароль' if has_password else 'Задать пароль' }}</button>
    </form>
    <form method="post" action="/admin/users/{{ me.nickname }}/reset_session" style="margin-top:12px"
          onsubmit="return confirm('Сбросить твою сессию в лаунчере? Придётся войти заново.');">
      <button type="submit" class="danger">Сбросить мою сессию в лаунчере</button>
    </form>
  </section>

  <section>
    <h2>📦 Обновления лаунчера</h2>
    {% if upload_error %}<div class="err" style="margin-bottom:14px">{{ upload_error }}</div>{% endif %}
    <form class="upload-form" method="post" action="/admin/updates/upload" enctype="multipart/form-data">
      <input type="text" name="version" placeholder="Версия, например 1.3" required>
      <textarea name="changelog" placeholder="Что нового в этой версии..." rows="3"></textarea>
      <input type="file" name="file" accept=".exe,.zip" required>
      <label style="display:flex;align-items:center;gap:6px;margin:6px 0;font-size:13px;color:#c8d0e0">
        <input type="checkbox" name="extra_needed" value="1"
               onchange="document.getElementById('extra_file_row').style.display=this.checked?'block':'none'">
        Приложить дополнительный файл
      </label>
      <div id="extra_file_row" style="display:none">
        <input type="file" name="extra_file" accept=".dll,.zip">
      </div>
      <button type="submit">⬆️ Загрузить и сделать активной</button>
    </form>
    <table>
      <tr><th>Версия</th><th>Файл</th><th>Размер</th><th>Доп. файл</th><th>Кем загружено</th><th>Дата</th><th>Статус</th><th>Действия</th></tr>
      {% for u in updates %}
      <tr>
        <td>{{ u.version }}</td>
        <td>{{ u.filename }}</td>
        <td>{{ (u.file_size / 1024 / 1024) | round(1) }} МБ</td>
        <td class="muted">{% if u.extra_filename %}{{ u.extra_filename }} ({{ (u.extra_file_size / 1024) | round(0) }} КБ){% else %}—{% endif %}</td>
        <td>{{ u.uploaded_by or '-' }}</td>
        <td class="muted">{{ u.uploaded_at }}</td>
        <td>{% if u.is_active %}<span class="active-pill">Активна</span>{% else %}—{% endif %}</td>
        <td class="actions">
          {% if not u.is_active %}
          <form method="post" action="/admin/updates/{{ u.id }}/activate">
            <button type="submit" class="success">Сделать активной</button>
          </form>
          {% endif %}
          <form method="post" action="/admin/updates/{{ u.id }}/delete" onsubmit="return confirm('Удалить эту версию?');">
            <button type="submit" class="danger">Удалить</button>
          </form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="8" class="muted">Обновлений ещё не загружено.</td></tr>
      {% endfor %}
    </table>
  </section>

  <section>
    <h2>👤 Пользователи</h2>
    <table>
      <tr><th>Никнейм</th><th>Статус</th><th>Коины</th><th>Минут</th><th>Запусков</th><th>Регистрация</th><th>Действия</th></tr>
      {% for p in profiles %}
      <tr>
        <td>{{ p.nickname }}</td>
        <td>
          {% if p.is_admin %}<span class="badge admin">Админ</span>{% endif %}
          {% if p.is_banned %}<span class="badge banned">Бан</span>{% else %}<span class="badge ok">ОК</span>{% endif %}
        </td>
        <td>{{ p.coins }}</td>
        <td>{{ p.total_playtime_minutes }}</td>
        <td>{{ p.total_launches }}</td>
        <td class="muted">{{ p.created_at }}</td>
        <td class="actions">
          <form method="post" action="/admin/users/{{ p.nickname }}/give_coins"
                style="display:inline-flex;gap:4px;align-items:center">
            <input type="number" step="any" name="amount" placeholder="±монет" required
                   style="width:70px">
            <button type="submit">Выдать</button>
          </form>
          {% if p.is_banned %}
          <form method="post" action="/admin/users/{{ p.nickname }}/unban"><button type="submit" class="success">Разбанить</button></form>
          {% else %}
          <form method="post" action="/admin/users/{{ p.nickname }}/ban" onsubmit="return confirm('Забанить {{ p.nickname }}?');">
            <button type="submit" class="danger">Забанить</button>
          </form>
          {% endif %}
          {% if p.is_admin %}
          <form method="post" action="/admin/users/{{ p.nickname }}/remove_admin"><button type="submit">Снять админа</button></form>
          {% else %}
          <form method="post" action="/admin/users/{{ p.nickname }}/make_admin"><button type="submit">Сделать админом</button></form>
          {% endif %}
          <form method="post" action="/admin/users/{{ p.nickname }}/reset_session"
                onsubmit="return confirm('Сбросить сессию лаунчера у {{ p.nickname }}? Ему придётся войти заново.');">
            <button type="submit">Сбросить сессию</button>
          </form>
          {% if not p.is_admin %}
          <form method="post" action="/admin/users/{{ p.nickname }}/set_password"
                style="display:inline-flex;gap:4px;align-items:center" autocomplete="off"
                onsubmit="return confirm('Сменить пароль {{ p.nickname }}? Его сессия лаунчера будет сброшена.');">
            <input type="text" name="new_password" placeholder="новый пароль" minlength="6" required style="width:110px">
            <button type="submit">Сменить пароль</button>
          </form>
          {% endif %}
          <form method="post" action="/admin/users/{{ p.nickname }}/delete"
                onsubmit="return confirm('Удалить аккаунт {{ p.nickname }} НАВСЕГДА? Это действие нельзя отменить.');">
            <button type="submit" class="danger">Удалить</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
  </section>
</main>
</body></html>
"""


QUESTS_TEMPLATE = """
<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Mafin Launcher — Задания</title>
<style>
* {box-sizing:border-box}
body{background:#1a1d23;color:#e1e4e8;font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:0}
header{background:#242830;padding:16px 28px;display:flex;justify-content:space-between;align-items:center;
       border-bottom:1px solid #2d323c}
header h1{font-size:18px;margin:0;color:#43b581}
header a{color:#7a8599;text-decoration:none;font-size:13px;margin-left:14px}
header a:hover{color:#fff}
main{padding:24px 28px;max-width:1000px;margin:0 auto}
section{background:#242830;border-radius:10px;padding:20px;margin-bottom:24px}
section h2{margin-top:0;font-size:15px;color:#43b581}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #2d323c;vertical-align:top}
th{color:#7a8599;font-weight:600}
tr:hover{background:#2d323c}
.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold}
.badge.on{background:#43b581;color:#fff}
.badge.off{background:#7a8599;color:#fff}
.actions form{display:inline}
.actions button{background:#2d323c;color:#e1e4e8;border:none;padding:5px 10px;border-radius:5px;
                 font-size:11px;cursor:pointer;margin:1px}
.actions button:hover{background:#3a3f4a}
.actions button.danger:hover{background:#f04747}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
.form-grid label{font-size:12px;color:#7a8599;display:block;margin-top:10px}
select,input[type=text],input[type=number],textarea{width:100%;padding:9px;border-radius:6px;
    border:1px solid #2d323c;background:#2d323c;color:#fff;margin-top:4px;font-family:inherit;font-size:13px}
textarea{grid-column:1 / -1}
.submit-row{grid-column:1 / -1;margin-top:16px}
button.primary{background:#43b581;color:#fff;border:none;padding:10px 18px;border-radius:6px;
                     font-weight:bold;cursor:pointer}
button.primary:hover{background:#379768}
.muted{color:#7a8599;font-size:12px}
.tpl-hint{font-size:12px;color:#7a8599;margin-top:6px}
</style></head>
<body>
<header>
  <h1>📋 Задания</h1>
  <div><a href="/admin/dashboard">← Назад в панель</a><a href="/admin/gifts">🎁 Подарки</a><a href="/admin/news">📰 Новости</a><a href="/admin/logout">Выйти</a></div>
</header>
<main>
  <section>
    <h2>➕ Новое задание</h2>
    <form method="post" action="/admin/quests/create">
      <div class="form-grid">
        <div>
          <label>Шаблон (тип действия)</label>
          <select name="template_id" id="template_id" onchange="updateHint()" required>
            {% for key, tpl in templates.items() %}
            <option value="{{ key }}" data-needs-extra="{{ '1' if tpl.needs_extra else '0' }}"
                    data-extra-label="{{ tpl.extra_label or '' }}" data-unit="{{ tpl.unit }}">
              {{ tpl.title.split('{')[0].strip() or key }}
            </option>
            {% endfor %}
          </select>
          <div class="tpl-hint" id="tpl-hint"></div>
        </div>
        <div>
          <label>Название задания (видно игроку)</label>
          <input type="text" name="title" placeholder="Например: Отыграй час в выходные" required>
        </div>
      </div>
      <div class="form-grid">
        <div>
          <label>Целевое значение</label>
          <input type="number" step="any" name="target_value" placeholder="Например 60" required>
        </div>
      </div>
      <div class="form-grid" id="extra-row">
        <div id="extra-field">
          <label id="extra-label">Доп. параметр</label>
          <input type="text" name="extra_param" id="extra_param" placeholder="Зависит от шаблона">
        </div>
      </div>
      <div class="form-grid">
        <div>
          <label>Награда, монет</label>
          <input type="number" step="any" name="reward_coins" placeholder="Например 50" required>
        </div>
      </div>
      <textarea name="description" placeholder="Описание задания для игрока (необязательно)" rows="2"></textarea>
      <div class="submit-row"><button type="submit" class="primary">Создать задание</button></div>
    </form>
  </section>

  <section>
    <h2>📜 Существующие задания</h2>
    <table>
      <tr><th>Название</th><th>Шаблон</th><th>Цель</th><th>Награда</th><th>Статус</th><th>Выполнили</th><th>Действия</th></tr>
      {% for q in quests %}
      <tr>
        <td>{{ q.title }}<div class="muted">{{ q.description or '' }}</div></td>
        <td class="muted">{{ q.template_id }}{% if q.extra_param %} ({{ q.extra_param }}){% endif %}</td>
        <td>{{ q.target_value }}</td>
        <td>{{ q.reward_coins }} 🪙</td>
        <td>{% if q.is_active %}<span class="badge on">Активно</span>{% else %}<span class="badge off">Выключено</span>{% endif %}</td>
        <td>{{ q.claims_count }}</td>
        <td class="actions">
          <form method="post" action="/admin/quests/{{ q.id }}/toggle">
            <button type="submit">{% if q.is_active %}Выключить{% else %}Включить{% endif %}</button>
          </form>
          <form method="post" action="/admin/quests/{{ q.id }}/delete" onsubmit="return confirm('Удалить задание «{{ q.title }}»?');">
            <button type="submit" class="danger">Удалить</button>
          </form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="muted">Заданий ещё нет.</td></tr>
      {% endfor %}
    </table>
  </section>
</main>
<script>
const templates = {{ templates_json | safe }};
function updateHint(){
  const sel = document.getElementById('template_id');
  const tpl = templates[sel.value];
  const extraRow = document.getElementById('extra-row');
  const extraInput = document.getElementById('extra_param');
  const extraLabel = document.getElementById('extra-label');
  const hint = document.getElementById('tpl-hint');
  if (!tpl) return;
  extraRow.style.display = tpl.needs_extra ? '' : 'none';
  extraInput.disabled = !tpl.needs_extra;
  extraInput.required = !!tpl.needs_extra;
  if (!tpl.needs_extra) extraInput.value = '';
  extraLabel.textContent = tpl.extra_label || 'Доп. параметр';
  hint.textContent = tpl.unit ? ('Единица измерения цели: ' + tpl.unit) : '';
}
document.addEventListener('DOMContentLoaded', updateHint);
</script>
</body></html>
"""


def web_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        nickname = session.get("admin_nickname")
        if not nickname:
            return redirect(url_for("admin_login_page"))
        db = get_db()
        profile = db.execute(
            "SELECT * FROM profiles WHERE nickname = ? AND is_admin = 1", (nickname,)
        ).fetchone()
        if not profile or profile['is_banned']:
            session.clear()
            return redirect(url_for("admin_login_page"))
        g.web_admin = dict(profile)
        return f(*args, **kwargs)
    return decorated


@app.route('/admin', methods=['GET'])
def admin_login_page():
    if session.get('admin_nickname'):
        return redirect(url_for('admin_dashboard'))
    return render_template_string(LOGIN_TEMPLATE, error=None)


def _normalize_code(raw):
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())

def _hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()

def generate_admin_code(db, nickname):
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
    db.execute("DELETE FROM admin_codes WHERE nickname = ? OR expires_at < ?", (nickname, int(time.time())))
    db.execute(
        "INSERT INTO admin_codes (nickname, code_hash, expires_at) VALUES (?, ?, ?)",
        (nickname, _hash_code(raw), int(time.time()) + ADMIN_CODE_TTL_SECONDS)
    )
    db.commit()
    return f"{raw[:5]}-{raw[5:]}"

@app.route('/admin/login', methods=['POST'])
def admin_login_submit():
    nickname = request.form.get('nickname', '').strip()
    code = _normalize_code(request.form.get('code', ''))
    ip = request.remote_addr or "?"
    keys = (f"web:{nickname}", f"webip:{ip}")
    generic_error = "Неверный ник или код."

    lockout_remaining = max(_check_login_lockout(k) for k in keys)
    if lockout_remaining > 0:
        return render_template_string(
            LOGIN_TEMPLATE,
            error=f"Слишком много неудачных попыток. Повторите через {int(lockout_remaining)} сек."
        ), 429

    def fail(reason):
        for k in keys:
            _register_login_failure(k)
        log_action("admin_panel_denied", nickname, f"{reason} (ip {ip})")
        return render_template_string(LOGIN_TEMPLATE, error=generic_error), 401

    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not profile or not profile['is_admin'] or profile['is_banned']:
        return fail("нет такого админа / не админ / бан")

    row = db.execute(
        "SELECT id FROM admin_codes WHERE nickname = ? AND code_hash = ? AND used = 0 AND expires_at > ?",
        (nickname, _hash_code(code), int(time.time()))
    ).fetchone()
    if not row:
        return fail("неверный или просроченный код")

    cur = db.execute("UPDATE admin_codes SET used = 1 WHERE id = ? AND used = 0", (row['id'],))
    db.commit()
    if cur.rowcount != 1:
        return fail("код уже использован")

    for k in keys:
        _clear_login_failures(k)
    session.clear()
    session.permanent = True
    session['admin_nickname'] = nickname
    log_action("admin_panel_login", nickname, f"ip {ip}")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/logout')
def admin_logout():
    if session.get('admin_nickname'):
        log_action("admin_panel_logout", session['admin_nickname'])
    session.clear()
    return redirect(url_for('admin_login_page'))


@app.route('/admin/dashboard')
@web_admin_required
def admin_dashboard():
    db = get_db()
    profiles = db.execute("SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
    updates = db.execute("SELECT * FROM updates ORDER BY uploaded_at DESC").fetchall()
    stats = {
        "total_profiles": db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0],
        "total_banned": db.execute("SELECT COUNT(*) FROM profiles WHERE is_banned = 1").fetchone()[0],
        "total_admins": db.execute("SELECT COUNT(*) FROM profiles WHERE is_admin = 1").fetchone()[0],
    }
    upload_error = session.pop('upload_error', None)
    pw_msg = session.pop('pw_msg', None)
    return render_template_string(
        DASHBOARD_TEMPLATE, profiles=profiles, updates=updates, stats=stats, me=g.web_admin,
        upload_error=upload_error, pw_msg=pw_msg,
        has_password=(g.web_admin['password_hash'] != '!')
    )


@app.route('/admin/set_password', methods=['POST'])
@web_admin_required
def admin_set_password():
    me = g.web_admin
    nickname = me['nickname']
    lock_key = f"pw:{nickname}"

    def back(kind, text):
        session['pw_msg'] = (kind, text)
        return redirect(url_for('admin_dashboard') + '#')

    if _check_login_lockout(lock_key) > 0:
        return back('err', "Слишком много неверных попыток. Подожди минуту.")

    new1 = request.form.get('new_password', '')
    new2 = request.form.get('new_password2', '')
    if len(new1) < 10:
        return back('err', "Пароль должен быть не короче 10 символов.")
    if new1 != new2:
        return back('err', "Пароли не совпали.")

    if me['password_hash'] != '!':
        ok, _ = verify_password(request.form.get('current_password', ''), me['salt'], me['password_hash'])
        if not ok:
            _register_login_failure(lock_key)
            log_action("admin_password_change_failed", nickname, f"ip {request.remote_addr}")
            return back('err', "Текущий пароль неверный.")
        _clear_login_failures(lock_key)

    salt = secrets.token_hex(16)
    db = get_db()
    db.execute(
        "UPDATE profiles SET password_hash = ?, salt = ?, token = NULL WHERE nickname = ?",
        (hash_password(new1, salt), salt, nickname)
    )
    db.commit()
    log_action("admin_password_set", nickname, f"ip {request.remote_addr}")
    return back('ok', "Пароль сохранён. Теперь можно входить в лаунчер по нику и этому паролю.")


@app.route('/admin/users/<nickname>/ban', methods=['POST'])
@web_admin_required
def web_ban_user(nickname):
    if nickname == g.web_admin['nickname']:
        return redirect(url_for('admin_dashboard'))
    reason = request.form.get('reason', 'Нарушение правил (веб-панель)')
    db = get_db()
    db.execute(
        "UPDATE profiles SET is_banned = 1, ban_reason = ?, token = NULL WHERE nickname = ?",
        (reason, nickname)
    )
    db.commit()
    log_action("ban", g.web_admin['nickname'], f"{nickname}: {reason}")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/users/<nickname>/unban', methods=['POST'])
@web_admin_required
def web_unban_user(nickname):
    db = get_db()
    db.execute("UPDATE profiles SET is_banned = 0, ban_reason = NULL WHERE nickname = ?", (nickname,))
    db.commit()
    log_action("unban", g.web_admin['nickname'], nickname)
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/users/<nickname>/make_admin', methods=['POST'])
@web_admin_required
def web_make_admin(nickname):
    db = get_db()
    db.execute("UPDATE profiles SET is_admin = 1 WHERE nickname = ?", (nickname,))
    db.commit()
    log_action("make_admin", g.web_admin['nickname'], nickname)
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/users/<nickname>/remove_admin', methods=['POST'])
@web_admin_required
def web_remove_admin(nickname):
    if nickname == g.web_admin['nickname']:
        return redirect(url_for('admin_dashboard'))
    db = get_db()
    db.execute("UPDATE profiles SET is_admin = 0 WHERE nickname = ?", (nickname,))
    db.commit()
    log_action("remove_admin", g.web_admin['nickname'], nickname)
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/users/<nickname>/delete', methods=['POST'])
@web_admin_required
def web_delete_user(nickname):
    if nickname == g.web_admin['nickname']:
        return redirect(url_for('admin_dashboard'))
    db = get_db()
    profile = db.execute("SELECT id FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if profile:
        cascade_delete_profile(db, nickname)
        log_action("delete_account", g.web_admin['nickname'], nickname)
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/users/<nickname>/reset_session', methods=['POST'])
@web_admin_required
def web_reset_launcher_session(nickname):
    db = get_db()
    cur = db.execute("UPDATE profiles SET token = NULL WHERE nickname = ?", (nickname,))
    db.commit()
    if cur.rowcount:
        log_action("admin_reset_session", g.web_admin['nickname'], f"сброшена сессия лаунчера: {nickname}")
        session['upload_error'] = f"Сессия лаунчера у {nickname} сброшена — ему нужно войти заново."
    else:
        session['upload_error'] = "Такого пользователя нет."
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/users/<nickname>/set_password', methods=['POST'])
@web_admin_required
def web_set_user_password(nickname):
    new_pw = request.form.get('new_password', '')
    db = get_db()
    target = db.execute("SELECT is_admin FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not target:
        session['upload_error'] = "Такого пользователя нет."
    elif target['is_admin']:
        session['upload_error'] = ("Пароль админа меняет только он сам (блок «Пароль для лаунчера») "
                                   "или владелец сервера командой set-password.")
    elif len(new_pw) < 6:
        session['upload_error'] = "Пароль должен быть не короче 6 символов."
    else:
        salt = secrets.token_hex(16)
        db.execute(
            "UPDATE profiles SET password_hash = ?, salt = ?, token = NULL WHERE nickname = ?",
            (hash_password(new_pw, salt), salt, nickname)
        )
        db.commit()
        log_action("admin_set_user_password", g.web_admin['nickname'], f"сменён пароль: {nickname}")
        session['upload_error'] = f"Пароль для {nickname} изменён, сессия лаунчера сброшена."
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/users/<nickname>/give_coins', methods=['POST'])
@web_admin_required
def web_give_coins(nickname):
    try:
        amount = float(request.form.get('amount', ''))
    except ValueError:
        session['upload_error'] = "Укажите корректное количество монет."
        return redirect(url_for('admin_dashboard'))
    db = get_db()
    profile = db.execute("SELECT id FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not profile:
        session['upload_error'] = "Такого пользователя нет."
        return redirect(url_for('admin_dashboard'))
    db.execute("UPDATE profiles SET coins = coins + ? WHERE nickname = ?", (amount, nickname))
    db.commit()
    log_action("give_coins", g.web_admin['nickname'], f"{nickname}: {amount:+g} монет")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/updates/upload', methods=['POST'])
@web_admin_required
def web_upload_update():
    version = request.form.get('version', '').strip()
    changelog = request.form.get('changelog', '').strip()
    file = request.files.get('file')
    extra_needed = request.form.get('extra_needed') == '1'
    extra_file = request.files.get('extra_file')

    if not version:
        session['upload_error'] = "Введите номер новой версии — без него загрузить обновление нельзя."
        return redirect(url_for('admin_dashboard'))

    if not file or file.filename == '':
        session['upload_error'] = "Выберите файл обновления (.exe или .zip)."
        return redirect(url_for('admin_dashboard'))

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_UPDATE_EXT:
        session['upload_error'] = f"Недопустимый тип файла ({ext or 'без расширения'}). Разрешены: .exe, .zip"
        return redirect(url_for('admin_dashboard'))

    extra_safe_name = None
    extra_size = 0
    if extra_needed:
        if not extra_file or extra_file.filename == '':
            session['upload_error'] = "Галочка «доп. файл» отмечена, но файл не выбран."
            return redirect(url_for('admin_dashboard'))
        extra_ext = os.path.splitext(extra_file.filename)[1].lower()
        if extra_ext not in ALLOWED_EXTRA_FILE_EXT:
            session['upload_error'] = (f"Недопустимый тип доп. файла ({extra_ext or 'без расширения'}). "
                                        f"Разрешены: {', '.join(sorted(ALLOWED_EXTRA_FILE_EXT))}")
            return redirect(url_for('admin_dashboard'))

    db = get_db()
    existing = db.execute("SELECT id FROM updates WHERE version = ?", (version,)).fetchone()
    if existing:
        session['upload_error'] = f"Версия {version} уже была загружена ранее. Укажите другой номер версии."
        return redirect(url_for('admin_dashboard'))

    safe_name = secure_filename(f"MafinLauncher_{version}{ext}")
    save_path = os.path.join(UPDATES_DIR, safe_name)
    file.save(save_path)
    file_size = os.path.getsize(save_path)

    if extra_needed:
        extra_safe_name = secure_filename(f"MafinLauncher_{version}_extra{extra_ext}")
        extra_save_path = os.path.join(UPDATES_DIR, extra_safe_name)
        extra_file.save(extra_save_path)
        extra_size = os.path.getsize(extra_save_path)

    db.execute("UPDATE updates SET is_active = 0")
    db.execute(
        "INSERT INTO updates (version, filename, changelog, is_active, uploaded_by, file_size, extra_filename, extra_file_size) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
        (version, safe_name, changelog, g.web_admin['nickname'], file_size, extra_safe_name, extra_size)
    )
    db.commit()
    extra_log = f", доп. файл {extra_safe_name} ({extra_size} байт)" if extra_safe_name else ""
    log_action("upload_update", g.web_admin['nickname'], f"v{version} ({safe_name}, {file_size} байт){extra_log}")
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/updates/<int:update_id>/activate', methods=['POST'])
@web_admin_required
def web_activate_update(update_id):
    db = get_db()
    db.execute("UPDATE updates SET is_active = 0")
    db.execute("UPDATE updates SET is_active = 1 WHERE id = ?", (update_id,))
    db.commit()
    row = db.execute("SELECT version FROM updates WHERE id = ?", (update_id,)).fetchone()
    log_action("activate_update", g.web_admin['nickname'], row['version'] if row else str(update_id))
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/updates/<int:update_id>/delete', methods=['POST'])
@web_admin_required
def web_delete_update(update_id):
    db = get_db()
    row = db.execute("SELECT * FROM updates WHERE id = ?", (update_id,)).fetchone()
    if row:
        try:
            os.remove(os.path.join(UPDATES_DIR, row['filename']))
        except OSError:
            pass
        if row['extra_filename']:
            try:
                os.remove(os.path.join(UPDATES_DIR, row['extra_filename']))
            except OSError:
                pass
        db.execute("DELETE FROM updates WHERE id = ?", (update_id,))
        db.commit()
        log_action("delete_update", g.web_admin['nickname'], row['version'])
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/quests', methods=['GET'])
@web_admin_required
def admin_quests_page():
    db = get_db()
    rows = db.execute("SELECT * FROM quests ORDER BY id DESC").fetchall()
    quests = []
    for r in rows:
        r = dict(r)
        r['claims_count'] = db.execute(
            "SELECT COUNT(*) AS c FROM quest_claims WHERE quest_id = ?", (r['id'],)
        ).fetchone()['c']
        quests.append(r)
    templates_json = json.dumps({
        key: {"needs_extra": tpl["needs_extra"], "extra_label": tpl.get("extra_label", ""), "unit": tpl["unit"]}
        for key, tpl in QUEST_TEMPLATES.items()
    })
    return render_template_string(
        QUESTS_TEMPLATE, quests=quests, templates=QUEST_TEMPLATES, templates_json=templates_json
    )

@app.route('/admin/quests/create', methods=['POST'])
@web_admin_required
def admin_quests_create():
    template_id = request.form.get('template_id', '').strip()
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    extra_param = request.form.get('extra_param', '').strip()
    try:
        target_value = float(request.form.get('target_value', '0'))
        reward_coins = float(request.form.get('reward_coins', '0'))
    except ValueError:
        return redirect(url_for('admin_quests_page'))

    if template_id not in QUEST_TEMPLATES or not title or target_value <= 0:
        return redirect(url_for('admin_quests_page'))

    db = get_db()
    db.execute(
        "INSERT INTO quests (template_id, title, description, target_value, extra_param, reward_coins, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (template_id, title, description, target_value, extra_param or None, reward_coins, g.web_admin['nickname'])
    )
    db.commit()
    log_action("quest_create", g.web_admin['nickname'], f"{title} ({template_id}, цель {target_value}, награда {reward_coins})")
    return redirect(url_for('admin_quests_page'))

@app.route('/admin/quests/<int:quest_id>/toggle', methods=['POST'])
@web_admin_required
def admin_quests_toggle(quest_id):
    db = get_db()
    row = db.execute("SELECT is_active, title FROM quests WHERE id = ?", (quest_id,)).fetchone()
    if row:
        new_state = 0 if row['is_active'] else 1
        db.execute("UPDATE quests SET is_active = ? WHERE id = ?", (new_state, quest_id))
        db.commit()
        log_action("quest_toggle", g.web_admin['nickname'], f"{row['title']}: {'вкл' if new_state else 'выкл'}")
    return redirect(url_for('admin_quests_page'))

@app.route('/admin/quests/<int:quest_id>/delete', methods=['POST'])
@web_admin_required
def admin_quests_delete(quest_id):
    db = get_db()
    row = db.execute("SELECT title FROM quests WHERE id = ?", (quest_id,)).fetchone()
    if row:
        db.execute("DELETE FROM quests WHERE id = ?", (quest_id,))
        db.execute("DELETE FROM quest_claims WHERE quest_id = ?", (quest_id,))
        db.commit()
        log_action("quest_delete", g.web_admin['nickname'], row['title'])
    return redirect(url_for('admin_quests_page'))


GIFTS_TEMPLATE = """
<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Mafin Launcher — Подарки</title>
<style>
* {box-sizing:border-box}
body{background:#1a1d23;color:#e1e4e8;font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:0}
header{background:#242830;padding:16px 28px;display:flex;justify-content:space-between;align-items:center;
       border-bottom:1px solid #2d323c}
header h1{font-size:18px;margin:0;color:#43b581}
header a{color:#7a8599;text-decoration:none;font-size:13px;margin-left:14px}
header a:hover{color:#fff}
main{padding:24px 28px;max-width:1050px;margin:0 auto}
section{background:#242830;border-radius:10px;padding:20px;margin-bottom:24px}
section h2{margin-top:0;font-size:15px;color:#43b581}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #2d323c;vertical-align:middle}
th{color:#7a8599;font-weight:600}
tr:hover{background:#2d323c}
.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold}
.badge.on{background:#43b581;color:#fff}
.badge.off{background:#7a8599;color:#fff}
.badge.soldout{background:#f04747;color:#fff}
.actions form{display:inline}
.actions button{background:#2d323c;color:#e1e4e8;border:none;padding:5px 10px;border-radius:5px;
                 font-size:11px;cursor:pointer;margin:1px}
.actions button:hover{background:#3a3f4a}
.actions button.danger:hover{background:#f04747}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
.form-grid label{font-size:12px;color:#7a8599;display:block;margin-top:10px}
input[type=text],input[type=number],input[type=datetime-local]{width:100%;padding:9px;border-radius:6px;
    border:1px solid #2d323c;background:#2d323c;color:#fff;margin-top:4px;font-family:inherit;font-size:13px}
input[type=file]{margin-top:6px}
.submit-row{grid-column:1 / -1;margin-top:16px}
button.primary{background:#43b581;color:#fff;border:none;padding:10px 18px;border-radius:6px;
                     font-weight:bold;cursor:pointer}
button.primary:hover{background:#379768}
.muted{color:#7a8599;font-size:12px}
.err{background:#3a2020;border:1px solid #f04747;color:#ff9b9b;padding:10px 12px;
     border-radius:6px;font-size:13px;white-space:pre-wrap;margin-bottom:14px}
.thumb{width:40px;height:40px;object-fit:cover;border-radius:6px;background:#1a1d23}
.limit-row{display:none}
</style></head>
<body>
<header>
  <h1>🎁 Подарки</h1>
  <div><a href="/admin/dashboard">← Назад в панель</a><a href="/admin/quests">📋 Задания</a><a href="/admin/news">📰 Новости</a><a href="/admin/logout">Выйти</a></div>
</header>
<main>
  <section>
    <h2>➕ Новый подарок</h2>
    {% if upload_error %}<div class="err">{{ upload_error }}</div>{% endif %}
    <form method="post" action="/admin/gifts/create" enctype="multipart/form-data">
      <div class="form-grid">
        <div>
          <label>Название подарка</label>
          <input type="text" name="name" placeholder="Например: Плюшевый мишка" required>
        </div>
        <div>
          <label>Цена, монет</label>
          <input type="number" step="any" min="0" name="price" placeholder="Например 15" required>
        </div>
      </div>
      <div class="form-grid">
        <div>
          <label>Картинка или гифка подарка</label>
          <input type="file" name="image" accept=".gif,.png,.jpg,.jpeg,.webp,.bmp" required>
        </div>
        <div>
          <label>Количество (оставьте пустым - без ограничения)</label>
          <input type="number" step="1" min="0" name="quantity" placeholder="Например 100">
        </div>
      </div>
      <div class="form-grid">
        <div>
          <label style="display:flex;align-items:center;gap:6px;margin-top:14px">
            <input type="checkbox" name="limit_time" value="1" style="width:auto"
                   onchange="document.getElementById('sale_until_row').style.display=this.checked?'block':'none'">
            Ограничить продажу по времени
          </label>
        </div>
      </div>
      <div class="form-grid limit-row" id="sale_until_row">
        <div>
          <label>Продавать до (время Екатеринбурга, UTC+5)</label>
          <input type="datetime-local" name="sale_until">
        </div>
      </div>
      <div class="submit-row"><button type="submit" class="primary">Добавить подарок</button></div>
    </form>
  </section>

  <section>
    <h2>📜 Подарки в магазине</h2>
    <table>
      <tr><th></th><th>Название</th><th>Цена</th><th>Остаток</th><th>Продажа до (ЕКБ)</th><th>Статус</th><th>Действия</th></tr>
      {% for gift in gifts %}
      <tr>
        <td><img class="thumb" src="/api/gifts/image/{{ gift.image_filename }}" alt=""></td>
        <td>{{ gift.name }}</td>
        <td>{{ gift.price }} 🪙</td>
        <td class="muted">{% if gift.quantity is none or gift.quantity < 0 %}∞{% else %}{{ gift.quantity }}{% endif %}</td>
        <td class="muted">{{ gift.sale_until_yekb or '—' }}</td>
        <td>
          {% if not gift.is_active %}<span class="badge off">Выключен</span>
          {% elif gift.quantity is not none and gift.quantity >= 0 and gift.quantity <= 0 %}<span class="badge soldout">Закончился</span>
          {% elif gift.sale_until and gift.sale_until_expired %}<span class="badge soldout">Срок вышел</span>
          {% else %}<span class="badge on">Активен</span>{% endif %}
        </td>
        <td class="actions">
          <form method="post" action="/admin/gifts/{{ gift.id }}/toggle">
            <button type="submit">{% if gift.is_active %}Выключить{% else %}Включить{% endif %}</button>
          </form>
          <form method="post" action="/admin/gifts/{{ gift.id }}/delete" onsubmit="return confirm('Удалить подарок «{{ gift.name }}»?');">
            <button type="submit" class="danger">Удалить</button>
          </form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="7" class="muted">Подарков ещё нет.</td></tr>
      {% endfor %}
    </table>
  </section>
</main>
</body></html>
"""

@app.route('/admin/gifts', methods=['GET'])
@web_admin_required
def admin_gifts_page():
    db = get_db()
    rows = db.execute("SELECT * FROM gifts ORDER BY id DESC").fetchall()
    gifts = []
    for r in rows:
        item = dict(r)
        item['sale_until_yekb'] = system_naive_to_yekb_input(item['sale_until']) if item['sale_until'] else None
        item['sale_until_expired'] = False
        if item['sale_until']:
            try:
                item['sale_until_expired'] = datetime.now() > datetime.fromisoformat(item['sale_until'])
            except Exception:
                pass
        gifts.append(item)
    return render_template_string(GIFTS_TEMPLATE, gifts=gifts, upload_error=None)

@app.route('/admin/gifts/create', methods=['POST'])
@web_admin_required
def admin_gifts_create():
    def fail(msg):
        db = get_db()
        rows = db.execute("SELECT * FROM gifts ORDER BY id DESC").fetchall()
        gifts = []
        for r in rows:
            item = dict(r)
            item['sale_until_yekb'] = system_naive_to_yekb_input(item['sale_until']) if item['sale_until'] else None
            item['sale_until_expired'] = False
            if item['sale_until']:
                try:
                    item['sale_until_expired'] = datetime.now() > datetime.fromisoformat(item['sale_until'])
                except Exception:
                    pass
            gifts.append(item)
        return render_template_string(GIFTS_TEMPLATE, gifts=gifts, upload_error=msg), 400

    name = request.form.get('name', '').strip()
    try:
        price = float(request.form.get('price', ''))
    except ValueError:
        return fail("Укажите корректную цену подарка.")
    if not name or price < 0:
        return fail("Название и цена подарка обязательны, цена не может быть отрицательной.")

    quantity_raw = request.form.get('quantity', '').strip()
    quantity = None
    if quantity_raw:
        try:
            quantity = int(quantity_raw)
        except ValueError:
            return fail("Количество должно быть целым числом (или оставьте поле пустым).")
        if quantity < 0:
            return fail("Количество не может быть отрицательным.")

    sale_until = None
    if request.form.get('limit_time') == '1':
        sale_until_raw = request.form.get('sale_until', '').strip()
        if not sale_until_raw:
            return fail("Укажите дату и время окончания продажи (по Екатеринбургу) или снимите галочку ограничения.")
        try:
            sale_until = yekb_input_to_system_naive(sale_until_raw).isoformat()
        except Exception:
            return fail("Не удалось разобрать дату/время окончания продажи.")

    image_file = request.files.get('image')
    if not image_file or image_file.filename == '':
        return fail("Прикрепите картинку или гифку подарка.")
    ext = os.path.splitext(image_file.filename)[1].lower()
    if ext not in ALLOWED_GIFT_IMAGE_EXT:
        return fail(f"Недопустимый формат картинки. Разрешены: {', '.join(sorted(ALLOWED_GIFT_IMAGE_EXT))}")

    image_file.seek(0, os.SEEK_END)
    size = image_file.tell()
    image_file.seek(0)
    if size > MAX_GIFT_IMAGE_SIZE_BYTES:
        return fail(f"Файл слишком большой (максимум {MAX_GIFT_IMAGE_SIZE_BYTES // (1024*1024)} МБ).")

    stored_filename = f"{secrets.token_hex(8)}{ext}"
    image_file.save(os.path.join(GIFTS_DIR, stored_filename))

    db = get_db()
    db.execute(
        "INSERT INTO gifts (name, price, image_filename, quantity, sale_until, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, price, stored_filename, quantity if quantity is not None else -1, sale_until, g.web_admin['nickname'])
    )
    db.commit()
    log_action("gift_create", g.web_admin['nickname'], f"{name} ({price} монет)")
    return redirect(url_for('admin_gifts_page'))

@app.route('/admin/gifts/<int:gift_id>/toggle', methods=['POST'])
@web_admin_required
def admin_gifts_toggle(gift_id):
    db = get_db()
    row = db.execute("SELECT is_active, name FROM gifts WHERE id = ?", (gift_id,)).fetchone()
    if row:
        new_state = 0 if row['is_active'] else 1
        db.execute("UPDATE gifts SET is_active = ? WHERE id = ?", (new_state, gift_id))
        db.commit()
        log_action("gift_toggle", g.web_admin['nickname'], f"{row['name']}: {'вкл' if new_state else 'выкл'}")
    return redirect(url_for('admin_gifts_page'))

@app.route('/admin/gifts/<int:gift_id>/delete', methods=['POST'])
@web_admin_required
def admin_gifts_delete(gift_id):
    db = get_db()
    row = db.execute("SELECT name, image_filename FROM gifts WHERE id = ?", (gift_id,)).fetchone()
    if row:
        db.execute("DELETE FROM gifts WHERE id = ?", (gift_id,))
        db.commit()
        log_action("gift_delete", g.web_admin['nickname'], row['name'])
    return redirect(url_for('admin_gifts_page'))


NEWS_TEMPLATE = """
<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Mafin Launcher — Новости</title>
<style>
* {box-sizing:border-box}
body{background:#1a1d23;color:#e1e4e8;font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:0}
header{background:#242830;padding:16px 28px;display:flex;justify-content:space-between;align-items:center;
       border-bottom:1px solid #2d323c}
header h1{font-size:18px;margin:0;color:#43b581}
header a{color:#7a8599;text-decoration:none;font-size:13px;margin-left:14px}
header a:hover{color:#fff}
main{padding:24px 28px;max-width:1050px;margin:0 auto}
section{background:#242830;border-radius:10px;padding:20px;margin-bottom:24px}
section h2{margin-top:0;font-size:15px;color:#43b581}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #2d323c;vertical-align:top}
th{color:#7a8599;font-weight:600}
tr:hover{background:#2d323c}
.actions form{display:inline}
.actions button{background:#2d323c;color:#e1e4e8;border:none;padding:5px 10px;border-radius:5px;
                 font-size:11px;cursor:pointer;margin:1px}
.actions button:hover{background:#3a3f4a}
.actions button.danger:hover{background:#f04747}
.form-grid{display:grid;grid-template-columns:1fr;gap:0 16px}
.form-grid label{font-size:12px;color:#7a8599;display:block;margin-top:10px}
input[type=text]{width:100%;padding:9px;border-radius:6px;
    border:1px solid #2d323c;background:#2d323c;color:#fff;margin-top:4px;font-family:inherit;font-size:13px}
textarea{width:100%;padding:9px;border-radius:6px;border:1px solid #2d323c;background:#2d323c;color:#fff;
    margin-top:4px;font-family:inherit;font-size:13px;resize:vertical}
.submit-row{margin-top:16px}
button.primary{background:#43b581;color:#fff;border:none;padding:10px 18px;border-radius:6px;
                     font-weight:bold;cursor:pointer}
button.primary:hover{background:#379768}
.muted{color:#7a8599;font-size:12px}
.body-preview{max-width:420px;white-space:pre-wrap;word-break:break-word}
.edit-form textarea{min-height:90px}
</style></head>
<body>
<header>
  <h1>📰 Новости</h1>
  <div><a href="/admin/dashboard">← Назад в панель</a><a href="/admin/quests">📋 Задания</a><a href="/admin/gifts">🎁 Подарки</a><a href="/admin/logout">Выйти</a></div>
</header>
<main>
  <section>
    <h2>➕ Опубликовать новость</h2>
    <form method="post" action="/admin/news/create">
      <div class="form-grid">
        <div>
          <label>Заголовок</label>
          <input type="text" name="title" placeholder="Например: Обновление 1.2 вышло!" required>
        </div>
        <div>
          <label>Текст новости</label>
          <textarea name="body" rows="4" placeholder="Текст новости для игроков" required></textarea>
        </div>
      </div>
      <div class="submit-row"><button type="submit" class="primary">Опубликовать</button></div>
    </form>
  </section>

  <section>
    <h2>📜 Опубликованные новости</h2>
    <table>
      <tr><th>Заголовок</th><th>Текст</th><th>Автор</th><th>Дата</th><th>Изменена</th><th>Действия</th></tr>
      {% for n in news %}
      <tr id="row-{{ n.id }}">
        <td>{{ n.title }}</td>
        <td class="body-preview">{{ n.body }}</td>
        <td class="muted">{{ n.author or '—' }}</td>
        <td class="muted">{{ n.created_at }}</td>
        <td class="muted">{% if n.edited %}✅ {{ n.edited_at or '' }}{% else %}—{% endif %}</td>
        <td class="actions">
          <button type="button" onclick="toggleEdit({{ n.id }})">✏️ Редактировать</button>
          <form method="post" action="/admin/news/{{ n.id }}/delete" onsubmit="return confirm('Удалить новость «{{ n.title }}»?');">
            <button type="submit" class="danger">Удалить</button>
          </form>
        </td>
      </tr>
      <tr id="edit-row-{{ n.id }}" style="display:none">
        <td colspan="6">
          <form class="edit-form" method="post" action="/admin/news/{{ n.id }}/edit">
            <div class="form-grid">
              <div>
                <label>Заголовок</label>
                <input type="text" name="title" value="{{ n.title }}" required>
              </div>
              <div>
                <label>Текст новости</label>
                <textarea name="body" rows="4" required>{{ n.body }}</textarea>
              </div>
            </div>
            <div class="submit-row">
              <button type="submit" class="primary">💾 Сохранить</button>
              <button type="button" onclick="toggleEdit({{ n.id }})">Отмена</button>
            </div>
          </form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="6" class="muted">Новостей ещё нет.</td></tr>
      {% endfor %}
    </table>
  </section>
</main>
<script>
function toggleEdit(id){
  const row = document.getElementById('edit-row-' + id);
  row.style.display = (row.style.display === 'none') ? 'table-row' : 'none';
}
</script>
</body></html>
"""

@app.route('/admin/news', methods=['GET'])
@web_admin_required
def admin_news_page():
    db = get_db()
    rows = db.execute(
        "SELECT id, title, body, author, created_at, edited, edited_at FROM news ORDER BY id DESC"
    ).fetchall()
    return render_template_string(NEWS_TEMPLATE, news=[dict(r) for r in rows])

@app.route('/admin/news/create', methods=['POST'])
@web_admin_required
def admin_news_create():
    title = request.form.get('title', '').strip()
    body = request.form.get('body', '').strip()
    if title and body:
        db = get_db()
        db.execute(
            "INSERT INTO news (title, body, author) VALUES (?, ?, ?)",
            (title, body, g.web_admin['nickname'])
        )
        db.commit()
        log_action("news_publish", g.web_admin['nickname'], title)
    return redirect(url_for('admin_news_page'))

@app.route('/admin/news/<int:news_id>/edit', methods=['POST'])
@web_admin_required
def admin_news_edit(news_id):
    title = request.form.get('title', '').strip()
    body = request.form.get('body', '').strip()
    if title and body:
        db = get_db()
        row = db.execute("SELECT id FROM news WHERE id = ?", (news_id,)).fetchone()
        if row:
            db.execute(
                "UPDATE news SET title = ?, body = ?, edited = 1, edited_at = ? WHERE id = ?",
                (title, body, datetime.now().isoformat(timespec="seconds"), news_id)
            )
            db.commit()
            log_action("news_edit", g.web_admin['nickname'], title)
    return redirect(url_for('admin_news_page'))

@app.route('/admin/news/<int:news_id>/delete', methods=['POST'])
@web_admin_required
def admin_news_delete(news_id):
    db = get_db()
    row = db.execute("SELECT title FROM news WHERE id = ?", (news_id,)).fetchone()
    if row:
        db.execute("DELETE FROM news WHERE id = ?", (news_id,))
        db.commit()
        log_action("news_delete", g.web_admin['nickname'], row['title'])
    return redirect(url_for('admin_news_page'))


_SHA256_CACHE = {}


def _file_sha256(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    cached = _SHA256_CACHE.get(key)
    if cached:
        return cached
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    _SHA256_CACHE.clear()
    _SHA256_CACHE[key] = h.hexdigest()
    return _SHA256_CACHE[key]


@app.route('/api/updates/latest', methods=['GET'])
def api_latest_update():
    db = get_db()
    row = db.execute(
        "SELECT * FROM updates WHERE is_active = 1 ORDER BY uploaded_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return jsonify({"available": False})
    result = {
        "available": True,
        "version": row["version"],
        "changelog": row["changelog"],
        "filename": row["filename"],
        "file_size": row["file_size"],
        "download_url": f"/api/updates/download/{row['filename']}"
    }
    main_hash = _file_sha256(os.path.join(UPDATES_DIR, os.path.basename(row["filename"])))
    if main_hash:
        result["sha256"] = main_hash
    if row["extra_filename"]:
        result["extra_filename"] = row["extra_filename"]
        result["extra_file_size"] = row["extra_file_size"]
        result["extra_download_url"] = f"/api/updates/download/{row['extra_filename']}"
        extra_hash = _file_sha256(os.path.join(UPDATES_DIR, os.path.basename(row["extra_filename"])))
        if extra_hash:
            result["extra_sha256"] = extra_hash
    return jsonify(result)


@app.route('/api/updates/download/<path:filename>', methods=['GET'])
def api_download_update(filename):
    safe_name = secure_filename(filename)
    path = os.path.join(UPDATES_DIR, safe_name)
    if not os.path.exists(path):
        return jsonify({"error": "Файл не найден"}), 404
    return send_from_directory(UPDATES_DIR, safe_name, as_attachment=True)


@app.route('/api/admin/profiles', methods=['GET'])
@admin_required
def get_all_profiles():
    db = get_db()
    rows = db.execute(
        "SELECT nickname, is_admin, is_banned, ban_reason, coins, total_playtime_minutes, total_launches, created_at, last_login, last_seen FROM profiles ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["is_online"] = is_online(d.get("last_seen"))
        result.append(d)
    return jsonify(result)

@app.route('/api/admin/ban', methods=['POST'])
@admin_required
def ban_profile():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    
    nickname = data.get('nickname', '').strip()
    reason = data.get('reason', 'Нарушение правил')
    
    if not nickname:
        return jsonify({"error": "nickname обязателен"}), 400
    
    if nickname == g.current_profile['nickname']:
        return jsonify({"error": "Нельзя забанить себя"}), 400
    
    db = get_db()
    profile = db.execute("SELECT id FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not profile:
        return jsonify({"error": "Профиль не найден"}), 404
    
    db.execute(
        "UPDATE profiles SET is_banned = 1, ban_reason = ?, token = NULL WHERE nickname = ?",
        (reason, nickname)
    )
    db.commit()
    
    log_action("ban", g.current_profile['nickname'], f"{nickname}: {reason}")
    
    return jsonify({"success": True})

@app.route('/api/admin/unban', methods=['POST'])
@admin_required
def unban_profile():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    
    nickname = data.get('nickname', '').strip()
    
    db = get_db()
    db.execute(
        "UPDATE profiles SET is_banned = 0, ban_reason = NULL WHERE nickname = ?",
        (nickname,)
    )
    db.commit()
    
    log_action("unban", g.current_profile['nickname'], nickname)
    
    return jsonify({"success": True})

@app.route('/api/admin/make_admin', methods=['POST'])
@admin_required
def make_admin():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    
    nickname = data.get('nickname', '').strip()
    
    db = get_db()
    db.execute("UPDATE profiles SET is_admin = 1 WHERE nickname = ?", (nickname,))
    db.commit()
    
    log_action("make_admin", g.current_profile['nickname'], nickname)
    
    return jsonify({"success": True})

@app.route('/api/admin/remove_admin', methods=['POST'])
@admin_required
def remove_admin():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Неверный формат"}), 400
    
    nickname = data.get('nickname', '').strip()
    
    if nickname == g.current_profile['nickname']:
        return jsonify({"error": "Нельзя снять права с себя"}), 400
    
    db = get_db()
    db.execute("UPDATE profiles SET is_admin = 0 WHERE nickname = ?", (nickname,))
    db.commit()
    
    log_action("remove_admin", g.current_profile['nickname'], nickname)
    
    return jsonify({"success": True})

@app.route('/api/admin/logs', methods=['GET'])
@admin_required
def get_logs():
    limit = request.args.get('limit', 100, type=int)
    db = get_db()
    rows = db.execute(
        "SELECT * FROM server_logs ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/stats', methods=['GET'])
@admin_required
def get_server_stats():
    db = get_db()
    total_profiles = db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    total_banned = db.execute("SELECT COUNT(*) FROM profiles WHERE is_banned = 1").fetchone()[0]
    total_admins = db.execute("SELECT COUNT(*) FROM profiles WHERE is_admin = 1").fetchone()[0]
    total_playtime = db.execute("SELECT SUM(total_playtime_minutes) FROM profiles").fetchone()[0] or 0
    total_coins = db.execute("SELECT SUM(coins) FROM profiles").fetchone()[0] or 0
    
    return jsonify({
        "total_profiles": total_profiles,
        "total_banned": total_banned,
        "total_admins": total_admins,
        "total_playtime_minutes": total_playtime,
        "total_coins": total_coins
    })


def _quest_progress_playtime_total(db, nickname, quest):
    row = db.execute("SELECT total_playtime_minutes FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    return (row['total_playtime_minutes'] or 0) if row else 0

def _quest_progress_playtime_single_session(db, nickname, quest):
    row = db.execute(
        "SELECT MAX(duration_minutes) AS m FROM play_sessions WHERE nickname = ?", (nickname,)
    ).fetchone()
    return (row['m'] or 0) if row else 0

def _quest_progress_launches_total(db, nickname, quest):
    row = db.execute("SELECT total_launches FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    return (row['total_launches'] or 0) if row else 0

def _quest_progress_coins_balance(db, nickname, quest):
    row = db.execute("SELECT coins FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    return (row['coins'] or 0) if row else 0

def _quest_progress_versions_variety(db, nickname, quest):
    row = db.execute(
        "SELECT COUNT(DISTINCT version) AS c FROM play_sessions WHERE nickname = ?", (nickname,)
    ).fetchone()
    return (row['c'] or 0) if row else 0

def _quest_progress_specific_version_minutes(db, nickname, quest):
    version = (quest.get('extra_param') or '').strip()
    if not version:
        return 0
    row = db.execute(
        "SELECT SUM(duration_minutes) AS s FROM play_sessions WHERE nickname = ? AND version = ?",
        (nickname, version)
    ).fetchone()
    return (row['s'] or 0) if row else 0

def _quest_progress_friends_count(db, nickname, quest):
    row = db.execute(
        "SELECT COUNT(*) AS c FROM friendships WHERE status = 'accepted' AND (requester = ? OR addressee = ?)",
        (nickname, nickname)
    ).fetchone()
    return (row['c'] or 0) if row else 0

def _quest_progress_friend_requests_sent(db, nickname, quest):
    row = db.execute("SELECT COUNT(*) AS c FROM friendships WHERE requester = ?", (nickname,)).fetchone()
    return (row['c'] or 0) if row else 0

def _quest_progress_messages_sent(db, nickname, quest):
    row = db.execute("SELECT COUNT(*) AS c FROM messages WHERE sender = ?", (nickname,)).fetchone()
    return (row['c'] or 0) if row else 0

def _quest_progress_messages_to_one(db, nickname, quest):
    target_nick = (quest.get('extra_param') or '').strip()
    if not target_nick:
        return 0
    row = db.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE sender = ? AND receiver = ?", (nickname, target_nick)
    ).fetchone()
    return (row['c'] or 0) if row else 0

def _quest_progress_achievements_unlocked(db, nickname, quest):
    row = db.execute("SELECT COUNT(*) AS c FROM achievements WHERE nickname = ?", (nickname,)).fetchone()
    return (row['c'] or 0) if row else 0

def _quest_progress_login_streak(db, nickname, quest):
    rows = db.execute(
        "SELECT day FROM login_days WHERE nickname = ? ORDER BY day DESC", (nickname,)
    ).fetchall()
    days = [r['day'] for r in rows]
    if not days:
        return 0
    streak = 0
    cursor = datetime.now().date()
    day_set = set(days)
    while cursor.strftime("%Y-%m-%d") in day_set:
        streak += 1
        cursor = cursor - timedelta(days=1)
    return streak

def _quest_progress_active_today(db, nickname, quest):
    today = datetime.now().strftime("%Y-%m-%d")
    row = db.execute(
        "SELECT 1 FROM login_days WHERE nickname = ? AND day = ?", (nickname, today)
    ).fetchone()
    return 1 if row else 0

def _quest_progress_total_active_days(db, nickname, quest):
    row = db.execute("SELECT COUNT(*) AS c FROM login_days WHERE nickname = ?", (nickname,)).fetchone()
    return (row['c'] or 0) if row else 0

def _quest_progress_top_rank(db, nickname, quest):
    rows = db.execute("SELECT nickname FROM profiles ORDER BY coins DESC").fetchall()
    for i, r in enumerate(rows, start=1):
        if r['nickname'] == nickname:
            return 1 if i <= quest['target_value'] else 0
    return 0

def _quest_progress_no_bans(db, nickname, quest):
    row = db.execute("SELECT is_banned FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    return 1 if (row and not row['is_banned']) else 0

def _quest_progress_account_age_days(db, nickname, quest):
    row = db.execute("SELECT created_at FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not row or not row['created_at']:
        return 0
    try:
        created = datetime.fromisoformat(row['created_at'])
        return (datetime.now() - created).total_seconds() / 86400.0
    except Exception:
        return 0

def _quest_progress_manual(db, nickname, quest):
    return 0

QUEST_TEMPLATES = {
    "playtime_total": {
        "title": "Наиграть {target} мин. суммарно", "unit": "минут",
        "needs_extra": False, "fn": _quest_progress_playtime_total,
    },
    "playtime_single_session": {
        "title": "Отыграть одну сессию от {target} мин.", "unit": "минут",
        "needs_extra": False, "fn": _quest_progress_playtime_single_session,
    },
    "launches_total": {
        "title": "Запустить лаунчер {target} раз", "unit": "раз",
        "needs_extra": False, "fn": _quest_progress_launches_total,
    },
    "coins_balance": {
        "title": "Накопить {target} монет на балансе", "unit": "монет",
        "needs_extra": False, "fn": _quest_progress_coins_balance,
    },
    "versions_variety": {
        "title": "Поиграть в {target} разных версий", "unit": "версий",
        "needs_extra": False, "fn": _quest_progress_versions_variety,
    },
    "specific_version_minutes": {
        "title": "Наиграть {target} мин. в версии «{extra}»", "unit": "минут",
        "needs_extra": True, "extra_label": "Версия (например 1.20.1)", "fn": _quest_progress_specific_version_minutes,
    },
    "friends_count": {
        "title": "Иметь {target} друзей", "unit": "друзей",
        "needs_extra": False, "fn": _quest_progress_friends_count,
    },
    "friend_requests_sent": {
        "title": "Отправить {target} заявок в друзья", "unit": "заявок",
        "needs_extra": False, "fn": _quest_progress_friend_requests_sent,
    },
    "messages_sent": {
        "title": "Отправить {target} сообщений", "unit": "сообщений",
        "needs_extra": False, "fn": _quest_progress_messages_sent,
    },
    "messages_to_one": {
        "title": "Написать {target} сообщений игроку «{extra}»", "unit": "сообщений",
        "needs_extra": True, "extra_label": "Ник получателя", "fn": _quest_progress_messages_to_one,
    },
    "achievements_unlocked": {
        "title": "Разблокировать {target} достижений", "unit": "достижений",
        "needs_extra": False, "fn": _quest_progress_achievements_unlocked,
    },
    "login_streak": {
        "title": "Заходить {target} дней подряд", "unit": "дней подряд",
        "needs_extra": False, "fn": _quest_progress_login_streak,
    },
    "active_today": {
        "title": "Зайти в лаунчер сегодня", "unit": "",
        "needs_extra": False, "fn": _quest_progress_active_today,
    },
    "total_active_days": {
        "title": "Быть активным {target} дней всего", "unit": "дней",
        "needs_extra": False, "fn": _quest_progress_total_active_days,
    },
    "top_rank": {
        "title": "Войти в топ-{target} игроков по монетам", "unit": "",
        "needs_extra": False, "fn": _quest_progress_top_rank,
    },
    "no_bans": {
        "title": "Не иметь банов на аккаунте", "unit": "",
        "needs_extra": False, "fn": _quest_progress_no_bans,
    },
    "account_age_days": {
        "title": "Иметь аккаунт старше {target} дней", "unit": "дней",
        "needs_extra": False, "fn": _quest_progress_account_age_days,
    },
    "manual": {
        "title": "{extra}", "unit": "",
        "needs_extra": True, "extra_label": "Описание задания (награда выдаётся вручную админом)",
        "fn": _quest_progress_manual,
    },
}

def compute_quest_progress(db, nickname, quest):
    template = QUEST_TEMPLATES.get(quest['template_id'])
    if not template:
        return 0
    try:
        return template['fn'](db, nickname, quest)
    except Exception as e:
        print(f"Ошибка расчёта прогресса задания {quest['id']} ({quest['template_id']}) для {nickname}: {e}")
        return 0

@app.route('/api/quests', methods=['GET'])
@auth_required
def list_quests():
    db = get_db()
    nickname = g.current_profile['nickname']
    quests = db.execute("SELECT * FROM quests WHERE is_active = 1 ORDER BY id DESC").fetchall()
    claimed_ids = {r['quest_id'] for r in db.execute(
        "SELECT quest_id FROM quest_claims WHERE nickname = ?", (nickname,)
    ).fetchall()}
    result = []
    for q in quests:
        q = dict(q)
        progress = compute_quest_progress(db, nickname, q)
        result.append({
            "id": q['id'], "title": q['title'], "description": q['description'],
            "target_value": q['target_value'], "reward_coins": q['reward_coins'],
            "progress": progress, "claimed": q['id'] in claimed_ids,
            "completed": progress >= q['target_value'],
        })
    return jsonify(result)

@app.route('/api/quests/claim', methods=['POST'])
@auth_required
def claim_quest():
    data = request.get_json(force=True, silent=True) or {}
    quest_id = data.get('quest_id')
    nickname = g.current_profile['nickname']
    db = get_db()
    quest = db.execute("SELECT * FROM quests WHERE id = ? AND is_active = 1", (quest_id,)).fetchone()
    if not quest:
        return jsonify({"error": "Задание не найдено"}), 404
    quest = dict(quest)
    already = db.execute(
        "SELECT 1 FROM quest_claims WHERE quest_id = ? AND nickname = ?", (quest_id, nickname)
    ).fetchone()
    if already:
        return jsonify({"error": "Награда уже получена"}), 409
    progress = compute_quest_progress(db, nickname, quest)
    if progress < quest['target_value']:
        return jsonify({"error": "Задание ещё не выполнено", "progress": progress,
                         "target": quest['target_value']}), 400
    try:
        db.execute("INSERT INTO quest_claims (quest_id, nickname) VALUES (?, ?)", (quest_id, nickname))
        db.execute("UPDATE profiles SET coins = coins + ? WHERE nickname = ?", (quest['reward_coins'], nickname))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Награда уже получена"}), 409
    log_action("quest_claim", nickname, f"quest #{quest_id} ({quest['template_id']}): +{quest['reward_coins']} монет")
    new_coins = db.execute("SELECT coins FROM profiles WHERE nickname = ?", (nickname,)).fetchone()['coins']
    return jsonify({"success": True, "reward_coins": quest['reward_coins'], "total_coins": new_coins})

def _cli_admin_code(nickname, create=False):
    init_db()
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    profile = db.execute("SELECT * FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    if not profile:
        if not create:
            sys.exit(f"Профиля «{nickname}» нет. Добавь --create, чтобы создать нового админа.")
        db.execute(
            "INSERT INTO profiles (nickname, password_hash, salt, is_admin) VALUES (?, '!', ?, 1)",
            (nickname, secrets.token_hex(16))
        )
        db.commit()
        print(f"Создан админ-профиль {nickname} без пароля. Войди в /admin по коду и задай пароль для лаунчера там.")
    elif not profile['is_admin']:
        if not create:
            sys.exit(f"«{nickname}» не админ. Добавь --create, чтобы выдать права.")
        db.execute("UPDATE profiles SET is_admin = 1 WHERE nickname = ?", (nickname,))
        db.commit()
        print(f"Выданы права админа: {nickname}")
    code = generate_admin_code(db, nickname)
    db.close()
    print(f"\nКод для входа в /admin: {code}")
    print(f"Действует {ADMIN_CODE_TTL_SECONDS // 60} мин., одноразовый. После входа сессия живёт {ADMIN_SESSION_DAYS} дней.")

def _cli_set_password(nickname):
    import getpass
    init_db()
    db = sqlite3.connect(DB_PATH)
    if not db.execute("SELECT 1 FROM profiles WHERE nickname = ?", (nickname,)).fetchone():
        sys.exit(f"Профиля «{nickname}» нет.")
    pw = getpass.getpass("Новый пароль для лаунчера: ")
    if len(pw) < 12:
        sys.exit("Пароль короче 12 символов, отменено.")
    if pw != getpass.getpass("Ещё раз: "):
        sys.exit("Пароли не совпали, отменено.")
    salt = secrets.token_hex(16)
    db.execute(
        "UPDATE profiles SET password_hash = ?, salt = ?, token = NULL WHERE nickname = ?",
        (hash_password(pw, salt), salt, nickname)
    )
    db.commit()
    db.close()
    print("Пароль изменён, старые токены лаунчера сброшены (нужно залогиниться заново).")

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == "set-password":
        _cli_set_password(sys.argv[2])
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "admin-code":
        _cli_admin_code(sys.argv[2], create="--create" in sys.argv[3:])
        sys.exit(0)
    init_db()
    print("=" * 60)
    print("🚀 Mafin Launcher Server запущен")
    print("📡 API адрес: http://0.0.0.0:10074")
    print("🛡️  Веб админ-панель: http://localhost:10074/admin")
    print("🔑 Вход в админку: python app.py admin-code НИК")
    print("=" * 60)
    app.run(host=os.environ.get("MAFIN_HOST", "0.0.0.0"), port=int(os.environ.get("MAFIN_PORT", "10074")), debug=False)
