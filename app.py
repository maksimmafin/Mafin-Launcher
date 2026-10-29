import os
import sys
import json
import sqlite3
import hashlib
import secrets
import time
import threading
import re
import socket
import struct
import signal
import zipfile
import shutil
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, g, session, redirect, url_for, render_template_string, send_from_directory, has_app_context
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
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=30000")
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

        CREATE TABLE IF NOT EXISTS browser_sync (
            nickname TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at INTEGER NOT NULL
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

        CREATE TABLE IF NOT EXISTS skins (
            game_name_lower TEXT PRIMARY KEY,
            game_name TEXT NOT NULL,
            uuid TEXT NOT NULL,
            owner TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT 'default',
            hash TEXT NOT NULL,
            png BLOB NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_skins_uuid ON skins(uuid);

        CREATE TABLE IF NOT EXISTS shop_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            group_name TEXT NOT NULL,
            duration_days INTEGER NOT NULL DEFAULT 0,
            price REAL NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS shop_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            game_name TEXT NOT NULL,
            item_id INTEGER,
            item_title TEXT NOT NULL,
            group_name TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            price REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            applied_at TEXT
        );

        CREATE TABLE IF NOT EXISTS mc_sessions (
            game_name_lower TEXT PRIMARY KEY,
            nick TEXT NOT NULL,
            ip TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            nickname TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_nick ON auth_sessions(nickname);
        -- сброс токена (бан, смена пароля, «сбросить сессию» в админке) и удаление профиля закрывают все сессии
        CREATE TRIGGER IF NOT EXISTS trg_auth_sessions_reset AFTER UPDATE OF token ON profiles
        WHEN NEW.token IS NULL
        BEGIN DELETE FROM auth_sessions WHERE nickname = OLD.nickname; END;
        CREATE TRIGGER IF NOT EXISTS trg_auth_sessions_del AFTER DELETE ON profiles
        BEGIN DELETE FROM auth_sessions WHERE nickname = OLD.nickname; END;

        CREATE TABLE IF NOT EXISTS cosmetics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            material TEXT NOT NULL DEFAULT 'CARVED_PUMPKIN',
            texture TEXT DEFAULT '',
            scale REAL NOT NULL DEFAULT 0.7,
            y_offset REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS cosmetic_owned (
            nickname TEXT NOT NULL,
            cosmetic_id INTEGER NOT NULL,
            price_paid REAL NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (nickname, cosmetic_id)
        );

        CREATE TABLE IF NOT EXISTS cosmetic_equipped (
            nickname TEXT PRIMARY KEY,
            cosmetic_id INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS weekly_awards (
            week_start TEXT NOT NULL,
            rank INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            minutes REAL NOT NULL,
            coins REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (week_start, rank)
        );
    """)
    
    try:
        db.execute("ALTER TABLE cosmetics ADD COLUMN model TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    for _col in ("y_adj", "scale_adj"):
        try:
            db.execute(f"ALTER TABLE cosmetic_equipped ADD COLUMN {_col} REAL NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    db.commit()
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
    own = None
    try:
        if has_app_context():
            db = get_db()
        else:
            db = own = sqlite3.connect(DB_PATH, timeout=30)
        db.execute(
            "INSERT INTO server_logs (action, nickname, details) VALUES (?, ?, ?)",
            (action, nickname, details)
        )
        db.commit()
    except Exception as e:
        print(f"Ошибка логирования: {e}")
    finally:
        if own is not None:
            own.close()

def _profile_by_token(db, token):
    """Профиль по токену: сначала таблица сессий (несколько устройств), затем старый profiles.token."""
    if not token:
        return None
    row = db.execute(
        "SELECT p.* FROM auth_sessions s JOIN profiles p ON p.nickname = s.nickname WHERE s.token = ?",
        (token,)).fetchone()
    if row:
        return row
    return db.execute("SELECT * FROM profiles WHERE token = ?", (token,)).fetchone()

def _add_session(db, nickname, token):
    db.execute("INSERT OR REPLACE INTO auth_sessions (token, nickname) VALUES (?, ?)", (token, nickname))
    db.execute(
        "DELETE FROM auth_sessions WHERE nickname = ? AND token NOT IN "
        "(SELECT token FROM auth_sessions WHERE nickname = ? ORDER BY created_at DESC, rowid DESC LIMIT 30)",
        (nickname, nickname))

def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({"error": "Требуется авторизация"}), 401
        
        db = get_db()
        profile = _profile_by_token(db, token)
        
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
    _add_session(db, nickname, token)
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

    lock_key = f"{nickname.lower()}|{_client_ip()}"
    lockout_remaining = _check_login_lockout(lock_key)
    if lockout_remaining > 0:
        return jsonify({
            "error": f"Слишком много неудачных попыток. Повторите через {int(lockout_remaining)} сек."
        }), 429
    
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
    
    if not profile:
        _register_login_failure(lock_key)
        return jsonify({"error": "Неверный ник или пароль"}), 404
    
    if profile['is_banned']:
        return jsonify({"error": f"Профиль заблокирован: {profile['ban_reason']}"}), 403
    
    ok, needs_upgrade = verify_password(password, profile['salt'], profile['password_hash'])
    if not ok:
        _register_login_failure(lock_key)
        return jsonify({"error": "Неверный ник или пароль"}), 401

    _clear_login_failures(lock_key)

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
    _add_session(db, profile['nickname'], token)
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
        viewer_row = _profile_by_token(db, token)
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

@app.route('/api/me', methods=['GET'])
@auth_required
def get_me():
    profile = dict(g.current_profile)
    for secret in ("password_hash", "salt", "token"):
        profile.pop(secret, None)
    profile["is_online"] = True
    return jsonify(profile)

@app.route('/api/logout', methods=['POST'])
@auth_required
def logout():
    db = get_db()
    nickname = g.current_profile['nickname']
    cur_token = request.headers.get('Authorization', '').replace('Bearer ', '')
    db.execute("DELETE FROM auth_sessions WHERE token = ?", (cur_token,))
    remaining = db.execute(
        "SELECT token FROM auth_sessions WHERE nickname = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (nickname,)).fetchone()
    db.execute(
        "UPDATE profiles SET token = ?, last_seen = CASE WHEN ? IS NULL THEN NULL ELSE last_seen END WHERE id = ?",
        (remaining['token'] if remaining else None, remaining['token'] if remaining else None,
         g.current_profile['id'])
    )
    db.commit()
    log_action("logout", nickname)
    return jsonify({"success": True})

@app.route('/api/heartbeat', methods=['POST'])
@auth_required
def heartbeat():
    db = get_db()
    was_online = is_online(g.current_profile['last_seen'])
    touch_last_seen(db, g.current_profile['nickname'])
    if not was_online:
        _tg_notify_friends_online(g.current_profile['nickname'])
    return jsonify({"success": True})

def _load_mc_secret():
    """Секрет для плагина MafinAuth: из MAFIN_MC_SECRET или из файла .mc_secret.
    Если ни того ни другого нет, создаётся случайный и сохраняется в .mc_secret."""
    env = os.environ.get("MAFIN_MC_SECRET", "").strip()
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mc_secret")
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value + "\n")
    except OSError as e:
        print(f"Не удалось сохранить .mc_secret: {e}. Секрет будет действовать до перезапуска.")
    return value

MC_SECRET = _load_mc_secret()
_mc_sessions = {}
_mc_sessions_lock = threading.Lock()

def _mc_sessions_load():
    """Восстанавливает игровые сессии из БД после перезапуска сервера."""
    try:
        db = sqlite3.connect(DB_PATH, timeout=30)
        try:
            db.execute("ALTER TABLE mc_sessions ADD COLUMN platform TEXT DEFAULT 'pc'")
            db.commit()
        except sqlite3.Error:
            pass
        rows = db.execute("SELECT game_name_lower, nick, ip, COALESCE(platform, 'pc') FROM mc_sessions").fetchall()
        db.close()
    except sqlite3.Error as e:
        print(f"Не удалось загрузить mc_sessions: {e}")
        return
    with _mc_sessions_lock:
        for key, nick, ip, platform in rows:
            _mc_sessions[key] = {"nick": nick, "ip": ip or "", "platform": platform or "pc"}

def _client_ip():
    """IP клиента. За обратным прокси задайте MAFIN_TRUST_PROXY=1 — тогда берётся X-Forwarded-For."""
    xff = request.headers.get("X-Forwarded-For", "")
    real = request.headers.get("X-Real-IP", "")
    local = _normalize_ip(request.remote_addr) in ("127.0.0.1", "::1", "localhost")
    if xff and (os.environ.get("MAFIN_TRUST_PROXY") == "1" or local):
        return _normalize_ip(xff.split(",")[0])
    if real and local:
        return _normalize_ip(real)
    return _normalize_ip(request.remote_addr)

def _normalize_ip(ip):
    ip = (ip or "").strip()
    if ip.lower().startswith("::ffff:"):
        ip = ip[7:]
    return ip

@app.route('/api/mc/session', methods=['POST'])
@auth_required
def mc_register_session():
    """Лаунчер сообщает, под каким игровым ником аккаунт собирается играть."""
    data = request.get_json(silent=True) or {}
    game_name = str(data.get('game_name', '')).strip()
    if not (3 <= len(game_name) <= 16) or not game_name.isascii() or not game_name.replace('_', '').isalnum():
        return jsonify({"error": "Некорректный игровой ник"}), 400

    me = g.current_profile['nickname']
    key = game_name.lower()
    db = get_db()
    with _mc_sessions_lock:
        cur = _mc_sessions.get(key)
        if cur and cur['nick'] != me:
            other = db.execute(
                "SELECT last_seen, is_banned FROM profiles WHERE nickname = ?", (cur['nick'],)
            ).fetchone()
            if other and not other['is_banned'] and is_online(other['last_seen']):
                return jsonify({"error": "Этот игровой ник сейчас используется другим аккаунтом"}), 409
        for k in [k for k, v in _mc_sessions.items() if v['nick'] == me and k != key]:
            del _mc_sessions[k]
        ip = _client_ip()
        platform = str(data.get('platform', '')).strip().lower()
        if platform not in ('pc', 'mobile'):
            platform = str(request.headers.get('X-Mafin-Platform', '')).strip().lower()
        if platform not in ('pc', 'mobile'):
            platform = 'mobile' if 'MafinLauncher-Android' in (request.headers.get('User-Agent') or '') else 'pc'
        print(f"[mc] сессия: ник={game_name} платформа={platform} UA={(request.headers.get('User-Agent') or '')[:60]}", flush=True)
        _mc_sessions[key] = {"nick": me, "ip": ip, "platform": platform}
        try:
            db.execute("DELETE FROM mc_sessions WHERE nick = ? AND game_name_lower != ?", (me, key))
            db.execute("INSERT OR REPLACE INTO mc_sessions (game_name_lower, nick, ip, updated_at, platform) "
                       "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)", (key, me, ip, platform))
            db.commit()
        except sqlite3.Error as e:
            print(f"mc_sessions persist error: {e}")
    return jsonify({"success": True})

def _mc_access(db, name, ip):
    """Единая проверка входа на сервер. Возвращает (разрешено, причина)."""
    with _mc_sessions_lock:
        sess = _mc_sessions.get(str(name).strip().lower())
    if not sess:
        return False, "not_logged_in"
    profile = db.execute(
        "SELECT last_seen, is_banned FROM profiles WHERE nickname = ?", (sess['nick'],)
    ).fetchone()
    if not profile:
        return False, "not_logged_in"
    if profile['is_banned']:
        return False, "banned"
    if not is_online(profile['last_seen']):
        return False, "offline"
    if os.environ.get("MAFIN_MC_IP_CHECK", "1") != "0" and sess['ip'] != _normalize_ip(ip):
        print(f"[mc] ip_mismatch: ник={name} ip лаунчера={sess['ip']} ip в игре={_normalize_ip(ip)}", flush=True)
        return False, "ip_mismatch"
    return True, None

@app.route('/api/mc/check', methods=['POST'])
def mc_check():
    """Вызывается плагином MafinAuth при входе игрока на Minecraft-сервер."""
    if not MC_SECRET:
        return jsonify({"allowed": False, "reason": "disabled"}), 503
    given = request.headers.get('X-Mc-Secret', '')
    if not secrets.compare_digest(given.encode('utf-8'), MC_SECRET.encode('utf-8')):
        return jsonify({"allowed": False, "reason": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    ok, reason = _mc_access(get_db(), str(data.get('name', '')), data.get('ip', ''))
    if ok:
        with _mc_sessions_lock:
            sess = _mc_sessions.get(str(data.get('name', '')).strip().lower()) or {}
        return jsonify({"allowed": True, "platform": sess.get("platform", "pc")})
    return jsonify({"allowed": False, "reason": reason})

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


@app.route('/api/messages/unread_summary', methods=['GET'])
@auth_required
def unread_summary():
    db = get_db()
    rows = db.execute(
        "SELECT sender, COUNT(*) AS c, MAX(id) AS last_id FROM messages "
        "WHERE receiver = ? AND is_read = 0 GROUP BY sender ORDER BY last_id DESC LIMIT 20",
        (g.current_profile['nickname'],)
    ).fetchall()
    items = [{"sender": r[0], "count": r[1], "last_id": r[2]} for r in rows]
    return jsonify({"senders": items, "count": sum(i["count"] for i in items)})


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


@app.route('/api/browser/sync', methods=['GET'])
@auth_required
def browser_sync_get():
    row = get_db().execute("SELECT data, updated_at FROM browser_sync WHERE nickname = ?",
                           (g.current_profile['nickname'],)).fetchone()
    if not row:
        return jsonify({"data": None, "updated_at": 0})
    return jsonify({"data": json.loads(row['data']), "updated_at": row['updated_at']})


@app.route('/api/browser/sync', methods=['PUT'])
@auth_required
def browser_sync_put():
    raw = request.get_data()
    if len(raw) > 2 * 1024 * 1024:
        return jsonify({"error": "Слишком большой объём данных"}), 413
    try:
        body = json.loads(raw)
        data = body["data"]
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        return jsonify({"error": "Неверный формат"}), 400
    now = int(time.time())
    db = get_db()
    db.execute("INSERT INTO browser_sync (nickname, data, updated_at) VALUES (?, ?, ?) "
               "ON CONFLICT(nickname) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
               (g.current_profile['nickname'], json.dumps(data, ensure_ascii=False), now))
    db.commit()
    return jsonify({"success": True, "updated_at": now})


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
    _tg_broadcast_news(title, body)
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
    db.execute("DELETE FROM mc_sessions WHERE nick = ?", (nickname,))
    try:
        db.execute("DELETE FROM tg_links WHERE nickname = ?", (nickname,))
    except sqlite3.OperationalError:
        pass
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
  <div>Вы вошли как <b>{{ me.nickname }}</b> &nbsp;|&nbsp; <a href="/admin/quests">📋 Задания</a> &nbsp;|&nbsp; <a href="/admin/gifts">🎁 Подарки</a> &nbsp;|&nbsp; <a href="/admin/news">📰 Новости</a> &nbsp;|&nbsp; <a href="/admin/minecraft">🖥 Сервер</a> &nbsp;|&nbsp; <a href="/admin/shop">🛒 Магазин</a> &nbsp;|&nbsp; <a href="/admin/cosmetics">🎩 Косметика</a> &nbsp;|&nbsp; <a href="/admin/modpack">🧩 Сборка</a> &nbsp;|&nbsp; <a href="/admin/logout">Выйти</a></div>
</header>
<main>
  <div class="stats">
    <div class="stat"><div class="num">{{ stats.total_profiles }}</div><div class="label">Пользователей</div></div>
    <div class="stat"><div class="num">{{ stats.online }}</div><div class="label">В сети</div></div>
    <div class="stat"><div class="num">{{ stats.total_banned }}</div><div class="label">Забанено</div></div>
    <div class="stat"><div class="num">{{ stats.total_admins }}</div><div class="label">Админов</div></div>
    <div class="stat"><div class="num">{{ stats.coins_total }}</div><div class="label">Монет у игроков</div></div>
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
    {% if upload_ok %}<div style="margin-bottom:14px;padding:10px 14px;border-radius:8px;background:#163a27;color:#7ee2a8">{{ upload_ok }}</div>{% endif %}
    <form class="upload-form" method="post" action="/admin/updates/upload" enctype="multipart/form-data">
      <label style="display:flex;align-items:center;gap:6px;margin:6px 0;font-size:13px;color:#c8d0e0">
        <input type="checkbox" name="mobile" value="1"
               onchange="var m=this.checked;var f=this.form;f.version.required=!m;f.version.style.display=m?'none':'';f.changelog.style.display=m?'none':'';f.extra_needed.disabled=m;f.file.accept=m?'.apk':'.exe,.zip';document.getElementById('extra_file_row').style.display='none'">
        📱 Это мобильная версия (.apk для Android)
      </label>
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
      <div id="up_status" class="muted" style="margin-top:8px"></div>
    </form>
    <script>
    (function () {
      var form = document.querySelector('form[action="/admin/updates/upload"]');
      if (!form || !window.fetch) return;
      var CH = 512 * 1024;
      function rid() { var a = new Uint8Array(12); crypto.getRandomValues(a); return Array.prototype.map.call(a, function (b) { return ('0' + b.toString(16)).slice(-2); }).join(''); }
      function say(t) { document.getElementById('up_status').textContent = t; }
      async function send(file, label) {
        var id = rid(), off = 0, fails = 0;
        while (off < file.size || off === 0) {
          var blob = file.slice(off, off + CH);
          try {
            var r = await fetch('/admin/updates/chunk?id=' + id + '&offset=' + off, {method: 'POST', body: blob, credentials: 'same-origin'});
            var j = null; try { j = await r.json(); } catch (e) {}
            if (r.status === 409 && j && typeof j.size === 'number') { off = j.size; continue; }
            if (!r.ok) throw new Error(j && j.error ? j.error : 'HTTP ' + r.status);
            off = j.size; fails = 0;
            say(label + ': ' + Math.floor(off * 100 / Math.max(file.size, 1)) + '%');
            if (file.size === 0) break;
          } catch (e) {
            if (++fails > 6) throw e;
            say(label + ': обрыв связи, повтор…');
            await new Promise(function (ok) { setTimeout(ok, 1500 * fails); });
          }
        }
        return id;
      }
      form.addEventListener('submit', async function (ev) {
        var jobs = [];
        ['file', 'extra_file'].forEach(function (n) {
          var inp = form.elements[n];
          var wanted = n === 'file' || (form.elements['extra_needed'] && form.elements['extra_needed'].checked);
          if (inp && inp.files && inp.files.length && wanted) jobs.push([n, inp]);
        });
        if (!jobs.length) return;
        ev.preventDefault();
        var btn = form.querySelector('button[type=submit]'); btn.disabled = true;
        try {
          for (var i = 0; i < jobs.length; i++) {
            var n = jobs[i][0], inp = jobs[i][1], f = inp.files[0];
            var id = await send(f, f.name);
            [[n + '_upload_id', id], [n + '_upload_name', f.name]].forEach(function (kv) {
              var h = document.createElement('input'); h.type = 'hidden'; h.name = kv[0]; h.value = kv[1]; form.appendChild(h);
            });
            inp.removeAttribute('required'); inp.removeAttribute('name');
          }
          say('Файл передан, сохраняю…');
          form.submit();
        } catch (e) {
          say('Не удалось загрузить: ' + e.message);
          btn.disabled = false;
        }
      });
    })();
    </script>
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
    <h3 style="margin:22px 0 8px">📱 Мобильная версия (Android)</h3>
    <table>
      <tr><th>Файл</th><th>Размер</th><th>Загружено</th><th>SHA-256</th><th>Действия</th></tr>
      {% if mobile %}
      <tr>
        <td>{{ mobile.name }}</td>
        <td>{{ mobile.size_mb }} МБ</td>
        <td class="muted">{{ mobile.when }}</td>
        <td class="muted">{{ mobile.sha }}…</td>
        <td class="actions">
          <a href="/download/mobile" style="margin-right:10px">Скачать</a>
          <form method="post" action="/admin/mobile/delete" onsubmit="return confirm('Удалить APK мобильной версии?');">
            <button type="submit" class="danger">Удалить</button>
          </form>
        </td>
      </tr>
      {% else %}
      <tr><td colspan="5" class="muted">APK ещё не загружен.</td></tr>
      {% endif %}
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


MC_DIR = os.path.abspath(os.environ.get("MAFIN_MC_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcserver")))
MC_VERSION = os.environ.get("MAFIN_MC_VERSION", "26.2")
MC_JAVA = os.environ.get("MAFIN_MC_JAVA", "java")
MC_MEMORY = os.environ.get("MAFIN_MC_MEMORY", "4G")
MC_PORT = int(os.environ.get("MAFIN_MC_PORT", "25565"))
MC_PUBLIC_HOST = os.environ.get("MAFIN_MC_HOST", "")
MC_NAME = os.environ.get("MAFIN_MC_NAME", "Mafin Server")
MC_PAPER_API = os.environ.get("MAFIN_MC_PAPER_API", "https://fill.papermc.io/v3/projects/paper")
_MC_API_OVERRIDDEN = "MAFIN_MC_PAPER_API" in os.environ
_mc_lock = threading.Lock()
_mc_proc = None


def _mc_path(*parts):
    return os.path.join(MC_DIR, *parts)


def _mc_find_jar():
    preferred = _mc_path("paper.jar")
    if os.path.isfile(preferred):
        return preferred
    if not os.path.isdir(MC_DIR):
        return None
    jars = [os.path.join(MC_DIR, n) for n in os.listdir(MC_DIR)
            if n.lower().startswith("paper") and n.lower().endswith(".jar")]
    if not jars:
        for n in os.listdir(MC_DIR):
            p = os.path.join(MC_DIR, n)
            if n.lower().endswith(".jar") and _mc_jar_info(p)["id"]:
                jars.append(p)
    return max(jars, key=os.path.getmtime) if jars else None


def _mc_jar_info(jar):
    """Версия Minecraft и нужная Java из version.json внутри paper.jar."""
    try:
        with zipfile.ZipFile(jar) as z:
            data = json.loads(z.read("version.json").decode("utf-8"))
        return {"id": data.get("id"), "java": data.get("java_version")}
    except Exception:
        return {"id": None, "java": None}


def _mc_java_major():
    try:
        out = subprocess.run([MC_JAVA, "-version"], capture_output=True, text=True, timeout=10)
        text = (out.stderr or "") + (out.stdout or "")
        m = re.search(r'version "(\d+)', text)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _mc_port():
    """Порт, настроенный в server.properties (или MAFIN_MC_PORT по умолчанию)."""
    v = _mc_read_props().get("server-port", "")
    return int(v) if v.isdigit() and 1 <= int(v) <= 65535 else MC_PORT


def _mc_max_players():
    v = _mc_read_props().get("max-players", "")
    return int(v) if v.isdigit() else 20


def _mc_run_port():
    """Порт, на котором сервер был запущен (если он сейчас работает)."""
    if not _mc_pid():
        return None
    try:
        with open(_mc_path("server.runport")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _mc_port_free(port):
    sock = socket.socket()
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _mc_plugin_jars():
    d = _mc_path("plugins")
    if not os.path.isdir(d):
        return []
    return [n for n in os.listdir(d) if n.lower().startswith("mafinauth") and n.lower().endswith(".jar")]


def _mc_eula_accepted():
    try:
        with open(_mc_path("eula.txt"), encoding="utf-8") as f:
            return any(line.strip().lower() == "eula=true" for line in f)
    except OSError:
        return False


def _mc_read_props():
    props = {}
    try:
        with open(_mc_path("server.properties"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()
    except OSError:
        pass
    return props


def _mc_set_props(updates):
    os.makedirs(MC_DIR, exist_ok=True)
    path = _mc_path("server.properties")
    lines = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    pending = dict(updates)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    for k, v in pending.items():
        lines.append(f"{k}={v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _mc_write_plugin_config():
    """Кладёт настройки для плагина MafinAuth, чтобы не вписывать секрет руками."""
    if not MC_SECRET:
        return
    cfg_dir = _mc_path("plugins", "MafinAuth")
    os.makedirs(cfg_dir, exist_ok=True)
    api_port = int(os.environ.get("MAFIN_PORT", "10074"))
    with open(os.path.join(cfg_dir, "config.yml"), "w", encoding="utf-8") as f:
        f.write(f'api_url: "http://127.0.0.1:{api_port}"\n')
        f.write(f"secret: {json.dumps(MC_SECRET)}\n")
        f.write("timeout_ms: 5000\n")
    cos_dir = _mc_path("plugins", "MafinCosmetics")
    os.makedirs(cos_dir, exist_ok=True)
    with open(os.path.join(cos_dir, "connection.yml"), "w", encoding="utf-8") as f:
        f.write(f'api_url: "http://127.0.0.1:{api_port}"\n')
        f.write(f"secret: {json.dumps(MC_SECRET)}\n")


MC_MIN_MEMORY_MB = 512


def _mc_fmt_mb(mb):
    if mb % 1024 == 0:
        return f"{mb // 1024} ГБ"
    if mb >= 1024:
        return f"{mb / 1024:.2f}".rstrip("0").rstrip(".") + " ГБ"
    return f"{mb} МБ"


def _mc_parse_memory(amount, unit):
    """Переводит введённое число и единицу (G или M) в мегабайты. Бросает ValueError с понятным текстом."""
    text = str(amount if amount is not None else "").strip().replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        raise ValueError("Введи объём памяти числом, например 4")
    if not value == value or value in (float("inf"), float("-inf")):
        raise ValueError("Введи объём памяти числом, например 4")
    unit = str(unit or "G").strip().upper()[:1]
    if unit not in ("G", "M"):
        raise ValueError("Единица должна быть ГБ или МБ")
    mb = int(round(value * (1024 if unit == "G" else 1)))
    if mb < MC_MIN_MEMORY_MB:
        raise ValueError(f"Минимум {MC_MIN_MEMORY_MB} МБ, иначе сервер не запустится")
    total = _mc_system_ram_mb()
    if total and mb > total:
        raise ValueError(f"В системе всего {_mc_fmt_mb(total)} памяти, больше выделить нельзя")
    return mb


def _mc_system_ram_mb():
    """Сколько оперативной памяти в системе (МБ) или None, если узнать не удалось."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _mc_default_memory_mb():
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([GgMm])[Bb]?\s*", MC_MEMORY or "")
    if m:
        mb = int(round(float(m.group(1)) * (1024 if m.group(2).lower() == "g" else 1)))
        if mb >= MC_MIN_MEMORY_MB:
            return mb
    return 4096


def _mc_get_memory_mb():
    """Память, выбранная в панели (launcher_memory.json), иначе MAFIN_MC_MEMORY, иначе 4 ГБ."""
    try:
        with open(_mc_path("launcher_memory.json"), encoding="utf-8") as f:
            mb = json.load(f).get("memory_mb")
        if isinstance(mb, int) and mb >= MC_MIN_MEMORY_MB:
            return mb
    except (OSError, ValueError, AttributeError):
        pass
    return _mc_default_memory_mb()


def _mc_save_memory(amount, unit):
    mb = _mc_parse_memory(amount, unit)
    os.makedirs(MC_DIR, exist_ok=True)
    tmp = _mc_path("launcher_memory.json.part")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"memory_mb": mb}, f)
    os.replace(tmp, _mc_path("launcher_memory.json"))
    note = " Применится после перезапуска сервера" if _mc_pid() else ""
    return f"Память сервера: {_mc_fmt_mb(mb)}.{note}"


def _mc_pid():
    """PID запущенного сервера или None."""
    global _mc_proc
    try:
        with open(_mc_path("server.pid")) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if _mc_proc is not None and _mc_proc.pid == pid and _mc_proc.poll() is not None:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return None
    except (OSError, IndexError):
        pass
    return pid


def _mc_send(command):
    fifo = _mc_path("console.fifo")
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(fd, (command + "\n").encode("utf-8"))
    finally:
        os.close(fd)


_MC_JOIN_RE = re.compile(r"\]: ([A-Za-z0-9_]{1,16})\[/([0-9A-Fa-f:.]+):\d+\] logged in with entity id")
_MC_KICK_TEXT = {
    "not_logged_in": "Войди в Mafin Launcher и запусти игру из него.",
    "offline": "Сессия устарела. Запусти Mafin Launcher и зайди снова.",
    "ip_mismatch": "Твой IP не совпадает с IP лаунчера. Зайди с того же устройства, где открыт лаунчер.",
    "banned": "Профиль заблокирован.",
}
_mc_guard_started = False


def _mc_guard_handle(name, ip):
    """Проверяет зашедшего игрока и кикает, если ему нельзя. Возвращает причину кика или None."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        ok, reason = _mc_access(db, name, ip)
        if ok:
            return None
        reason = reason or "not_logged_in"
        try:
            _mc_send(f"kick {name} {_MC_KICK_TEXT.get(reason, 'Доступ запрещён.')}")
        except OSError as e:
            print(f"[mc-guard] не удалось кикнуть {name}: {e}")
        try:
            db.execute("INSERT INTO server_logs (action, nickname, details) VALUES (?, ?, ?)",
                       ("mc_guard_kick", name, f"{reason}, ip {ip}"))
            db.commit()
        except sqlite3.Error:
            pass
        print(f"[mc-guard] кикнут {name} ({ip}): {reason}")
        return reason
    finally:
        db.close()


def _mc_guard_loop():
    log_path = _mc_path("console.log")
    pos = None
    buf = b""
    while True:
        time.sleep(0.4)
        try:
            size = os.path.getsize(log_path)
        except OSError:
            pos = None
            continue
        if pos is None or size < pos:
            pos = size if pos is None else 0
            buf = b""
            if pos == size:
                continue
        if size == pos:
            continue
        try:
            with open(log_path, "rb") as f:
                f.seek(pos)
                chunk = f.read(size - pos)
            pos += len(chunk)
        except OSError:
            continue
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for raw in lines:
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw.decode("utf-8", "replace"))
            m = _MC_JOIN_RE.search(line)
            if m:
                try:
                    _mc_guard_handle(m.group(1), m.group(2))
                except Exception as e:
                    print(f"[mc-guard] ошибка проверки {m.group(1)}: {e}")


def _mc_guard_start():
    global _mc_guard_started
    if _mc_guard_started:
        return
    _mc_guard_started = True
    threading.Thread(target=_mc_guard_loop, name="mc-guard", daemon=True).start()


def _mc_start():
    global _mc_proc
    if not hasattr(os, "mkfifo"):
        raise RuntimeError("Запуск сервера из app.py работает только на Linux")
    with _mc_lock:
        if _mc_pid():
            raise RuntimeError("Сервер уже запущен")
        jar = _mc_find_jar()
        if not jar:
            raise RuntimeError("Paper не установлен. Нажми «Установить Paper» или положи paper.jar в папку сервера")
        need_java = _mc_jar_info(jar)["java"]
        if not _mc_eula_accepted():
            raise RuntimeError("Сначала прими EULA Mojang")
        have = _mc_java_major()
        if have is None:
            raise RuntimeError(f"Не найдена Java («{MC_JAVA}»). Установи Java или задай MAFIN_MC_JAVA")
        if need_java and have < int(need_java):
            raise RuntimeError(f"Нужна Java {need_java} или новее, установлена {have}")
        mem_mb = _mc_get_memory_mb()
        total = _mc_system_ram_mb()
        if total and mem_mb > total:
            raise RuntimeError(f"Выделено {_mc_fmt_mb(mem_mb)} памяти, а в системе всего {_mc_fmt_mb(total)}. Уменьши объём в настройках")
        port = _mc_port()
        _mc_set_props({"online-mode": "false", "server-port": str(port)})
        _mc_write_plugin_config()
        fifo = _mc_path("console.fifo")
        if not os.path.exists(fifo):
            os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDWR)
        out = open(_mc_path("console.log"), "ab")
        try:
            cmd = [MC_JAVA, f"-Xms{mem_mb}M", f"-Xmx{mem_mb}M", "-jar", os.path.basename(jar), "nogui"]
            _mc_proc = subprocess.Popen(cmd, cwd=MC_DIR, stdin=fd, stdout=out, stderr=out, start_new_session=True)
        finally:
            os.close(fd)
            out.close()
        with open(_mc_path("server.pid"), "w") as f:
            f.write(str(_mc_proc.pid))
        with open(_mc_path("server.runport"), "w") as f:
            f.write(str(port))
    return f"Сервер запускается с {_mc_fmt_mb(mem_mb)} памяти (первый запуск может занять несколько минут)"


def _mc_stop(timeout=45):
    pid = _mc_pid()
    if not pid:
        return "Сервер уже остановлен"
    try:
        _mc_send("stop")
    except OSError:
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _mc_pid():
            return "Сервер остановлен"
        time.sleep(0.5)
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _mc_pid():
            return "Сервер остановлен (принудительно, SIGTERM)"
        time.sleep(0.5)
    os.kill(pid, signal.SIGKILL)
    return "Сервер убит (SIGKILL)"


def _mc_ping(host="127.0.0.1", port=None, timeout=1.5):
    """Server List Ping: сервер уже принимает подключения? Сколько игроков?"""
    port = port or _mc_run_port() or _mc_port()

    def varint(n):
        out = b""
        while True:
            b = n & 0x7F
            n >>= 7
            out += bytes([b | (0x80 if n else 0)])
            if not n:
                return out

    def read_varint(sock):
        n = 0
        for i in range(5):
            b = sock.recv(1)
            if not b:
                raise ConnectionError("closed")
            n |= (b[0] & 0x7F) << (7 * i)
            if not b[0] & 0x80:
                return n
        raise ValueError("varint too long")

    def read_exact(sock, size):
        buf = b""
        while len(buf) < size:
            chunk = sock.recv(size - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            addr = host.encode("utf-8")
            hs = b"\x00" + varint(0) + varint(len(addr)) + addr + struct.pack(">H", port) + varint(1)
            s.sendall(varint(len(hs)) + hs)
            s.sendall(b"\x01\x00")
            read_varint(s)
            if read_varint(s) != 0:
                return None
            size = read_varint(s)
            data = json.loads(read_exact(s, size).decode("utf-8"))
        players = data.get("players") or {}
        return {"online": players.get("online", 0), "max": players.get("max", 0),
                "version": (data.get("version") or {}).get("name")}
    except Exception:
        return None


def _mc_status():
    jar = _mc_find_jar()
    pid = _mc_pid()
    ping = _mc_ping() if pid else None
    props = _mc_read_props()
    info = _mc_jar_info(jar) if jar else {"id": None, "java": None}
    mem_mb = _mc_get_memory_mb()
    return {
        "installed": bool(jar),
        "minecraft_version": info["id"],
        "java_required": info["java"],
        "java_installed": _mc_java_major(),
        "eula": _mc_eula_accepted(),
        "running": bool(pid),
        "ready": bool(ping),
        "players_online": ping["online"] if ping else 0,
        "players_max": ping["max"] if ping else 0,
        "online_mode": props.get("online-mode"),
        "port": _mc_port(),
        "run_port": _mc_run_port(),
        "max_players": _mc_max_players(),
        "plugin_installed": bool(_mc_plugin_jars()),
        "plugin_files": _mc_plugin_jars(),
        "dir": MC_DIR,
        "branding": _mc_branding(),
        "plugins": _mc_list_plugins(),
        "jar_file": os.path.basename(jar) if jar else None,
        "secret_set": bool(MC_SECRET),
        "supported": hasattr(os, "mkfifo"),
        "core": "paper",
        "memory_mb": mem_mb,
        "memory_text": _mc_fmt_mb(mem_mb),
        "system_ram_mb": _mc_system_ram_mb(),
        "motd": _mc_get_motd(),
        "worlds": _mc_list_worlds(),
        "active_world": _mc_active_world(),
        "backups": _mc_list_backups(),
        "spawns": {w: _mc_read_spawn(w) for w in _mc_list_worlds()},
        "job": dict(_mc_job),
    }


def _mc_download_paper(version):
    if not re.fullmatch(r"\d+(\.\d+){1,3}", version):
        raise ValueError("Некорректная версия")
    headers = {"User-Agent": "MafinLauncherServer/1.0"}
    req = urllib.request.Request(f"{MC_PAPER_API}/versions/{version}/builds/latest", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    downloads = data.get("downloads") or {}
    dl = downloads.get("server:default") or next((v for v in downloads.values() if isinstance(v, dict) and v.get("url")), None)
    if not dl or not dl.get("url"):
        raise RuntimeError("PaperMC не вернул ссылку на скачивание для этой версии")
    url = dl["url"]
    host = (urlparse(url).hostname or "").lower()
    if not _MC_API_OVERRIDDEN and not (urlparse(url).scheme == "https" and (host == "papermc.io" or host.endswith(".papermc.io"))):
        raise RuntimeError(f"Ссылка ведёт не на papermc.io: {host}")
    expected = ((dl.get("checksums") or {}).get("sha256") or "").lower()
    os.makedirs(MC_DIR, exist_ok=True)
    tmp = _mc_path("paper.jar.part")
    sha = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            sha.update(chunk)
            f.write(chunk)
    if expected and sha.hexdigest() != expected:
        os.remove(tmp)
        raise RuntimeError("Контрольная сумма скачанного файла не совпала, файл удалён")
    os.replace(tmp, _mc_path("paper.jar"))
    return f"Paper {version} build {data.get('id', '?')} ({data.get('channel', '?')}) скачан"


def _mc_branding():
    data = {"name": MC_NAME, "version_min": "", "version_max": ""}
    try:
        with open(_mc_path("launcher_server.json"), encoding="utf-8") as f:
            saved = json.load(f)
        for k in data:
            if isinstance(saved.get(k), str):
                data[k] = saved[k]
    except (OSError, ValueError):
        pass
    return data


def _mc_version_key(v):
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?$", (v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None


def _mc_save_branding(name, vmin, vmax):
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("Название должно быть от 1 до 40 символов")
    vmin, vmax = str(vmin or "").strip(), str(vmax or "").strip()
    for v in (vmin, vmax):
        if v and not _mc_version_key(v):
            raise ValueError(f"Некорректная версия «{v}». Пример: 1.20.4 или 26.2")
    if vmin and vmax and _mc_version_key(vmin) > _mc_version_key(vmax):
        raise ValueError("Версия «от» не может быть новее версии «до»")
    os.makedirs(MC_DIR, exist_ok=True)
    with open(_mc_path("launcher_server.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "version_min": vmin, "version_max": vmax}, f, ensure_ascii=False, indent=2)
    return f"Сохранено: «{name}»" + (f", версии {vmin or '…'} – {vmax or '…'}" if (vmin or vmax) else "")


MODRINTH_API = os.environ.get("MAFIN_MODRINTH_API", "https://api.modrinth.com/v2").rstrip("/")
_MODRINTH_OVERRIDDEN = "MAFIN_MODRINTH_API" in os.environ
MC_PLUGIN_LOADERS = ["paper", "spigot", "bukkit", "folia", "purpur"]
MC_PLUGIN_MAX_BYTES = 100 * 1024 * 1024
MC_PROTECTED_PLUGINS = ("mafinauth",)


class _McPluginError(Exception):
    def __init__(self, message, can_force=False):
        super().__init__(message)
        self.can_force = can_force


def _modrinth_get(path, params=None):
    url = MODRINTH_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "MafinLauncherServer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _McPluginError("Не найдено на Modrinth")
        raise _McPluginError(f"Modrinth ответил ошибкой {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise _McPluginError(f"Не удалось связаться с Modrinth: {e}")


def _mc_plugin_yml_name(jar_path):
    try:
        with zipfile.ZipFile(jar_path) as z:
            for fn in ("plugin.yml", "paper-plugin.yml"):
                if fn in z.namelist():
                    text = z.read(fn).decode("utf-8", errors="replace")
                    m = re.search(r"^name:\s*['\"]?([^'\"\r\n#]+)", text, re.M)
                    return m.group(1).strip() if m else ""
    except (OSError, zipfile.BadZipFile):
        pass
    return None


def _mc_list_plugins():
    d = _mc_path("plugins")
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d), key=str.lower):
            p = os.path.join(d, n)
            if n.lower().endswith(".jar") and os.path.isfile(p):
                out.append({"file": n, "name": _mc_plugin_yml_name(p) or n[:-4],
                            "size": os.path.getsize(p),
                            "protected": n.lower().startswith(MC_PROTECTED_PLUGINS)})
    return out


def _mc_plugin_search(query, compat):
    facets = [["categories:" + c for c in MC_PLUGIN_LOADERS]]
    if compat:
        ver = _mc_jar_info(_mc_find_jar()).get("id") if _mc_find_jar() else None
        facets.append(["versions:" + (ver or MC_VERSION)])
    data = _modrinth_get("/search", {"query": query, "facets": json.dumps(facets), "limit": 20,
                                     "index": "relevance" if query else "downloads"})
    return [{"slug": h.get("slug"), "title": h.get("title"), "description": h.get("description"),
             "author": h.get("author"), "downloads": h.get("downloads", 0)} for h in data.get("hits", [])]


def _mc_install_plugin(project, force=False, _depth=0, _seen=None):
    """Ставит плагин с Modrinth и его обязательные зависимости. Возвращает список сообщений."""
    seen = _seen if _seen is not None else set()
    if project in seen or _depth > 3:
        return []
    seen.add(project)
    params = {"loaders": json.dumps(MC_PLUGIN_LOADERS)}
    ver = _mc_jar_info(_mc_find_jar()).get("id") if _mc_find_jar() else None
    ver = ver or MC_VERSION
    if not force:
        params["game_versions"] = json.dumps([ver])
    versions = _modrinth_get(f"/project/{urllib.parse.quote(str(project), safe='')}/version", params)
    if not versions:
        raise _McPluginError(f"Для Minecraft {ver} подходящей версии плагина нет. "
                             f"Можно поставить последнюю версию для Paper без гарантии совместимости", can_force=True)
    chosen = next((v for v in versions if v.get("version_type") == "release"), versions[0])
    files = chosen.get("files") or []
    f = next((x for x in files if x.get("primary")), None) or next(
        (x for x in files if str(x.get("filename", "")).lower().endswith(".jar")), None)
    if not f:
        raise _McPluginError("В этой версии нет файла .jar")
    filename = secure_filename(f.get("filename", ""))
    if not filename.lower().endswith(".jar"):
        raise _McPluginError("Файл плагина должен быть .jar")
    url = f.get("url", "")
    host = (urlparse(url).hostname or "").lower()
    if not _MODRINTH_OVERRIDDEN and not (urlparse(url).scheme == "https" and host == "cdn.modrinth.com"):
        raise _McPluginError(f"Ссылка ведёт не на cdn.modrinth.com: {host}")
    hashes = f.get("hashes") or {}
    if not (hashes.get("sha512") or hashes.get("sha1")):
        raise _McPluginError("У файла нет контрольной суммы")

    plugins_dir = _mc_path("plugins")
    os.makedirs(plugins_dir, exist_ok=True)
    tmp = os.path.join(plugins_dir, filename + ".part")
    h512, h1, size = hashlib.sha512(), hashlib.sha1(), 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MafinLauncherServer/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MC_PLUGIN_MAX_BYTES:
                        raise _McPluginError("Файл плагина слишком большой")
                    h512.update(chunk)
                    h1.update(chunk)
                    out.write(chunk)
        except (urllib.error.URLError, OSError) as e:
            raise _McPluginError(f"Не удалось скачать файл плагина: {e}")
        if (hashes.get("sha512") and h512.hexdigest() != hashes["sha512"].lower()) or \
           (hashes.get("sha1") and h1.hexdigest() != hashes["sha1"].lower()):
            raise _McPluginError("Контрольная сумма файла не совпала, плагин не установлен")
        new_name = _mc_plugin_yml_name(tmp)
        if new_name is None:
            raise _McPluginError("Это не плагин для Paper (внутри нет plugin.yml)")
        for old in _mc_list_plugins():
            if old["name"].lower() == new_name.lower() and old["file"] != filename:
                if old["protected"]:
                    raise _McPluginError("Защитный плагин MafinAuth заменить из панели нельзя")
                os.remove(_mc_path("plugins", old["file"]))
        os.replace(tmp, os.path.join(plugins_dir, filename))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    messages = [f"{new_name or filename} {chosen.get('version_number', '')} установлен".replace("  ", " ")]
    for dep in chosen.get("dependencies") or []:
        if dep.get("dependency_type") == "required" and dep.get("project_id"):
            messages += _mc_install_plugin(dep["project_id"], force=force, _depth=_depth + 1, _seen=seen)
    return messages


MAFINAUTH_JAR_B64 = (
    "UEsDBAoAAAgAAGCARV0AAAAAAAAAAAAAAAAJAAQATUVUQS1JTkYv/soAAFBLAwQUAAgICABggEVdAAAAAAAAAAAAAAAAFAAA"
    "AE1FVEEtSU5GL01BTklGRVNULk1G803My0xLLS7RDUstKs7Mz7NSMNQz4OVyLkpNLElN0XWqtFIwMtUz0DPRM1TQcE3OySwo"
    "TlVwTMkvKMkszdXk5eLlAgBQSwcI4PPvakQAAABCAAAAUEsDBBQACAgIAGCARV0AAAAAAAAAAAAAAAAKAAAAY29uZmlnLnlt"
    "bDVQvUrDUBTe8xSHdlGQ9EaUQsBBSgWHOqh7KFJowWJo08HNJqMVHdxFnyDURNMkTV/hnDfyO0nlQsh3zz3fX5v4jRN54lSW"
    "NPR923+kAwVccEZc8ZZ4yzHJi4RAJfEPp4Rxip11/U2P8E6WmMe8lhVxjp+clERCWR1aQ3/iLWb3LrXGQeC7nY5z3LUNjuM6"
    "xnRPWpbVJv7klPPaSGgTv0OsUC0YgFoFrR1oE46VE1cEXKtzqY9wKt6oYXBI1GxyJq80OL+4vPIGPe+m37vu35JE+5y2qn5I"
    "VFvXZU31b4HOVKCA5DdnTQkZglWAlYIUY12tw6plaz66m40ChGzifOF+A28xXoXEv4A7UKMotIl6Gg+orgRxoW3v5SOwJ/Js"
    "BZPp6GEReNO5S6fGGOsPUEsHCPpPasMuAQAAqwEAAFBLAwQUAAgICABggEVdAAAAAAAAAAAAAAAACgAAAHBsdWdpbi55bWxF"
    "jj0OgkAQhfs9xXQ0ulEKCzp7PcQG17DFrmQBa8GYWHgHr0BQEqJxzzBzIwdNtHl5P1/xnLI6gbXaGresykzstS/MziUwlzNh"
    "lWHnK2nHXSoG5B9VuZn+8CheyDgSG12k3uTlp8MrHanGB7bYUwP4whY493TAblSgBgM+6cJIABzwxksYA3YTYCbQGXu8sw50"
    "4uL7E1aqcmmmvXgDUEsHCNqCgaqdAAAAvQAAAFBLAwQKAAAIAABggEVdAAAAAAAAAAAAAAAAAwAAAHJ1L1BLAwQKAAAIAABg"
    "gEVdAAAAAAAAAAAAAAAACQAAAHJ1L21hZmluL1BLAwQKAAAIAABggEVdAAAAAAAAAAAAAAAADgAAAHJ1L21hZmluL2F1dGgv"
    "UEsDBBQACAgIAGCARV0AAAAAAAAAAAAAAAAdAAAAcnUvbWFmaW4vYXV0aC9NYWZpbkF1dGguY2xhc3OVWQlgVNd1PVfbH4Yv"
    "YUZgEGYRAmNJSBo2ByOBbRDCDEhCllhswJa/Zr6kj0Z/xrNgK2kSDI3tNG5qZ3FM06R20sZtHNdCWIMkF0gX22m6ZFSnS9p0"
    "jZ2mS9I23dLWITnvzUhoR5Y0b73vbu/e++4dff3ayBUAu8X1Ige5BvJM5KNAsDYS6/J3JHt6nIQ/Gk52Oa7/lHXa8h9g06Ln"
    "goKdjusk7hTkllcc9cKDBQa8Jhaq88tiSX+v1cljVjLR7W9Sw90cCRbHrdP2XrvTSoYT9RG30+lSRwtNFGGRYEGXnV0VbC2v"
    "aJzARlAvJ2NWwom4/k4nbPv3samfuFznwWKBYUWd9mQs7EGx4ObuRCJa6/dv3rK9ZhN/N9du3rRp+zYvluJmA8tMLEeJwP8e"
    "CWU4bUvEHJec3lHeqLUTttwuf2axbvpKxfSlBRT9FhMrsYrqJNdHYmGBbzqcB2sE4vdiLcoMrDOxHrcKbpoKJ/DYbih+zFGK"
    "XjoDUxXHFYrbTJSjghTDttulQHmBAbWx0UQVqilbPNkRz2K8uTwQmIFzD/xEELeDMTvhwWYBlCxbTfixyoPbBd6E02tHkon2"
    "3rjS9XYTd2AHj1BtATeh8E5HGqgIKCx1JnZiF9nIomiKU3jN4F0m7qa1wnDie3iwR7N+XBlQvYm9aMhcS2Okq8uOCVaWZxlP"
    "JpywP8xVEvFndinAPQIrI0BpeqA0c+s1fb3h0vTV0XOjj42eTV9JD9eWpi+nh9IjHF9MX1GTwcz+R0fPVXHE9ZF0f2l6cML2"
    "pXR/+tV0/+iTo0/VeBHAAQMHTTSiSbB8Nn4o0yNWzNUqn/Hm6GGH0GLgXhOtaBvD5NoJvzJw/3429WHHVqr1uvYje5JOOKTQ"
    "rh9Twgyg67NQdV4cwVEDx0zch/tpgfqA0r5/77jBeyKdTU447PAylpUfyCKdBFO3ECdw0sADJh5Eu6D0RnQFRVS7awcThzM3"
    "fd2VJiOehwiKtmWiA0FBfodanGAAMxzUvmeb6AQ1nqc2BStmh1Y25pg4hZ6s69ux00qApZODVGaZzPTCNRAxEcXDjHnTIOi8"
    "RJKJpE2Wa2kLWDcZVzbuTgIi5jgSBpImTuMRwZobwFPDMbvLiSfsWMNpyhHXGp5wyFar/kYF4RL9rPRpgD6+FH0m3o8P0AJ6"
    "rR4VEYNWQkUbjuIJS6PfMJPxTl9SfvFBEx9STpHnuJ0RL87gMQNnTZzDzwo2TeMxGrb67Jh/d7zPDbbocUvMpvM4rpaMDkSN"
    "Nlu9tmBJ+YwUz+BxE0/gSXoIQXeHQjE7To6XT7SSgDu+wxM/h48ZeMrEzyuWlswERAUT1/5IfGxBGcovmHhavSr5wW472OOD"
    "4BNKb58UHJjXSzHPt+PTeNbAZ0w8h/OC7e9VYetb7TjfYApwMFB/sP3Y/sDhhsZA22HBjukGMj9c2ks+a+KXlIkYvdQGbXAh"
    "Po9fNvC8iRfwBUGZUmBPXyTm+K2QOpuM2f6E/WjCXx/pjUZcfZV5akGwc0ZbmhXBYTbjSPR9/4qJX8WXGLtCTtwKhyP0mIdm"
    "MP95Sjc75etUK4568GsMEW6kPfO2KJV82cRLSiUFdjxoRW0fc62XlUGUePEK+g1cUDF9QFAyPfy02g8n7Th1ceuMsSy7PR4F"
    "fchFnxeDSBm4ZGIIwwJz/NiR1gCZIFdWgl5SNrunZoEZbV7Dbxq4bOIKrjIrvCEDfJCTMUflbpMRzYd3Rey3TPw2fofGkxh7"
    "EHbM+0GYis+D1yk8QxNDW6L6cF/U9uBNBl4rGg07wUx2dyoecRXd3zPxdfw+ldNtW1qMwHtJ6W7Axh8KFt5X3RSsbstaxDeQ"
    "NjBq4o/xlqB8DhSRUF9LsoNvbrcdi+sneCzR2zX35c2NSqv6T0z8Kf6MvtZySDl9c/m8T8/3Mr+lnuO/EKyay3Tpp9/GXxn4"
    "a6WOvxHcNhNknN4VtzUb+y03FNbaqJwF7XTgOpU7/Z2Jv8d3KG+cOTJTvznkrZsv3rkYoArewXcN/IOJ7+EfmVzMDkqmOoh1"
    "6sN1qOMUUyON559N/ItK2b18ZhPJeH0kZKv85Qcm/hX/RnfZ3dh46FjDXoaQCRkvX377UX+LleDr71IHP8R/GPhPE/+F/+YL"
    "PgucCtxWgg9X7LoXa27qu61Ym9KPG7TrKqaRacocIpkf4X8N/J+J/1eZ+jQyTWPI81gShpQQPzZxTQlR0Nqwu+1QMxHQbUUk"
    "hw9oVyySjKosa6YaxIc8YcH6fskxhDVn8XWAhkeDdlQ5OIsG8ZiyQLyU67QVTtqHOqfkKFktz4g/H30eKaT7Jl1uOmGrI2wr"
    "lItMuUldh6fbinery/CIT1DoRhLtKq23Q+2OJr3ElKVyswr8DyetcHxKaj9G+bhHlpO9SGdn2HGJagUJOtH2Xieub8IjK4mh"
    "w2KiHPLIakFD+hOqNGGNMaQqF11clzZaSVcptlQtqgpkrIjhdCh9cfTM6LnS9JXRs/w9p6qTGo+UClrTn2Whci59efSJ9ODo"
    "mczJEQJ/bPRsTWn68zzPmaI2G5kMG6OPsfS5kh7QeMsEn0t/gRN1LNCSqYoIofavZmojEjvLJb372kQOiKA0/ZkJeEt1HaaL"
    "q5RCo4XKsKTFG1DVl2ZgUAnLmuwiTw5qWcckmUKDLK5n3E0/zdJtJLtxNiPNBUKqim5IUyDu9GVCbxAcTb+g2RvIqKk/PaD4"
    "4sKQOjf6OPshLSiJX9EsnqOsg+nL2SLxqkZ4gavDmYVLShziLmdte9IjlYKckyeVzVSZUi01tIeYzbwkyOCwd1Y3vIF7TirY"
    "ZRMplXlki6LEfhvDTZlOiuxQ2cl4ZS0/iViS9v1DeZ8p2+UO8hBkYuOEyUP17O/NDLHGI7XMq8uYaPB9HUNeVn7iwbIHKivK"
    "DNnJQDVrEcLQoFxKsKiR7tCc7O2wY4eV56kH0G1wM8PCtoQV7Gmyotktb8QdS9oEt7/3JI+ZG5lqTboq0TjqxB1i3e3So3Wi"
    "QNddMx2lPjj2FpC7aMxhcphgIC+dBbglC0HoAhWx1cvr2RkMZ79J87ZFkrGgvU+rvGj8W7MapWdmLnsikUQ8EbOiTXaiOxKK"
    "e4TV0/ZxMAYDmu5rox8ffWL0mYzlZc1YWa76miLrY4PpS6XikUOMmRkXpz1zpbRcKmrVxr0MVB8oc1lLldWWSVlVmRPVgw96"
    "pI18id+KOv7eoF+XNx45ovImPmftPHpMsJoJVk20b0ZvIPpFBXK/V47LCUNOquD9gGD9ddNy3NORHjtrYZkCc58VTERiVOpz"
    "E20wC5jRROYO4usbI5GeZHTuimrSQZUVzgB+Yu73IYuint7T5iRsXqYZYHSO1YeteNymrRjj6XDBeJU1OZkzJDhLrjNDxkVr"
    "nzI3J6ZChjAh3DC/lIVPy4SZIadYWcxLqYaEebVzgyqj1sDYzHc9ByJ1KMAKlaMA7K/pPkd956L7lVile3+234ld7A3Wy/k8"
    "J6JmqzgT9vmVlyAXOBC5k22BXlzG2V3joNd4TK2+WJmCUcnG9N3k86WwZBArKl/FCt/qFErPY3mlmuSqJoUNeaEUKhWAhq/x"
    "bdLwWzLTbYuLn0zhfYOoJfSWN1CouhTuPA+T+3t8+1LYXzmEZi7XfmQIh4dxPAfDeEhYgYUI0T0Maq2ychixXKgTiugIHgVS"
    "+BklTI4WphZFbNchD+tRjFuxBhuwDeWoRwXasBEhVKGXWjqNTfgQdfs4tuBZbMWXsE3uVrpB7ja/h1rYLXuIUuniHJHls6/b"
    "mMKHm9h8JIWPNldWVafw8dq8krw3kXdBMVRVXZI3gmc0QxsH8KmSvCH84hA+l8IXr/NXxtsE7iDCHSgkt0tRx9vciVtwJ7m7"
    "iyt3az6KINdwi4G1mT+p51oh2drLT4Mt++QeXvLTKMncl7zAYlUh/t5U5Rb4XuzfOIRfr+JnBF8BmofwGxnVXQSL2leHMZKD"
    "60r/ag58v+t7Yxhfy4XvDxQaPawewh8N45vqRv5cUJvHjZCS7y9T+Nva/JL8YbwteBMe36YXUZidfhVrawvU+J8Ei/G151E8"
    "gO+XFKTw7yn8z3nkS/8A3tXzWqPE0GveEiMvhZ/0Z0+NSC7Q36yUWz0kxogsVMrdXyxmP+VdQnHekny8jNd1/4YuAvOzij5K"
    "1QJ7SbmBkPup+ABqcIDqPoh70IhWNOE+NOMh1jNdaEES91KfrXiJZvIyjuASMVzFMWK9H2/hOL6JE3gbJ/XlbIWxOOcaIvpu"
    "XjHwToCjd7Fat8sN/OgnMNW/gDyZy4MYZMurvsvJutfb6l9E7J+vbMzZtTElRS9B/9j85D3+rR88y77qiwOF31Fm95XXP7ya"
    "/f2vFD7zXfYHNhbL4pQUn8eW3F0voprTZXq6Lo/T1ZyW6OmSfE4XcXqLnuYX7Fr5ZUWjJkNK01+tLRL0EaCyWFb1F8saftby"
    "s46fW/vH7XYFQwNg4SkEqSYbn6LSnoNDmXq0Snzw/hjblKUWFRUtXJ3LH0r80rjEVaSnAsoSkrmtWCpSQs78xbKZg/4poSgK"
    "j+wfD0V+vQYUF8vWIbn9Ir5fLDv04N3rMUz9zw30hgVkNFcCmu0DclCLViD3EapRt826bdFtq24P6/aoPEjYPfShEzgk7QXq"
    "G8Ez8tDdJay9X9Hzb7C3sEA62Ic4/zbeEZvzTvbdBQvEkR7pRclPAVBLBwiBRRc1tQ0AAAccAABQSwECCgAKAAAIAABggEVd"
    "AAAAAAAAAAAAAAAACQAEAAAAAAAAAAAAAAAAAAAATUVUQS1JTkYv/soAAFBLAQIUABQACAgIAGCARV3g8+9qRAAAAEIAAAAU"
    "AAAAAAAAAAAAAAAAACsAAABNRVRBLUlORi9NQU5JRkVTVC5NRlBLAQIUABQACAgIAGCARV36T2rDLgEAAKsBAAAKAAAAAAAA"
    "AAAAAAAAALEAAABjb25maWcueW1sUEsBAhQAFAAICAgAYIBFXdqCgaqdAAAAvQAAAAoAAAAAAAAAAAAAAAAAFwIAAHBsdWdp"
    "bi55bWxQSwECCgAKAAAIAABggEVdAAAAAAAAAAAAAAAAAwAAAAAAAAAAAAAAAADsAgAAcnUvUEsBAgoACgAACAAAYIBFXQAA"
    "AAAAAAAAAAAAAAkAAAAAAAAAAAAAAAAADQMAAHJ1L21hZmluL1BLAQIKAAoAAAgAAGCARV0AAAAAAAAAAAAAAAAOAAAAAAAA"
    "AAAAAAAAADQDAABydS9tYWZpbi9hdXRoL1BLAQIUABQACAgIAGCARV2BRRc1tQ0AAAccAAAdAAAAAAAAAAAAAAAAAGADAABy"
    "dS9tYWZpbi9hdXRoL01hZmluQXV0aC5jbGFzc1BLBQYAAAAACAAIANwBAABgEQAAAAA="
)
MAFINAUTH_JAR_SHA256 = "42b1fdf1669e551ac32039edc4be2757c17405f84cfeac52d39406e1decd499f"


def _mc_install_builtin_mafinauth():
    import base64
    data = base64.b64decode(MAFINAUTH_JAR_B64)
    if hashlib.sha256(data).hexdigest() != MAFINAUTH_JAR_SHA256:
        raise _McPluginError("Встроенный файл MafinAuth повреждён")
    os.makedirs(_mc_path("plugins"), exist_ok=True)
    dest = _mc_path("plugins", "MafinAuth.jar")
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return "MafinAuth установлен. Секрет для него создаётся и записывается автоматически при запуске сервера"


def _mc_delete_plugin(filename):
    if filename != os.path.basename(filename) or not filename.lower().endswith(".jar"):
        raise _McPluginError("Некорректное имя файла")
    if filename.lower().startswith(MC_PROTECTED_PLUGINS):
        raise _McPluginError("Защитный плагин MafinAuth удалить из панели нельзя: без него сервер откроется для всех")
    path = _mc_path("plugins", filename)
    if not os.path.isfile(path):
        raise _McPluginError("Файл не найден")
    os.remove(path)
    return f"{filename} удалён. Перезапусти сервер, чтобы изменения вступили в силу"


MC_ZIP_MAX_ENTRIES = 60000
MC_WORLD_MAX_BYTES = 8 * 1024 ** 3
MC_RESERVED_WORLD_NAMES = {
    "mods", "config", "plugins", "libraries", "logs", "cache", "versions", "defaultconfigs",
    "kubejs", "scripts", "resourcepacks", "datapacks", "crash-reports", "backups",
}


def _mc_rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _mc_safe_rel(rel, root):
    """Нормализует путь из архива. Возвращает (чистый_путь, полный_путь) или None,
    если путь пытается выйти за пределы папки."""
    raw = str(rel or "").replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or "\x00" in raw:
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    full = os.path.normpath(os.path.join(root, *parts))
    if os.path.commonpath([root, full]) != root:
        return None
    return "/".join(parts), full


def _mc_extract_zip(zf, prefix, dest_root, limit):
    """Распаковывает записи с префиксом prefix в dest_root. Возвращает (список файлов, пропущено)."""
    written, total, skipped = [], 0, 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if "__MACOSX/" in name or rel.endswith(".DS_Store"):
            continue
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            skipped += 1
            continue
        res = _mc_safe_rel(rel, dest_root)
        if not res:
            skipped += 1
            continue
        rel_clean, full = res
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with zf.open(info) as src, open(full, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise _McPluginError("Архив слишком большой после распаковки")
                out.write(chunk)
        written.append(rel_clean)
    return written, skipped


def _mc_active_world():
    return _mc_read_props().get("level-name") or "world"


def _mc_list_worlds():
    out = []
    try:
        names = sorted(os.listdir(MC_DIR), key=str.lower)
    except OSError:
        return out
    for n in names:
        p = _mc_path(n)
        if os.path.isdir(p) and not os.path.islink(p) and os.path.isfile(os.path.join(p, "level.dat")):
            out.append(n)
    return out


def _mc_world_activate(name):
    if _mc_pid():
        raise ValueError("Сначала останови сервер")
    if name not in _mc_list_worlds():
        raise ValueError("Такого мира нет")
    _mc_set_props({"level-name": name})
    return f"Активный мир: «{name}». Он загрузится при следующем запуске сервера"


def _mc_world_delete(name):
    if _mc_pid():
        raise ValueError("Сначала останови сервер")
    if name not in _mc_list_worlds():
        raise ValueError("Такого мира нет")
    if name == _mc_active_world():
        raise ValueError("Активный мир удалить нельзя. Сначала сделай активным другой")
    shutil.rmtree(_mc_path(name))
    return f"Мир «{name}» удалён"


_mc_job = {"running": False, "text": "", "ok": True}
_mc_job_lock = threading.Lock()
_MC_REGION_RE = re.compile(r"^r\.(-?\d+)\.(-?\d+)\.mca$")
_MC_OVERSIZE_RE = re.compile(r"^c\.(-?\d+)\.(-?\d+)\.mcc$")
_MC_BACKUP_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}\.zip$")


def _mc_backups_dir():
    return _mc_path("backups")


def _mc_dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _mc_list_backups():
    out = []
    try:
        for n in os.listdir(_mc_backups_dir()):
            full = os.path.join(_mc_backups_dir(), n)
            if _MC_BACKUP_RE.match(n) and os.path.isfile(full):
                st = os.stat(full)
                out.append({"file": n, "size": st.st_size, "time": int(st.st_mtime)})
    except OSError:
        pass
    return sorted(out, key=lambda b: -b["time"])


def _mc_zip_world(name):
    src = _mc_path(name)
    os.makedirs(_mc_backups_dir(), exist_ok=True)
    size = _mc_dir_size(src)
    if shutil.disk_usage(MC_DIR).free < size + 200 * 1024 * 1024:
        raise ValueError("Недостаточно места на диске для бэкапа")
    dest = os.path.join(_mc_backups_dir(), f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")
    tmp = dest + ".part"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as z:
            for root, _dirs, files in os.walk(src):
                for f in files:
                    if f == "session.lock":
                        continue
                    full = os.path.join(root, f)
                    try:
                        z.write(full, os.path.relpath(full, MC_DIR))
                    except OSError:
                        pass
        os.replace(tmp, dest)
    finally:
        _mc_rm(tmp)
    return dest


def _mc_world_backup(name):
    if name not in _mc_list_worlds():
        raise ValueError("Такого мира нет")
    running = bool(_mc_pid())
    if running:
        try:
            _mc_send("save-off")
            _mc_send("save-all flush")
            time.sleep(6)
        except OSError:
            running = False
    try:
        dest = _mc_zip_world(name)
    finally:
        if running:
            try:
                _mc_send("save-on")
            except OSError:
                pass
    return f"Бэкап готов: {os.path.basename(dest)} ({os.path.getsize(dest) // (1024 * 1024)} МБ)"


def _mc_overworld_base(world_dir):
    new_style = os.path.join(world_dir, "dimensions", "minecraft", "overworld")
    return new_style if os.path.isdir(new_style) else world_dir


def _nbt_parse(buf):
    pos = [0]

    def take(n):
        chunk = buf[pos[0]:pos[0] + n]
        if len(chunk) != n:
            raise ValueError("NBT обрезан")
        pos[0] += n
        return chunk

    def string():
        return take(struct.unpack(">H", take(2))[0]).decode("utf-8", "replace")

    def payload(t):
        if t == 1:
            return struct.unpack(">b", take(1))[0]
        if t == 2:
            return struct.unpack(">h", take(2))[0]
        if t == 3:
            return struct.unpack(">i", take(4))[0]
        if t == 4:
            return struct.unpack(">q", take(8))[0]
        if t == 5:
            return struct.unpack(">f", take(4))[0]
        if t == 6:
            return struct.unpack(">d", take(8))[0]
        if t == 7:
            return list(take(struct.unpack(">i", take(4))[0]))
        if t == 8:
            return string()
        if t == 9:
            et, n = struct.unpack(">bi", take(5))
            return [payload(et) for _ in range(max(n, 0))]
        if t == 10:
            out = {}
            while True:
                tt = struct.unpack(">b", take(1))[0]
                if tt == 0:
                    return out
                key = string()
                out[key] = payload(tt)
        if t == 11:
            n = struct.unpack(">i", take(4))[0]
            return list(struct.unpack(">%di" % n, take(4 * n)))
        if t == 12:
            n = struct.unpack(">i", take(4))[0]
            return list(struct.unpack(">%dq" % n, take(8 * n)))
        raise ValueError("Неизвестный тип NBT")

    if struct.unpack(">b", take(1))[0] != 10:
        raise ValueError("Это не NBT")
    string()
    return payload(10)


def _mc_read_spawn(world):
    """Точка мирового спавна из level.dat: (x, z) или None. Поддерживает старый и новый формат."""
    import gzip
    try:
        with gzip.open(_mc_path(world, "level.dat"), "rb") as f:
            data = _nbt_parse(f.read()).get("Data") or {}
    except Exception:
        return None
    if "SpawnX" in data and "SpawnZ" in data:
        return int(data["SpawnX"]), int(data["SpawnZ"])
    spawn = data.get("spawn")
    if isinstance(spawn, dict):
        pos = spawn.get("pos")
        if isinstance(pos, list) and len(pos) >= 3:
            return int(pos[0]), int(pos[2])
    return None


def _mc_world_reset(name, cx, cz, radius, restart):
    if name not in _mc_list_worlds():
        raise ValueError("Такого мира нет")
    if radius < 0 or radius > 5000:
        raise ValueError("Радиус спавна: от 0 до 5000 блоков")
    was_running = bool(_mc_pid())
    if was_running:
        _mc_stop()
        if _mc_pid():
            raise ValueError("Не удалось остановить сервер")
    if cx is None or cz is None:
        found = _mc_read_spawn(name)
        cx, cz = found if found else (0, 0)
    backup = _mc_zip_world(name)
    base = _mc_overworld_base(_mc_path(name))
    rx0, rx1 = int((cx - radius) // 512), int((cx + radius) // 512)
    rz0, rz1 = int((cz - radius) // 512), int((cz + radius) // 512)

    def keep_region(rx, rz):
        return rx0 <= rx <= rx1 and rz0 <= rz <= rz1

    removed = kept = 0
    for sub in ("region", "poi", "entities"):
        folder = os.path.join(base, sub)
        if not os.path.isdir(folder):
            continue
        for fname in os.listdir(folder):
            full = os.path.join(folder, fname)
            if not os.path.isfile(full):
                continue
            m = _MC_REGION_RE.match(fname)
            c = _MC_OVERSIZE_RE.match(fname)
            keep = False
            if sub != "entities":
                if m:
                    keep = keep_region(int(m.group(1)), int(m.group(2)))
                elif c:
                    keep = keep_region(int(c.group(1)) >> 5, int(c.group(2)) >> 5)
            if keep:
                kept += 1
            else:
                _mc_rm(full)
                removed += 1
    msg = (f"Мир «{name}» сброшен: удалено файлов {removed}, спавн сохранён ({kept} файлов). "
           f"Незер и Энд, инвентари игроков и регионы плагинов не тронуты. Бэкап: {os.path.basename(backup)}")
    if restart or was_running:
        try:
            _mc_start()
            msg += ". Сервер запущен"
        except RuntimeError as e:
            msg += f". Сервер не запустился: {e}"
    return msg


def _mc_start_job(title, fn):
    with _mc_job_lock:
        if _mc_job["running"]:
            raise ValueError("Уже выполняется другая операция: " + _mc_job["text"])
        _mc_job.update(running=True, text=title + "…", ok=True)

    def work():
        try:
            text, ok = fn(), True
        except Exception as e:
            text, ok = f"Ошибка: {e}", False
        with _mc_job_lock:
            _mc_job.update(running=False, text=text, ok=ok)
    threading.Thread(target=work, daemon=True).start()
    return title + ": запущено, ход виден в блоке «Миры»"


def _mc_world_import(zip_path, wanted_name, fallback_name):
    """Ставит мир из zip-архива как отдельную папку (существующие миры не перезаписываются)."""
    stage = _mc_path(".world_stage")
    shutil.rmtree(stage, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            if len(z.infolist()) > MC_ZIP_MAX_ENTRIES:
                raise _McPluginError("В архиве слишком много файлов")
            roots = []
            for i in z.infolist():
                n = i.filename.replace("\\", "/")
                if "__MACOSX/" in n:
                    continue
                if n == "level.dat" or n.endswith("/level.dat"):
                    roots.append(n[:-len("level.dat")])
            if not roots:
                raise _McPluginError("В архиве нет мира (не найден level.dat)")
            roots.sort(key=lambda r: (r.count("/"), r))
            root = roots[0]
            if len(roots) > 1 and roots[1].count("/") == root.count("/"):
                raise _McPluginError("В архиве несколько миров. Загрузи их по одному")
            base = (wanted_name or "").strip() or root.rstrip("/").split("/")[-1] or fallback_name or "world"
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", base).strip(".")[:40]
            if not name.strip("_"):
                name = "world_import"
            if name.lower() in MC_RESERVED_WORLD_NAMES:
                raise _McPluginError(f"Имя «{name}» зарезервировано, выбери другое")
            if os.path.lexists(_mc_path(name)):
                raise _McPluginError(f"Папка «{name}» уже существует. Укажи другое имя мира")
            os.makedirs(stage)
            _mc_extract_zip(z, root, stage, MC_WORLD_MAX_BYTES)
        if not os.path.isfile(os.path.join(stage, "level.dat")):
            raise _McPluginError("Не удалось распаковать мир")
        os.replace(stage, _mc_path(name))
        return f"Мир «{name}» загружен. Сделай его активным в списке миров и перезапусти сервер"
    except zipfile.BadZipFile:
        raise _McPluginError("Файл повреждён или это не .zip")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _mc_props_unescape(s):
    def repl(m):
        t = m.group(1)
        if t[0] == "u" and len(t) == 5:
            return chr(int(t[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(t, t)
    out = re.sub(r"\\(u[0-9a-fA-F]{4}|.)", repl, s)
    try:
        return out.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return out


def _mc_props_escape(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif o < 32 or o > 126:
            b = ch.encode("utf-16-be")
            out.append("".join("\\u%04x" % int.from_bytes(b[i:i + 2], "big") for i in range(0, len(b), 2)))
        else:
            out.append(ch)
    text = "".join(out)
    return "\\u0020" + text[1:] if text.startswith(" ") else text


def _mc_get_motd():
    """Описание для редактирования: цветовые коды показываются как &a, &6 и т.д."""
    raw = _mc_read_props().get("motd")
    return _mc_props_unescape(raw).replace("\u00a7", "&") if raw is not None else ""


def _mc_motd_plain():
    raw = _mc_read_props().get("motd")
    text = _mc_props_unescape(raw) if raw is not None else ""
    return re.sub("\u00a7.", "", text).replace("\n", " ").strip()


def _mc_save_motd(text):
    text = str(text or "").replace("\r", "")
    text = "".join(ch for ch in text if ch == "\n" or (ch >= " " and ch != "\x7f"))
    lines = text.split("\n")
    if len(lines) > 2:
        raise ValueError("В описании не больше двух строк")
    lines = [re.sub(r"&([0-9a-fk-orA-FK-OR])", "\u00a7\\1", ln.rstrip()) for ln in lines]
    for ln in lines:
        if len(re.sub("\u00a7.", "", ln)) > 100:
            raise ValueError("Строка описания слишком длинная (максимум 100 символов)")
    _mc_set_props({"motd": _mc_props_escape("\n".join(lines))})
    note = " Применится после перезапуска сервера" if _mc_pid() else ""
    return "Описание сервера сохранено." + note


@app.route('/admin/minecraft/world/upload', methods=['POST'])
@web_admin_required
def admin_minecraft_world_upload():
    f = request.files.get('file')
    if not f or not f.filename or not f.filename.lower().endswith('.zip'):
        return jsonify({"success": False, "error": "Выбери архив .zip с миром"}), 400
    os.makedirs(MC_DIR, exist_ok=True)
    tmp = _mc_path("world_upload.part")
    f.save(tmp)
    try:
        msg = _mc_world_import(tmp, request.form.get('name', ''),
                               os.path.splitext(secure_filename(f.filename))[0])
    except _McPluginError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        _mc_rm(tmp)
    print(f"[mc] {g.web_admin['nickname']}: world upload -> {msg}")
    return jsonify({"success": True, "message": msg})


@app.route('/admin/minecraft/backup/<fname>', methods=['GET'])
@web_admin_required
def admin_minecraft_backup_download(fname):
    if not _MC_BACKUP_RE.match(fname) or not os.path.isfile(os.path.join(_mc_backups_dir(), fname)):
        return "", 404
    return send_from_directory(_mc_backups_dir(), fname, as_attachment=True)


@app.route('/api/mc/status', methods=['GET'])
def mc_public_status():
    """Публичный статус для лаунчера (без чувствительных данных)."""
    st = _mc_status()
    host = MC_PUBLIC_HOST or request.host.split(':')[0]
    return jsonify({
        "success": True,
        "name": _mc_branding()["name"], "version_min": _mc_branding()["version_min"],
        "version_max": _mc_branding()["version_max"], "running": st["running"], "ready": st["ready"],
        "players_online": st["players_online"], "players_max": st["players_max"],
        "version": st["minecraft_version"], "address": f"{host}:{st['run_port'] or st['port']}",
        "description": _mc_motd_plain(), "core": "paper",
        "modpack": _modpack_summary(),
    })


@app.route('/admin/minecraft', methods=['GET'])
@web_admin_required
def admin_minecraft_page():
    return render_template_string(MC_ADMIN_TEMPLATE, default_version=MC_VERSION)


@app.route('/admin/minecraft/status', methods=['GET'])
@web_admin_required
def admin_minecraft_status():
    return jsonify(_mc_status())


@app.route('/admin/minecraft/plugins/search', methods=['GET'])
@web_admin_required
def admin_minecraft_plugin_search():
    try:
        results = _mc_plugin_search(request.args.get('q', '').strip()[:100], request.args.get('compat') == '1')
        return jsonify({"success": True, "results": results})
    except _McPluginError as e:
        return jsonify({"success": False, "error": str(e)}), 502


def _mc_system_stats():
    """Нагрузка VDS: load average, занятость и «кража» процессора соседями, свободная память."""
    stats = {"cpus": os.cpu_count()}
    try:
        stats["load"] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        pass

    def read_cpu():
        with open("/proc/stat") as f:
            return [int(x) for x in f.readline().split()[1:9]]
    try:
        a = read_cpu()
        time.sleep(1)
        b = read_cpu()
        d = [y - x for x, y in zip(a, b)]
        total = sum(d) or 1
        stats["cpu_busy_pct"] = round(100 * (total - d[3] - d[4]) / total, 1)
        stats["cpu_steal_pct"] = round(100 * d[7] / total, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
        stats["mem_available_mb"] = mem["MemAvailable"] // 1024
        stats["mem_total_mb"] = mem["MemTotal"] // 1024
    except (OSError, ValueError, KeyError):
        pass
    return stats


def _mc_diagnose():
    """Спрашивает у сервера TPS, время тика и пинг игроков (spark) и собирает вывод из лога."""
    if not _mc_pid():
        raise ValueError("Сервер не запущен")
    log_path = _mc_path("logs", "latest.log")
    try:
        start = os.path.getsize(log_path)
    except OSError:
        start = 0
    for cmd in ("tps", "mspt", "spark ping"):
        _mc_send(cmd)
        time.sleep(0.3)
    system = _mc_system_stats()
    time.sleep(1.5)
    lines = []
    try:
        with open(log_path, "rb") as f:
            f.seek(start if start <= os.path.getsize(log_path) else 0)
            text = f.read(60000).decode("utf-8", errors="replace")
        for raw in text.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw).strip()
            if line:
                lines.append(line)
    except OSError:
        pass
    hints = []
    steal = system.get("cpu_steal_pct")
    if steal is not None and steal >= 5:
        hints.append(f"Процессор «крадут» соседи по VDS ({steal}%): это даёт скачки пинга у всех сразу. Поможет другой тариф или хостинг.")
    busy, cpus, load = system.get("cpu_busy_pct"), system.get("cpus") or 1, (system.get("load") or [0])[0]
    if load > cpus:
        hints.append(f"Load average ({load}) выше числа ядер ({cpus}): VDS перегружен.")
    elif busy is not None and busy >= 85:
        hints.append(f"Процессор занят на {busy}%.")
    avail = system.get("mem_available_mb")
    if avail is not None and avail < 1024:
        hints.append(f"Свободно только {avail} МБ памяти: возможна подкачка, из-за неё сервер лагает.")
    if not hints:
        hints.append("Сам VDS выглядит нормально. Если TPS и MSPT ниже тоже в порядке, пинг у конкретных игроков чаще всего "
                     "из-за маршрута до сервера (расстояние, провайдер).")
    return {"system": system, "output": lines[-40:], "hints": hints}


@app.route('/admin/minecraft/diagnose', methods=['GET'])
@web_admin_required
def admin_minecraft_diagnose():
    try:
        return jsonify({"success": True, **_mc_diagnose()})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except OSError as e:
        return jsonify({"success": False, "error": f"Консоль сервера недоступна: {e}"}), 500


@app.route('/admin/minecraft/log', methods=['GET'])
@web_admin_required
def admin_minecraft_log():
    for p in (_mc_path("logs", "latest.log"), _mc_path("console.log")):
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 60000))
                text = f.read().decode("utf-8", errors="replace")
            return jsonify({"log": "\n".join(text.splitlines()[-200:])})
        except OSError:
            continue
    return jsonify({"log": ""})


@app.route('/admin/minecraft/upload/<kind>', methods=['POST'])
@web_admin_required
def admin_minecraft_upload(kind):
    """Загрузка плагина (kind=plugin) или серверного Paper (kind=server) прямо из админки."""
    f = request.files.get('file')
    if kind not in ('plugin', 'server') or not f or not f.filename:
        return jsonify({"success": False, "error": "Выбери файл .jar"}), 400
    name = secure_filename(f.filename)
    if not name.lower().endswith('.jar'):
        return jsonify({"success": False, "error": "Нужен файл .jar"}), 400
    if kind == 'server' and _mc_pid():
        return jsonify({"success": False, "error": "Сначала останови сервер"}), 400
    os.makedirs(MC_DIR, exist_ok=True)
    tmp = _mc_path("upload.part")
    f.save(tmp)
    try:
        if os.path.getsize(tmp) > 300 * 1024 * 1024:
            raise ValueError("Файл слишком большой")
        with zipfile.ZipFile(tmp) as z:
            names = set(z.namelist())
        if kind == 'plugin' and not ({'plugin.yml', 'paper-plugin.yml'} & names):
            raise ValueError("Это не плагин (внутри нет plugin.yml)")
        if kind == 'server' and 'version.json' not in names:
            raise ValueError("Это не серверный Paper (внутри нет version.json)")
        if kind == 'plugin':
            os.makedirs(_mc_path("plugins"), exist_ok=True)
            dest = _mc_path("plugins", name)
            msg = f"Плагин {name} загружен в plugins/. Перезапусти сервер, чтобы он заработал"
        else:
            dest = _mc_path("paper.jar")
            msg = f"Сервер загружен как paper.jar ({name})"
        os.replace(tmp, dest)
    except zipfile.BadZipFile:
        return jsonify({"success": False, "error": "Файл повреждён или это не .jar"}), 400
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    print(f"[mc] {g.web_admin['nickname']}: upload {kind} {name}")
    return jsonify({"success": True, "message": msg})


@app.route('/admin/minecraft/<action>', methods=['POST'])
@web_admin_required
def admin_minecraft_action(action):
    if not request.is_json:
        return jsonify({"success": False, "error": "Ожидается JSON"}), 400
    data = request.get_json(silent=True) or {}
    try:
        if action == "install":
            msg = _mc_download_paper(str(data.get("version") or MC_VERSION).strip())
        elif action == "accept_eula":
            if data.get("accept") is not True:
                return jsonify({"success": False, "error": "Нужно явно подтвердить согласие"}), 400
            os.makedirs(MC_DIR, exist_ok=True)
            with open(_mc_path("eula.txt"), "w", encoding="utf-8") as f:
                f.write("# Принято администратором через админ-панель Mafin Launcher\n"
                        "# https://aka.ms/MinecraftEULA\neula=true\n")
            msg = "EULA принята"
        elif action == "branding":
            msg = _mc_save_branding(data.get("name"), data.get("version_min"), data.get("version_max"))
        elif action == "plugin_install":
            project = str(data.get("project", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_\-]{2,64}", project):
                return jsonify({"success": False, "error": "Некорректный плагин"}), 400
            msg = "; ".join(_mc_install_plugin(project, force=bool(data.get("force"))))
            if _mc_pid():
                msg += ". Перезапусти сервер, чтобы плагин заработал"
        elif action == "plugin_install_builtin":
            if data.get("name") != "mafinauth":
                return jsonify({"success": False, "error": "Неизвестный встроенный плагин"}), 400
            msg = _mc_install_builtin_mafinauth()
            if _mc_pid():
                msg += ". Перезапусти сервер, чтобы плагин заработал"
        elif action == "plugin_delete":
            msg = _mc_delete_plugin(str(data.get("file", "")))
        elif action == "settings":
            try:
                new_port = int(data.get("port"))
                new_slots = int(data.get("max_players"))
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "Порт и слоты должны быть числами"}), 400
            if not 1024 <= new_port <= 65535:
                return jsonify({"success": False, "error": "Порт должен быть от 1024 до 65535"}), 400
            if not 1 <= new_slots <= 1000:
                return jsonify({"success": False, "error": "Слотов должно быть от 1 до 1000"}), 400
            if new_port == int(os.environ.get("MAFIN_PORT", "10074")):
                return jsonify({"success": False, "error": "Этот порт занят самим API лаунчера"}), 400
            if new_port != (_mc_run_port() or _mc_port()) and not _mc_port_free(new_port):
                return jsonify({"success": False, "error": f"Порт {new_port} уже занят другой программой"}), 400
            _mc_set_props({"server-port": str(new_port), "max-players": str(new_slots)})
            if _mc_pid():
                msg = f"Сохранено: порт {new_port}, слотов {new_slots}. Применится после перезапуска сервера"
            else:
                msg = f"Сохранено: порт {new_port}, слотов {new_slots}"
        elif action == "motd":
            msg = _mc_save_motd(data.get("motd"))
        elif action == "world_activate":
            msg = _mc_world_activate(str(data.get("name", "")))
        elif action == "world_delete":
            msg = _mc_world_delete(str(data.get("name", "")))
        elif action == "world_backup":
            wname = str(data.get("name", ""))
            msg = _mc_start_job(f"Бэкап мира «{wname}»", lambda: _mc_world_backup(wname))
        elif action == "world_reset":
            wname = str(data.get("name", ""))
            try:
                cx = float(data["x"]) if str(data.get("x", "")).strip() != "" else None
                cz = float(data["z"]) if str(data.get("z", "")).strip() != "" else None
                rad = float(data.get("radius", 600))
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "Координаты и радиус должны быть числами"}), 400
            restart = bool(data.get("restart"))
            msg = _mc_start_job(f"Сброс мира «{wname}»", lambda: _mc_world_reset(wname, cx, cz, rad, restart))
        elif action == "backup_delete":
            fname = str(data.get("file", ""))
            if not _MC_BACKUP_RE.match(fname) or not os.path.isfile(os.path.join(_mc_backups_dir(), fname)):
                raise ValueError("Бэкап не найден")
            os.remove(os.path.join(_mc_backups_dir(), fname))
            msg = f"Бэкап {fname} удалён"
        elif action == "memory":
            msg = _mc_save_memory(data.get("amount"), data.get("unit"))
        elif action == "start":
            msg = _mc_start()
        elif action == "stop":
            msg = _mc_stop()
        elif action == "restart":
            _mc_stop()
            msg = _mc_start()
        elif action == "command":
            cmd = str(data.get("command", "")).replace("\r", " ").replace("\n", " ").strip().lstrip("/")
            if not cmd or len(cmd) > 250:
                return jsonify({"success": False, "error": "Пустая или слишком длинная команда"}), 400
            if not _mc_pid():
                return jsonify({"success": False, "error": "Сервер не запущен"}), 400
            _mc_send(cmd)
            msg = f"Отправлено: {cmd}"
        else:
            return jsonify({"success": False, "error": "Неизвестное действие"}), 404
    except _McPluginError as e:
        return jsonify({"success": False, "error": str(e), "can_force": e.can_force}), (409 if e.can_force else 400)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    print(f"[mc] {g.web_admin['nickname']}: {action} -> {msg}")
    return jsonify({"success": True, "message": msg})


MC_ADMIN_TEMPLATE = """
<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Mafin Launcher — Minecraft-сервер</title>
<style>
* {box-sizing:border-box}
body{background:#1a1d23;color:#e1e4e8;font-family:'Segoe UI',Arial,sans-serif;margin:0}
header{background:#242830;padding:16px 28px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #2d323c}
header h1{font-size:18px;margin:0;color:#43b581}
header a{color:#7a8599;text-decoration:none;font-size:13px;margin-left:14px}
header a:hover{color:#fff}
main{padding:24px 28px;max-width:1000px;margin:0 auto}
section{background:#242830;border-radius:10px;padding:20px;margin-bottom:20px}
section h2{margin-top:0;font-size:15px;color:#43b581}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
button{background:#2d323c;color:#e1e4e8;border:none;padding:9px 16px;border-radius:6px;cursor:pointer;font-size:13px}
button:hover{background:#3a3f4a}
button.primary{background:#43b581;color:#fff;font-weight:bold}
button.primary:hover{background:#379768}
button.danger:hover{background:#f04747}
button:disabled{opacity:.45;cursor:not-allowed}
input[type=text]{flex:1;min-width:200px;padding:9px;border-radius:6px;border:1px solid #2d323c;background:#2d323c;color:#fff;font-size:13px}
.kv{display:grid;grid-template-columns:200px 1fr;gap:6px 12px;font-size:13px}
.kv div:nth-child(odd){color:#7a8599}
.ok{color:#43b581}.bad{color:#f04747}.warn{color:#faa61a}
pre{background:#14161a;border-radius:8px;padding:12px;font-size:12px;max-height:380px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:0}
#msg{margin-top:12px;font-size:13px;min-height:18px}
.muted{color:#7a8599;font-size:12px}
.item{background:#14161a;border-radius:8px;padding:10px 12px;margin-top:8px;display:flex;gap:12px;align-items:center;justify-content:space-between}
.item .t{font-weight:bold;font-size:13px}
.item .d{color:#7a8599;font-size:12px;margin-top:2px}
code{background:#14161a;padding:1px 5px;border-radius:4px}
.bar{background:#14161a;border-radius:6px;height:8px;margin-top:6px}
.bar div{background:#43b581;height:8px;border-radius:6px}
</style></head>
<body>
<header><h1>🖥 Minecraft-сервер</h1>
<div><a href="/admin/dashboard">← Назад в панель</a><a href="/admin/logout">Выйти</a></div></header>
<main>
<section>
  <h2>Состояние</h2>
  <div class="kv" id="kv"><div>Загрузка…</div><div></div></div>
  <div id="msg"></div>
</section>
<section>
  <h2>Управление</h2>
  <div class="row">
    <input type="text" id="ver" value="{{ default_version }}" style="max-width:140px;flex:none" title="Версия Minecraft">
    <button onclick="act('install',{version:val('ver')})">⬇ Установить / обновить Paper</button>
  </div>
  <div class="row">
    <label><input type="checkbox" id="eula"> Я принимаю <a href="https://aka.ms/MinecraftEULA" target="_blank" style="color:#43b581">EULA Mojang</a></label>
    <button onclick="acceptEula()">Принять EULA</button>
  </div>
  <div class="row">
    <label>Порт <input type="text" id="port" style="width:90px;flex:none" inputmode="numeric"></label>
    <label>Слоты <input type="text" id="slots" style="width:80px;flex:none" inputmode="numeric"></label>
    <button onclick="saveSettings()">💾 Сохранить порт и слоты</button>
  </div>
  <p class="muted">Порт нужно открыть в фаерволе VDS (TCP). Изменения применяются после перезапуска сервера.</p>
  <div class="row">
    <label>Оперативная память <input type="text" id="mem_amount" style="width:90px;flex:none" inputmode="decimal" placeholder="4"></label>
    <select id="mem_unit" style="padding:9px;border-radius:6px;background:#2d323c;color:#fff;border:1px solid #2d323c">
      <option value="G">ГБ</option><option value="M">МБ</option>
    </select>
    <button onclick="saveMemory()">💾 Сохранить память</button>
  </div>
  <p class="muted" id="mem_hint">Сколько памяти выделить серверу (Java -Xms и -Xmx). Пиши число, например 4 или 1.5 (ГБ), либо 2048 и выбери МБ. Минимум 512 МБ, больше, чем есть в системе, указать нельзя. Применится после перезапуска сервера.</p>
  <div class="row">
    <button class="primary" onclick="act('start')">▶ Запустить</button>
    <button onclick="act('restart')">⟳ Перезапустить</button>
    <button class="danger" onclick="if(confirm('Остановить сервер?'))act('stop')">■ Остановить</button>
  </div>
  <div class="row">
    <input type="text" id="cmd" placeholder="Команда консоли, например: say Привет" onkeydown="if(event.key==='Enter')sendCmd()">
    <button onclick="sendCmd()">Отправить</button>
  </div>
  <p class="muted">Установка скачивает Paper с papermc.io (около 60 МБ). Остановка ждёт до 45 секунд.</p>
</section>
<section>
  <h2>Название и версии в лаунчере</h2>
  <div class="row">
    <label>Название <input type="text" id="b_name" style="width:220px;flex:none" maxlength="40"></label>
    <label>Версии от <input type="text" id="b_min" style="width:90px;flex:none" placeholder="1.20.4"></label>
    <label>до <input type="text" id="b_max" style="width:90px;flex:none" placeholder="26.2"></label>
    <button onclick="saveBranding()">💾 Сохранить</button>
  </div>
  <p class="muted">Это видят игроки на вкладке «Серверы» в лаунчере. Диапазон версий нужен, если стоят ViaVersion и ViaBackwards: лаунчер запустит игру в подходящей версии. Пусто означает только версию сервера.</p>
</section>
<section>
  <h2>Описание сервера (MOTD)</h2>
  <div class="row">
    <textarea id="motd" rows="2" maxlength="210" placeholder="Первая строка&#10;Вторая строка (необязательно)" style="flex:1;min-width:260px;padding:9px;border-radius:6px;border:1px solid #2d323c;background:#2d323c;color:#fff;font-size:13px;font-family:inherit;resize:vertical"></textarea>
    <button onclick="saveMotd()">💾 Сохранить описание</button>
  </div>
  <p class="muted">Текст под названием сервера в списке серверов Minecraft. До двух строк, лучше не длиннее 60 символов в строке. Цвета и стили: <code>&amp;a</code>, <code>&amp;6</code>, <code>&amp;l</code> и так далее. Применится после перезапуска сервера.</p>
</section>
<section>
  <h2>Плагины (Modrinth)</h2>
  <div class="row">
    <input type="text" id="pq" placeholder="Поиск плагина…" onkeydown="if(event.key==='Enter')searchPlugins()">
    <label><input type="checkbox" id="pcompat"> только для моей версии</label>
    <button onclick="searchPlugins()">🔎 Найти</button>
  </div>
  <div class="row">
    <button class="primary" onclick="act('plugin_install_builtin',{name:'mafinauth'})">🛡 MafinAuth (вход через лаунчер)</button>
    <button onclick="installPlugin('viaversion')">ViaVersion</button>
    <button onclick="installPlugin('viabackwards')">ViaBackwards</button>
    <button onclick="installPlugin('luckperms')">LuckPerms</button>
    <button onclick="installPlugin('skinsrestorer')">SkinsRestorer (скины)</button>
  </div>
  <div class="row">
    <button onclick="installVia()">🔀 Универсальный сервер: ViaVersion + ViaBackwards</button>
    <button onclick="installAll()">⬇ Всё сразу: MafinAuth + Via</button>
  </div>
  <p class="muted">MafinAuth встроен в панель и ставится без интернета. ViaVersion и ViaBackwards скачиваются с Modrinth. После установки перезапусти сервер.</p>
  <div id="presults" class="muted" style="margin-top:12px">Нажми «Найти», чтобы показать популярные плагины.</div>
  <h2 style="margin-top:22px">Установленные</h2>
  <div id="pinstalled" class="muted">—</div>
</section>
<section>
  <h2>Миры</h2>
  <div id="worlds" class="muted">—</div>
  <div class="row">
    <label>Мир (.zip) <input type="file" id="f_world" accept=".zip"></label>
    <label>Имя папки <input type="text" id="w_name" style="width:160px;flex:none" maxlength="40" placeholder="необязательно"></label>
    <button onclick="uploadWorld()">⬆ Загрузить мир</button>
  </div>
  <div id="jobbox" class="muted" style="margin:10px 0"></div>
  <h2 style="margin-top:18px">Сброс мира с сохранением спавна</h2>
  <div class="row">
    <label>Центр X <input type="number" id="r_x" placeholder="авто" style="width:90px;flex:none"></label>
    <label>Центр Z <input type="number" id="r_z" placeholder="авто" style="width:90px;flex:none"></label>
    <label>Радиус, блоков <input type="number" id="r_rad" value="600" min="0" max="5000" style="width:90px;flex:none"></label>
    <label style="flex:none"><input type="checkbox" id="r_restart"> запустить сервер после</label>
  </div>
  <p class="muted">Если X и Z пустые, центр берётся из точки спавна мира (level.dat; для мира показана рядом с названием). Если спавн задан плагином (EssentialsX, Multiverse), впишите его координаты вручную. Кнопка «♻ Сбросить» у мира: сервер остановится, сделается бэкап, затем удалятся все блоки и сущности (в том числе динамит) Обычного мира, кроме областей вокруг указанного центра. Сид сохраняется, поэтому земля сгенерируется такой же. Незер, Энд, инвентари игроков и регионы плагинов (WorldGuard) не затрагиваются.</p>
  <h2 style="margin-top:18px">Бэкапы</h2>
  <div id="backups" class="muted">—</div>
  <p class="muted">В архиве должна быть папка мира с файлом level.dat. Загруженный мир ставится отдельной папкой и ничего не перезаписывает. Чтобы играть в нём, сделай его активным (сервер при этом остановлен) и запусти сервер. Размер архива до 500 МБ.</p>
</section>
<section>
  <h2>Файлы</h2>
  <p class="muted">Если установка не сработала, загрузи файлы вручную. Они попадут в папку сервера.</p>
  <div class="row">
    <label>Paper (.jar) <input type="file" id="f_server" accept=".jar"></label>
    <button onclick="upload('server')">⬆ Загрузить сервер</button>
  </div>
  <div class="row">
    <label>Плагин MafinAuth (.jar) <input type="file" id="f_plugin" accept=".jar"></label>
    <button onclick="upload('plugin')">⬆ Загрузить плагин</button>
  </div>
</section>
<section>
  <h2>Диагностика пинга</h2>
  <div class="row">
    <button onclick="diagnose()">🩺 Проверить сервер</button>
  </div>
  <p class="muted">Показывает нагрузку VDS, TPS и время тика (MSPT) сервера и пинг игроков. Занимает несколько секунд, сервер должен быть запущен.</p>
  <pre id="diag" style="margin-top:10px;display:none"></pre>
</section>
<section>
  <h2>Консоль</h2>
  <pre id="log">—</pre>
</section>
</main>
<script>
function val(id){return document.getElementById(id).value.trim()}
function setMsg(t,ok){const m=document.getElementById('msg');m.textContent=t;m.className=ok?'ok':'bad'}
async function call(action,body){
  const r=await fetch('/admin/minecraft/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  let d={};try{d=await r.json()}catch(e){}
  return d;
}
async function act(action,body){
  setMsg('Выполняю…',true);
  const d=await call(action,body);
  setMsg(d.success?d.message:(d.error||'Ошибка'),!!d.success);
  refresh();
}
function acceptEula(){
  if(!document.getElementById('eula').checked){setMsg('Отметь галочку согласия',false);return}
  act('accept_eula',{accept:true});
}
function esc2(t){return esc(t).split('"').join('&quot;')}
function saveBranding(){act('branding',{name:val('b_name'),version_min:val('b_min'),version_max:val('b_max')})}
async function searchPlugins(){
  const box=document.getElementById('presults');box.textContent='Ищу…';
  const c=document.getElementById('pcompat').checked?'1':'0';
  let d={};
  try{d=await (await fetch('/admin/minecraft/plugins/search?q='+encodeURIComponent(val('pq'))+'&compat='+c)).json()}catch(e){}
  if(!d.success){box.textContent=d.error||'Ошибка поиска';return}
  if(!d.results.length){box.textContent='Ничего не найдено';return}
  box.innerHTML=d.results.map(r=>'<div class="item"><div><div class="t">'+esc(r.title)+' <span class="muted">— '+esc(r.author)+', '+(Number(r.downloads)||0)+' загрузок</span></div><div class="d">'+esc(r.description)+'</div></div><button class="primary" data-install="'+esc2(r.slug)+'">Установить</button></div>').join('');
}
async function installPlugin(slug,force){
  setMsg('Устанавливаю '+slug+'…',true);
  const d=await call('plugin_install',{project:slug,force:!!force});
  if(!d.success&&d.can_force&&confirm(d.error+'. Установить всё равно?'))return installPlugin(slug,true);
  setMsg(d.success?d.message:(d.error||'Ошибка'),!!d.success);
  refresh();
}
async function installVia(){await installPlugin('viaversion');await installPlugin('viabackwards')}
async function installAll(){await call('plugin_install_builtin',{name:'mafinauth'});await installVia()}
function delPlugin(file){if(confirm('Удалить '+file+'?'))act('plugin_delete',{file:file})}
document.addEventListener('click',function(e){
  const i=e.target.closest('button[data-install]');if(i)installPlugin(i.dataset.install);
  const x=e.target.closest('button[data-del]');if(x)delPlugin(x.dataset.del);
});
let brandingLoaded=false;
function renderPlugins(list){
  const box=document.getElementById('pinstalled');
  if(!list.length){box.textContent='Плагинов нет';return}
  box.innerHTML=list.map(p=>'<div class="item"><div><div class="t">'+esc(p.name)+'</div><div class="d">'+esc(p.file)+' · '+Math.round(p.size/1024)+' КБ</div></div>'+(p.protected?'<span class="muted">🔒 защитный</span>':'<button data-del="'+esc2(p.file)+'">Удалить</button>')+'</div>').join('');
}
let motdLoaded=false,memLoaded=false;
function saveMotd(){act('motd',{motd:document.getElementById('motd').value})}
function saveMemory(){act('memory',{amount:val('mem_amount'),unit:val('mem_unit')})}
async function uploadWorld(){
  const inp=document.getElementById('f_world');
  if(!inp.files.length){setMsg('Выбери архив .zip с миром',false);return}
  const fd=new FormData();fd.append('file',inp.files[0]);fd.append('name',val('w_name'));
  setMsg('Загружаю мир…',true);
  let d={};
  try{const r=await fetch('/admin/minecraft/world/upload',{method:'POST',body:fd});d=await r.json()}catch(e){}
  setMsg(d.success?d.message:(d.error||'Ошибка загрузки'),!!d.success);
  inp.value='';refresh();
}
function renderWorlds(s){
  const box=document.getElementById('worlds');
  const list=s.worlds||[];
  const busy=s.running;
  let html='<div class="muted" style="margin-bottom:6px">Активный мир (level-name): <code>'+esc(s.active_world||'world')+'</code></div>';
  if(!list.length){box.innerHTML=html+'Миров пока нет. Папка мира появится после первого запуска сервера или загрузки архива.';return}
  html+=list.map(w=>{
    const isAct=(w===s.active_world);
    return '<div class="item"><div><div class="t">'+esc(w)+(isAct?' <span class="ok">— активный</span>':'')+'</div><div class="d">'+((s.spawns||{})[w]?'спавн мира: X '+s.spawns[w][0]+', Z '+s.spawns[w][1]:'спавн не определён (будет 0, 0)')+'</div></div><div><button data-wbak="'+esc2(w)+'">💾 Бэкап</button> <button class="danger" data-wreset="'+esc2(w)+'">♻ Сбросить</button> '+(isAct?'':'<button data-wact="'+esc2(w)+'"'+(busy?' disabled':'')+'>Сделать активным</button> <button class="danger" data-wdel="'+esc2(w)+'"'+(busy?' disabled':'')+'>Удалить</button>')+'</div></div>';
  }).join('');
  box.innerHTML=html;
  const job=s.job||{};
  const jb=document.getElementById('jobbox');
  if(jb)jb.innerHTML=job.text?('<span class="'+(job.running?'':(job.ok?'ok':'bad'))+'">'+(job.running?'⏳ ':'')+esc(job.text)+'</span>'):'';
  const bk=document.getElementById('backups');
  if(bk){
    const bl=s.backups||[];
    bk.innerHTML=bl.length?bl.map(b=>'<div class="item"><div><div class="t">'+esc(b.file)+'</div><div class="d">'+Math.round(b.size/1048576)+' МБ · '+new Date(b.time*1000).toLocaleString()+'</div></div><div><a href="/admin/minecraft/backup/'+encodeURIComponent(b.file)+'"><button>⬇ Скачать</button></a> <button class="danger" data-bdel="'+esc2(b.file)+'">Удалить</button></div></div>').join(''):'Бэкапов пока нет';
  }
}
function renderExtra(s){
  if(!motdLoaded&&typeof s.motd==='string'){document.getElementById('motd').value=s.motd;motdLoaded=true}
  if(!memLoaded&&s.memory_mb){
    const gb=s.memory_mb%1024===0;
    document.getElementById('mem_amount').value=gb?(s.memory_mb/1024):s.memory_mb;
    document.getElementById('mem_unit').value=gb?'G':'M';
    memLoaded=true;
  }
  renderWorlds(s);
}
document.addEventListener('click',function(e){
  const c=e.target.closest('button[data-wact]');
  if(c&&confirm('Сделать мир «'+c.dataset.wact+'» активным? Он загрузится при запуске сервера.'))act('world_activate',{name:c.dataset.wact});
  const bk=e.target.closest('button[data-wbak]');
  if(bk)act('world_backup',{name:bk.dataset.wbak});
  const rs=e.target.closest('button[data-wreset]');
  if(rs&&confirm('Сбросить мир «'+rs.dataset.wreset+'»? Сервер будет остановлен, сделается бэкап, затем весь Обычный мир, кроме области спавна, будет удалён и сгенерируется заново.'))act('world_reset',{name:rs.dataset.wreset,x:val('r_x'),z:val('r_z'),radius:val('r_rad'),restart:document.getElementById('r_restart').checked});
  const bd=e.target.closest('button[data-bdel]');
  if(bd&&confirm('Удалить бэкап '+bd.dataset.bdel+'?'))act('backup_delete',{file:bd.dataset.bdel});
  const d=e.target.closest('button[data-wdel]');
  if(d&&confirm('Удалить мир «'+d.dataset.wdel+'» НАВСЕГДА? Это нельзя отменить.'))act('world_delete',{name:d.dataset.wdel});
});
async function diagnose(){
  const box=document.getElementById('diag');box.style.display='block';box.textContent='Проверяю…';
  let d={};
  try{d=await (await fetch('/admin/minecraft/diagnose')).json()}catch(e){}
  if(!d.success){box.textContent=d.error||'Ошибка';return}
  const s=d.system||{};
  const head=[
    'VDS: ядер '+(s.cpus||'?')+', load '+((s.load||[]).join(' / ')||'?')+', занято '+(s.cpu_busy_pct!=null?s.cpu_busy_pct+'%':'?')+', украдено соседями '+(s.cpu_steal_pct!=null?s.cpu_steal_pct+'%':'?'),
    'Память: свободно '+(s.mem_available_mb!=null?s.mem_available_mb+' МБ из '+s.mem_total_mb+' МБ':'?'),
    '', 'Выводы:'].concat(d.hints.map(h=>' • '+h)).concat(['','Ответ сервера (tps, mspt, spark ping):']).concat(d.output.length?d.output:['(пусто, подожди пару секунд и повтори)']);
  box.textContent=head.join(String.fromCharCode(10));
}
async function upload(kind){
  const inp=document.getElementById('f_'+kind);
  if(!inp.files.length){setMsg('Выбери файл .jar',false);return}
  const fd=new FormData();fd.append('file',inp.files[0]);
  setMsg('Загружаю…',true);
  let d={};
  try{const r=await fetch('/admin/minecraft/upload/'+kind,{method:'POST',body:fd});d=await r.json()}catch(e){}
  setMsg(d.success?d.message:(d.error||'Ошибка загрузки'),!!d.success);
  inp.value='';refresh();
}
function esc(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML}
function saveSettings(){
  act('settings',{port:val('port'),max_players:val('slots')});
}
let settingsLoaded=false;
function sendCmd(){
  const c=val('cmd');if(!c)return;
  act('command',{command:c});document.getElementById('cmd').value='';
}
function yn(v,good){return '<span class="'+(v===good?'ok':'bad')+'">'+(v?'да':'нет')+'</span>'}
async function refresh(){
  try{
    const s=await (await fetch('/admin/minecraft/status')).json();
    const rows=[
      ['Папка сервера','<code>'+esc(s.dir)+'</code>'],
      ['Платформа',s.supported?'<span class="ok">Linux, управление доступно</span>':'<span class="bad">управление запуском недоступно (только Linux)</span>'],
      ['Paper установлен',yn(s.installed,true)+(s.jar_file?' — '+esc(s.jar_file):'')+(s.minecraft_version?' (Minecraft '+s.minecraft_version+')':'')],
      ['Java',(s.java_installed?s.java_installed:'<span class="bad">не найдена</span>')+(s.java_required?' (нужна '+s.java_required+'+)':'')],
      ['EULA принята',yn(s.eula,true)],
      ['Процесс',s.running?'<span class="ok">запущен</span>':'остановлен'],
      ['Принимает игроков',s.ready?'<span class="ok">да</span>':(s.running?'<span class="warn">запускается…</span>':'—')],
      ['Игроков',s.ready?(s.players_online+' / '+s.players_max):'—'],
      ['online-mode',s.online_mode==='false'?'<span class="ok">false (нужно для лаунчера)</span>':'<span class="warn">'+(s.online_mode||'не задан')+' — при запуске будет выставлено false</span>'],
      ['Память',esc(s.memory_text||'—')+(s.system_ram_mb?' <span class="muted">(в системе '+Math.round(s.system_ram_mb/102.4)/10+' ГБ)</span>':'')],
      ['Плагин MafinAuth',s.plugin_installed?'<span class="ok">найден: '+s.plugin_files.map(esc).join(', ')+'</span>':'<span class="warn">не найден. Загрузи кнопкой ниже или положи .jar в <code>'+esc(s.dir)+'/plugins</code></span>'],
      ['MAFIN_MC_SECRET',s.secret_set?'<span class="ok">задан</span>':'<span class="bad">не задан — плагин никого не пустит</span>'],
      ['Порт',s.port+(s.run_port&&s.run_port!==s.port?' <span class="warn">(работает на '+s.run_port+', новый порт после перезапуска)</span>':'')],
      ['Слоты',s.max_players]
    ];
    renderPlugins(s.plugins||[]);
    renderExtra(s);
    if(!brandingLoaded&&s.branding){
      document.getElementById('b_name').value=s.branding.name;
      document.getElementById('b_min').value=s.branding.version_min;
      document.getElementById('b_max').value=s.branding.version_max;
      brandingLoaded=true;
    }
    if(!settingsLoaded){
      document.getElementById('port').value=s.port;
      document.getElementById('slots').value=s.max_players;
      settingsLoaded=true;
    }
    document.getElementById('kv').innerHTML=rows.map(r=>'<div>'+r[0]+'</div><div>'+r[1]+'</div>').join('');
    const l=await (await fetch('/admin/minecraft/log')).json();
    const pre=document.getElementById('log');
    const atEnd=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
    pre.textContent=l.log||'—';
    if(atEnd)pre.scrollTop=pre.scrollHeight;
  }catch(e){}
}
refresh();setInterval(refresh,4000);
</script>
</body></html>
"""

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
        "online": sum(1 for r in db.execute("SELECT last_seen FROM profiles").fetchall() if is_online(r[0])),
        "coins_total": round(db.execute("SELECT COALESCE(SUM(coins), 0) FROM profiles").fetchone()[0] or 0),
    }
    upload_error = session.pop('upload_error', None)
    upload_ok = session.pop('upload_ok', None)
    pw_msg = session.pop('pw_msg', None)
    mobile = None
    apk = _site_mobile_apk()
    if apk:
        st = os.stat(apk)
        h = hashlib.sha256()
        with open(apk, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b''):
                h.update(chunk)
        mobile = {"name": MOBILE_APK_NAME, "size_mb": round(st.st_size / 1024 / 1024, 1),
                  "when": datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M'), "sha": h.hexdigest()[:12]}
    return render_template_string(
        DASHBOARD_TEMPLATE, profiles=profiles, updates=updates, stats=stats, me=g.web_admin, mobile=mobile,
        upload_error=upload_error, upload_ok=upload_ok, pw_msg=pw_msg,
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


CHUNKS_DIR = os.path.join(UPDATES_DIR, "_chunks")


class _StagedUpload:
    """Файл, уже загруженный кусками (/admin/updates/chunk): ведёт себя как FileStorage для save()/filename."""

    def __init__(self, path, filename):
        self.path = path
        self.filename = filename

    def save(self, dest):
        os.replace(self.path, dest)


def _upload_field(name):
    """Файл из обычного multipart-поля либо из поля, загруженного кусками (name_upload_id + name_upload_name)."""
    up_id = (request.form.get(name + '_upload_id') or '').strip()
    if up_id:
        if not re.fullmatch(r'[0-9a-f]{8,64}', up_id):
            return None
        path = os.path.join(CHUNKS_DIR, up_id + '.part')
        if not os.path.isfile(path):
            return None
        return _StagedUpload(path, request.form.get(name + '_upload_name') or '')
    return request.files.get(name)


@app.route('/admin/updates/chunk', methods=['POST'])
@web_admin_required
def web_upload_chunk():
    """Приём большого файла кусками по ~512 КБ. Работает даже при маленьком client_max_body_size в nginx (по умолчанию 1 МБ)."""
    up_id = (request.args.get('id') or '').strip()
    if not re.fullmatch(r'[0-9a-f]{8,64}', up_id):
        return jsonify({"error": "bad id"}), 400
    try:
        offset = int(request.args.get('offset', '0'))
    except ValueError:
        return jsonify({"error": "bad offset"}), 400
    os.makedirs(CHUNKS_DIR, exist_ok=True)
    now = time.time()
    for fn in os.listdir(CHUNKS_DIR):
        fp = os.path.join(CHUNKS_DIR, fn)
        try:
            if now - os.path.getmtime(fp) > 86400:
                os.remove(fp)
        except OSError:
            pass
    path = os.path.join(CHUNKS_DIR, up_id + '.part')
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if offset == 0:
        size = 0
        open(path, 'wb').close()
    if offset != size:
        return jsonify({"error": "offset mismatch", "size": size}), 409
    data = request.get_data(cache=False)
    if size + len(data) > MAX_UPDATE_SIZE_BYTES:
        return jsonify({"error": "file too large"}), 413
    with open(path, 'ab') as f:
        f.write(data)
    return jsonify({"size": size + len(data)})


@app.route('/admin/updates/upload', methods=['POST'])
@web_admin_required
def web_upload_update():
    if request.form.get('mobile') == '1':
        apk = _upload_field('file')
        if not apk or not (apk.filename or '').lower().endswith('.apk'):
            session['upload_error'] = "Для мобильной версии выберите файл .apk."
            return redirect(url_for('admin_dashboard'))
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        tmp = os.path.join(DOWNLOADS_DIR, MOBILE_APK_NAME + ".part")
        apk.save(tmp)
        dst = os.path.join(DOWNLOADS_DIR, MOBILE_APK_NAME)
        os.replace(tmp, dst)
        log_action("upload_mobile_apk", g.web_admin['nickname'], f"{os.path.getsize(dst)} байт")
        session['upload_ok'] = f"📱 Мобильная версия загружена ({os.path.getsize(dst) / 1024 / 1024:.1f} МБ). Она доступна по /download/mobile и в проверке обновлений мобильного лаунчера."
        return redirect(url_for('admin_dashboard'))
    version = request.form.get('version', '').strip()
    changelog = request.form.get('changelog', '').strip()
    file = _upload_field('file')
    extra_needed = request.form.get('extra_needed') == '1'
    extra_file = _upload_field('extra_file')

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
    session['upload_ok'] = f"✅ Версия {version} загружена и сделана активной ({file_size / 1024 / 1024:.1f} МБ)."
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/mobile/delete', methods=['POST'])
@web_admin_required
def web_delete_mobile():
    apk = _site_mobile_apk()
    if apk:
        os.remove(apk)
        log_action("delete_mobile_apk", g.web_admin['nickname'], MOBILE_APK_NAME)
        session['upload_ok'] = "APK мобильной версии удалён."
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

import html as _html

DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
SITE_NAME = os.environ.get("SITE_NAME", "Mafin Launcher")
SITE_CONTACT = os.environ.get("SITE_CONTACT", "mafinstudio.help@gmail.com")
SITE_DISCORD = os.environ.get("SITE_DISCORD", "")
SITE_TELEGRAM = os.environ.get("SITE_TELEGRAM", "https://t.me/mafinlauncher")

SITE_CSS = """
:root{--bg:#14181a;--card:#171b1d;--card2:#20262a;--line:#33403d;--text:#e6e9ee;--mut:#8f9b97;--acc:#43b581;--acc2:#2c8a60;--ok:#43b581;--bad:#d96b6b}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;font:16px/1.6 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:var(--bg);color:var(--text)}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:980px;margin:0 auto;padding:0 20px}
header.top{border-bottom:1px solid var(--line);background:var(--card)}
header.top .wrap{display:flex;align-items:center;gap:22px;height:60px}
.logo{font-weight:800;font-size:19px;color:var(--text)}.logo:hover{text-decoration:none}
nav{display:flex;flex-wrap:wrap;gap:6px 18px;margin-left:auto}header.top .wrap{flex-wrap:wrap;min-height:60px;padding-top:8px;padding-bottom:8px}nav a{color:var(--mut);font-size:15px}nav a:hover{color:var(--text);text-decoration:none}
.hero{padding:70px 0 40px;text-align:center}
.hero h1{font-size:44px;line-height:1.15;margin:0 0 14px}
.hero p{color:var(--mut);font-size:18px;max-width:600px;margin:0 auto 28px}
.btn{display:inline-block;border:0;cursor:pointer;font:inherit;font-weight:700;color:#fff;padding:13px 26px;border-radius:12px;background:linear-gradient(135deg,var(--acc),var(--acc2))}
.btn:hover{filter:brightness(1.1);text-decoration:none}.btn.sm{padding:8px 16px;font-size:14px;border-radius:9px}
.btn.ghost{background:var(--card2);color:var(--text);border:1px solid var(--line)}
.btn.red{background:var(--bad)}.btn[disabled]{opacity:.5;cursor:default}
.meta{color:var(--mut);font-size:14px;margin-top:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin:30px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px}
.card h3{margin:0 0 8px;font-size:18px}.card p{margin:0;color:var(--mut);font-size:15px}
.steps{counter-reset:s;list-style:none;padding:0;margin:0}
.steps li{counter-increment:s;position:relative;padding:10px 0 10px 46px;color:var(--mut)}
.steps li:before{content:counter(s);position:absolute;left:0;top:8px;width:32px;height:32px;border-radius:50%;background:var(--card2);border:1px solid var(--line);text-align:center;line-height:30px;color:var(--text);font-weight:700}
h2{margin:40px 0 6px;font-size:26px}
footer{border-top:1px solid var(--line);margin-top:60px;padding:26px 0;color:var(--mut);font-size:14px}
footer .wrap{display:flex;flex-wrap:wrap;gap:8px 22px}
.doc{padding:40px 0}.doc h1{font-size:34px;margin:0 0 6px}.doc h2{font-size:21px;margin:30px 0 8px}
.doc p,.doc li{color:var(--text);opacity:.9}.doc ul{padding-left:22px}.upd{color:var(--mut);font-size:14px}
input,select{width:100%;padding:11px 13px;border-radius:10px;border:1px solid var(--line);background:var(--card2);color:var(--text);font:inherit}
label{display:block;font-size:14px;color:var(--mut);margin:12px 0 5px}
.tabs{display:flex;gap:8px;margin-bottom:6px}.tabs button{flex:1}
.err{color:var(--bad);font-size:14px;min-height:20px;margin-top:10px}.okmsg{color:var(--ok)}
.stat{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.stat div{background:var(--card2);border-radius:12px;padding:14px}.stat b{display:block;font-size:24px}.stat span{color:var(--mut);font-size:13px}
.item{display:flex;align-items:center;gap:14px;padding:14px 0;border-top:1px solid var(--line)}
.item:first-child{border-top:0}.item .t{flex:1}.item .t small{display:block;color:var(--mut)}
.price{font-weight:800;white-space:nowrap}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:8px 6px;border-top:1px solid var(--line)}th{color:var(--mut);font-weight:600;border-top:0}
.hatview{flex:none;width:260px;height:340px;background:var(--card2);border-radius:10px;border:1px solid var(--line);margin-right:16px;overflow:hidden}
.hatview canvas{display:block;width:260px;height:340px}
.skinprev{image-rendering:pixelated;width:128px;height:128px;background:var(--card2);border-radius:10px;border:1px solid var(--line)}
.row{display:flex;gap:16px;flex-wrap:wrap}.row>*{flex:1;min-width:200px}
.btn{background:var(--acc)!important;color:#08180f!important;border:2px solid #000!important;border-radius:0!important;box-shadow:inset 0 -3px 0 var(--acc2)}
.btn.ghost{background:var(--card2)!important;color:var(--text)!important;border:1px solid var(--line)!important;box-shadow:none}
.btn.red{background:var(--bad)!important}
.card,.stat div,.hatview,.skinprev,input,select{border-radius:0!important}
.card{border:2px solid #000;box-shadow:inset 0 0 0 1px var(--line)}
.hero{text-align:left}.hero p{margin-left:0}
.steps li:before{border-radius:0!important}
input:focus,select:focus{border-color:var(--acc);outline:0}
.hero2{display:grid;grid-template-columns:1.1fr 1fr;gap:32px;align-items:start;padding:56px 0 24px}
.hero2 h1{font-size:42px;line-height:1.15;margin:6px 0 14px}.hero2 p{color:var(--mut);max-width:520px;margin:0 0 24px}
.kick{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--acc)}
.acts{display:flex;gap:10px;flex-wrap:wrap}
.spec{background:var(--card);border:2px solid #000;box-shadow:inset 0 0 0 1px var(--line)}
.spec-h{padding:12px 16px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);border-bottom:1px solid var(--line)}
.spec-r{padding:12px 16px;border-bottom:1px solid var(--line);display:grid;gap:2px}.spec-r:last-child{border:0}
.spec-r b{font-weight:600}.spec-r span{color:var(--mut);font-size:14px}
@media(max-width:760px){.hero2{grid-template-columns:1fr;padding-top:32px}.hero2 h1{font-size:32px}}
"""


def _site_page(title, body, head_extra=""):
    contact = ""
    if SITE_DISCORD:
        contact += f'<a href="{_html.escape(SITE_DISCORD)}" target="_blank" rel="noopener">Discord</a>'
    if SITE_TELEGRAM:
        contact += f'<a href="{_html.escape(SITE_TELEGRAM)}" target="_blank" rel="noopener">Telegram</a>'
    footer = (f'<footer><div class="wrap"><span>© {datetime.now().year} {_html.escape(SITE_NAME)}</span>'
              f'<a href="/terms">Условия использования</a><a href="/privacy">Политика конфиденциальности</a>'
              f'{contact}<span>Не является официальным продуктом Minecraft и не связан с Mojang или Microsoft.</span>'
              f'</div></footer>')
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(title)} — {_html.escape(SITE_NAME)}</title><style>{SITE_CSS}</style>{head_extra}</head><body>
<header class="top"><div class="wrap"><a class="logo" href="/">{_html.escape(SITE_NAME)}</a>
<nav><a href="/">Главная</a><a href="/cabinet">Личный кабинет</a><a href="/terms">Условия использования</a><a href="/privacy">Политика конфиденциальности</a><a href="/download" class="dl">Скачать</a></nav></div></header>
{body}{footer}</body></html>"""
    resp = app.response_class(page, mimetype="text/html")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


MOBILE_APK_NAME = "mafinlaunchermobile.apk"


def _site_mobile_apk():
    path = os.path.join(DOWNLOADS_DIR, MOBILE_APK_NAME)
    return path if os.path.isfile(path) else None


def _site_latest_download():
    """(папка, имя файла, версия) самого свежего файла для скачивания или None."""
    try:
        files = [f for f in os.listdir(DOWNLOADS_DIR)
                 if not f.startswith(".") and f.lower() != MOBILE_APK_NAME
                 and os.path.isfile(os.path.join(DOWNLOADS_DIR, f))]
    except OSError:
        files = []
    if files:
        files.sort(key=lambda f: os.path.getmtime(os.path.join(DOWNLOADS_DIR, f)), reverse=True)
        return DOWNLOADS_DIR, files[0], None
    row = get_db().execute(
        "SELECT filename, version FROM updates WHERE is_active = 1 ORDER BY uploaded_at DESC LIMIT 1").fetchone()
    if row:
        name = os.path.basename(row["filename"])
        if os.path.isfile(os.path.join(UPDATES_DIR, name)):
            return UPDATES_DIR, name, row["version"]
    return None


def _fmt_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


@app.route('/api/mobile/latest', methods=['GET'])
def api_mobile_latest():
    """Сведения о последнем APK для проверки обновлений в мобильном лаунчере."""
    apk = _site_mobile_apk()
    if not apk:
        return jsonify({"available": False})
    st = os.stat(apk)
    h = hashlib.sha256()
    with open(apk, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return jsonify({"available": True, "size": st.st_size, "modified": int(st.st_mtime),
                    "sha256": h.hexdigest(), "url": "/download/mobile"})


@app.route('/api/admin/mobile/upload', methods=['POST'])
@admin_required
def api_admin_mobile_upload():
    """Загрузка нового APK мобильной версии из админки (поле формы file)."""
    f = request.files.get("file")
    if not f or not (f.filename or "").lower().endswith(".apk"):
        return jsonify({"error": "Нужен файл .apk"}), 400
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    tmp = os.path.join(DOWNLOADS_DIR, MOBILE_APK_NAME + ".part")
    f.save(tmp)
    os.replace(tmp, os.path.join(DOWNLOADS_DIR, MOBILE_APK_NAME))
    return jsonify({"success": True, "size": os.path.getsize(os.path.join(DOWNLOADS_DIR, MOBILE_APK_NAME))})


@app.route('/download/mobile', methods=['GET'])
@app.route('/download/android', methods=['GET'])
def site_download_mobile():
    if not _site_mobile_apk():
        return _site_page("Скачать", '<div class="wrap doc"><h1>Мобильная версия пока недоступна</h1>'
                          '<p>Файл ещё не загружен. Загляните позже.</p></div>'), 404
    return send_from_directory(DOWNLOADS_DIR, MOBILE_APK_NAME, as_attachment=True,
                               mimetype="application/vnd.android.package-archive")


@app.route('/download', methods=['GET'])
def site_download():
    found = _site_latest_download()
    if not found:
        return _site_page("Скачать", '<div class="wrap doc"><h1>Файл пока недоступен</h1>'
                          '<p>Лаунчер ещё не загружен. Загляните позже.</p></div>'), 404
    folder, name, _ = found
    return send_from_directory(folder, name, as_attachment=True)


def _site_home_body(btn, meta):
    name = _html.escape(SITE_NAME)
    return f"""<div class="wrap home">
<section class="hero2"><div>
<div class="kick">Лаунчер Minecraft</div>
<h1>Играйте в Minecraft<br>с {name}</h1>
<p>Лаунчер с друзьями, чатом, скинами, достижениями и привилегиями на нашем сервере. Моды, сборки и ресурспаки ставятся из каталога Modrinth.</p>
<div class="acts">{btn}</div>{meta}</div>
<div class="spec">
<div class="spec-h">Что внутри</div>
<div class="spec-r"><b>Скины</b><span>Загрузите скин в лаунчере или в кабинете, его увидят игроки лаунчера</span></div>
<div class="spec-r"><b>Привилегии</b><span>Покупаются за монеты, которые дают за игру и задания</span></div>
<div class="spec-r"><b>Друзья</b><span>Список друзей, личные сообщения, подарки, топ по времени в игре</span></div>
<div class="spec-r"><b>Моды и сборки</b><span>Forge, Fabric, NeoForge, Quilt, установка из Modrinth</span></div>
<div class="spec-r"><b>P2P-игра</b><span>Мультиплеер с друзьями без VPN и аренды сервера</span></div>
</div></section>
<h2>Как начать</h2><ol class="steps">
<li>Нажмите «Скачать лаунчер» и запустите файл.</li>
<li>Зарегистрируйтесь в лаунчере или здесь, в <a href="/cabinet">личном кабинете</a>.</li>
<li>Выберите версию Minecraft и нажмите «Играть».</li></ol></div>"""


@app.route('/', methods=['GET'])
def site_index():
    found = _site_latest_download()
    if found:
        folder, name, version = found
        size = _fmt_size(os.path.getsize(os.path.join(folder, name)))
        ver = f" · версия {_html.escape(str(version))}" if version else ""
        btn = '<a class="btn" href="/download">Скачать лаунчер</a>'
        apk = _site_mobile_apk()
        if apk:
            btn += f' <a class="btn ghost" href="/download/mobile">Android · {_fmt_size(os.path.getsize(apk))}</a>'
        meta = f'<div class="meta">{_html.escape(name)} · {size}{ver} · Windows</div>'
    else:
        btn = '<span class="btn" style="opacity:.5">Скоро</span>'
        apk = _site_mobile_apk()
        if apk:
            btn += f' <a class="btn ghost" href="/download/mobile">Android · {_fmt_size(os.path.getsize(apk))}</a>'
        meta = '<div class="meta">Файл лаунчера ещё не загружен</div>'
    body = _site_home_body(btn, meta)
    return _site_page("Главная", body)


def _contact_html():
    parts = []
    if SITE_CONTACT:
        c = _html.escape(SITE_CONTACT)
        parts.append(f'<a href="{c}">{c}</a>' if SITE_CONTACT.startswith("http") else
                     f'<a href="mailto:{c}">{c}</a>' if "@" in SITE_CONTACT else c)
    if SITE_DISCORD:
        d = _html.escape(SITE_DISCORD)
        parts.append(f'Discord: <a href="{d}" target="_blank" rel="noopener">{d}</a>')
    if SITE_TELEGRAM:
        t = _html.escape(SITE_TELEGRAM)
        parts.append(f'Telegram: <a href="{t}" target="_blank" rel="noopener">{t}</a>')
    return " · ".join(parts) if parts else "через сообщество проекта (ссылка на Discord указана на этом сайте)"


@app.route('/terms', methods=['GET'])
def site_terms():
    n = _html.escape(SITE_NAME)
    body = f"""<div class="wrap doc"><h1>Условия использования</h1><p class="upd">Редакция от {datetime.now().strftime('%d.%m.%Y')}</p>
<p>Используя лаунчер {n}, сайт и связанные сервисы (далее — «Сервис»), вы соглашаетесь с этими условиями. Если вы не согласны, не пользуйтесь Сервисом.</p>
<h2>1. Что такое Сервис</h2><p>{n} — неофициальный лаунчер для Minecraft с аккаунтами, друзьями, чатом, достижениями, скинами и магазином виртуальных привилегий для нашего игрового сервера. Сервис не связан с Mojang Studios и Microsoft; Minecraft является товарным знаком Mojang Studios.</p>
<h2>2. Аккаунт</h2><ul><li>Вы отвечаете за сохранность пароля и за действия под своим аккаунтом.</li>
<li>Нельзя выдавать себя за других людей, занимать чужие ники и создавать аккаунты для обхода блокировки.</li>
<li>Мы можем заблокировать или удалить аккаунт за нарушение этих условий и правил игрового сервера.</li></ul>
<h2>3. Правила поведения</h2><p>Запрещено: читерство и использование эксплойтов, оскорбления и травля в чате, спам, распространение вредоносных файлов, попытки взлома Сервиса, загрузка скинов и другого контента с оскорбительным, непристойным или нарушающим чужие права содержанием.</p>
<h2>4. Скины и пользовательский контент</h2><p>Загружая скин, вы подтверждаете, что имеете право его использовать. Скин становится виден другим игрокам лаунчера. Мы вправе удалить любой контент без объяснения причин.</p>
<h2>5. Монеты и привилегии</h2><ul><li>Монеты — внутренняя игровая валюта, она не является деньгами, не имеет денежной стоимости, не обменивается на деньги и не передаётся вне Сервиса.</li>
<li>Привилегии — виртуальные цифровые услуги на ограниченный срок, который указан в магазине. После выдачи привилегии возврат монет не производится, кроме случаев технической ошибки Сервиса.</li>
<li>При блокировке аккаунта за нарушения монеты и привилегии не компенсируются.</li></ul>
<h2>6. Отказ от гарантий</h2><p>Сервис предоставляется «как есть». Мы стараемся обеспечить стабильную работу, но не гарантируем отсутствие сбоев, потери данных и непрерывную доступность. Вы пользуетесь Сервисом на свой риск, ответственность Сервиса ограничена в пределах, допускаемых законом.</p>
<h2>7. Изменения</h2><p>Мы можем изменять эти условия и работу Сервиса. Актуальная редакция всегда опубликована на этой странице; продолжая пользоваться Сервисом, вы принимаете изменения.</p>
<h2>8. Контакты</h2><p>По вопросам, жалобам и удалению аккаунта: {_contact_html()}.</p></div>"""
    return _site_page("Условия использования", body)


@app.route('/privacy', methods=['GET'])
def site_privacy():
    n = _html.escape(SITE_NAME)
    body = f"""<div class="wrap doc"><h1>Политика конфиденциальности</h1><p class="upd">Редакция от {datetime.now().strftime('%d.%m.%Y')}</p>
<p>Здесь описано, какие данные собирает {n} (лаунчер, сайт и игровой сервер) и как они используются.</p>
<h2>1. Какие данные мы храним</h2><ul>
<li><b>Аккаунт:</b> ник, хэш пароля (сам пароль не хранится), дата регистрации, время последнего входа и последней активности.</li>
<li><b>Игровая статистика:</b> время в игре, число запусков, версии Minecraft, достижения, задания, монеты, покупки привилегий, подарки.</li>
<li><b>Социальные функции:</b> список друзей и заявки, личные сообщения между пользователями, отправленные подарки.</li>
<li><b>Скины:</b> загруженный вами PNG-файл скина и игровой ник, к которому он привязан.</li>
<li><b>Технические данные:</b> IP-адрес при обращении к серверу (в журналах и для связи игровой сессии с аккаунтом), записи о действиях (вход, покупка, действия администраторов).</li></ul>
<h2>2. Зачем мы это используем</h2><p>Для работы аккаунта и функций лаунчера, показа скинов, выдачи купленных привилегий на игровом сервере, защиты от мошенничества и злоупотреблений, а также для расследования нарушений.</p>
<h2>3. Передача третьим лицам</h2><p>Мы не продаём и не передаём ваши данные рекламным сетям. Игровой ник и купленная привилегия передаются нашему игровому серверу. Для загрузки лаунчер может скачивать файлы Minecraft с серверов Mojang/Microsoft и модуль скинов с GitHub — при этом эти сервисы видят ваш IP-адрес по своим правилам.</p>
<h2>4. Discord</h2><p>Если в лаунчере включён Discord Rich Presence, статус игры отображается через локальное приложение Discord на вашем компьютере. Лаунчер не получает доступ к вашей переписке и учётной записи Discord и не отправляет ваш пароль или токен Discord на наш сервер.</p>
<h2>5. Хранение и защита</h2><p>Пароли хранятся в виде хэшей. Данные находятся на нашем сервере; доступ к ним имеют только администраторы проекта. Аккаунт хранится, пока вы им пользуетесь или пока не запросите удаление.</p>
<h2>6. Ваши права</h2><p>Вы можете в любой момент запросить копию своих данных, их исправление или удаление аккаунта вместе со связанными данными (друзья, сообщения, скины, статистика). Напишите нам: {_contact_html()}. Мы удалим данные в разумный срок, кроме тех, которые обязаны хранить по закону или для защиты от нарушителей.</p>
<h2>7. Дети</h2><p>Сервисом могут пользоваться только лица, достигшие возраста, с которого они вправе самостоятельно принимать такие условия, либо с согласия родителей.</p>
<h2>8. Изменения</h2><p>Мы можем обновлять эту политику; актуальная редакция всегда на этой странице.</p></div>"""
    return _site_page("Политика конфиденциальности", body)


CABINET_JS = r"""
const $=s=>document.querySelector(s);
const el=(t,a={},...k)=>{const e=document.createElement(t);for(const[x,v]of Object.entries(a)){if(x==='class')e.className=v;else if(x.startsWith('on'))e.addEventListener(x.slice(2),v);else e.setAttribute(x,v)}for(const c of k)e.append(c);return e};
let token=localStorage.getItem('mafin_token')||'';
async function api(path,opt={}){
  const r=await fetch(path,{method:opt.body?'POST':'GET',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:opt.body?JSON.stringify(opt.body):undefined});
  let d={};try{d=await r.json()}catch(e){}
  if(r.status===401&&token){logout(true)}
  if(!r.ok)throw new Error(d.error||('Ошибка '+r.status));
  return d}
function logout(silent){if(!silent&&token)api('/api/logout',{body:{}}).catch(()=>{});token='';localStorage.removeItem('mafin_token');render()}
function fmtTime(m){m=Math.round(m||0);return Math.floor(m/60)+' ч '+(m%60)+' мин'}
function authView(){
  let mode='login';
  const err=el('div',{class:'err'});
  const nick=el('input',{autocomplete:'username',maxlength:20}),pass=el('input',{type:'password',autocomplete:'current-password'});
  const go=el('button',{class:'btn',style:'width:100%;margin-top:14px'},'Войти');
  const tl=el('button',{class:'btn sm',type:'button'},'Вход'),tr=el('button',{class:'btn sm ghost',type:'button'},'Регистрация');
  const setMode=m=>{mode=m;go.textContent=m==='login'?'Войти':'Создать аккаунт';tl.className='btn sm'+(m==='login'?'':' ghost');tr.className='btn sm'+(m==='login'?' ghost':'');err.textContent=''};
  tl.onclick=()=>setMode('login');tr.onclick=()=>setMode('register');
  const submit=async()=>{err.textContent='';go.disabled=true;
    try{const d=await api(mode==='login'?'/api/login':'/api/register',{body:{nickname:nick.value.trim(),password:pass.value}});
      token=d.token;localStorage.setItem('mafin_token',token);render()}
    catch(e){err.textContent=e.message}finally{go.disabled=false}};
  go.onclick=submit;pass.addEventListener('keydown',e=>{if(e.key==='Enter')submit()});
  return el('div',{class:'card',style:'max-width:420px;margin:40px auto'},
    el('h3',{},'Личный кабинет'),el('p',{},'Войдите тем же ником и паролем, что и в лаунчере.'),
    el('div',{class:'tabs',style:'margin-top:14px'},tl,tr),
    el('label',{},'Ник'),nick,el('label',{},'Пароль'),pass,go,err)}
function setBoxUV(geo,u,v,w,h,d,tw,th){
  const reg=[[u+d+w,v+d,d,h],[u,v+d,d,h],[u+d,v,w,d],[u+d+w,v,w,d],[u+d,v+d,w,h],[u+d+w+d,v+d,w,h]];
  const uv=geo.attributes.uv;
  reg.forEach((r,i)=>{const x0=r[0]/tw,x1=(r[0]+r[2])/tw,y1=1-r[1]/th,y0=1-(r[1]+r[3])/th,k=i*4;
    uv.setXY(k,x0,y1);uv.setXY(k+1,x1,y1);uv.setXY(k+2,x0,y0);uv.setXY(k+3,x1,y0)});
  uv.needsUpdate=true}
function pixTex(T,src){const t=new T.Texture(src);t.magFilter=T.NearestFilter;t.minFilter=T.NearestFilter;t.generateMipmaps=false;t.needsUpdate=true;return t}
function defaultSkinCanvas(){
  const c=document.createElement('canvas');c.width=64;c.height=64;const x=c.getContext('2d');
  const fill=(col,a,b,w,h)=>{x.fillStyle=col;x.fillRect(a,b,w,h)};
  fill('#c58c63',0,0,32,16);fill('#4b2e1a',8,0,8,8);fill('#4b2e1a',0,8,32,4);fill('#4b2e1a',24,8,8,8);
  fill('#222',10,12,2,2);fill('#222',14,12,2,2);fill('#8a5a3c',11,14,4,1);
  fill('#2f8f9d',16,16,24,16);fill('#c58c63',40,16,16,16);fill('#2f8f9d',44,16,8,4);
  fill('#3b4cc0',0,16,16,16);fill('#c58c63',32,48,16,16);fill('#2f8f9d',36,48,8,4);fill('#3b4cc0',16,48,16,16);
  return c}
function mkSkinViewer(box){
  const T=window.THREE;
  if(!T){box.append(el('p',{style:'color:var(--mut)'},'3D-просмотр не загрузился'));return null}
  const W=260,H=340;
  const renderer=new T.WebGLRenderer({antialias:true,alpha:true,preserveDrawingBuffer:true});
  renderer.setSize(W,H);renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
  box.append(renderer.domElement);
  const scene=new T.Scene();
  const cam=new T.PerspectiveCamera(32,W/H,1,500);cam.position.set(0,22,118);cam.lookAt(0,22,0);
  scene.add(new T.AmbientLight(0xffffff,0.9));
  const dl=new T.DirectionalLight(0xffffff,0.45);dl.position.set(30,50,70);scene.add(dl);
  const pivot=new T.Group();scene.add(pivot);
  let player=null,hat=null,drag=false,lastX=0,idle=true;
  const el_=renderer.domElement;el_.style.cursor='grab';el_.style.touchAction='none';
  el_.addEventListener('pointerdown',e=>{drag=true;idle=false;lastX=e.clientX;el_.setPointerCapture(e.pointerId)});
  el_.addEventListener('pointermove',e=>{if(drag){pivot.rotation.y+=(e.clientX-lastX)*0.012;lastX=e.clientX}});
  const end=()=>{drag=false};el_.addEventListener('pointerup',end);el_.addEventListener('pointercancel',end);
  function mat(tex,alpha){return new T.MeshLambertMaterial({map:tex,transparent:!!alpha,alphaTest:alpha?0.02:0})}
  function part(w,h,d,u,v,ou,ov,x,y,z,tex,tw,th,over,grow){
    const g=new T.Group(),geo=new T.BoxGeometry(w,h,d);setBoxUV(geo,u,v,w,h,d,tw,th);g.add(new T.Mesh(geo,mat(tex,false)));
    if(over){const og=new T.BoxGeometry(w+grow,h+grow,d+grow);setBoxUV(og,ou,ov,w,h,d,tw,th);g.add(new T.Mesh(og,mat(tex,true)))}
    g.position.set(x,y,z);return g}
  function setSkin(src,slim){
    if(player)pivot.remove(player);
    const tw=src.width,th=src.height,tex=pixTex(T,src),old=th<64,aw=slim?3:4,ax=slim?5.5:6;
    player=new T.Group();
    player.add(part(8,8,8,0,0,32,0,0,28,0,tex,tw,th,true,1));
    player.add(part(8,12,4,16,16,16,32,0,18,0,tex,tw,th,!old,0.5));
    player.add(part(aw,12,4,40,16,40,32,-(4+aw/2),18,0,tex,tw,th,!old,0.5));
    player.add(part(aw,12,4,old?40:32,old?16:48,48,48,4+aw/2,18,0,tex,tw,th,!old,0.5));
    player.add(part(4,12,4,0,16,0,32,-2,6,0,tex,tw,th,!old,0.5));
    player.add(part(4,12,4,old?0:16,old?16:48,0,48,2,6,0,tex,tw,th,!old,0.5));
    pivot.add(player)}
  function matCanvas(name){
    const c=document.createElement('canvas');c.width=16;c.height=16;const x=c.getContext('2d');
    let h=0;for(const ch of name)h=(h*31+ch.charCodeAt(0))%360;
    const base=/PUMPKIN/.test(name)?'#d9822b':/GOLD/.test(name)?'#f5d03a':/DIAMOND/.test(name)?'#4fe0d6':/IRON/.test(name)?'#cfcfcf':/LEATHER/.test(name)?'#9a5a2e':'hsl('+h+',45%,50%)';
    x.fillStyle=base;x.fillRect(0,0,16,16);
    for(let i=0;i<40;i++){x.fillStyle='rgba(0,0,0,'+(Math.random()*0.12)+')';x.fillRect(Math.random()*16|0,Math.random()*16|0,2,2)}
    if(/CARVED_PUMPKIN/.test(name)){x.fillStyle='#2a1500';x.fillRect(3,4,3,3);x.fillRect(10,4,3,3);x.fillRect(4,10,8,2);x.fillRect(3,9,2,1);x.fillRect(11,9,2,1)}
    return c}
  function setHat(s){
    if(hat){pivot.remove(hat);hat=null}
    if(!s)return;
    const size=Math.max(3,s.scale*16),g=new T.Group();
    if(s.img){
      const tw=s.img.width,th=s.img.height,tex=pixTex(T,s.img),geo=new T.BoxGeometry(size,size,size);
      setBoxUV(geo,0,0,8,8,8,tw,th);g.add(new T.Mesh(geo,mat(tex,false)));
      const og=new T.BoxGeometry(size*1.06,size*1.06,size*1.06);setBoxUV(og,32,0,8,8,8,tw,th);g.add(new T.Mesh(og,mat(tex,true)))}
    else{
      const tex=pixTex(T,matCanvas(s.material||''));
      g.add(new T.Mesh(new T.BoxGeometry(size,size,size),mat(tex,false)))}
    g.position.set(0,32+size/2+s.y*16,0);hat=g;pivot.add(g)}
  setSkin(defaultSkinCanvas(),false);
  let seen=false;
  function tick(){
    if(box.isConnected)seen=true;else if(seen)return;
    if(idle&&!drag)pivot.rotation.y+=0.006;
    renderer.render(scene,cam);requestAnimationFrame(tick)}
  tick();
  return {setSkin,setHat,pivot,renderer,scene,cam}}
function mkHatSection(me,sname,up,del){
  const cs=el('div',{class:'card',style:'margin-top:16px'});
  cs.append(el('h3',{},'🎩 Шапки и предпросмотр'),el('p',{},'Крутите модель мышью. Предпросмотр примерный — в игре шапка может выглядеть чуть иначе.'));
  const vbox=el('div',{class:'hatview',style:'flex:none;width:260px;max-width:100%'});
  const viewer=mkSkinViewer(vbox);
  const msg=el('div',{class:'err'});
  const list=el('div',{style:'margin-top:8px'});
  const ctl=el('div',{style:'margin-top:12px'});
  const side=el('div',{style:'flex:1;min-width:240px'});
  side.append(ctl,list,msg);
  cs.append(el('div',{class:'row',style:'align-items:flex-start'},vbox,side));
  let data=null,adj={y:0,s:0},saveTimer=null,curImg=null;
  async function refreshSkin(){
    if(!viewer)return;
    try{
      const d=await api('/api/skin/mine?game_name='+encodeURIComponent(sname.value.trim()));
      if(d.skin){
        const img=new Image();
        img.onload=()=>viewer.setSkin(img,d.skin.model==='slim');
        img.src='/yggdrasil/textures/'+d.skin.hash
      }else viewer.setSkin(defaultSkinCanvas(),false)
    }catch(e){}}
  function applyHat(){
    if(!viewer||!data)return;
    const it=data.items.find(i=>i.equipped);
    if(!it){viewer.setHat(null);return}
    const spec=img=>({img,material:it.material,scale:Math.min(Math.max(it.scale+adj.s,0.2),2),y:it.y_offset+adj.y});
    if(it.has_texture){
      if(curImg&&curImg.id===it.id&&curImg.img){viewer.setHat(spec(curImg.img));return}
      const img=new Image();
      img.onload=()=>{curImg={id:it.id,img};viewer.setHat(spec(img))};
      img.onerror=()=>viewer.setHat(spec(null));
      img.src='/api/cosmetics/texture/'+it.id
    }else viewer.setHat(spec(null))}
  function queueSave(){
    applyHat();drawCtl();
    clearTimeout(saveTimer);
    saveTimer=setTimeout(async()=>{try{await api('/api/cosmetics/adjust',{body:{y_adj:adj.y,scale_adj:adj.s}})}catch(e){msg.className='err';msg.textContent=e.message}},450)}
  function drawCtl(){
    ctl.replaceChildren();
    const it=data&&data.items.find(i=>i.equipped);
    if(!it){ctl.append(el('p',{style:'color:var(--mut)'},'Наденьте купленную шапку, чтобы настроить её положение.'));return}
    const step=(label,dec,inc,val)=>el('div',{style:'display:flex;align-items:center;gap:8px;margin:6px 0'},
      el('span',{style:'width:70px'},label),
      el('button',{class:'btn sm ghost',type:'button',onclick:dec},label==='Высота'?'▼':'◀'),
      el('span',{style:'min-width:56px;text-align:center'},val),
      el('button',{class:'btn sm ghost',type:'button',onclick:inc},label==='Высота'?'▲':'▶'));
    const r=v=>Math.round(v*100)/100;
    ctl.append(
      step('Высота',()=>{adj.y=r(Math.max(adj.y-0.05,-0.6));queueSave()},()=>{adj.y=r(Math.min(adj.y+0.05,0.6));queueSave()},(adj.y>0?'+':'')+adj.y.toFixed(2)),
      step('Размер',()=>{adj.s=r(Math.max(adj.s-0.05,-0.5));queueSave()},()=>{adj.s=r(Math.min(adj.s+0.05,0.8));queueSave()},(adj.s>0?'+':'')+adj.s.toFixed(2)),
      el('button',{class:'btn sm ghost',type:'button',onclick:()=>{adj={y:0,s:0};queueSave()}},'Сбросить'))}
  async function load(){
    data=await api('/api/cosmetics');
    adj={y:data.y_adj||0,s:data.scale_adj||0};
    list.replaceChildren();
    if(!data.items.length)list.append(el('p',{style:'color:var(--mut)'},'Сейчас в магазине нет шапок.'));
    for(const it of data.items){
      const acts=el('div',{style:'display:flex;gap:6px'});
      if(!it.owned){
        const b=el('button',{class:'btn sm',type:'button'},'Купить');
        b.onclick=async()=>{
          if(!confirm('Купить «'+it.title+'» за '+it.price+' монет?'))return;
          b.disabled=true;msg.className='err';msg.textContent='';
          try{const r=await api('/api/cosmetics/buy',{body:{id:it.id}});
            msg.className='err okmsg';msg.textContent='Куплено! Осталось монет: '+Math.floor(r.total_coins);await load()}
          catch(e){msg.textContent=e.message}finally{b.disabled=false}};
        acts.append(b)
      }else{
        const b=el('button',{class:'btn sm'+(it.equipped?' ghost':''),type:'button'},it.equipped?'Снять':'Надеть');
        b.onclick=async()=>{
          msg.className='err';msg.textContent='';
          try{await api('/api/cosmetics/equip',{body:{id:it.equipped?null:it.id}});await load()}
          catch(e){msg.textContent=e.message}};
        acts.append(b)}
      list.append(el('div',{class:'item'},el('div',{class:'t'},it.title,el('small',{},it.description||'')),el('div',{class:'price'},it.owned?'✔ куплено':it.price+' 🪙'),acts))}
    applyHat();drawCtl()}
  const upOrig=up.onclick,delOrig=del.onclick;
  up.onclick=async()=>{await upOrig();refreshSkin()};
  del.onclick=async()=>{await delOrig();refreshSkin()};
  sname.addEventListener('change',refreshSkin);
  refreshSkin();
  load().catch(e=>{msg.textContent=e.message});
  return cs}
async function mkTgSection(){
  const tg=el('div',{class:'card',style:'margin-top:16px'});
  tg.append(el('h3',{},'✈️ Telegram'),el('p',{},'Привяжите аккаунт, чтобы получать уведомления, когда друзья заходят в сеть, и новости.'));
  const list=el('div',{});
  const msg=el('div',{class:'err'});
  tg.append(list,msg);
  let timer=null;
  async function load(){
    let d;
    try{d=await api('/api/tg/status')}catch(e){msg.textContent=e.message;return}
    list.replaceChildren();
    let any=false;
    for(const [bot,v] of Object.entries(d)){
      if(!v.enabled)continue;
      any=true;
      const acts=el('div',{style:'display:flex;gap:6px;align-items:center'});
      if(v.linked){
        const b=el('button',{class:'btn sm red',type:'button'},'Отвязать');
        b.onclick=async()=>{if(!confirm('Отвязать Telegram ('+v.title+')?'))return;
          try{await api('/api/tg/unlink',{body:{bot}});await load()}catch(e){msg.textContent=e.message}};
        acts.append(b)
      }else{
        const b=el('button',{class:'btn sm',type:'button'},'Привязать');
        b.onclick=async()=>{
          msg.className='err';msg.textContent='';b.disabled=true;
          try{
            const r=await api('/api/tg/link',{body:{bot}});
            acts.replaceChildren(el('a',{class:'btn sm',href:r.url,target:'_blank',rel:'noopener'},'Открыть Telegram'));
            let n=0;clearInterval(timer);
            timer=setInterval(async()=>{n++;if(!list.isConnected||n>100){clearInterval(timer);return}
              try{const s=await api('/api/tg/status');if(s[bot]&&s[bot].linked){clearInterval(timer);await load()}}catch(e){}},6000)
          }catch(e){msg.textContent=e.message;b.disabled=false}};
        acts.append(b)}
      list.append(el('div',{class:'item'},el('div',{class:'t'},v.title,el('small',{},v.linked?'Привязан':'Не привязан')),el('div',{}),acts))}
    if(!any)list.append(el('p',{style:'color:var(--mut)'},'Telegram-боты на сервере не настроены.'))}
  await load();
  return tg}
async function cabinetView(){
  const me=await api('/api/me');
  const root=el('div',{});
  const out=el('button',{class:'btn sm ghost',onclick:()=>logout()},'Выйти');
  root.append(el('div',{style:'display:flex;align-items:center;gap:12px;margin:30px 0 0'},el('h2',{style:'margin:0;flex:1'},'👤 '+me.nickname),out));
  const stats=el('div',{class:'stat'});
  const st=(v,l)=>el('div',{},el('b',{},String(v)),el('span',{},l));
  stats.append(st(Math.floor(me.coins||0),'монет'),st(fmtTime(me.total_playtime_minutes),'в игре'),st(me.total_launches||0,'запусков'),st((me.created_at||'').slice(0,10),'регистрация'));
  root.append(stats);
  const shop=el('div',{class:'card',style:'margin-bottom:16px'});
  shop.append(el('h3',{},'👑 Привилегии'));
  const gname=el('input',{value:me.nickname.replace(/[^A-Za-z0-9_]/g,'').slice(0,16),maxlength:16});
  shop.append(el('label',{},'Игровой ник на сервере'),gname);
  const msg=el('div',{class:'err'});
  const list=el('div',{style:'margin-top:8px'});
  shop.append(list,msg);
  root.append(shop);
  async function loadShop(){
    const d=await api('/api/shop/items');
    list.replaceChildren();
    if(!d.items.length)list.append(el('p',{style:'color:var(--mut)'},'Сейчас в магазине нет товаров.'));
    for(const it of d.items){
      const b=el('button',{class:'btn sm'},'Купить');
      b.onclick=async()=>{
        if(!confirm('Купить «'+it.title+'» за '+it.price+' монет для ника '+gname.value+'?'))return;
        b.disabled=true;msg.className='err';msg.textContent='';
        try{const r=await api('/api/shop/buy',{body:{item_id:it.id,game_name:gname.value.trim()}});
          msg.className='err okmsg';msg.textContent='Куплено! Осталось монет: '+Math.floor(r.total_coins)+(r.status==='pending'?' (привилегия выдастся, когда сервер будет в сети)':'');
          loadShop();me.coins=r.total_coins;stats.firstChild.firstChild.textContent=Math.floor(r.total_coins)}
        catch(e){msg.textContent=e.message}finally{b.disabled=false}};
      list.append(el('div',{class:'item'},el('div',{class:'t'},it.title,el('small',{},(it.description||'')+(it.duration_days?' · '+it.duration_days+' дн.':''))),el('div',{class:'price'},it.price+' 🪙'),b))}
    if(d.purchases.length){
      const tb=el('table',{},el('tr',{},...['Товар','Ник','Цена','Статус'].map(h=>el('th',{},h))));
      for(const p of d.purchases)tb.append(el('tr',{},el('td',{},p.item_title),el('td',{},p.game_name),el('td',{},String(p.price)),el('td',{},p.status)));
      list.append(el('h3',{style:'margin-top:22px'},'Мои покупки'),tb)}}
  await loadShop();
  const sk=el('div',{class:'card'});
  sk.append(el('h3',{},'🧑 Скин'),el('p',{},'PNG 64×64 или 64×32. Виден игрокам, которые играют через лаунчер.'));
  const sname=el('input',{value:gname.value,maxlength:16}),model=el('select',{},el('option',{value:'default'},'Классические руки (Steve)'),el('option',{value:'slim'},'Тонкие руки (Alex)'));
  const file=el('input',{type:'file',accept:'image/png'});
  const prev=el('img',{class:'skinprev',alt:''});prev.style.display='none';
  const smsg=el('div',{class:'err'});
  let b64='';
  file.onchange=()=>{smsg.className='err';smsg.textContent='';b64='';prev.style.display='none';const f=file.files[0];if(!f)return;
    if(f.size>256*1024){smsg.textContent='Файл больше 256 КБ';return}
    const fr=new FileReader();fr.onload=()=>{const img=new Image();img.onload=()=>{
      if(!((img.width===64&&img.height===64)||(img.width===64&&img.height===32))){smsg.textContent='Нужен PNG 64×64 или 64×32, а у вас '+img.width+'×'+img.height;return}
      b64=fr.result.split(',')[1];prev.src=fr.result;prev.style.display='block'};img.onerror=()=>{smsg.textContent='Это не PNG-картинка'};img.src=fr.result};fr.readAsDataURL(f)};
  const up=el('button',{class:'btn sm',style:'margin-top:14px'},'Загрузить скин'),del=el('button',{class:'btn sm red',style:'margin:14px 0 0 8px'},'Удалить скин');
  up.onclick=async()=>{smsg.className='err';smsg.textContent='';if(!b64){smsg.textContent='Сначала выберите PNG-файл';return}
    try{await api('/api/skin/upload',{body:{game_name:sname.value.trim(),png_b64:b64,model:model.value}});smsg.className='err okmsg';smsg.textContent='Скин загружен!'}catch(e){smsg.textContent=e.message}};
  del.onclick=async()=>{if(!confirm('Удалить скин для ника '+sname.value+'?'))return;try{await api('/api/skin/delete',{body:{game_name:sname.value.trim()}});smsg.className='err okmsg';smsg.textContent='Скин удалён'}catch(e){smsg.textContent=e.message}};
  sk.append(el('div',{class:'row'},el('div',{},el('label',{},'Игровой ник'),sname,el('label',{},'Руки'),model,el('label',{},'Файл'),file),el('div',{},prev)),up,del,smsg);
  root.append(sk);
  const hv=mkHatSection(me,sname,up,del);
  root.append(hv);
  root.append(await mkTgSection());
  return root}
async function render(){
  const box=$('#app');box.replaceChildren();
  if(!token){box.append(authView());return}
  try{box.append(await cabinetView())}catch(e){if(token){box.append(el('div',{class:'card'},'Не удалось загрузить кабинет: '+e.message))}else render()}}
render();
"""


@app.route('/cabinet', methods=['GET'])
def site_cabinet():
    body = '<div class="wrap"><div id="app"></div></div><script>' + CABINET_JS + '</script>'
    resp = _site_page("Личный кабинет", body, head_extra='<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>')
    resp.headers["Cache-Control"] = "no-store"
    return resp


HUB_CSS = r""":root{--bg:#14181a;--bg2:#171b1d;--bg3:#2a3236;--tx:#e6e9ee;--mu:#8f9b97;--ac:#43b581;--bad:#d96b6b;--r:0px;--ln:#33403d}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--tx);font-family:'Segoe UI',system-ui,Arial,sans-serif;font-size:15px}
#app{min-height:100vh;display:flex;flex-direction:column;max-width:720px;margin:0 auto}
.top{position:sticky;top:0;z-index:5;display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:var(--bg2);border-bottom:1px solid var(--bg3)}
.brand{font-weight:700;color:var(--ac);font-size:17px}.who{display:flex;gap:12px;align-items:center;font-size:13px}
main{flex:1;padding:12px 12px 90px;display:flex;flex-direction:column;gap:10px}
.tabs{position:fixed;bottom:0;left:0;right:0;z-index:6;display:flex;overflow-x:auto;background:var(--bg2);border-top:1px solid var(--bg3);padding:4px 4px calc(4px + env(safe-area-inset-bottom));scrollbar-width:none;max-width:720px;margin:0 auto}
.tabs::-webkit-scrollbar{display:none}
.tab{flex:0 0 auto;min-width:64px;background:none;border:0;color:var(--mu);display:flex;flex-direction:column;align-items:center;gap:1px;padding:5px 6px;border-radius:10px;font:inherit;cursor:pointer}
.tab span{font-size:20px}.tab small{font-size:10.5px}.tab.on{color:var(--ac);background:var(--bg3)}
.card{background:var(--bg2);border:1px solid var(--bg3);border-radius:var(--r);padding:12px 14px;display:flex;flex-direction:column;gap:8px}
.card h2,.card h3,.card p{margin:0}.card h3{font-size:15px}
.muted{color:var(--mu)}.ok{color:var(--ac)}.err{color:var(--bad);font-size:13px}.pre{white-space:pre-wrap;word-break:break-word}
.row{display:flex;gap:8px;align-items:center}.row.between{justify-content:space-between}.row.wrap{flex-wrap:wrap}.grow{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
input,textarea,select{width:100%;background:var(--bg);color:var(--tx);border:1px solid var(--bg3);border-radius:10px;padding:10px 12px;font:inherit}
input[type=checkbox]{width:auto}input[type=file]{padding:8px}
textarea{resize:vertical}
.btn{display:inline-block;text-align:center;text-decoration:none;background:var(--ac);color:#fff;border:0;border-radius:10px;padding:11px 16px;font:inherit;font-weight:600;cursor:pointer}
.btn.sm{padding:7px 12px;font-size:13.5px}.btn.wide{width:100%}.btn.ghost{background:var(--bg3);color:var(--tx)}.btn:disabled{opacity:.5}
.stat{display:flex;justify-content:space-between;gap:10px;padding:2px 0}
.li{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--bg3)}.li:last-child{border:0}.li .grow{cursor:pointer}
.log{font-size:12.5px;align-items:flex-start;flex-direction:column;gap:0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#555;margin-right:2px}.dot.on{background:var(--ac)}
.chk{display:flex;gap:8px;align-items:center}
.auth{margin:36px 12px;gap:10px}
.toast{position:fixed;left:50%;bottom:84px;transform:translateX(-50%);background:var(--ac);color:#fff;padding:10px 16px;border-radius:10px;z-index:20;max-width:88vw;font-size:14px}.toast.bad{background:var(--bad)}
.giftrow,.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(88px,1fr));gap:8px}
.grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.gift{display:flex;flex-direction:column;align-items:center;text-align:center;gap:3px}.gift img{width:56px;height:56px;object-fit:contain;image-rendering:pixelated}
.gift.big img{width:84px;height:84px}
.bar{height:8px;background:var(--bg3);border-radius:6px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ac)}
.ach{flex-direction:row;align-items:center;opacity:.55}.ach.on{opacity:1;border-color:var(--ac)}.aic{font-size:28px;width:40px;text-align:center}
.rank{width:34px;color:var(--ac)}
.chat{display:flex;flex-direction:column;gap:6px;height:52vh;overflow-y:auto;background:var(--bg2);border:1px solid var(--bg3);border-radius:var(--r);padding:10px}
.msg{display:flex;flex-direction:column;align-items:flex-start;max-width:86%}.msg.me{align-self:flex-end;align-items:flex-end}
.bub{background:var(--bg3);padding:7px 11px;border-radius:14px;word-break:break-word;white-space:pre-wrap}.bub.me{background:var(--ac);color:#fff}.bub.gift img{width:48px;height:48px;display:block}
.typing{min-height:16px;font-size:12px}
.skinprev{width:128px;height:128px;image-rendering:pixelated;object-fit:contain;background:var(--bg);border-radius:8px;align-self:center}
.card,.chat{border:2px solid #000!important;box-shadow:inset 0 0 0 1px var(--ln);border-radius:0!important}
input,textarea,select,.btn,.bub,.toast,.tab,.skinprev{border-radius:0!important}
input,textarea,select{border:1px solid var(--ln)!important;background:#20262a!important}
input:focus,textarea:focus,select:focus{border-color:var(--ac)!important;outline:0}
.btn{color:#08180f!important;border:2px solid #000!important;box-shadow:inset 0 -3px 0 #2c8a60;font-weight:700}
.btn.ghost{color:var(--tx)!important;background:#20262a;border:1px solid var(--ln)!important;box-shadow:none}
.bub.me{color:#08180f!important}
.toast{color:#08180f!important;border:2px solid #000}
.plates{display:grid;grid-template-columns:1fr 1fr;gap:8px}.plate{background:var(--bg2);border:2px solid #000;box-shadow:inset 0 0 0 1px var(--ln);padding:10px 12px;display:grid;gap:2px;cursor:pointer}.plate b{font-size:20px;color:var(--ac)}.plist{grid-column:1/3;background:var(--bg2);border:1px solid var(--ln);padding:8px 12px;font-size:14px}
.top{background:var(--bg2);border-bottom:1px solid var(--ln)}.tabs{border-top:1px solid var(--ln)}
.tab span{display:none}.tab{min-width:72px;padding:11px 8px}.tab small{font-size:12px}.tab.on{background:#20262a;box-shadow:inset 0 2px 0 var(--ac)}
.bar,.bar i,.dot{border-radius:0!important}
"""

HUB_JS = r"""const $=(s,r=document)=>r.querySelector(s);
const h=(t,a,...k)=>{const e=document.createElement(t);if(a)for(const[x,v]of Object.entries(a)){if(v===false||v==null)continue;if(x==='class')e.className=v;else if(x==='html')e.innerHTML=v;else if(x.startsWith('on'))e.addEventListener(x.slice(2),v);else if(x==='value')e.value=v;else e.setAttribute(x,v===true?'':v)}for(const c of k.flat())if(c!=null&&c!==false)e.append(c.nodeType?c:document.createTextNode(String(c)));return e};
const ls={get(k){try{return localStorage.getItem(k)||''}catch(e){return''}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}},del(k){try{localStorage.removeItem(k)}catch(e){}}};
const S={token:'',nick:'',admin:false,coins:0,tab:'profile',chat:null,view:null,timers:[],achCatalog:null};
(function(){const m=/[#&]t=([0-9a-f]+)/.exec(location.hash);if(m){ls.set('mafin_token',m[1]);const g=/[#&]go=(\w+)/.exec(location.hash);if(g)ls.set('mafin_go',g[1]);history.replaceState(null,'',location.pathname)}S.token=ls.get('mafin_token')})();
async function api(path,opt={}){
  const method=opt.method||(opt.body!==undefined?'POST':'GET');
  const r=await fetch(path,{method,headers:{'Content-Type':'application/json',...(S.token?{Authorization:'Bearer '+S.token}:{})},body:opt.body!==undefined?JSON.stringify(opt.body):undefined});
  let d={};try{d=await r.json()}catch(e){}
  if(r.status===401&&S.token&&!opt.soft){signOut(true)}
  if(!r.ok)throw new Error(d.error||('Ошибка '+r.status));
  return d}
const toast=(t,bad)=>{const e=h('div',{class:'toast'+(bad?' bad':'')},t);document.body.append(e);setTimeout(()=>e.remove(),3200)};
const guard=async(fn)=>{try{return await fn()}catch(e){toast(e.message,true)}};
const fmtMin=m=>{m=Math.round(m||0);return Math.floor(m/60)+' ч '+(m%60)+' мин'};
const num=v=>{v=Number(v||0);return Number.isInteger(v)?String(v):v.toFixed(1)};
const fmtDate=s=>{if(!s)return'';const d=new Date(String(s).replace(' ','T')+(/[zZ]|[+-]\d\d:?\d\d$/.test(s)?'':'Z'));return isNaN(d)?String(s):d.toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})};
const dot=on=>h('span',{class:'dot '+(on?'on':'')});
const clearTimers=()=>{S.timers.forEach(clearInterval);S.timers=[]};
const every=(fn,ms)=>{S.timers.push(setInterval(fn,ms))};
function signOut(silent){if(!silent&&S.token)api('/api/logout',{body:{},soft:true}).catch(()=>{});S.token='';S.nick='';ls.del('mafin_token');clearTimers();render()}
const TABS=[['profile','👤','Профиль'],['friends','👥','Друзья'],['gifts','🎁','Подарки'],['shop','🛒','Магазин'],['quests','📋','Задания'],['ach','🏆','Ачивки'],['top','🥇','Топ'],['news','📰','Новости'],['skin','🧍','Скин'],['cab','🎩','Шапка'],['tg','✈️','Telegram'],['server','⛏','Сервер'],['admin','🛡','Админ']];
function authView(){
  let mode='login';
  const err=h('div',{class:'err'});
  const nick=h('input',{autocomplete:'username',maxlength:20,placeholder:'Ник'}),pass=h('input',{type:'password',autocomplete:'current-password',placeholder:'Пароль'});
  const go=h('button',{class:'btn wide'},'Войти');
  const t1=h('button',{class:'btn sm',type:'button'},'Вход'),t2=h('button',{class:'btn sm ghost',type:'button'},'Регистрация');
  const setMode=m=>{mode=m;go.textContent=m==='login'?'Войти':'Создать аккаунт';t1.className='btn sm'+(m==='login'?'':' ghost');t2.className='btn sm'+(m==='login'?' ghost':'');err.textContent=''};
  t1.onclick=()=>setMode('login');t2.onclick=()=>setMode('register');
  const submit=async()=>{err.textContent='';go.disabled=true;
    try{const d=await api(mode==='login'?'/api/login':'/api/register',{body:{nickname:nick.value.trim(),password:pass.value},soft:true});S.token=d.token;ls.set('mafin_token',d.token);render()}
    catch(e){err.textContent=e.message}finally{go.disabled=false}};
  go.onclick=submit;pass.addEventListener('keydown',e=>{if(e.key==='Enter')submit()});
  return h('div',{class:'card auth'},h('h2',{},'⛏ Mafin'),h('p',{class:'muted'},'Тот же ник и пароль, что и в лаунчере.'),h('div',{class:'row'},t1,t2),nick,pass,err,go)}
async function render(){
  clearTimers();
  const root=$('#app');root.replaceChildren();
  if(!S.token){root.append(authView());return}
  let me;
  try{me=await api('/api/me')}catch(e){root.append(authView());return}
  S.me=me;S.nick=me.nickname;S.admin=!!me.is_admin;S.coins=me.coins;
  const go=ls.get('mafin_go');if(go){ls.del('mafin_go');S.tab=go;S.chat=null}
  const top=h('div',{class:'top'},h('div',{class:'brand'},'⛏ Mafin'),h('div',{class:'who'},h('span',{id:'coins'},'🪙 '+num(S.coins)),h('b',{},S.nick)));
  const bar=h('nav',{class:'tabs'});
  for(const[id,ic,name]of TABS){if(id==='admin'&&!S.admin)continue;bar.append(h('button',{class:'tab'+(S.tab===id?' on':''),'data-tab':id,onclick:()=>{S.tab=id;S.chat=null;render()}},h('span',{},ic),h('small',{},name)))}
  const main=h('main',{id:'main'});
  root.append(top,main,bar);
  const active=bar.querySelector('.on');if(active)active.scrollIntoView({inline:'center',block:'nearest'});
  api('/api/heartbeat',{body:{},soft:true}).catch(()=>{});
  every(()=>api('/api/heartbeat',{body:{},soft:true}).catch(()=>{}),60000);
  try{await VIEWS[S.tab](main)}catch(e){main.append(h('div',{class:'card err'},'Не удалось загрузить: '+e.message))}
}
const setCoins=c=>{S.coins=c;const e=$('#coins');if(e)e.textContent='🪙 '+num(c)};
const card=(...k)=>h('div',{class:'card'},...k);
const stat=(a,b)=>h('div',{class:'stat'},h('span',{class:'muted'},a),h('b',{},b));
const VIEWS={};
VIEWS.profile=async main=>{
  const p=await api('/api/profile/'+encodeURIComponent(S.nick));
  const bio=h('textarea',{rows:3,maxlength:300,placeholder:'Расскажите о себе'},p.bio||'');
  const vis=h('input',{type:'checkbox'});vis.checked=!!p.gifts_visible;
  vis.onchange=()=>guard(()=>api('/api/profile/gifts_visibility',{body:{visible:vis.checked}}));
  const gifts=h('div',{class:'giftrow'});
  main.append(card(h('div',{class:'row between'},h('h2',{},p.nickname),h('span',{},dot(p.is_online),p.is_online?' В сети':' Не в сети')),
    bio,h('button',{class:'btn sm',onclick:()=>guard(async()=>{await api('/api/profile/me',{method:'PUT',body:{bio:bio.value.trim()}});toast('Описание сохранено')})},'💾 Сохранить описание')),
    card(stat('🪙 Монеты',num(p.coins)),stat('⏱ Время в игре',fmtMin(p.total_playtime_minutes)),stat('🚀 Запусков',p.total_launches),stat('🏆 Ачивок',p.achievements_count)),
    card(h('label',{class:'chk'},vis,' Показывать подарки на моём профиле'),h('h3',{},'🎁 Полученные подарки'),gifts),
    card(h('button',{class:'btn ghost wide',onclick:()=>signOut()},'Выйти из аккаунта')));
  await fillReceived(gifts,S.nick);
  const pl=h('div',{class:'plates'});main.prepend(pl);
  try{const[fl,us]=await Promise.all([api('/api/friends/list'),api('/api/messages/unread_summary').catch(()=>({senders:[],count:0}))]);
    const on=(fl.friends||[]).filter(f=>f.is_online);
    const a=h('div',{class:'plate'},h('span',{class:'muted'},'Друзей в сети'),h('b',{},on.length+' / '+(fl.friends||[]).length));
    const bx=h('div',{class:'plist',style:'display:none'},on.length?on.map(f=>h('div',{},dot(true),' '+f.nickname)):h('span',{class:'muted'},'Никого нет в сети'));
    a.onclick=()=>{bx.style.display=bx.style.display==='none'?'block':'none'};
    const n=h('div',{class:'plate',onclick:()=>{S.tab='friends';S.chat=null;render()}},h('span',{class:'muted'},'Новые сообщения'),h('b',{},String(us.count||0)));
    const nl=h('div',{class:'plist'},(us.senders||[]).map(x=>h('div',{},x.sender+' · '+x.count)));
    pl.append(a,n,bx,us.senders&&us.senders.length?nl:'')}catch(e){}};
async function fillReceived(box,nick){
  try{const d=await api('/api/gifts/received/'+encodeURIComponent(nick));
    if(d.hidden){box.append(h('p',{class:'muted'},'Подарки скрыты владельцем профиля'));return}
    if(!d.gifts.length){box.append(h('p',{class:'muted'},'Пока нет подарков'));return}
    for(const g of d.gifts)box.append(h('div',{class:'gift',title:g.name+' от '+g.from+(g.note?': '+g.note:'')},h('img',{src:g.image_url,alt:g.name}),h('small',{},g.name),h('small',{class:'muted'},'от '+g.from)))}
  catch(e){box.append(h('p',{class:'muted'},'Не удалось загрузить подарки'))}}
async function openProfile(nick){
  const main=$('#main');main.replaceChildren(h('p',{class:'muted'},'Загрузка...'));
  const p=await api('/api/profile/'+encodeURIComponent(nick));
  const gifts=h('div',{class:'giftrow'});
  let actions;
  const st=p.friendship_status;
  const reload=()=>guard(()=>openProfile(nick));
  if(p.friendship_status==='self')actions=h('span',{});
  else if(st==='friends')actions=h('span',{class:'ok'},'✅ Вы друзья');
  else if(st==='request_sent')actions=h('span',{class:'muted'},'⏳ Заявка отправлена');
  else if(st==='request_received')actions=h('button',{class:'btn sm',onclick:()=>guard(async()=>{await api('/api/friends/respond',{body:{nickname:nick,accept:true}});reload()})},'Принять заявку');
  else actions=h('button',{class:'btn sm',onclick:()=>guard(async()=>{await api('/api/friends/request',{body:{nickname:nick}});reload()})},'➕ Добавить в друзья');
  main.replaceChildren(h('button',{class:'btn sm ghost',onclick:()=>render()},'← Назад'),
    card(h('div',{class:'row between'},h('h2',{},p.nickname),h('span',{},dot(p.is_online),p.is_online?' В сети':' Не в сети')),h('p',{},p.bio||''),
      stat('🪙 Монеты',num(p.coins)),stat('⏱ Время в игре',fmtMin(p.total_playtime_minutes)),stat('🚀 Запусков',p.total_launches),stat('🏆 Ачивок',p.achievements_count),
      h('div',{class:'row'},actions,p.friendship_status==='self'?null:h('button',{class:'btn sm',onclick:()=>{S.tab='gifts';S.giftTo=nick;render()}},'🎁 Подарить'))),
    p.gifts_visible?card(h('h3',{},'🎁 Полученные подарки'),gifts):card(h('p',{class:'muted'},'🎁 Подарки скрыты владельцем профиля')));
  if(p.gifts_visible)await fillReceived(gifts,nick)}
VIEWS.friends=async main=>{
  if(S.chat)return chatView(main,S.chat);
  const d=await api('/api/friends/list');
  const unread=await api('/api/messages/unread_count').catch(()=>({count:0}));
  const q=h('input',{placeholder:'Найти игрока по нику'}),res=h('div',{});
  const search=async()=>{res.replaceChildren();if(q.value.trim().length<2)return;
    const r=await api('/api/users/search?q='+encodeURIComponent(q.value.trim()));
    for(const u of r.results)res.append(h('div',{class:'li'},h('span',{class:'grow',onclick:()=>openProfile(u.nickname)},dot(u.is_online),' ',u.nickname),
      u.friendship_status==='none'?h('button',{class:'btn sm',onclick:()=>guard(async()=>{await api('/api/friends/request',{body:{nickname:u.nickname}});toast('Заявка отправлена');render()})},'➕'):h('small',{class:'muted'},({friends:'друг',request_sent:'заявка отправлена',request_received:'ждёт ответа'})[u.friendship_status]||'')));
    if(!r.results.length)res.append(h('p',{class:'muted'},'Никого не найдено'))};
  let t;q.oninput=()=>{clearTimeout(t);t=setTimeout(()=>guard(search),350)};
  main.append(card(h('h3',{},'🔎 Поиск'),q,res));
  if(d.incoming_requests.length)main.append(card(h('h3',{},'Заявки в друзья'),d.incoming_requests.map(r=>h('div',{class:'li'},h('span',{class:'grow',onclick:()=>openProfile(r.requester)},r.requester),
    h('button',{class:'btn sm',onclick:()=>guard(async()=>{await api('/api/friends/respond',{body:{nickname:r.requester,accept:true}});render()})},'Принять'),
    h('button',{class:'btn sm ghost',onclick:()=>guard(async()=>{await api('/api/friends/respond',{body:{nickname:r.requester,accept:false}});render()})},'Отклонить')))));
  main.append(card(h('h3',{},'Друзья'+(unread.count?' · новых сообщений: '+unread.count:'')),
    d.friends.length?d.friends.map(f=>h('div',{class:'li'},h('span',{class:'grow',onclick:()=>openProfile(f.nickname)},dot(f.is_online),' ',f.nickname),
      h('button',{class:'btn sm',onclick:()=>{S.chat=f.nickname;render()}},'💬'),
      h('button',{class:'btn sm ghost',onclick:()=>{if(confirm('Удалить '+f.nickname+' из друзей?'))guard(async()=>{await api('/api/friends/remove',{body:{nickname:f.nickname}});render()})}},'✕'))):h('p',{class:'muted'},'Пока нет друзей. Найдите игроков через поиск.')));
  if(d.outgoing_requests.length)main.append(card(h('h3',{},'Отправленные заявки'),d.outgoing_requests.map(r=>h('div',{class:'li'},h('span',{class:'grow'},r.addressee),h('small',{class:'muted'},'ожидает')))))};
async function chatView(main,peer){
  const list=h('div',{class:'chat'}),typing=h('div',{class:'typing muted'}),inp=h('input',{placeholder:'Сообщение',maxlength:2000});
  let last=0,seen=new Set(),lastType=0;
  const add=m=>{if(seen.has(m.id))return;seen.add(m.id);last=Math.max(last,m.id);const mine=m.sender===S.nick;
    let body;
    if(m.message_type==='gift'||m.gift_name)body=h('div',{class:'bub gift'+(mine?' me':'')},m.gift_image_url?h('img',{src:m.gift_image_url}):null,h('div',{},'🎁 '+(m.gift_name||'Подарок')+(m.gift_note?': '+m.gift_note:'')));
    else body=h('div',{class:'bub'+(mine?' me':'')},m.text);
    list.append(h('div',{class:'msg'+(mine?' me':'')},body,h('small',{class:'muted'},fmtDate(m.created_at))));list.scrollTop=list.scrollHeight};
  const poll=async()=>{try{const d=await api('/api/messages/with/'+encodeURIComponent(peer)+'?after_id='+last);d.messages.forEach(add);
    const t=await api('/api/messages/typing/'+encodeURIComponent(peer));typing.textContent=t.is_typing?peer+' печатает...':''}catch(e){}};
  const send=async()=>{const text=inp.value.trim();if(!text)return;inp.value='';await guard(async()=>{await api('/api/messages/send',{body:{to:peer,text}});await poll()})};
  inp.addEventListener('keydown',e=>{if(e.key==='Enter')send()});
  inp.addEventListener('input',()=>{const n=Date.now();if(n-lastType>2500){lastType=n;api('/api/messages/typing',{body:{to:peer},soft:true}).catch(()=>{})}});
  main.append(h('div',{class:'row between'},h('button',{class:'btn sm ghost',onclick:()=>{S.chat=null;render()}},'← Друзья'),h('b',{},'💬 '+peer),h('button',{class:'btn sm ghost',onclick:()=>openProfile(peer)},'Профиль')),
    list,typing,h('div',{class:'row'},inp,h('button',{class:'btn',onclick:send},'➤')));
  await poll();every(poll,3000)}
VIEWS.gifts=async main=>{
  const d=await api('/api/gifts');
  const to=h('input',{placeholder:'Кому (ник)',value:S.giftTo||''});S.giftTo='';
  const note=h('input',{placeholder:'Сообщение к подарку (необязательно)',maxlength:500});
  main.append(card(h('h3',{},'🎁 Магазин подарков'),to,note,h('p',{class:'muted'},'Выберите подарок ниже: он будет отправлен указанному игроку.')));
  if(!d.gifts.length){main.append(card(h('p',{class:'muted'},'Пока нет подарков в продаже')));return}
  const grid=h('div',{class:'grid'});
  for(const g of d.gifts)grid.append(h('div',{class:'card gift big'},h('img',{src:g.image_url,alt:g.name}),h('b',{},g.name),h('small',{class:'muted'},g.quantity==null?'без ограничения':'осталось: '+g.quantity),
    h('button',{class:'btn sm',onclick:()=>guard(async()=>{if(!to.value.trim())throw new Error('Укажите, кому подарить');if(!confirm('Отправить «'+g.name+'» игроку '+to.value.trim()+' за '+num(g.price)+' 🪙?'))return;
      const r=await api('/api/gifts/send',{body:{gift_id:g.id,to:to.value.trim(),message:note.value.trim()}});if(r.total_coins!=null)setCoins(r.total_coins);else setCoins(S.coins-g.price);toast('Подарок отправлен!')})},'🪙 '+num(g.price))));
  main.append(grid)};
VIEWS.shop=async main=>{
  const d=await api('/api/shop/items');setCoins(d.coins);
  const name=h('input',{placeholder:'Игровой ник',value:S.nick,maxlength:16});
  main.append(card(h('h3',{},'🛒 Привилегии'),h('p',{class:'muted'},'Покупка выдаётся на указанный игровой ник на сервере.'),name));
  if(!d.items.length)main.append(card(h('p',{class:'muted'},'Товаров пока нет')));
  for(const it of d.items)main.append(card(h('div',{class:'row between'},h('b',{},it.title),h('span',{class:'ok'},'🪙 '+num(it.price))),h('p',{class:'muted'},it.description||''),h('small',{class:'muted'},it.duration_days?('Срок: '+it.duration_days+' дн.'):'Навсегда'),
    h('button',{class:'btn sm',onclick:()=>guard(async()=>{if(!confirm('Купить «'+it.title+'» для '+name.value.trim()+' за '+num(it.price)+' 🪙?'))return;const r=await api('/api/shop/buy',{body:{item_id:it.id,game_name:name.value.trim()}});setCoins(r.total_coins);toast(r.status==='applied'?'Куплено и выдано':'Куплено, выдача в течение минуты');render()})},'Купить')));
  if(d.purchases.length)main.append(card(h('h3',{},'Мои покупки'),d.purchases.map(p=>h('div',{class:'li'},h('span',{class:'grow'},p.item_title+' → '+p.game_name),h('small',{class:'muted'},({applied:'выдано',pending:'ожидает'})[p.status]||p.status)))))};
VIEWS.quests=async main=>{
  const q=await api('/api/quests');
  if(!q.length){main.append(card(h('p',{class:'muted'},'Заданий пока нет')));return}
  for(const x of q){const pr=Math.min(x.progress,x.target_value),pct=x.target_value?Math.round(100*pr/x.target_value):0;
    main.append(card(h('b',{},x.title),x.description?h('p',{class:'muted'},x.description):null,h('div',{class:'bar'},h('i',{style:'width:'+pct+'%'})),
      h('div',{class:'row between'},h('small',{class:'muted'},num(pr)+'/'+num(x.target_value)+' · награда '+num(x.reward_coins)+' 🪙'),
        x.claimed?h('span',{class:'ok'},'✅ Получено'):x.completed?h('button',{class:'btn sm',onclick:()=>guard(async()=>{const r=await api('/api/quests/claim',{body:{quest_id:x.id}});setCoins(r.total_coins);toast('Получено '+num(r.reward_coins)+' монет!');render()})},'Забрать награду'):h('small',{class:'muted'},'В процессе'))))}};
VIEWS.ach=async main=>{
  if(!S.achCatalog)S.achCatalog=(await api('/api/achievements/catalog')).achievements;
  const[mine,cn]=await Promise.all([api('/api/achievements/mine'),api('/api/achievement_counters/mine')]);
  const got=new Map(mine.achievements.map(a=>[a.achievement_id,a.unlocked_at]));
  const facts={total_launches:S.me.total_launches,total_play_minutes:S.me.total_playtime_minutes,...cn.counters};
  main.append(card(h('b',{},'🏆 Получено: '+got.size+' из '+S.achCatalog.length)));
  for(const a of S.achCatalog){const on=got.has(a.id);let prog='';
    if(!on&&a.counter&&a.target){const v=Math.min(Number(facts[a.counter]||0),a.target);prog=h('div',{class:'bar'},h('i',{style:'width:'+Math.round(100*v/a.target)+'%'}))}
    main.append(h('div',{class:'card ach'+(on?' on':'')},h('div',{class:'aic'},a.icon),h('div',{class:'grow'},h('b',{},a.title),h('p',{class:'muted'},a.desc),prog,on?h('small',{class:'ok'},'Получено '+fmtDate(got.get(a.id))):null)))}};
VIEWS.top=async main=>{
  const[t,w]=await Promise.all([api('/api/top?limit=20'),api('/api/top/weekly?limit=20')]);
  const rows=(arr,f)=>arr.map(r=>h('div',{class:'li'},h('b',{class:'rank'},'#'+r.rank),h('span',{class:'grow',onclick:()=>openProfile(r.nickname)},dot(r.is_online),' ',r.nickname),h('small',{class:'muted'},f(r))));
  main.append(card(h('h3',{},'🥇 Топ недели'),h('small',{class:'muted'},'Награды: '+(w.rewards||[]).map(num).join(' / ')+' 🪙 · минимум '+num(w.min_minutes)+' мин'+(w.seconds_left?' · осталось '+Math.floor(w.seconds_left/3600)+' ч':'')),
    w.top.length?rows(w.top,r=>fmtMin(r.minutes)):h('p',{class:'muted'},'На этой неделе ещё никого')),
    card(h('h3',{},'🏅 Топ за всё время'),rows(t.top,r=>fmtMin(r.playtime)+' · '+num(r.coins)+' 🪙')))};
VIEWS.news=async main=>{
  const d=await api('/api/news');
  if(d.news.length){const mx=Math.max(...d.news.map(n=>n.id));ls.set('mafin_seen_news',String(mx))}
  if(S.admin){const t=h('input',{placeholder:'Заголовок'}),b=h('textarea',{rows:3,placeholder:'Текст новости'});
    main.append(card(h('h3',{},'Опубликовать новость'),t,b,h('button',{class:'btn sm',onclick:()=>guard(async()=>{await api('/api/news',{body:{title:t.value.trim(),body:b.value.trim()}});render()})},'📣 Опубликовать')))}
  if(!d.news.length)main.append(card(h('p',{class:'muted'},'Новостей пока нет')));
  for(const n of d.news){const c=card(h('div',{class:'row between'},h('b',{},n.title),h('small',{class:'muted'},fmtDate(n.created_at)+(n.edited?' (изм.)':''))),h('p',{class:'pre'},n.body),h('small',{class:'muted'},n.author||''));
    if(S.admin)c.append(h('div',{class:'row'},h('button',{class:'btn sm ghost',onclick:()=>{const t=prompt('Заголовок',n.title);if(t==null)return;const b=prompt('Текст',n.body);if(b==null)return;guard(async()=>{await api('/api/news/'+n.id,{method:'PUT',body:{title:t,body:b}});render()})}},'✏️'),h('button',{class:'btn sm ghost',onclick:()=>{if(confirm('Удалить новость?'))guard(async()=>{await api('/api/news/'+n.id,{method:'DELETE'});render()})}},'🗑')));
    main.append(c)}};
VIEWS.skin=async main=>{
  const name=h('input',{value:S.nick,maxlength:16,placeholder:'Игровой ник'});
  const model=h('select',{},h('option',{value:'default'},'Обычная модель (Steve)'),h('option',{value:'slim'},'Тонкие руки (Alex)'));
  const prev=h('img',{class:'skinprev',alt:''});const file=h('input',{type:'file',accept:'image/png'});
  const info=h('p',{class:'muted'});let png=null;
  const loadCur=async()=>{prev.removeAttribute('src');info.textContent='';
    const r=await api('/api/skin/mine?game_name='+encodeURIComponent(name.value.trim()));
    if(r.skin){const b=await fetch('/yggdrasil/textures/'+r.skin.hash);prev.src=URL.createObjectURL(await b.blob());model.value=r.skin.model;info.textContent='Текущий скин загружен'}else info.textContent='Скин для этого ника не загружен'};
  file.onchange=()=>{const f=file.files[0];if(!f)return;const fr=new FileReader();fr.onload=()=>{const img=new Image();img.onload=()=>{if(!(img.width===64&&(img.height===64||img.height===32))){toast('Скин должен быть PNG 64×64 или 64×32',true);png=null;return}
    png=fr.result.split(',')[1];prev.src=fr.result;info.textContent='Новый скин выбран, нажмите «Загрузить»'};img.src=fr.result};fr.readAsDataURL(f)};
  main.append(card(h('h3',{},'🧍 Скин'),h('p',{class:'muted'},'Скин показывается на сервере вместе с модулем скинов лаунчера.'),name,model,file,prev,info,
    h('div',{class:'row'},h('button',{class:'btn sm',onclick:()=>guard(async()=>{if(!png)throw new Error('Сначала выберите PNG-файл');await api('/api/skin/upload',{body:{game_name:name.value.trim(),model:model.value,png_b64:png}});png=null;toast('Скин загружен');await loadCur()})},'⬆️ Загрузить'),
      h('button',{class:'btn sm ghost',onclick:()=>guard(async()=>{if(!confirm('Удалить скин?'))return;await api('/api/skin/delete',{body:{game_name:name.value.trim()}});toast('Скин удалён');await loadCur()})},'🗑 Удалить'),
      h('button',{class:'btn sm ghost',onclick:()=>guard(loadCur)},'Обновить'))));
  await loadCur()};
VIEWS.cab=async main=>{
  main.append(card(h('h3',{},'🎩 Личный кабинет'),h('p',{class:'muted'},'Просмотр скина с шапкой в 3D, покупка и настройка шапки стрелками.'),h('a',{class:'btn wide',href:'/cabinet'},'Открыть кабинет')))};
VIEWS.tg=async main=>{
  const d=await api('/api/tg/status');
  for(const[bot,s]of Object.entries(d)){const box=card(h('h3',{},'✈️ '+s.title));
    if(!s.enabled)box.append(h('p',{class:'muted'},'Бот не настроен на сервере'));
    else if(s.linked)box.append(h('p',{class:'ok'},'✅ Привязан'),h('p',{class:'muted'},'Уведомления: друзья '+(s.notify_friends?'вкл':'выкл')+', новости '+(s.notify_news?'вкл':'выкл')+'. Настройки меняются командами бота.'),
      h('button',{class:'btn sm ghost',onclick:()=>guard(async()=>{await api('/api/tg/unlink',{body:{bot}});render()})},'Отвязать'));
    else box.append(h('p',{class:'muted'},'Не привязан. Бот присылает уведомления о друзьях и новостях, показывает профиль, топ, магазин и шапки.'),
      h('button',{class:'btn sm',onclick:()=>guard(async()=>{const r=await api('/api/tg/link',{body:{bot}});window.location.href=r.url})},'🔗 Привязать'));
    main.append(box)}};
VIEWS.server=async main=>{
  const s=await api('/api/mc/status');
  main.append(card(h('h3',{},'⛏ '+(s.name||'Сервер')),h('p',{},dot(s.running&&s.ready),s.running&&s.ready?' Работает':' Недоступен'),stat('Адрес',s.address||'—'),stat('Игроков',(s.players_online||0)+' / '+(s.players_max||0)),stat('Версия',s.version||(s.version_min?(s.version_min+' – '+s.version_max):'—')),s.description?h('p',{class:'muted'},s.description):null,
    h('button',{class:'btn sm',onclick:()=>{try{navigator.clipboard.writeText(s.address);toast('Адрес скопирован')}catch(e){toast(s.address)}}},'📋 Скопировать адрес')));
  const mp=await api('/api/modpack').catch(()=>null);
  if(mp&&mp.modpack)main.append(card(h('h3',{},'🧩 Сборка сервера'),stat('Название',mp.modpack.name||'—'),stat('Minecraft',mp.modpack.mc_version),stat('Загрузчик',(mp.modpack.loader||'vanilla')+(mp.modpack.loader_version?' '+mp.modpack.loader_version:'')),stat('Файлов',(mp.modpack.files||[]).length),h('p',{class:'muted'},'Установка сборки — в приложении лаунчера.')))};
VIEWS.admin=async main=>{
  if(!S.admin){main.append(card(h('p',{class:'muted'},'Нет доступа')));return}
  const[st,ps]=await Promise.all([api('/api/admin/stats'),api('/api/admin/profiles')]);
  main.append(card(h('h3',{},'🛡 Статистика'),stat('Игроков',st.total_profiles),stat('Забанено',st.total_banned),stat('Админов',st.total_admins),stat('Время в игре',fmtMin(st.total_playtime_minutes)),stat('Монет у игроков',num(st.total_coins))));
  const q=h('input',{placeholder:'Фильтр по нику'}),list=h('div',{});
  const draw=()=>{list.replaceChildren();const f=q.value.trim().toLowerCase();
    for(const p of ps.filter(x=>x.nickname.toLowerCase().includes(f)).slice(0,60)){
      const act=(label,fn,cls)=>h('button',{class:'btn sm '+(cls||'ghost'),onclick:()=>guard(async()=>{await fn();ps.splice(0,ps.length,...await api('/api/admin/profiles'));draw()})},label);
      const row=h('div',{class:'card'},h('div',{class:'row between'},h('b',{},dot(p.is_online),' ',p.nickname,p.is_admin?' 🛡':'',p.is_banned?' ⛔':''),h('small',{class:'muted'},num(p.coins)+' 🪙 · '+fmtMin(p.total_playtime_minutes))),
        h('small',{class:'muted'},'Запусков: '+p.total_launches+' · вход: '+fmtDate(p.last_login)+(p.ban_reason?' · причина бана: '+p.ban_reason:'')),
        h('div',{class:'row wrap'},p.nickname===S.nick?null:[
          p.is_banned?act('Разбанить',()=>api('/api/admin/unban',{body:{nickname:p.nickname}})):act('Забанить',()=>{const r=prompt('Причина бана','');if(r===null)throw new Error('Отменено');return api('/api/admin/ban',{body:{nickname:p.nickname,reason:r}})}),
          p.is_admin?act('Снять админа',()=>api('/api/admin/remove_admin',{body:{nickname:p.nickname}})):act('Сделать админом',()=>api('/api/admin/make_admin',{body:{nickname:p.nickname}})),
          act('Удалить',()=>{if(!confirm('Удалить аккаунт '+p.nickname+' безвозвратно?'))throw new Error('Отменено');return api('/api/admin/delete_account',{body:{nickname:p.nickname}})})]));
      list.append(row)}};
  q.oninput=draw;draw();
  main.append(card(h('h3',{},'Игроки'),q),list);
  const logs=await api('/api/admin/logs?limit=40');
  main.append(card(h('h3',{},'Журнал'),logs.map(l=>h('div',{class:'li log'},h('small',{class:'muted'},fmtDate(l.created_at)),h('span',{class:'grow'},(l.action||'')+' · '+(l.nickname||'')+' · '+(l.details||''))))))};
render();
"""

HUB_HTML = (
    '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    '<meta name="theme-color" content="#23272e"><title>Mafin</title><style>' + HUB_CSS + '</style></head>'
    '<body><div id="app"></div><script>' + HUB_JS + '</script></body></html>'
)

ACHIEVEMENT_CATALOG = json.loads(r'''[{"id":"launch_1","icon":"🎮","title":"Первый запуск","desc":"Запустите Minecraft через лаунчер в первый раз","counter":"total_launches","target":1.0},{"id":"launch_10","icon":"🕹️","title":"Разогрелся","desc":"Запустите игру 10 раз","counter":"total_launches","target":10.0},{"id":"launch_50","icon":"🔥","title":"Завсегдатай","desc":"Запустите игру 50 раз","counter":"total_launches","target":50.0},{"id":"launch_200","icon":"🏰","title":"Домосед","desc":"Запустите игру 200 раз","counter":"total_launches","target":200.0},{"id":"launch_1000","icon":"👑","title":"Легенда лаунчера","desc":"Запустите игру 1000 раз","counter":"total_launches","target":1000.0},{"id":"time_30m","icon":"⏱️","title":"Полчаса как один миг","desc":"Наиграйте суммарно 30 минут","counter":"total_play_minutes","target":30.0},{"id":"time_5h","icon":"⏳","title":"Погружение","desc":"Наиграйте суммарно 5 часов","counter":"total_play_minutes","target":300.0},{"id":"time_10h","icon":"🕰️","title":"Втянулся","desc":"Наиграйте суммарно 10 часов","counter":"total_play_minutes","target":600.0},{"id":"time_24h","icon":"🌍","title":"Целые сутки","desc":"Наиграйте суммарно 24 часа","counter":"total_play_minutes","target":1440.0},{"id":"time_100h","icon":"🏆","title":"Ветеран","desc":"Наиграйте суммарно 100 часов","counter":"total_play_minutes","target":6000.0},{"id":"time_500h","icon":"🌟","title":"Легенда Minecraft","desc":"Наиграйте суммарно 500 часов","counter":"total_play_minutes","target":30000.0},{"id":"versions_installed_1","icon":"📦","title":"Первая установка","desc":"Установите свою первую версию Minecraft","counter":"versions_installed","target":1.0},{"id":"versions_installed_5","icon":"📦","title":"Коллекционер версий","desc":"Установите 5 разных версий Minecraft","counter":"versions_installed","target":5.0},{"id":"versions_installed_20","icon":"🗃️","title":"Архив версий","desc":"Установите 20 разных версий Minecraft","counter":"versions_installed","target":20.0},{"id":"distinct_launched_3","icon":"🔀","title":"Разнообразие","desc":"Запустите 3 разные версии Minecraft","counter":"distinct_versions_launched","target":3.0},{"id":"distinct_launched_10","icon":"🎲","title":"Испытатель версий","desc":"Запустите 10 разных версий Minecraft","counter":"distinct_versions_launched","target":10.0},{"id":"same_version_20","icon":"💎","title":"Верность выбору","desc":"Запустите одну и ту же версию 20 раз","counter":"max_single_version_launches","target":20.0},{"id":"legacy_launch","icon":"🦕","title":"Археолог","desc":"Запустите очень старую версию Minecraft (alpha/beta/classic)","counter":"legacy_launches","target":1.0},{"id":"prerelease_launch","icon":"🧪","title":"Тестировщик","desc":"Запустите снапшот, pre-release или RC версию","counter":"prerelease_launches","target":1.0},{"id":"forge_install","icon":"⚒️","title":"Модостроитель","desc":"Установите Forge","counter":"forge_installs","target":1.0},{"id":"fabric_install","icon":"🧵","title":"Фабрикант","desc":"Установите Fabric","counter":"fabric_installs","target":1.0},{"id":"both_loaders","icon":"🛠️","title":"Универсал","desc":"Установите и Forge, и Fabric"},{"id":"mods_1","icon":"🧩","title":"Первый мод","desc":"Добавьте свой первый мод","counter":"mods_installed","target":1.0},{"id":"mods_10","icon":"🧰","title":"Моддер","desc":"Добавьте 10 модов","counter":"mods_installed","target":10.0},{"id":"mods_50","icon":"🏗️","title":"Сборкодел","desc":"Добавьте 50 модов","counter":"mods_installed","target":50.0},{"id":"resourcepack_1","icon":"🎨","title":"Художник","desc":"Добавьте свой первый ресурспак","counter":"resourcepacks_installed","target":1.0},{"id":"resourcepack_5","icon":"🖼️","title":"Куратор текстур","desc":"Добавьте 5 ресурспаков","counter":"resourcepacks_installed","target":5.0},{"id":"skin_change_1","icon":"👕","title":"Новый образ","desc":"Смените скин персонажа","counter":"skins_changed","target":1.0},{"id":"skin_change_5","icon":"🧥","title":"Модник","desc":"Смените скин 5 раз","counter":"skins_changed","target":5.0},{"id":"accounts_2","icon":"👥","title":"Не один","desc":"Добавьте второй аккаунт","counter":"accounts_count","target":2.0},{"id":"accounts_5","icon":"👨‍👩‍👧‍👦","title":"Целая семья","desc":"Добавьте 5 аккаунтов","counter":"accounts_count","target":5.0},{"id":"server_add_1","icon":"🌐","title":"В сеть","desc":"Добавьте свой первый сервер","counter":"servers_added_total","target":1.0},{"id":"server_add_10","icon":"🗺️","title":"Коллекционер серверов","desc":"Добавьте 10 серверов","counter":"servers_added_total","target":10.0},{"id":"server_ping","icon":"📶","title":"Разведчик","desc":"Проверьте пинг сервера","counter":"server_pings","target":1.0},{"id":"p2p_host_1","icon":"🏠","title":"Гостеприимный хозяин","desc":"Создайте P2P-комнату для друзей","counter":"p2p_host_count","target":1.0},{"id":"p2p_host_10","icon":"🏟️","title":"Хост со стажем","desc":"Создайте P2P-комнату 10 раз","counter":"p2p_host_count","target":10.0},{"id":"p2p_join_1","icon":"🚪","title":"В гости","desc":"Подключитесь к другу через P2P","counter":"p2p_join_count","target":1.0},{"id":"logs_cleaned","icon":"🧹","title":"Чистюля","desc":"Очистите старые логи","counter":"logs_cleaned","target":1.0},{"id":"smart_cleanup_1","icon":"✨","title":"Генеральная уборка","desc":"Запустите умную очистку временных файлов","counter":"smart_cleanups","target":1.0},{"id":"smart_cleanup_10","icon":"🧼","title":"Порядок превыше всего","desc":"Запустите умную очистку 10 раз","counter":"smart_cleanups","target":10.0},{"id":"orphan_cleanup","icon":"🗑️","title":"Ничего лишнего","desc":"Очистите неиспользуемые ассеты","counter":"orphan_asset_cleanups","target":1.0},{"id":"export_profile","icon":"💾","title":"Архивариус","desc":"Экспортируйте профиль лаунчера","counter":"exports","target":1.0},{"id":"import_profile","icon":"📥","title":"Реставратор","desc":"Импортируйте профиль лаунчера","counter":"imports","target":1.0},{"id":"theme_change","icon":"🎭","title":"Стиль важен","desc":"Смените тему лаунчера","counter":"theme_changes","target":1.0},{"id":"java_custom","icon":"☕","title":"Технарь","desc":"Укажите свою Java вручную в настройках","counter":"java_path_customized","target":1.0},{"id":"memory_custom","icon":"🧠","title":"Оптимизатор","desc":"Измените выделение ОЗУ по умолчанию","counter":"memory_customized","target":1.0},{"id":"dir_custom","icon":"📁","title":"Своя папка","desc":"Смените папку установки игры","counter":"dir_customized","target":1.0},{"id":"search_used","icon":"🔎","title":"Следопыт","desc":"Воспользуйтесь поиском версий","counter":"search_used","target":1.0},{"id":"hotkey_used","icon":"⌨️","title":"Скорострел","desc":"Воспользуйтесь горячей клавишей лаунчера","counter":"hotkey_used","target":1.0},{"id":"night_owl_1","icon":"🌙","title":"Полуночник","desc":"Запустите игру ночью (00:00-05:00)","counter":"night_launches","target":1.0},{"id":"night_owl_10","icon":"🦉","title":"Сова","desc":"Запустите игру ночью 10 раз","counter":"night_launches","target":10.0},{"id":"weekend_gamer","icon":"🎉","title":"Выходной день","desc":"Запустите игру по выходным 5 раз","counter":"weekend_launches","target":5.0},{"id":"crash_recovery","icon":"🩹","title":"Не сдаюсь","desc":"Успешно перезапустите игру после сбоя","counter":"crash_recoveries","target":1.0},{"id":"discord_online","icon":"💬","title":"На связи","desc":"Подключитесь к Discord Rich Presence","counter":"discord_connected","target":1.0},{"id":"collector_25","icon":"🥈","title":"На полпути","desc":"Разблокируйте 25 других ачивок","counter":"unlocked_count","target":25.0},{"id":"collector_all","icon":"🥇","title":"Всё собрал","desc":"Разблокируйте все остальные ачивки"}]''')


@app.route('/m', methods=['GET'])
@app.route('/m/', methods=['GET'])
@app.route('/mobile', methods=['GET'])
def site_hub():
    resp = app.response_class(HUB_HTML, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route('/api/achievements/catalog', methods=['GET'])
def achievements_catalog():
    return jsonify({"achievements": ACHIEVEMENT_CATALOG})


SKIN_MAX_BYTES = 256 * 1024
SKIN_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
SKIN_KEY_PATH = os.environ.get("MAFIN_SKIN_KEY", os.path.join(BASE_DIR, "skin_signing_key.pem"))
_skin_key_lock = threading.Lock()
_skin_key_cache = {}


def _skin_signing_key():
    """RSA-ключ для подписи текстур (нужен пакет cryptography). Без него отдаём неподписанные текстуры."""
    with _skin_key_lock:
        if "key" in _skin_key_cache:
            return _skin_key_cache["key"]
        key = None
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            if os.path.isfile(SKIN_KEY_PATH):
                with open(SKIN_KEY_PATH, "rb") as f:
                    key = serialization.load_pem_private_key(f.read(), password=None)
            else:
                key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
                pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
                with open(SKIN_KEY_PATH, "wb") as f:
                    f.write(pem)
                try:
                    os.chmod(SKIN_KEY_PATH, 0o600)
                except OSError:
                    pass
        except Exception as e:
            print(f"[skins] подпись текстур отключена ({e}). Установи: pip install cryptography")
            key = None
        _skin_key_cache["key"] = key
        return key


def _skin_public_pem():
    key = _skin_signing_key()
    if not key:
        return None
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")


def _skin_sign(value):
    key = _skin_signing_key()
    if not key:
        return None
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return base64.b64encode(key.sign(value.encode("ascii"), padding.PKCS1v15(), hashes.SHA1())).decode("ascii")


def _skin_base_url():
    return (os.environ.get("MAFIN_PUBLIC_URL") or request.host_url).rstrip("/")


def _skin_png_ok(data):
    if len(data) > SKIN_MAX_BYTES or len(data) < 70 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return False
    w, h = struct.unpack(">II", data[16:24])
    return (w, h) in ((64, 64), (64, 32))


@app.route('/api/skin/upload', methods=['POST'])
@auth_required
def skin_upload():
    import base64
    data = request.get_json(silent=True) or {}
    name = str(data.get('game_name', '')).strip()
    model = 'slim' if str(data.get('model', 'default')) == 'slim' else 'default'
    if not SKIN_NAME_RE.fullmatch(name):
        return jsonify({"error": "Некорректный игровой ник (3–16 символов: латиница, цифры, _)"}), 400
    try:
        png = base64.b64decode(str(data.get('png_b64', '')), validate=True)
    except Exception:
        return jsonify({"error": "Файл скина повреждён"}), 400
    if not _skin_png_ok(png):
        return jsonify({"error": "Скин должен быть PNG 64×64 или 64×32 (до 256 КБ)"}), 400

    me = g.current_profile['nickname']
    db = get_db()
    row = db.execute("SELECT owner FROM skins WHERE game_name_lower = ?", (name.lower(),)).fetchone()
    if row and row['owner'] != me:
        return jsonify({"error": "Этот игровой ник уже занят скином другого аккаунта"}), 403
    digest = hashlib.sha256(png + model.encode()).hexdigest()
    db.execute(
        "INSERT INTO skins (game_name_lower, game_name, uuid, owner, model, hash, png, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(game_name_lower) DO UPDATE SET game_name=excluded.game_name, model=excluded.model, "
        "hash=excluded.hash, png=excluded.png, updated_at=excluded.updated_at",
        (name.lower(), name, _offline_uuid(name), me, model, digest, png,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.commit()
    _skin_public_base["url"] = _skin_base_url()
    return jsonify({"success": True, "hash": digest})


_skin_public_base = {}
_skin_pushed = {}


def _skinsrestorer_installed():
    return any("skinsrestorer" in p["name"].lower() for p in _mc_list_plugins())


def _skin_push_pending():
    """Передаёт загруженные скины плагину SkinsRestorer через консоль сервера (нужно для версий 1.20.2+)."""
    base = _skin_public_base.get("url") or os.environ.get("MAFIN_PUBLIC_URL", "").rstrip("/")
    if not base or not _mc_pid() or not _skinsrestorer_installed() or not _mc_ping():
        return 0
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT game_name, model, hash FROM skins").fetchall()
    finally:
        db.close()
    done = 0
    for r in rows:
        name, h = r["game_name"], r["hash"]
        if _skin_pushed.get(name) == h or not SKIN_NAME_RE.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", h):
            continue
        custom = f"mafin_{name}_{h[:8]}"
        kind = "slim" if r["model"] == "slim" else "classic"
        try:
            _mc_send(f"sr createcustom {custom} {base}/yggdrasil/textures/{h} {kind}")
            time.sleep(8)
            _mc_send(f"sr setskin {name} {custom}")
        except OSError as e:
            print(f"[skins] консоль сервера недоступна: {e}")
            return done
        _skin_pushed[name] = h
        done += 1
        print(f"[skins] скин {name} передан в SkinsRestorer")
    return done


def _skin_push_loop():
    while True:
        time.sleep(30)
        try:
            _skin_push_pending()
        except Exception as e:
            print(f"[skins] ошибка передачи в SkinsRestorer: {e}")


@app.route('/api/skin/delete', methods=['POST'])
@auth_required
def skin_delete():
    data = request.get_json(silent=True) or {}
    name = str(data.get('game_name', '')).strip().lower()
    db = get_db()
    db.execute("DELETE FROM skins WHERE game_name_lower = ? AND owner = ?",
               (name, g.current_profile['nickname']))
    db.commit()
    return jsonify({"success": True})


@app.route('/api/skin/mine', methods=['GET'])
@auth_required
def skin_mine():
    name = str(request.args.get('game_name', '')).strip().lower()
    row = get_db().execute("SELECT hash, model FROM skins WHERE game_name_lower = ? AND owner = ?",
                           (name, g.current_profile['nickname'])).fetchone()
    if not row:
        return jsonify({"success": True, "skin": None})
    return jsonify({"success": True, "skin": {"hash": row["hash"], "model": row["model"]}})


@app.route('/yggdrasil', methods=['GET'])
@app.route('/yggdrasil/', methods=['GET'])
def ygg_meta():
    host = request.host.split(':')[0]
    meta = {
        "meta": {"serverName": "Mafin Skins", "implementationName": "mafin-skins",
                 "implementationVersion": "1.0"},
        "skinDomains": [host],
    }
    pem = _skin_public_pem()
    if pem:
        meta["signaturePublicKey"] = pem
    return jsonify(meta)


def _ygg_undashed(u):
    return str(u).replace("-", "").lower()


@app.route('/yggdrasil/sessionserver/session/minecraft/profile/<uid>', methods=['GET'])
def ygg_profile(uid):
    import base64
    uid_nd = _ygg_undashed(uid)
    if not re.fullmatch(r"[0-9a-f]{32}", uid_nd):
        return "", 204
    dashed = f"{uid_nd[:8]}-{uid_nd[8:12]}-{uid_nd[12:16]}-{uid_nd[16:20]}-{uid_nd[20:]}"
    row = get_db().execute("SELECT game_name, model, hash FROM skins WHERE uuid = ?", (dashed,)).fetchone()
    if not row:
        return "", 204
    skin = {"url": f"{_skin_base_url()}/yggdrasil/textures/{row['hash']}"}
    if row['model'] == 'slim':
        skin["metadata"] = {"model": "slim"}
    payload = {"timestamp": int(time.time() * 1000), "profileId": uid_nd,
               "profileName": row['game_name'], "textures": {"SKIN": skin}}
    value = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    prop = {"name": "textures", "value": value}
    if request.args.get("unsigned", "true").lower() == "false":
        sig = _skin_sign(value)
        if sig:
            prop["signature"] = sig
        else:
            print("[skins] клиент просит подписанные текстуры, а подписать нечем: pip install cryptography")
    print(f"[skins] профиль {row['game_name']} отдан клиенту ({request.remote_addr})")
    return jsonify({"id": uid_nd, "name": row['game_name'], "properties": [prop]})


@app.route('/yggdrasil/api/profiles/minecraft', methods=['POST'])
def ygg_profiles_by_name():
    names = request.get_json(silent=True)
    if not isinstance(names, list):
        return jsonify([])
    out = []
    db = get_db()
    for n in names[:100]:
        row = db.execute("SELECT game_name, uuid FROM skins WHERE game_name_lower = ?",
                         (str(n).lower(),)).fetchone()
        if row:
            out.append({"id": _ygg_undashed(row['uuid']), "name": row['game_name']})
    return jsonify(out)


@app.route('/yggdrasil/textures/<texture_hash>', methods=['GET'])
def ygg_texture(texture_hash):
    if not re.fullmatch(r"[0-9a-f]{64}", texture_hash):
        return "", 404
    row = get_db().execute("SELECT png FROM skins WHERE hash = ?", (texture_hash,)).fetchone()
    if not row:
        return "", 404
    resp = app.response_class(bytes(row['png']), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


SHOP_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
SHOP_GROUP_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
_shop_lock = threading.Lock()


def _offline_uuid(name):
    """UUID игрока на сервере с online-mode=false (так его считает Minecraft)."""
    import uuid
    return str(uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest(), version=3))


def _shop_command(purchase):
    """Команда LuckPerms для консоли. Все части проверяются, лишних символов в ней быть не может."""
    name, group = str(purchase["game_name"]), str(purchase["group_name"])
    days = int(purchase["duration_days"])
    if not SHOP_NAME_RE.fullmatch(name) or not SHOP_GROUP_RE.fullmatch(group) or not 0 <= days <= 3650:
        raise ValueError("некорректные данные покупки")
    uid = _offline_uuid(name)
    if days > 0:
        return f"lp user {uid} parent addtemp {group} {days}d accumulate"
    return f"lp user {uid} parent add {group}"


def _shop_luckperms_installed():
    return any(p["name"].lower() == "luckperms" for p in _mc_list_plugins())


def _shop_apply_pending():
    """Выдаёт оплаченные привилегии, пока сервер работает и LuckPerms загружен. Возвращает число выданных."""
    with _shop_lock:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute("SELECT * FROM shop_purchases WHERE status = 'pending' ORDER BY id LIMIT 50").fetchall()
            if not rows or not _mc_pid():
                return 0
            if not _shop_luckperms_installed():
                db.execute("UPDATE shop_purchases SET error = ? WHERE status = 'pending'",
                           ("LuckPerms не установлен на сервере",))
                db.commit()
                return 0
            if not _mc_ping():
                return 0
            done = 0
            for r in rows:
                try:
                    _mc_send(_shop_command(r))
                except ValueError as e:
                    db.execute("UPDATE shop_purchases SET status = 'failed', error = ? WHERE id = ?", (str(e), r["id"]))
                    continue
                except OSError as e:
                    db.execute("UPDATE shop_purchases SET error = ? WHERE id = ?", (f"консоль недоступна: {e}", r["id"]))
                    break
                db.execute("UPDATE shop_purchases SET status = 'applied', error = NULL, applied_at = ? WHERE id = ?",
                           (datetime.now(timezone.utc).isoformat(timespec="seconds"), r["id"]))
                done += 1
                time.sleep(0.2)
            db.commit()
            return done
        finally:
            db.close()


def _shop_loop():
    while True:
        time.sleep(20)
        try:
            _shop_apply_pending()
        except Exception as e:
            print(f"[shop] ошибка выдачи: {e}")


def _shop_start():
    threading.Thread(target=_shop_loop, daemon=True).start()


@app.route('/api/shop/items', methods=['GET'])
@auth_required
def shop_items():
    db = get_db()
    items = db.execute(
        "SELECT id, title, description, duration_days, price FROM shop_items WHERE enabled = 1 "
        "ORDER BY sort_order, id").fetchall()
    mine = db.execute(
        "SELECT item_title, game_name, price, duration_days, status, created_at FROM shop_purchases "
        "WHERE nickname = ? ORDER BY id DESC LIMIT 10", (g.current_profile['nickname'],)).fetchall()
    return jsonify({"success": True, "coins": g.current_profile['coins'],
                    "items": [dict(r) for r in items], "purchases": [dict(r) for r in mine]})


@app.route('/api/shop/buy', methods=['POST'])
@auth_required
def shop_buy():
    data = request.get_json(silent=True) or {}
    try:
        item_id = int(data.get('item_id'))
    except (TypeError, ValueError):
        return jsonify({"error": "item_id обязателен"}), 400
    game_name = str(data.get('game_name', '')).strip()
    if not SHOP_NAME_RE.fullmatch(game_name):
        return jsonify({"error": "Игровой ник: 3–16 символов, латиница, цифры и _"}), 400

    db = get_db()
    item = db.execute("SELECT * FROM shop_items WHERE id = ? AND enabled = 1", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Товар не найден или снят с продажи"}), 404
    price = float(item['price'])
    cur = db.execute(
        "UPDATE profiles SET coins = coins - ? WHERE id = ? AND coins >= ?",
        (price, g.current_profile['id'], price))
    if cur.rowcount == 0:
        db.rollback()
        return jsonify({"error": "Недостаточно монет"}), 402
    me = g.current_profile['nickname']
    purchase_id = db.execute(
        "INSERT INTO shop_purchases (nickname, game_name, item_id, item_title, group_name, duration_days, price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (me, game_name, item['id'], item['title'], item['group_name'], item['duration_days'], price)).lastrowid
    db.commit()
    log_action("shop_buy", me, f"{item['title']} -> {game_name} (-{price:g} монет)")
    try:
        _shop_apply_pending()
    except Exception as e:
        print(f"[shop] выдача при покупке не удалась: {e}")
    status = db.execute("SELECT status FROM shop_purchases WHERE id = ?", (purchase_id,)).fetchone()['status']
    coins = db.execute("SELECT coins FROM profiles WHERE id = ?", (g.current_profile['id'],)).fetchone()['coins']
    return jsonify({"success": True, "total_coins": coins, "status": status, "purchase_id": purchase_id})


@app.route('/admin/shop', methods=['GET'])
@web_admin_required
def admin_shop_page():
    return render_template_string(SHOP_ADMIN_TEMPLATE)


@app.route('/admin/shop/data', methods=['GET'])
@web_admin_required
def admin_shop_data():
    db = get_db()
    items = db.execute("SELECT * FROM shop_items ORDER BY sort_order, id").fetchall()
    purchases = db.execute("SELECT * FROM shop_purchases ORDER BY id DESC LIMIT 60").fetchall()
    return jsonify({"items": [dict(r) for r in items], "purchases": [dict(r) for r in purchases],
                    "luckperms": _shop_luckperms_installed(), "server_running": bool(_mc_pid())})


@app.route('/admin/shop/<action>', methods=['POST'])
@web_admin_required
def admin_shop_action(action):
    if not request.is_json:
        return jsonify({"success": False, "error": "Ожидается JSON"}), 400
    data = request.get_json(silent=True) or {}
    db = get_db()
    admin = g.web_admin['nickname']

    def fail(msg, code=400):
        return jsonify({"success": False, "error": msg}), code

    if action == "item_save":
        title = re.sub(r"[\x00-\x1f\x7f]", "", str(data.get("title", ""))).strip()
        desc = re.sub(r"[\x00-\x1f\x7f]", " ", str(data.get("description", ""))).strip()
        group = str(data.get("group_name", "")).strip()
        try:
            days = int(data.get("duration_days", 0))
            price = float(data.get("price"))
            order = int(data.get("sort_order", 0))
        except (TypeError, ValueError):
            return fail("Срок, цена и порядок должны быть числами")
        if not 1 <= len(title) <= 40:
            return fail("Название: от 1 до 40 символов")
        if len(desc) > 200:
            return fail("Описание: не больше 200 символов")
        if not SHOP_GROUP_RE.fullmatch(group):
            return fail("Группа LuckPerms: латиница, цифры, _ . - (до 32 символов)")
        if not 0 <= days <= 3650:
            return fail("Срок: от 0 (навсегда) до 3650 дней")
        if not 0 < price <= 1000000:
            return fail("Цена должна быть больше 0")
        enabled = 1 if data.get("enabled", True) else 0
        item_id = data.get("id")
        if item_id:
            cur = db.execute(
                "UPDATE shop_items SET title=?, description=?, group_name=?, duration_days=?, price=?, enabled=?, sort_order=? WHERE id=?",
                (title, desc, group, days, price, enabled, order, int(item_id)))
            if cur.rowcount == 0:
                return fail("Товар не найден", 404)
        else:
            db.execute(
                "INSERT INTO shop_items (title, description, group_name, duration_days, price, enabled, sort_order) VALUES (?,?,?,?,?,?,?)",
                (title, desc, group, days, price, enabled, order))
        db.commit()
        log_action("shop_item_save", admin, f"{title} ({group}, {days} дн., {price:g})")
        return jsonify({"success": True, "message": f"Товар «{title}» сохранён"})

    if action == "item_delete":
        try:
            item_id = int(data.get("id"))
        except (TypeError, ValueError):
            return fail("id обязателен")
        db.execute("DELETE FROM shop_items WHERE id = ?", (item_id,))
        db.commit()
        log_action("shop_item_delete", admin, str(item_id))
        return jsonify({"success": True, "message": "Товар удалён"})

    if action in ("purchase_retry", "purchase_refund"):
        try:
            pid = int(data.get("id"))
        except (TypeError, ValueError):
            return fail("id обязателен")
        row = db.execute("SELECT * FROM shop_purchases WHERE id = ?", (pid,)).fetchone()
        if not row:
            return fail("Покупка не найдена", 404)
        if action == "purchase_retry":
            if row["status"] not in ("failed", "applied"):
                return fail("Повторить можно выданную или неудавшуюся покупку")
            db.execute("UPDATE shop_purchases SET status = 'pending', error = NULL WHERE id = ?", (pid,))
            db.commit()
            applied = _shop_apply_pending()
            return jsonify({"success": True, "message": "Выдача отправлена" if applied else
                            "Поставлено в очередь: выдача пройдёт, когда сервер запущен и LuckPerms загружен"})
        if row["status"] not in ("pending", "failed"):
            return fail("Вернуть монеты можно только за невыданную покупку")
        with _shop_lock:
            cur = db.execute("UPDATE shop_purchases SET status = 'refunded' WHERE id = ? AND status IN ('pending','failed')", (pid,))
            if cur.rowcount:
                db.execute("UPDATE profiles SET coins = coins + ? WHERE nickname = ?", (row["price"], row["nickname"]))
            db.commit()
        if not cur.rowcount:
            return fail("Покупка уже обработана")
        log_action("shop_refund", admin, f"{row['nickname']}: +{row['price']:g} монет")
        return jsonify({"success": True, "message": f"Возвращено {row['price']:g} монет игроку {row['nickname']}"})

    return fail("Неизвестное действие", 404)


SHOP_ADMIN_TEMPLATE = """
<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Mafin Launcher — Магазин привилегий</title>
<style>
* {box-sizing:border-box}
body{background:#1a1d23;color:#e1e4e8;font-family:'Segoe UI',Arial,sans-serif;margin:0}
header{background:#242830;padding:16px 28px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #2d323c}
header h1{font-size:18px;margin:0;color:#43b581}
header a{color:#7a8599;text-decoration:none;font-size:13px;margin-left:14px}
header a:hover{color:#fff}
main{padding:24px 28px;max-width:1100px;margin:0 auto}
section{background:#242830;border-radius:10px;padding:20px;margin-bottom:20px}
section h2{margin-top:0;font-size:15px;color:#43b581}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-top:10px}
label{font-size:12px;color:#7a8599;display:flex;flex-direction:column;gap:4px}
input[type=text],input[type=number]{padding:8px;border-radius:6px;border:1px solid #2d323c;background:#2d323c;color:#fff;font-size:13px}
button{background:#2d323c;color:#e1e4e8;border:none;padding:8px 14px;border-radius:6px;cursor:pointer;font-size:13px}
button:hover{background:#3a3f4a}
button.primary{background:#43b581;color:#fff;font-weight:bold}
button.danger:hover{background:#f04747}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #2d323c}
th{color:#7a8599;font-weight:normal;font-size:12px}
.ok{color:#43b581}.bad{color:#f04747}.warn{color:#faa61a}.muted{color:#7a8599;font-size:12px}
#msg{margin-top:12px;font-size:13px;min-height:18px}
</style></head>
<body>
<header><h1>🛒 Магазин привилегий</h1>
<div><a href="/admin/cosmetics">🎩 Косметика</a><a href="/admin/modpack">🧩 Сборка</a><a href="/admin/minecraft">🖥 Сервер</a><a href="/admin/dashboard">← Панель</a><a href="/admin/logout">Выйти</a></div></header>
<main>
<section>
  <h2>Состояние</h2>
  <div id="state" class="muted">Загрузка…</div>
  <p class="muted">Покупка списывает монеты и выдаёт группу командой LuckPerms через консоль сервера. Если сервер выключен, выдача ждёт его запуска. Группа должна уже существовать в LuckPerms (<code>/lp creategroup имя</code>).</p>
  <div id="msg"></div>
</section>
<section>
  <h2 id="formTitle">Новый товар</h2>
  <div class="row">
    <label>Название<input type="text" id="f_title" maxlength="40" style="width:200px"></label>
    <label>Группа LuckPerms<input type="text" id="f_group" maxlength="32" style="width:150px" placeholder="vip"></label>
    <label>Срок, дней (0 = навсегда)<input type="number" id="f_days" value="30" min="0" max="3650" style="width:130px"></label>
    <label>Цена, монет<input type="number" id="f_price" value="100" min="0" step="any" style="width:100px"></label>
    <label>Порядок<input type="number" id="f_order" value="0" style="width:70px"></label>
    <label style="flex-direction:row;align-items:center;gap:6px;padding-bottom:8px"><input type="checkbox" id="f_enabled" checked> в продаже</label>
  </div>
  <div class="row">
    <label style="flex:1">Описание (что получает игрок)<input type="text" id="f_desc" maxlength="200" style="width:100%"></label>
    <button class="primary" onclick="saveItem()">💾 Сохранить</button>
    <button onclick="resetForm()">Очистить</button>
  </div>
</section>
<section>
  <h2>Товары</h2>
  <table><thead><tr><th>Название</th><th>Группа</th><th>Срок</th><th>Цена</th><th>Статус</th><th></th></tr></thead>
  <tbody id="items"></tbody></table>
</section>
<section>
  <h2>Последние покупки</h2>
  <table><thead><tr><th>Когда</th><th>Аккаунт</th><th>Игрок</th><th>Товар</th><th>Цена</th><th>Статус</th><th></th></tr></thead>
  <tbody id="purchases"></tbody></table>
</section>
</main>
<script>
let editId=null,itemsById={};
function esc(t){const d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML}
function esc2(t){return esc(t).split('"').join('&quot;')}
function val(id){return document.getElementById(id).value.trim()}
function setMsg(t,ok){const m=document.getElementById('msg');m.textContent=t;m.className=ok?'ok':'bad'}
async function call(action,body){
  const r=await fetch('/admin/shop/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  let d={};try{d=await r.json()}catch(e){}
  return d;
}
async function act(action,body){
  const d=await call(action,body);
  setMsg(d.success?d.message:(d.error||'Ошибка'),!!d.success);
  load();
  return d;
}
function resetForm(){
  editId=null;document.getElementById('formTitle').textContent='Новый товар';
  ['f_title','f_group','f_desc'].forEach(i=>document.getElementById(i).value='');
  document.getElementById('f_days').value=30;document.getElementById('f_price').value=100;
  document.getElementById('f_order').value=0;document.getElementById('f_enabled').checked=true;
}
function saveItem(){
  act('item_save',{id:editId,title:val('f_title'),group_name:val('f_group'),duration_days:val('f_days'),
    price:val('f_price'),sort_order:val('f_order'),description:val('f_desc'),enabled:document.getElementById('f_enabled').checked})
  .then(d=>{if(d.success)resetForm()});
}
function editItem(id){
  const it=itemsById[id];if(!it)return;
  editId=id;document.getElementById('formTitle').textContent='Изменить товар';
  document.getElementById('f_title').value=it.title;document.getElementById('f_group').value=it.group_name;
  document.getElementById('f_days').value=it.duration_days;document.getElementById('f_price').value=it.price;
  document.getElementById('f_order').value=it.sort_order;document.getElementById('f_desc').value=it.description||'';
  document.getElementById('f_enabled').checked=!!it.enabled;window.scrollTo(0,0);
}
const ST={pending:'<span class="warn">⏳ ждёт выдачи</span>',applied:'<span class="ok">✅ выдано</span>',failed:'<span class="bad">❌ ошибка</span>',refunded:'<span class="muted">↩ возврат</span>'};
function dur(d){return d>0?d+' дн.':'навсегда'}
async function load(){
  let d={};try{d=await (await fetch('/admin/shop/data')).json()}catch(e){return}
  document.getElementById('state').innerHTML='Сервер: '+(d.server_running?'<span class="ok">запущен</span>':'<span class="warn">остановлен</span>')+' · LuckPerms: '+(d.luckperms?'<span class="ok">установлен</span>':'<span class="bad">не установлен (поставь на странице сервера)</span>');
  itemsById={};
  document.getElementById('items').innerHTML=d.items.map(it=>{itemsById[it.id]=it;
    return '<tr><td>'+esc(it.title)+'<div class="muted">'+esc(it.description)+'</div></td><td><code>'+esc(it.group_name)+'</code></td><td>'+dur(it.duration_days)+'</td><td>'+esc(it.price)+' 🪙</td><td>'+(it.enabled?'<span class="ok">в продаже</span>':'<span class="muted">скрыт</span>')+'</td><td><button data-edit="'+it.id+'">Изменить</button> <button class="danger" data-delitem="'+it.id+'">Удалить</button></td></tr>'}).join('')||'<tr><td colspan="6" class="muted">Товаров пока нет</td></tr>';
  document.getElementById('purchases').innerHTML=d.purchases.map(p=>
    '<tr><td class="muted">'+esc((p.created_at||'').replace('T',' ').slice(0,16))+'</td><td>'+esc(p.nickname)+'</td><td>'+esc(p.game_name)+'</td><td>'+esc(p.item_title)+' <span class="muted">('+dur(p.duration_days)+')</span></td><td>'+esc(p.price)+'</td><td>'+(ST[p.status]||esc(p.status))+(p.error?'<div class="muted">'+esc(p.error)+'</div>':'')+'</td><td>'+
    (p.status==='failed'||p.status==='applied'?'<button data-retry="'+p.id+'">Выдать ещё раз</button> ':'')+(p.status==='pending'||p.status==='failed'?'<button class="danger" data-refund="'+p.id+'">Вернуть монеты</button>':'')+'</td></tr>').join('')||'<tr><td colspan="7" class="muted">Покупок пока нет</td></tr>';
}
document.addEventListener('click',function(e){
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.edit)editItem(Number(b.dataset.edit));
  else if(b.dataset.delitem){if(confirm('Удалить товар? Прошлые покупки останутся.'))act('item_delete',{id:Number(b.dataset.delitem)})}
  else if(b.dataset.retry)act('purchase_retry',{id:Number(b.dataset.retry)});
  else if(b.dataset.refund){if(confirm('Вернуть монеты игроку?'))act('purchase_refund',{id:Number(b.dataset.refund)})}
});
load();setInterval(load,6000);
</script>
</body></html>
"""



MODPACK_DIR = os.path.join(BASE_DIR, "server_modpack")
MODPACK_FILES_DIR = os.path.join(MODPACK_DIR, "files")
MODPACK_MANIFEST_PATH = os.path.join(MODPACK_DIR, "manifest.json")
MODPACK_ROOTS = ("mods", "resourcepacks", "shaderpacks", "config")
MODPACK_LOADERS = ("vanilla", "fabric", "forge", "neoforge", "quilt")
MODPACK_UPLOAD_MAX = 200 * 1024 * 1024
MODPACK_MAX_FILES = 400
MODPACK_UPLOAD_EXT = {
    "mods": (".jar",), "resourcepacks": (".zip",), "shaderpacks": (".zip",),
    "config": (".toml", ".json", ".json5", ".cfg", ".properties", ".txt", ".yml", ".yaml", ".conf"),
}
_MODPACK_NAME_RE = re.compile(r"^[A-Za-z0-9._+\-() \[\]]{1,120}$")
_MODPACK_VERSION_RE = re.compile(r"^\d{1,2}\.\d{1,2}(\.\d{1,2})?$")
_MODRINTH_ROOTS = {"mod": "mods", "resourcepack": "resourcepacks", "shader": "shaderpacks"}
_MODRINTH_LOADER_FILTER = {"fabric": ["fabric"], "quilt": ["quilt", "fabric"],
                           "forge": ["forge"], "neoforge": ["neoforge"]}
_modpack_lock = threading.Lock()
os.makedirs(MODPACK_FILES_DIR, exist_ok=True)


def _modpack_empty():
    return {"name": "", "mc_version": "", "loader": "vanilla", "loader_version": "",
            "revision": 0, "updated_at": "", "files": []}


def _modpack_load():
    try:
        with open(MODPACK_MANIFEST_PATH, "r", encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict) and isinstance(m.get("files"), list):
            base = _modpack_empty()
            base.update(m)
            return base
    except (OSError, ValueError):
        pass
    return _modpack_empty()


def _modpack_save(m):
    m["revision"] = int(m.get("revision", 0)) + 1
    m["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = MODPACK_MANIFEST_PATH + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MODPACK_MANIFEST_PATH)


def _modpack_summary():
    """Краткая сводка для публичного статуса сервера (None, если сборка не настроена)."""
    m = _modpack_load()
    if not m["files"]:
        return None
    return {"name": m["name"] or "Сборка сервера", "revision": m["revision"],
            "mc_version": m["mc_version"], "loader": m["loader"],
            "loader_version": m["loader_version"], "files": len(m["files"]),
            "size": sum(int(f.get("size") or 0) for f in m["files"])}


def _modpack_clean_name(name):
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._+\-() \[\]]", "_", base).strip(" .")
    return base[:120]


def _modpack_path_ok(path):
    parts = str(path).split("/")
    if not 2 <= len(parts) <= 5 or parts[0] not in MODPACK_ROOTS:
        return False
    return all(_MODPACK_NAME_RE.fullmatch(p) and not p.startswith(".") for p in parts[1:])


def _modpack_modrinth_pick(project_id, root, mc, loader):
    params = {"game_versions": json.dumps([mc])}
    if root == "mods":
        params["loaders"] = json.dumps(_MODRINTH_LOADER_FILTER[loader])
    versions = _modrinth_get(f"/project/{urllib.parse.quote(project_id, safe='')}/version", params) or []
    for kind in ("release", "beta", "alpha"):
        for v in versions:
            if v.get("version_type") == kind and v.get("files"):
                return v
    return None


def _modpack_modrinth_entry(project, ver, root, optional):
    files = ver.get("files") or []
    fobj = next((x for x in files if x.get("primary")), files[0] if files else None)
    if not fobj:
        raise _McPluginError("У версии нет файлов")
    fname = _modpack_clean_name(fobj.get("filename"))
    hashes = fobj.get("hashes") or {}
    url = fobj.get("url") or ""
    pu = urlparse(url)
    if pu.scheme != "https" or (pu.hostname or "").lower() != "cdn.modrinth.com":
        raise _McPluginError("Modrinth отдал неожиданную ссылку на файл")
    if not hashes.get("sha512"):
        raise _McPluginError("У файла на Modrinth нет контрольной суммы")
    path = f"{root}/{fname}"
    if not fname or not _modpack_path_ok(path):
        raise _McPluginError(f"Недопустимое имя файла: {fobj.get('filename')}")
    return {"id": secrets.token_hex(16), "path": path, "title": project.get("title") or fname,
            "size": int(fobj.get("size") or 0), "sha512": hashes["sha512"].lower(),
            "sha1": (hashes.get("sha1") or "").lower(), "url": url, "source": "modrinth",
            "optional": bool(optional), "project_id": project.get("id"),
            "version_id": ver.get("id"), "version": ver.get("version_number") or ""}


def _modpack_add_modrinth(m, query, optional, with_deps):
    q = (query or "").strip()
    mt = re.search(r"modrinth\.com/(?:mod|resourcepack|shader|plugin|datapack|modpack)/([A-Za-z0-9_\-]+)", q)
    slug = mt.group(1) if mt else q
    if not re.fullmatch(r"[A-Za-z0-9_\-]{2,64}", slug):
        raise _McPluginError("Укажи ссылку на проект Modrinth или его slug")
    mc, loader = m["mc_version"], m["loader"]
    if not mc:
        raise _McPluginError("Сначала укажи версию Minecraft в настройках сборки")
    added, skipped, queue_, seen = [], [], [(slug, 0, optional)], set()
    known = {f.get("project_id") for f in m["files"] if f.get("project_id")}
    while queue_:
        ident, depth, opt = queue_.pop(0)
        if ident in seen:
            continue
        seen.add(ident)
        project = _modrinth_get(f"/project/{urllib.parse.quote(ident, safe='')}")
        if not project:
            raise _McPluginError("Проект не найден на Modrinth")
        title, pid = project.get("title") or ident, project.get("id")
        if pid in known:
            skipped.append(title)
            continue
        root = _MODRINTH_ROOTS.get(project.get("project_type"))
        if not root:
            raise _McPluginError(f"«{title}»: тип «{project.get('project_type')}» не подходит "
                                 f"(нужны моды, ресурспаки или шейдеры)")
        if root == "mods" and loader not in _MODRINTH_LOADER_FILTER:
            raise _McPluginError("Для сборки без загрузчика моды добавлять нельзя: "
                                 "выбери Fabric, Forge, NeoForge или Quilt")
        ver = _modpack_modrinth_pick(pid, root, mc, loader)
        if not ver:
            if depth == 0:
                raise _McPluginError(f"У «{title}» нет файла под Minecraft {mc}"
                                     + (f" и {loader}" if root == "mods" else ""))
            skipped.append(f"{title} (нет версии под {mc})")
            continue
        entry = _modpack_modrinth_entry(project, ver, root, opt)
        m["files"] = [f for f in m["files"] if f["path"] != entry["path"]]
        m["files"].append(entry)
        known.add(pid)
        added.append(entry["title"])
        if with_deps and depth < 4:
            for dep in ver.get("dependencies") or []:
                if dep.get("dependency_type") == "required" and dep.get("project_id") \
                        and dep["project_id"] not in seen:
                    queue_.append((dep["project_id"], depth + 1, False))
    if len(m["files"]) > MODPACK_MAX_FILES:
        raise _McPluginError(f"В сборке не может быть больше {MODPACK_MAX_FILES} файлов")
    return added, skipped


def _modpack_unlink_uploads(m, removed_entries):
    still = {f.get("id") for f in m["files"]}
    for f in removed_entries:
        if f.get("source") == "upload" and f.get("id") not in still and re.fullmatch(r"[0-9a-f]{32}", f.get("id", "")):
            try:
                os.remove(os.path.join(MODPACK_FILES_DIR, f["id"] + ".bin"))
            except OSError:
                pass


@app.route('/api/modpack', methods=['GET'])
@auth_required
def modpack_manifest():
    m = _modpack_load()
    return jsonify({"success": True, "modpack": m if m["files"] else None})


@app.route('/api/modpack/file/<fid>', methods=['GET'])
@auth_required
def modpack_file(fid):
    if not re.fullmatch(r"[0-9a-f]{32}", fid):
        return jsonify({"error": "Не найдено"}), 404
    entry = next((f for f in _modpack_load()["files"] if f.get("id") == fid and f.get("source") == "upload"), None)
    if not entry:
        return jsonify({"error": "Не найдено"}), 404
    return send_from_directory(MODPACK_FILES_DIR, fid + ".bin", as_attachment=True,
                               download_name=os.path.basename(entry["path"]))


@app.route('/admin/modpack', methods=['GET'])
@web_admin_required
def admin_modpack_page():
    return app.response_class(MODPACK_ADMIN_TEMPLATE, mimetype="text/html")


@app.route('/admin/modpack/data', methods=['GET'])
@web_admin_required
def admin_modpack_data():
    return jsonify(_modpack_load())


@app.route('/admin/modpack/<action>', methods=['POST'])
@web_admin_required
def admin_modpack_action(action):
    if not request.is_json:
        return jsonify({"success": False, "error": "Ожидается JSON"}), 400
    data = request.get_json(silent=True) or {}
    admin = g.web_admin['nickname']

    def fail(msg, code=400):
        return jsonify({"success": False, "error": msg}), code

    with _modpack_lock:
        m = _modpack_load()
        if action == "settings":
            name = re.sub(r"[\x00-\x1f\x7f]", "", str(data.get("name", ""))).strip()
            mc = str(data.get("mc_version", "")).strip()
            loader = str(data.get("loader", "vanilla")).strip().lower()
            lv = str(data.get("loader_version", "")).strip()
            if not 1 <= len(name) <= 40:
                return fail("Название: от 1 до 40 символов")
            if not _MODPACK_VERSION_RE.fullmatch(mc):
                return fail("Версия Minecraft вида 1.21.1")
            if loader not in MODPACK_LOADERS:
                return fail("Неизвестный загрузчик")
            if loader != "vanilla" and not re.fullmatch(r"[A-Za-z0-9._+\-]{1,64}", lv):
                return fail("Укажи версию загрузчика (например, 0.16.10 для Fabric)")
            if loader == "vanilla":
                lv = ""
            changed = (m["mc_version"], m["loader"]) != (mc, loader) and bool(m["files"])
            m.update(name=name, mc_version=mc, loader=loader, loader_version=lv)
            _modpack_save(m)
            log_action("modpack_settings", admin, f"{name} {mc} {loader} {lv}")
            msg = "Настройки сохранены"
            if changed:
                msg += ". Файлы с Modrinth подобраны под прежние версию и загрузчик: удали и добавь их заново"
            return jsonify({"success": True, "message": msg})

        if action == "add_modrinth":
            try:
                added, skipped = _modpack_add_modrinth(
                    m, data.get("query"), bool(data.get("optional")), bool(data.get("with_deps", True)))
            except _McPluginError as e:
                return fail(str(e))
            if added:
                _modpack_save(m)
                log_action("modpack_add", admin, ", ".join(added)[:300])
            parts = []
            if added:
                parts.append("Добавлено: " + ", ".join(added))
            if skipped:
                parts.append("Пропущено: " + ", ".join(skipped))
            return jsonify({"success": True, "message": ". ".join(parts) or "Ничего не добавлено"})

        if action in ("remove", "set_optional"):
            fid = str(data.get("id", ""))
            entry = next((f for f in m["files"] if f.get("id") == fid), None)
            if not entry:
                return fail("Файл не найден", 404)
            if action == "remove":
                m["files"] = [f for f in m["files"] if f is not entry]
                _modpack_save(m)
                _modpack_unlink_uploads(m, [entry])
                log_action("modpack_remove", admin, entry["path"])
                return jsonify({"success": True, "message": f"{entry['path']} удалён из сборки"})
            entry["optional"] = bool(data.get("optional"))
            _modpack_save(m)
            return jsonify({"success": True, "message": "Сохранено"})

        if action == "clear":
            removed = list(m["files"])
            m["files"] = []
            _modpack_save(m)
            _modpack_unlink_uploads(m, removed)
            log_action("modpack_clear", admin, f"{len(removed)} файлов")
            return jsonify({"success": True, "message": "Сборка очищена"})
    return fail("Неизвестное действие", 404)


@app.route('/admin/modpack_upload', methods=['POST'])
@web_admin_required
def admin_modpack_upload():
    f = request.files.get('file')
    root = str(request.form.get('root', 'mods'))
    optional = request.form.get('optional') in ('1', 'true', 'on')
    if root not in MODPACK_ROOTS or not f or not f.filename:
        return jsonify({"success": False, "error": "Выбери файл и папку назначения"}), 400
    fname = _modpack_clean_name(f.filename)
    path = f"{root}/{fname}"
    if not fname or not _modpack_path_ok(path) or not fname.lower().endswith(MODPACK_UPLOAD_EXT[root]):
        return jsonify({"success": False, "error": "Недопустимое имя или расширение файла для этой папки: "
                        + ", ".join(MODPACK_UPLOAD_EXT[root])}), 400
    fid = secrets.token_hex(16)
    dest = os.path.join(MODPACK_FILES_DIR, fid + ".bin")
    tmp = dest + ".part"
    try:
        f.save(tmp)
        size = os.path.getsize(tmp)
        if size == 0 or size > MODPACK_UPLOAD_MAX:
            raise ValueError("Файл пустой или больше 200 МБ")
        if fname.lower().endswith((".jar", ".zip")) and not zipfile.is_zipfile(tmp):
            raise ValueError("Файл повреждён: это не zip/jar-архив")
        h = hashlib.sha256()
        with open(tmp, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        with _modpack_lock:
            m = _modpack_load()
            if len(m["files"]) >= MODPACK_MAX_FILES:
                raise ValueError(f"В сборке не может быть больше {MODPACK_MAX_FILES} файлов")
            os.replace(tmp, dest)
            old = [x for x in m["files"] if x["path"] == path]
            m["files"] = [x for x in m["files"] if x["path"] != path]
            m["files"].append({"id": fid, "path": path, "title": fname, "size": size,
                               "sha256": h.hexdigest(), "url": f"/api/modpack/file/{fid}",
                               "source": "upload", "optional": optional})
            _modpack_save(m)
            _modpack_unlink_uploads(m, old)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    log_action("modpack_upload", g.web_admin['nickname'], path)
    return jsonify({"success": True, "message": f"{path} загружен ({size / 1048576:.1f} МБ)"})


COSMETIC_SEED = (
    ("Тыква", "Классическая тыква на голове", "CARVED_PUMPKIN", 0.75, 0.0, 100),
    ("Торт", "Праздничный торт", "CAKE", 0.8, 0.0, 200),
    ("Светокамень", "Светится даже днём", "GLOWSTONE", 0.7, 0.0, 300),
    ("Алмазный блок", "Для тех, кто не бедствует", "DIAMOND_BLOCK", 0.7, 0.0, 500),
    ("Маяк", "Маячок над головой", "BEACON", 0.7, 0.0, 800),
    ("Яйцо дракона", "Редкая реликвия", "DRAGON_EGG", 0.75, 0.0, 1500),
)
_COSMETIC_MATERIAL_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_COSMETIC_TEX_URL_RE = re.compile(r"^https?://textures\.minecraft\.net/texture/([0-9a-f]{16,128})$")
_cosmetic_lock = threading.Lock()


def _cosmetic_normalize_texture(raw):
    """Приводит текстуру головы к ссылке textures.minecraft.net. Принимает ссылку, хэш или base64-значение."""
    import base64
    raw = (raw or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[0-9a-f]{16,128}", raw):
        return f"http://textures.minecraft.net/texture/{raw}"
    m = _COSMETIC_TEX_URL_RE.match(raw)
    if m:
        return f"http://textures.minecraft.net/texture/{m.group(1)}"
    try:
        pad = raw + "=" * (-len(raw) % 4)
        url = json.loads(base64.b64decode(pad).decode("utf-8"))["textures"]["SKIN"]["url"]
    except Exception:
        raise ValueError("Текстура: нужна ссылка textures.minecraft.net, хэш текстуры или base64-значение с minecraft-heads")
    m = _COSMETIC_TEX_URL_RE.match(str(url))
    if not m:
        raise ValueError("Текстура должна лежать на textures.minecraft.net")
    return f"http://textures.minecraft.net/texture/{m.group(1)}"


COSMETIC_MODELS_DIR = os.path.join(BASE_DIR, "cosmetic_models")
COSMETIC_PACK_PATH = os.path.join(BASE_DIR, "cosmetic_pack.zip")
PUBLIC_BASE_FILE = os.path.join(BASE_DIR, ".public_base")
MC_PACK_FORMAT = int(os.environ.get("MAFIN_PACK_FORMAT", "88"))
_cos_pack = {"sha1": "", "size": 0, "count": 0}
_cos_pack_lock = threading.Lock()
_COS_SLUG_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_COS_PARENTS_OK = {"block/block", "minecraft:block/block", "item/generated", "minecraft:item/generated",
                   "item/handheld", "minecraft:item/handheld", "builtin/entity", "minecraft:builtin/entity"}
COS_MODEL_ZIP_MAX = 8 * 1024 * 1024
COS_MODEL_TEXTURE_MAX = 2 * 1024 * 1024


def _cos_slugify(text):
    slug = re.sub(r"[^a-z0-9_]+", "_", str(text or "").lower()).strip("_")[:32]
    return slug or "model"


def _cos_models_list():
    out = []
    try:
        names = sorted(os.listdir(COSMETIC_MODELS_DIR))
    except OSError:
        return out
    for n in names:
        d = os.path.join(COSMETIC_MODELS_DIR, n)
        if _COS_SLUG_RE.match(n) and os.path.isfile(os.path.join(d, "model.json")):
            tex = [f for f in os.listdir(os.path.join(d, "textures"))] if os.path.isdir(os.path.join(d, "textures")) else []
            out.append({"slug": n, "textures": len(tex), "size": _mc_dir_size(d)})
    return out


def _cos_public_base():
    env = (os.environ.get("MAFIN_PUBLIC_URL") or "").strip().rstrip("/")
    if env:
        return env
    try:
        with open(PUBLIC_BASE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _cos_remember_public_base():
    """Запоминает внешний адрес сервера. Вызывается только из запросов авторизованного админа."""
    if os.environ.get("MAFIN_PUBLIC_URL"):
        return
    base = request.host_url.rstrip("/")
    host = request.host.split(":")[0].lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?", base):
        return
    if base != _cos_public_base():
        try:
            with open(PUBLIC_BASE_FILE, "w", encoding="utf-8") as f:
                f.write(base)
        except OSError:
            pass


def _cos_png_ok(data):
    return len(data) <= COS_MODEL_TEXTURE_MAX and data[:8] == b"\x89PNG\r\n\x1a\n"


def _cosmetic_model_import(zip_path, wanted_name):
    """Принимает zip с моделью Java Block/Item (из Blockbench) или ресурспаком, сохраняет модель и текстуры."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ValueError("Это не zip-архив")
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > 400 or sum(i.file_size for i in infos) > 40 * 1024 * 1024:
            raise ValueError("В архиве слишком много или слишком большие файлы")
        pngs, models = {}, []
        for i in infos:
            name = i.filename.replace("\\", "/")
            low = name.lower()
            if low.startswith("__macosx/") or "/." in "/" + low:
                continue
            base = low.rsplit("/", 1)[-1]
            if low.endswith(".png"):
                if i.file_size > COS_MODEL_TEXTURE_MAX:
                    raise ValueError(f"Текстура {base} больше 2 МБ")
                data = zf.read(i)
                if _cos_png_ok(data):
                    pngs[base[:-4]] = data
            elif low.endswith(".json") and i.file_size <= 1024 * 1024 and base != "pack.mcmeta":
                try:
                    obj = json.loads(zf.read(i).decode("utf-8-sig"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("elements"), list) and obj["elements"]:
                    models.append((base[:-5], obj))
    if not models:
        raise ValueError("В архиве нет модели: нужен JSON Java Block/Item (в Blockbench: File → Export → Export Block/Item Model)")
    if len(models) > 1:
        referenced = set()
        for _n, obj in models:
            par = str(obj.get("parent", "")).split("/")[-1].split(":")[-1]
            referenced.add(par)
        free = [m for m in models if m[0] not in referenced] or models
        models = [max(free, key=lambda m: len(m[1]["elements"]))]
    mname, model = models[0]
    if len(model["elements"]) > 400:
        raise ValueError("В модели больше 400 элементов")
    for el in model["elements"]:
        if not (isinstance(el, dict) and isinstance(el.get("from"), list) and isinstance(el.get("to"), list)
                and len(el["from"]) == 3 and len(el["to"]) == 3
                and all(isinstance(v, (int, float)) for v in el["from"] + el["to"])):
            raise ValueError("В модели некорректный элемент (from/to)")
    slug = _cos_slugify(wanted_name or mname)
    base_slug, n = slug, 2
    while os.path.exists(os.path.join(COSMETIC_MODELS_DIR, slug)):
        slug = f"{base_slug}_{n}"
        n += 1
    if model.get("parent") not in _COS_PARENTS_OK:
        model.pop("parent", None)
    tex_names, out_tex = {}, {}
    textures = model.get("textures") if isinstance(model.get("textures"), dict) else {}
    for key, ref in list(textures.items()):
        if not isinstance(ref, str) or ref.startswith("#"):
            continue
        tname = ref.split(":")[-1].split("/")[-1].lower()
        if tname not in pngs:
            raise ValueError(f"В архиве нет текстуры «{tname}.png», на которую ссылается модель")
        if tname not in tex_names:
            clean = _cos_slugify(tname)
            while clean in out_tex:
                clean += "_x"
            tex_names[tname] = clean
            out_tex[clean] = pngs[tname]
        textures[key] = f"mafin:item/hats/{slug}/{tex_names[tname]}"
    if not out_tex:
        raise ValueError("У модели нет ни одной текстуры")
    model["textures"] = textures
    model.pop("overrides", None)
    target = os.path.join(COSMETIC_MODELS_DIR, slug)
    os.makedirs(os.path.join(target, "textures"), exist_ok=True)
    with open(os.path.join(target, "model.json"), "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False)
    for tname, data in out_tex.items():
        with open(os.path.join(target, "textures", tname + ".png"), "wb") as f:
            f.write(data)
    return slug


def _cosmetic_pack_build():
    """Собирает ресурспак со всеми загруженными моделями. Одинаковый набор даёт одинаковый sha1."""
    with _cos_pack_lock:
        slugs = [m["slug"] for m in _cos_models_list()]
        if not slugs:
            _mc_rm(COSMETIC_PACK_PATH)
            _cos_pack.update(sha1="", size=0, count=0)
            return
        tmp = COSMETIC_PACK_PATH + ".part"

        def add(z, name, data):
            zi = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
        with zipfile.ZipFile(tmp, "w") as z:
            add(z, "pack.mcmeta", json.dumps({"pack": {
                "pack_format": MC_PACK_FORMAT, "min_format": MC_PACK_FORMAT, "max_format": 999,
                "description": "Mafin: шапки"}}))
            for slug in slugs:
                d = os.path.join(COSMETIC_MODELS_DIR, slug)
                with open(os.path.join(d, "model.json"), "rb") as f:
                    add(z, f"assets/mafin/models/item/hats/{slug}.json", f.read())
                add(z, f"assets/mafin/items/hat_{slug}.json", json.dumps(
                    {"model": {"type": "minecraft:model", "model": f"mafin:item/hats/{slug}"}}))
                for tname in sorted(os.listdir(os.path.join(d, "textures"))):
                    with open(os.path.join(d, "textures", tname), "rb") as f:
                        add(z, f"assets/mafin/textures/item/hats/{slug}/{tname}", f.read())
        os.replace(tmp, COSMETIC_PACK_PATH)
        h = hashlib.sha1()
        with open(COSMETIC_PACK_PATH, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        _cos_pack.update(sha1=h.hexdigest(), size=os.path.getsize(COSMETIC_PACK_PATH), count=len(slugs))


@app.route('/api/cosmetics/pack.zip', methods=['GET'])
def cosmetics_pack_download():
    if not os.path.isfile(COSMETIC_PACK_PATH):
        return "", 404
    resp = send_from_directory(BASE_DIR, os.path.basename(COSMETIC_PACK_PATH), as_attachment=True,
                               download_name="mafin-hats.zip")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route('/api/cosmetics/model/<slug>.json', methods=['GET'])
def cosmetics_model_json(slug):
    path = os.path.join(COSMETIC_MODELS_DIR, slug, "model.json")
    if not _COS_SLUG_RE.match(slug) or not os.path.isfile(path):
        return "", 404
    with open(path, "r", encoding="utf-8") as f:
        resp = app.response_class(f.read(), mimetype="application/json")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route('/api/cosmetics/model/<slug>/<tex>.png', methods=['GET'])
def cosmetics_model_texture(slug, tex):
    path = os.path.join(COSMETIC_MODELS_DIR, slug, "textures", tex + ".png")
    if not _COS_SLUG_RE.match(slug) or not _COS_SLUG_RE.match(tex) or not os.path.isfile(path):
        return "", 404
    resp = send_from_directory(os.path.join(COSMETIC_MODELS_DIR, slug, "textures"), tex + ".png")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route('/admin/cosmetics/model_upload', methods=['POST'])
@web_admin_required
def admin_cosmetics_model_upload():
    _cos_remember_public_base()
    f = request.files.get('file')
    if not f or not f.filename or not f.filename.lower().endswith('.zip'):
        return jsonify({"success": False, "error": "Выбери архив .zip с моделью"}), 400
    os.makedirs(COSMETIC_MODELS_DIR, exist_ok=True)
    tmp = os.path.join(COSMETIC_MODELS_DIR, "upload.part")
    f.save(tmp)
    try:
        if os.path.getsize(tmp) > COS_MODEL_ZIP_MAX:
            return jsonify({"success": False, "error": "Архив больше 8 МБ"}), 400
        slug = _cosmetic_model_import(tmp, request.form.get('name', '') or os.path.splitext(f.filename)[0])
        _cosmetic_pack_build()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        _mc_rm(tmp)
    log_action("cosmetic_model_upload", g.web_admin['nickname'], slug)
    return jsonify({"success": True, "message": f"Модель «{slug}» загружена, ресурспак пересобран. Выберите её в форме шапки"})


def _cosmetic_server_lines():
    """Строки для плагина: ник|материал|текстура|масштаб|сдвиг. Только игроки, которые сейчас в сессии."""
    with _mc_sessions_lock:
        sessions = dict(_mc_sessions)
    if not sessions:
        return []
    db = get_db()
    rows = db.execute(
        "SELECT e.nickname, c.material, c.texture, c.model, c.scale + e.scale_adj AS scale, c.y_offset + e.y_adj AS y_offset "
        "FROM cosmetic_equipped e "
        "JOIN cosmetics c ON c.id = e.cosmetic_id "
        "JOIN cosmetic_owned o ON o.nickname = e.nickname AND o.cosmetic_id = e.cosmetic_id").fetchall()
    by_nick = {r["nickname"]: r for r in rows}
    lines = []
    for game_lower, sess in sessions.items():
        r = by_nick.get(sess["nick"])
        if not r or not SKIN_NAME_RE.fullmatch(game_lower):
            continue
        lines.append("hat|%s|%s|%s|%.3f|%.3f|%s" % (game_lower, r["material"], r["texture"] or "-",
                                                    min(max(r["scale"], 0.2), 2.0), r["y_offset"], r["model"] or "-"))
    return lines


@app.route('/api/cosmetics/server', methods=['GET'])
def cosmetics_server():
    """Опрашивается плагином MafinCosmetics. Тот же секрет, что у MafinAuth."""
    if not MC_SECRET:
        return jsonify({"error": "disabled"}), 503
    given = request.headers.get('X-Mc-Secret', '')
    if not secrets.compare_digest(given.encode('utf-8'), MC_SECRET.encode('utf-8')):
        return jsonify({"error": "forbidden"}), 403
    lines = _cosmetic_server_lines()
    base = _cos_public_base()
    if _cos_pack["sha1"] and base:
        lines.append("pack|%s/api/cosmetics/pack.zip?v=%s|%s" % (base, _cos_pack["sha1"][:12], _cos_pack["sha1"]))
    return app.response_class("v1\n" + "".join(l + "\n" for l in lines), mimetype="text/plain")


@app.route('/api/cosmetics', methods=['GET'])
@auth_required
def cosmetics_list():
    db = get_db()
    me = g.current_profile['nickname']
    items = db.execute("SELECT id, title, description, price, enabled, material, texture, scale, y_offset, model "
                       "FROM cosmetics ORDER BY sort_order, id").fetchall()
    owned = {r["cosmetic_id"] for r in db.execute(
        "SELECT cosmetic_id FROM cosmetic_owned WHERE nickname = ?", (me,)).fetchall()}
    eq = db.execute("SELECT cosmetic_id, y_adj, scale_adj FROM cosmetic_equipped WHERE nickname = ?", (me,)).fetchone()
    eq_id = eq["cosmetic_id"] if eq else None
    out = []
    for r in items:
        has = r["id"] in owned
        if not r["enabled"] and not has:
            continue
        out.append({"id": r["id"], "title": r["title"], "description": r["description"] or "",
                    "price": r["price"], "owned": has, "equipped": has and r["id"] == eq_id,
                    "material": r["material"], "has_texture": bool(r["texture"]), "model": r["model"] or "",
                    "scale": r["scale"], "y_offset": r["y_offset"]})
    return jsonify({"success": True, "coins": g.current_profile['coins'], "items": out,
                    "equipped_id": eq_id if eq_id in owned else None,
                    "y_adj": eq["y_adj"] if eq else 0, "scale_adj": eq["scale_adj"] if eq else 0})


@app.route('/api/cosmetics/buy', methods=['POST'])
@auth_required
def cosmetics_buy():
    data = request.get_json(silent=True) or {}
    try:
        cid = int(data.get('id'))
    except (TypeError, ValueError):
        return jsonify({"error": "id обязателен"}), 400
    db = get_db()
    code, res = _cosmetic_buy_core(db, g.current_profile['nickname'], cid)
    if code != 200:
        return jsonify({"error": res}), code
    coins = db.execute("SELECT coins FROM profiles WHERE id = ?", (g.current_profile['id'],)).fetchone()['coins']
    return jsonify({"success": True, "total_coins": coins})


@app.route('/api/cosmetics/equip', methods=['POST'])
@auth_required
def cosmetics_equip():
    data = request.get_json(silent=True) or {}
    db = get_db()
    me = g.current_profile['nickname']
    cid = data.get('id')
    if cid is None:
        db.execute("DELETE FROM cosmetic_equipped WHERE nickname = ?", (me,))
        db.commit()
        return jsonify({"success": True, "equipped_id": None})
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return jsonify({"error": "id обязателен"}), 400
    if not db.execute("SELECT 1 FROM cosmetic_owned WHERE nickname = ? AND cosmetic_id = ?", (me, cid)).fetchone():
        return jsonify({"error": "Сначала купи эту шапку"}), 403
    db.execute("INSERT OR REPLACE INTO cosmetic_equipped (nickname, cosmetic_id, y_adj, scale_adj) VALUES (?, ?, 0, 0)",
               (me, cid))
    db.commit()
    return jsonify({"success": True, "equipped_id": cid, "y_adj": 0, "scale_adj": 0})


@app.route('/api/cosmetics/adjust', methods=['POST'])
@auth_required
def cosmetics_adjust():
    data = request.get_json(silent=True) or {}
    try:
        y_adj = float(data.get('y_adj', 0))
        scale_adj = float(data.get('scale_adj', 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Некорректные значения"}), 400
    y_adj = round(min(max(y_adj, -0.6), 0.6), 3)
    scale_adj = round(min(max(scale_adj, -0.5), 0.8), 3)
    db = get_db()
    cur = db.execute("UPDATE cosmetic_equipped SET y_adj = ?, scale_adj = ? WHERE nickname = ?",
                     (y_adj, scale_adj, g.current_profile['nickname']))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Сначала наденьте шапку"}), 409
    return jsonify({"success": True, "y_adj": y_adj, "scale_adj": scale_adj})


_cosmetic_tex_cache = {}


@app.route('/api/cosmetics/texture/<int:cid>', methods=['GET'])
def cosmetics_texture(cid):
    png = _cosmetic_tex_cache.get(cid)
    if png is None:
        row = get_db().execute("SELECT texture FROM cosmetics WHERE id = ?", (cid,)).fetchone()
        url = row["texture"] if row else ""
        if not url or not _COSMETIC_TEX_URL_RE.match(url):
            return "", 404
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "MafinLauncher"}),
                                        timeout=8) as r:
                png = r.read(300 * 1024)
        except Exception:
            return "", 404
        if png[:8] != b"\x89PNG\r\n\x1a\n":
            return "", 404
        _cosmetic_tex_cache[cid] = png
    resp = app.response_class(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route('/admin/cosmetics', methods=['GET'])
@web_admin_required
def admin_cosmetics_page():
    return app.response_class(COSMETICS_ADMIN_TEMPLATE, mimetype="text/html")


@app.route('/admin/cosmetics/data', methods=['GET'])
@web_admin_required
def admin_cosmetics_data():
    db = get_db()
    items = db.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM cosmetic_owned o WHERE o.cosmetic_id = c.id) AS owners "
        "FROM cosmetics c ORDER BY c.sort_order, c.id").fetchall()
    plugin = any(p["name"].lower() == "mafincosmetics" for p in _mc_list_plugins())
    _cos_remember_public_base()
    return jsonify({"items": [dict(r) for r in items], "plugin": plugin, "server_running": bool(_mc_pid()),
                    "models": [dict(m, used=db.execute("SELECT COUNT(*) FROM cosmetics WHERE model = ?",
                                                       (m["slug"],)).fetchone()[0]) for m in _cos_models_list()],
                    "pack": {"ready": bool(_cos_pack["sha1"]), "sha1": _cos_pack["sha1"], "size": _cos_pack["size"],
                             "public_base": _cos_public_base()}})


@app.route('/admin/cosmetics/<action>', methods=['POST'])
@web_admin_required
def admin_cosmetics_action(action):
    if not request.is_json:
        return jsonify({"success": False, "error": "Ожидается JSON"}), 400
    data = request.get_json(silent=True) or {}
    db = get_db()
    admin = g.web_admin['nickname']

    def fail(msg, code=400):
        return jsonify({"success": False, "error": msg}), code

    if action == "item_save":
        title = re.sub(r"[\x00-\x1f\x7f]", "", str(data.get("title", ""))).strip()
        desc = re.sub(r"[\x00-\x1f\x7f]", " ", str(data.get("description", ""))).strip()
        material = str(data.get("material", "")).strip().upper()
        model = str(data.get("model", "")).strip()
        if model:
            if not _COS_SLUG_RE.match(model) or not os.path.isfile(os.path.join(COSMETIC_MODELS_DIR, model, "model.json")):
                return fail("3D-модель не найдена: загрузите её в блоке «3D-модели»")
            material = "PAPER"
            data["texture"] = ""
        try:
            price = float(data.get("price"))
            scale = float(data.get("scale", 0.7))
            y_off = float(data.get("y_offset", 0))
            order = int(data.get("sort_order", 0))
            texture = _cosmetic_normalize_texture(data.get("texture", ""))
        except ValueError as e:
            return fail(str(e) if "Текстур" in str(e) else "Цена, масштаб, сдвиг и порядок должны быть числами")
        except TypeError:
            return fail("Цена, масштаб, сдвиг и порядок должны быть числами")
        if not 1 <= len(title) <= 40:
            return fail("Название: от 1 до 40 символов")
        if len(desc) > 200:
            return fail("Описание: не больше 200 символов")
        if not _COSMETIC_MATERIAL_RE.fullmatch(material):
            return fail("Материал: имя из Bukkit Material, например CARVED_PUMPKIN или PLAYER_HEAD")
        if material == "PLAYER_HEAD" and not texture:
            return fail("Для PLAYER_HEAD нужна текстура головы")
        if not 0 < price <= 1000000:
            return fail("Цена должна быть больше 0")
        if not 0.1 <= scale <= 3.0:
            return fail("Масштаб: от 0.1 до 3")
        if not -2.0 <= y_off <= 3.0:
            return fail("Сдвиг по высоте: от -2 до 3")
        enabled = 1 if data.get("enabled", True) else 0
        item_id = data.get("id")
        if item_id:
            cur = db.execute(
                "UPDATE cosmetics SET title=?, description=?, material=?, texture=?, scale=?, y_offset=?, "
                "price=?, enabled=?, sort_order=?, model=? WHERE id=?",
                (title, desc, material, texture, scale, y_off, price, enabled, order, model, int(item_id)))
            if cur.rowcount == 0:
                return fail("Товар не найден", 404)
        else:
            db.execute(
                "INSERT INTO cosmetics (title, description, material, texture, scale, y_offset, price, enabled, sort_order, model) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", (title, desc, material, texture, scale, y_off, price, enabled, order, model))
        db.commit()
        log_action("cosmetic_save", admin, title)
        return jsonify({"success": True, "message": "Сохранено"})

    if action == "model_delete":
        slug = str(data.get("slug", "")).strip()
        if not _COS_SLUG_RE.match(slug) or not os.path.isdir(os.path.join(COSMETIC_MODELS_DIR, slug)):
            return fail("Модель не найдена", 404)
        used = db.execute("SELECT COUNT(*) AS n FROM cosmetics WHERE model = ?", (slug,)).fetchone()["n"]
        if used:
            return fail(f"Модель используется шапками ({used}). Сначала смените или удалите их")
        shutil.rmtree(os.path.join(COSMETIC_MODELS_DIR, slug), ignore_errors=True)
        _cosmetic_pack_build()
        log_action("cosmetic_model_delete", admin, slug)
        return jsonify({"success": True, "message": f"Модель «{slug}» удалена, ресурспак пересобран"})

    if action == "item_delete":
        try:
            item_id = int(data.get("id"))
        except (TypeError, ValueError):
            return fail("id обязателен")
        owners = db.execute("SELECT COUNT(*) AS n FROM cosmetic_owned WHERE cosmetic_id = ?", (item_id,)).fetchone()["n"]
        if owners:
            return fail(f"У этой шапки есть владельцы ({owners}). Сними её с продажи, а не удаляй")
        db.execute("DELETE FROM cosmetics WHERE id = ?", (item_id,))
        db.commit()
        log_action("cosmetic_delete", admin, str(item_id))
        return jsonify({"success": True, "message": "Удалено"})

    if action == "seed":
        have = {r["title"] for r in db.execute("SELECT title FROM cosmetics").fetchall()}
        added = 0
        for i, (title, desc, material, scale, y_off, price) in enumerate(COSMETIC_SEED):
            if title in have:
                continue
            db.execute("INSERT INTO cosmetics (title, description, material, texture, scale, y_offset, price, enabled, sort_order) "
                       "VALUES (?,?,?,?,?,?,?,1,?)", (title, desc, material, "", scale, y_off, price, i))
            added += 1
        db.commit()
        return jsonify({"success": True, "message": f"Добавлено примеров: {added}"})

    return fail("Неизвестное действие", 404)


def _parse_weekly_rewards():
    raw = os.environ.get("MAFIN_WEEKLY_REWARDS", "500,300,150")
    out = []
    for part in raw.split(","):
        try:
            v = float(part.strip())
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out[:10]


WEEKLY_REWARDS = _parse_weekly_rewards()
try:
    WEEKLY_MIN_MINUTES = float(os.environ.get("MAFIN_WEEKLY_MIN_MINUTES", "30"))
except ValueError:
    WEEKLY_MIN_MINUTES = 30.0
_weekly_lock = threading.Lock()


def _week_start(dt=None):
    dt = dt or datetime.now()
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _weekly_ranking(db, start, end, limit):
    return db.execute(
        "SELECT s.nickname AS nickname, SUM(s.duration_minutes) AS minutes, SUM(s.coins_earned) AS coins, "
        "p.last_seen AS last_seen FROM play_sessions s JOIN profiles p ON p.nickname = s.nickname "
        "WHERE s.start_time >= ? AND s.start_time < ? AND p.is_banned = 0 "
        "GROUP BY s.nickname HAVING SUM(s.duration_minutes) > 0 "
        "ORDER BY minutes DESC, s.nickname ASC LIMIT ?",
        (start.isoformat(), end.isoformat(), limit)).fetchall()


def _weekly_settle(now=None):
    """Выплачивает награды за закончившиеся недели. Безопасно вызывать сколько угодно раз."""
    now = now or datetime.now()
    prev_start = _week_start(now) - timedelta(days=7)
    paid_total = 0
    with _weekly_lock:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        try:
            last = db.execute("SELECT MAX(week_start) AS w FROM weekly_awards").fetchone()["w"]
            if last is None:
                db.execute("INSERT OR IGNORE INTO weekly_awards (week_start, rank, nickname, minutes, coins) "
                           "VALUES (?, 0, '', 0, 0)", (prev_start.date().isoformat(),))
                db.commit()
                return 0
            week = datetime.fromisoformat(last) + timedelta(days=7)
            while week <= prev_start:
                key = week.date().isoformat()
                rows = _weekly_ranking(db, week, week + timedelta(days=7), len(WEEKLY_REWARDS))
                try:
                    db.execute("INSERT INTO weekly_awards (week_start, rank, nickname, minutes, coins) "
                               "VALUES (?, 0, '', 0, 0)", (key,))
                    rank = 0
                    for r in rows:
                        if r["minutes"] < WEEKLY_MIN_MINUTES:
                            break
                        reward = WEEKLY_REWARDS[rank]
                        rank += 1
                        db.execute("INSERT INTO weekly_awards (week_start, rank, nickname, minutes, coins) "
                                   "VALUES (?, ?, ?, ?, ?)", (key, rank, r["nickname"], r["minutes"], reward))
                        db.execute("UPDATE profiles SET coins = coins + ? WHERE nickname = ?", (reward, r["nickname"]))
                        db.execute("INSERT INTO server_logs (action, nickname, details) VALUES (?, ?, ?)",
                                   ("weekly_award", r["nickname"], f"неделя {key}: место {rank}, +{reward:g} монет"))
                        paid_total += 1
                    db.commit()
                except sqlite3.IntegrityError:
                    db.rollback()
                week += timedelta(days=7)
        finally:
            db.close()
    return paid_total


def _weekly_loop():
    while True:
        try:
            _weekly_settle()
        except Exception as e:
            print(f"[weekly] ошибка выплаты наград: {e}")
        time.sleep(3600)


@app.route('/api/top/weekly', methods=['GET'])
def get_top_weekly():
    limit = min(max(request.args.get('limit', 10, type=int), 1), 50)
    try:
        _weekly_settle()
    except Exception as e:
        print(f"[weekly] ошибка выплаты наград: {e}")
    now = datetime.now()
    start = _week_start(now)
    end = start + timedelta(days=7)
    db = get_db()
    rows = _weekly_ranking(db, start, end, limit)
    top = [{"rank": i, "nickname": r["nickname"], "minutes": round(r["minutes"], 1),
            "coins": round(r["coins"] or 0, 1), "is_online": is_online(r["last_seen"])}
           for i, r in enumerate(rows, 1)]
    last = db.execute("SELECT MAX(week_start) AS w FROM weekly_awards WHERE rank > 0").fetchone()["w"]
    last_week = None
    if last:
        winners = db.execute("SELECT rank, nickname, minutes, coins FROM weekly_awards "
                             "WHERE week_start = ? AND rank > 0 ORDER BY rank", (last,)).fetchall()
        last_week = {"week_start": last, "winners": [dict(w) for w in winners]}
    return jsonify({"success": True, "week_start": start.date().isoformat(),
                    "seconds_left": max(0, int((end - now).total_seconds())),
                    "rewards": WEEKLY_REWARDS, "min_minutes": WEEKLY_MIN_MINUTES,
                    "top": top, "last_week": last_week})


_ADMIN_EXTRA_CSS = """
* {box-sizing:border-box}
body{background:#1a1d23;color:#e1e4e8;font-family:'Segoe UI',Arial,sans-serif;margin:0}
header{background:#242830;padding:16px 28px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #2d323c}
header h1{font-size:18px;margin:0;color:#43b581}
header a{color:#7a8599;text-decoration:none;font-size:13px;margin-left:14px}
header a:hover{color:#fff}
main{padding:24px 28px;max-width:1100px;margin:0 auto}
section{background:#242830;border-radius:10px;padding:20px;margin-bottom:20px}
section h2{margin-top:0;font-size:15px;color:#43b581}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-top:10px}
label{font-size:12px;color:#7a8599;display:flex;flex-direction:column;gap:4px}
input[type=text],input[type=number],select{padding:8px;border-radius:6px;border:1px solid #2d323c;background:#2d323c;color:#fff;font-size:13px}
button{background:#2d323c;color:#e1e4e8;border:none;padding:8px 14px;border-radius:6px;cursor:pointer;font-size:13px}
button:hover{background:#3a3f4a}
button.primary{background:#43b581;color:#fff;font-weight:bold}
button.danger:hover{background:#f04747}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #2d323c}
th{color:#7a8599;font-weight:normal;font-size:12px}
.ok{color:#43b581}.bad{color:#f04747}.warn{color:#faa61a}.muted{color:#7a8599;font-size:12px}
#msg{margin-top:12px;font-size:13px;min-height:18px}
"""

_ADMIN_EXTRA_JS = """
function esc(t){const d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML}
function val(id){return document.getElementById(id).value.trim()}
function setMsg(t,ok){const m=document.getElementById('msg');m.textContent=t;m.className=ok?'ok':'bad'}
"""

COSMETICS_ADMIN_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Mafin Launcher — Косметика</title>
<style>""" + _ADMIN_EXTRA_CSS + """</style></head>
<body>
<header><h1>🎩 Косметика (шапки)</h1>
<div><a href="/admin/shop">🛒 Магазин</a><a href="/admin/minecraft">🖥 Сервер</a><a href="/admin/dashboard">← Панель</a><a href="/admin/logout">Выйти</a></div></header>
<main>
<section>
  <h2>Состояние</h2>
  <div id="state" class="muted">Загрузка…</div>
  <p class="muted">Игрок покупает шапку в лаунчере за монеты и надевает её. Плагин <b>MafinCosmetics</b> на сервере раз в несколько секунд
  спрашивает у этого сервера, у кого что надето, и показывает шапку над головой всем игрокам. Плагин нужно собрать из папки
  <code>mafin-cosmetics</code> и загрузить на странице сервера (его секрет и адрес пропишутся автоматически при запуске сервера).</p>
  <p class="muted"><b>Материал</b> — имя из Bukkit Material (<code>CARVED_PUMPKIN</code>, <code>CAKE</code>, <code>BEACON</code>…).
  Для своей формы возьми <code>PLAYER_HEAD</code> и вставь текстуру с minecraft-heads.com (поле «Value», ссылка или хэш).
  Масштаб и сдвиг подбираются глазами: в плагине есть команда <code>/mafinhat preview МАТЕРИАЛ масштаб сдвиг</code> для оператора.</p>
  <div class="row"><button onclick="act('seed',{})">➕ Добавить примеры</button></div>
  <div id="msg"></div>
</section>
<section>
  <h2>🧊 3D-модели</h2>
  <p class="muted">Загрузите готовую модель в формате Java Block/Item: zip с файлом <code>.json</code> и текстурами <code>.png</code> (в Blockbench: File → Export → Export Block/Item Model; подходят и модели с сайтов вроде mcmodels.net). Сервер сам соберёт ресурспак, раздаст его игрокам при входе и обновит без перезапуска. Загруженную модель выберите в форме шапки ниже.</p>
  <div class="row">
    <label>Модель (.zip) <input type="file" id="m_file" accept=".zip"></label>
    <label>Название <input type="text" id="m_name" maxlength="32" style="width:160px;flex:none" placeholder="необязательно"></label>
    <button class="primary" onclick="uploadModel()">⬆ Загрузить модель</button>
  </div>
  <div id="packinfo" class="muted" style="margin:10px 0"></div>
  <div id="models" class="muted">—</div>
</section>
<section>
  <h2 id="formTitle">Новая шапка</h2>
  <div class="row">
    <label>Название<input type="text" id="f_title" maxlength="40" style="width:180px"></label>
    <label>Материал<input type="text" id="f_material" maxlength="64" style="width:180px" placeholder="CARVED_PUMPKIN"></label>
    <label>3D-модель<select id="f_model" style="width:170px"><option value="">— нет —</option></select></label>
    <label>Цена, монет<input type="number" id="f_price" value="200" min="0" step="any" style="width:100px"></label>
    <label>Масштаб<input type="number" id="f_scale" value="0.7" min="0.1" max="3" step="0.05" style="width:80px"></label>
    <label>Сдвиг по высоте<input type="number" id="f_y" value="0" min="-2" max="3" step="0.05" style="width:100px"></label>
    <label>Порядок<input type="number" id="f_order" value="0" style="width:70px"></label>
    <label style="flex-direction:row;align-items:center;gap:6px;padding-bottom:8px"><input type="checkbox" id="f_enabled" checked> в продаже</label>
  </div>
  <div class="row">
    <label style="flex:1">Текстура головы (для PLAYER_HEAD)<input type="text" id="f_texture" style="width:100%"></label>
  </div>
  <div class="row">
    <label style="flex:1">Описание<input type="text" id="f_desc" maxlength="200" style="width:100%"></label>
    <button class="primary" onclick="saveItem()">💾 Сохранить</button>
    <button onclick="resetForm()">Очистить</button>
  </div>
</section>
<section>
  <h2>Шапки</h2>
  <table><thead><tr><th>Название</th><th>Материал</th><th>Цена</th><th>Владельцев</th><th>Статус</th><th></th></tr></thead>
  <tbody id="items"></tbody></table>
</section>
</main>
<script>""" + _ADMIN_EXTRA_JS + """
let editId=null,byId={};
async function call(action,body){
  const r=await fetch('/admin/cosmetics/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  let d={};try{d=await r.json()}catch(e){}
  return d;
}
async function act(action,body){const d=await call(action,body);setMsg(d.success?d.message:(d.error||'Ошибка'),!!d.success);load();return d}
async function uploadModel(){
  const inp=document.getElementById('m_file');
  if(!inp.files.length){setMsg('Выбери архив .zip с моделью',false);return}
  const fd=new FormData();fd.append('file',inp.files[0]);fd.append('name',val('m_name'));
  setMsg('Загружаю модель…',true);
  let d={};
  try{const r=await fetch('/admin/cosmetics/model_upload',{method:'POST',body:fd});d=await r.json()}catch(e){}
  setMsg(d.success?d.message:(d.error||'Ошибка загрузки'),!!d.success);
  inp.value='';load();
}
function resetForm(){
  editId=null;document.getElementById('formTitle').textContent='Новая шапка';
  document.getElementById('f_model').value='';
  ['f_title','f_material','f_texture','f_desc'].forEach(i=>document.getElementById(i).value='');
  document.getElementById('f_price').value=200;document.getElementById('f_scale').value=0.7;
  document.getElementById('f_y').value=0;document.getElementById('f_order').value=0;document.getElementById('f_enabled').checked=true;
}
function saveItem(){
  act('item_save',{id:editId,title:val('f_title'),material:val('f_material'),texture:val('f_texture'),model:val('f_model'),price:val('f_price'),
    scale:val('f_scale'),y_offset:val('f_y'),sort_order:val('f_order'),description:val('f_desc'),
    enabled:document.getElementById('f_enabled').checked}).then(d=>{if(d.success)resetForm()});
}
function editItem(id){
  const it=byId[id];if(!it)return;
  editId=id;document.getElementById('formTitle').textContent='Изменить шапку';
  document.getElementById('f_title').value=it.title;document.getElementById('f_material').value=it.material;
  document.getElementById('f_texture').value=it.texture||'';document.getElementById('f_price').value=it.price;
  document.getElementById('f_model').value=it.model||'';
  document.getElementById('f_scale').value=it.scale;document.getElementById('f_y').value=it.y_offset;
  document.getElementById('f_order').value=it.sort_order;document.getElementById('f_desc').value=it.description||'';
  document.getElementById('f_enabled').checked=!!it.enabled;window.scrollTo(0,0);
}
async function load(){
  let d={};try{d=await (await fetch('/admin/cosmetics/data')).json()}catch(e){return}
  document.getElementById('state').innerHTML='Сервер: '+(d.server_running?'<span class="ok">запущен</span>':'<span class="warn">остановлен</span>')+
    ' · Плагин MafinCosmetics: '+(d.plugin?'<span class="ok">установлен</span>':'<span class="bad">не установлен — шапки не будут видны в игре</span>');
  const sel=document.getElementById('f_model'),cur=sel.value;
  sel.innerHTML='<option value="">— нет —</option>'+(d.models||[]).map(m=>'<option value="'+esc(m.slug)+'">'+esc(m.slug)+'</option>').join('');
  sel.value=cur;
  const pk=d.pack||{};
  document.getElementById('packinfo').innerHTML=pk.ready?('Ресурспак готов: '+(d.models||[]).length+' мод., '+Math.round(pk.size/1024)+' КБ. '+(pk.public_base?'Игроки скачивают его с <code>'+esc(pk.public_base)+'/api/cosmetics/pack.zip</code>':'<span class="bad">Внешний адрес сервера неизвестен: откройте эту страницу по внешнему адресу (не localhost) или задайте MAFIN_PUBLIC_URL, иначе игроки не получат пак</span>')):'Ресурспака пока нет: загрузите первую модель.';
  document.getElementById('models').innerHTML=(d.models||[]).length?(d.models).map(m=>'<div class="item"><div><div class="t">'+esc(m.slug)+'</div><div class="d">текстур: '+m.textures+' · '+Math.round(m.size/1024)+' КБ · шапок: '+m.used+'</div></div><button class="danger" data-mdel="'+esc(m.slug)+'">Удалить</button></div>').join(''):'Моделей пока нет';
  byId={};
  document.getElementById('items').innerHTML=d.items.map(it=>{byId[it.id]=it;
    return '<tr><td>'+esc(it.title)+'<div class="muted">'+esc(it.description)+'</div></td><td>'+(it.model?'🧊 <code>'+esc(it.model)+'</code>':'<code>'+esc(it.material)+'</code>')+'</td><td>'+esc(it.price)+' 🪙</td><td>'+it.owners+'</td><td>'+
    (it.enabled?'<span class="ok">в продаже</span>':'<span class="muted">скрыта</span>')+'</td><td><button data-edit="'+it.id+'">Изменить</button> <button class="danger" data-del="'+it.id+'">Удалить</button></td></tr>'}).join('')||
    '<tr><td colspan="6" class="muted">Шапок пока нет. Нажми «Добавить примеры» или создай свою.</td></tr>';
}
document.addEventListener('click',function(e){
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.edit)editItem(Number(b.dataset.edit));
  else if(b.dataset.del){if(confirm('Удалить шапку?'))act('item_delete',{id:Number(b.dataset.del)})}
  else if(b.dataset.mdel){if(confirm('Удалить модель '+b.dataset.mdel+'?'))act('model_delete',{slug:b.dataset.mdel})}
});
load();setInterval(load,8000);
</script>
</body></html>
"""

MODPACK_ADMIN_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Mafin Launcher — Сборка сервера</title>
<style>""" + _ADMIN_EXTRA_CSS + """</style></head>
<body>
<header><h1>🧩 Сборка сервера</h1>
<div><a href="/admin/minecraft">🖥 Сервер</a><a href="/admin/dashboard">← Панель</a><a href="/admin/logout">Выйти</a></div></header>
<main>
<section>
  <h2>Что это</h2>
  <p class="muted">Рекомендуемый набор для клиента: загрузчик, моды, ресурспаки, шейдеры и конфиги. Игрок нажимает одну кнопку в лаунчере,
  и у него появляется сборка с этими файлами; при каждом изменении здесь лаунчер предложит обновиться. Файлы с Modrinth игроки качают
  напрямую оттуда (с проверкой хэша), свои файлы отдаёт этот сервер. Сам Paper-сервер модов не использует: сборка нужна клиентам.</p>
  <div id="msg"></div>
</section>
<section>
  <h2>Настройки <span class="muted" id="rev"></span></h2>
  <div class="row">
    <label>Название<input type="text" id="s_name" maxlength="40" style="width:200px"></label>
    <label>Версия Minecraft<input type="text" id="s_mc" style="width:110px" placeholder="1.21.1"></label>
    <label>Загрузчик<select id="s_loader"><option>vanilla</option><option>fabric</option><option>forge</option><option>neoforge</option><option>quilt</option></select></label>
    <label>Версия загрузчика<input type="text" id="s_lv" style="width:140px" placeholder="0.16.10"></label>
    <button class="primary" onclick="saveSettings()">💾 Сохранить</button>
  </div>
</section>
<section>
  <h2>Добавить с Modrinth</h2>
  <div class="row">
    <label style="flex:1">Ссылка или slug<input type="text" id="m_q" style="width:100%" placeholder="https://modrinth.com/mod/sodium"></label>
    <label style="flex-direction:row;align-items:center;gap:6px;padding-bottom:8px"><input type="checkbox" id="m_deps" checked> с зависимостями</label>
    <label style="flex-direction:row;align-items:center;gap:6px;padding-bottom:8px"><input type="checkbox" id="m_opt"> необязательный</label>
    <button class="primary" onclick="addModrinth()">➕ Добавить</button>
  </div>
  <p class="muted">Подбирается свежий релиз под версию и загрузчик из настроек. Моды, ресурспаки и шейдеры определяются автоматически.</p>
</section>
<section>
  <h2>Загрузить свой файл</h2>
  <div class="row">
    <label>Папка<select id="u_root"><option value="mods">mods</option><option value="resourcepacks">resourcepacks</option><option value="shaderpacks">shaderpacks</option><option value="config">config</option></select></label>
    <label>Файл<input type="file" id="u_file"></label>
    <label style="flex-direction:row;align-items:center;gap:6px;padding-bottom:8px"><input type="checkbox" id="u_opt"> необязательный</label>
    <button class="primary" onclick="upload()">⬆ Загрузить</button>
  </div>
</section>
<section>
  <h2>Файлы сборки <span class="muted" id="count"></span></h2>
  <div class="row" style="margin:0 0 10px"><button class="danger" onclick="if(confirm('Убрать все файлы из сборки?'))act('clear',{})">Очистить сборку</button></div>
  <table><thead><tr><th>Путь</th><th>Название</th><th>Источник</th><th>Размер</th><th>Необязательный</th><th></th></tr></thead>
  <tbody id="files"></tbody></table>
</section>
</main>
<script>""" + _ADMIN_EXTRA_JS + """
async function call(action,body){
  const r=await fetch('/admin/modpack/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  let d={};try{d=await r.json()}catch(e){}
  return d;
}
async function act(action,body){setMsg('Работаю…',true);const d=await call(action,body);setMsg(d.success?d.message:(d.error||'Ошибка'),!!d.success);load();return d}
function saveSettings(){act('settings',{name:val('s_name'),mc_version:val('s_mc'),loader:val('s_loader'),loader_version:val('s_lv')})}
function addModrinth(){act('add_modrinth',{query:val('m_q'),with_deps:document.getElementById('m_deps').checked,optional:document.getElementById('m_opt').checked}).then(d=>{if(d.success)document.getElementById('m_q').value=''})}
async function upload(){
  const f=document.getElementById('u_file').files[0];if(!f){setMsg('Выбери файл',false);return}
  const fd=new FormData();fd.append('file',f);fd.append('root',val('u_root'));fd.append('optional',document.getElementById('u_opt').checked?'1':'0');
  setMsg('Загружаю…',true);
  const r=await fetch('/admin/modpack_upload',{method:'POST',body:fd});let d={};try{d=await r.json()}catch(e){}
  setMsg(d.success?d.message:(d.error||'Ошибка'),!!d.success);if(d.success)document.getElementById('u_file').value='';load();
}
let first=true;
async function load(){
  let d={};try{d=await (await fetch('/admin/modpack/data')).json()}catch(e){return}
  document.getElementById('rev').textContent=d.revision?('· ревизия '+d.revision+(d.updated_at?', '+d.updated_at.replace('T',' ').slice(0,16):'')):'';
  if(first||document.activeElement===document.body){
    document.getElementById('s_name').value=d.name||'';document.getElementById('s_mc').value=d.mc_version||'';
    document.getElementById('s_loader').value=d.loader||'vanilla';document.getElementById('s_lv').value=d.loader_version||'';first=false;
  }
  const total=d.files.reduce((a,f)=>a+(f.size||0),0);
  document.getElementById('count').textContent='· '+d.files.length+' шт., '+(total/1048576).toFixed(1)+' МБ';
  document.getElementById('files').innerHTML=d.files.map(f=>'<tr><td><code>'+esc(f.path)+'</code></td><td>'+esc(f.title)+(f.version?'<div class="muted">'+esc(f.version)+'</div>':'')+'</td><td>'+(f.source==='modrinth'?'Modrinth':'свой файл')+'</td><td>'+((f.size||0)/1048576).toFixed(2)+' МБ</td><td><input type="checkbox" data-opt="'+f.id+'" '+(f.optional?'checked':'')+'></td><td><button class="danger" data-rm="'+f.id+'">Убрать</button></td></tr>').join('')||'<tr><td colspan="6" class="muted">Файлов пока нет</td></tr>';
}
document.addEventListener('click',function(e){const b=e.target.closest('button[data-rm]');if(b&&confirm('Убрать файл из сборки?'))act('remove',{id:b.dataset.rm})});
document.addEventListener('change',function(e){const c=e.target.closest('input[data-opt]');if(c)act('set_optional',{id:c.dataset.opt,optional:c.checked})});
load();
</script>
</body></html>
"""



TG_PROD_TOKEN_CODE = ""
TG_TEST_TOKEN_CODE = ""
TG_PROD_NEWS_CHAT_CODE = ""
TG_TEST_NEWS_CHAT_CODE = ""

TG_BOTS = {
    "prod": {"token": os.environ.get("TG_PROD_TOKEN", TG_PROD_TOKEN_CODE).strip(),
             "news_chat": os.environ.get("TG_PROD_NEWS_CHAT", TG_PROD_NEWS_CHAT_CODE).strip(),
             "title": "Mafin (прод)", "username": ""},
    "test": {"token": os.environ.get("TG_TEST_TOKEN", TG_TEST_TOKEN_CODE).strip(),
             "news_chat": os.environ.get("TG_TEST_NEWS_CHAT", TG_TEST_NEWS_CHAT_CODE).strip(),
             "title": "Mafin (тест dc2)", "username": ""},
}
TG_CODE_TTL_SECONDS = 600

def _tg_enabled(bot):
    return bot in TG_BOTS and bool(TG_BOTS[bot]["token"])

def _tg_call(bot, method, payload=None, timeout=35):
    cfg = TG_BOTS[bot]
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{cfg['token']}/{method}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def _tg_send(bot, chat_id, text, markup=None):
    if not _tg_enabled(bot):
        return False
    prefix = "[TEST dc2] " if bot == "test" else ""
    payload = {"chat_id": chat_id, "text": (prefix + text)[:4000], "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    for attempt in range(2):
        try:
            _tg_call(bot, "sendMessage", payload, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 403 and isinstance(chat_id, int):
                try:
                    db = _tg_db()
                    db.execute("DELETE FROM tg_links WHERE bot = ? AND chat_id = ?", (bot, chat_id))
                    db.commit()
                    db.close()
                except Exception:
                    pass
                return False
            if e.code == 429 and attempt == 0:
                try:
                    wait = int(json.loads(e.read().decode("utf-8")).get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(min(max(wait, 1), 10))
                continue
            print(f"[tg:{bot}] send error: {e}")
            return False
        except Exception as e:
            print(f"[tg:{bot}] send error: {e}")
            return False
    return False

def _tg_edit(bot, chat_id, message_id, text, markup=None):
    prefix = "[TEST dc2] " if bot == "test" else ""
    payload = {"chat_id": chat_id, "message_id": message_id, "text": (prefix + text)[:4000],
               "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    try:
        _tg_call(bot, "editMessageText", payload, timeout=10)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            print(f"[tg:{bot}] edit error: {e}")
    except Exception as e:
        print(f"[tg:{bot}] edit error: {e}")

def _tg_answer(bot, cq_id, text=""):
    try:
        _tg_call(bot, "answerCallbackQuery", {"callback_query_id": cq_id, "text": text[:190]}, timeout=10)
    except Exception:
        pass

def _tg_db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    return db

def _tg_init_tables():
    db = _tg_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tg_links (
            bot TEXT NOT NULL, nickname TEXT NOT NULL, chat_id INTEGER NOT NULL,
            notify_friends INTEGER NOT NULL DEFAULT 1, notify_news INTEGER NOT NULL DEFAULT 1,
            linked_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (bot, nickname));
        CREATE INDEX IF NOT EXISTS idx_tg_links_chat ON tg_links(bot, chat_id);
        CREATE TABLE IF NOT EXISTS tg_link_codes (
            code TEXT PRIMARY KEY, bot TEXT NOT NULL, nickname TEXT NOT NULL, expires REAL NOT NULL);
    """)
    db.commit(); db.close()

_tg_notify_last = {}

def _tg_notify_friends_online(nickname):
    """Сообщает друзьям (привязавшим Telegram), что игрок появился в сети."""
    now = time.time()
    if now - _tg_notify_last.get(nickname, 0) < 600:
        return
    _tg_notify_last[nickname] = now

    def work():
        try:
            db = _tg_db()
            me = db.execute("SELECT is_banned FROM profiles WHERE nickname = ?", (nickname,)).fetchone()
            if not me or me["is_banned"]:
                db.close()
                return
            rows = db.execute(
                "SELECT t.bot, t.chat_id FROM tg_links t JOIN friendships f ON f.status='accepted' AND "
                "((f.requester = ? AND f.addressee = t.nickname) OR (f.addressee = ? AND f.requester = t.nickname)) "
                "WHERE t.notify_friends = 1", (nickname, nickname)).fetchall()
            db.close()
            for r in rows:
                _tg_send(r["bot"], r["chat_id"], f"🟢 {nickname} сейчас в сети")
                time.sleep(0.05)
        except Exception as e:
            print(f"[tg] friends notify error: {e}")
    if any(_tg_enabled(b) for b in TG_BOTS):
        threading.Thread(target=work, daemon=True).start()

def _tg_broadcast_news(title, body):
    def work():
        try:
            db = _tg_db()
            rows = db.execute("SELECT bot, chat_id FROM tg_links WHERE notify_news = 1").fetchall()
            db.close()
            text = f"📰 {title}\n\n{body[:3500]}"
            for r in rows:
                _tg_send(r["bot"], r["chat_id"], text)
                time.sleep(0.05)
            for bot, cfg in TG_BOTS.items():
                if cfg["news_chat"]:
                    _tg_send(bot, cfg["news_chat"], text)
        except Exception as e:
            print(f"[tg] news broadcast error: {e}")
    if any(_tg_enabled(b) for b in TG_BOTS):
        threading.Thread(target=work, daemon=True).start()

def _tg_kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}

def _tg_menu_markup():
    return _tg_kb([[("👥 Друзья", "m:friends"), ("👤 Профиль", "m:me")],
                   [("🎩 Шапки", "m:hats"), ("⚙️ Настроить шапку", "m:hat")],
                   [("🔔 Уведомления", "m:settings")]])

_TG_HELP = ("Что умею:\n"
            "/friends — кто из друзей в сети\n/me — профиль и монеты\n"
            "/hats — купить и надеть шапку\n/hat — настроить шапку стрелками\n"
            "/settings — уведомления\n/news on|off, /notify on|off — быстрые переключатели\n"
            "/unlink — отвязать аккаунт")

def _tg_fmt_signed(v):
    return f"{v:+.2f}"

def _tg_view_friends(db, nick):
    rows = db.execute(
        "SELECT p.nickname, p.last_seen FROM friendships f JOIN profiles p ON p.nickname = "
        "CASE WHEN f.requester = ? THEN f.addressee ELSE f.requester END "
        "WHERE f.status = 'accepted' AND (f.requester = ? OR f.addressee = ?)", (nick, nick, nick)).fetchall()
    if not rows:
        return "У вас пока нет друзей.", _tg_menu_markup()
    rows = sorted(rows, key=lambda r: (not is_online(r["last_seen"]), r["nickname"].lower()))
    return "\n".join(f"{'🟢' if is_online(r['last_seen']) else '⚪'} {r['nickname']}" for r in rows), _tg_menu_markup()

def _tg_view_me(db, nick):
    p = db.execute("SELECT coins, total_playtime_minutes, total_launches, created_at FROM profiles "
                   "WHERE nickname = ?", (nick,)).fetchone()
    if not p:
        return "Профиль не найден.", None
    minutes = int(round(p["total_playtime_minutes"] or 0))
    return (f"👤 {nick}\n🪙 Монет: {int(p['coins'] or 0)}\n⏱ В игре: {minutes // 60} ч {minutes % 60} мин\n"
            f"🚀 Запусков: {p['total_launches'] or 0}\n📅 Регистрация: {(p['created_at'] or '')[:10]}"), _tg_menu_markup()

def _tg_view_settings(db, chat_link):
    f, n = bool(chat_link["notify_friends"]), bool(chat_link["notify_news"])
    return ("🔔 Уведомления\nНажмите, чтобы переключить.",
            _tg_kb([[(f"👥 Друзья в сети: {'вкл' if f else 'выкл'}", "nt:f")],
                    [(f"📰 Новости: {'вкл' if n else 'выкл'}", "nt:n")],
                    [("← Меню", "m:menu")]]))

def _tg_view_hats(db, nick):
    coins = db.execute("SELECT coins FROM profiles WHERE nickname = ?", (nick,)).fetchone()
    items = db.execute("SELECT id, title, price, enabled FROM cosmetics ORDER BY sort_order, id").fetchall()
    owned = {r["cosmetic_id"] for r in db.execute(
        "SELECT cosmetic_id FROM cosmetic_owned WHERE nickname = ?", (nick,)).fetchall()}
    eq = db.execute("SELECT cosmetic_id FROM cosmetic_equipped WHERE nickname = ?", (nick,)).fetchone()
    eq_id = eq["cosmetic_id"] if eq and eq["cosmetic_id"] in owned else None
    lines, rows = [f"🎩 Шапки\n🪙 Монет: {int((coins['coins'] if coins else 0) or 0)}\n"], []
    shown = 0
    for it in items:
        has = it["id"] in owned
        if (not it["enabled"] and not has) or shown >= 20:
            continue
        shown += 1
        if it["id"] == eq_id:
            lines.append(f"✅ {it['title']} — надета")
            rows.append([(f"Снять «{it['title']}»", "he:0")])
        elif has:
            lines.append(f"✔ {it['title']} — куплена")
            rows.append([(f"🎩 Надеть «{it['title']}»", f"he:{it['id']}")])
        else:
            lines.append(f"• {it['title']} — {it['price']:g} 🪙")
            rows.append([(f"🪙 Купить «{it['title']}» ({it['price']:g})", f"hb:{it['id']}")])
    if not shown:
        lines.append("Шапок пока нет в продаже.")
    if eq_id:
        rows.append([("⚙️ Настроить надетую шапку", "m:hat")])
    rows.append([("← Меню", "m:menu")])
    return "\n".join(lines), _tg_kb(rows)

def _tg_view_hat_adjust(db, nick):
    row = db.execute("SELECT e.cosmetic_id, e.y_adj, e.scale_adj, c.title FROM cosmetic_equipped e "
                     "JOIN cosmetics c ON c.id = e.cosmetic_id "
                     "JOIN cosmetic_owned o ON o.nickname = e.nickname AND o.cosmetic_id = e.cosmetic_id "
                     "WHERE e.nickname = ?", (nick,)).fetchone()
    if not row:
        return "Шапка не надета. Откройте /hats, купите и наденьте её.", _tg_kb([[("🎩 Шапки", "m:hats")]])
    return (f"🎩 {row['title']}\nВысота: {_tg_fmt_signed(row['y_adj'])}\nРазмер: {_tg_fmt_signed(row['scale_adj'])}\n\n"
            "В игре шапка обновится через несколько секунд."), _tg_kb([
        [("⬆️ Выше", "ha:y:+"), ("⬇️ Ниже", "ha:y:-")],
        [("➕ Больше", "ha:s:+"), ("➖ Меньше", "ha:s:-")],
        [("Сбросить", "ha:r"), ("← Меню", "m:menu")]])

def _tg_view(db, link, name):
    nick = link["nickname"]
    if name == "friends":
        return _tg_view_friends(db, nick)
    if name == "me":
        return _tg_view_me(db, nick)
    if name == "hats":
        return _tg_view_hats(db, nick)
    if name == "hat":
        return _tg_view_hat_adjust(db, nick)
    if name == "settings":
        return _tg_view_settings(db, link)
    return "Главное меню", _tg_menu_markup()

def _cosmetic_buy_core(db, nick, cid):
    prof = db.execute("SELECT id, is_banned FROM profiles WHERE nickname = ?", (nick,)).fetchone()
    if not prof or prof["is_banned"]:
        return 403, "Профиль недоступен"
    item = db.execute("SELECT * FROM cosmetics WHERE id = ? AND enabled = 1", (cid,)).fetchone()
    if not item:
        return 404, "Товар не найден или снят с продажи"
    price = float(item["price"])
    with _cosmetic_lock:
        try:
            db.execute("INSERT INTO cosmetic_owned (nickname, cosmetic_id, price_paid) VALUES (?, ?, ?)",
                       (nick, cid, price))
        except sqlite3.IntegrityError:
            db.rollback()
            return 409, "Это уже куплено"
        cur = db.execute("UPDATE profiles SET coins = coins - ? WHERE id = ? AND coins >= ?",
                         (price, prof["id"], price))
        if cur.rowcount == 0:
            db.rollback()
            return 402, "Недостаточно монет"
        db.commit()
    log_action("cosmetic_buy", nick, f"{item['title']} (-{price:g} монет)")
    return 200, item["title"]

def _tg_handle_message(bot, msg):
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text or (msg.get("chat") or {}).get("type") != "private":
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    db = _tg_db()
    try:
        link = db.execute("SELECT * FROM tg_links WHERE bot = ? AND chat_id = ?", (bot, chat_id)).fetchone()
        if cmd == "/start" and arg:
            row = db.execute("SELECT * FROM tg_link_codes WHERE code = ? AND bot = ?", (arg, bot)).fetchone()
            if not row or row["expires"] < time.time():
                _tg_send(bot, chat_id, "Код недействителен или истёк. Нажмите «Привязать» в лаунчере или кабинете ещё раз.")
                return
            db.execute("DELETE FROM tg_link_codes WHERE code = ?", (arg,))
            db.execute("DELETE FROM tg_links WHERE bot = ? AND chat_id = ?", (bot, chat_id))
            db.execute("INSERT OR REPLACE INTO tg_links (bot, nickname, chat_id) VALUES (?, ?, ?)",
                       (bot, row["nickname"], chat_id))
            db.commit()
            _tg_send(bot, chat_id, f"✅ Аккаунт {row['nickname']} привязан.\n\n{_TG_HELP}", _tg_menu_markup())
        elif cmd == "/start" and not link:
            _tg_send(bot, chat_id, "Привет! Чтобы привязать аккаунт, нажмите «Привязать Telegram» в лаунчере или личном кабинете.")
        elif not link:
            _tg_send(bot, chat_id, "Аккаунт не привязан. Нажмите «Привязать Telegram» в лаунчере или личном кабинете.")
        elif cmd in ("/start", "/menu"):
            _tg_send(bot, chat_id, "Главное меню", _tg_menu_markup())
        elif cmd == "/help":
            _tg_send(bot, chat_id, _TG_HELP, _tg_menu_markup())
        elif cmd == "/unlink":
            db.execute("DELETE FROM tg_links WHERE bot = ? AND chat_id = ?", (bot, chat_id)); db.commit()
            _tg_send(bot, chat_id, "Аккаунт отвязан.")
        elif cmd in ("/news", "/notify"):
            if arg.lower() not in ("on", "off"):
                _tg_send(bot, chat_id, f"Использование: {cmd} on|off"); return
            col = "notify_news" if cmd == "/news" else "notify_friends"
            db.execute(f"UPDATE tg_links SET {col} = ? WHERE bot = ? AND chat_id = ?",
                       (1 if arg.lower() == "on" else 0, bot, chat_id)); db.commit()
            _tg_send(bot, chat_id, "Готово.")
        elif cmd in ("/friends", "/status", "/me", "/hats", "/hat", "/settings"):
            name = {"/status": "friends"}.get(cmd, cmd[1:])
            text_, markup = _tg_view(db, link, name)
            _tg_send(bot, chat_id, text_, markup)
        else:
            _tg_send(bot, chat_id, _TG_HELP, _tg_menu_markup())
    finally:
        db.close()

def _tg_handle_callback(bot, cq):
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    data = str(cq.get("data") or "")
    if not chat_id or not message_id:
        _tg_answer(bot, cq.get("id"))
        return
    db = _tg_db()
    try:
        link = db.execute("SELECT * FROM tg_links WHERE bot = ? AND chat_id = ?", (bot, chat_id)).fetchone()
        if not link:
            _tg_answer(bot, cq["id"], "Аккаунт не привязан")
            return
        nick = link["nickname"]
        profile = db.execute("SELECT is_banned FROM profiles WHERE nickname = ?", (nick,)).fetchone()
        if not profile or profile["is_banned"]:
            _tg_answer(bot, cq["id"], "Профиль недоступен")
            return
        note, view = "", None
        kind, _, rest = data.partition(":")
        if kind == "m":
            view = rest if rest in ("friends", "me", "hats", "hat", "settings", "menu") else "menu"
        elif kind == "nt" and rest in ("f", "n"):
            col = "notify_friends" if rest == "f" else "notify_news"
            db.execute(f"UPDATE tg_links SET {col} = 1 - {col} WHERE bot = ? AND chat_id = ?", (bot, chat_id))
            db.commit()
            link = db.execute("SELECT * FROM tg_links WHERE bot = ? AND chat_id = ?", (bot, chat_id)).fetchone()
            view = "settings"
        elif kind == "hb" and rest.isdigit():
            item = db.execute("SELECT title, price FROM cosmetics WHERE id = ? AND enabled = 1", (int(rest),)).fetchone()
            if not item:
                note, view = "Товар снят с продажи", "hats"
            else:
                _tg_answer(bot, cq["id"])
                _tg_edit(bot, chat_id, message_id, f"Купить «{item['title']}» за {item['price']:g} монет?",
                         _tg_kb([[("✅ Да, купить", f"hy:{rest}"), ("Отмена", "m:hats")]]))
                return
        elif kind == "hy" and rest.isdigit():
            code, res = _cosmetic_buy_core(db, nick, int(rest))
            if code == 200:
                db.execute("INSERT OR REPLACE INTO cosmetic_equipped (nickname, cosmetic_id, y_adj, scale_adj) "
                           "VALUES (?, ?, 0, 0)", (nick, int(rest)))
                db.commit()
                note = f"Куплено и надето: {res}"
            else:
                note = res
            view = "hats"
        elif kind == "he" and rest.isdigit():
            cid = int(rest)
            if cid == 0:
                db.execute("DELETE FROM cosmetic_equipped WHERE nickname = ?", (nick,))
                db.commit()
                note = "Шапка снята"
            elif not db.execute("SELECT 1 FROM cosmetic_owned WHERE nickname = ? AND cosmetic_id = ?", (nick, cid)).fetchone():
                note = "Сначала купите эту шапку"
            else:
                db.execute("INSERT OR REPLACE INTO cosmetic_equipped (nickname, cosmetic_id, y_adj, scale_adj) "
                           "VALUES (?, ?, 0, 0)", (nick, cid))
                db.commit()
                note = "Шапка надета"
            view = "hats"
        elif kind == "ha":
            row = db.execute("SELECT y_adj, scale_adj FROM cosmetic_equipped WHERE nickname = ?", (nick,)).fetchone()
            if not row:
                note, view = "Сначала наденьте шапку", "hats"
            else:
                y, sc = row["y_adj"], row["scale_adj"]
                parts = rest.split(":")
                if parts[0] == "r":
                    y = sc = 0.0
                elif parts[0] in ("y", "s") and len(parts) == 2 and parts[1] in ("+", "-"):
                    d = 0.05 if parts[1] == "+" else -0.05
                    if parts[0] == "y":
                        y = round(min(max(y + d, -0.6), 0.6), 3)
                    else:
                        sc = round(min(max(sc + d, -0.5), 0.8), 3)
                db.execute("UPDATE cosmetic_equipped SET y_adj = ?, scale_adj = ? WHERE nickname = ?", (y, sc, nick))
                db.commit()
                view = "hat"
        else:
            view = "menu"
        _tg_answer(bot, cq["id"], note)
        if view:
            text_, markup = _tg_view(db, link, view)
            _tg_edit(bot, chat_id, message_id, (note + "\n\n" if note and view != "hat" else "") + text_, markup)
    finally:
        db.close()

_TG_COMMANDS = [("menu", "Главное меню"), ("friends", "Друзья в сети"), ("me", "Профиль и монеты"),
                ("hats", "Купить и надеть шапку"), ("hat", "Настроить шапку"), ("settings", "Уведомления"),
                ("help", "Помощь"), ("unlink", "Отвязать аккаунт")]

def _tg_poll_loop(bot):
    offset = 0
    while not TG_BOTS[bot]["username"]:
        try:
            TG_BOTS[bot]["username"] = (_tg_call(bot, "getMe", timeout=10).get("result") or {}).get("username", "")
            _tg_call(bot, "setMyCommands", {"commands": [{"command": c, "description": d} for c, d in _TG_COMMANDS]},
                     timeout=10)
        except Exception as e:
            print(f"[tg:{bot}] getMe error: {e}")
            time.sleep(10)
    while True:
        try:
            res = _tg_call(bot, "getUpdates", {"offset": offset, "timeout": 25,
                                               "allowed_updates": ["message", "callback_query"]}, timeout=35)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if upd.get("message"):
                        _tg_handle_message(bot, upd["message"])
                    elif upd.get("callback_query"):
                        _tg_handle_callback(bot, upd["callback_query"])
                except Exception as e:
                    print(f"[tg:{bot}] handler error: {e}")
        except Exception as e:
            print(f"[tg:{bot}] poll error: {e}")
            time.sleep(5)

def _tg_start():
    _tg_init_tables()
    for bot in TG_BOTS:
        if _tg_enabled(bot):
            threading.Thread(target=_tg_poll_loop, args=(bot,), daemon=True).start()
            print(f"[tg:{bot}] бот запущен")

@app.route('/api/tg/status', methods=['GET'])
@auth_required
def tg_status():
    db = get_db()
    nick = g.current_profile['nickname']
    out = {}
    for bot, cfg in TG_BOTS.items():
        row = db.execute("SELECT notify_friends, notify_news FROM tg_links WHERE bot = ? AND nickname = ?",
                         (bot, nick)).fetchone()
        out[bot] = {"enabled": _tg_enabled(bot), "title": cfg["title"], "linked": bool(row),
                    "notify_friends": bool(row and row["notify_friends"]),
                    "notify_news": bool(row and row["notify_news"])}
    return jsonify(out)

@app.route('/api/tg/link', methods=['POST'])
@auth_required
def tg_link():
    bot = str((request.get_json(silent=True) or {}).get("bot", "")).strip().lower()
    if bot not in TG_BOTS:
        return jsonify({"error": "Неизвестный бот"}), 400
    if not _tg_enabled(bot):
        return jsonify({"error": "Бот не настроен на сервере"}), 503
    username = TG_BOTS[bot]["username"]
    if not username:
        return jsonify({"error": "Бот ещё запускается, попробуйте через пару секунд"}), 503
    db = get_db()
    nick = g.current_profile['nickname']
    db.execute("DELETE FROM tg_link_codes WHERE nickname = ? AND bot = ? OR expires < ?", (nick, bot, time.time()))
    code = secrets.token_urlsafe(12)
    db.execute("INSERT INTO tg_link_codes (code, bot, nickname, expires) VALUES (?, ?, ?, ?)",
               (code, bot, nick, time.time() + TG_CODE_TTL_SECONDS))
    db.commit()
    return jsonify({"success": True, "url": f"https://t.me/{username}?start={code}",
                    "expires_in": TG_CODE_TTL_SECONDS})

@app.route('/api/tg/unlink', methods=['POST'])
@auth_required
def tg_unlink():
    bot = str((request.get_json(silent=True) or {}).get("bot", "")).strip().lower()
    db = get_db()
    db.execute("DELETE FROM tg_links WHERE bot = ? AND nickname = ?", (bot, g.current_profile['nickname']))
    db.commit()
    return jsonify({"success": True})

MAFIN_THEME_CSS = """
:root{--mf-bg:#14181a;--mf-pan:#171b1d;--mf-pan2:#20262a;--mf-pan3:#2a3236;--mf-line:#33403d;--mf-tx:#e6e9ee;--mf-mu:#8f9b97;--mf-ac:#43b581;--mf-acd:#2c8a60;--mf-bad:#d96b6b}
html,body{background:var(--mf-bg)!important;color:var(--mf-tx)!important;font-family:"Segoe UI",system-ui,sans-serif!important}
body{padding-left:210px!important;margin:0!important}
.mf-side{position:fixed;left:0;top:0;bottom:0;width:210px;background:var(--mf-pan);border-right:1px solid var(--mf-line);padding:18px 12px;overflow:auto;z-index:50;box-sizing:border-box}
.mf-side .mf-t{font-size:17px;font-weight:600;margin:0 6px 2px;color:var(--mf-tx)}
.mf-side .mf-s{font-size:11px;color:var(--mf-mu);margin:0 6px 16px}
.mf-side a{display:block;padding:9px 12px;color:var(--mf-mu)!important;text-decoration:none;border:1px solid transparent;margin-bottom:3px;font-size:13px}
.mf-side a:hover{color:var(--mf-tx)!important;background:var(--mf-pan2)}
.mf-side a.on{color:var(--mf-tx)!important;background:var(--mf-pan2);border-color:var(--mf-line);box-shadow:inset 3px 0 0 var(--mf-ac)}
.mf-side .mf-g{font-size:10px;letter-spacing:.1em;color:#5f6b67;margin:16px 12px 6px}
header{background:transparent!important;border:0!important;padding:26px 28px 0!important;display:block!important}
header>div{display:none!important}
header h1{color:var(--mf-tx)!important;font-size:22px!important;font-weight:600!important}
main{padding:18px 28px 40px!important;max-width:1180px!important;margin:0 auto!important}
section,.card,.stat,.panel,.box{background:var(--mf-pan)!important;border:2px solid #000!important;box-shadow:inset 0 0 0 1px var(--mf-line)!important;border-radius:0!important}
section h2,h2{color:var(--mf-tx)!important;font-size:15px!important;font-weight:600!important}
.stat .num{color:var(--mf-tx)!important;font-weight:600!important}
.stat .label{color:var(--mf-mu)!important;text-transform:uppercase;font-size:10px!important;letter-spacing:.08em}
table{border-collapse:collapse}
th{color:var(--mf-mu)!important;font-weight:500!important;font-size:11px!important}
th,td{border-bottom:1px solid var(--mf-line)!important}
tr:hover{background:var(--mf-pan2)!important}
input,textarea,select{background:var(--mf-pan2)!important;border:1px solid var(--mf-line)!important;border-radius:0!important;color:var(--mf-tx)!important}
input:focus,textarea:focus,select:focus{border-color:var(--mf-ac)!important;outline:0!important}
button,.btn{border-radius:0!important;background:var(--mf-pan2)!important;color:var(--mf-tx)!important;border:1px solid var(--mf-line)!important}
button:hover{background:var(--mf-pan3)!important}
button.primary,.upload-form button,button[type=submit].primary,button.success:hover{background:var(--mf-ac)!important;color:#08180f!important;border:2px solid #000!important;font-weight:700!important;box-shadow:inset 0 -3px 0 var(--mf-acd)}
button.danger,.actions button.danger{color:var(--mf-bad)!important}
button.danger:hover{background:#3a2020!important}
.badge,.active-pill,.pill,.tag{border-radius:0!important;font-weight:500!important;background:var(--mf-pan2)!important;color:var(--mf-mu)!important;border:1px solid var(--mf-line)}
.badge.admin,.active-pill{background:#1f3a2f!important;color:#8fe0b9!important;border-color:var(--mf-acd)}
.badge.banned{background:transparent!important;color:var(--mf-bad)!important;border-color:#5a3030}
.err{background:#2a1c1c!important;border:1px solid #5a3030!important;color:#e8a0a0!important;border-radius:0!important}
.okmsg,.ok-box{background:#1b2a24!important;border:1px solid var(--mf-acd)!important;color:#a6f0c6!important;border-radius:0!important}
pre,.item,code{background:#0f1315!important;border-radius:0!important}
.muted,.kv div:nth-child(odd){color:var(--mf-mu)!important}
.ok{color:var(--mf-ac)!important}.bad{color:var(--mf-bad)!important}
.bar,.bar div{border-radius:0!important}.bar{background:var(--mf-pan3)!important}.bar div{background:var(--mf-ac)!important}
a{color:var(--mf-ac)}
@media (max-width:820px){body{padding-left:0!important;padding-top:56px}.mf-side{width:auto;right:0;bottom:auto;height:auto;display:flex;overflow-x:auto;padding:8px;border-right:0;border-bottom:1px solid var(--mf-line)}.mf-side .mf-t,.mf-side .mf-s,.mf-side .mf-g{display:none}.mf-side a{white-space:nowrap;margin:0 3px 0 0}}
"""

_MF_NAV = (("/admin/dashboard", "Пользователи", "dash"), ("/admin/minecraft", "Minecraft-сервер", "mc"),
           ("/admin/shop", "Магазин", "shop"), ("/admin/cosmetics", "Косметика", "cos"),
           ("/admin/modpack", "Сборка сервера", "pack"), ("/admin/quests", "Задания", "quests"),
           ("/admin/gifts", "Подарки", "gifts"), ("/admin/news", "Новости", "news"))
_MF_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\ufe0f\u200d]+\\s*")


def _mafin_theme(tpl, active):
    nav = '<nav class="mf-side"><div class="mf-t">Mafin</div><div class="mf-s">админ-панель</div>'
    for href, title, key in _MF_NAV:
        nav += '<a href="%s"%s>%s</a>' % (href, ' class="on"' if key == active else "", title)
    nav += '<div class="mf-g">СИСТЕМА</div><a href="/admin/logout">Выход</a></nav>'
    out = tpl.replace("</style>", "</style><style>" + MAFIN_THEME_CSS + "</style>", 1)
    out = out.replace("<body>", "<body>" + nav, 1)

    def strip_tag(m):
        return m.group(1) + _MF_EMOJI.sub("", m.group(2)) + m.group(3)
    out = re.sub(r"(<(?:h1|h2|h3|button|label|a|summary)\b[^>]*>)([^<]{0,160})(</|<)", strip_tag, out)
    return out


LOGIN_TEMPLATE = """
<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mafin — вход в админ-панель</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#14181a radial-gradient(ellipse at 50% 45%,#1d2a26,#14181a 65%);color:#e6e9ee;font-family:"Segoe UI",system-ui,sans-serif}
.card{width:min(400px,calc(100vw - 32px));background:#171b1d;border:2px solid #000;box-shadow:inset 0 0 0 1px #33403d;padding:28px}
.hd{display:flex;gap:12px;align-items:center;margin-bottom:22px}
.lg{width:48px;height:48px;background:#2a3236;border:1px solid #33403d;display:grid;place-items:center}
.lg i{width:16px;height:16px;background:#43b581;display:block}
h1{margin:0;font-size:18px;font-weight:600}.sub{color:#8f9b97;font-size:12px;margin-top:2px}
label{display:block;font-size:10px;letter-spacing:.08em;color:#8f9b97;text-transform:uppercase;margin-bottom:6px}
input{width:100%;height:38px;background:#20262a;border:1px solid #33403d;color:#e6e9ee;padding:0 12px;font-size:14px;margin-bottom:14px;border-radius:0}
input:focus{border-color:#43b581;outline:0}
button{width:100%;height:42px;background:#43b581;color:#08180f;border:2px solid #000;box-shadow:inset 0 -3px 0 #2c8a60;font-weight:700;font-size:14px;cursor:pointer;border-radius:0}
button:hover{background:#4cc28e}
.err{background:#2a1c1c;border:1px solid #5a3030;color:#e8a0a0;padding:10px 12px;margin-bottom:14px;white-space:pre-wrap;font-size:13px}
.hint{font-size:12px;color:#8f9b97;margin:-6px 0 14px}code{background:#0f1315;padding:1px 5px}
</style></head>
<body><div class="card">
<div class="hd"><div class="lg"><i></i></div><div><h1>Mafin · админ-панель</h1><div class="sub">Только для администраторов</div></div></div>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post" action="/admin/login">
<label>Никнейм</label><input type="text" name="nickname" required autofocus>
<label>Одноразовый код</label><input type="text" name="code" autocomplete="off" required>
<div class="hint">Получить код: на сервере <code>python app.py admin-code ВАШ_НИК</code></div>
<button type="submit">Войти</button>
</form>
</div></body></html>
"""

DASHBOARD_TEMPLATE = _mafin_theme(DASHBOARD_TEMPLATE, "dash")
MC_ADMIN_TEMPLATE = _mafin_theme(MC_ADMIN_TEMPLATE, "mc")
SHOP_ADMIN_TEMPLATE = _mafin_theme(SHOP_ADMIN_TEMPLATE, "shop")
COSMETICS_ADMIN_TEMPLATE = _mafin_theme(COSMETICS_ADMIN_TEMPLATE, "cos")
MODPACK_ADMIN_TEMPLATE = _mafin_theme(MODPACK_ADMIN_TEMPLATE, "pack")
QUESTS_TEMPLATE = _mafin_theme(QUESTS_TEMPLATE, "quests")
GIFTS_TEMPLATE = _mafin_theme(GIFTS_TEMPLATE, "gifts")
NEWS_TEMPLATE = _mafin_theme(NEWS_TEMPLATE, "news")


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == "set-password":
        _cli_set_password(sys.argv[2])
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "admin-code":
        _cli_admin_code(sys.argv[2], create="--create" in sys.argv[3:])
        sys.exit(0)
    init_db()
    _tg_start()
    _mc_sessions_load()
    try:
        _cosmetic_pack_build()
    except Exception as e:
        print(f"[cosmetics] не удалось собрать ресурспак: {e}")
    _mc_guard_start()
    _shop_start()
    threading.Thread(target=_weekly_loop, daemon=True).start()
    threading.Thread(target=_skin_push_loop, daemon=True).start()
    if os.environ.get("MAFIN_MC_AUTOSTART") == "1":
        try:
            print("[mc]", _mc_start())
        except Exception as e:
            print(f"[mc] автозапуск не удался: {e}")
    print("=" * 60)
    print("🚀 Mafin Launcher Server запущен")
    print("📡 API адрес: http://0.0.0.0:10074")
    print("🛡️  Веб админ-панель: http://localhost:10074/admin")
    print("🔑 Вход в админку: python app.py admin-code НИК")
    print("=" * 60)
    app.run(host=os.environ.get("MAFIN_HOST", "0.0.0.0"), port=int(os.environ.get("MAFIN_PORT", "10074")), debug=False)
