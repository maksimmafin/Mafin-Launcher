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
LAUNCHER_SESSION_DAYS = 7  # сколько живёт сессия лаунчера (скользящее окно)
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
    if "token_expires_at" not in existing_cols:
        db.execute("ALTER TABLE profiles ADD COLUMN token_expires_at INTEGER")
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

def new_token_expiry():
    return int(time.time()) + LAUNCHER_SESSION_DAYS * 86400

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

        now_ts = int(time.time())
        expires_at = profile['token_expires_at']
        if expires_at is not None and expires_at < now_ts:
            db.execute("UPDATE profiles SET token = NULL, token_expires_at = NULL WHERE id = ?", (profile['id'],))
            db.commit()
            return jsonify({"error": "Сессия истекла, войдите заново", "code": "session_expired"}), 401
        
        if profile['is_banned']:
            return jsonify({"error": f"Профиль заблокирован: {profile['ban_reason']}"}), 403

        # Скользящее окно: продлеваем до 7 дней, но пишем в БД не чаще раза в час.
        # Старые токены без срока получают его при первом же запросе.
        if expires_at is None or expires_at - now_ts < LAUNCHER_SESSION_DAYS * 86400 - 3600:
            db.execute("UPDATE profiles SET token_expires_at = ? WHERE id = ?", (new_token_expiry(), profile['id']))
            db.commit()
        
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
        "INSERT INTO profiles (nickname, password_hash, salt, token, token_expires_at, last_login) VALUES (?, ?, ?, ?, ?, ?)",
        (nickname, password_hash, salt, token, new_token_expiry(), datetime.now().isoformat())
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

    now_ts = int(time.time())
    old_exp = profile['token_expires_at']
    if profile['token'] and (old_exp is None or old_exp >= now_ts):
        token = profile['token']
    else:
        token = generate_token()
    expires_at = new_token_expiry()
    now_iso = datetime.now().isoformat()
    if needs_upgrade:
        new_hash = hash_password(password, profile['salt'])
        db.execute(
            "UPDATE profiles SET token = ?, token_expires_at = ?, last_login = ?, last_seen = ?, password_hash = ? WHERE id = ?",
            (token, expires_at, now_iso, now_iso, new_hash, profile['id'])
        )
    else:
        db.execute(
            "UPDATE profiles SET token = ?, token_expires_at = ?, last_login = ?, last_seen = ? WHERE id = ?",
            (token, expires_at, now_iso, now_iso, profile['id'])
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

def _load_mc_secret():
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

def _normalize_ip(ip):
    ip = (ip or "").strip()
    if ip.lower().startswith("::ffff:"):
        ip = ip[7:]
    return ip

@app.route('/api/mc/session', methods=['POST'])
@auth_required
def mc_register_session():
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
        _mc_sessions[key] = {"nick": me, "ip": _normalize_ip(request.remote_addr)}
    return jsonify({"success": True})

def _mc_access(db, name, ip):
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
    if sess['ip'] != _normalize_ip(ip):
        return False, "ip_mismatch"
    return True, None

@app.route('/api/mc/check', methods=['POST'])
def mc_check():
    if not MC_SECRET:
        return jsonify({"allowed": False, "reason": "disabled"}), 503
    given = request.headers.get('X-Mc-Secret', '')
    if not secrets.compare_digest(given.encode('utf-8'), MC_SECRET.encode('utf-8')):
        return jsonify({"allowed": False, "reason": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    ok, reason = _mc_access(get_db(), str(data.get('name', '')), data.get('ip', ''))
    if ok:
        return jsonify({"allowed": True})
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
  <div>Вы вошли как <b>{{ me.nickname }}</b> &nbsp;|&nbsp; <a href="/admin/quests">📋 Задания</a> &nbsp;|&nbsp; <a href="/admin/gifts">🎁 Подарки</a> &nbsp;|&nbsp; <a href="/admin/news">📰 Новости</a> &nbsp;|&nbsp; <a href="/admin/minecraft">🖥 Сервер</a> &nbsp;|&nbsp; <a href="/admin/shop">🛒 Магазин</a> &nbsp;|&nbsp; <a href="/admin/logout">Выйти</a></div>
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
    v = _mc_read_props().get("server-port", "")
    return int(v) if v.isdigit() and 1 <= int(v) <= 65535 else MC_PORT


def _mc_max_players():
    v = _mc_read_props().get("max-players", "")
    return int(v) if v.isdigit() else 20


def _mc_run_port():
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
    if not MC_SECRET:
        return
    cfg_dir = _mc_path("plugins", "MafinAuth")
    os.makedirs(cfg_dir, exist_ok=True)
    api_port = int(os.environ.get("MAFIN_PORT", "10074"))
    with open(os.path.join(cfg_dir, "config.yml"), "w", encoding="utf-8") as f:
        f.write(f'api_url: "http://127.0.0.1:{api_port}"\n')
        f.write(f"secret: {json.dumps(MC_SECRET)}\n")
        f.write("timeout_ms: 5000\n")


MC_MIN_MEMORY_MB = 512


def _mc_fmt_mb(mb):
    if mb % 1024 == 0:
        return f"{mb // 1024} ГБ"
    if mb >= 1024:
        return f"{mb / 1024:.2f}".rstrip("0").rstrip(".") + " ГБ"
    return f"{mb} МБ"


def _mc_parse_memory(amount, unit):
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
    written, total, skipped = [], 0, 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if "__MACOSX/" in name or rel.endswith(".DS_Store"):
            continue
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:  # символическая ссылка
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


def _mc_world_import(zip_path, wanted_name, fallback_name):
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
        "modpack": None,
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
    return '<div class="item"><div><div class="t">'+esc(w)+(isAct?' <span class="ok">— активный</span>':'')+'</div></div><div>'+(isAct?'':'<button data-wact="'+esc2(w)+'"'+(busy?' disabled':'')+'>Сделать активным</button> <button class="danger" data-wdel="'+esc2(w)+'"'+(busy?' disabled':'')+'>Удалить</button>')+'</div></div>';
  }).join('');
  box.innerHTML=html;
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

SKIN_MAX_BYTES = 256 * 1024
SKIN_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
SKIN_KEY_PATH = os.environ.get("MAFIN_SKIN_KEY", os.path.join(BASE_DIR, "skin_signing_key.pem"))
_skin_key_lock = threading.Lock()
_skin_key_cache = {}


def _skin_signing_key():
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
    import uuid
    return str(uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest(), version=3))


def _shop_command(purchase):
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
<div><a href="/admin/minecraft">🖥 Сервер</a><a href="/admin/dashboard">← Панель</a><a href="/admin/logout">Выйти</a></div></header>
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


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == "set-password":
        _cli_set_password(sys.argv[2])
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "admin-code":
        _cli_admin_code(sys.argv[2], create="--create" in sys.argv[3:])
        sys.exit(0)
    init_db()
    _mc_guard_start()
    _shop_start()
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
