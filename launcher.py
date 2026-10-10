import os
import sys
import io
import subprocess
import json
import threading
import time
import math
import uuid as uuid_module
import socket
import struct
import queue
import collections
import re
import traceback
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
from urllib.parse import urlparse
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext, simpledialog
import shutil
import tempfile
import gzip
import zipfile


def install_packages():
    try:
        import setuptools
    except ImportError:
        print("Установка setuptools...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "setuptools", "--quiet"])

    try:
        import requests
    except ImportError:
        print("Установка requests...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])

    MIN_MLL_VERSION = (8, 0)

    def _mll_version_ok():
        try:
            from importlib.metadata import version as pkg_version
            ver_str = pkg_version("minecraft-launcher-lib")
            parts = tuple(int(p) for p in re.findall(r"\d+", ver_str)[:2])
            return parts >= MIN_MLL_VERSION
        except Exception:
            return None

    try:
        from minecraft_launcher_lib.install import install_minecraft_version
        from minecraft_launcher_lib.utils import get_version_list
        from minecraft_launcher_lib.command import get_minecraft_command
        import minecraft_launcher_lib.runtime

        if _mll_version_ok() is False:
            print("minecraft-launcher-lib устарел (нет нормальной поддержки Forge/Fabric под новые версии), обновляю...")
            raise ImportError("outdated minecraft-launcher-lib")
        print("minecraft-launcher-lib уже установлен.")
        return
    except (ImportError, AttributeError, ModuleNotFoundError):
        print("Установка/обновление minecraft-launcher-lib...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--upgrade", "minecraft-launcher-lib", "--quiet"]
            )
            from minecraft_launcher_lib.install import install_minecraft_version
            from minecraft_launcher_lib.utils import get_version_list
            from minecraft_launcher_lib.command import get_minecraft_command
            print("Установка успешна.")
        except Exception as e:
            print(f"Не удалось установить minecraft-launcher-lib: {e}")
            sys.exit(1)

    try:
        import pypresence
    except ImportError:
        print("Установка pypresence (для Discord Rich Presence)...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "pypresence", "--quiet"])
        except Exception as e:
            print(f"Не удалось установить pypresence, Discord RPC будет недоступен: {e}")


install_packages()

import requests
from minecraft_launcher_lib.install import install_minecraft_version
from minecraft_launcher_lib.utils import get_version_list
from minecraft_launcher_lib.command import get_minecraft_command
import minecraft_launcher_lib.forge as mll_forge
import minecraft_launcher_lib.runtime as mll_runtime
import minecraft_launcher_lib.mod_loader as mll_modloader

try:
    from PIL import Image, ImageTk, ImageSequence, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pystray
except Exception:
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "pystray", "--quiet"], check=True, timeout=90)
        import pystray
    except Exception as _e:
        print(f"pystray недоступен, сворачивание в трей отключено: {_e}")
        pystray = None
HAS_TRAY = pystray is not None


def animate_image_on_label(label, image_bytes, max_px=96, circular=False):
    def _fallback_static():
        try:
            img = tk.PhotoImage(data=image_bytes)
            factor = max(1, max(img.width(), img.height()) // max_px)
            if factor > 1:
                img = img.subsample(factor, factor)
            label.configure(image=img, text="", width=0, height=0)
            label.image = img
        except Exception:
            pass

    if not HAS_PIL:
        _fallback_static()
        return

    def _apply_circle_mask(rgba):
        w, h = rgba.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        rgba = rgba.crop((left, top, left + side, top + side))
        mask = Image.new("L", (side, side), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, side, side), fill=255)
        rgba.putalpha(mask)
        return rgba

    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(pil_img):
            duration = frame.info.get("duration", 100) or 100
            rgba = frame.convert("RGBA")
            w, h = rgba.size
            longest = max(w, h)
            if longest > max_px:
                scale = max_px / longest
                rgba = rgba.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            if circular:
                rgba = _apply_circle_mask(rgba)
            frames.append(ImageTk.PhotoImage(rgba))
            durations.append(max(int(duration), 20))
    except Exception:
        _fallback_static()
        return

    if not frames:
        return

    label.image = frames
    label.configure(image=frames[0], text="", width=0, height=0)
    if len(frames) <= 1:
        return

    state = {"idx": 0}

    def step():
        try:
            if not label.winfo_exists():
                return
        except Exception:
            return
        state["idx"] = (state["idx"] + 1) % len(frames)
        label.configure(image=frames[state["idx"]])
        label.after(durations[state["idx"]], step)

    label.after(durations[0], step)


def _resolve_config_path():
    name = "launcher_config.json"
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    p_base = os.path.join(base, name)
    p_cwd = os.path.abspath(name)
    if os.path.exists(p_base):
        return p_base
    if os.path.exists(p_cwd):
        return p_cwd
    return p_base if os.access(base, os.W_OK) else p_cwd


CONFIG_FILE = _resolve_config_path()

APP_VERSION = "1.8.1"


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_sha256(path, expected):
    if not expected:
        return
    if _sha256_file(path).lower() != str(expected).strip().lower():
        try:
            os.remove(path)
        except OSError:
            pass
        raise Exception("Контрольная сумма файла обновления не совпала. "
                        "Файл удалён — обновление не установлено. Попробуйте позже.")


def _version_tuple(version):
    return tuple(int(p) for p in re.findall(r"\d+", str(version)))


def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


ACHIEVEMENT_SOUND_FILE = resource_path("achievement.wav")


def play_achievement_sound():
    if not os.path.exists(ACHIEVEMENT_SOUND_FILE):
        return

    def _play():
        try:
            if os.name == "nt":
                import winsound
                winsound.PlaySound(ACHIEVEMENT_SOUND_FILE, winsound.SND_FILENAME | winsound.SND_ASYNC)
            elif sys.platform == "darwin":
                subprocess.Popen(["afplay", ACHIEVEMENT_SOUND_FILE],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["aplay", "-q", ACHIEVEMENT_SOUND_FILE],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"Не удалось проиграть звук ачивки: {e}")

    threading.Thread(target=_play, daemon=True).start()

DISCORD_CLIENT_ID = "1548297015220371496"

SERVER_URL = os.environ.get("MAFIN_SERVER_URL", "http://121.0.0.1:3096")

AUTHLIB_INJECTOR_LATEST = "https://authlib-injector.yushi.moe/artifact/latest.json"
AUTHLIB_INJECTOR_FALLBACK = ("https://github.com/yushijinhun/authlib-injector/releases/download/"
                             "v1.2.5/authlib-injector-1.2.5.jar")


def offline_uuid(name):
    import hashlib
    return str(uuid_module.UUID(bytes=hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest(), version=3))


def ensure_authlib_injector(status_callback=None):
    import hashlib
    path = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)), "authlib-injector.jar")
    if os.path.isfile(path) and zipfile.is_zipfile(path):
        return path
    if status_callback:
        status_callback("⬇️ Скачиваю модуль скинов (authlib-injector)...")
    urls, sha = [], None
    try:
        info = requests.get(AUTHLIB_INJECTOR_LATEST, timeout=8).json()
        if info.get("download_url"):
            urls.append(info["download_url"])
            sha = (info.get("checksums") or {}).get("sha256")
    except Exception as e:
        print(f"authlib-injector: не удалось получить latest.json: {e}")
    urls.append(AUTHLIB_INJECTOR_FALLBACK)
    for i, url in enumerate(urls):
        try:
            data = requests.get(url, timeout=40).content
            if len(data) < 50 * 1024:
                continue
            if i == 0 and sha and hashlib.sha256(data).hexdigest().lower() != str(sha).lower():
                print("authlib-injector: контрольная сумма не совпала")
                continue
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            if not zipfile.is_zipfile(tmp):
                os.remove(tmp)
                continue
            os.replace(tmp, path)
            return path
        except Exception as e:
            print(f"authlib-injector: не удалось скачать {url}: {e}")
    return None


def skin_agent_jvm_args(api_base, status_callback=None):
    if not api_base:
        return []
    try:
        root = api_base.rstrip("/") + "/yggdrasil"
        r = requests.get(root + "/", timeout=4)
        if r.status_code != 200:
            return []
        import base64
        prefetched = base64.b64encode(r.content).decode("ascii")
        jar = ensure_authlib_injector(status_callback)
        if not jar:
            return []
        return [f"-javaagent:{jar}={root}",
                f"-Dauthlibinjector.yggdrasil.prefetched={prefetched}",
                "-Dauthlibinjector.noShowServerName"]
    except Exception as e:
        print(f"Скины: сервер скинов недоступен, запускаю без него: {e}")
        return []

LAUNCHER_THEMES = {
    "green":  {"label": "🟢 Зелёная (по умолчанию)", "accent": "#43b581", "accent_hover": "#379768"},
    "blue":   {"label": "🔵 Синяя",                    "accent": "#3b82f6", "accent_hover": "#2f68c9"},
    "purple": {"label": "🟣 Фиолетовая",                "accent": "#8b5cf6", "accent_hover": "#6f45cc"},
    "red":    {"label": "🔴 Красная",                   "accent": "#ed4245", "accent_hover": "#c33538"},
    "orange": {"label": "🟠 Оранжевая",                 "accent": "#f97316", "accent_hover": "#c85c11"},
}
DEFAULT_LAUNCHER_THEME = "green"


def _win_build():
    try:
        return sys.getwindowsversion().build if os.name == "nt" else 0
    except Exception:
        return 0


WINDOW_MATERIALS = {
    "solid":    {"label": "⬛ Классика (без эффектов)", "backdrop": None, "min_build": 0},
    "mica":     {"label": "🪟 Mica",                    "backdrop": 2,    "min_build": 22000},
    "mica_alt": {"label": "🪟 Mica Alt",                "backdrop": 4,    "min_build": 22621},
    "acrylic":  {"label": "💎 Acrylic",                 "backdrop": 3,    "min_build": 17134},
}
DEFAULT_WINDOW_MATERIAL = "mica" if _win_build() >= 22000 else "solid"

PALETTES = {
    "solid": {
        "bg": "#1a1d23", "surface": "#242830", "surface2": "#2d323c", "surface3": "#363c48",
        "console": "#0d1117", "border": "#3a3f4a",
        "text": "#e6e9ee", "text_dim": "#8a94a6", "text_faint": "#5c6270",
    },
    "mica": {
        "bg": "#000000", "surface": "#2a2a2e", "surface2": "#343439", "surface3": "#3f3f46",
        "console": "#1b1b1f", "border": "#47474f",
        "text": "#f4f4f6", "text_dim": "#a1a1aa", "text_faint": "#71717a",
    },
    "mica_alt": {
        "bg": "#000000", "surface": "#202024", "surface2": "#2a2a30", "surface3": "#35353d",
        "console": "#141417", "border": "#3d3d45",
        "text": "#f4f4f6", "text_dim": "#a1a1aa", "text_faint": "#71717a",
    },
    "acrylic": {
        "bg": "#000000", "surface": "#272b35", "surface2": "#323744", "surface3": "#3d4352",
        "console": "#191c24", "border": "#464d5e",
        "text": "#f5f7fa", "text_dim": "#a8b0c0", "text_faint": "#737b8c",
    },
}

_THEME = {"material": "solid", "pal": {}, "accent": "#43b581", "hover": "#379768"}

_LEG_BG = {"#1a1d23": "bg", "#242830": "surface", "#23272e": "surface",
           "#2d323c": "surface2", "#0d1117": "console", "#3a3f4a": "border"}
_LEG_FG = {"#7a8599": "text_dim", "#888": "text_dim", "#888888": "text_dim",
           "#5c6270": "text_faint", "#4a4f5a": "text_faint",
           "#c9d1d9": "text", "#e1e4e8": "text"}
_LEG_ACCENT = "#43b581"
_OPT_ALIAS = {"bg": "background", "fg": "foreground"}
_BG_OPTS = {"background", "activebackground", "highlightbackground", "highlightcolor",
            "readonlybackground", "selectbackground", "troughcolor", "disabledbackground"}
_FG_OPTS = {"foreground", "activeforeground", "selectforeground", "disabledforeground",
            "insertbackground"}
_ACCENT_BG_OPTS = {"background", "activebackground", "selectbackground", "highlightcolor"}


def _pal(token):
    if token == "accent":
        return _THEME["accent"]
    if token == "accent_hover":
        return _THEME["hover"]
    return _THEME["pal"].get(token, "#ff00ff")


def _translate(opt, val):
    opt = _OPT_ALIAS.get(opt, opt)
    if not isinstance(val, str) or not val.startswith("#") and val != "white":
        return val, None
    v = val.lower()
    tok = None
    if opt in _BG_OPTS:
        tok = _LEG_BG.get(v)
        if tok is None and v in (_LEG_ACCENT, _THEME["accent"].lower()) and opt in _ACCENT_BG_OPTS:
            tok = "accent"
        if tok is None:
            tok = _THEME.get("_rev", {}).get(v)
    elif opt in _FG_OPTS:
        tok = _LEG_FG.get(v) or _THEME.get("_rev", {}).get(v)
        if tok in ("bg", "accent"):
            tok = None
    if tok is None:
        return val, None
    return _pal(tok), tok


def set_theme(material, accent, accent_hover):
    pal = PALETTES.get(material, PALETTES["solid"])
    _THEME.update(material=material, pal=dict(pal), accent=accent, hover=accent_hover)
    _THEME["_rev"] = {v.lower(): k for k, v in pal.items()}
    _THEME["_rev"][accent.lower()] = "accent"


set_theme("solid", LAUNCHER_THEMES[DEFAULT_LAUNCHER_THEME]["accent"],
          LAUNCHER_THEMES[DEFAULT_LAUNCHER_THEME]["accent_hover"])

_TK_DEFAULT_BG_CLASSES = ("Frame", "Label", "Canvas", "Labelframe", "Checkbutton",
                          "Radiobutton", "Scale", "Message", "Toplevel")
_orig_bw_init = tk.BaseWidget.__init__
_orig_misc_configure = tk.Misc._configure


_TOKEN_DEFAULTS = {
    "button": {"background": "surface2", "foreground": "text", "activebackground": "surface3",
               "activeforeground": "text", "highlightbackground": "bg"},
    "entry": {"background": "surface2", "foreground": "text", "insertbackground": "text",
              "readonlybackground": "surface", "highlightbackground": "border",
              "highlightcolor": "accent", "selectbackground": "accent",
              "selectforeground": "text", "disabledbackground": "surface"},
    "spinbox": {"background": "surface2", "foreground": "text", "insertbackground": "text",
                "highlightbackground": "border", "highlightcolor": "accent",
                "selectbackground": "accent", "buttonbackground": "surface3"},
    "text": {"background": "surface2", "foreground": "text", "insertbackground": "text",
             "highlightbackground": "border", "highlightcolor": "accent",
             "selectbackground": "accent", "selectforeground": "text"},
    "listbox": {"background": "surface", "foreground": "text", "highlightbackground": "border",
                "highlightcolor": "accent", "selectbackground": "accent",
                "selectforeground": "text"},
    "scrollbar": {"background": "surface3", "troughcolor": "surface", "activebackground": "text_faint",
                  "highlightbackground": "bg"},
    "scale": {"troughcolor": "surface2", "foreground": "text", "highlightbackground": "bg",
              "activebackground": "accent"},
    "menubutton": {"background": "surface2", "foreground": "text", "activebackground": "surface3",
                   "activeforeground": "text"},
}
_FG_DEFAULT_WIDGETS = ("label", "checkbutton", "radiobutton", "labelframe", "message")
_SELECTCOLOR_WIDGETS = ("checkbutton", "radiobutton")
_LITERAL_DEFAULTS = {
    "button": {"relief": "flat", "borderwidth": 0, "highlightthickness": 0, "cursor": "hand2"},
    "entry": {"relief": "flat", "highlightthickness": 1},
    "spinbox": {"relief": "flat", "highlightthickness": 1},
    "text": {"relief": "flat", "highlightthickness": 1},
    "listbox": {"relief": "flat", "highlightthickness": 1},
    "scrollbar": {"relief": "flat", "borderwidth": 0},
    "scale": {"relief": "flat", "highlightthickness": 0, "borderwidth": 0},
    "checkbutton": {"highlightthickness": 0},
    "radiobutton": {"highlightthickness": 0},
}


_CARD_STYLES = {"ttk::frame": "TFrame", "ttk::label": "TLabel", "ttk::checkbutton": "TCheckbutton",
                "ttk::radiobutton": "TRadiobutton", "ttk::labelframe": "TLabelframe"}


def _bw_init(self, master, widgetName, cnf={}, kw={}, extra=()):
    merged = dict(tk._cnfmerge((cnf, kw)) or {})
    if widgetName in _CARD_STYLES and "style" not in merged:
        try:
            top, root = master.winfo_toplevel(), _THEME.get("root")
            if root is not None and top is not root:
                merged["style"] = "Card." + _CARD_STYLES[widgetName]
        except Exception:
            pass
    toks = {}
    for k, v in list(merged.items()):
        nv, tok = _translate(k, v)
        if tok:
            merged[k] = nv
            toks[_OPT_ALIAS.get(k, k)] = tok

    def given(opt):
        return opt in merged or any(a == opt and k in merged for k, a in _OPT_ALIAS.items())

    is_tk_widget = widgetName in ("frame", "label", "canvas", "labelframe", "checkbutton",
                                  "radiobutton", "scale", "message", "toplevel")
    if widgetName == "toplevel":
        merged.pop("bg", None)
        merged["background"] = _pal("surface")
        toks["background"] = "surface"
    elif is_tk_widget and not given("background"):
        ptok = (getattr(master, "_tok", None) or {}).get("background") or "bg"
        merged["background"] = _pal(ptok)
        toks["background"] = ptok
    for opt, tok in _TOKEN_DEFAULTS.get(widgetName, {}).items():
        if not given(opt):
            merged[opt] = _pal(tok)
            toks[opt] = tok
    if widgetName in _FG_DEFAULT_WIDGETS and not given("foreground"):
        merged["foreground"] = _pal("text")
        toks["foreground"] = "text"
    if widgetName in _SELECTCOLOR_WIDGETS and not given("selectcolor"):
        merged["selectcolor"] = _pal("surface2")
        toks["selectcolor"] = "surface2"
        if not given("activebackground"):
            ptok = (getattr(master, "_tok", None) or {}).get("background") or "bg"
            merged["activebackground"] = _pal(ptok)
            toks["activebackground"] = ptok
        if not given("activeforeground"):
            merged["activeforeground"] = _pal("text")
            toks["activeforeground"] = "text"
    for opt, val in _LITERAL_DEFAULTS.get(widgetName, {}).items():
        if not given(opt):
            merged[opt] = val
    if widgetName in ("entry", "text", "listbox", "spinbox") and given("highlightthickness") is False:
        pass
    _orig_bw_init(self, master, widgetName, merged, {}, extra)
    if toks:
        self._tok = toks
    if widgetName == "toplevel":
        _schedule_toplevel_style(self)


def _schedule_toplevel_style(win):
    try:
        win.bind("<Map>", lambda e, w=win: _style_toplevel(w) if e.widget is w else None, add="+")
        win.after_idle(lambda w=win: _style_toplevel(w))
        win.after(120, lambda w=win: _style_toplevel(w))
    except Exception:
        pass


def _misc_configure(self, cmd, cnf, kw):
    if cmd == "configure" and (kw or isinstance(cnf, dict)):
        merged = dict(cnf) if isinstance(cnf, dict) else {}
        merged.update(kw or {})
        toks = getattr(self, "_tok", None)
        changed = False
        for k, v in list(merged.items()):
            name = _OPT_ALIAS.get(k, k)
            nv, tok = _translate(k, v)
            if tok:
                merged[k] = nv
                if toks is None:
                    toks = self._tok = {}
                toks[name] = tok
                changed = True
            elif toks and name in toks:
                del toks[name]
        if changed or isinstance(cnf, dict):
            return _orig_misc_configure(self, cmd, None, merged)
    return _orig_misc_configure(self, cmd, cnf, kw)


tk.BaseWidget.__init__ = _bw_init
tk.Misc._configure = _misc_configure


def retheme_tree(widget):
    toks = getattr(widget, "_tok", None)
    if toks:
        for opt, tok in list(toks.items()):
            try:
                widget.tk.call(widget._w, "configure", "-" + opt, _pal(tok))
            except Exception:
                pass
    if isinstance(widget, ttk.Combobox):
        try:
            lb = f"{widget._w}.popdown.f.l"
            widget.tk.call(lb, "configure", "-background", _pal("surface2"), "-foreground",
                           _pal("text"), "-selectbackground", _pal("accent"),
                           "-selectforeground", "#ffffff", "-borderwidth", 0,
                           "-highlightthickness", 0)
        except Exception:
            pass
    for child in widget.winfo_children():
        retheme_tree(child)


def _hwnd_of(win):
    import ctypes
    win.update_idletasks()
    wid = win.winfo_id()
    root = ctypes.windll.user32.GetAncestor(wid, 2)
    return root or wid


def _colorref(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def apply_window_material(win, material=None, caption=None):
    material = material or _THEME["material"]
    if os.name != "nt":
        return "solid"
    try:
        import ctypes
        from ctypes import wintypes
        build = _win_build()
        spec = WINDOW_MATERIALS.get(material, WINDOW_MATERIALS["solid"])
        hwnd = _hwnd_of(win)
        dwm = ctypes.windll.dwmapi

        def set_attr(attr, value):
            v = ctypes.c_int(value & 0xFFFFFFFF if value >= 0 else value)
            try:
                dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(v), 4)
            except Exception:
                pass

        class MARGINS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                        ("t", ctypes.c_int), ("b", ctypes.c_int)]

        def extend(n):
            try:
                m = MARGINS(n, n, n, n)
                dwm.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(m))
            except Exception:
                pass

        set_attr(20, 1)
        set_attr(19, 1)
        if build >= 22000:
            set_attr(33, 2)

        applied = "solid"
        wants_effect = spec["backdrop"] is not None and build >= spec["min_build"]
        if wants_effect and build >= 22621:
            extend(-1)
            set_attr(38, spec["backdrop"])
            set_attr(35, 0xFFFFFFFF)
            set_attr(34, 0xFFFFFFFF)
            applied = material
        elif wants_effect and material == "mica" and build >= 22000:
            extend(-1)
            set_attr(1029, 1)
            applied = "mica"
        elif wants_effect and material == "acrylic":
            if _legacy_acrylic(hwnd, 0xD01A1C22):
                applied = "acrylic"
        if applied == "solid":
            if build >= 22621:
                set_attr(38, 1)
            extend(0)
            _legacy_acrylic(hwnd, None)
            set_attr(35, _colorref(caption or PALETTES["solid"]["bg"]))
            set_attr(36, _colorref(PALETTES["solid"]["text"]))
        win.update_idletasks()
        return applied
    except Exception as e:
        print(f"Не удалось применить материал окна: {e}")
        return "solid"


def _legacy_acrylic(hwnd, gradient_abgr):
    try:
        import ctypes

        class ACCENT(ctypes.Structure):
            _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                        ("color", ctypes.c_uint), ("anim", ctypes.c_int)]

        class WCA(ctypes.Structure):
            _fields_ = [("attr", ctypes.c_int), ("data", ctypes.c_void_p),
                        ("size", ctypes.c_size_t)]

        acc = ACCENT(0 if gradient_abgr is None else 4, 2, gradient_abgr or 0, 0)
        data = WCA(19, ctypes.cast(ctypes.pointer(acc), ctypes.c_void_p), ctypes.sizeof(acc))
        fn = ctypes.windll.user32.SetWindowCompositionAttribute
        return bool(fn(ctypes.c_void_p(hwnd), ctypes.byref(data)))
    except Exception:
        return False


def _style_toplevel(win):
    try:
        if not win.winfo_exists() or win.overrideredirect():
            return
        apply_window_material(win, "solid", caption=_pal("surface"))
    except Exception:
        pass


def apply_ttk_styles(style, root):
    p, A, AH = _THEME["pal"], _THEME["accent"], _THEME["hover"]
    BG, S1, S2, S3 = p["bg"], p["surface"], p["surface2"], p["surface3"]
    TXT, DIM, BRD = p["text"], p["text_dim"], p["border"]
    FONT = ("Segoe UI", 10)
    style.theme_use("clam")
    root.configure(bg=BG)
    style.configure(".", background=BG, foreground=TXT, font=FONT, bordercolor=BRD,
                    lightcolor=BG, darkcolor=BG, focuscolor=BG, troughcolor=S1)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TXT, font=FONT)

    style.configure("TButton", background=S2, foreground=TXT, font=FONT, padding=(14, 7),
                    borderwidth=1, bordercolor=BRD, lightcolor=S2, darkcolor=S2, focuscolor=S2,
                    relief="flat")
    style.map("TButton",
              background=[("disabled", S1), ("pressed", S1), ("active", S3)],
              foreground=[("disabled", p["text_faint"])],
              bordercolor=[("active", A), ("focus", A)])
    style.configure("Accent.TButton", background=A, foreground="#ffffff",
                    font=("Segoe UI", 12, "bold"), padding=(18, 11), borderwidth=0,
                    bordercolor=A, lightcolor=A, darkcolor=A, focuscolor=A)
    style.map("Accent.TButton",
              background=[("disabled", S2), ("pressed", AH), ("active", AH)],
              bordercolor=[("active", AH)], lightcolor=[("active", AH)], darkcolor=[("active", AH)],
              foreground=[("disabled", p["text_faint"])])
    style.configure("Small.TButton", background=S2, foreground=TXT, font=("Segoe UI", 9),
                    padding=(8, 4), borderwidth=1, bordercolor=BRD, lightcolor=S2, darkcolor=S2)
    style.map("Small.TButton", background=[("pressed", S1), ("active", S3)],
              bordercolor=[("active", A)])

    for name in ("TEntry", "TSpinbox"):
        style.configure(name, fieldbackground=S2, background=S2, foreground=TXT,
                        insertcolor=TXT, bordercolor=BRD, lightcolor=BRD, darkcolor=BRD,
                        padding=(8, 6), arrowcolor=TXT, selectbackground=A, selectforeground="#ffffff")
        style.map(name, bordercolor=[("focus", A), ("hover", p["text_faint"])],
                  lightcolor=[("focus", A)], darkcolor=[("focus", A)])
    style.configure("TCombobox", fieldbackground=S2, background=S2, foreground=TXT,
                    arrowcolor=TXT, bordercolor=BRD, lightcolor=BRD, darkcolor=BRD,
                    padding=(8, 6), selectbackground=S2, selectforeground=TXT)
    style.map("TCombobox",
              fieldbackground=[("readonly", S2), ("disabled", S1)],
              selectbackground=[("readonly", S2)], selectforeground=[("readonly", TXT)],
              background=[("active", S3), ("!disabled", S2)],
              bordercolor=[("focus", A), ("hover", p["text_faint"])],
              arrowcolor=[("disabled", p["text_faint"])])
    root.option_add("*TCombobox*Listbox.background", S2)
    root.option_add("*TCombobox*Listbox.foreground", TXT)
    root.option_add("*TCombobox*Listbox.selectBackground", A)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*TCombobox*Listbox.font", FONT)

    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=BG, foreground=TXT, indicatorcolor=S2,
                        indicatorbackground=S2, upperbordercolor=BRD, lowerbordercolor=BRD,
                        padding=3)
        style.map(name, background=[("active", BG)], foreground=[("disabled", p["text_faint"])],
                  indicatorcolor=[("selected", A), ("pressed", A)],
                  indicatorbackground=[("selected", A)])
    style.configure("TLabelframe", background=BG, foreground=TXT, bordercolor=BRD,
                    lightcolor=BG, darkcolor=BG, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=BG, foreground=A, font=("Segoe UI", 10, "bold"))
    style.configure("TSeparator", background=BRD)
    style.configure("Card.TFrame", background=S1)
    style.configure("Card.TLabel", background=S1, foreground=TXT, font=FONT)
    style.configure("Card.TLabelframe", background=S1, foreground=TXT, bordercolor=BRD,
                    lightcolor=S1, darkcolor=S1, relief="solid", borderwidth=1)
    style.configure("Card.TLabelframe.Label", background=S1, foreground=A, font=("Segoe UI", 10, "bold"))
    for name in ("Card.TCheckbutton", "Card.TRadiobutton"):
        style.configure(name, background=S1, foreground=TXT, indicatorcolor=S2,
                        indicatorbackground=S2, upperbordercolor=BRD, lowerbordercolor=BRD, padding=3)
        style.map(name, background=[("active", S1)], foreground=[("disabled", p["text_faint"])],
                  indicatorcolor=[("selected", A), ("pressed", A)],
                  indicatorbackground=[("selected", A)])

    style.configure("TProgressbar", background=A, troughcolor=S1, bordercolor=S1,
                    lightcolor=A, darkcolor=A, thickness=8)
    style.configure("Horizontal.TProgressbar", background=A, troughcolor=S1, bordercolor=S1,
                    lightcolor=A, darkcolor=A, thickness=8)
    style.configure("TScale", background=BG, troughcolor=S2, bordercolor=BG,
                    lightcolor=A, darkcolor=A)

    for orient, side in (("Vertical", "ns"), ("Horizontal", "ew")):
        style.layout(f"{orient}.TScrollbar", [(f"{orient}.Scrollbar.trough", {
            "sticky": side, "children": [(f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure(f"{orient}.TScrollbar", background=S3, troughcolor=BG, bordercolor=BG,
                        lightcolor=S3, darkcolor=S3, gripcount=0, arrowsize=10, width=10)
        style.map(f"{orient}.TScrollbar", background=[("pressed", A), ("active", p["text_faint"])],
                  lightcolor=[("pressed", A), ("active", p["text_faint"])],
                  darkcolor=[("pressed", A), ("active", p["text_faint"])])

    style.layout("TNotebook.Tab", [])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0, padding=0,
                    lightcolor=BG, darkcolor=BG, bordercolor=BG)

    style.configure("Treeview", background=S1, fieldbackground=S1, foreground=TXT,
                    bordercolor=BRD, lightcolor=S1, darkcolor=S1, rowheight=26, borderwidth=0)
    style.map("Treeview", background=[("selected", A)], foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=S2, foreground=TXT, relief="flat",
                    font=("Segoe UI", 10, "bold"), padding=(8, 6), borderwidth=0)
    style.map("Treeview.Heading", background=[("active", S3)])


GAME_DIR_NAME = ".minecraft_mafinlauncher"
LEGACY_GAME_DIR_NAMES = (".minecraft_tlauncher", "minecraft_tlauncher")


def _app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _replace_path_prefix(obj, old, new):
    if isinstance(obj, str):
        return obj.replace(old, new) if old in obj else obj
    if isinstance(obj, list):
        return [_replace_path_prefix(x, old, new) for x in obj]
    if isinstance(obj, dict):
        return {k: _replace_path_prefix(v, old, new) for k, v in obj.items()}
    return obj


def migrate_legacy_game_dir(config=None):
    bases = []
    for base in (_app_base_dir(), os.getcwd()):
        if base not in bases:
            bases.append(base)
    for base in bases:
        new = os.path.join(base, GAME_DIR_NAME)
        if os.path.exists(new):
            continue
        for old_name in LEGACY_GAME_DIR_NAMES:
            old = os.path.join(base, old_name)
            if os.path.isdir(old):
                try:
                    os.rename(old, new)
                    print(f"Папка игры переименована: {old} -> {new}")
                    break
                except OSError as e:
                    print(f"Не удалось переименовать {old}: {e}")
    if config is None:
        return False
    current = config.get("minecraft_dir")
    if not current:
        return False
    head, tail = os.path.split(os.path.normpath(current))
    if tail not in LEGACY_GAME_DIR_NAMES:
        return False
    old, new = os.path.join(head, tail), os.path.join(head, GAME_DIR_NAME)
    if os.path.isdir(old) and not os.path.exists(new):
        try:
            os.rename(old, new)
        except OSError as e:
            print(f"Не удалось переименовать {old}: {e}")
            return False
    if not os.path.isdir(new):
        return False
    for key in list(config.keys()):
        config[key] = _replace_path_prefix(config[key], old, new)
    return True


migrate_legacy_game_dir()

DEFAULT_CONFIG = {
    "minecraft_dir": os.path.join(os.getcwd(), GAME_DIR_NAME),
    "java_path": "java",
    "memory_mb": 2048,
    "auto_memory": False,
    "jvm_preset": "default",
    "last_version": "",
    "accounts": [],
    "selected_account": 0,
    "skin_auto_download": True,
    "skin_system_enabled": True,
    "launcher_theme": DEFAULT_LAUNCHER_THEME,
    "window_material": DEFAULT_WINDOW_MATERIAL,
    "nav_style": "list",
    "auto_backup_worlds": False,
    "backup_keep": 5,
    "show_alpha_beta": False,
    "servers": [],
    "p2p_last_port": 25565,
    "auto_clean_logs": True,
    "log_max_days": 7,
    "server_token": "",
    "server_nickname": "",
}


_JSON_IO_LOCK = threading.RLock()


def load_json_file(path, default):
    with _JSON_IO_LOCK:
        for candidate in (path, path + ".bak"):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(default, dict):
                    if not isinstance(data, dict):
                        continue
                    for k, v in default.items():
                        if k not in data:
                            data[k] = v
                return data
            except Exception:
                continue
    return default.copy() if isinstance(default, dict) else default


def save_json_file(path, data):
    """Атомарная запись: tmp + fsync + replace, чтобы токен не терялся при сбое/гонке потоков."""
    with _JSON_IO_LOCK:
        try:
            text = None
            for _ in range(3):
                try:
                    text = json.dumps(data, indent=4, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.01)
            if text is None:
                return
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        json.load(f)
                    shutil.copyfile(path, path + ".bak")
                except Exception:
                    pass
            os.replace(tmp, path)
        except Exception as e:
            print(f"Ошибка сохранения {path}: {e}")


class SessionError(Exception):
    pass


class ServerAPI:

    def __init__(self, base_url):
        self.base_url = (base_url or "http://127.0.0.1:3092").rstrip("/")
        self.token = None
        self.nickname = None
        self.is_admin = False
        self.coins = 0
        self.playtime = 0

    def set_base_url(self, base_url):
        self.base_url = (base_url or "http://127.0.0.1:3092").rstrip("/")

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _safe_json(self, resp):
        try:
            return resp.json()
        except Exception:
            return {"error": f"Некорректный ответ сервера (HTTP {resp.status_code})"}

    def _get(self, path, params=None, timeout=10):
        r = requests.get(f"{self.base_url}{path}", headers=self._headers(), params=params, timeout=timeout)
        return r.status_code, self._safe_json(r)

    def _post(self, path, payload=None):
        r = requests.post(f"{self.base_url}{path}", headers=self._headers(), json=payload or {}, timeout=10)
        return r.status_code, self._safe_json(r)

    def _put(self, path, payload=None):
        r = requests.put(f"{self.base_url}{path}", headers=self._headers(), json=payload or {}, timeout=10)
        return r.status_code, self._safe_json(r)

    def _delete(self, path):
        r = requests.delete(f"{self.base_url}{path}", headers=self._headers(), timeout=10)
        return r.status_code, self._safe_json(r)

    def cosmetics_list(self):
        return self._get("/api/cosmetics")

    def cosmetics_buy(self, cosmetic_id):
        return self._post("/api/cosmetics/buy", {"id": cosmetic_id})

    def fetch_bytes(self, path):
        r = requests.get(f"{self.base_url}{path}", headers=self._headers(), timeout=10)
        return r.status_code, (r.content if r.status_code == 200 else b"")

    def skin_mine(self, game_name):
        return self._get("/api/skin/mine", {"game_name": game_name})

    def tg_status(self):
        return self._get("/api/tg/status")

    def tg_unlink(self, bot):
        return self._post("/api/tg/unlink", {"bot": bot})

    def cosmetics_adjust(self, y_adj, scale_adj):
        return self._post("/api/cosmetics/adjust", {"y_adj": y_adj, "scale_adj": scale_adj})

    def cosmetics_equip(self, cosmetic_id):
        return self._post("/api/cosmetics/equip", {"id": cosmetic_id})

    def top_weekly(self, limit=20):
        return self._get("/api/top/weekly", params={"limit": limit})

    def modpack_manifest(self):
        return self._get("/api/modpack", timeout=20)

    def register(self, nickname, password):
        return self._post("/api/register", {"nickname": nickname, "password": password})

    def login(self, nickname, password):
        status, data = self._post("/api/login", {"nickname": nickname, "password": password})
        if status == 200 and data.get("success"):
            self.token = data.get("token")
            self.nickname = data.get("nickname")
            self.is_admin = bool(data.get("is_admin"))
            self.coins = data.get("coins", 0)
            self.playtime = data.get("playtime", 0)
        return status, data

    def login_with_token(self, token):
        self.token = token
        status, data = self.me()
        if status == 200 and "nickname" in data:
            self.nickname = data.get("nickname")
            self.is_admin = bool(data.get("is_admin"))
            self.coins = data.get("coins", 0)
            self.playtime = data.get("total_playtime_minutes", 0)
            return True, data
        if status in (401, 403):
            self.token = None
            return False, data
        return False, {"_transient": True, "status": status, "error": data.get("error") if isinstance(data, dict) else None}

    def drop_local(self):
        """Сбросить сессию только локально, не трогая токен на сервере."""
        self.token = None
        self.nickname = None
        self.is_admin = False
        self.coins = 0
        self.playtime = 0

    def logout(self):
        if self.token:
            try:
                self._post("/api/logout")
            except Exception:
                pass
        self.token = None
        self.nickname = None
        self.is_admin = False
        self.coins = 0
        self.playtime = 0

    def is_logged_in(self):
        return bool(self.token)

    def me(self):
        return self._get("/api/me")

    def heartbeat(self):
        return self._post("/api/heartbeat")

    def top(self, limit=10):
        return self._get("/api/top", params={"limit": limit})

    def report_launch(self, version):
        return self._post("/api/launch", {"version": version})

    def register_mc_session(self, game_name):
        return self._post("/api/mc/session", {"game_name": game_name})

    def mc_status(self):
        return self._get("/api/mc/status", timeout=4)

    def report_playtime(self, minutes, version, launch_counted=False):
        return self._post("/api/playtime", {
            "minutes": minutes, "version": version, "launch_counted": launch_counted,
        })

    def admin_profiles(self):
        return self._get("/api/admin/profiles")

    def admin_ban(self, nickname, reason):
        return self._post("/api/admin/ban", {"nickname": nickname, "reason": reason})

    def admin_unban(self, nickname):
        return self._post("/api/admin/unban", {"nickname": nickname})

    def admin_make_admin(self, nickname):
        return self._post("/api/admin/make_admin", {"nickname": nickname})

    def admin_remove_admin(self, nickname):
        return self._post("/api/admin/remove_admin", {"nickname": nickname})

    def admin_logs(self, limit=100):
        return self._get("/api/admin/logs", params={"limit": limit})

    def admin_stats(self):
        return self._get("/api/admin/stats")

    def admin_delete_account(self, nickname):
        return self._post("/api/admin/delete_account", {"nickname": nickname})

    def list_gifts(self):
        return self._get("/api/gifts")

    def skin_upload(self, game_name, png_bytes, model="default"):
        import base64
        return self._post("/api/skin/upload", {
            "game_name": game_name, "model": model,
            "png_b64": base64.b64encode(png_bytes).decode("ascii")})

    def skin_delete(self, game_name):
        return self._post("/api/skin/delete", {"game_name": game_name})

    def shop_items(self):
        return self._get("/api/shop/items", timeout=4)

    def shop_buy(self, item_id, game_name):
        return self._post("/api/shop/buy", {"item_id": item_id, "game_name": game_name})

    def send_gift(self, gift_id, to, message=""):
        return self._post("/api/gifts/send", {"gift_id": gift_id, "to": to, "message": message})

    def gifts_received(self, nickname):
        return self._get(f"/api/gifts/received/{nickname}")

    def gift_image_url(self, image_url):
        return f"{self.base_url}{image_url}"

    def list_quests(self):
        return self._get("/api/quests")

    def claim_quest(self, quest_id):
        return self._post("/api/quests/claim", {"quest_id": quest_id})

    def sync_achievements(self, ids):
        return self._post("/api/achievements/sync", {"ids": ids})

    def my_achievements(self):
        return self._get("/api/achievements/mine")

    def bump_achievement_counters(self, deltas=None, set_values=None):
        payload = {}
        if deltas:
            payload["deltas"] = deltas
        if set_values:
            payload["set"] = set_values
        if not payload:
            return 200, {"success": True}
        return self._post("/api/achievement_counters/bump", payload)

    def my_achievement_counters(self):
        return self._get("/api/achievement_counters/mine")

    def get_profile(self, nickname):
        return self._get(f"/api/profile/{nickname}")

    def search_users(self, query):
        return self._get("/api/users/search", params={"q": query})

    def update_my_bio(self, bio):
        r = requests.put(f"{self.base_url}/api/profile/me", headers=self._headers(),
                          json={"bio": bio}, timeout=10)
        return r.status_code, self._safe_json(r)

    def set_gifts_visibility(self, visible):
        return self._post("/api/profile/gifts_visibility", {"visible": visible})

    def friend_request(self, nickname):
        return self._post("/api/friends/request", {"nickname": nickname})

    def friend_respond(self, nickname, accept):
        return self._post("/api/friends/respond", {"nickname": nickname, "accept": accept})

    def friend_remove(self, nickname):
        return self._post("/api/friends/remove", {"nickname": nickname})

    def friends_list(self):
        return self._get("/api/friends/list")

    def send_message(self, to, text):
        return self._post("/api/messages/send", {"to": to, "text": text})

    def get_messages_with(self, nickname, after_id=0):
        return self._get(f"/api/messages/with/{nickname}", params={"after_id": after_id})

    def set_typing(self, to_nickname):
        return self._post("/api/messages/typing", {"to": to_nickname})

    def get_typing(self, nickname):
        return self._get(f"/api/messages/typing/{nickname}")

    def unread_message_count(self):
        return self._get("/api/messages/unread_count")

    def get_news(self):
        return self._get("/api/news")

    def news_latest_id(self):
        return self._get("/api/news/latest_id")

    def publish_news(self, title, body):
        return self._post("/api/news", {"title": title, "body": body})

    def edit_news(self, news_id, title, body):
        return self._put(f"/api/news/{news_id}", {"title": title, "body": body})

    def delete_news(self, news_id):
        return self._delete(f"/api/news/{news_id}")

    def latest_update(self):
        return self._get("/api/updates/latest")

    def download_update(self, download_url, dest_path, progress_cb=None):
        url = f"{self.base_url}{download_url}" if download_url.startswith("/") else download_url
        with requests.get(url, headers=self._headers(), stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            done = 0
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
        return dest_path


class ProfileWindow(tk.Toplevel):

    def __init__(self, parent, api, nickname, on_friend_action=None):
        super().__init__(parent)
        self.api = api
        self.nickname = nickname
        self.on_friend_action = on_friend_action or (lambda: None)
        self.is_self = (nickname == api.nickname)

        self.title(f"Профиль — {nickname}")
        self.geometry("380x420")

        self.content = tk.Frame(self)
        self.content.pack(fill="both", expand=True, padx=16, pady=16)
        self._loading_label = tk.Label(self.content, text="Загрузка...", fg="#888")
        self._loading_label.pack(pady=30)

        self.refresh()

    def refresh(self):
        for w in self.content.winfo_children():
            w.destroy()
        tk.Label(self.content, text="Загрузка...", fg="#888").pack(pady=30)

        def work():
            return self.api.get_profile(self.nickname)

        def done(result, error):
            for w in self.content.winfo_children():
                w.destroy()
            if error or not result or result[0] != 200:
                tk.Label(self.content, text="Не удалось загрузить профиль", fg="#ed4245").pack(pady=30)
                return
            self._render(result[1])

        threading.Thread(target=lambda: self.after(0, done, *self._safe_work(work)), daemon=True).start()

    @staticmethod
    def _safe_work(work_fn):
        try:
            return work_fn(), None
        except Exception as e:
            return None, e

    def _render(self, p):
        online_txt = "🟢 В сети" if p.get("is_online") else "⚪ Не в сети"
        header = tk.Frame(self.content)
        header.pack(fill="x", pady=(0, 10))
        tk.Label(header, text=p["nickname"], font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(header, text=online_txt, fg="#43b581" if p.get("is_online") else "#888").pack(side="right")

        bio = p.get("bio") or ("Пока ничего не написано о себе." if self.is_self else "")
        self.bio_var = tk.StringVar(value=bio)
        if self.is_self:
            self.bio_entry = tk.Text(self.content, height=3, width=40, wrap="word",
                                      bg="#2d323c", fg="white", insertbackground="white")
            self.bio_entry.insert("1.0", p.get("bio") or "")
            self.bio_entry.pack(fill="x", pady=(0, 6))
            tk.Button(self.content, text="💾 Сохранить описание", command=self._save_bio).pack(anchor="w", pady=(0, 10))
        else:
            tk.Label(self.content, text=bio, wraplength=340, justify="left", fg="#c9d1d9").pack(
                fill="x", pady=(0, 10), anchor="w")

        stats = tk.Frame(self.content)
        stats.pack(fill="x", pady=(0, 12))
        for label, value in [
            ("🪙 Монеты", f"{p.get('coins', 0):g}"),
            ("⏱ Время в игре", f"{p.get('total_playtime_minutes', 0):g} мин"),
            ("🚀 Запусков", p.get("total_launches", 0)),
            ("🏆 Ачивок", p.get("achievements_count", 0)),
        ]:
            row = tk.Frame(stats)
            row.pack(fill="x")
            tk.Label(row, text=label, fg="#888").pack(side="left")
            tk.Label(row, text=str(value), font=("Segoe UI", 10, "bold")).pack(side="right")

        gifts_frame = tk.Frame(self.content)
        gifts_frame.pack(fill="x", pady=(4, 10))
        if self.is_self:
            self.gifts_visible_var = tk.BooleanVar(value=bool(p.get("gifts_visible", True)))
            tk.Checkbutton(gifts_frame, text="Показывать подарки на моём профиле",
                            variable=self.gifts_visible_var,
                            command=self._toggle_gifts_visibility).pack(anchor="w")
        if p.get("gifts_visible", True):
            self._render_gifts_received(gifts_frame)
        else:
            if not self.is_self:
                tk.Label(gifts_frame, text="🎁 Подарки скрыты владельцем профиля", fg="#888").pack(anchor="w")

        actions = tk.Frame(self.content)
        actions.pack(fill="x", pady=(6, 0))
        if not self.is_self:
            status = p.get("friendship_status", "none")
            if status == "friends":
                tk.Label(actions, text="✅ Вы друзья", fg="#43b581").pack(side="left")
            elif status == "request_sent":
                tk.Label(actions, text="⏳ Заявка отправлена", fg="#888").pack(side="left")
            elif status == "request_received":
                tk.Button(actions, text="Принять заявку в друзья", bg="#43b581", fg="white",
                          command=self._accept_request).pack(side="left")
            else:
                tk.Button(actions, text="➕ Добавить в друзья", command=self._send_request).pack(side="left")
        tk.Button(actions, text="🎁 Подарить подарок", command=self._open_gift_shop).pack(side="left", padx=(8, 0))

    def _render_gifts_received(self, parent):
        tk.Label(parent, text="🎁 Полученные подарки", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))
        holder = tk.Frame(parent)
        holder.pack(fill="x")
        placeholder = tk.Label(holder, text="Загрузка...", fg="#888")
        placeholder.pack(anchor="w")

        def work():
            return self.api.gifts_received(self.nickname)

        def done(result, error):
            for w in holder.winfo_children():
                w.destroy()
            if error or not result or result[0] != 200:
                tk.Label(holder, text="Не удалось загрузить подарки", fg="#ed4245").pack(anchor="w")
                return
            gifts = result[1].get("gifts", [])
            if not gifts:
                tk.Label(holder, text="Пока нет подарков", fg="#888").pack(anchor="w")
                return
            row = tk.Frame(holder)
            row.pack(fill="x")
            for i, giftinfo in enumerate(gifts[:12]):
                cell = tk.Frame(row, bd=1, relief="solid")
                cell.grid(row=i // 6, column=i % 6, padx=3, pady=3)
                self._load_gift_thumbnail(cell, giftinfo["image_url"])
                tip = f"{giftinfo['name']} от {giftinfo['from']}"
                if giftinfo.get("note"):
                    tip += f": {giftinfo['note']}"
                tk.Label(cell, text=giftinfo["name"], font=("Segoe UI", 7), wraplength=60).pack()

        threading.Thread(target=lambda: self.after(0, done, *self._safe_work(work)), daemon=True).start()

    def _load_gift_thumbnail(self, cell, image_url):
        label = tk.Label(cell, text="🎁", width=6, height=3)
        label.pack()

        def work():
            resp = requests.get(f"{self.api.base_url}{image_url}", timeout=10)
            resp.raise_for_status()
            return resp.content

        def done(data, error):
            if error or not data:
                return
            animate_image_on_label(label, data, max_px=48)

        threading.Thread(target=lambda: self.after(0, done, *self._safe_work(work)), daemon=True).start()

    def _open_gift_shop(self):
        GiftShopWindow(self.master, self.api, self.nickname)

    def _save_bio(self):
        bio = self.bio_entry.get("1.0", "end").strip()
        status, data = self.api.update_my_bio(bio)
        if status == 200:
            self.refresh()
        else:
            messagebox.showerror("Ошибка", data.get("error", "Не удалось сохранить описание"))

    def _toggle_gifts_visibility(self):
        status, data = self.api.set_gifts_visibility(self.gifts_visible_var.get())
        if status != 200:
            messagebox.showerror("Ошибка", data.get("error", "Не удалось сохранить настройку"))
            self.gifts_visible_var.set(not self.gifts_visible_var.get())

    def _send_request(self):
        status, data = self.api.friend_request(self.nickname)
        if status == 200 and data.get("success"):
            self.on_friend_action()
            self.refresh()
        else:
            messagebox.showerror("Ошибка", data.get("error", "Не удалось отправить заявку"))

    def _accept_request(self):
        status, data = self.api.friend_respond(self.nickname, True)
        if status == 200 and data.get("success"):
            self.on_friend_action()
            self.refresh()
        else:
            messagebox.showerror("Ошибка", data.get("error", "Не удалось принять заявку"))


class GiftShopWindow(tk.Toplevel):

    def __init__(self, master, api, preset_nickname=None):
        super().__init__(master)
        self.api = api
        self.preset_nickname = preset_nickname or ""
        self.title("🎁 Магазин подарков")
        self.geometry("520x420")
        self.configure(bg="#23272e")

        tk.Label(self, text="🎁 Магазин подарков", font=("Segoe UI", 13, "bold"),
                 bg="#23272e", fg="white").pack(pady=(10, 6))

        self.canvas_holder = tk.Frame(self, bg="#23272e")
        self.canvas_holder.pack(fill="both", expand=True, padx=10, pady=6)

        self.status_label = tk.Label(self, text="Загрузка витрины...", bg="#23272e", fg="#888")
        self.status_label.pack(pady=(0, 8))

        self._load()

    def _load(self):
        def work():
            return self.api.list_gifts()

        def done(result, error):
            for w in self.canvas_holder.winfo_children():
                w.destroy()
            if error or not result or result[0] != 200:
                self.status_label.configure(text="Не удалось загрузить магазин подарков", fg="#ed4245")
                return
            gifts = result[1].get("gifts", [])
            if not gifts:
                self.status_label.configure(text="Пока нет подарков в продаже", fg="#888")
                return
            self.status_label.configure(text="")
            for gift in gifts:
                self._render_gift_row(gift)

        threading.Thread(target=lambda: self.after(0, done, *self._safe_work(work)), daemon=True).start()

    @staticmethod
    def _safe_work(work_fn):
        try:
            return work_fn(), None
        except Exception as e:
            return None, e

    def _render_gift_row(self, gift):
        row = tk.Frame(self.canvas_holder, bg="#2d323c")
        row.pack(fill="x", pady=3)

        thumb = tk.Label(row, text="🎁", width=6, height=3, bg="#2d323c", fg="white")
        thumb.pack(side="left", padx=6, pady=6)
        self._load_thumbnail(thumb, gift["image_url"])

        info = tk.Frame(row, bg="#2d323c")
        info.pack(side="left", fill="x", expand=True, padx=6)
        tk.Label(info, text=gift["name"], font=("Segoe UI", 10, "bold"),
                 bg="#2d323c", fg="white").pack(anchor="w")
        qty_txt = "неограничено" if gift.get("quantity") is None else f"осталось: {gift['quantity']}"
        tk.Label(info, text=f"🪙 {gift['price']:g} монет · {qty_txt}",
                 bg="#2d323c", fg="#888").pack(anchor="w")
        if gift.get("sale_until"):
            tk.Label(info, text=f"⏳ Продажа до {gift['sale_until']}",
                     bg="#2d323c", fg="#faa61a", font=("Segoe UI", 8)).pack(anchor="w")

        tk.Button(row, text="Подарить", command=lambda g=gift: self._open_send_dialog(g)).pack(
            side="right", padx=8)

    def _load_thumbnail(self, label, image_url):
        def work():
            resp = requests.get(self.api.gift_image_url(image_url), timeout=10)
            resp.raise_for_status()
            return resp.content

        def done(data, error):
            if error or not data:
                return
            animate_image_on_label(label, data, max_px=48)

        threading.Thread(target=lambda: self.after(0, done, *self._safe_work(work)), daemon=True).start()

    def _open_send_dialog(self, gift):
        dialog = tk.Toplevel(self)
        dialog.title(f"Подарить: {gift['name']}")
        dialog.geometry("360x260")
        dialog.configure(bg="#23272e")
        dialog.transient(self)
        dialog.grab_set()

        tk.Label(dialog, text=f"🎁 {gift['name']} — {gift['price']:g} монет",
                 bg="#23272e", fg="white", font=("Segoe UI", 10, "bold")).pack(pady=(12, 8))

        tk.Label(dialog, text="Кому (ник):", bg="#23272e", fg="#888").pack(anchor="w", padx=14)
        nickname_var = tk.StringVar(value=self.preset_nickname)
        tk.Entry(dialog, textvariable=nickname_var, bg="#2d323c", fg="white",
                 insertbackground="white").pack(fill="x", padx=14, pady=(0, 8))

        tk.Label(dialog, text="Сообщение к подарку:", bg="#23272e", fg="#888").pack(anchor="w", padx=14)
        message_text = tk.Text(dialog, height=4, bg="#2d323c", fg="white", insertbackground="white")
        message_text.pack(fill="x", padx=14, pady=(0, 10))

        status_lbl = tk.Label(dialog, text="", bg="#23272e", fg="#ed4245")
        status_lbl.pack()

        send_btn = tk.Button(dialog, text="Подарить 🎁", bg="#43b581", fg="white")

        def do_send():
            to = nickname_var.get().strip()
            message = message_text.get("1.0", "end").strip()
            if not to:
                status_lbl.configure(text="Укажите ник получателя")
                return

            send_btn.configure(state="disabled")
            status_lbl.configure(text="Отправка...", fg="#888")

            def work():
                return self.api.send_gift(gift["id"], to, message)

            def done(result, error):
                if error:
                    send_btn.configure(state="normal")
                    status_lbl.configure(text="Ошибка сети", fg="#ed4245")
                    return
                status, data = result
                if status == 200 and data.get("success"):
                    messagebox.showinfo("Готово", f"🎁 Подарок «{gift['name']}» отправлен игроку {to}!")
                    dialog.destroy()
                    self._load()
                else:
                    send_btn.configure(state="normal")
                    status_lbl.configure(text=data.get("error", "Не удалось отправить подарок"), fg="#ed4245")

            threading.Thread(target=lambda: self.after(0, done, *self._safe_work(work)), daemon=True).start()

        send_btn.configure(command=do_send)
        send_btn.pack(pady=(0, 10))


class UserSearchWindow(tk.Toplevel):

    def __init__(self, parent, api, on_friend_action=None):
        super().__init__(parent)
        self.api = api
        self.on_friend_action = on_friend_action or (lambda: None)
        self.title("Найти людей")
        self.geometry("340x420")

        top = tk.Frame(self)
        top.pack(fill="x", padx=10, pady=10)
        self.query_entry = tk.Entry(top)
        self.query_entry.pack(side="left", fill="x", expand=True)
        self.query_entry.bind("<Return>", lambda e: self._search())
        tk.Button(top, text="🔍", command=self._search).pack(side="left", padx=(6, 0))
        self.query_entry.focus_set()

        self.results_frame = tk.Frame(self)
        self.results_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        tk.Label(self.results_frame, text="Введите минимум 2 символа ника", fg="#888").pack(pady=20)

    def _search(self):
        query = self.query_entry.get().strip()
        for w in self.results_frame.winfo_children():
            w.destroy()
        if len(query) < 2:
            tk.Label(self.results_frame, text="Введите минимум 2 символа ника", fg="#888").pack(pady=20)
            return
        tk.Label(self.results_frame, text="Ищем...", fg="#888").pack(pady=20)

        def work():
            return self.api.search_users(query)

        def done(result, error):
            for w in self.results_frame.winfo_children():
                w.destroy()
            if error or not result or result[0] != 200:
                tk.Label(self.results_frame, text="Ошибка поиска", fg="#ed4245").pack(pady=20)
                return
            results = result[1].get("results", [])
            if not results:
                tk.Label(self.results_frame, text="Никого не найдено", fg="#888").pack(pady=20)
                return
            for r in results:
                self._render_result_row(r)

        threading.Thread(target=lambda: self.after(0, done, *self._safe_work(work)), daemon=True).start()

    @staticmethod
    def _safe_work(work_fn):
        try:
            return work_fn(), None
        except Exception as e:
            return None, e

    def _render_result_row(self, r):
        row = tk.Frame(self.results_frame, relief="groove", borderwidth=1, cursor="hand2")
        row.pack(fill="x", pady=2)
        online = "🟢" if r.get("is_online") else "⚪"
        status_txt = {"friends": "друзья", "pending": "заявка"}.get(r.get("friendship_status"), "")
        label = tk.Label(row, text=f"{online} {r['nickname']}" + (f"  ({status_txt})" if status_txt else ""),
                          anchor="w", padx=8, pady=6)
        label.pack(fill="x")
        for widget in (row, label):
            widget.bind("<Button-1>", lambda e, n=r["nickname"]: self._open_profile(n))

    def _open_profile(self, nickname):
        ProfileWindow(self, self.api, nickname, on_friend_action=self.on_friend_action)


class QuestsPanel(tk.Frame):

    def __init__(self, parent, api):
        super().__init__(parent)
        self.api = api

        top = tk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)
        tk.Label(top, text="📋 Задания", font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Button(top, text="Обновить", command=self.refresh).pack(side="right")

        container = tk.Frame(self)
        container.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.list_frame = tk.Frame(canvas)
        self.list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, fg="#888").pack(pady=(0, 6))

        self.refresh()

    def refresh(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.status_var.set("Загрузка...")

        def work():
            return self.api.list_quests()

        def done(result, error):
            if error or not result or result[0] != 200:
                self.status_var.set("Не удалось загрузить задания")
                return
            quests = result[1]
            if not quests:
                self.status_var.set("Заданий пока нет")
                return
            self.status_var.set("")
            for q in quests:
                self._render_quest_row(q)

        threading.Thread(target=lambda: self.after(0, done, *self._safe_work(work)), daemon=True).start()

    @staticmethod
    def _safe_work(work_fn):
        try:
            return work_fn(), None
        except Exception as e:
            return None, e

    def _render_quest_row(self, q):
        row = tk.Frame(self.list_frame, relief="groove", borderwidth=1)
        row.pack(fill="x", pady=4, padx=2)

        title = q["title"] + (f" — {q['description']}" if q.get("description") else "")
        tk.Label(row, text=title, font=("Segoe UI", 10, "bold"), anchor="w", justify="left",
                 wraplength=420).pack(fill="x", padx=8, pady=(6, 2))

        progress = min(q["progress"], q["target_value"])
        pct = int(100 * progress / q["target_value"]) if q["target_value"] else 0
        bar_frame = tk.Frame(row)
        bar_frame.pack(fill="x", padx=8)
        bar_bg = tk.Frame(bar_frame, bg="#333", height=8)
        bar_bg.pack(fill="x")
        bar_fg = tk.Frame(bar_bg, bg="#43b581", height=8, width=max(1, pct * 4))
        bar_fg.place(x=0, y=0, relheight=1)

        info_frame = tk.Frame(row)
        info_frame.pack(fill="x", padx=8, pady=(4, 8))
        tk.Label(info_frame, text=f"{progress:g}/{q['target_value']:g}   Награда: {q['reward_coins']:g} 🪙",
                 fg="#888").pack(side="left")

        if q["claimed"]:
            tk.Label(info_frame, text="✅ Получено", fg="#43b581").pack(side="right")
        elif q["completed"]:
            tk.Button(info_frame, text="Забрать награду", bg="#43b581", fg="white",
                      command=lambda qid=q["id"]: self._claim(qid)).pack(side="right")
        else:
            tk.Label(info_frame, text="В процессе", fg="#888").pack(side="right")

    def _claim(self, quest_id):
        status, data = self.api.claim_quest(quest_id)
        if status == 200:
            messagebox.showinfo("Задание выполнено", f"Получено {data['reward_coins']:g} монет!")
            self.api.coins = data.get("total_coins", self.api.coins)
        else:
            messagebox.showerror("Ошибка", data.get("error", "Не удалось получить награду"))
        self.refresh()


class StatsManager:

    def __init__(self):
        self.stats = {
            "total_launches": 0,
            "total_play_time_minutes": 0,
            "versions_launched": {},
            "last_launch": None,
        }

    def record_launch(self, version):
        self.stats["total_launches"] += 1
        self.stats["last_launch"] = datetime.now().isoformat()
        self.stats["versions_launched"][version] = \
            self.stats["versions_launched"].get(version, 0) + 1

    def add_play_time(self, minutes):
        self.stats["total_play_time_minutes"] += minutes

    def adopt_server_totals(self, total_launches, total_play_minutes):
        if total_launches is not None:
            self.stats["total_launches"] = max(self.stats["total_launches"], total_launches)
        if total_play_minutes is not None:
            self.stats["total_play_time_minutes"] = max(
                self.stats["total_play_time_minutes"], total_play_minutes)

    def get_summary(self):
        launches = self.stats.get("total_launches", 0)
        minutes = self.stats.get("total_play_time_minutes", 0)
        minutes = int(round(minutes))
        hours = minutes // 60
        mins = minutes % 60
        last = self.stats.get("last_launch", "Никогда")
        if last and last != "Никогда":
            try:
                last = datetime.fromisoformat(last).strftime("%d.%m.%Y %H:%M")
            except Exception:
                pass
        return f"Запусков: {launches} | Время: {hours}ч {mins}м | Последний: {last}"


def _f(facts, key, default=0):
    return facts.get(key, default)


ACHIEVEMENTS = [
    {"id": "launch_1", "icon": "🎮", "title": "Первый запуск",
     "desc": "Запустите Minecraft через лаунчер в первый раз",
     "check": lambda f: _f(f, "total_launches") >= 1},
    {"id": "launch_10", "icon": "🕹️", "title": "Разогрелся",
     "desc": "Запустите игру 10 раз", "check": lambda f: _f(f, "total_launches") >= 10},
    {"id": "launch_50", "icon": "🔥", "title": "Завсегдатай",
     "desc": "Запустите игру 50 раз", "check": lambda f: _f(f, "total_launches") >= 50},
    {"id": "launch_200", "icon": "🏰", "title": "Домосед",
     "desc": "Запустите игру 200 раз", "check": lambda f: _f(f, "total_launches") >= 200},
    {"id": "launch_1000", "icon": "👑", "title": "Легенда лаунчера",
     "desc": "Запустите игру 1000 раз", "check": lambda f: _f(f, "total_launches") >= 1000},

    {"id": "time_30m", "icon": "⏱️", "title": "Полчаса как один миг",
     "desc": "Наиграйте суммарно 30 минут", "check": lambda f: _f(f, "total_play_minutes") >= 30},
    {"id": "time_5h", "icon": "⏳", "title": "Погружение",
     "desc": "Наиграйте суммарно 5 часов", "check": lambda f: _f(f, "total_play_minutes") >= 300},
    {"id": "time_10h", "icon": "🕰️", "title": "Втянулся",
     "desc": "Наиграйте суммарно 10 часов", "check": lambda f: _f(f, "total_play_minutes") >= 600},
    {"id": "time_24h", "icon": "🌍", "title": "Целые сутки",
     "desc": "Наиграйте суммарно 24 часа", "check": lambda f: _f(f, "total_play_minutes") >= 1440},
    {"id": "time_100h", "icon": "🏆", "title": "Ветеран",
     "desc": "Наиграйте суммарно 100 часов", "check": lambda f: _f(f, "total_play_minutes") >= 6000},
    {"id": "time_500h", "icon": "🌟", "title": "Легенда Minecraft",
     "desc": "Наиграйте суммарно 500 часов", "check": lambda f: _f(f, "total_play_minutes") >= 30000},

    {"id": "versions_installed_1", "icon": "📦", "title": "Первая установка",
     "desc": "Установите свою первую версию Minecraft",
     "check": lambda f: _f(f, "versions_installed") >= 1},
    {"id": "versions_installed_5", "icon": "📦", "title": "Коллекционер версий",
     "desc": "Установите 5 разных версий Minecraft",
     "check": lambda f: _f(f, "versions_installed") >= 5},
    {"id": "versions_installed_20", "icon": "🗃️", "title": "Архив версий",
     "desc": "Установите 20 разных версий Minecraft",
     "check": lambda f: _f(f, "versions_installed") >= 20},
    {"id": "distinct_launched_3", "icon": "🔀", "title": "Разнообразие",
     "desc": "Запустите 3 разные версии Minecraft",
     "check": lambda f: _f(f, "distinct_versions_launched") >= 3},
    {"id": "distinct_launched_10", "icon": "🎲", "title": "Испытатель версий",
     "desc": "Запустите 10 разных версий Minecraft",
     "check": lambda f: _f(f, "distinct_versions_launched") >= 10},
    {"id": "same_version_20", "icon": "💎", "title": "Верность выбору",
     "desc": "Запустите одну и ту же версию 20 раз",
     "check": lambda f: _f(f, "max_single_version_launches") >= 20},
    {"id": "legacy_launch", "icon": "🦕", "title": "Археолог",
     "desc": "Запустите очень старую версию Minecraft (alpha/beta/classic)",
     "check": lambda f: _f(f, "legacy_launches") >= 1},
    {"id": "prerelease_launch", "icon": "🧪", "title": "Тестировщик",
     "desc": "Запустите снапшот, pre-release или RC версию",
     "check": lambda f: _f(f, "prerelease_launches") >= 1},

    {"id": "forge_install", "icon": "⚒️", "title": "Модостроитель",
     "desc": "Установите Forge", "check": lambda f: _f(f, "forge_installs") >= 1},
    {"id": "fabric_install", "icon": "🧵", "title": "Фабрикант",
     "desc": "Установите Fabric", "check": lambda f: _f(f, "fabric_installs") >= 1},
    {"id": "both_loaders", "icon": "🛠️", "title": "Универсал",
     "desc": "Установите и Forge, и Fabric",
     "check": lambda f: _f(f, "forge_installs") >= 1 and _f(f, "fabric_installs") >= 1},
    {"id": "mods_1", "icon": "🧩", "title": "Первый мод",
     "desc": "Добавьте свой первый мод", "check": lambda f: _f(f, "mods_installed") >= 1},
    {"id": "mods_10", "icon": "🧰", "title": "Моддер",
     "desc": "Добавьте 10 модов", "check": lambda f: _f(f, "mods_installed") >= 10},
    {"id": "mods_50", "icon": "🏗️", "title": "Сборкодел",
     "desc": "Добавьте 50 модов", "check": lambda f: _f(f, "mods_installed") >= 50},

    {"id": "resourcepack_1", "icon": "🎨", "title": "Художник",
     "desc": "Добавьте свой первый ресурспак",
     "check": lambda f: _f(f, "resourcepacks_installed") >= 1},
    {"id": "resourcepack_5", "icon": "🖼️", "title": "Куратор текстур",
     "desc": "Добавьте 5 ресурспаков", "check": lambda f: _f(f, "resourcepacks_installed") >= 5},
    {"id": "skin_change_1", "icon": "👕", "title": "Новый образ",
     "desc": "Смените скин персонажа", "check": lambda f: _f(f, "skins_changed") >= 1},
    {"id": "skin_change_5", "icon": "🧥", "title": "Модник",
     "desc": "Смените скин 5 раз", "check": lambda f: _f(f, "skins_changed") >= 5},

    {"id": "accounts_2", "icon": "👥", "title": "Не один",
     "desc": "Добавьте второй аккаунт", "check": lambda f: _f(f, "accounts_count") >= 2},
    {"id": "accounts_5", "icon": "👨‍👩‍👧‍👦", "title": "Целая семья",
     "desc": "Добавьте 5 аккаунтов", "check": lambda f: _f(f, "accounts_count") >= 5},
    {"id": "server_add_1", "icon": "🌐", "title": "В сеть",
     "desc": "Добавьте свой первый сервер", "check": lambda f: _f(f, "servers_added_total") >= 1},
    {"id": "server_add_10", "icon": "🗺️", "title": "Коллекционер серверов",
     "desc": "Добавьте 10 серверов", "check": lambda f: _f(f, "servers_added_total") >= 10},
    {"id": "server_ping", "icon": "📶", "title": "Разведчик",
     "desc": "Проверьте пинг сервера", "check": lambda f: _f(f, "server_pings") >= 1},

    {"id": "p2p_host_1", "icon": "🏠", "title": "Гостеприимный хозяин",
     "desc": "Создайте P2P-комнату для друзей",
     "check": lambda f: _f(f, "p2p_host_count") >= 1},
    {"id": "p2p_host_10", "icon": "🏟️", "title": "Хост со стажем",
     "desc": "Создайте P2P-комнату 10 раз", "check": lambda f: _f(f, "p2p_host_count") >= 10},
    {"id": "p2p_join_1", "icon": "🚪", "title": "В гости",
     "desc": "Подключитесь к другу через P2P", "check": lambda f: _f(f, "p2p_join_count") >= 1},

    {"id": "logs_cleaned", "icon": "🧹", "title": "Чистюля",
     "desc": "Очистите старые логи", "check": lambda f: _f(f, "logs_cleaned") >= 1},
    {"id": "smart_cleanup_1", "icon": "✨", "title": "Генеральная уборка",
     "desc": "Запустите умную очистку временных файлов",
     "check": lambda f: _f(f, "smart_cleanups") >= 1},
    {"id": "smart_cleanup_10", "icon": "🧼", "title": "Порядок превыше всего",
     "desc": "Запустите умную очистку 10 раз", "check": lambda f: _f(f, "smart_cleanups") >= 10},
    {"id": "orphan_cleanup", "icon": "🗑️", "title": "Ничего лишнего",
     "desc": "Очистите неиспользуемые ассеты",
     "check": lambda f: _f(f, "orphan_asset_cleanups") >= 1},
    {"id": "export_profile", "icon": "💾", "title": "Архивариус",
     "desc": "Экспортируйте профиль лаунчера", "check": lambda f: _f(f, "exports") >= 1},
    {"id": "import_profile", "icon": "📥", "title": "Реставратор",
     "desc": "Импортируйте профиль лаунчера", "check": lambda f: _f(f, "imports") >= 1},
    {"id": "theme_change", "icon": "🎭", "title": "Стиль важен",
     "desc": "Смените тему лаунчера", "check": lambda f: _f(f, "theme_changes") >= 1},
    {"id": "java_custom", "icon": "☕", "title": "Технарь",
     "desc": "Укажите свою Java вручную в настройках",
     "check": lambda f: _f(f, "java_path_customized") >= 1},
    {"id": "memory_custom", "icon": "🧠", "title": "Оптимизатор",
     "desc": "Измените выделение ОЗУ по умолчанию",
     "check": lambda f: _f(f, "memory_customized") >= 1},
    {"id": "dir_custom", "icon": "📁", "title": "Своя папка",
     "desc": "Смените папку установки игры",
     "check": lambda f: _f(f, "dir_customized") >= 1},
    {"id": "search_used", "icon": "🔎", "title": "Следопыт",
     "desc": "Воспользуйтесь поиском версий", "check": lambda f: _f(f, "search_used") >= 1},
    {"id": "hotkey_used", "icon": "⌨️", "title": "Скорострел",
     "desc": "Воспользуйтесь горячей клавишей лаунчера",
     "check": lambda f: _f(f, "hotkey_used") >= 1},

    {"id": "night_owl_1", "icon": "🌙", "title": "Полуночник",
     "desc": "Запустите игру ночью (00:00-05:00)",
     "check": lambda f: _f(f, "night_launches") >= 1},
    {"id": "night_owl_10", "icon": "🦉", "title": "Сова",
     "desc": "Запустите игру ночью 10 раз", "check": lambda f: _f(f, "night_launches") >= 10},
    {"id": "weekend_gamer", "icon": "🎉", "title": "Выходной день",
     "desc": "Запустите игру по выходным 5 раз",
     "check": lambda f: _f(f, "weekend_launches") >= 5},
    {"id": "crash_recovery", "icon": "🩹", "title": "Не сдаюсь",
     "desc": "Успешно перезапустите игру после сбоя",
     "check": lambda f: _f(f, "crash_recoveries") >= 1},
    {"id": "discord_online", "icon": "💬", "title": "На связи",
     "desc": "Подключитесь к Discord Rich Presence",
     "check": lambda f: _f(f, "discord_connected") >= 1},

    {"id": "collector_25", "icon": "🥈", "title": "На полпути",
     "desc": "Разблокируйте 25 других ачивок",
     "check": lambda f: _f(f, "unlocked_count") >= 25},
    {"id": "collector_all", "icon": "🥇", "title": "Всё собрал",
     "desc": "Разблокируйте все остальные ачивки",
     "check": lambda f: _f(f, "unlocked_count") >= len(ACHIEVEMENTS) - 1},
]


class AchievementManager:

    def __init__(self, stats_manager, config):
        self.stats = stats_manager
        self.config = config
        self.data = {"unlocked": {}, "counters": {}, "owner": None}
        self._by_id = {a["id"]: a for a in ACHIEVEMENTS}

    def bump(self, counter_name, amount=1):
        self.data["counters"][counter_name] = self.data["counters"].get(counter_name, 0) + amount

    def set_flag(self, flag_name, value=1):
        self.data["counters"][flag_name] = value

    def is_unlocked(self, achievement_id):
        return achievement_id in self.data["unlocked"]

    def clear_all(self):
        self.data["unlocked"] = {}
        self.data["counters"] = {}
        self.data["owner"] = None

    def ensure_owner(self, nickname):
        if not nickname:
            return
        if self.data.get("owner") != nickname:
            self.data["unlocked"] = {}
            self.data["counters"] = {}
            self.data["owner"] = nickname

    def mark_unlocked_silent(self, achievement_id, when=None):
        if achievement_id in self.data["unlocked"]:
            return
        if achievement_id not in self._by_id:
            return
        self.data["unlocked"][achievement_id] = when or datetime.now().isoformat()

    def adopt_server_counters(self, server_counters):
        if not server_counters:
            return
        counters = self.data["counters"]
        for name, value in server_counters.items():
            counters[name] = max(counters.get(name, 0), value)

    def _build_facts(self):
        stats = self.stats.stats
        counters = self.data["counters"]
        versions_launched = stats.get("versions_launched", {}) or {}
        default_dir = os.path.normpath(DEFAULT_CONFIG["minecraft_dir"])
        return {
            "total_launches": stats.get("total_launches", 0),
            "total_play_minutes": stats.get("total_play_time_minutes", 0),
            "distinct_versions_launched": len(versions_launched),
            "max_single_version_launches": max(versions_launched.values(), default=0),
            "accounts_count": len(self.config.get("accounts", [])),
            "unlocked_count": len(self.data["unlocked"]),
            **counters,
        }

    def check_all(self, notify_callback=None):
        facts = self._build_facts()
        newly_unlocked = []
        for ach in ACHIEVEMENTS:
            if ach["id"] in self.data["unlocked"]:
                continue
            try:
                if ach["check"](facts):
                    self.data["unlocked"][ach["id"]] = datetime.now().isoformat()
                    newly_unlocked.append(ach)
            except Exception as e:
                print(f"Ошибка проверки ачивки {ach['id']}: {e}")
        if newly_unlocked:
            facts2 = self._build_facts()
            for ach in ACHIEVEMENTS:
                if ach["id"] in self.data["unlocked"]:
                    continue
                try:
                    if ach["check"](facts2):
                        self.data["unlocked"][ach["id"]] = datetime.now().isoformat()
                        newly_unlocked.append(ach)
                except Exception:
                    pass
        if notify_callback:
            for ach in newly_unlocked:
                notify_callback(ach)
        return newly_unlocked

    def list_all(self):
        result = []
        for ach in ACHIEVEMENTS:
            unlocked_at = self.data["unlocked"].get(ach["id"])
            result.append((ach, unlocked_at is not None, unlocked_at))
        return result


DISCORD_LOG_FILE = "discord_rpc.log"
INSTALL_LOG_FILE = "install_errors.log"


def _log_install(message):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(INSTALL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

_SERVER_CONNECT_RE = re.compile(r"Connecting to\s+([^,]+),\s*(\d+)")

_WORLD_LOAD_RE = re.compile(r'Preparing level "(.+?)"')

_GAMEMODE_CHANGE_RE = re.compile(r"Game mode changed to (\w+)", re.IGNORECASE)

_GAME_TYPE_NAMES = {0: "Выживание", 1: "Творческий", 2: "Приключение", 3: "Наблюдатель"}
_GAME_TYPE_NAMES_EN = {
    "survival": "Выживание", "creative": "Творческий",
    "adventure": "Приключение", "spectator": "Наблюдатель",
}

_SERVER_DISCONNECT_HINT_RE = re.compile(
    r"(Connection reset|Connection lost|Connection timed out|TimeoutException|"
    r"ClosedChannelException|Server returned an invalid ping response|"
    r"Server-Fehler|Internal Exception|Encountered an unexpected exception)",
    re.IGNORECASE,
)


def _nbt_read_string(buf, pos):
    length = struct.unpack_from(">H", buf, pos)[0]
    pos += 2
    s = buf[pos:pos + length].decode("utf-8", errors="replace")
    return s, pos + length


def _nbt_skip_payload(buf, pos, tag_id):
    if tag_id == 1:
        return pos + 1
    if tag_id == 2:
        return pos + 2
    if tag_id == 3:
        return pos + 4
    if tag_id == 4:
        return pos + 8
    if tag_id == 5:
        return pos + 4
    if tag_id == 6:
        return pos + 8
    if tag_id == 7:
        n = struct.unpack_from(">i", buf, pos)[0]
        return pos + 4 + n
    if tag_id == 8:
        n = struct.unpack_from(">H", buf, pos)[0]
        return pos + 2 + n
    if tag_id == 9:
        list_type = buf[pos]
        pos += 1
        n = struct.unpack_from(">i", buf, pos)[0]
        pos += 4
        for _ in range(n):
            pos = _nbt_skip_payload(buf, pos, list_type)
        return pos
    if tag_id == 10:
        while True:
            t = buf[pos]
            pos += 1
            if t == 0:
                break
            _, pos = _nbt_read_string(buf, pos)
            pos = _nbt_skip_payload(buf, pos, t)
        return pos
    if tag_id == 11:
        n = struct.unpack_from(">i", buf, pos)[0]
        return pos + 4 + n * 4
    if tag_id == 12:
        n = struct.unpack_from(">i", buf, pos)[0]
        return pos + 4 + n * 8
    raise ValueError(f"Неизвестный тип NBT-тега: {tag_id}")


def _read_version_json(minecraft_dir, version_id):
    path = os.path.join(minecraft_dir, "versions", version_id, f"{version_id}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _resolve_base_version_id(minecraft_dir, version_id, _seen=None):
    _seen = _seen or set()
    if version_id in _seen:
        return version_id
    _seen.add(version_id)
    data = _read_version_json(minecraft_dir, version_id)
    if not data:
        return version_id
    parent = data.get("inheritsFrom")
    if not parent:
        return version_id
    return _resolve_base_version_id(minecraft_dir, parent, _seen)


def read_level_dat_gametype(minecraft_dir, world_name):
    path = os.path.join(minecraft_dir, "saves", world_name, "level.dat")
    if not os.path.exists(path):
        return None, False
    try:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            buf = gzip.decompress(raw)
        except OSError:
            buf = raw

        pos = 0
        root_tag = buf[pos]
        pos += 1
        if root_tag != 10:
            return None, False
        _, pos = _nbt_read_string(buf, pos)

        game_type = None
        hardcore = False

        def parse_compound(pos, inside_data):
            nonlocal game_type, hardcore
            while True:
                t = buf[pos]
                pos += 1
                if t == 0:
                    break
                name, pos = _nbt_read_string(buf, pos)
                if t == 10:
                    pos = parse_compound(pos, inside_data or name == "Data")
                elif inside_data and t == 3 and name == "GameType":
                    game_type = struct.unpack_from(">i", buf, pos)[0]
                    pos += 4
                elif inside_data and t == 1 and name == "hardcore":
                    hardcore = bool(buf[pos])
                    pos += 1
                else:
                    pos = _nbt_skip_payload(buf, pos, t)
            return pos

        parse_compound(pos, False)
        return game_type, hardcore
    except Exception as e:
        print(f"Не удалось прочитать level.dat для мира '{world_name}': {e}")
        return None, False


def _log_discord(message):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(DISCORD_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class DiscordRPC:

    def __init__(self, client_id=""):
        self.client_id = (client_id or "").strip()
        self.rpc = None
        self.connected = False
        self._lock = threading.Lock()
        self._start_ts = None
        self._session_start_ts = None
        self.last_error = None
        self._version = None
        self._player = None
        self._server = None
        self._world = None
        self._gamemode = None

    def is_available(self):
        return bool(self.client_id)

    def connect(self, retries=3, delay=2):
        if not self.client_id:
            self.last_error = "Client ID не задан в коде (DISCORD_CLIENT_ID)"
            return False
        try:
            from pypresence import Presence
        except ImportError as e:
            self.last_error = f"pypresence не установлен: {e}"
            _log_discord(self.last_error)
            return False

        with self._lock:
            if self.connected:
                return True
            last_exc = None
            for attempt in range(1, retries + 1):
                try:
                    self.rpc = Presence(self.client_id)
                    self.rpc.connect()
                    self.connected = True
                    self.last_error = None
                    if self._session_start_ts is None:
                        self._session_start_ts = time.time()
                    _log_discord("Успешно подключено к Discord IPC.")
                    return True
                except Exception as e:
                    last_exc = e
                    self.rpc = None
                    self.connected = False
                    _log_discord(f"Попытка {attempt}/{retries} не удалась: {e}")
                    if attempt < retries:
                        time.sleep(delay)
            self.last_error = (
                f"Не удалось подключиться к Discord IPC: {last_exc}. "
                f"Убедись, что Discord (десктопное приложение) запущен, "
                f"и что DISCORD_CLIENT_ID в коде верный."
            )
            print("Discord RPC:", self.last_error, flush=True)
        return self.connected

    def set_menu_state(self):
        self._version = None
        self._player = None
        self._server = None
        self._world = None
        self._gamemode = None
        self.update(state="В меню лаунчера", details="Mafin Launcher", start=self._session_start_ts)

    def set_playing_state(self, version, player):
        self._version = version
        self._player = player
        self._server = None
        self._world = None
        self._gamemode = None
        self._push_playing_update()

    def set_server(self, server_name):
        if self._version is None:
            return
        self._server = server_name
        self._world = None
        self._gamemode = None
        self._push_playing_update()

    def set_world(self, world_name, gamemode_label=None):
        if self._version is None:
            return
        self._server = None
        self._world = world_name
        if gamemode_label is not None:
            self._gamemode = gamemode_label
        self._push_playing_update()

    def _push_playing_update(self):
        details = f"Игрок: {self._player}"
        version_part = f"MC {self._version}" if self._version else None
        if self._server:
            state = f"Онлайн: {self._server}"
        elif self._world:
            if self._gamemode:
                state = f"{self._world} [{self._gamemode}]"
            else:
                state = f"{self._world}"
        else:
            state = "Главное меню"
        if version_part:
            state = f"{state} • {version_part}"
        self.update(state=state, details=details, start=self._session_start_ts)

    def update(self, state=None, details=None, start=None,
               large_image="launcher_icon", large_text="Mafin Launcher"):
        if not self.connected or not self.rpc:
            return
        try:
            self.rpc.update(
                state=state, details=details, start=start,
                large_image=large_image, large_text=large_text,
            )
        except Exception as e:
            print("Discord RPC: не удалось обновить статус:", e)
            self.connected = False

    def clear(self):
        if self.connected and self.rpc:
            try:
                self.rpc.clear()
            except Exception:
                pass

    def close(self):
        with self._lock:
            if self.rpc:
                try:
                    self.rpc.close()
                except Exception:
                    pass
            self.rpc = None
            self.connected = False


class UPnPHelper:
    def __init__(self):
        self.control_url = None
        self.service_type = None
        self.external_ip = None

    def discover(self, timeout=3):
        ssdp = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 3\r\n"
            "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
            "\r\n"
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(timeout)
        try:
            sock.sendto(ssdp.encode(), ("239.255.255.250", 1900))
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            sock.close()
            return False
        except Exception:
            sock.close()
            return False
        sock.close()

        location = None
        for line in data.decode(errors="ignore").splitlines():
            if line.upper().startswith("LOCATION:"):
                location = line.split(":", 1)[1].strip()
                break
        if not location:
            return False

        try:
            resp = urlopen(location, timeout=5)
            xml_data = resp.read()
        except Exception:
            return False

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError:
            return False

        parsed = urlparse(location)
        base = f"{parsed.scheme}://{parsed.netloc}"

        for st in (
            "urn:schemas-upnp-org:service:WANIPConnection:1",
            "urn:schemas-upnp-org:service:WANIPConnection:2",
            "urn:schemas-upnp-org:service:WANPPPConnection:1",
        ):
            for svc in root.iter():
                if svc.tag.endswith("}serviceType") and svc.text and svc.text.strip() == st:
                    parent = None
                    for p in root.iter():
                        for child in p:
                            if child is svc:
                                parent = p
                                break
                    if parent is not None:
                        for child in parent:
                            if child.tag.endswith("}controlURL"):
                                self.control_url = base + child.text.strip()
                                self.service_type = st
                                return True
        return False

    def get_external_ip(self):
        if not self.control_url:
            return None
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            f'<u:GetExternalIPAddress xmlns:u="{self.service_type}"/>'
            '</s:Body></s:Envelope>'
        )
        req = Request(self.control_url, data=body.encode(), method="POST")
        req.add_header("Content-Type", 'text/xml; charset="utf-8"')
        req.add_header("SOAPAction", f'"{self.service_type}#GetExternalIPAddress"')
        try:
            resp = urlopen(req, timeout=5)
            root = ET.fromstring(resp.read())
            for el in root.iter():
                if el.tag.endswith("}NewExternalIPAddress"):
                    self.external_ip = el.text.strip()
                    return self.external_ip
        except Exception:
            pass
        return None

    def add_port_mapping(self, external_port, internal_port, protocol="TCP", description="JaleyLauncher"):
        if not self.control_url:
            return False
        local_ip = self._get_local_ip()
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            f'<u:AddPortMapping xmlns:u="{self.service_type}">'
            f"<NewRemoteHost></NewRemoteHost>"
            f"<NewExternalPort>{external_port}</NewExternalPort>"
            f"<NewProtocol>{protocol}</NewProtocol>"
            f"<NewInternalPort>{internal_port}</NewInternalPort>"
            f"<NewInternalClient>{local_ip}</NewInternalClient>"
            f"<NewEnabled>1</NewEnabled>"
            f"<NewPortMappingDescription>{description}</NewPortMappingDescription>"
            f"<NewLeaseDuration>0</NewLeaseDuration>"
            f"</u:AddPortMapping>"
            "</s:Body></s:Envelope>"
        )
        req = Request(self.control_url, data=body.encode(), method="POST")
        req.add_header("Content-Type", 'text/xml; charset="utf-8"')
        req.add_header("SOAPAction", f'"{self.service_type}#AddPortMapping"')
        try:
            urlopen(req, timeout=5)
            return True
        except Exception:
            return False

    def remove_port_mapping(self, external_port, protocol="TCP"):
        if not self.control_url:
            return False
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            f'<u:DeletePortMapping xmlns:u="{self.service_type}">'
            f"<NewRemoteHost></NewRemoteHost>"
            f"<NewExternalPort>{external_port}</NewExternalPort>"
            f"<NewProtocol>{protocol}</NewProtocol>"
            f"</u:DeletePortMapping>"
            "</s:Body></s:Envelope>"
        )
        req = Request(self.control_url, data=body.encode(), method="POST")
        req.add_header("Content-Type", 'text/xml; charset="utf-8"')
        req.add_header("SOAPAction", f'"{self.service_type}#DeletePortMapping"')
        try:
            urlopen(req, timeout=5)
            return True
        except Exception:
            return False

    @staticmethod
    def _get_local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()


class P2PManager:
    def __init__(self, log_callback=None):
        self.log_cb = log_callback or (lambda msg: print(msg))
        self.relay_server = None
        self.relay_threads = []
        self.proxy_server = None
        self.proxy_threads = []
        self.running = False
        self.upnp = UPnPHelper()
        self.mapped_port = None
        self.local_ip = UPnPHelper._get_local_ip()

    def _log(self, msg):
        self.log_cb(msg)

    def get_external_ip(self):
        ip_re = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
            try:
                resp = requests.get(url, timeout=5)
                candidate = resp.text.strip()
                if resp.status_code == 200 and ip_re.match(candidate):
                    return candidate
            except Exception:
                continue
        try:
            return self._get_external_ip_via_stun()
        except Exception:
            return None

    @staticmethod
    def _get_external_ip_via_stun(stun_host="stun.l.google.com", stun_port=19302, timeout=3.0):
        magic_cookie = 0x2112A442
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            transaction_id = os.urandom(12)
            header = struct.pack("!HHI12s", 0x0001, 0, magic_cookie, transaction_id)
            sock.sendto(header, (stun_host, stun_port))
            data, _ = sock.recvfrom(2048)
        except Exception:
            return None
        finally:
            sock.close()

        if len(data) < 20:
            return None
        msg_type, msg_len, cookie, txn_id = struct.unpack("!HHI12s", data[:20])
        if txn_id != transaction_id:
            return None
        body = data[20:20 + msg_len]
        offset = 0
        while offset + 4 <= len(body):
            attr_type, attr_len = struct.unpack("!HH", body[offset:offset + 4])
            attr_val = body[offset + 4: offset + 4 + attr_len]
            if attr_type == 0x0020 and len(attr_val) >= 8:
                family = attr_val[1]
                if family == 0x01:
                    xip_bytes = bytes(b ^ c for b, c in zip(attr_val[4:8], struct.pack("!I", magic_cookie)))
                    return socket.inet_ntoa(xip_bytes)
            elif attr_type == 0x0001 and len(attr_val) >= 8:
                family = attr_val[1]
                if family == 0x01:
                    return socket.inet_ntoa(attr_val[4:8])
            offset += 4 + attr_len + ((4 - attr_len % 4) % 4)
        return None

    def create_room(self, mc_port=25565, external_port=None):
        if self.running:
            self._log("⚠ Комната уже создана")
            return None
        if external_port is None:
            external_port = mc_port
        self.running = True
        self._log("🔍 Поиск UPnP IGD...")
        upnp_ok = False
        ext_ip = None

        if self.upnp.discover():
            ext_ip = self.upnp.get_external_ip()
            if self.upnp.add_port_mapping(external_port, external_port):
                self.mapped_port = external_port
                upnp_ok = True
                self._log(f"✅ UPnP: порт {external_port} проброшен")
            else:
                self._log("⚠ UPnP: не удалось пробросить порт")
        else:
            self._log("⚠ UPnP IGD не найден")

        if not ext_ip:
            ext_ip = self.get_external_ip()

        self.relay_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.relay_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.relay_server.settimeout(1.0)
        try:
            self.relay_server.bind(("0.0.0.0", external_port))
            self.relay_server.listen(5)
        except OSError as e:
            self._log(f"❌ Не удалось занять порт {external_port}: {e}")
            self.running = False
            return None

        self._log(f"🎮 Relay запущен на порту {external_port}")
        display_ip = ext_ip if ext_ip else self.local_ip
        self._log(f"📡 Ваш адрес для друзей: {display_ip}:{external_port}")

        def accept_loop():
            while self.running:
                try:
                    client, addr = self.relay_server.accept()
                    self._log(f"🔗 Подключение от {addr[0]}:{addr[1]}")
                    t = threading.Thread(
                        target=self._relay_client,
                        args=(client, "127.0.0.1", mc_port),
                        daemon=True,
                    )
                    t.start()
                    self.relay_threads.append(t)
                except socket.timeout:
                    continue
                except OSError:
                    break

        t = threading.Thread(target=accept_loop, daemon=True)
        t.start()
        self.relay_threads.append(t)
        address = f"{ext_ip}:{external_port}" if ext_ip else f"{self.local_ip}:{external_port}"
        return {"address": address, "upnp_ok": upnp_ok, "external_port": external_port}

    def _relay_client(self, client_sock, target_host, target_port):
        try:
            target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target.settimeout(5)
            target.connect((target_host, target_port))
        except Exception as e:
            self._log(f"❌ Не удалось подключиться к MC-серверу: {e}")
            try:
                client_sock.close()
            except Exception:
                pass
            return

        def forward(src, dst, label):
            try:
                while self.running:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                try:
                    src.close()
                except Exception:
                    pass
                try:
                    dst.close()
                except Exception:
                    pass
                self._log(f"🔌 {label} отключён")

        threading.Thread(target=forward, args=(client_sock, target, "Клиент"), daemon=True).start()
        threading.Thread(target=forward, args=(target, client_sock, "MC-сервер"), daemon=True).start()

    def join_room(self, remote_address, local_port=0):
        if ":" in remote_address:
            host, port_str = remote_address.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 25565
        else:
            host = remote_address
            port = 25565

        self.running = True
        self.proxy_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.proxy_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.proxy_server.settimeout(1.0)
        try:
            self.proxy_server.bind(("127.0.0.1", local_port))
            self.proxy_server.listen(1)
        except OSError as e:
            self._log(f"❌ Не удалось создать proxy: {e}")
            self.running = False
            return None

        actual_port = self.proxy_server.getsockname()[1]
        self._log(f"🔗 Локальный proxy на 127.0.0.1:{actual_port} -> {host}:{port}")
        self._log(f"🎮 Подключитесь в Minecraft к: 127.0.0.1:{actual_port}")

        def accept_loop():
            while self.running:
                try:
                    client, addr = self.proxy_server.accept()
                    self._log("🎮 Локальный игрок подключился")
                    t = threading.Thread(
                        target=self._relay_client,
                        args=(client, host, port),
                        daemon=True,
                    )
                    t.start()
                    self.proxy_threads.append(t)
                except socket.timeout:
                    continue
                except OSError:
                    break

        t = threading.Thread(target=accept_loop, daemon=True)
        t.start()
        self.proxy_threads.append(t)
        return f"127.0.0.1:{actual_port}"

    def stop(self):
        self.running = False
        if self.relay_server:
            try:
                self.relay_server.close()
            except Exception:
                pass
            self.relay_server = None
        if self.proxy_server:
            try:
                self.proxy_server.close()
            except Exception:
                pass
            self.proxy_server = None
        if self.mapped_port and self.upnp.control_url:
            self.upnp.remove_port_mapping(self.mapped_port)
            self._log(f"🧹 UPnP: порт {self.mapped_port} закрыт")
            self.mapped_port = None
        self.relay_threads.clear()
        self.proxy_threads.clear()
        self._log("⏹ P2P остановлен")


class SplashScreen:
    def __init__(self, parent):
        self.root = tk.Toplevel(parent)
        self._after_id = None
        self.root.overrideredirect(True)
        W, H = 520, 320
        BG = "#1a1d23"
        ACCENT = LAUNCHER_THEMES[DEFAULT_LAUNCHER_THEME]["accent"]
        self.root.configure(bg=BG)

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = (screen_w - W) // 2
        y = (screen_h - H) // 2
        self.root.geometry(f"{W}x{H}+{x}+{y}")

        border = tk.Frame(self.root, bg=ACCENT)
        border.pack(fill="both", expand=True, padx=2, pady=2)
        inner = tk.Frame(border, bg=BG)
        inner.pack(fill="both", expand=True, padx=2, pady=2)

        tk.Label(
            inner, text="⚡ Mafin Launcher", font=("Segoe UI", 24, "bold"),
            fg="white", bg=BG
        ).pack(pady=(35, 0))
        tk.Label(
            inner, text=f"v{APP_VERSION}", font=("Segoe UI", 10),
            fg="#7a8599", bg=BG
        ).pack(pady=(0, 10))

        self.canvas = tk.Canvas(inner, width=70, height=70, bg=BG, highlightthickness=0)
        self.canvas.pack(pady=5)

        self._spinner_color = ACCENT
        self._spinner_bg = BG
        self._spinner_track = "#2d323c"
        self.angle = 0

        self.progress = ttk.Progressbar(inner, orient="horizontal", length=400, mode="determinate")
        self.progress.pack(pady=(15, 8))

        self.status_var = tk.StringVar()
        self.status_var.set("Загрузка...")
        tk.Label(
            inner, textvariable=self.status_var, font=("Segoe UI", 10),
            fg="#7a8599", bg=BG
        ).pack()

        self.animate_spinner()
        self.root.update()

    def animate_spinner(self):
        self.canvas.delete("all")
        cx, cy, r = 35, 35, 24
        width = 4

        self.canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=self._spinner_track, width=width
        )

        extent = 90
        self.canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=self.angle, extent=extent, style="arc",
            outline=self._spinner_color, width=width
        )

        for a in (self.angle, self.angle + extent):
            rad = math.radians(a)
            ex = cx + r * math.cos(rad)
            ey = cy - r * math.sin(rad)
            self.canvas.create_oval(
                ex - width / 2, ey - width / 2, ex + width / 2, ey + width / 2,
                fill=self._spinner_color, outline=""
            )

        now = time.perf_counter()
        elapsed = now - getattr(self, "_last_frame_at", now)
        self._last_frame_at = now
        degrees_per_second = 340
        elapsed = min(elapsed, 0.1)
        self.angle = (self.angle - degrees_per_second * elapsed) % 360
        self._after_id = self.root.after(10, self.animate_spinner)

    @staticmethod
    def _blend_color(c1, c2, t):
        c1 = c1.lstrip("#")
        c2 = c2.lstrip("#")
        r1, g1, b1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
        r2, g2, b2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
        r = int(r2 + (r1 - r2) * t)
        g = int(g2 + (g1 - g2) * t)
        b = int(b2 + (b1 - b2) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def pump(self, seconds):
        steps = max(1, int(seconds / 0.02))
        for _ in range(steps):
            try:
                self.root.update()
            except tk.TclError:
                break
            time.sleep(0.02)

    def update_status(self, text, progress_val=None):
        self.status_var.set(text)
        if progress_val is not None:
            self.progress["value"] = progress_val
        try:
            self.root.update()
        except tk.TclError:
            pass

    def close(self):
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            self.root.destroy()
        except Exception:
            pass


class LauncherCore:
    def __init__(self, config):
        self.config = config
        self.minecraft_dir = config.get("minecraft_dir", os.path.join(os.getcwd(), GAME_DIR_NAME))
        self.java_path = config.get("java_path", "java")
        self.memory = config.get("memory_mb", 2048)
        self.pack_java = None
        self.pack_memory = None
        self.jvm_preset = config.get("jvm_preset", "default")
        self.accounts = config.get("accounts", [])
        self.selected_account = config.get("selected_account", 0)

    def effective_java(self):
        return self.pack_java or self.java_path

    def effective_memory(self):
        return int(self.pack_memory or self.memory)

    def get_java_version(self):
        try:
            proc = subprocess.run([self.effective_java(), "-version"], capture_output=True, text=True)
            return proc.stderr.splitlines()[0] if proc.stderr else "Java not found"
        except Exception:
            return None

    def check_java(self):
        return self.get_java_version() is not None

    def _resolve_base_version_id(self, version):
        seen = set()
        current = version
        while current and current not in seen:
            seen.add(current)
            json_path = os.path.join(self.minecraft_dir, "versions", current, f"{current}.json")
            if not os.path.isfile(json_path):
                break
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                break
            parent = data.get("inheritsFrom")
            if not parent:
                break
            current = parent
        return current

    def resolve_java_executable(self, version, status_callback=None):
        if self.effective_java() and self.effective_java() != "java":
            return self.effective_java()

        base_version = self._resolve_base_version_id(version)

        try:
            info = mll_runtime.get_version_runtime_information(base_version, self.minecraft_dir)
        except Exception as e:
            print(f"Не удалось определить нужный JVM-рантайм для {base_version}: {e}")
            info = None

        if not info:
            return "java"

        runtime_name = info["name"]
        try:
            exe = mll_runtime.get_executable_path(runtime_name, self.minecraft_dir)
            if not exe or not os.path.exists(exe):
                if status_callback:
                    status_callback(f"☕ Скачивание Java {info.get('javaMajorVersion', '')} ({runtime_name})...")
                mll_runtime.install_jvm_runtime(runtime_name, self.minecraft_dir)
                exe = mll_runtime.get_executable_path(runtime_name, self.minecraft_dir)
            if exe and os.path.exists(exe):
                return exe
        except Exception as e:
            print(f"Не удалось скачать/найти JVM-рантайм {runtime_name}: {e}")

        return "java"

    def get_versions(self, include_alpha_beta=False):
        try:
            versions = get_version_list()
            allowed_types = {"release", "snapshot"}
            if include_alpha_beta:
                allowed_types |= {"old_beta", "old_alpha"}
            vlist = [v["id"] for v in versions if v["type"] in allowed_types]
            return vlist
        except Exception as e:
            print("Ошибка получения версий:", e)
            return ["1.21.4", "1.20.4", "1.19.2", "1.18.2", "1.16.5", "1.12.2", "1.7.10"]

    def get_installed_versions(self):
        versions_dir = os.path.join(self.minecraft_dir, "versions")
        result = []
        if not os.path.isdir(versions_dir):
            return result
        for name in sorted(os.listdir(versions_dir)):
            path = os.path.join(versions_dir, name)
            if not os.path.isdir(path):
                continue
            size = 0
            for root, _, files in os.walk(path):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            result.append({"id": name, "size_mb": round(size / (1024 * 1024), 1)})
        return result

    def get_total_game_size(self):
        total = 0
        if not os.path.isdir(self.minecraft_dir):
            return 0
        for root, _, files in os.walk(self.minecraft_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def _get_used_libraries(self, exclude_version=None):
        used = set()
        versions_dir = os.path.join(self.minecraft_dir, "versions")
        if not os.path.isdir(versions_dir):
            return used
        for name in os.listdir(versions_dir):
            if name == exclude_version:
                continue
            json_path = os.path.join(versions_dir, name, f"{name}.json")
            if not os.path.isfile(json_path):
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for lib in data.get("libraries", []):
                    downloads = lib.get("downloads", {})
                    artifact = downloads.get("artifact", {})
                    if "path" in artifact:
                        used.add(artifact["path"])
                    for cls in downloads.get("classifiers", {}).values():
                        if "path" in cls:
                            used.add(cls["path"])
            except Exception:
                pass
        return used

    def _get_used_asset_index(self, exclude_version=None):
        used = set()
        versions_dir = os.path.join(self.minecraft_dir, "versions")
        if not os.path.isdir(versions_dir):
            return used
        for name in os.listdir(versions_dir):
            if name == exclude_version:
                continue
            json_path = os.path.join(versions_dir, name, f"{name}.json")
            if not os.path.isfile(json_path):
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                idx = data.get("assetIndex", {}).get("id")
                if idx:
                    used.add(idx)
            except Exception:
                pass
        return used

    def _get_used_asset_hashes(self):
        used = set()
        indexes_dir = os.path.join(self.minecraft_dir, "assets", "indexes")
        if not os.path.isdir(indexes_dir):
            return used
        for fname in os.listdir(indexes_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(indexes_dir, fname), "r", encoding="utf-8") as f:
                    data = json.load(f)
                for obj in data.get("objects", {}).values():
                    h = obj.get("hash", "")
                    if h:
                        used.add(h)
            except Exception:
                pass
        return used

    def build_delete_plan(self, version_id):
        versions_dir = os.path.abspath(os.path.join(self.minecraft_dir, "versions"))
        target = os.path.abspath(os.path.join(versions_dir, version_id))
        if os.path.commonpath([versions_dir, target]) != versions_dir:
            raise Exception("Некорректное имя версии")
        if not os.path.isdir(target):
            raise Exception(f"Версия {version_id} не найдена на диске")

        used_libs = self._get_used_libraries(exclude_version=version_id)
        used_indexes = self._get_used_asset_index(exclude_version=version_id)
        version_json = os.path.join(target, f"{version_id}.json")
        libs_to_check = []
        asset_index_id = None

        if os.path.isfile(version_json):
            try:
                with open(version_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for lib in data.get("libraries", []):
                    downloads = lib.get("downloads", {})
                    artifact = downloads.get("artifact", {})
                    if "path" in artifact:
                        libs_to_check.append(artifact["path"])
                    for cls in downloads.get("classifiers", {}).values():
                        if "path" in cls:
                            libs_to_check.append(cls["path"])
                asset_index_id = data.get("assetIndex", {}).get("id")
            except Exception:
                pass

        idx_file = None
        if asset_index_id and asset_index_id not in used_indexes:
            candidate = os.path.join(self.minecraft_dir, "assets", "indexes", f"{asset_index_id}.json")
            if os.path.isfile(candidate):
                idx_file = candidate

        libs_dir = os.path.join(self.minecraft_dir, "libraries")
        lib_files = []
        for lib_path in libs_to_check:
            if lib_path not in used_libs:
                full = os.path.join(libs_dir, lib_path)
                if os.path.isfile(full):
                    lib_files.append(full)

        used_hashes = self._get_used_asset_hashes()
        objects_dir = os.path.join(self.minecraft_dir, "assets", "objects")
        asset_files = []
        if os.path.isdir(objects_dir):
            for d in os.listdir(objects_dir):
                sub = os.path.join(objects_dir, d)
                if not os.path.isdir(sub) or len(d) != 2:
                    continue
                for fname in os.listdir(sub):
                    if fname not in used_hashes:
                        fpath = os.path.join(sub, fname)
                        if os.path.isfile(fpath):
                            asset_files.append(fpath)

        return {
            "version_id": version_id,
            "target": target,
            "idx_file": idx_file,
            "lib_files": lib_files,
            "libs_dir": libs_dir,
            "asset_files": asset_files,
            "objects_dir": objects_dir,
        }

    def execute_delete_plan(self, plan):
        shutil.rmtree(plan["target"])

        if plan["idx_file"] and os.path.isfile(plan["idx_file"]):
            os.remove(plan["idx_file"])

        libs_dir = plan["libs_dir"]
        removed_libs = 0
        for full in plan["lib_files"]:
            if os.path.isfile(full):
                os.remove(full)
                removed_libs += 1
                parent = os.path.dirname(full)
                while parent != libs_dir:
                    try:
                        if os.path.isdir(parent) and not os.listdir(parent):
                            os.rmdir(parent)
                        else:
                            break
                    except OSError:
                        break
                    parent = os.path.dirname(parent)

        removed_assets = 0
        for fpath in plan["asset_files"]:
            if os.path.isfile(fpath):
                os.remove(fpath)
                removed_assets += 1
        objects_dir = plan["objects_dir"]
        if os.path.isdir(objects_dir):
            for d in os.listdir(objects_dir):
                sub = os.path.join(objects_dir, d)
                if os.path.isdir(sub):
                    try:
                        if not os.listdir(sub):
                            os.rmdir(sub)
                    except OSError:
                        pass

        return {"removed_libs": removed_libs, "removed_assets": removed_assets}

    def delete_version(self, version_id):
        plan = self.build_delete_plan(version_id)
        return self.execute_delete_plan(plan)

    game_dir = None

    def game_path(self):
        return self.game_dir or self.minecraft_dir

    def get_forge_versions(self, mc_version):
        try:
            all_versions = mll_forge.list_forge_versions()
        except Exception as e:
            print("Не удалось получить список версий Forge:", e)
            return []
        prefix = mc_version + "-"
        matched = [v for v in all_versions if v.startswith(prefix)]
        matched.reverse()
        return matched

    def install_forge(self, forge_version, callback=None):
        mc_version, sep, loader_version = forge_version.partition("-")
        if not sep or not loader_version:
            raise Exception(f"Некорректный формат версии Forge: {forge_version}")

        state = {"max": 0, "status": ""}

        def set_status(text):
            state["status"] = text
            if callback:
                callback(0, 0, text)

        def set_progress(value):
            if callback:
                callback(value, state["max"], state["status"])

        def set_max(value):
            state["max"] = value

        java = self.resolve_java_executable(mc_version, status_callback=set_status)
        forge_loader = mll_modloader.get_mod_loader("forge")
        _log_install(f"Forge {forge_version}: установка через mod_loader API, java={java}")
        try:
            installed_version = forge_loader.install(
                mc_version, self.minecraft_dir,
                loader_version=loader_version,
                callback={
                    "setStatus": set_status,
                    "setProgress": set_progress,
                    "setMax": set_max,
                },
                java=java,
            )
        except Exception as e:
            _log_install(f"Forge {forge_version}: ОШИБКА установки (java={java}): {e}\n{traceback.format_exc()}")
            error_text = str(e)
            if "UnsupportedClassVersionError" in error_text or "has been compiled by a more recent version" in error_text:
                raise Exception(
                    f"Установщик Forge не смог запуститься на используемой Java ({java}) - "
                    f"версия Java слишком старая для этого установщика.\n"
                    f"Подробности: {INSTALL_LOG_FILE}"
                )
            raise Exception(
                f"Не удалось установить Forge {forge_version}: {e}\n"
                f"Подробности в файле {INSTALL_LOG_FILE} рядом с лаунчером."
            )
        _log_install(f"Forge {forge_version}: установлен успешно как {installed_version}.")

    def get_forge_launch_version(self, forge_version):
        mc_version, sep, loader_version = forge_version.partition("-")
        if not sep:
            raise Exception(f"Некорректный формат версии Forge: {forge_version}")
        return mll_modloader.get_mod_loader("forge").get_installed_version(mc_version, loader_version)

    def get_fabric_loader_versions(self, mc_version):
        try:
            url = f"https://meta.fabricmc.net/v2/versions/loader/{mc_version}"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return [v["loader"]["version"] for v in data if v.get("loader")]
        except Exception as e:
            print("Ошибка получения версий Fabric:", e)
            return []

    def install_fabric(self, mc_version, loader_version, callback=None):
        state = {"max": 0, "status": ""}

        def set_status(text):
            state["status"] = text
            if callback:
                callback(0, 0, text)

        def set_progress(value):
            if callback:
                callback(value, state["max"], state["status"])

        def set_max(value):
            state["max"] = value

        java = self.resolve_java_executable(mc_version, status_callback=set_status)
        fabric_loader = mll_modloader.get_mod_loader("fabric")
        _log_install(f"Fabric {mc_version}/{loader_version}: установка через mod_loader API, java={java}")
        try:
            installed_version = fabric_loader.install(
                mc_version, self.minecraft_dir,
                loader_version=loader_version,
                callback={
                    "setStatus": set_status,
                    "setProgress": set_progress,
                    "setMax": set_max,
                },
                java=java,
            )
        except Exception as e:
            _log_install(f"Fabric {mc_version}/{loader_version}: ОШИБКА установки (java={java}): {e}\n{traceback.format_exc()}")
            error_text = str(e)
            if "UnsupportedClassVersionError" in error_text or "has been compiled by a more recent version" in error_text:
                raise Exception(
                    f"Установщик Fabric не смог запуститься на используемой Java ({java}) - "
                    f"версия Java слишком старая для этого установщика.\n"
                    f"Подробности: {INSTALL_LOG_FILE}"
                )
            raise Exception(
                f"Не удалось установить Fabric {mc_version}/{loader_version}: {e}\n"
                f"Подробности в файле {INSTALL_LOG_FILE} рядом с лаунчером."
            )
        _log_install(f"Fabric {mc_version}/{loader_version}: установлен успешно как {installed_version}.")
        if callback:
            callback(100, 100, "Fabric установлен")

    def get_fabric_launch_version(self, mc_version, loader_version):
        return mll_modloader.get_mod_loader("fabric").get_installed_version(mc_version, loader_version)

    @staticmethod
    def _version_sort_key(v):
        return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)

    QUILT_META = "https://meta.quiltmc.org/v3/versions/loader"

    def get_quilt_versions(self, mc_version):
        r = requests.get(f"{self.QUILT_META}/{requests.utils.quote(mc_version, safe='')}", timeout=(4, 8))
        r.raise_for_status()
        versions = [e["loader"]["version"] for e in r.json() if e.get("loader", {}).get("version")]
        versions = list(dict.fromkeys(versions))
        stable = [v for v in versions if not re.search(r"alpha|beta|rc|pre|snapshot", v, re.I)]
        return stable + [v for v in versions if v not in stable]

    def install_quilt(self, mc_version, loader_version, callback=None):
        state = {"max": 0, "status": ""}

        def set_status(text):
            state["status"] = text
            if callback:
                callback(0, 0, text)

        def set_progress(value):
            if callback:
                callback(value, state["max"], state["status"])

        def set_max(value):
            state["max"] = value

        quote = lambda x: requests.utils.quote(x, safe="")
        url = f"{self.QUILT_META}/{quote(mc_version)}/{quote(loader_version)}/profile/json"
        set_status(f"Получаю профиль Quilt {loader_version}...")
        _log_install(f"Quilt {mc_version}/{loader_version}: профиль {url}")
        r = requests.get(url, timeout=30)
        if r.status_code == 404:
            raise Exception(f"Quilt {loader_version} не поддерживает Minecraft {mc_version}")
        r.raise_for_status()
        profile = r.json()
        version_id = profile.get("id") or f"quilt-loader-{loader_version}-{mc_version}"
        vdir = os.path.join(self.minecraft_dir, "versions", version_id)
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, f"{version_id}.json"), "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        set_status("Скачиваю библиотеки Quilt и Minecraft...")
        install_minecraft_version(version_id, self.minecraft_dir,
                                  callback={"setStatus": set_status, "setProgress": set_progress,
                                            "setMax": set_max})
        self.ensure_inherited_client_jar(version_id)
        _log_install(f"Quilt {mc_version}/{loader_version}: установлен как {version_id}")
        return version_id

    def get_loader_versions_generic(self, loader_id, mc_version):
        if loader_id == "quilt":
            try:
                return self.get_quilt_versions(mc_version)
            except Exception as e:
                print(f"Ошибка получения версий Quilt: {e}")
                return []
        try:
            if loader_id not in mll_modloader.list_mod_loader():
                print(f"{loader_id}: не поддерживается установленной minecraft-launcher-lib (обновите: pip install -U minecraft-launcher-lib)")
                return []
            loader = mll_modloader.get_mod_loader(loader_id)
            try:
                versions = loader.get_loader_versions(mc_version, False)
            except TypeError:
                versions = loader.get_loader_versions(mc_version)
            versions = list(dict.fromkeys(versions))
        except Exception as e:
            print(f"Ошибка получения версий {loader_id}: {e}")
            return []
        return sorted(versions, key=self._version_sort_key, reverse=True)

    def install_loader_generic(self, loader_id, title, mc_version, loader_version, callback=None):
        state = {"max": 0, "status": ""}

        def set_status(text):
            state["status"] = text
            if callback:
                callback(0, 0, text)

        def set_progress(value):
            if callback:
                callback(value, state["max"], state["status"])

        def set_max(value):
            state["max"] = value

        if loader_id == "quilt":
            try:
                self.install_quilt(mc_version, loader_version, callback)
            except Exception as e:
                _log_install(f"Quilt {mc_version}/{loader_version}: ОШИБКА: {e}\n{traceback.format_exc()}")
                raise Exception(f"Не удалось установить Quilt {mc_version}/{loader_version}: {e}\n"
                                f"Подробности в файле {INSTALL_LOG_FILE} рядом с лаунчером.")
            if callback:
                callback(100, 100, "Quilt установлен")
            return
        java = self.resolve_java_executable(mc_version, status_callback=set_status)
        loader = mll_modloader.get_mod_loader(loader_id)
        _log_install(f"{title} {mc_version}/{loader_version}: установка через mod_loader API, java={java}")
        try:
            installed_version = loader.install(
                mc_version, self.minecraft_dir,
                loader_version=loader_version,
                callback={"setStatus": set_status, "setProgress": set_progress, "setMax": set_max},
                java=java,
            )
        except Exception as e:
            _log_install(f"{title} {mc_version}/{loader_version}: ОШИБКА установки (java={java}): {e}\n{traceback.format_exc()}")
            raise Exception(
                f"Не удалось установить {title} {mc_version}/{loader_version}: {e}\n"
                f"Подробности в файле {INSTALL_LOG_FILE} рядом с лаунчером."
            )
        _log_install(f"{title} {mc_version}/{loader_version}: установлен успешно как {installed_version}.")
        if callback:
            callback(100, 100, f"{title} установлен")

    def ensure_inherited_client_jar(self, version_id):
        if not version_id.startswith(("quilt-loader-", "fabric-loader-")):
            return
        base = self._resolve_base_version_id(version_id)
        if base == version_id:
            return
        versions_dir = os.path.join(self.minecraft_dir, "versions")
        base_jar = os.path.join(versions_dir, base, f"{base}.jar")
        child_jar = os.path.join(versions_dir, version_id, f"{version_id}.jar")
        if not os.path.isfile(base_jar) or os.path.getsize(base_jar) < 1024:
            return
        if os.path.isfile(child_jar) and os.path.getsize(child_jar) == os.path.getsize(base_jar):
            return
        os.makedirs(os.path.dirname(child_jar), exist_ok=True)
        shutil.copyfile(base_jar, child_jar)

    def get_loader_launch_version(self, loader_id, mc_version, loader_version):
        if loader_id == "quilt":
            return f"quilt-loader-{loader_version}-{mc_version}"
        return mll_modloader.get_mod_loader(loader_id).get_installed_version(mc_version, loader_version)

    def install_version(self, version, callback=None):
        state = {"max": 0, "status": ""}

        def set_status(text):
            state["status"] = text
            if callback:
                callback(0, 0, text)

        def set_progress(value):
            if callback:
                callback(value, state["max"], state["status"])

        def set_max(value):
            state["max"] = value

        install_minecraft_version(version, self.minecraft_dir, callback={
            "setStatus": set_status,
            "setProgress": set_progress,
            "setMax": set_max,
        })
        try:
            self.smart_cleanup()
        except Exception as e:
            print(f"Умная очистка после установки версии не удалась: {e}")

    def smart_cleanup(self, status_callback=None):
        freed = 0
        removed = 0

        def _dir_size(path):
            total = 0
            for dp, _dn, files in os.walk(path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        pass
            return total

        def _remove_path(path):
            nonlocal freed, removed
            try:
                if os.path.isdir(path):
                    size = _dir_size(path)
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    size = os.path.getsize(path)
                    os.remove(path)
                else:
                    return
                freed += size
                removed += 1
            except Exception as e:
                print(f"Умная очистка: не удалось удалить {path}: {e}")

        if status_callback:
            status_callback("🧹 Поиск временных файлов установки...")

        for name in ("forge-installer.jar", "fabric-installer.jar", "neoforge-installer.jar", "quilt-installer.jar"):
            p = os.path.join(self.minecraft_dir, name)
            if os.path.exists(p):
                _remove_path(p)

        versions_dir = os.path.join(self.minecraft_dir, "versions")
        if os.path.isdir(versions_dir):
            for entry in os.listdir(versions_dir):
                version_dir = os.path.join(versions_dir, entry)
                if not os.path.isdir(version_dir):
                    continue
                natives_dir = os.path.join(version_dir, "natives")
                json_path = os.path.join(version_dir, f"{entry}.json")
                if os.path.isdir(natives_dir) and not os.path.exists(json_path):
                    _remove_path(natives_dir)

        try:
            sys_temp = tempfile.gettempdir()
            for entry in os.listdir(sys_temp):
                if entry.startswith("minecraft-launcher-lib-"):
                    _remove_path(os.path.join(sys_temp, entry))
        except Exception as e:
            print(f"Умная очистка: не удалось проверить системный temp: {e}")

        if status_callback:
            status_callback(f"🧹 Очистка завершена, удалено объектов: {removed}" if removed else "🧹 Нечего чистить")

        return freed, removed

    def launch(self, version, player_name, extra_args=None, callback=None, status_callback=None):
        if self.effective_java() and self.effective_java() != "java":
            if not self.check_java():
                raise Exception("Java не найдена по указанному пути (настройки или настройки сборки). Проверьте путь.")
            resolved_java = self.effective_java()
        else:
            resolved_java = self.resolve_java_executable(version, status_callback=status_callback)
            if resolved_java == "java" and not self.check_java():
                raise Exception(
                    "Java не найдена ни в системе, ни через автозагрузку. "
                    "Проверьте подключение к интернету или укажите свою Java в настройках."
                )
        player_uuid = offline_uuid(player_name)

        options = {
            "username": player_name,
            "uuid": player_uuid,
            "token": "0",
            "userType": "legacy",
            "jvmArguments": [f"-Xmx{self.effective_memory()}M", f"-Xms{self.effective_memory() // 2}M"],
        }
        options["jvmArguments"] += jvm_preset_flags(getattr(self, "jvm_preset", "default"), self.effective_memory())
        skin_url = getattr(self, "skin_server_url", "")
        if skin_url:
            options["jvmArguments"] += skin_agent_jvm_args(skin_url, status_callback)
        if resolved_java and resolved_java != "java":
            options["executablePath"] = resolved_java
        if self.game_dir:
            os.makedirs(self.game_dir, exist_ok=True)
            options["gameDirectory"] = self.game_dir
        self.ensure_inherited_client_jar(version)
        version_jar_path = os.path.join(self.minecraft_dir, "versions", version, f"{version}.jar")
        if not os.path.exists(version_jar_path):
            try:
                os.makedirs(os.path.dirname(version_jar_path), exist_ok=True)
                with zipfile.ZipFile(version_jar_path, "w"):
                    pass
            except Exception:
                pass

        base_version = _resolve_base_version_id(self.minecraft_dir, version)
        if base_version != version:
            base_jar = os.path.join(self.minecraft_dir, "versions", base_version, f"{base_version}.jar")
            if not os.path.isfile(base_jar) or os.path.getsize(base_jar) < 1024:
                raise Exception(
                    f"Базовая ванильная версия '{base_version}', от которой зависит '{version}', "
                    f"не установлена или её файл повреждён/пуст:\n{base_jar}\n\n"
                    f"Скорее всего, установка версии Minecraft прошла не полностью. "
                    f"Удалите папку versions\\{base_version} целиком и заново нажмите "
                    f"«Установить и запустить» (не просто «Запустить»)."
                )

        cmd = get_minecraft_command(version, self.minecraft_dir, options)
        if "--userType" not in cmd:
            cmd.extend(["--userType", "legacy"])
        if "--accessToken" not in cmd:
            cmd.extend(["--accessToken", "0"])
        if "--uuid" not in cmd:
            cmd.extend(["--uuid", player_uuid])

        if extra_args:
            cmd.extend(extra_args)
        _log_install(f"Launch {version}: cmd={cmd}")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creationflags, text=True, cwd=self.game_path()
        )
        if callback:
            callback(proc)
        return proc


MRPACK_ALLOWED_HOSTS = ("cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com")
MRPACK_MAX_OVERRIDES_BYTES = 2 * 1024 ** 3


def _mrpack_safe_path(base, rel):
    """Полный путь внутри base или None, если rel пытается выйти за его пределы."""
    rel = (rel or "").replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    base_n = os.path.normpath(base)
    full = os.path.normpath(os.path.join(base_n, *rel.split("/")))
    if full != base_n and not full.startswith(base_n + os.sep):
        return None
    return full


def parse_mrpack_index(mrpack_path):
    """Читает modrinth.index.json: версия Minecraft, загрузчик и список файлов."""
    with zipfile.ZipFile(mrpack_path) as z:
        try:
            raw = z.read("modrinth.index.json")
        except KeyError:
            raise ValueError("В архиве нет modrinth.index.json, это не сборка Modrinth")
        index = json.loads(raw.decode("utf-8"))
    if index.get("game", "minecraft") != "minecraft":
        raise ValueError("Это сборка не для Minecraft")
    deps = index.get("dependencies") or {}
    mc = str(deps.get("minecraft") or "").strip()
    if not mc:
        raise ValueError("В сборке не указана версия Minecraft")
    loader, loader_version = "vanilla", ""
    for key, name in (("fabric-loader", "fabric"), ("quilt-loader", "quilt"),
                      ("neoforge", "neoforge"), ("forge", "forge")):
        if deps.get(key):
            loader, loader_version = name, str(deps[key]).strip()
            break
    if loader == "forge" and not loader_version.startswith(mc + "-"):
        loader_version = f"{mc}-{loader_version}"
    return {"name": index.get("name") or "Сборка", "mc_version": mc, "loader": loader,
            "loader_version": loader_version, "files": index.get("files") or []}


def install_mrpack(mrpack_path, dest_dir, progress=None, hosts=None, allow_http=False):
    """Распаковывает сборку Modrinth (.mrpack) в dest_dir: докачивает моды, кладёт overrides.
    Не трогает интерфейс, можно вызывать из потока. Возвращает информацию о сборке и список ошибок."""
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    hosts = tuple(h.lower() for h in (hosts or MRPACK_ALLOWED_HOSTS))
    info = parse_mrpack_index(mrpack_path)
    os.makedirs(dest_dir, exist_ok=True)
    files = [f for f in info["files"] if (f.get("env") or {}).get("client") != "unsupported"]
    total, failed, state, lock = len(files), [], {"done": 0}, threading.Lock()

    def fetch(f):
        rel = f.get("path", "")
        name = os.path.basename(rel) or "файл"
        target = _mrpack_safe_path(dest_dir, rel)
        if not target:
            raise ValueError(f"{name}: небезопасный путь")
        urls = []
        for u in f.get("downloads") or []:
            p = urlparse(u)
            if (p.hostname or "").lower() in hosts and (p.scheme == "https" or (allow_http and p.scheme == "http")):
                urls.append(u)
        if not urls:
            raise ValueError(f"{name}: нет разрешённой ссылки для скачивания")
        sha1 = ((f.get("hashes") or {}).get("sha1") or "").lower()
        sha512 = ((f.get("hashes") or {}).get("sha512") or "").lower()
        if not (sha1 or sha512):
            raise ValueError(f"{name}: нет контрольной суммы")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        last_error, tmp = None, target + ".part"
        for url in urls:
            try:
                h1, h512 = hashlib.sha1(), hashlib.sha512()
                with requests.get(url, stream=True, timeout=30, headers=ModrinthClient.HEADERS) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as out:
                        for chunk in r.iter_content(65536):
                            out.write(chunk)
                            h1.update(chunk)
                            h512.update(chunk)
                if (sha512 and h512.hexdigest() != sha512) or (sha1 and h1.hexdigest() != sha1):
                    raise ValueError("не совпала контрольная сумма")
                os.replace(tmp, target)
                return
            except Exception as e:
                last_error = e
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        raise Exception(f"{name}: {last_error}")

    def job(f):
        try:
            fetch(f)
        except Exception as e:
            with lock:
                failed.append(str(e))
        with lock:
            state["done"] += 1
            done = state["done"]
        if progress:
            progress(done, total, os.path.basename(f.get("path", "")))

    if total:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(job, files))

    with zipfile.ZipFile(mrpack_path) as z:
        size = 0
        for prefix in ("overrides/", "client-overrides/"):
            for m in z.infolist():
                if m.is_dir() or not m.filename.startswith(prefix):
                    continue
                size += m.file_size
                if size > MRPACK_MAX_OVERRIDES_BYTES:
                    raise ValueError("Слишком большой размер файлов сборки")
                rel = m.filename[len(prefix):]
                target = _mrpack_safe_path(dest_dir, rel)
                if not target:
                    failed.append(f"{rel}: небезопасный путь")
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(m) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    os.makedirs(os.path.join(dest_dir, "mods"), exist_ok=True)
    info["failed"] = failed
    return info


class ModrinthClient:
    API = "https://api.modrinth.com/v2"
    LOADERS = {"Любой": [], "Fabric": ["fabric"], "Forge": ["forge"],
               "NeoForge": ["neoforge"], "Quilt": ["quilt", "fabric"]}
    SORTS = {"Релевантность": "relevance", "Загрузки": "downloads", "Подписчики": "follows",
             "Новые": "newest", "Обновлённые": "updated"}
    HEADERS = {"User-Agent": f"MafinLauncher/{APP_VERSION} (minecraft launcher)", "Accept": "application/json"}

    def _get(self, path, params=None):
        r = requests.get(self.API + path, params=params, headers=self.HEADERS, timeout=20)
        if r.status_code == 429:
            raise Exception("Modrinth: слишком много запросов, подождите минуту")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def search(self, project_type, query, game_version, loaders, sort, offset=0, limit=25):
        facets = [[f"project_type:{project_type}"]]
        if game_version:
            facets.append([f"versions:{game_version}"])
        if loaders and project_type in ("mod", "modpack"):
            facets.append([f"categories:{l}" for l in loaders])
        params = {"facets": json.dumps(facets), "limit": limit, "offset": offset,
                  "index": self.SORTS.get(sort, "relevance")}
        if query:
            params["query"] = query
        data = self._get("/search", params) or {}
        return data.get("hits", []), data.get("total_hits", 0)

    def best_version(self, project_id, game_version, loaders):
        params = {}
        if loaders:
            params["loaders"] = json.dumps(loaders)
        if game_version:
            params["game_versions"] = json.dumps([game_version])
        versions = self._get(f"/project/{project_id}/version", params) or []
        for kind in ("release", "beta", "alpha"):
            for v in versions:
                if v.get("version_type") == kind and v.get("files"):
                    return v
        return None

    def _post(self, path, payload):
        r = requests.post(self.API + path, json=payload, headers=self.HEADERS, timeout=30)
        if r.status_code == 429:
            raise Exception("Modrinth: слишком много запросов, подождите минуту")
        r.raise_for_status()
        return r.json()

    def versions_by_hashes(self, hashes):
        out = {}
        for i in range(0, len(hashes), 100):
            out.update(self._post("/version_files", {"hashes": hashes[i:i + 100], "algorithm": "sha1"}) or {})
        return out

    def latest_by_hashes(self, hashes, loaders, game_version):
        out = {}
        for i in range(0, len(hashes), 100):
            out.update(self._post("/version_files/update", {
                "hashes": hashes[i:i + 100], "algorithm": "sha1",
                "loaders": loaders, "game_versions": [game_version]}) or {})
        return out

    def project_titles(self, ids):
        ids, out = list(ids), {}
        for i in range(0, len(ids), 50):
            for pr in self._get("/projects", {"ids": json.dumps(ids[i:i + 50])}) or []:
                out[pr["id"]] = pr.get("title")
        return out

    def version_by_id(self, version_id):
        return self._get(f"/version/{version_id}")

    def project(self, project_id):
        return self._get(f"/project/{project_id}")


def _fmt_mb(done, total=None):
    mb = lambda b: f"{b / 1048576:.1f}"
    if total:
        return f"{mb(done)} МБ из {mb(total)} МБ ({int(done * 100 / total)}%)"
    return f"{mb(done)} МБ"


class DownloadMeter:

    def __init__(self, minecraft_dir, version_id):
        self.dir, self.version_id = minecraft_dir, version_id
        self.files = {}
        self.finished = {}
        self._seen_json, self._seen_index = set(), set()
        self._stop = threading.Event()
        self.active = False
        self._lock = threading.Lock()
        self.total = 0
        self.done = 0

    @staticmethod
    def _os_name():
        return {"win32": "windows", "darwin": "osx"}.get(sys.platform, "linux")

    def _allowed(self, lib):
        rules = lib.get("rules")
        if not rules:
            return True
        allowed = False
        for r in rules:
            if "features" in r:
                continue
            osn = (r.get("os") or {}).get("name")
            if osn is None or osn == self._os_name():
                allowed = r.get("action") == "allow"
        return allowed

    def _add(self, path, size):
        size = int(size or 0)
        if size <= 0 or path in self.files:
            return
        try:
            if os.path.getsize(path) >= size:
                return
        except OSError:
            pass
        self.files[path] = size

    def _load_json(self, vid):
        p = os.path.join(self.dir, "versions", vid, f"{vid}.json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _discover(self):
        vid, guard = self.version_id, 0
        while vid and guard < 4:
            guard += 1
            data = self._load_json(vid)
            if data is None:
                return
            if vid not in self._seen_json:
                self._seen_json.add(vid)
                client = (data.get("downloads") or {}).get("client") or {}
                if client.get("size"):
                    self._add(os.path.join(self.dir, "versions", vid, f"{vid}.jar"), client["size"])
                for lib in data.get("libraries", []):
                    if not self._allowed(lib):
                        continue
                    dl = lib.get("downloads") or {}
                    art = dl.get("artifact")
                    if art and art.get("path"):
                        self._add(os.path.join(self.dir, "libraries", *art["path"].split("/")), art.get("size"))
                    natives = lib.get("natives") or {}
                    key = natives.get(self._os_name())
                    if key:
                        cl = (dl.get("classifiers") or {}).get(key.replace("${arch}", "64"))
                        if cl and cl.get("path"):
                            self._add(os.path.join(self.dir, "libraries", *cl["path"].split("/")), cl.get("size"))
            idx_id = (data.get("assetIndex") or {}).get("id")
            if idx_id and idx_id not in self._seen_index:
                ip = os.path.join(self.dir, "assets", "indexes", f"{idx_id}.json")
                try:
                    with open(ip, "r", encoding="utf-8") as f:
                        objs = json.load(f).get("objects", {})
                    self._seen_index.add(idx_id)
                    for obj in objs.values():
                        h = obj.get("hash", "")
                        if len(h) >= 2:
                            self._add(os.path.join(self.dir, "assets", "objects", h[:2], h), obj.get("size"))
                except Exception:
                    pass
            vid = data.get("inheritsFrom")

    def snapshot(self):
        with self._lock:
            try:
                self._discover()
            except Exception:
                pass
            done = sum(self.finished.values())
            for path, size in self.files.items():
                if path in self.finished:
                    continue
                try:
                    cur = os.path.getsize(path)
                except OSError:
                    continue
                if cur >= size:
                    self.finished[path] = size
                    done += size
                else:
                    done += cur
            self.total = sum(self.files.values())
            self.done = min(done, self.total)
            return self.done, self.total

    def start(self, on_update, interval=0.5):
        self.active = True

        def loop():
            while not self._stop.wait(interval):
                try:
                    d, t = self.snapshot()
                    if t:
                        on_update(d, t)
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        self.active = False
        self._stop.set()


def _short_number(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def mc_game_version(version_id):
    """Версия игры из идентификатора: 1.20.4, fabric-loader-0.15.7-1.20.4, 26.2 и т.п."""
    text = (version_id or "").strip()
    m = re.search(r"\b1\.\d{1,2}(?:\.\d{1,2})?\b", text)
    if m:
        return m.group(0)
    m = re.search(r"(?:^|[-_ ])(\d{2}\.\d{1,2}(?:\.\d{1,2})?)(?:$|[-_ ])", text)
    return m.group(1) if m else ""


def mc_version_key(version):
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", (version or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None


def server_version_range(server):
    """(от, до) для записи сервера или (None, None), если версия не указана."""
    lo = (server.get("version_from") or server.get("version") or "").strip()
    hi = (server.get("version_to") or server.get("version") or "").strip()
    if not lo and not hi:
        return None, None
    lo, hi = lo or hi, hi or lo
    klo, khi = mc_version_key(lo), mc_version_key(hi)
    if klo and khi and klo > khi:
        lo, hi = hi, lo
    return lo, hi


def format_server_versions(server):
    lo, hi = server_version_range(server)
    if not lo:
        return ""
    return lo if lo == hi else f"{lo} – {hi}"


def pick_server_launch_version(lo, hi, current):
    """Какую версию запускать для сервера: текущую, если она подходит, иначе новейшую из диапазона.
    Возвращает (версия, это_текущая_версия)."""
    if not lo:
        return current, True
    ck, lk, hk = mc_version_key(current), mc_version_key(lo), mc_version_key(hi)
    if ck and lk and hk and lk <= ck <= hk:
        return current, True
    return hi, False


def server_join_args(mc_version, address):
    """Аргументы запуска, чтобы игра сразу подключилась к серверу."""
    host, _, port = address.partition(":")
    key = mc_version_key(mc_version)
    if key is None or key >= (1, 20, 0):
        return ["--quickPlayMultiplayer", address]
    return ["--server", host, "--port", port or "25565"]


LAUNCHER_SERVER_MIN_VERSION = (1, 20, 4)


def _nbt_str(text):
    data = "".join(ch for ch in str(text) if ord(ch) <= 0xFFFF).encode("utf-8")[:65535]
    return struct.pack(">H", len(data)) + data


def _servers_entry_bytes(name, address):
    """Одна запись сервера: compound { ip: String, name: String }."""
    return b"\x08" + _nbt_str("ip") + _nbt_str(address) + b"\x08" + _nbt_str("name") + _nbt_str(name) + b"\x00"


def _servers_dat_build(entries_raw):
    body = b"".join(entries_raw)
    return (b"\x0a" + _nbt_str("") + b"\x09" + _nbt_str("servers") + b"\x0a"
            + struct.pack(">i", len(entries_raw)) + body + b"\x00")


def _servers_dat_read_entry(buf, pos):
    """Читает compound-запись списка. Возвращает (ip, name, конец_записи)."""
    ip = name = None
    while True:
        t = buf[pos]
        pos += 1
        if t == 0:
            return ip, name, pos
        key, pos = _nbt_read_string(buf, pos)
        if t == 8 and key in ("ip", "name"):
            value, pos = _nbt_read_string(buf, pos)
            if key == "ip":
                ip = value
            else:
                name = value
        else:
            pos = _nbt_skip_payload(buf, pos, t)


def servers_dat_add(game_dir, name, address):
    """Добавляет сервер в servers.dat (список «Сетевая игра»), не трогая остальные записи.
    Если запись с таким адресом уже есть, ничего не меняется. Старая запись с тем же названием
    и другим адресом (сервер переехал на другой порт) заменяется. Возвращает True, если файл изменён."""
    path = os.path.join(game_dir, "servers.dat")
    new_entry = _servers_entry_bytes(name, address)
    if not os.path.isfile(path):
        os.makedirs(game_dir, exist_ok=True)
        data = _servers_dat_build([new_entry])
    else:
        with open(path, "rb") as f:
            buf = f.read()
        if len(buf) < 4 or buf[0] != 10:
            raise ValueError("servers.dat повреждён или не в формате NBT")
        pos = 1
        _, pos = _nbt_read_string(buf, pos)
        list_start = None
        while True:
            t = buf[pos]
            if t == 0:
                root_end = pos
                break
            pos += 1
            key, pos = _nbt_read_string(buf, pos)
            if t == 9 and key == "servers" and list_start is None:
                list_start = pos
            pos = _nbt_skip_payload(buf, pos, t)
            if list_start is not None and key == "servers":
                list_end = pos
        if list_start is None:
            tail = b"\x09" + _nbt_str("servers") + b"\x0a" + struct.pack(">i", 1) + new_entry
            data = buf[:root_end] + tail + buf[root_end:]
        else:
            list_type = buf[list_start]
            count = struct.unpack_from(">i", buf, list_start + 1)[0]
            if count > 0 and list_type != 10:
                raise ValueError("В servers.dat неожиданный тип списка серверов")
            kept, p = [], list_start + 5
            for _ in range(max(count, 0)):
                ip, nm, end = _servers_dat_read_entry(buf, p)
                raw = buf[p:end]
                p = end
                if (ip or "").lower() == address.lower():
                    return False
                if nm == name:
                    continue
                kept.append(raw)
            kept.append(new_entry)
            new_list = (b"\x0a" + struct.pack(">i", len(kept)) + b"".join(kept))
            data = buf[:list_start] + new_list + buf[list_end:]
        try:
            shutil.copyfile(path, path + ".launcher_bak")
        except OSError:
            pass
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True



def ping_minecraft_server(host, port, timeout=3.0):
    """Server List Ping: версия сервера и число игроков. None, если сервер не ответил."""
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

    try:
        start = time.time()
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
            buf = b""
            while len(buf) < size:
                chunk = s.recv(min(65536, size - len(buf)))
                if not chunk:
                    raise ConnectionError("closed")
                buf += chunk
        data = json.loads(buf.decode("utf-8"))
        players = data.get("players") or {}
        return {"version": (data.get("version") or {}).get("name") or "",
                "online": players.get("online", 0), "max": players.get("max", 0),
                "ms": int((time.time() - start) * 1000)}
    except Exception:
        return None


def detect_game_version_and_loader(version_id):
    text = (version_id or "").strip()
    match_version = mc_game_version(text)
    low = text.lower()
    loader = ""
    if "neoforge" in low:
        loader = "NeoForge"
    elif "forge" in low:
        loader = "Forge"
    elif "fabric" in low:
        loader = "Fabric"
    elif "quilt" in low:
        loader = "Quilt"
    return match_version, loader


class ModrinthBrowser(tk.Toplevel):
    PAGE = 25

    def __init__(self, app, kind="mods"):
        super().__init__(app.root)
        self.app, self.kind = app, kind
        self.project_type = {"mods": "mod", "resourcepacks": "resourcepack", "modpacks": "modpack"}[kind]
        if kind == "mods":
            self.target_dir = app.mod_manager.mods_dir
        elif kind == "modpacks":
            self.target_dir = tempfile.mkdtemp(prefix="mafin_mrpack_")
        else:
            self.target_dir = app.rp_manager.rp_dir
        os.makedirs(self.target_dir, exist_ok=True)
        pack = app.config.get("active_modpack", "")
        suffix = f" → {pack}" if pack and kind == "mods" else ""
        self.title({"mods": "Modrinth — моды", "resourcepacks": "Modrinth — ресурспаки",
                    "modpacks": "Modrinth — сборки"}[kind] + suffix)
        self.geometry("940x640")
        self.minsize(780, 520)
        self.transient(app.root)
        self.results, self.total, self.busy = [], 0, False
        self.client = ModrinthClient()

        version, loader = detect_game_version_and_loader(app.version_var.get())
        if not loader:
            loader = {"forge": "Forge", "fabric": "Fabric", "neoforge": "NeoForge",
                      "quilt": "Quilt"}.get(app.modloader_var.get(), "")
        if kind == "modpacks":
            version, loader = "", ""
        self.query_var = tk.StringVar()
        self.version_var = tk.StringVar(value=version)
        self.loader_var = tk.StringVar(value=loader or "Любой")
        self.sort_var = tk.StringVar(value="Загрузки")
        self.deps_var = tk.BooleanVar(value=True)

        self._build()
        self.search()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=14, pady=(12, 4))
        entry = ttk.Entry(top, textvariable=self.query_var, font=("Segoe UI", 11))
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        entry.bind("<Return>", lambda e: self.search())
        entry.focus_set()
        ttk.Button(top, text="🔍 Найти", command=self.search).pack(side="left", padx=(8, 0))

        flt = ttk.Frame(self)
        flt.pack(fill="x", padx=14, pady=4)
        ttk.Label(flt, text="Версия MC:").pack(side="left")
        ver = ttk.Entry(flt, textvariable=self.version_var, width=9)
        ver.pack(side="left", padx=(4, 12))
        ver.bind("<Return>", lambda e: self.search())
        if self.kind in ("mods", "modpacks"):
            ttk.Label(flt, text="Загрузчик:").pack(side="left")
            combo = ttk.Combobox(flt, textvariable=self.loader_var, state="readonly", width=10,
                                 values=list(ModrinthClient.LOADERS))
            combo.pack(side="left", padx=(4, 12))
            combo.bind("<<ComboboxSelected>>", lambda e: self.search())
        ttk.Label(flt, text="Сортировка:").pack(side="left")
        sort = ttk.Combobox(flt, textvariable=self.sort_var, state="readonly", width=14,
                            values=list(ModrinthClient.SORTS))
        sort.pack(side="left", padx=4)
        sort.bind("<<ComboboxSelected>>", lambda e: self.search())

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=14, pady=6)
        cols = ("name", "author", "downloads", "updated")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor in (("name", "Название", 330, "w"), ("author", "Автор", 150, "w"),
                                         ("downloads", "Загрузки", 90, "e"), ("updated", "Обновлён", 100, "e")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "name"))
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_details())
        self.tree.bind("<Double-1>", lambda e: self.install())

        self.desc = tk.Label(self, text="", anchor="nw", justify="left", wraplength=880,
                             fg="#7a8599", height=3)
        self.desc.pack(fill="x", padx=14)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=14, pady=(4, 6))
        ttk.Button(bar, text="⬇ Установить", style="Accent.TButton", command=self.install).pack(side="left")
        if self.kind == "mods":
            ttk.Checkbutton(bar, text="Ставить зависимости", variable=self.deps_var).pack(side="left", padx=12)
        ttk.Button(bar, text="🌐 На сайте", command=self._open_page).pack(side="left")
        self.more_btn = ttk.Button(bar, text="Показать ещё", command=lambda: self.search(append=True))
        self.more_btn.pack(side="right")

        foot = ttk.Frame(self)
        foot.pack(fill="x", padx=14, pady=(0, 12))
        self.progress = ttk.Progressbar(foot, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 4))
        self.status = tk.Label(foot, text="Загрузка…", anchor="w", fg="#7a8599")
        self.status.pack(fill="x")

    def _open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    def _set_status(self, text, error=False):
        if self.winfo_exists():
            self.status.configure(text=text, fg="#ed4245" if error else "#7a8599")

    def _run(self, work, done):
        def runner():
            try:
                result, error = work(), None
            except Exception as e:
                result, error = None, e
            try:
                self.after(0, lambda: self.winfo_exists() and done(result, error))
            except Exception:
                pass
        threading.Thread(target=runner, daemon=True).start()

    def _loaders(self):
        return ModrinthClient.LOADERS.get(self.loader_var.get(), []) if self.kind in ("mods", "modpacks") else []

    def search(self, append=False):
        if self.busy:
            return
        offset = len(self.results) if append else 0
        args = (self.project_type, self.query_var.get().strip(), self.version_var.get().strip(),
                self._loaders(), self.sort_var.get(), offset, self.PAGE)
        self.busy = True
        self._set_status("Ищу…")

        def done(result, error):
            self.busy = False
            if error:
                self._set_status(f"Ошибка поиска: {error}", error=True)
                return
            items, total = result
            if not append:
                self.results = []
                self.tree.delete(*self.tree.get_children())
            self.total = total
            for hit in items:
                if self.tree.exists(hit["project_id"]):
                    continue
                self.results.append(hit)
                self.tree.insert("", "end", iid=hit["project_id"], values=(
                    hit.get("title", ""), hit.get("author", ""), _short_number(hit.get("downloads")),
                    (hit.get("date_modified") or "")[:10]))
            self.more_btn.state(["!disabled" if len(self.results) < total else "disabled"])
            self._set_status(f"Показано {len(self.results)} из {total}" if total else "Ничего не найдено")

        self._run(lambda: self.client.search(*args), done)

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return next((m for m in self.results if m["project_id"] == sel[0]), None)

    def _show_details(self):
        hit = self._selected()
        self.desc.configure(text=(hit.get("description", "") if hit else ""))

    def _open_page(self):
        hit = self._selected()
        if hit:
            self._open_url(f"https://modrinth.com/{hit.get('project_type', self.project_type)}/"
                           f"{hit.get('slug') or hit['project_id']}")

    def install(self):
        hit = self._selected()
        if not hit or self.busy:
            if not hit:
                self._set_status("Выберите элемент из списка", error=True)
            return
        loaders = self._loaders()
        version = self.version_var.get().strip()
        if self.kind == "modpacks":
            self._install_modpack(hit, version, loaders)
            return
        want_deps = self.deps_var.get() and self.kind == "mods"
        self.busy = True
        self.progress["value"] = 0

        def done(result, error):
            self.busy = False
            self.progress["value"] = 0
            if error:
                self._set_status(f"Ошибка установки: {error}", error=True)
                return
            installed, skipped, missing = result
            parts = []
            if installed:
                parts.append("Установлено: " + ", ".join(installed))
            if skipped:
                parts.append("Уже есть: " + ", ".join(skipped))
            if missing:
                parts.append("Нет файла под эту версию/загрузчик: " + ", ".join(missing))
            self._set_status(" • ".join(parts) or "Ничего не установлено", error=not (installed or skipped))
            if installed:
                if self.kind == "mods":
                    self.app.refresh_mods()
                    self.app._bump_achievement("mods_installed", len(installed))
                else:
                    self.app.refresh_rps()

        def work():
            if self.kind == "mods":
                self.after(0, lambda: self._set_status("Создаю бэкап сборки…"))
                self.app.auto_pack_backup("modrinth")
            return self._install_chain(hit, version, loaders, want_deps)

        self._run(work, done)

    def _install_modpack(self, hit, version, loaders):
        title = hit.get("title", hit["project_id"])
        self.busy = True
        self.progress["value"] = 0

        def work():
            self.after(0, lambda: self._set_status(f"Ищу версию сборки: {title}…"))
            ver = self.client.best_version(hit["project_id"], version, loaders)
            if not ver:
                raise Exception("Нет подходящей версии сборки (попробуйте убрать фильтры версии и загрузчика)")
            file_obj = next((f for f in ver["files"] if (f.get("filename") or "").endswith(".mrpack")), None)
            if not file_obj:
                raise Exception("У этой версии нет файла .mrpack, такой формат не поддерживается")
            self.after(0, lambda: self._set_status(f"Скачиваю сборку «{title}»…"))
            self._download(title, file_obj)
            mrpack = os.path.join(self.target_dir, os.path.basename(file_obj["filename"]))
            name = self.app._unique_pack_name(title)
            dest = self.app._modpack_dir(name)
            info = install_mrpack(
                mrpack, dest,
                lambda d, t, f: self.after(0, lambda: self._on_pack_progress(title, d, t, f)))
            try:
                os.remove(mrpack)
            except OSError:
                pass
            self.app.root.after(0, lambda: self.app._finish_mrpack_install(name, info))
            return name

        def done(result, error):
            self.busy = False
            self.progress["value"] = 0
            if error:
                self._set_status(f"Ошибка установки сборки: {error}", error=True)
            else:
                self._set_status(f"Сборка «{result}» установлена")

        self._run(work, done)

    def _on_pack_progress(self, title, done, total, fname):
        if self.winfo_exists() and total:
            self.progress["value"] = done * 100 / total
            self.status.configure(text=f"{title}: {done}/{total} — {fname}")

    def _install_chain(self, hit, version, loaders, want_deps):
        installed, skipped, missing, seen = [], [], [], set()
        queue_ = [(hit["project_id"], hit.get("title", hit["project_id"]), None, 0)]
        while queue_:
            project_id, title, forced_version_id, depth = queue_.pop(0)
            if project_id in seen:
                continue
            seen.add(project_id)
            self.after(0, lambda n=title: self._set_status(f"Ищу файл: {n}…"))
            ver = self.client.best_version(project_id, version, loaders)
            if not ver and forced_version_id:
                ver = self.client.version_by_id(forced_version_id)
            if not ver or not ver.get("files"):
                missing.append(title)
                continue
            file_obj = next((f for f in ver["files"] if f.get("primary")), ver["files"][0])
            if self._download(title, file_obj):
                installed.append(title)
            else:
                skipped.append(title)
            if want_deps and depth < 4:
                for dep in ver.get("dependencies", []):
                    if dep.get("dependency_type") != "required":
                        continue
                    dep_pid, dep_vid = dep.get("project_id"), dep.get("version_id")
                    try:
                        if not dep_pid and dep_vid:
                            v = self.client.version_by_id(dep_vid)
                            dep_pid = v.get("project_id") if v else None
                        if not dep_pid or dep_pid in seen:
                            continue
                        info = self.client.project(dep_pid) or {}
                        queue_.append((dep_pid, info.get("title", dep_pid), dep_vid, depth + 1))
                    except Exception:
                        pass
        return installed, skipped, missing

    def _download(self, name, file_obj):
        import hashlib
        filename = os.path.basename(file_obj.get("filename") or "download.jar")
        dest = os.path.join(self.target_dir, filename)
        sha512 = (file_obj.get("hashes") or {}).get("sha512")
        if os.path.isfile(dest) and sha512:
            h = hashlib.sha512()
            with open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if h.hexdigest().lower() == sha512.lower():
                return False
        tmp = dest + ".part"
        digest = hashlib.sha512()
        with requests.get(file_obj["url"], stream=True, timeout=30, headers=ModrinthClient.HEADERS) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or file_obj.get("size") or 0)
            done = 0
            with open(tmp, "wb") as out:
                for chunk in r.iter_content(65536):
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if total:
                        self.after(0, lambda d=done, t=total, n=name: self._on_progress(n, d, t))
        if sha512 and digest.hexdigest().lower() != sha512.lower():
            os.remove(tmp)
            raise Exception(f"{filename}: файл повреждён (не совпал хэш), попробуйте ещё раз")
        os.replace(tmp, dest)
        return True

    def _on_progress(self, name, done, total):
        if self.winfo_exists():
            self.progress["value"] = done * 100 / total
            self.status.configure(text=f"Скачиваю {name}: {_fmt_mb(done, total)}")


try:
    import tomllib as _tomllib
except Exception:
    _tomllib = None


class ModChecker:
    ERROR, WARN, INFO = "error", "warn", "info"
    IGNORED_DEPS = {"minecraft", "java", "fabricloader", "fabric-loader", "fabric_loader", "quilt_loader",
                    "quilt-loader", "forge", "neoforge", "fml", "javafml", "lowcodefml", "mclanguage"}
    COMPAT = {"fabric": {"fabric"}, "quilt": {"quilt", "fabric"},
              "forge": {"forge", "forge-legacy"}, "neoforge": {"neoforge"}}
    SOFT = {"neoforge": {"forge"}}
    MAX_NESTED = 100 * 1024 * 1024

    def __init__(self, mods_dir, loader="", mc_version=""):
        self.mods_dir = mods_dir
        self.loader = (loader or "").lower() if (loader or "").lower() in self.COMPAT else ""
        self.mc_version = mc_version or ""

    @staticmethod
    def _vtuple(s):
        m = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(s or ""))
        return tuple(int(x) for x in m.group(1).split(".")) if m else None

    @staticmethod
    def _cmp(a, b):
        n = max(len(a), len(b))
        a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
        return (a > b) - (a < b)

    @classmethod
    def _match_clause(cls, clause, v):
        m = re.match(r"^(>=|<=|>|<|=|~|\^)?(.+)$", clause)
        if not m:
            return True
        op, val = m.group(1) or "=", m.group(2)
        if re.search(r"[xX*]", val):
            prefix = []
            for part in val.split("."):
                if part in ("x", "X", "*"):
                    break
                if not part.isdigit():
                    return True
                prefix.append(int(part))
            vv = v + (0,) * (len(prefix) - len(v))
            return vv[:len(prefix)] == tuple(prefix)
        t = cls._vtuple(val)
        if t is None:
            return True
        c = cls._cmp(v, t)
        if op == ">=":
            return c >= 0
        if op == "<=":
            return c <= 0
        if op == ">":
            return c > 0
        if op == "<":
            return c < 0
        if op == "~":
            idx = min(len(t) - 1, 1)
            return c >= 0 and cls._cmp(v, t[:idx] + (t[idx] + 1,)) < 0
        if op == "^":
            return c >= 0 and cls._cmp(v, (t[0] + 1,)) < 0
        return c == 0

    @classmethod
    def match_fabric(cls, pred, ver):
        v = cls._vtuple(ver)
        if v is None:
            return True
        if isinstance(pred, (list, tuple)):
            return any(cls.match_fabric(p, ver) for p in pred) if pred else True
        if isinstance(pred, dict):
            return True
        pred = str(pred or "").strip()
        if pred in ("", "*"):
            return True
        for alt in pred.split("||"):
            if all(cls._match_clause(c, v) for c in alt.split()):
                return True
        return False

    @classmethod
    def match_maven(cls, rng, ver):
        v = cls._vtuple(ver)
        if v is None or not rng:
            return True
        segs = re.findall(r"[\[\(][^\]\)]*[\]\)]", str(rng))
        if not segs:
            return True
        for seg in segs:
            lo_inc, hi_inc = seg[0] == "[", seg[-1] == "]"
            inner = seg[1:-1]
            if "," not in inner:
                t = cls._vtuple(inner)
                if t is None or cls._cmp(v, t) == 0:
                    return True
                continue
            lo, hi = [x.strip() for x in inner.split(",", 1)]
            ok = True
            if lo and cls._vtuple(lo) is not None:
                c = cls._cmp(v, cls._vtuple(lo))
                ok = ok and (c > 0 or (c == 0 and lo_inc))
            if hi and cls._vtuple(hi) is not None:
                c = cls._cmp(v, cls._vtuple(hi))
                ok = ok and (c < 0 or (c == 0 and hi_inc))
            if ok:
                return True
        return False

    @classmethod
    def _matches(cls, kind, pred, ver):
        if kind in ("forge", "neoforge", "forge-legacy"):
            return cls.match_maven(pred, ver)
        return cls.match_fabric(pred, ver)

    @staticmethod
    def _pred_text(pred):
        if isinstance(pred, (list, tuple)):
            return " или ".join(str(p) for p in pred)
        if isinstance(pred, dict):
            return ""
        return "" if str(pred or "").strip() in ("", "*") else str(pred)

    def _parse_zip(self, zf, nested_level=0):
        info = {"kinds": set(), "mods": [], "nested_mods": []}
        names = set(zf.namelist())

        def rd(n):
            return zf.read(n).decode("utf-8-sig", "replace")

        def mk(kind, mid, ver, name, provides=(), depends=(), breaks=(), mcversion=""):
            return {"id": str(mid), "version": str(ver) if ver is not None else None, "name": name or mid,
                    "provides": [str(x) for x in provides], "depends": list(depends),
                    "breaks": list(breaks), "kind": kind, "mcversion": mcversion}

        if "fabric.mod.json" in names:
            try:
                d = json.loads(rd("fabric.mod.json"), strict=False)
                if d.get("id"):
                    info["kinds"].add("fabric")
                    info["mods"].append(mk("fabric", d["id"], d.get("version"), d.get("name"),
                                           d.get("provides") or [],
                                           list((d.get("depends") or {}).items()),
                                           list((d.get("breaks") or {}).items())))
            except Exception:
                pass
        if "quilt.mod.json" in names:
            try:
                q = json.loads(rd("quilt.mod.json"), strict=False).get("quilt_loader", {})
                if q.get("id"):
                    info["kinds"].add("quilt")

                    def norm(items):
                        out = []
                        for it in items or []:
                            if isinstance(it, str):
                                out.append((it, "*"))
                            elif isinstance(it, dict) and it.get("id") and not it.get("optional"):
                                out.append((it["id"], it.get("versions", "*")))
                        return out
                    prov = [p if isinstance(p, str) else p.get("id") for p in q.get("provides") or []]
                    info["mods"].append(mk("quilt", q["id"], q.get("version"),
                                           (q.get("metadata") or {}).get("name"),
                                           [p for p in prov if p], norm(q.get("depends")),
                                           norm(q.get("breaks"))))
            except Exception:
                pass
        for toml_name, kind in (("META-INF/neoforge.mods.toml", "neoforge"), ("META-INF/mods.toml", "forge")):
            if toml_name not in names:
                continue
            text = rd(toml_name)
            data = None
            if _tomllib:
                try:
                    data = _tomllib.loads(text)
                except Exception:
                    data = None
            jar_ver = None
            if "META-INF/MANIFEST.MF" in names:
                mm = re.search(r"^Implementation-Version:\s*(\S+)", rd("META-INF/MANIFEST.MF"), re.M)
                jar_ver = mm.group(1) if mm else None
            if data is not None:
                for m in data.get("mods", []) or []:
                    mid = m.get("modId")
                    if not mid:
                        continue
                    ver = m.get("version")
                    if not ver or "${" in str(ver):
                        ver = jar_ver
                    deps, breaks = [], []
                    for dep in (data.get("dependencies") or {}).get(mid, []) or []:
                        did = dep.get("modId")
                        if not did or str(dep.get("side", "BOTH")).upper() == "SERVER":
                            continue
                        dtype = str(dep.get("type", "")).lower()
                        if dtype == "incompatible":
                            breaks.append((did, dep.get("versionRange", "*")))
                        elif dep.get("mandatory") is True or dtype == "required":
                            deps.append((did, dep.get("versionRange", "*")))
                    info["kinds"].add(kind)
                    info["mods"].append(mk(kind, mid, ver, m.get("displayName"), (), deps, breaks))
            else:
                ids = re.findall(r'modId\s*=\s*"([^"]+)"', text)
                if ids:
                    info["kinds"].add(kind)
                    info["mods"].append(mk(kind, ids[0], jar_ver, ids[0]))
        if "mcmod.info" in names:
            try:
                data = json.loads(rd("mcmod.info"), strict=False)
                if isinstance(data, dict):
                    data = data.get("modList", [])
                for m in data or []:
                    if isinstance(m, dict) and m.get("modid"):
                        info["kinds"].add("forge-legacy")
                        info["mods"].append(mk("forge-legacy", m["modid"], m.get("version"), m.get("name"),
                                               mcversion=m.get("mcversion", "")))
            except Exception:
                pass

        if nested_level == 0:
            for n in names:
                if n.startswith(("META-INF/jars/", "META-INF/jarjar/")) and n.lower().endswith(".jar"):
                    try:
                        if zf.getinfo(n).file_size > self.MAX_NESTED:
                            continue
                        with zipfile.ZipFile(io.BytesIO(zf.read(n))) as nz:
                            sub = self._parse_zip(nz, 1)
                        info["nested_mods"].extend(sub["mods"])
                    except Exception:
                        pass
        return info

    def scan(self):
        issues, entries = [], []

        def add(level, fn, text):
            issues.append({"level": level, "file": fn, "text": text})

        if not os.path.isdir(self.mods_dir):
            return issues, 0
        files = sorted(f for f in os.listdir(self.mods_dir) if f.lower().endswith(".jar"))
        for fn in files:
            try:
                with zipfile.ZipFile(os.path.join(self.mods_dir, fn)) as zf:
                    entries.append((fn, self._parse_zip(zf)))
            except zipfile.BadZipFile:
                add(self.ERROR, fn, "Повреждённый архив — файл не открывается как jar")
            except Exception as e:
                add(self.ERROR, fn, f"Не удалось прочитать файл: {e}")

        provided = {}
        for fn, info in entries:
            for m in info["mods"] + info["nested_mods"]:
                provided.setdefault(m["id"], m["version"])
                for pid in m["provides"]:
                    provided.setdefault(pid, m["version"])

        by_id = {}
        for fn, info in entries:
            for m in info["mods"]:
                by_id.setdefault(m["id"], []).append((fn, m["version"]))
        for mid, lst in by_id.items():
            files_of = sorted({f for f, _ in lst})
            if len(files_of) > 1:
                vers = ", ".join(sorted({str(v) for _, v in lst}))
                for f in files_of:
                    others = ", ".join(x for x in files_of if x != f)
                    add(self.ERROR, f, f"Дубликат мода «{mid}» (версии: {vers}). Также в: {others}. Оставьте один файл")

        compat = self.COMPAT.get(self.loader, set())
        soft = self.SOFT.get(self.loader, set())
        for fn, info in entries:
            if not info["kinds"]:
                add(self.INFO, fn, "Метаданные мода не найдены (возможно, библиотека или нестандартный мод) — пропущен")
                continue
            if self.loader:
                if info["kinds"] & compat:
                    pass
                elif info["kinds"] & soft:
                    add(self.WARN, fn, f"Мод в формате {'/'.join(sorted(info['kinds']))}: на {self.loader} может не запуститься")
                else:
                    add(self.ERROR, fn, f"Мод для {'/'.join(sorted(info['kinds']))}, а выбран загрузчик {self.loader} — не загрузится")
                    continue
            for m in info["mods"]:
                if self.loader and m["kind"] not in (compat | soft):
                    continue
                kind = m["kind"]
                if m.get("mcversion") and self.mc_version and not str(m["mcversion"]).startswith(self.mc_version):
                    add(self.WARN, fn, f"«{m['name']}» собран для Minecraft {m['mcversion']}, а выбран {self.mc_version}")
                for did, pred in m["depends"]:
                    ptxt = self._pred_text(pred)
                    if did == "minecraft":
                        if self.mc_version and not self._matches(kind, pred, self.mc_version):
                            add(self.ERROR, fn, f"«{m['name']}» не подходит для Minecraft {self.mc_version} (нужно: {ptxt or '?'})")
                    elif did in self.IGNORED_DEPS:
                        continue
                    elif did not in provided:
                        add(self.ERROR, fn, f"«{m['name']}» требует мод «{did}»" + (f" {ptxt}" if ptxt else "") + ", но он не установлен")
                    elif not self._matches(kind, pred, provided[did]):
                        add(self.WARN, fn, f"«{m['name']}» требует «{did}» {ptxt}, установлена версия {provided[did]}")
                for bid, pred in m["breaks"]:
                    if bid in provided and bid != m["id"] and self._matches(kind, pred, provided[bid]):
                        add(self.ERROR, fn, f"«{m['name']}» конфликтует с установленным модом «{bid}»")

        seen, unique = set(), []
        for it in issues:
            key = (it["level"], it["file"], it["text"])
            if key not in seen:
                seen.add(key)
                unique.append(it)
        order = {self.ERROR: 0, self.WARN: 1, self.INFO: 2}
        unique.sort(key=lambda i: (order[i["level"]], i["file"].lower()))
        return unique, len(files)


def modrinth_download_file(file_obj, dest_dir, on_progress=None, dest_path=None):
    import hashlib
    filename = os.path.basename(file_obj.get("filename") or "download.jar")
    dest = dest_path or os.path.join(dest_dir, filename)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    hashes = file_obj.get("hashes") or {}
    sha512, sha1 = hashes.get("sha512"), hashes.get("sha1")
    tmp = dest + ".part"
    digest = hashlib.sha512() if sha512 else hashlib.sha1()
    expected = sha512 or sha1
    try:
        with requests.get(file_obj["url"], stream=True, timeout=30, headers=ModrinthClient.HEADERS) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or file_obj.get("size") or 0)
            done = 0
            with open(tmp, "wb") as out:
                for chunk in r.iter_content(65536):
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress and total:
                        on_progress(done, total)
        if expected and digest.hexdigest().lower() != expected.lower():
            raise Exception(f"{filename}: файл повреждён (не совпал хэш), попробуйте ещё раз")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return dest


def find_mod_updates(client, mods_dir, loaders, game_version):
    import hashlib
    if not os.path.isdir(mods_dir):
        return [], 0, 0
    files = sorted(f for f in os.listdir(mods_dir) if f.lower().endswith(".jar"))
    by_hash = {}
    for fn in files:
        h = hashlib.sha1()
        with open(os.path.join(mods_dir, fn), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        by_hash.setdefault(h.hexdigest(), fn)
    if not by_hash:
        return [], 0, 0
    current = client.versions_by_hashes(list(by_hash))
    latest = client.latest_by_hashes(list(current), loaders, game_version) if current else {}
    rank = {"release": 0, "beta": 1, "alpha": 2}
    updates = []
    for h, cur in current.items():
        new = latest.get(h)
        if not new or new.get("id") == cur.get("id"):
            continue
        if (new.get("date_published") or "") <= (cur.get("date_published") or ""):
            continue
        if rank.get(new.get("version_type"), 0) > rank.get(cur.get("version_type"), 0):
            continue
        new_files = new.get("files") or []
        fobj = next((x for x in new_files if x.get("primary")), new_files[0] if new_files else None)
        if not fobj:
            continue
        updates.append({"file": by_hash[h], "hash": h, "project_id": new.get("project_id"),
                        "current": cur.get("version_number") or "?", "new": new.get("version_number") or "?",
                        "new_type": new.get("version_type", "release"), "file_obj": fobj})
    titles = client.project_titles({u["project_id"] for u in updates if u["project_id"]})
    for u in updates:
        u["title"] = titles.get(u["project_id"]) or u["file"]
    updates.sort(key=lambda u: u["title"].lower())
    return updates, len(files), len(by_hash) - len(current)


import hashlib

def get_system_memory_mb():
    """(всего МБ, свободно МБ) или (None, None), если узнать не удалось."""
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys // 1048576), int(st.ullAvailPhys // 1048576)
        elif sys.platform == "darwin":
            total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=3).strip()) // 1048576
            free = None
            try:
                out = subprocess.check_output(["vm_stat"], text=True, timeout=3)
                page = int(re.search(r"page size of (\d+) bytes", out).group(1))
                pages = sum(int(m.group(1)) for m in re.finditer(r"Pages (?:free|inactive|speculative):\s+(\d+)", out))
                free = pages * page // 1048576
            except Exception:
                pass
            return total, free
        else:
            vals = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    vals[k] = int(v.split()[0])
            total = vals.get("MemTotal")
            free = vals.get("MemAvailable", vals.get("MemFree"))
            if total:
                return total // 1024, (free // 1024 if free else None)
    except Exception:
        pass
    return None, None


def count_enabled_mods(mods_dir):
    try:
        return sum(1 for f in os.listdir(mods_dir) if f.lower().endswith((".jar", ".litemod")))
    except OSError:
        return 0


def recommend_memory(total_mb, free_mb, mods_count, loader, mc_version):
    """Подбирает ОЗУ под машину и сборку. Возвращает {'mb', 'reason', 'capped'}."""
    m = re.match(r"(\d+)\.(\d+)", str(mc_version or ""))
    key = (int(m.group(1)), int(m.group(2))) if m else None
    old = key is not None and key < (1, 13)
    modded = bool(loader) and loader != "vanilla"
    if modded:
        want = 3072 + 32 * max(0, int(mods_count))
        if old:
            want = int(want * 0.8)
        want = min(want, 10240)
    else:
        want = 3072 if (key is None or key >= (1, 17)) else 2048
    capped = False
    if total_mb:
        limit = total_mb - max(2048, total_mb // 4)
        if free_mb:
            limit = min(limit, max(free_mb - 512, 1024))
        limit = max(limit, 1024)
        if want > limit:
            want, capped = limit, True
    mb = max(1024, int(want) // 256 * 256)
    parts = []
    if total_mb:
        parts.append(f"ОЗУ компьютера: {total_mb / 1024:.1f} ГБ" + (f", свободно сейчас: {free_mb / 1024:.1f} ГБ" if free_mb else ""))
    else:
        parts.append("объём ОЗУ компьютера определить не удалось")
    parts.append(f"модов: {mods_count}" if modded else "без модов")
    reason = "; ".join(parts) + f" → {mb / 1024:.2g} ГБ ({mb} МБ)"
    if capped:
        reason += ". Ограничено, чтобы системе осталась память"
    return {"mb": mb, "reason": reason, "capped": capped}


_JVM_MOJANG = ["-XX:+UnlockExperimentalVMOptions", "-XX:+UseG1GC", "-XX:G1NewSizePercent=20",
               "-XX:G1ReservePercent=20", "-XX:MaxGCPauseMillis=50", "-XX:G1HeapRegionSize=32M"]
_JVM_OPTIMIZED = ["-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
                  "-XX:+UnlockExperimentalVMOptions", "-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40",
                  "-XX:G1HeapRegionSize=8M", "-XX:G1ReservePercent=20", "-XX:G1HeapWastePercent=5",
                  "-XX:G1MixedGCCountTarget=4", "-XX:InitiatingHeapOccupancyPercent=15",
                  "-XX:G1MixedGCLiveThresholdPercent=90", "-XX:G1RSetUpdatingPauseTimePercent=5",
                  "-XX:SurvivorRatio=32", "-XX:+PerfDisableSharedMem", "-XX:MaxTenuringThreshold=1"]
JVM_PRESET_TITLES = {"default": "Без доп. флагов", "auto": "Автоматически (по объёму ОЗУ)",
                     "mojang": "Как в лаунчере Mojang (G1GC)", "optimized": "Оптимизированный G1GC (для сборок с модами)"}


def resolve_jvm_preset(preset, mem_mb):
    if preset == "auto":
        return "optimized" if int(mem_mb or 0) >= 4096 else "mojang"
    return preset if preset in ("mojang", "optimized") else "default"


def jvm_preset_flags(preset, mem_mb):
    name = resolve_jvm_preset(preset, mem_mb)
    return list({"mojang": _JVM_MOJANG, "optimized": _JVM_OPTIMIZED}.get(name, []))


SERVER_PACK_STATE_FILE = ".mafin_server_pack.json"
SERVER_PACK_ROOTS = ("mods", "resourcepacks", "shaderpacks", "config")


def _sp_hash_spec(f):
    for algo in ("sha512", "sha256", "sha1"):
        v = str(f.get(algo) or "").lower()
        if v:
            return algo, v
    return None, None


def _sp_file_hash(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def serverpack_load_state(pack_dir):
    try:
        with open(os.path.join(pack_dir, SERVER_PACK_STATE_FILE), "r", encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("files", {})
            return st
    except (OSError, ValueError):
        pass
    return {"revision": 0, "files": {}}


def serverpack_save_state(pack_dir, state):
    os.makedirs(pack_dir, exist_ok=True)
    path = os.path.join(pack_dir, SERVER_PACK_STATE_FILE)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def serverpack_plan(manifest, pack_dir, state, optional_choice):
    """Что скачать, что оставить и что удалить. Ничего не меняет на диске."""
    files_state = state.get("files") or {}
    wanted = [f for f in manifest.get("files", []) if not f.get("optional") or optional_choice.get(f.get("path"))]
    download, keep, bad = [], [], []
    for f in wanted:
        path = str(f.get("path") or "")
        target = _mrpack_safe_path(pack_dir, path)
        algo, want = _sp_hash_spec(f)
        if not target or path.split("/")[0] not in SERVER_PACK_ROOTS or not algo:
            bad.append(path)
            continue
        root = path.split("/")[0]
        recorded = files_state.get(path) or {}
        exists = os.path.isfile(target)
        if not exists and root == "mods" and os.path.isfile(target + ".disabled"):
            keep.append(f)
            continue
        if not exists:
            download.append(f)
            continue
        if root == "config" and recorded.get("hash") == want:
            keep.append(f)
            continue
        st = os.stat(target)
        if recorded.get("hash") == want and recorded.get("size") == st.st_size and recorded.get("mtime") == int(st.st_mtime):
            keep.append(f)
            continue
        size = int(f.get("size") or 0)
        if (not size or st.st_size == size) and _sp_file_hash(target, algo) == want:
            keep.append(f)
        else:
            download.append(f)
    wanted_paths = {str(f.get("path")) for f in wanted}
    remove = sorted(p for p in files_state if p not in wanted_paths)
    return {"download": download, "keep": keep, "remove": remove, "bad": bad,
            "download_bytes": sum(int(f.get("size") or 0) for f in download)}


def serverpack_apply(manifest, pack_dir, plan, state, download, progress=None, workers=4):
    """Скачивает файлы плана (download(f, tmp_path) пишет файл), проверяет хэши, удаляет лишнее.
    Возвращает (новое состояние, список ошибок)."""
    from concurrent.futures import ThreadPoolExecutor
    files_state = dict(state.get("files") or {})
    failed, lock, done = [], threading.Lock(), [0]
    total = len(plan["download"])

    def job(f):
        path = f["path"]
        target = _mrpack_safe_path(pack_dir, path)
        tmp = target + ".part"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            download(f, tmp)
            algo, want = _sp_hash_spec(f)
            if _sp_file_hash(tmp, algo) != want:
                raise ValueError("не совпала контрольная сумма")
            os.replace(tmp, target)
            st = os.stat(target)
            with lock:
                files_state[path] = {"hash": want, "size": st.st_size, "mtime": int(st.st_mtime)}
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            with lock:
                failed.append(f"{os.path.basename(path)}: {e}")
        finally:
            with lock:
                done[0] += 1
                n = done[0]
            if progress:
                progress(n, total, os.path.basename(path))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(job, plan["download"]))
    for f in plan["keep"]:
        path = f["path"]
        target = _mrpack_safe_path(pack_dir, path)
        _algo, want = _sp_hash_spec(f)
        if (files_state.get(path) or {}).get("hash") == want or not target:
            continue
        if os.path.isfile(target):
            st = os.stat(target)
            files_state[path] = {"hash": want, "size": st.st_size, "mtime": int(st.st_mtime)}
    for path in plan["remove"]:
        target = _mrpack_safe_path(pack_dir, path)
        for cand in ((target, target + ".disabled") if target else ()):
            try:
                os.remove(cand)
            except OSError:
                pass
        files_state.pop(path, None)
    new_state = {"revision": 0 if failed else int(manifest.get("revision") or 0), "files": files_state,
                 "name": manifest.get("name", "")}
    return new_state, failed


def serverpack_download(api, f, dest):
    """Скачивает один файл сборки: с нашего сервера (с токеном) или только с cdn.modrinth.com."""
    url = str(f.get("url") or "")
    headers = {"User-Agent": f"MafinLauncher/{APP_VERSION}"}
    if url.startswith("/api/modpack/file/"):
        full = api.base_url + url
        if api.token:
            headers["Authorization"] = f"Bearer {api.token}"
    else:
        p = urlparse(url)
        if p.scheme != "https" or (p.hostname or "").lower() != "cdn.modrinth.com":
            raise ValueError("недопустимая ссылка на файл")
        full = url
    with requests.get(full, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(dest, "wb") as out:
            for chunk in r.iter_content(256 * 1024):
                out.write(chunk)


BISECT_STATE_FILE = ".mafin_bisect.json"


def build_mod_graph(mods_dir, names, loader="", mc_version=""):
    """{файл: множество файлов, без которых он не запустится} по метаданным модов."""
    checker = ModChecker(mods_dir, loader, mc_version)
    provided, needs = {}, {}
    for fn in names:
        try:
            with zipfile.ZipFile(os.path.join(mods_dir, fn)) as zf:
                info = checker._parse_zip(zf)
        except Exception:
            needs[fn] = set()
            continue
        needs[fn] = {d for m in info["mods"] for d, _p in m["depends"] if d not in ModChecker.IGNORED_DEPS}
        for m in info["mods"] + info["nested_mods"]:
            provided.setdefault(m["id"], fn)
            for pid in m["provides"]:
                provided.setdefault(pid, fn)
    graph = {}
    for fn in names:
        graph[fn] = {provided[d] for d in needs.get(fn, ()) if d in provided and provided[d] != fn}
    return graph


def make_closure(graph):
    def closure(files):
        out, stack = set(), list(files)
        while stack:
            fn = stack.pop()
            if fn in out or fn not in graph:
                continue
            out.add(fn)
            stack.extend(graph[fn])
        return sorted(out)
    return closure


def mod_bisect_gen(files, closure):
    """Генератор: отдаёт набор файлов для проверки, получает True, если проблема воспроизвелась."""
    def reduce(fixed, cands):
        fixed, cands = list(fixed), list(cands)
        while len(cands) > 1:
            mid = len(cands) // 2
            a, b = cands[:mid], cands[mid:]
            if (yield closure(fixed + a)):
                cands = a
            elif (yield closure(fixed + b)):
                cands = b
            else:
                fixed, cands = fixed + a, b
        return fixed, cands

    files = list(files)
    if not files:
        return {"status": "nothing", "culprits": []}
    if (yield closure([])):
        return {"status": "not_mods", "culprits": []}
    fixed, cands = yield from reduce([], files)
    culprits = fixed + cands
    if fixed:
        if (yield closure(cands)):
            culprits = cands
        else:
            f2, c2 = yield from reduce(cands, fixed)
            culprits = f2 + c2
    chosen = set(culprits)
    extra = [f for f in closure(culprits) if f not in chosen]
    if extra and (yield closure(extra)):
        f2, c2 = yield from reduce([], extra)
        culprits = f2 + c2
    return {"status": "found", "culprits": culprits}


class ModBisectSession:
    def __init__(self, files, closure):
        self.files = list(files)
        self.closure = closure
        self._gen = mod_bisect_gen(self.files, closure)
        self.tests = 0
        self.result = None
        self.current = None
        try:
            self.current = next(self._gen)
        except StopIteration as e:
            self.result = e.value

    def estimated_total(self):
        n = max(1, len(self.files))
        return 1 + math.ceil(math.log2(n)) + 1

    def answer(self, problem):
        self.tests += 1
        try:
            self.current = self._gen.send(bool(problem))
        except StopIteration as e:
            self.current, self.result = None, e.value


def bisect_state_path(mods_dir):
    return os.path.join(os.path.dirname(os.path.normpath(mods_dir)), BISECT_STATE_FILE)


def bisect_save_state(mods_dir, originally_enabled):
    with open(bisect_state_path(mods_dir), "w", encoding="utf-8") as f:
        json.dump({"mods_dir": os.path.normpath(mods_dir), "enabled": list(originally_enabled)}, f)


def bisect_clear_state(mods_dir):
    try:
        os.remove(bisect_state_path(mods_dir))
    except OSError:
        pass


def bisect_restore(manager, originally_enabled):
    for name in originally_enabled:
        try:
            manager.set_enabled(name, True)
        except OSError:
            pass
    bisect_clear_state(manager.mods_dir)


def bisect_apply(manager, originally_enabled, enabled_set):
    enabled_set = set(enabled_set)
    for name in originally_enabled:
        manager.set_enabled(name, name in enabled_set)




class ServerPackDialog(tk.Toplevel):
    """Установка и обновление сборки сервера: показывает, что изменится, и синхронизирует файлы."""

    def __init__(self, app, auto_start=False, on_done=None, activate=False):
        super().__init__(app.root)
        self.app, self.on_done, self.auto_start, self.activate = app, on_done, auto_start, activate
        self.manifest, self.opt_vars, self.busy, self._reported = None, {}, False, False
        self.title("Сборка сервера")
        self.geometry("660x520")
        self.minsize(560, 420)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.head = tk.Label(self, text="Загрузка…", anchor="w", font=("Segoe UI Semibold", 13))
        self.head.pack(fill="x", padx=14, pady=(12, 2))
        self.info = tk.Label(self, text="", anchor="w", justify="left", fg="#7a8599", wraplength=620)
        self.info.pack(fill="x", padx=14)
        self.local = tk.Label(self, text="", anchor="w", justify="left", wraplength=620)
        self.local.pack(fill="x", padx=14, pady=(2, 6))
        self.opt_box = ttk.LabelFrame(self, text="Необязательные файлы (отметь, что установить)")
        self.log = tk.Text(self, height=10, state="disabled", bg="#242830", fg="#e1e4e8", font=("Consolas", 9),
                           relief="flat", wrap="word")
        self.log.pack(fill="both", expand=True, padx=14, pady=6)
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=14)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=14, pady=10)
        self.go_btn = ttk.Button(bar, text="📥 Установить", style="Accent.TButton", command=self._start, state="disabled")
        self.go_btn.pack(side="left")
        ttk.Button(bar, text="Закрыть", command=self._close).pack(side="right")
        app._async(self._fetch, self._loaded)

    def _fetch(self):
        status, data = self.app.api.modpack_manifest()
        if status != 200 or not data.get("success"):
            raise Exception(data.get("error", f"HTTP {status}"))
        return data.get("modpack")

    def _ui(self, fn):
        try:
            self.after(0, fn)
        except Exception:
            pass

    def _say(self, text):
        try:
            self.log.config(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.config(state="disabled")
        except tk.TclError:
            pass

    def _loaded(self, manifest, error):
        if not self.winfo_exists():
            return
        if error or not manifest:
            self.head.config(text="Сборка сервера недоступна")
            self.info.config(text=f"Не удалось получить сборку: {error}" if error else "Администратор ещё не настроил сборку.")
            return
        self.manifest = manifest
        total = sum(int(f.get("size") or 0) for f in manifest["files"])
        loader = manifest.get("loader", "vanilla")
        lv = f" {manifest.get('loader_version')}" if manifest.get("loader_version") else ""
        self.head.config(text=f"🧩 {manifest.get('name') or 'Сборка сервера'}")
        self.info.config(text=f"Minecraft {manifest.get('mc_version')} • {loader}{lv} • ревизия {manifest.get('revision')} • "
                              f"файлов: {len(manifest['files'])} • {total / 1048576:.1f} МБ")
        sp = self.app.config.get("server_pack") or {}
        pk = self.app._find_modpack(sp.get("pack_name", "")) if sp.get("pack_name") else None
        if pk:
            have = int(serverpack_load_state(self.app._modpack_dir(pk["name"])).get("revision") or 0)
            now = int(manifest.get("revision") or 0)
            self.local.config(text=(f"Установлена ревизия {have}: " + ("актуальна ✅" if have >= now else f"есть обновление до {now}")),
                              fg="#43b581" if have >= now else "#faa61a")
            self.go_btn.config(text="🔄 Проверить и обновить")
        else:
            self.local.config(text="Ещё не установлена", fg="#7a8599")
        optional = [f for f in manifest["files"] if f.get("optional")]
        if optional:
            self.opt_box.pack(fill="x", padx=14, pady=4, before=self.log)
            saved = (sp.get("optional") or {})
            for f in optional:
                v = tk.BooleanVar(value=bool(saved.get(f["path"])))
                self.opt_vars[f["path"]] = v
                ttk.Checkbutton(self.opt_box, text=f"{f.get('title') or f['path']}  ({f['path']}, {int(f.get('size') or 0) / 1048576:.1f} МБ)",
                                variable=v).pack(anchor="w", padx=8, pady=1)
        self.go_btn.config(state="normal")
        if self.auto_start:
            self._start()

    def _start(self):
        if self.busy or not self.manifest:
            return
        self.busy = True
        self.go_btn.config(state="disabled")
        choice = {p: bool(v.get()) for p, v in self.opt_vars.items()}
        sp = self.app.config.setdefault("server_pack", {})
        sp["optional"] = choice
        try:
            pk = self.app._ensure_server_pack_entry(self.manifest)
        except Exception as e:
            self.busy = False
            self.go_btn.config(state="normal")
            messagebox.showerror("Сборка сервера", f"Не удалось подготовить сборку: {e}", parent=self)
            return
        self._say("Сравниваю файлы…")
        threading.Thread(target=self._worker, args=(self.manifest, pk, choice), daemon=True).start()

    def _worker(self, manifest, pk, choice):
        app = self.app
        pack_dir = app._modpack_dir(pk["name"])
        try:
            state = serverpack_load_state(pack_dir)
            plan = serverpack_plan(manifest, pack_dir, state, choice)
            self._ui(lambda: self._say(f"Скачать: {len(plan['download'])} ({plan['download_bytes'] / 1048576:.1f} МБ), "
                                       f"оставить: {len(plan['keep'])}, убрать устаревшее: {len(plan['remove'])}"))
            for bad in plan["bad"]:
                self._ui(lambda b=bad: self._say(f"⚠ пропущен недопустимый путь: {b}"))
            if (plan["download"] or plan["remove"]) and os.path.isdir(os.path.join(pack_dir, "mods")) \
                    and app.config.get("auto_pack_backup", True) and state.get("files"):
                try:
                    mgr = app._pack_backup_mgr(pk)
                    mgr.create("server-pack", auto=True)
                    mgr.prune(int(app.config.get("pack_backup_keep", 5)))
                    self._ui(lambda: self._say("Сделан бэкап сборки перед обновлением"))
                except Exception as e:
                    self._ui(lambda: self._say(f"⚠ бэкап не сделан: {e}"))

            def progress(n, total, name):
                def upd():
                    try:
                        self.progress.config(maximum=max(1, total), value=n)
                    except tk.TclError:
                        return
                    self._say(f"[{n}/{total}] {name}")
                self._ui(upd)

            new_state, failed = serverpack_apply(manifest, pack_dir, plan, state,
                                                 lambda f, dest: serverpack_download(app.api, f, dest), progress)
            serverpack_save_state(pack_dir, new_state)
            self._ui(lambda: self._finished(pk, failed))
        except Exception as e:
            self._ui(lambda: self._finished(pk, [str(e)]))

    def _finished(self, pk, failed):
        self.busy = False
        try:
            self.go_btn.config(state="normal")
        except tk.TclError:
            return
        app = self.app
        app.refresh_modpacks()
        app.refresh_mods()
        if failed:
            self._say("Ошибки:\n" + "\n".join(f"  • {x}" for x in failed[:12]))
            messagebox.showwarning("Сборка сервера", "Не всё удалось скачать:\n\n" + "\n".join(failed[:8])
                                   + "\n\nНажми кнопку ещё раз, чтобы докачать недостающее.", parent=self)
            self._report(False)
            return
        self._say("✅ Готово")
        active = app.config.get("active_modpack", "") == pk["name"]
        if self.activate or active or messagebox.askyesno("Сборка сервера", "Сборка установлена. Сделать её активной?", parent=self):
            app._activate_modpack(pk["name"])
        self._report(True)
        self.destroy()

    def _report(self, ok):
        if not self._reported and self.on_done:
            self._reported = True
            self.on_done(ok)

    def _close(self):
        if self.busy and not messagebox.askyesno("Сборка сервера", "Идёт скачивание. Закрыть окно? Недокачанное можно будет докачать позже.", parent=self):
            return
        self._report(False)
        self.destroy()


class ModBisectWindow(tk.Toplevel):
    """Ищет мод, из-за которого вылетает игра: отключает половину модов и спрашивает, осталась ли проблема."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app, self.mgr = app, app.mod_manager
        self.title("Поиск виновного мода")
        self.geometry("720x600")
        self.minsize(620, 520)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.originally = [n for n, enabled in self.mgr.list_mods() if enabled]
        self.session, self.graph, self.closure = None, None, None
        self._await_start, self._launch_t, self._alive = False, 0.0, True

        self.intro = tk.Label(self, justify="left", anchor="nw", wraplength=680, fg="#e1e4e8", text=(
            "Как это работает:\n"
            "1. Убедись, что проблема (вылет, баг) сейчас воспроизводится.\n"
            "2. Лаунчер отключит часть модов и попросит запустить игру. Зависимости модов учитываются автоматически.\n"
            "3. После каждого запуска нажми, осталась ли проблема. Вылет при старте с той же ошибкой тоже считается проблемой.\n"
            "4. Через несколько шагов лаунчер назовёт виновника. В конце все моды вернутся на место, "
            "а виновника можно будет отключить одной кнопкой.\n\n"
            f"Включённых модов сейчас: {len(self.originally)}."))
        self.intro.pack(fill="x", padx=16, pady=(14, 6))
        self.step = tk.Label(self, text="", anchor="w", font=("Segoe UI Semibold", 12))
        self.step.pack(fill="x", padx=16, pady=(6, 2))
        self.desc = tk.Label(self, text="", anchor="w", justify="left", wraplength=680, fg="#7a8599")
        self.desc.pack(fill="x", padx=16)
        self.box = tk.Text(self, height=12, state="disabled", bg="#242830", fg="#e1e4e8", font=("Consolas", 9), relief="flat")
        self.box.pack(fill="both", expand=True, padx=16, pady=8)
        self.status = tk.Label(self, text="", anchor="w", fg="#faa61a")
        self.status.pack(fill="x", padx=16)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=16, pady=10)
        self.start_btn = ttk.Button(bar, text="🔍 Начать поиск", style="Accent.TButton", command=self._begin)
        self.start_btn.pack(side="left")
        self.run_btn = ttk.Button(bar, text="▶ Запустить игру", command=self._run_game)
        self.yes_btn = ttk.Button(bar, text="🔴 Проблема есть", command=lambda: self._answer(True))
        self.no_btn = ttk.Button(bar, text="🟢 Проблемы нет", command=lambda: self._answer(False))
        self.final_btn = ttk.Button(bar, text="⛔ Отключить найденное", command=self._disable_found)
        ttk.Button(bar, text="Закрыть", command=self._close).pack(side="right")
        self.stop_btn = ttk.Button(bar, text="⏹ Остановить", command=self._stop)
        if not self.originally:
            self.start_btn.config(state="disabled")
            self.status.config(text="В этой сборке нет включённых модов.")

    def _set_box(self, lines):
        self.box.config(state="normal")
        self.box.delete("1.0", "end")
        self.box.insert("end", "\n".join(lines))
        self.box.config(state="disabled")

    def _begin(self):
        app = self.app
        if app.installing or (app.process is not None and app.process.poll() is None):
            messagebox.showwarning("Поиск виновного мода", "Закрой запущенную игру и дождись конца установки.", parent=self)
            return
        self.start_btn.config(state="disabled")
        self.status.config(text="Анализирую моды…", fg="#faa61a")
        mc, det = detect_game_version_and_loader(app.version_var.get())
        loader = app.modloader_var.get()
        if loader == "vanilla":
            loader = det.lower() if det else ""
        mods_dir, names = self.mgr.mods_dir, list(self.originally)
        app._async(lambda: build_mod_graph(mods_dir, names, loader, mc), self._graph_ready)

    def _graph_ready(self, graph, error):
        if not self._alive:
            return
        if error:
            self.start_btn.config(state="normal")
            self.status.config(text=f"Не удалось проанализировать моды: {error}", fg="#ed4245")
            return
        self.graph, self.closure = graph, make_closure(graph)
        self.session = ModBisectSession(self.originally, self.closure)
        try:
            bisect_save_state(self.mgr.mods_dir, self.originally)
        except OSError as e:
            self.status.config(text=f"Не удалось сохранить состояние: {e}", fg="#ed4245")
            return
        self.intro.pack_forget()
        self.start_btn.pack_forget()
        for b in (self.run_btn, self.yes_btn, self.no_btn, self.stop_btn):
            b.pack(side="left", padx=(0, 6))
        self.status.config(text="")
        self._apply_current()
        self._poll()

    def _apply_current(self):
        s = self.session
        cur = s.current or []
        bisect_apply(self.mgr, self.originally, cur)
        self.app.refresh_mods()
        self.step.config(text=f"Шаг {s.tests + 1} из ~{s.estimated_total()}")
        if not cur:
            self.desc.config(text="Все моды отключены. Запусти игру и проверь, остаётся ли проблема без модов.")
            self._set_box(["(ни одного мода не включено)"])
        else:
            self.desc.config(text=f"Включено модов: {len(cur)} из {len(self.originally)} (вместе с нужными им библиотеками). "
                                  "Запусти игру и проверь, осталась ли проблема.")
            self._set_box(cur)

    def _run_game(self):
        self._await_start, self._launch_t = True, time.time()
        self.app.launch_only()

    def _poll(self):
        if not self._alive:
            return
        app = self.app
        running = app.process is not None and app.process.poll() is None
        if running:
            self._await_start = False
        elif self._await_start and time.time() - self._launch_t > 90 and not app.installing:
            self._await_start = False
        busy = running or self._await_start or app.installing
        state = "disabled" if busy else "normal"
        for b in (self.run_btn, self.yes_btn, self.no_btn):
            b.config(state=state)
        if self.session and self.session.result is None:
            self.status.config(text="Игра запущена… ответь после её закрытия." if busy else "", fg="#faa61a")
            self.after(1000, self._poll)

    def _answer(self, problem):
        self.session.answer(problem)
        if self.session.result is not None:
            self._finish()
        else:
            self._apply_current()

    def _finish(self):
        res = self.session.result
        bisect_restore(self.mgr, self.originally)
        self.app.refresh_mods()
        for b in (self.run_btn, self.yes_btn, self.no_btn, self.stop_btn):
            b.pack_forget()
        self.status.config(text="")
        self.found = res.get("culprits", [])
        if res["status"] == "not_mods":
            self.step.config(text="Дело не в модах")
            self.desc.config(text="Проблема воспроизводится и совсем без модов. Причину стоит искать в Java, драйверах видеокарты, "
                                  "настройках, ресурспаках или самой версии игры. Все моды возвращены на место.")
            self._set_box([])
        elif res["status"] == "found" and self.found:
            chosen = set(self.found)
            dependents = [f for f in self.originally if f not in chosen and chosen & set(self.closure([f]))]
            self.dependents = dependents
            self.step.config(text="🎯 Найден вероятный виновник")
            self.desc.config(text="Проблема пропадает без этого набора. Все моды возвращены на место. "
                                  "Отключи найденное и проверь игру: так ты убедишься, что дело в нём.")
            lines = ["Виновник:"] + [f"  • {f}" for f in self.found]
            if len(self.found) > 1:
                lines.append("(вылет случается только когда включены вместе все эти моды)")
            if dependents:
                lines += ["", "От него зависят (без него не запустятся, их тоже придётся отключить):"] + [f"  • {f}" for f in dependents]
            self._set_box(lines)
            self.final_btn.pack(side="left")
        else:
            self.step.config(text="Нечего проверять")
            self.desc.config(text="В сборке нет включённых модов.")
            self._set_box([])
        try:
            bisect_clear_state(self.mgr.mods_dir)
        except OSError:
            pass

    def _disable_found(self):
        names = list(dict.fromkeys(self.found + getattr(self, "dependents", [])))
        for n in names:
            try:
                self.mgr.set_enabled(n, False)
            except OSError as e:
                messagebox.showerror("Поиск виновного мода", f"Не удалось отключить {n}: {e}", parent=self)
                break
        self.app.refresh_mods()
        self.final_btn.config(state="disabled")
        self.status.config(text=f"Отключено файлов: {len(names)}. Включить обратно можно на вкладке «Моды».", fg="#43b581")

    def _stop(self):
        if self.session and self.session.result is None:
            if not messagebox.askyesno("Поиск виновного мода", "Остановить поиск и вернуть все моды как были?", parent=self):
                return
            bisect_restore(self.mgr, self.originally)
            self.app.refresh_mods()
        self._close(force=True)

    def _close(self, force=False):
        if not force and self.session and self.session.result is None:
            if not messagebox.askyesno("Поиск виновного мода", "Поиск не закончен. Вернуть все моды как были и закрыть окно?", parent=self):
                return
            bisect_restore(self.mgr, self.originally)
            self.app.refresh_mods()
        self._alive = False
        self.destroy()


class ModUpdateWindow(tk.Toplevel):
    LOADER_FILTERS = {"fabric": ["fabric"], "forge": ["forge"], "neoforge": ["neoforge"], "quilt": ["quilt", "fabric"]}

    def __init__(self, app):
        super().__init__(app.root)
        self.app, self.busy, self.updates = app, False, []
        self.title("Обновление модов (Modrinth)")
        self.geometry("960x560")
        self.minsize(780, 420)
        self.transient(app.root)
        mc, det = detect_game_version_and_loader(app.version_var.get())
        loader = app.modloader_var.get()
        if loader == "vanilla":
            loader = det.lower() if det else ""
        self.loader, self.mc = loader, mc
        self.client = ModrinthClient()

        pack = app.config.get("active_modpack", "") or "Основная"
        tk.Label(self, text=f"Сборка: {pack}  •  Minecraft: {mc or 'не определён'}  •  "
                            f"Загрузчик: {self.loader or 'не выбран'}", anchor="w", fg="#7a8599"
                 ).pack(fill="x", padx=14, pady=(12, 4))

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=14, pady=6)
        cols = ("mod", "cur", "new", "file")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        for col, text, width, anchor, stretch in (("mod", "Мод", 230, "w", False), ("cur", "Сейчас", 170, "w", False),
                                                  ("new", "Станет", 190, "w", False), ("file", "Файл", 300, "w", True)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=14, pady=(4, 6))
        ttk.Button(bar, text="⬆ Обновить выбранные", style="Accent.TButton",
                   command=lambda: self.apply(selected_only=True)).pack(side="left")
        ttk.Button(bar, text="⬆ Обновить все", command=lambda: self.apply(selected_only=False)).pack(side="left", padx=8)
        ttk.Button(bar, text="🔄 Проверить заново", command=self.check).pack(side="left")
        ttk.Button(bar, text="Закрыть", command=self.destroy).pack(side="right")

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=14, pady=(0, 4))
        self.status = tk.Label(self, text="", anchor="w", fg="#7a8599")
        self.status.pack(fill="x", padx=14, pady=(0, 12))
        self.check()

    def _set(self, text, error=False):
        if self.winfo_exists():
            self.status.configure(text=text, fg="#ed4245" if error else "#7a8599")

    def _bg(self, work, done):
        self.busy = True

        def runner():
            try:
                result, error = work(), None
            except Exception as e:
                result, error = None, e
            try:
                self.after(0, lambda: self.winfo_exists() and finish(result, error))
            except Exception:
                pass

        def finish(result, error):
            self.busy = False
            done(result, error)
        threading.Thread(target=runner, daemon=True).start()

    def check(self):
        if self.busy:
            return
        loaders = self.LOADER_FILTERS.get(self.loader, [])
        if not loaders or not self.mc:
            self._set("Выберите версию Minecraft и загрузчик на вкладке «Игра» — без них нельзя подобрать обновления.", True)
            return
        mods_dir = self.app.mod_manager.mods_dir
        self._set("Проверяю моды на Modrinth…")

        def done(result, error):
            if error:
                self._set(f"Ошибка проверки: {error}", True)
                return
            self.updates, total, unknown = result
            self.tree.delete(*self.tree.get_children())
            for i, u in enumerate(self.updates):
                tag = " (бета/альфа)" if u["new_type"] != "release" else ""
                self.tree.insert("", "end", iid=str(i), values=(u["title"], u["current"], u["new"] + tag,
                                                                  f"{u['file']} → {u['file_obj'].get('filename', '')}"))
            tail = f" • не найдено на Modrinth: {unknown}" if unknown else ""
            if self.updates:
                self._set(f"Доступно обновлений: {len(self.updates)} из {total} модов{tail}")
            else:
                self._set(f"✅ Всё актуально: проверено модов {total}{tail}")
        self._bg(lambda: find_mod_updates(self.client, mods_dir, loaders, self.mc), done)

    def apply(self, selected_only):
        if self.busy:
            return
        if selected_only:
            items = [self.updates[int(i)] for i in self.tree.selection()]
        else:
            items = list(self.updates)
        if not items:
            self._set("Нечего обновлять: выберите строки в списке" if selected_only else "Обновлений нет", True)
            return
        proc = getattr(self.app, "process", None)
        if proc is not None and proc.poll() is None:
            messagebox.showwarning("Обновление", "Закройте игру: файлы модов сейчас используются.", parent=self)
            return
        if not messagebox.askyesno("Обновление", f"Обновить модов: {len(items)}?", parent=self):
            return
        mods_dir = self.app.mod_manager.mods_dir
        self.progress["value"] = 0
        self._set("Создаю бэкап сборки…")

        def work():
            self.app.auto_pack_backup("update-mods")
            ok, failed = [], []
            for u in items:
                new_path = None
                try:
                    def prog(d, t, n=u["title"]):
                        self.after(0, lambda: self._on_progress(n, d, t))
                    new_path = modrinth_download_file(u["file_obj"], mods_dir, prog)
                    old = os.path.join(mods_dir, u["file"])
                    if os.path.abspath(old) != os.path.abspath(new_path) and os.path.isfile(old):
                        os.remove(old)
                    ok.append(u["title"])
                except Exception as e:
                    old = os.path.join(mods_dir, u["file"])
                    if new_path and os.path.abspath(old) != os.path.abspath(new_path) and os.path.isfile(old):
                        try:
                            os.remove(new_path)
                        except OSError:
                            pass
                    failed.append(f"{u['title']}: {e}")
            issues, _n = ModChecker(mods_dir, self.loader, self.mc).scan()
            return ok, failed, sum(1 for i in issues if i["level"] == "error")

        def done(result, error):
            self.progress["value"] = 0
            self.app.refresh_mods()
            if error:
                self._set(f"Ошибка обновления: {error}", True)
                return
            ok, failed, problems = result
            parts = [f"Обновлено: {len(ok)}"]
            if failed:
                parts.append("не удалось: " + "; ".join(failed))
            if problems:
                parts.append(f"⚠ после обновления найдено ошибок совместимости: {problems} (откройте «Проверить моды»)")
            self._set(" • ".join(parts), error=bool(failed))
            self.check()
        self._bg(work, done)

    def _on_progress(self, name, done, total):
        if self.winfo_exists():
            self.progress["value"] = done * 100 / total
            self.status.configure(text=f"Скачиваю {name}: {_fmt_mb(done, total)}", fg="#7a8599")


SHOP_STATUS_TEXT = {"pending": "⏳ ждёт выдачи", "applied": "✅ выдано",
                    "failed": "❌ ошибка", "refunded": "↩ возврат"}
SHOP_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def shop_duration_text(days):
    return "навсегда" if not days else f"{int(days)} дн."


class ServerEditDialog(tk.Toplevel):
    """Добавление и изменение сервера: название, адрес, версия или диапазон версий."""

    def __init__(self, app, server=None, on_save=None):
        super().__init__(app.root)
        self.app, self.on_save = app, on_save
        server = server or {}
        self.title("Изменить сервер" if server else "Новый сервер")
        self.transient(app.root)
        self.resizable(False, False)
        self.grab_set()

        is_range = bool(server.get("version_from") or server.get("version_to"))
        self.name_var = tk.StringVar(value=server.get("name", ""))
        self.addr_var = tk.StringVar(value=server.get("address", ""))
        self.range_var = tk.BooleanVar(value=is_range)
        self.v1_var = tk.StringVar(value=server.get("version_from") or server.get("version", ""))
        self.v2_var = tk.StringVar(value=server.get("version_to", ""))
        versions = list(getattr(app, "_all_versions", []) or [])

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Название:").grid(row=0, column=0, sticky="w", pady=4)
        name_entry = ttk.Entry(frm, textvariable=self.name_var, width=34)
        name_entry.grid(row=0, column=1, columnspan=2, sticky="w", padx=6)
        name_entry.focus_set()

        ttk.Label(frm, text="Адрес (ip:порт):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.addr_var, width=34).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Button(frm, text="🔍 Определить версию", command=self._detect).grid(row=1, column=2, padx=4)

        self.v1_label = ttk.Label(frm, text="Версия:")
        self.v1_label.grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(frm, textvariable=self.v1_var, width=18, values=versions).grid(row=2, column=1, sticky="w", padx=6)

        self.v2_label = ttk.Label(frm, text="до версии:")
        self.v2_label.grid(row=3, column=0, sticky="w", pady=4)
        self.v2_combo = ttk.Combobox(frm, textvariable=self.v2_var, width=18, values=versions)
        self.v2_combo.grid(row=3, column=1, sticky="w", padx=6)

        ttk.Checkbutton(frm, text="Диапазон версий (от и до)", variable=self.range_var,
                        command=self._toggle_range).grid(row=4, column=1, columnspan=2, sticky="w", padx=6, pady=(2, 6))
        ttk.Label(frm, foreground="#7a8599", justify="left",
                  text="Версию можно не указывать: тогда игра запустится в той версии, что выбрана сейчас.\n"
                       "Диапазон нужен серверам с ViaVersion/ViaBackwards: если ваша версия\n"
                       "в него входит, она и запустится, иначе будет взята новейшая из диапазона.").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="e")
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="Сохранить", style="Accent.TButton", command=self._save).pack(side="right", padx=4)
        self._toggle_range()

    def _toggle_range(self):
        if self.range_var.get():
            self.v1_label.config(text="Версия от:")
            self.v2_label.grid()
            self.v2_combo.grid()
        else:
            self.v1_label.config(text="Версия:")
            self.v2_label.grid_remove()
            self.v2_combo.grid_remove()

    def _parse_address(self):
        address = self.addr_var.get().strip()
        host, _, port_str = address.partition(":")
        if not re.fullmatch(r"[A-Za-z0-9.\-_]+", host or "") or (port_str and not port_str.isdigit()):
            return None, None, None
        port = int(port_str) if port_str else 25565
        if not 1 <= port <= 65535:
            return None, None, None
        return address, host, port

    def _detect(self):
        address, host, port = self._parse_address()
        if not address:
            messagebox.showwarning("Сервер", "Сначала впиши адрес в формате ip:порт.", parent=self)
            return

        def work():
            info = ping_minecraft_server(host, port)
            self.after(0, lambda: self._detected(info))
        threading.Thread(target=work, daemon=True).start()

    def _detected(self, info):
        if not self.winfo_exists():
            return
        if not info:
            messagebox.showinfo("Сервер", "Сервер не ответил. Проверь адрес или впиши версию вручную.", parent=self)
            return
        m = re.search(r"\d+\.\d+(?:\.\d+)?", info["version"])
        if m:
            self.v1_var.set(m.group(0))
            self.range_var.set(False)
            self._toggle_range()
        messagebox.showinfo("Сервер", f"Версия сервера: {info['version'] or 'неизвестна'}\n"
                                      f"Игроки: {info['online']}/{info['max']}", parent=self)

    def _save(self):
        address, host, port = self._parse_address()
        if not address:
            messagebox.showwarning("Сервер", "Адрес должен быть в формате ip:порт (или домен).", parent=self)
            return
        v1, v2 = self.v1_var.get().strip(), self.v2_var.get().strip()
        for v in (v1, v2 if self.range_var.get() else ""):
            if v and mc_version_key(v) is None:
                messagebox.showwarning("Сервер", f"Версия «{v}» непонятна. Пример: 1.20.4 или 26.2.", parent=self)
                return
        data = {"name": self.name_var.get().strip() or address, "address": address}
        if self.range_var.get() and v1 and v2 and v1 != v2:
            if mc_version_key(v1) > mc_version_key(v2):
                v1, v2 = v2, v1
            data["version_from"], data["version_to"] = v1, v2
        elif v1 or v2:
            data["version"] = v1 or v2
        if self.on_save:
            self.on_save(data)
        self.destroy()


class ModpackCreateDialog(tk.Toplevel):
    LOADERS = (("vanilla", "Без загрузчика"), ("fabric", "Fabric"), ("forge", "Forge"),
               ("neoforge", "NeoForge"), ("quilt", "Quilt"))

    def __init__(self, app):
        super().__init__(app.root)
        self.app, self._token = app, 0
        self.title("Новая сборка")
        self.transient(app.root)
        self.resizable(False, False)
        self.grab_set()

        self.name_var = tk.StringVar()
        self.mc_var = tk.StringVar(value=app.version_var.get().strip())
        self.loader_var = tk.StringVar(value=app.modloader_var.get() or "vanilla")
        self.lv_var = tk.StringVar()
        self._pending_lv = app._current_loader_version() if self.loader_var.get() != "vanilla" else ""

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Название:").grid(row=0, column=0, sticky="w", pady=4)
        name_entry = ttk.Entry(frm, textvariable=self.name_var, width=34)
        name_entry.grid(row=0, column=1, columnspan=2, sticky="w", padx=6)
        name_entry.focus_set()

        ttk.Label(frm, text="Версия Minecraft:").grid(row=1, column=0, sticky="w", pady=4)
        self.mc_combo = ttk.Combobox(frm, textvariable=self.mc_var, width=31,
                                     values=list(getattr(app, "_all_versions", []) or []))
        self.mc_combo.grid(row=1, column=1, columnspan=2, sticky="w", padx=6)
        self.mc_combo.bind("<<ComboboxSelected>>", lambda e: self._load_loader_versions())
        self.mc_combo.bind("<FocusOut>", lambda e: self._load_loader_versions())

        ttk.Label(frm, text="Загрузчик:").grid(row=2, column=0, sticky="nw", pady=4)
        radios = ttk.Frame(frm)
        radios.grid(row=2, column=1, columnspan=2, sticky="w", padx=6)
        for i, (value, text) in enumerate(self.LOADERS):
            ttk.Radiobutton(radios, text=text, variable=self.loader_var, value=value,
                            command=self._load_loader_versions).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 10))

        ttk.Label(frm, text="Версия загрузчика:").grid(row=3, column=0, sticky="w", pady=4)
        self.lv_combo = ttk.Combobox(frm, textvariable=self.lv_var, width=31, state="readonly")
        self.lv_combo.grid(row=3, column=1, columnspan=2, sticky="w", padx=6)

        row = ttk.Frame(frm)
        row.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(row, text="Отмена", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Создать", style="Accent.TButton", command=self._create).pack(side="right")
        self.bind("<Return>", lambda e: self._create())
        self._load_loader_versions()

    def _load_loader_versions(self):
        loader, mc = self.loader_var.get(), self.mc_var.get().strip()
        self._token += 1
        token = self._token
        if loader == "vanilla" or not mc:
            self.lv_combo.config(values=[], state="disabled")
            self.lv_var.set("")
            return
        self.lv_combo.config(values=["Загрузка..."], state="readonly")
        self.lv_var.set("Загрузка...")
        core = self.app.core

        def fetch():
            try:
                if loader == "forge":
                    versions = core.get_forge_versions(mc)
                elif loader == "fabric":
                    versions = core.get_fabric_loader_versions(mc)
                else:
                    versions = core.get_loader_versions_generic(loader, mc)
            except Exception:
                versions = []

            def apply():
                if token != self._token or not self.winfo_exists():
                    return
                if not versions:
                    self.lv_combo.config(values=["Недоступно для этой версии"])
                    self.lv_var.set("Недоступно для этой версии")
                    return
                self.lv_combo.config(values=versions)
                pending, self._pending_lv = self._pending_lv, ""
                self.lv_var.set(pending if pending in versions else versions[0])
            try:
                self.after(0, apply)
            except Exception:
                pass
        threading.Thread(target=fetch, daemon=True).start()

    def _create(self):
        app = self.app
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Ошибка", "Введите название сборки.", parent=self)
            return
        if name.lower().startswith("основная") or app._find_modpack(name) or os.path.exists(app._modpack_dir(name)):
            messagebox.showerror("Ошибка", "Сборка с таким названием уже есть.", parent=self)
            return
        loader, mc = self.loader_var.get(), self.mc_var.get().strip()
        lv = self.lv_var.get().strip()
        if lv in ("Загрузка...", "Недоступно для этой версии"):
            lv = ""
        if loader != "vanilla" and not mc:
            messagebox.showerror("Ошибка", "Укажите версию Minecraft.", parent=self)
            return
        if loader != "vanilla" and not lv:
            messagebox.showerror("Ошибка", "Выберите версию загрузчика (или подождите, пока она загрузится).",
                                 parent=self)
            return
        self.destroy()
        app._create_modpack(name, mc, loader, lv)


class ModCheckWindow(tk.Toplevel):
    ICONS = {"error": "⛔", "warn": "⚠", "info": "ℹ"}

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Проверка модов")
        self.geometry("960x540")
        self.minsize(760, 400)
        self.transient(app.root)
        mc, det = detect_game_version_and_loader(app.version_var.get())
        loader = app.modloader_var.get()
        if loader == "vanilla":
            loader = det.lower() if det else ""
        self.loader, self.mc, self.busy = loader, mc, False

        pack = app.config.get("active_modpack", "") or "Основная"
        info = (f"Сборка: {pack}  •  Minecraft: {mc or 'не определён'}  •  Загрузчик: "
                f"{self.loader or 'не выбран (проверка загрузчика пропущена)'}")
        tk.Label(self, text=info, anchor="w", fg="#7a8599").pack(fill="x", padx=14, pady=(12, 4))

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=14, pady=6)
        self.tree = ttk.Treeview(mid, columns=("lvl", "file", "text"), show="headings", selectmode="browse")
        for col, text, width, anchor, stretch in (("lvl", "", 36, "center", False),
                                                  ("file", "Файл", 240, "w", False),
                                                  ("text", "Проблема", 620, "w", True)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch)
        self.tree.tag_configure("error", foreground="#ed4245")
        self.tree.tag_configure("warn", foreground="#faa61a")
        self.tree.tag_configure("info", foreground="#7a8599")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_full())

        self.detail = tk.Label(self, text="", anchor="nw", justify="left", wraplength=900, fg="#7a8599", height=3)
        self.detail.pack(fill="x", padx=14)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=14, pady=(4, 6))
        ttk.Button(bar, text="⛔ Отключить выбранный файл", command=self.disable_selected).pack(side="left")
        ttk.Button(bar, text="🔄 Перепроверить", command=self.run).pack(side="left", padx=8)
        ttk.Button(bar, text="📂 Папка модов",
                   command=lambda: app._open_folder(app.mod_manager.mods_dir)).pack(side="left")
        ttk.Button(bar, text="Закрыть", command=self.destroy).pack(side="right")

        self.status = tk.Label(self, text="", anchor="w", fg="#7a8599")
        self.status.pack(fill="x", padx=14, pady=(0, 12))
        self.run()

    def _show_full(self):
        sel = self.tree.selection()
        self.detail.configure(text=self.tree.item(sel[0], "values")[2] if sel else "")

    def run(self):
        if self.busy:
            return
        self.busy = True
        self.status.configure(text="Проверяю…", fg="#7a8599")
        checker = ModChecker(self.app.mod_manager.mods_dir, self.loader, self.mc)

        def runner():
            try:
                result, error = checker.scan(), None
            except Exception as e:
                result, error = None, e
            try:
                self.after(0, lambda: self.winfo_exists() and self._done(result, error))
            except Exception:
                pass
        threading.Thread(target=runner, daemon=True).start()

    def _done(self, result, error):
        self.busy = False
        if error:
            self.status.configure(text=f"Ошибка проверки: {error}", fg="#ed4245")
            return
        issues, total = result
        self.tree.delete(*self.tree.get_children())
        for it in issues:
            self.tree.insert("", "end", values=(self.ICONS[it["level"]], it["file"], it["text"]), tags=(it["level"],))
        errors = sum(1 for i in issues if i["level"] == "error")
        warns = sum(1 for i in issues if i["level"] == "warn")
        if errors or warns:
            self.status.configure(text=f"Проверено модов: {total} • ошибок: {errors} • предупреждений: {warns}",
                                  fg="#ed4245" if errors else "#faa61a")
        else:
            self.status.configure(text=f"✅ Проверено модов: {total} — проблем не найдено", fg="#43b581")
        self.detail.configure(text="")

    def disable_selected(self):
        sel = self.tree.selection()
        if not sel:
            self.status.configure(text="Выберите строку в списке", fg="#ed4245")
            return
        fn = self.tree.item(sel[0], "values")[1]
        if fn.lower().endswith(".jar"):
            try:
                self.app.mod_manager.set_enabled(fn, False)
            except Exception as e:
                self.status.configure(text=f"Не удалось отключить: {e}", fg="#ed4245")
                return
            self.app.refresh_mods()
            self.run()


class ModpackBackupManager:
    PARTS = ("mods", "config")
    REASONS = {"manual": "вручную", "modrinth": "перед установкой с Modrinth",
               "delete-mod": "перед удалением мода", "update-mods": "перед обновлением модов", "before-restore": "перед откатом",
               "server-pack": "перед обновлением сборки сервера"}

    def __init__(self, pack_dir, backup_dir):
        self.pack_dir, self.backup_dir = pack_dir, backup_dir

    def _iter_files(self):
        for part in self.PARTS:
            base = os.path.join(self.pack_dir, part)
            for root_dir, _dirs, files in os.walk(base):
                for fn in files:
                    full = os.path.join(root_dir, fn)
                    yield full, os.path.relpath(full, self.pack_dir).replace(os.sep, "/")

    def fingerprint(self):
        import hashlib
        h = hashlib.sha1()
        for full, rel in sorted(self._iter_files(), key=lambda x: x[1]):
            try:
                st = os.stat(full)
            except OSError:
                continue
            h.update(f"{rel}|{st.st_size}|{int(st.st_mtime)}\n".encode("utf-8"))
        return h.hexdigest()

    @staticmethod
    def _comment(path):
        try:
            with zipfile.ZipFile(path) as zf:
                return zf.comment.decode("utf-8", "replace")
        except Exception:
            return ""

    def list(self):
        items = []
        if not os.path.isdir(self.backup_dir):
            return items
        for fn in os.listdir(self.backup_dir):
            if not fn.endswith(".zip"):
                continue
            m = re.match(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})__([a-z0-9-]+?)(?:-\d+)?\.zip$", fn)
            if not m:
                continue
            path = os.path.join(self.backup_dir, fn)
            try:
                when = datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
                size = os.path.getsize(path)
            except Exception:
                continue
            items.append({"path": path, "name": fn, "time": when, "reason": m.group(2), "size": size})
        items.sort(key=lambda i: (i["time"], i["name"]), reverse=True)
        return items

    def create(self, reason="manual", auto=False):
        files = list(self._iter_files())
        if not files:
            return None
        fp = self.fingerprint()
        if auto:
            latest = self.list()
            if latest and self._comment(latest[0]["path"]) == fp:
                return None
        os.makedirs(self.backup_dir, exist_ok=True)
        safe_reason = re.sub(r"[^a-z0-9-]", "", reason.lower()) or "manual"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path, n = os.path.join(self.backup_dir, f"{stamp}__{safe_reason}.zip"), 2
        while os.path.exists(path):
            path, n = os.path.join(self.backup_dir, f"{stamp}__{safe_reason}-{n}.zip"), n + 1
        tmp = path + ".part"
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for full, rel in files:
                    comp = zipfile.ZIP_STORED if rel.lower().endswith((".jar", ".disabled")) else zipfile.ZIP_DEFLATED
                    try:
                        zf.write(full, rel, compress_type=comp)
                    except FileNotFoundError:
                        continue
                zf.comment = fp.encode("utf-8")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return path

    def prune(self, keep):
        autos = [i for i in self.list() if i["reason"] != "manual"]
        for item in autos[max(1, int(keep)):]:
            try:
                os.remove(item["path"])
            except OSError:
                pass

    def delete(self, path):
        if os.path.abspath(os.path.dirname(path)) == os.path.abspath(self.backup_dir) and os.path.isfile(path):
            os.remove(path)

    def restore(self, path):
        self.create("before-restore", auto=True)
        tmp = os.path.join(self.pack_dir, ".restore_tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        tmp_real = os.path.realpath(tmp)
        try:
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    top = info.filename.split("/", 1)[0]
                    if top not in self.PARTS:
                        continue
                    target = os.path.realpath(os.path.join(tmp, info.filename))
                    if target != tmp_real and not target.startswith(tmp_real + os.sep):
                        raise Exception(f"Небезопасный путь в архиве: {info.filename}")
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src_f, open(target, "wb") as out_f:
                        shutil.copyfileobj(src_f, out_f)
            for part in self.PARTS:
                dst, src = os.path.join(self.pack_dir, part), os.path.join(tmp, part)
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                if os.path.isdir(src):
                    shutil.move(src, dst)
            os.makedirs(os.path.join(self.pack_dir, "mods"), exist_ok=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ModpackBackupWindow(tk.Toplevel):
    def __init__(self, app, title, manager, on_restored=None):
        super().__init__(app.root)
        self.app, self.mgr, self.on_restored, self.busy = app, manager, on_restored, False
        self.title(title)
        self.geometry("720x420")
        self.minsize(600, 320)
        self.transient(app.root)
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=14, pady=6)
        self.tree = ttk.Treeview(mid, columns=("date", "reason", "size"), show="headings", selectmode="browse")
        for col, text, width, anchor in (("date", "Дата", 160, "w"), ("reason", "Причина", 360, "w"),
                                         ("size", "Размер", 90, "e")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "reason"))
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=14, pady=(4, 6))
        ttk.Button(bar, text="➕ Создать сейчас", style="Accent.TButton", command=self.create).pack(side="left")
        ttk.Button(bar, text="↩ Откатить", command=self.restore).pack(side="left", padx=8)
        ttk.Button(bar, text="🗑️ Удалить", command=self.delete).pack(side="left")
        ttk.Button(bar, text="Закрыть", command=self.destroy).pack(side="right")
        self.status = tk.Label(self, text="", anchor="w", fg="#7a8599")
        self.status.pack(fill="x", padx=14, pady=(0, 12))
        self.reload()

    def _set(self, text, error=False):
        if self.winfo_exists():
            self.status.configure(text=text, fg="#ed4245" if error else "#7a8599")

    def reload(self):
        self.tree.delete(*self.tree.get_children())
        self._items = self.mgr.list()
        for i, it in enumerate(self._items):
            self.tree.insert("", "end", iid=str(i), values=(
                it["time"].strftime("%d.%m.%Y %H:%M:%S"),
                ModpackBackupManager.REASONS.get(it["reason"], it["reason"]),
                f"{it['size'] / 1048576:.1f} МБ"))
        self._set(f"Копий: {len(self._items)}" if self._items else "Бэкапов пока нет")

    def _selected(self):
        sel = self.tree.selection()
        return self._items[int(sel[0])] if sel else None

    def _bg(self, work, done):
        if self.busy:
            return
        self.busy = True

        def runner():
            try:
                result, error = work(), None
            except Exception as e:
                result, error = None, e
            try:
                self.after(0, lambda: self.winfo_exists() and finish(result, error))
            except Exception:
                pass

        def finish(result, error):
            self.busy = False
            done(result, error)
        threading.Thread(target=runner, daemon=True).start()

    def create(self):
        self._set("Создаю бэкап…")

        def done(result, error):
            if error:
                self._set(f"Ошибка: {error}", True)
            elif result is None:
                self._set("Нечего сохранять: папки mods и config пусты", True)
            else:
                self._set("Бэкап создан")
            self.reload() if self.winfo_exists() else None
        self._bg(lambda: self.mgr.create("manual"), done)

    def restore(self):
        it = self._selected()
        if not it:
            self._set("Выберите бэкап из списка", True)
            return
        proc = getattr(self.app, "process", None)
        if self.app.installing or (proc is not None and proc.poll() is None):
            messagebox.showwarning("Откат", "Закройте игру и дождитесь окончания установки.", parent=self)
            return
        if not messagebox.askyesno("Откат", "Заменить текущие mods и config содержимым выбранного бэкапа?", parent=self):
            return
        self._set("Откатываю…")

        def done(result, error):
            if error:
                self._set(f"Не удалось откатить: {error}", True)
            else:
                self._set("Готово: сборка возвращена к выбранному состоянию")
                if self.on_restored:
                    self.on_restored()
            self.reload() if self.winfo_exists() else None
        self._bg(lambda: self.mgr.restore(it["path"]), done)

    def delete(self):
        it = self._selected()
        if not it:
            self._set("Выберите бэкап из списка", True)
            return
        if not messagebox.askyesno("Удаление", "Удалить выбранный бэкап?", parent=self):
            return
        try:
            self.mgr.delete(it["path"])
        except Exception as e:
            self._set(f"Не удалось удалить: {e}", True)
            return
        self.reload()


class WorldBackupManager:
    def __init__(self, minecraft_dir):
        self.set_dir(minecraft_dir)

    def set_dir(self, minecraft_dir):
        self.saves_dir = os.path.join(minecraft_dir, "saves")
        self.backup_dir = os.path.join(minecraft_dir, "backups")

    def list_worlds(self):
        if not os.path.isdir(self.saves_dir):
            return []
        return sorted(d for d in os.listdir(self.saves_dir)
                      if os.path.isfile(os.path.join(self.saves_dir, d, "level.dat")))

    def list_backups(self, world=None):
        if not os.path.isdir(self.backup_dir):
            return []
        items = []
        for name in os.listdir(self.backup_dir):
            if not name.endswith(".zip") or "__" not in name:
                continue
            owner = name[:-4].rsplit("__", 1)[0]
            if world is not None and owner != world:
                continue
            path = os.path.join(self.backup_dir, name)
            items.append({"world": owner, "path": path, "mtime": os.path.getmtime(path),
                          "size": os.path.getsize(path)})
        items.sort(key=lambda i: i["mtime"], reverse=True)
        return items

    def world_mtime(self, world):
        path = os.path.join(self.saves_dir, world, "level.dat")
        return os.path.getmtime(path) if os.path.exists(path) else 0

    def create(self, world, progress=None):
        src = os.path.join(self.saves_dir, world)
        if not os.path.isdir(src):
            raise Exception(f"Мир не найден: {world}")
        os.makedirs(self.backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(self.backup_dir, f"{world}__{stamp}.zip")
        tmp = dest + ".part"
        files = []
        for root_dir, _dirs, names in os.walk(src):
            for n in names:
                if n != "session.lock":
                    files.append(os.path.join(root_dir, n))
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for i, full in enumerate(files, 1):
                try:
                    zf.write(full, os.path.join(world, os.path.relpath(full, src)))
                except OSError:
                    continue
                if progress and i % 25 == 0:
                    progress(i, len(files))
        os.replace(tmp, dest)
        return dest

    def prune(self, world, keep):
        for old in self.list_backups(world)[max(1, keep):]:
            try:
                os.remove(old["path"])
            except OSError:
                pass

    def restore(self, backup_path):
        owner = os.path.basename(backup_path)[:-4].rsplit("__", 1)[0]
        target = os.path.join(self.saves_dir, owner)
        saved_as = None
        if os.path.exists(target):
            saved_as = f"{owner}_до_восстановления_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            os.replace(target, os.path.join(self.saves_dir, saved_as))
        base = os.path.realpath(self.saves_dir)
        with zipfile.ZipFile(backup_path) as zf:
            for member in zf.infolist():
                dest = os.path.realpath(os.path.join(self.saves_dir, member.filename))
                if not dest.startswith(base + os.sep):
                    raise Exception("Архив содержит небезопасные пути")
            zf.extractall(self.saves_dir)
        return owner, saved_as

    def delete(self, backup_path):
        os.remove(backup_path)


class ModManager:
    def __init__(self, minecraft_dir, game_dir=None):
        self.mods_dir = os.path.join(game_dir or minecraft_dir, "mods")
        os.makedirs(self.mods_dir, exist_ok=True)

    def list_mods(self):
        mods = []
        if not os.path.isdir(self.mods_dir):
            return mods
        for f in sorted(os.listdir(self.mods_dir)):
            if f.endswith(".disabled"):
                base = f[: -len(".disabled")]
                if base.endswith(".jar") or base.endswith(".litemod"):
                    mods.append((base, False))
            elif f.endswith(".jar") or f.endswith(".litemod"):
                mods.append((f, True))
        return mods

    def add_mod(self, file_path):
        if not os.path.isfile(file_path):
            raise Exception(f"Файл не найден: {file_path}")
        dest = os.path.join(self.mods_dir, os.path.basename(file_path))
        shutil.copy2(file_path, dest)

    def set_enabled(self, mod_name, enabled):
        enabled_path = os.path.join(self.mods_dir, mod_name)
        disabled_path = enabled_path + ".disabled"
        if enabled:
            if os.path.exists(disabled_path) and not os.path.exists(enabled_path):
                os.replace(disabled_path, enabled_path)
        else:
            if os.path.exists(enabled_path):
                os.replace(enabled_path, disabled_path)

    def delete_mod(self, mod_name):
        removed = False
        for candidate in (mod_name, mod_name + ".disabled"):
            path = os.path.join(self.mods_dir, candidate)
            if os.path.isfile(path):
                os.remove(path)
                removed = True
        if not removed:
            raise FileNotFoundError(f"Файл мода не найден: {mod_name}")


class ResourcePackManager:
    def __init__(self, minecraft_dir, game_dir=None):
        self.rp_dir = os.path.join(game_dir or minecraft_dir, "resourcepacks")
        os.makedirs(self.rp_dir, exist_ok=True)

    def list_packs(self):
        packs = []
        if not os.path.isdir(self.rp_dir):
            return packs
        for f in sorted(os.listdir(self.rp_dir)):
            if f.endswith(".zip") or os.path.isdir(os.path.join(self.rp_dir, f)):
                size = 0
                fpath = os.path.join(self.rp_dir, f)
                if os.path.isfile(fpath):
                    size = os.path.getsize(fpath)
                packs.append({"name": f, "size_kb": round(size / 1024, 1)})
        return packs

    def add_pack(self, file_path):
        if not os.path.isfile(file_path):
            raise Exception(f"Файл не найден: {file_path}")
        dest = os.path.join(self.rp_dir, os.path.basename(file_path))
        shutil.copy2(file_path, dest)

    def delete_pack(self, name):
        path = os.path.join(self.rp_dir, name)
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)


def _install_universal_clipboard_bindings(root):
    v_codes = {86} if os.name == "nt" else {55}
    c_codes = {67} if os.name == "nt" else {54}
    x_codes = {88} if os.name == "nt" else {53}

    def _selected_text(widget):
        if isinstance(widget, tk.Text):
            if widget.tag_ranges("sel"):
                return widget.get("sel.first", "sel.last"), "sel.first", "sel.last"
            return None, None, None
        try:
            if widget.selection_present():
                return widget.selection_get(), "sel.first", "sel.last"
        except Exception:
            pass
        return None, None, None

    def _handle(event):
        widget = event.widget
        keysym = (event.keysym or "").lower()
        keycode = event.keycode

        if keysym == "v" or keycode in v_codes:
            if keysym == "v":
                return
            try:
                text = root.clipboard_get()
            except Exception:
                return "break"
            try:
                sel_text, s, e = _selected_text(widget)
                if sel_text is not None:
                    widget.delete(s, e)
                widget.insert("insert", text)
            except Exception:
                pass
            return "break"

        if keysym == "c" or keycode in c_codes:
            if keysym == "c":
                return
            try:
                sel_text, _, _ = _selected_text(widget)
                if sel_text is not None:
                    root.clipboard_clear()
                    root.clipboard_append(sel_text)
            except Exception:
                pass
            return "break"

        if keysym == "x" or keycode in x_codes:
            if keysym == "x":
                return
            try:
                sel_text, s, e = _selected_text(widget)
                if sel_text is not None:
                    root.clipboard_clear()
                    root.clipboard_append(sel_text)
                    widget.delete(s, e)
            except Exception:
                pass
            return "break"

    for widget_class in ("Text", "Entry", "TEntry", "TCombobox"):
        root.bind_class(widget_class, "<Control-KeyPress>", _handle, add="+")


class LauncherApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"⚡ Mafin Launcher v{APP_VERSION}")
        _install_universal_clipboard_bindings(self.root)
        self.root.geometry("1200x760")
        self.root.minsize(1050, 650)

        self.config = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
        if migrate_legacy_game_dir(self.config):
            save_json_file(CONFIG_FILE, self.config)
        self.core = LauncherCore(self.config)
        _active_pack = self.config.get("active_modpack", "")
        if _active_pack and any(pk.get("name") == _active_pack for pk in self.config.get("modpacks", [])):
            self.core.game_dir = self._modpack_dir(_active_pack)
        else:
            self.config["active_modpack"] = ""
        self._apply_pack_overrides()
        self.mod_manager = ModManager(self.core.minecraft_dir, self.core.game_dir)
        self.rp_manager = ResourcePackManager(self.core.minecraft_dir, self.core.game_dir)
        self._tray_icon = None
        self._tray_notified = False
        self._loader_tokens = {}
        self._pending_loader_version = {}
        self.p2p = P2PManager(log_callback=self._p2p_log)
        self.stats = StatsManager()
        self.achievements = AchievementManager(self.stats, self.config)

        self.api = ServerAPI(SERVER_URL)
        self._pending_sync_lock = threading.Lock()
        self.server_nick_var = tk.StringVar(value=self.config.get("server_nickname", ""))
        self.server_pass_var = tk.StringVar()
        self.server_status_var = tk.StringVar(value="Не авторизован")
        self.admin_tab_id = None
        self.admin_profiles_map = {}

        self.version_var = tk.StringVar()
        self.player_var = tk.StringVar()
        self.mem_var = tk.StringVar(value=str(self.core.memory))
        self.auto_mem_var = tk.BooleanVar(value=bool(self.config.get("auto_memory", False)))
        self.dir_var = tk.StringVar(value=self.core.minecraft_dir)
        self.modloader_var = tk.StringVar(value="vanilla")
        self.forge_var = tk.StringVar(value="Без Forge")
        self.fabric_var = tk.StringVar(value="Без Fabric")
        self.neoforge_var = tk.StringVar(value="Без NeoForge")
        self.quilt_var = tk.StringVar(value="Без Quilt")
        self.search_var = tk.StringVar()
        self.installing = False
        self.process = None
        self._log_queue = queue.Queue()
        self._log_tail = collections.deque(maxlen=120)
        self._log_readers = []
        self._log_poll_id = None
        self._launch_start_time = None
        self._current_world = None

        self._active_game_version = None
        self._active_game_player = None
        self._achievements_restore_pending = False

        self.discord = DiscordRPC(DISCORD_CLIENT_ID)
        if self.discord.is_available():
            _log_discord("Инициализация Discord RPC")
            threading.Thread(target=self._init_discord_rpc, daemon=True).start()
        else:
            _log_discord("Discord RPC пропущен: DISCORD_CLIENT_ID пустой в коде launcher.py")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.world_backups = WorldBackupManager(self.core.game_dir or self.config.get("minecraft_dir", DEFAULT_CONFIG["minecraft_dir"]))
        self.setup_ui()
        self.root.bind("<Unmap>", self._on_root_unmap, add="+")
        self.root.update_idletasks()
        self._apply_look()
        self.root.after(250, lambda: apply_window_material(self.root, _THEME["material"]))
        self.load_versions()
        self.update_accounts_ui()
        self.update_player_from_accounts()
        self._setup_hotkeys()
        self._try_restore_server_session()
        self.root.after(1500, self._check_for_updates)

    _HOTKEY_KEYSYMS = {
        "l": "launch", "cyrillic_de": "launch",
        "i": "install", "cyrillic_sha": "install",
        "q": "quit", "cyrillic_shorti": "quit",
        "k": "palette", "cyrillic_ka": "palette",
        "b": "sidebar", "cyrillic_i": "sidebar",
    }
    _HOTKEY_KEYCODES = ({76: "launch", 73: "install", 81: "quit", 75: "palette", 66: "sidebar"} if os.name == "nt"
                        else {46: "launch", 31: "install", 24: "quit", 45: "palette", 56: "sidebar"})

    def _setup_hotkeys(self):
        self._last_hotkey_at = {}
        self.root.bind_all("<Control-KeyPress>", self._on_hotkey, add="+")

    def _on_hotkey(self, event):
        try:
            if event.widget.winfo_toplevel() is not self.root:
                return
        except Exception:
            return
        keysym = (event.keysym or "").lower()
        action_name = self._HOTKEY_KEYSYMS.get(keysym) or self._HOTKEY_KEYCODES.get(event.keycode)
        if not action_name:
            return

        now = time.time()
        if now - self._last_hotkey_at.get(action_name, 0) < 1.0:
            return "break"
        self._last_hotkey_at[action_name] = now

        actions = {
            "launch": self.launch_only,
            "install": self.install_and_launch,
            "quit": lambda: self.on_close(force=True),
            "palette": self.show_command_palette,
            "sidebar": self._toggle_sidebar,
        }
        try:
            actions[action_name]()
        except Exception as e:
            print(f"Ошибка горячей клавиши {action_name}: {e}")
        if action_name != "quit":
            try:
                self._bump_achievement("hotkey_used", 1)
            except Exception as e:
                print(f"Ошибка счётчика hotkey_used: {e}")
        return "break"

    def save_config(self):
        self.config["minecraft_dir"] = self.dir_var.get().strip()
        mem_str = self.mem_var.get().strip()
        self.config["memory_mb"] = int(mem_str) if mem_str.isdigit() else 2048
        self.config["auto_memory"] = bool(self.auto_mem_var.get())
        self.config["java_path"] = self.core.java_path
        self.config["last_version"] = self.version_var.get().strip()
        self.config["accounts"] = self.core.accounts
        self.config["selected_account"] = self.core.selected_account
        save_json_file(CONFIG_FILE, self.config)

    def _init_discord_rpc(self):
        if self.discord.connect():
            if self._active_game_version:
                self.discord.set_playing_state(self._active_game_version, self._active_game_player)
            else:
                self.discord.set_menu_state()
            _log_discord("Discord RPC активен, статус выставлен.")
            self._bump_achievement("discord_connected", amount=1)

    def _make_tray_image(self):
        from PIL import Image, ImageDraw
        try:
            icon_file = resource_path("icon.ico")
            if os.path.exists(icon_file):
                return Image.open(icon_file).convert("RGBA")
        except Exception as e:
            print(f"Не удалось загрузить icon.ico для трея: {e}")
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(67, 181, 129, 255))
        d.line([(17, 46), (17, 18), (32, 36), (47, 18), (47, 46)], fill="white", width=6, joint="curve")
        return img

    def _tray_notify(self, title, message, only_if_hidden=False):
        icon = self._tray_icon
        if not icon:
            return
        try:
            icon.notify(message, title)
        except Exception as e:
            print(f"Уведомление трея не удалось: {e}")

    def _tray_hide(self, reason="minimize"):
        if not HAS_TRAY:
            return False
        if self._tray_icon:
            return True
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Показать лаунчер", lambda i, it: self.root.after(0, self._tray_show), default=True),
                pystray.MenuItem("Запустить игру", lambda i, it: self.root.after(0, self.launch_only)),
                pystray.MenuItem("Выход", lambda i, it: self.root.after(0, self._tray_quit)),
            )
            icon = pystray.Icon("mafin_launcher", self._make_tray_image(), f"Mafin Launcher v{APP_VERSION}", menu)
            icon.run_detached()
            self._tray_icon = icon
        except Exception as e:
            print(f"Не удалось создать иконку в трее: {e}")
            self._tray_icon = None
            return False
        self.root.withdraw()
        text = ("Лаунчер свёрнут в трей. Клик по иконке — открыть."
                if reason == "minimize" else "Лаунчер продолжает работать в трее. Выход — через меню иконки.")
        self.root.after(700, lambda: self._tray_notify("Mafin Launcher", text))
        return True

    def _tray_stop(self):
        icon, self._tray_icon = self._tray_icon, None
        if icon:
            try:
                icon.stop()
            except Exception:
                pass

    def _tray_show(self):
        self._tray_stop()
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _tray_quit(self):
        self._tray_stop()
        self.on_close(force=True)

    def _on_root_unmap(self, event):
        if event.widget is not self.root or self._tray_icon or not HAS_TRAY:
            return
        if not self.config.get("tray_on_minimize", True):
            return

        def check():
            try:
                if self.root.state() == "iconic":
                    self._tray_hide("minimize")
            except Exception:
                pass
        self.root.after(80, check)

    def _on_tray_settings(self):
        self.config["tray_on_minimize"] = bool(self.tray_min_var.get())
        self.config["tray_on_close"] = bool(self.tray_close_var.get())
        save_json_file(CONFIG_FILE, self.config)

    def on_close(self, force=False):
        if not force and self.config.get("tray_on_close", False) and self._tray_hide("close"):
            return
        self._tray_stop()
        try:
            self.discord.clear()
            self.discord.close()
        except Exception:
            pass
        self.root.destroy()

    def _check_achievements(self):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self._check_achievements)
            return
        if not self.api.is_logged_in():
            return
        if getattr(self, "_achievements_restore_pending", False):
            return
        try:
            self.achievements.check_all(notify_callback=self._show_achievement_toast)
        except Exception as e:
            print(f"Ошибка проверки ачивок: {e}")

    def _bump_achievement(self, counter_name, amount=1):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, lambda: self._bump_achievement(counter_name, amount))
            return
        try:
            self.achievements.bump(counter_name, amount)
        except Exception as e:
            print(f"Ошибка обновления счётчика ачивок {counter_name}: {e}")
        self._sync_counter_to_server(deltas={counter_name: amount})
        self._check_achievements()

    def _set_achievement_flag(self, flag_name, value=1):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, lambda: self._set_achievement_flag(flag_name, value))
            return
        try:
            self.achievements.set_flag(flag_name, value)
        except Exception as e:
            print(f"Ошибка установки флага ачивок {flag_name}: {e}")
        self._sync_counter_to_server(set_values={flag_name: value})
        self._check_achievements()

    def _sync_counter_to_server(self, deltas=None, set_values=None):
        if not self.api.is_logged_in():
            return

        def work():
            try:
                self.api.bump_achievement_counters(deltas=deltas, set_values=set_values)
            except Exception as e:
                print(f"Не удалось синхронизировать счётчик ачивок с сервером: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _show_achievement_toast(self, achievement):
        self._render_achievement_toast(achievement)
        play_achievement_sound()
        if self.api.is_logged_in():
            ach_id = achievement.get("id")
            threading.Thread(target=self._sync_achievement_to_server, args=(ach_id,), daemon=True).start()

    def _sync_achievement_to_server(self, achievement_id, attempt=1, max_attempts=5):
        try:
            status, data = self.api.sync_achievements([achievement_id])
            if status == 200:
                return
            raise RuntimeError(f"status={status} data={data}")
        except Exception as e:
            if attempt >= max_attempts:
                print(f"Не удалось синхронизировать ачивку {achievement_id} с сервером после "
                      f"{attempt} попыток, сдаюсь: {e}")
                return
            delay = min(2 ** attempt, 30)
            print(f"Не удалось синхронизировать ачивку {achievement_id} (попытка {attempt}): {e}. "
                  f"Повтор через {delay}с.")
            time.sleep(delay)
            self._sync_achievement_to_server(achievement_id, attempt=attempt + 1, max_attempts=max_attempts)

    def _sync_achievements_from_server(self):
        if not self.api.is_logged_in():
            return
        try:
            status, me_data = self.api.me()
            if status == 200:
                self.stats.adopt_server_totals(
                    me_data.get("total_launches"), me_data.get("total_playtime_minutes"))
            status, counters_data = self.api.my_achievement_counters()
            if status == 200:
                self.achievements.adopt_server_counters(counters_data.get("counters"))
            status, data = self.api.my_achievements()
            if status != 200:
                return
            for entry in data.get("achievements", []):
                self.achievements.mark_unlocked_silent(entry["achievement_id"], entry.get("unlocked_at"))
            if hasattr(self, "_ach_list_frame"):
                self.root.after(0, self.refresh_achievements)
        except Exception as e:
            print(f"Не удалось загрузить ачивки с сервера: {e}")
        finally:
            self._achievements_restore_pending = False
            self.root.after(0, self._check_achievements)

    def _render_achievement_toast(self, achievement):
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        try:
            toast.attributes("-alpha", 0.97)
        except Exception:
            pass

        bg = "#242830"
        border = "#43b581"
        card = tk.Frame(toast, bg=bg, highlightbackground=border, highlightthickness=2)
        card.pack(fill="both", expand=True)

        tk.Label(card, text="🏆 Ачивка разблокирована!", bg=bg, fg=border,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(10, 0))
        tk.Label(card, text=f"{achievement['icon']}  {achievement['title']}", bg=bg, fg="white",
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=14, pady=(4, 0))
        tk.Label(card, text=achievement["desc"], bg=bg, fg="#7a8599",
                 font=("Segoe UI", 9), wraplength=280, justify="left").pack(anchor="w", padx=14, pady=(2, 12))

        toast.update_idletasks()
        w, h = 320, toast.winfo_reqheight()
        sw = toast.winfo_screenwidth()
        sh = toast.winfo_screenheight()
        stack_index = getattr(self, "_toast_stack", 0)
        self._toast_stack = stack_index + 1
        x = sw - w - 24
        y = sh - h - 60 - stack_index * (h + 10)
        toast.geometry(f"{w}x{h}+{x}+{y}")

        def _close():
            self._toast_stack = max(0, getattr(self, "_toast_stack", 1) - 1)
            try:
                toast.destroy()
            except Exception:
                pass

        toast.after(4500, _close)
        card.bind("<Button-1>", lambda e: _close())

    def build_achievements_tab(self, frame):
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=10, pady=(10, 5))
        self._ach_progress_label = ttk.Label(top, text="", font=("Segoe UI", 11, "bold"))
        self._ach_progress_label.pack(side="left")
        ttk.Button(top, text="🔄 Обновить", command=self.refresh_achievements).pack(side="right")

        canvas_frame = ttk.Frame(frame)
        canvas_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        canvas = tk.Canvas(canvas_frame, bg=self._bg, highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        self._ach_list_frame = ttk.Frame(canvas)
        self._ach_list_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self._ach_list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)

        self.refresh_achievements()

    def refresh_achievements(self):
        for child in self._ach_list_frame.winfo_children():
            child.destroy()

        entries = self.achievements.list_all()
        unlocked_count = sum(1 for _, unlocked, _ in entries if unlocked)
        self._ach_progress_label.config(
            text=f"Разблокировано {unlocked_count} / {len(entries)}"
        )

        for ach, unlocked, unlocked_at in entries:
            row = tk.Frame(self._ach_list_frame, bg=self._bg2, highlightbackground="#3a3f4a",
                            highlightthickness=1)
            row.pack(fill="x", pady=3, padx=2)

            icon_color = "white" if unlocked else "#4a4f5a"
            tk.Label(row, text=ach["icon"], bg=self._bg2, fg=icon_color,
                     font=("Segoe UI", 18)).pack(side="left", padx=(12, 10), pady=8)

            text_frame = tk.Frame(row, bg=self._bg2)
            text_frame.pack(side="left", fill="x", expand=True, pady=6)
            title_color = "white" if unlocked else "#7a8599"
            title_text = ach["title"] if unlocked else f"??? ({ach['title'][0]}...)"
            tk.Label(text_frame, text=title_text, bg=self._bg2, fg=title_color,
                     font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
            desc_text = ach["desc"] if unlocked else "Ачивка ещё не разблокирована"
            tk.Label(text_frame, text=desc_text, bg=self._bg2, fg="#5c6270",
                     font=("Segoe UI", 9), anchor="w").pack(fill="x")

            if unlocked and unlocked_at:
                try:
                    dt = datetime.fromisoformat(unlocked_at).strftime("%d.%m.%Y %H:%M")
                except Exception:
                    dt = ""
                tk.Label(row, text=dt, bg=self._bg2, fg="#43b581",
                         font=("Segoe UI", 9)).pack(side="right", padx=12)

    def _p2p_log(self, msg):
        if hasattr(self, "p2p_log_text") and self.p2p_log_text:
            self.root.after(0, lambda: self._append_p2p_log(msg))

    def _append_p2p_log(self, msg):
        try:
            self.p2p_log_text.config(state="normal")
            self.p2p_log_text.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
            self.p2p_log_text.see(tk.END)
            self.p2p_log_text.config(state="disabled")
        except Exception:
            pass

    def setup_ui(self):
        style = ttk.Style()
        self._style = style
        _THEME["root"] = self.root
        theme_key = self.config.get("launcher_theme", DEFAULT_LAUNCHER_THEME)
        theme = LAUNCHER_THEMES.get(theme_key, LAUNCHER_THEMES[DEFAULT_LAUNCHER_THEME])
        material = self.config.get("window_material", DEFAULT_WINDOW_MATERIAL)
        if material not in WINDOW_MATERIALS:
            material = "solid"
        set_theme(material, theme["accent"], theme["accent_hover"])
        apply_ttk_styles(style, self.root)
        self._sync_color_attrs()
        TEXT_DIM = _pal("text_dim")

        bottom = tk.Frame(self.root)
        bottom.pack(side="bottom", fill="x", pady=(0, 6), padx=14)
        self.status_var = tk.StringVar(value="Готов")
        tk.Label(bottom, textvariable=self.status_var, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(side="left")
        self.stats_label = tk.Label(bottom, text="", fg=TEXT_DIM, font=("Segoe UI", 8))
        self.stats_label.pack(side="right")

        shell = tk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        self._shell, self._nav_defs = shell, []
        self._build_sidebar(shell)

        content = tk.Frame(shell)
        content.pack(side="left", fill="both", expand=True, padx=(4, 14), pady=(14, 0))
        self._content = content
        self._page_title = tk.Label(content, text="", anchor="w", fg=_pal("text"),
                                    font=("Segoe UI Semibold", 20))
        self._page_title.pack(fill="x", pady=(0, 8))
        self.notebook = ttk.Notebook(content)
        self.notebook.pack(fill="both", expand=True)

        tabs = [
            ("🎮 Игра", self.build_game_tab),
            ("👤 Профили", self.build_profiles_tab),
            ("🧑 Скин", self.build_skin_tab),
            ("📦 Моды", self.build_mods_tab),
            ("🧩 Сборки", self.build_modpacks_tab),
            ("🗂️ Версии", self.build_versions_tab),
            ("💾 Бэкапы", self.build_backups_tab),
            ("🎨 Ресурспаки", self.build_resourcepacks_tab),
            ("🌐 Серверы", self.build_servers_tab),
            ("🔗 P2P", self.build_p2p_tab),
            ("🖼️ Скриншоты", self.build_screenshots_tab),
            ("📋 Логи", self.build_logs_tab),
            ("🏆 Ачивки", self.build_achievements_tab),
            ("🏅 Топ недели", self.build_top_tab),
            ("🖥️ Аккаунт", self.build_account_tab),
            ("👥 Друзья", self.build_friends_tab),
            ("🎁 Подарки", self.build_gifts_tab),
            ("👑 Привилегии", self.build_privileges_tab),
            ("📋 Задания", self.build_quests_tab),
            ("🛡️ Админка", self.build_admin_tab),
            ("⚙️ Настройки", self.build_settings_tab),
            ("📰 Новости", self.build_news_tab),
            ("ℹ️ О программе", self.build_about_tab),
        ]
        _orig_tab = self.notebook.tab

        def _tab(*args, **kwargs):
            result = _orig_tab(*args, **kwargs)
            if "state" in kwargs:
                self.root.after_idle(self._nav_sync)
            return result
        self.notebook.tab = _tab
        self._orig_notebook_tab = _orig_tab

        for name, builder in tabs:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=name)
            self._add_nav_item(name, tab)
            builder(tab)
            if name == "🛡️ Админка":
                self.admin_tab_id = tab
            if name == "👥 Друзья":
                self.friends_tab_frame = tab
            if name == "📋 Задания":
                self.quests_tab_frame = tab
            if name == "🎁 Подарки":
                self.gifts_tab_frame = tab
            if name == "👑 Привилегии":
                self.privileges_tab_frame = tab

        if self.admin_tab_id is not None:
            self.notebook.tab(self.admin_tab_id, state="hidden")

        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._nav_sync(), add="+")
        self._nav_sync()
        self._update_stats_label()

    def _sync_color_attrs(self):
        self._bg, self._bg2 = _pal("bg"), _pal("surface")
        self._accent, self._accent_hover = _pal("accent"), _pal("accent_hover")

    NAV_STYLES = {
        "list": "📃 Список (с полоской)",
        "pill": "💊 Пилюли (акцентная заливка)",
        "compact": "▫️ Компактно (только иконки)",
    }

    @staticmethod
    def _rrect(cv, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return cv.create_polygon(pts, smooth=True, **kw)

    def _build_sidebar(self, parent, before=None):
        style = self.config.get("nav_style", "list")
        if style not in self.NAV_STYLES:
            style = "list"
        self._nav_style = style
        compact = style == "compact"
        width = 72 if compact else 236
        side = tk.Frame(parent, width=width)
        side.pack(side="left", fill="y", **({"before": before} if before else {}))
        side.pack_propagate(False)
        self._side = side

        brand = tk.Frame(side)
        brand.pack(fill="x", padx=(14 if compact else 20), pady=(18, 8))
        toggle = tk.Label(brand, text="☰", fg=_pal("text_dim"), cursor="hand2",
                          font=("Segoe UI", 13))
        toggle.bind("<Button-1>", lambda e: self._toggle_sidebar())
        toggle.bind("<Enter>", lambda e: toggle.configure(fg=_pal("text")))
        toggle.bind("<Leave>", lambda e: toggle.configure(fg=_pal("text_dim")))
        if compact:
            tk.Label(brand, text="⚡", fg=_pal("accent"), font=("Segoe UI Emoji", 20)).pack()
            toggle.pack(pady=(8, 0))
        else:
            toggle.pack(side="right")
            tk.Label(brand, text="⚡ Mafin", anchor="w", fg=_pal("text"),
                     font=("Segoe UI Semibold", 18)).pack(fill="x")
            tk.Label(brand, text=f"Launcher  •  v{APP_VERSION}", anchor="w",
                     fg=_pal("text_dim"), font=("Segoe UI", 8)).pack(fill="x")

        play = tk.Label(side, text="▶" if compact else "▶  Играть", cursor="hand2",
                        bg=_pal("accent"), fg="#ffffff", pady=10,
                        font=("Segoe UI", 12 if compact else 11, "bold"))
        play.pack(side="bottom", fill="x", padx=12, pady=(6, 14))
        play.bind("<Button-1>", lambda e: self.launch_only())
        play.bind("<Enter>", lambda e: play.configure(bg=_pal("accent_hover")))
        play.bind("<Leave>", lambda e: play.configure(bg=_pal("accent")))

        self._nav_canvas = tk.Canvas(side, highlightthickness=0, bd=0, width=width)
        self._nav_canvas.pack(fill="both", expand=True)
        self._nav_inner = tk.Frame(self._nav_canvas)
        win_id = self._nav_canvas.create_window((0, 0), window=self._nav_inner, anchor="nw")
        self._nav_canvas.bind(
            "<Configure>", lambda e: self._nav_canvas.itemconfigure(win_id, width=e.width))
        self._nav_inner.bind(
            "<Configure>",
            lambda e: self._nav_canvas.configure(scrollregion=self._nav_canvas.bbox("all")))
        self._nav_canvas.bind("<MouseWheel>", self._nav_wheel)
        self._nav_inner.bind("<MouseWheel>", self._nav_wheel)
        self._nav_items = []

    def _toggle_sidebar(self):
        if self._nav_style == "compact":
            self.set_nav_style(self.config.get("nav_style_prev", "list"))
        else:
            self.config["nav_style_prev"] = self._nav_style
            self.set_nav_style("compact")

    def set_nav_style(self, style):
        if style not in self.NAV_STYLES:
            return
        if style != "compact":
            self.config["nav_style_prev"] = style
        self.config["nav_style"] = style
        self._side.destroy()
        self._build_sidebar(self._shell, before=self._content)
        for name, tab in self._nav_defs:
            self._add_nav_item(name, tab, record=False)
        self._nav_sync()
        self.save_config()

    def _nav_wheel(self, event):
        if self._nav_inner.winfo_reqheight() > self._nav_canvas.winfo_height():
            self._nav_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _add_nav_item(self, name, tab, record=True):
        if record:
            self._nav_defs.append((name, tab))
        icon, _, text = name.partition(" ")
        if not text:
            icon, text = "•", name
        compact = self._nav_style == "compact"
        cv = tk.Canvas(self._nav_inner, highlightthickness=0, bd=0, cursor="hand2",
                       height=44 if compact else 40, width=56 if compact else 10)
        item = {"tab": tab, "cv": cv, "icon": icon, "text": text, "hover": False}
        self._nav_items.append(item)
        cv.bind("<Button-1>", lambda e, t=tab: self.notebook.select(t))
        cv.bind("<Enter>", lambda e, i=item: self._nav_hover(i, True))
        cv.bind("<Leave>", lambda e, i=item: self._nav_hover(i, False))
        cv.bind("<Configure>", lambda e, i=item: self._nav_paint(i))
        cv.bind("<MouseWheel>", self._nav_wheel)

    def _nav_hover(self, item, state):
        item["hover"] = state
        self._nav_paint(item)
        if self._nav_style == "compact":
            self._nav_tip(item if state else None)

    def _nav_tip(self, item):
        tip = getattr(self, "_tip", None)
        if tip is not None:
            tip.destroy()
            self._tip = None
        if item is None:
            return
        cv = item["cv"]
        x = cv.winfo_rootx() - self.root.winfo_rootx() + cv.winfo_width() + 6
        y = cv.winfo_rooty() - self.root.winfo_rooty() + 6
        self._tip = tk.Label(self.root, text=item["text"], fg=_pal("text"), bg=_pal("surface3"),
                             padx=10, pady=4, font=("Segoe UI", 9))
        self._tip.place(x=x, y=y)
        self._tip.lift()

    def _nav_paint(self, item):
        cv = item["cv"]
        try:
            selected = self.notebook.select() == str(item["tab"])
            w, h = max(cv.winfo_width(), 20), int(cv.cget("height"))
        except Exception:
            return
        cv.delete("all")
        style, hover = self._nav_style, item["hover"]
        accent = _pal("accent")
        dim, txt = _pal("text_dim"), _pal("text")
        fg = txt if (selected or hover) else dim
        font = ("Segoe UI Semibold", 10) if selected else ("Segoe UI", 10)
        if style == "pill":
            if selected:
                self._rrect(cv, 2, 3, w - 2, h - 3, 17, fill=accent, outline="")
                fg = "#ffffff"
            elif hover:
                self._rrect(cv, 2, 3, w - 2, h - 3, 17, fill=_pal("surface2"), outline="")
        else:
            if selected or hover:
                self._rrect(cv, 2, 3, w - 2, h - 3, 9,
                            fill=_pal("surface2") if selected else _pal("surface"), outline="")
            if selected:
                self._rrect(cv, 5, h / 2 - 10, 8, h / 2 + 10, 1, fill=accent, outline="")
        if style == "compact":
            cv.create_text(w / 2, h / 2, text=item["icon"], fill=fg, font=("Segoe UI Emoji", 15))
        else:
            cv.create_text(30, h / 2, text=item["icon"], fill=fg, font=("Segoe UI Emoji", 12))
            cv.create_text(52, h / 2, text=item["text"], fill=fg, font=font, anchor="w")

    def _nav_sync(self):
        if not hasattr(self, "_nav_items"):
            return
        for item in self._nav_items:
            item["cv"].pack_forget()
        for item in self._nav_items:
            try:
                hidden = str(self._orig_notebook_tab(item["tab"], "state")) == "hidden"
            except Exception:
                hidden = False
            if not hidden:
                item["cv"].pack(fill="x", padx=(8, 8), pady=1)
            self._nav_paint(item)
        try:
            self._page_title.configure(text=self._orig_notebook_tab(self.notebook.select(), "text"))
        except Exception:
            pass

    def _palette_commands(self):
        cmds = [(name, lambda t=tab: self.notebook.select(t)) for name, tab in self._nav_defs
                if str(self._orig_notebook_tab(tab, "state")) != "hidden"]
        cmds = [("➡️  Перейти: " + n, f) for n, f in cmds]
        cmds += [
            ("▶  Запустить игру", self.launch_only),
            ("⬇️  Установить и запустить", self.install_and_launch),
            ("📂  Открыть папку игры", self.open_game_folder),
            ("☰  Свернуть / развернуть панель", self._toggle_sidebar),
        ]
        cmds += [("🪟  Материал: " + v["label"].split(" ", 1)[1], lambda k=k: self.apply_material(k))
                 for k, v in WINDOW_MATERIALS.items()]
        cmds += [("🎨  Акцент: " + v["label"].split(" ", 1)[1].split(" (")[0],
                  lambda k=k: self.apply_theme(k)) for k, v in LAUNCHER_THEMES.items()]
        cmds += [("🧭  Меню: " + lbl.split(" ", 1)[1], lambda k=k: self.set_nav_style(k))
                 for k, lbl in self.NAV_STYLES.items()]
        return cmds

    def show_command_palette(self):
        old = getattr(self, "_palette", None)
        if old is not None and old.winfo_exists():
            old.destroy()
            return
        cmds = self._palette_commands()
        win = tk.Toplevel(self.root)
        self._palette = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        w, h = 520, 340
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 90
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.configure(bg=_pal("accent"))
        box = tk.Frame(win, bg=_pal("surface"))
        box.pack(fill="both", expand=True, padx=1, pady=1)
        var = tk.StringVar()
        entry = tk.Entry(box, textvariable=var, relief="flat", bg=_pal("surface2"),
                         fg=_pal("text"), insertbackground=_pal("text"), font=("Segoe UI", 13))
        entry.pack(fill="x", padx=12, pady=(12, 8), ipady=8)
        lb = tk.Listbox(box, relief="flat", bg=_pal("surface"), fg=_pal("text"),
                        selectbackground=_pal("accent"), selectforeground="#ffffff",
                        activestyle="none", highlightthickness=0, font=("Segoe UI", 11))
        lb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        shown = []

        def refill(*_):
            q = var.get().strip().lower()
            shown[:] = [c for c in cmds if q in c[0].lower()]
            lb.delete(0, "end")
            for label, _fn in shown:
                lb.insert("end", "  " + label)
            if shown:
                lb.selection_set(0)

        def run(_e=None):
            sel = lb.curselection()
            if not sel:
                return "break"
            fn = shown[sel[0]][1]
            win.destroy()
            self.root.after(30, fn)
            return "break"

        def move(d):
            sel = lb.curselection()
            i = max(0, min(len(shown) - 1, (sel[0] if sel else 0) + d))
            lb.selection_clear(0, "end")
            lb.selection_set(i)
            lb.see(i)
            return "break"

        entry.bind("<Return>", run)
        entry.bind("<Down>", lambda e: move(1))
        entry.bind("<Up>", lambda e: move(-1))
        entry.bind("<Escape>", lambda e: win.destroy())
        lb.bind("<Double-Button-1>", run)
        win.bind("<FocusOut>", lambda e: win.after(120, lambda: (
            win.destroy() if win.winfo_exists() and win.focus_get() is None else None)))
        var.trace_add("write", refill)
        refill()
        win.update_idletasks()
        entry.focus_force()

    def _update_stats_label(self):
        self.stats_label.config(text=self.stats.get_summary())

    def _apply_look(self):
        theme = LAUNCHER_THEMES.get(self.config.get("launcher_theme"),
                                    LAUNCHER_THEMES[DEFAULT_LAUNCHER_THEME])
        wanted = self.config.get("window_material", DEFAULT_WINDOW_MATERIAL)
        if wanted not in WINDOW_MATERIALS:
            wanted = "solid"
        set_theme(wanted, theme["accent"], theme["accent_hover"])
        applied = apply_window_material(self.root, wanted)
        if applied != wanted:
            set_theme("solid", theme["accent"], theme["accent_hover"])
            if wanted != "solid" and hasattr(self, "status_var"):
                self.status_var.set(
                    f"{WINDOW_MATERIALS[wanted]['label']} недоступен в этой версии Windows - "
                    "используется классический фон")
        apply_ttk_styles(self._style, self.root)
        self._sync_color_attrs()
        retheme_tree(self.root)
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                _style_toplevel(child)

        for attr in (
            "player_display", "total_size_label", "p2p_address_label",
            "server_status_label", "admin_stats_label", "game_size_label",
        ):
            widget = getattr(self, attr, None)
            if widget is not None:
                try:
                    widget.config(foreground=self._accent)
                except Exception:
                    pass
        if hasattr(self, "news_text"):
            try:
                self.news_text.tag_config("title", foreground=self._accent)
            except Exception:
                pass
        self._nav_sync()
        return applied

    def apply_theme(self, theme_key, bump_achievement=True):
        if theme_key not in LAUNCHER_THEMES:
            return
        old_key = self.config.get("launcher_theme", DEFAULT_LAUNCHER_THEME)
        self.config["launcher_theme"] = theme_key
        self._apply_look()
        self.save_config()
        if bump_achievement and theme_key != old_key:
            self._bump_achievement("theme_changes", 1)

    def apply_material(self, material, bump_achievement=True):
        if material not in WINDOW_MATERIALS:
            return
        old = self.config.get("window_material", DEFAULT_WINDOW_MATERIAL)
        self.config["window_material"] = material
        self._apply_look()
        self.save_config()
        if bump_achievement and material != old:
            self._bump_achievement("theme_changes", 1)

    def build_game_tab(self, frame):
        left = ttk.Frame(frame)
        left.pack(side="left", fill="both", expand=True, padx=15, pady=15)

        search_frame = ttk.Frame(left)
        search_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Label(search_frame, text="🔍").pack(side="left")
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=35)
        self.search_entry.pack(side="left", padx=5, fill="x", expand=True)
        self.search_var.trace_add("write", lambda *_: self._filter_versions())

        ttk.Label(left, text="Версия:").grid(row=1, column=0, sticky="w", pady=5)
        self.version_combo = ttk.Combobox(left, textvariable=self.version_var, width=30)
        self.version_combo.grid(row=1, column=1, pady=5, padx=5)
        self.version_combo.bind("<<ComboboxSelected>>", self.on_version_select)

        ttk.Label(left, text="Загрузчик:").grid(row=2, column=0, sticky="w", pady=5)
        loader_frame = ttk.Frame(left)
        loader_frame.grid(row=2, column=1, pady=5, padx=5, sticky="w")
        ttk.Radiobutton(loader_frame, text="Vanilla", variable=self.modloader_var,
                        value="vanilla", command=self._on_loader_change).pack(side="left", padx=2)
        ttk.Radiobutton(loader_frame, text="Forge", variable=self.modloader_var,
                        value="forge", command=self._on_loader_change).pack(side="left", padx=2)
        ttk.Radiobutton(loader_frame, text="Fabric", variable=self.modloader_var,
                        value="fabric", command=self._on_loader_change).pack(side="left", padx=2)
        ttk.Radiobutton(loader_frame, text="NeoForge", variable=self.modloader_var,
                        value="neoforge", command=self._on_loader_change).pack(side="left", padx=2)
        ttk.Radiobutton(loader_frame, text="Quilt", variable=self.modloader_var,
                        value="quilt", command=self._on_loader_change).pack(side="left", padx=2)

        self.forge_label = ttk.Label(left, text="Версия Forge:")
        self.forge_label.grid(row=3, column=0, sticky="w", pady=5)
        self.forge_combo = ttk.Combobox(left, textvariable=self.forge_var, width=26, state="readonly")
        self.forge_combo.grid(row=3, column=1, pady=5, padx=5, sticky="w")
        self.forge_combo.set("Без Forge")
        self.forge_install_btn = ttk.Button(left, text="⚡ Установить Forge", command=self.install_forge_only)
        self.forge_install_btn.grid(row=3, column=2, pady=5, padx=5)

        self.fabric_label = ttk.Label(left, text="Версия Fabric:")
        self.fabric_label.grid(row=4, column=0, sticky="w", pady=5)
        self.fabric_combo = ttk.Combobox(left, textvariable=self.fabric_var, width=30, state="readonly")
        self.fabric_combo.grid(row=4, column=1, pady=5, padx=5)
        self.fabric_combo.set("Без Fabric")

        self.neoforge_label = ttk.Label(left, text="Версия NeoForge:")
        self.neoforge_label.grid(row=3, column=0, sticky="w", pady=5)
        self.neoforge_combo = ttk.Combobox(left, textvariable=self.neoforge_var, width=26, state="readonly")
        self.neoforge_combo.grid(row=3, column=1, pady=5, padx=5, sticky="w")
        self.neoforge_combo.set("Без NeoForge")
        self.quilt_label = ttk.Label(left, text="Версия Quilt:")
        self.quilt_label.grid(row=4, column=0, sticky="w", pady=5)
        self.quilt_combo = ttk.Combobox(left, textvariable=self.quilt_var, width=30, state="readonly")
        self.quilt_combo.grid(row=4, column=1, pady=5, padx=5)
        self.quilt_combo.set("Без Quilt")
        for _w in (self.neoforge_label, self.neoforge_combo, self.quilt_label, self.quilt_combo):
            _w.grid_remove()

        ttk.Label(left, text="Игрок:").grid(row=5, column=0, sticky="w", pady=5)
        self.player_entry = ttk.Entry(left, textvariable=self.player_var, width=30, state="readonly")
        self.player_entry.grid(row=5, column=1, pady=5, padx=5)

        ttk.Label(left, text="ОЗУ (МБ):").grid(row=6, column=0, sticky="w", pady=5)
        self.mem_entry = ttk.Entry(left, textvariable=self.mem_var, width=30)
        self.mem_entry.grid(row=6, column=1, pady=5, padx=5)
        auto_frame = ttk.Frame(left)
        auto_frame.grid(row=6, column=2, sticky="w", padx=5)
        ttk.Button(auto_frame, text="🤖 Авто", width=8, command=self.auto_memory_now).pack(side="left")
        ttk.Checkbutton(auto_frame, text="при запуске", variable=self.auto_mem_var).pack(side="left", padx=4)

        btn_frame = ttk.Frame(left)
        btn_frame.grid(row=7, column=0, columnspan=3, pady=20)
        self.install_btn = ttk.Button(
            btn_frame, text="📥 Установить и запустить",
            style="Accent.TButton", command=self.install_and_launch
        )
        self.install_btn.pack(side="left", padx=5)
        self.launch_btn = ttk.Button(btn_frame, text="🚀 Запустить", command=self.launch_only)
        self.launch_btn.pack(side="left", padx=5)

        self.progress = ttk.Progressbar(left, orient="horizontal", length=420, mode="determinate")
        self.progress.grid(row=8, column=0, columnspan=3, pady=10)

        right = ttk.Frame(frame, width=160)
        right.pack(side="right", fill="y", padx=15, pady=15)
        if HAS_PIL:
            self.skin_label = ttk.Label(right, text="🧑 Скин")
            self.skin_label.pack(pady=10)
            self.skin_image = None
        else:
            ttk.Label(right, text="(PIL не установлен)", foreground="#555").pack(pady=10)
        ttk.Label(right, text="Игрок:").pack(pady=5)
        self.player_display = ttk.Label(right, text="", foreground=self._accent, font=("Segoe UI", 12, "bold"))
        self.player_display.pack(pady=5)

        ttk.Label(
            right, text="Ctrl+L — запуск\nCtrl+I — установка\nCtrl+Q — выход",
            foreground="#555", font=("Segoe UI", 8)
        ).pack(side="bottom", pady=10)

        self._on_loader_change()

    def _filter_versions(self):
        query = self.search_var.get().strip().lower()
        if not hasattr(self, "_all_versions"):
            return
        if query and not self.achievements.is_unlocked("search_used"):
            self._bump_achievement("search_used", 1)
        if not query:
            self.version_combo.config(values=self._all_versions)
        else:
            filtered = [v for v in self._all_versions if query in v.lower()]
            self.version_combo.config(values=filtered)

    _LOADER_UI = {
        "neoforge": ("NeoForge", "Без NeoForge", "neoforge_var", "neoforge_combo"),
        "quilt": ("Quilt", "Без Quilt", "quilt_var", "quilt_combo"),
    }

    def _on_loader_change(self):
        loader = self.modloader_var.get()
        for w in (self.forge_label, self.forge_combo, self.forge_install_btn,
                  self.fabric_label, self.fabric_combo,
                  self.neoforge_label, self.neoforge_combo,
                  self.quilt_label, self.quilt_combo):
            w.grid_remove()
        if loader == "forge":
            self.forge_label.grid()
            self.forge_combo.grid()
            self.forge_install_btn.grid()
            self._fetch_forge_versions()
        elif loader == "fabric":
            self.fabric_label.grid()
            self.fabric_combo.grid()
            self._fetch_fabric_loaders()
        elif loader == "neoforge":
            self.neoforge_label.grid()
            self.neoforge_combo.grid()
            self._fetch_loader_versions("neoforge")
        elif loader == "quilt":
            self.quilt_label.grid()
            self.quilt_combo.grid()
            self._fetch_loader_versions("quilt")

    def _fetch_loader_versions(self, loader_id):
        version = self.version_var.get().strip()
        if not version:
            return
        title, none_label, var_name, combo_name = self._LOADER_UI[loader_id]
        var, combo = getattr(self, var_name), getattr(self, combo_name)
        token = self._loader_tokens[loader_id] = self._loader_tokens.get(loader_id, 0) + 1
        cache = self.config.setdefault("loader_versions_cache", {}).setdefault(loader_id, {})

        def show(versions):
            if not versions:
                combo.config(values=[f"{title} недоступен"])
                var.set(f"{title} недоступен")
                return
            current = var.get()
            combo.config(values=[none_label] + versions)
            pending = self._pending_loader_version.pop(loader_id, None)
            if pending in versions:
                var.set(pending)
            elif current in versions:
                var.set(current)
            else:
                var.set(versions[0])

        cached = cache.get(version)
        if cached:
            show(cached)
        else:
            combo.config(values=["Загрузка..."])
            var.set("Загрузка...")

        def fetch():
            versions = self.core.get_loader_versions_generic(loader_id, version)

            def apply():
                if token != self._loader_tokens.get(loader_id):
                    return
                if versions:
                    if cache.get(version) != versions:
                        cache[version] = versions
                        self.save_config()
                        show(versions)
                elif not cached:
                    show([])
            self.root.after(0, apply)

        threading.Thread(target=fetch, daemon=True).start()

    def _current_loader_version(self):
        var = {"forge": self.forge_var, "fabric": self.fabric_var,
               "neoforge": self.neoforge_var, "quilt": self.quilt_var}.get(self.modloader_var.get())
        v = var.get().strip() if var else ""
        if not v or v.startswith("Без ") or v.endswith("недоступен") or v == "Загрузка...":
            return ""
        return v

    def _fetch_forge_versions(self):
        version = self.version_var.get().strip()
        if not version:
            return
        self._forge_request_token = getattr(self, "_forge_request_token", 0) + 1
        my_token = self._forge_request_token
        self.forge_combo.config(values=["Загрузка..."])
        self.forge_var.set("Загрузка...")

        def fetch():
            try:
                forge_list = self.core.get_forge_versions(version)
            except Exception:
                forge_list = []

            def apply():
                if my_token != self._forge_request_token:
                    return
                if not forge_list:
                    self.forge_combo.config(values=["Forge недоступен"])
                    self.forge_var.set("Forge недоступен")
                else:
                    vals = ["Без Forge"] + forge_list
                    self.forge_combo.config(values=vals)
                    self.forge_var.set("Без Forge")
                    _pv = self._pending_loader_version.pop("forge", None)
                    if _pv in forge_list:
                        self.forge_var.set(_pv)
            self.root.after(0, apply)

        threading.Thread(target=fetch, daemon=True).start()

    def install_forge_only(self):
        version = self.version_var.get().strip()
        if not version:
            messagebox.showerror("Ошибка", "Сначала выберите версию Minecraft")
            return
        minecraft_dir = self.dir_var.get().strip()
        os.makedirs(minecraft_dir, exist_ok=True)
        self.core.minecraft_dir = minecraft_dir
        self.forge_install_btn.config(state="disabled")
        self.status_var.set(f"Проверка Forge для {version}...")

        def work():
            try:
                forge_list = self.core.get_forge_versions(version)
            except Exception:
                forge_list = []
            if not forge_list:
                def show_none():
                    messagebox.showinfo("Forge", f"Для {version} сборок Forge не найдено.")
                    self.forge_install_btn.config(state="normal")
                    self.status_var.set("Готов")
                self.root.after(0, show_none)
                return

            chosen = self.forge_var.get().strip()
            if chosen in ("", "Без Forge", "Загрузка...", "Forge недоступен"):
                chosen = forge_list[0]

            def install_callback(progress, total, stage):
                if total:
                    pct = int(progress / total * 100)
                    self.root.after(0, lambda: self.progress.config(value=pct))
                    self.root.after(0, lambda: self.status_var.set(f"{stage}: {progress}/{total}"))
                else:
                    self.root.after(0, lambda: self.status_var.set(stage))

            try:
                self.root.after(0, lambda: self.status_var.set(f"Установка Forge {chosen}..."))
                self.core.install_forge(chosen, install_callback)

                def on_success():
                    self.forge_combo.config(values=["Без Forge"] + forge_list)
                    self.forge_var.set(chosen)
                    self.modloader_var.set("forge")
                    self._on_loader_change()
                    self.refresh_installed_versions()
                    self.progress.config(value=0)
                    self.status_var.set("Готов")
                    messagebox.showinfo("Готово", f"Forge {chosen} установлен.")
                self.root.after(0, on_success)
            except Exception as e:
                def on_error(err=e):
                    messagebox.showerror("Ошибка Forge", str(err))
                    self.status_var.set("Готов")
                self.root.after(0, on_error)
            finally:
                self.root.after(0, lambda: self.forge_install_btn.config(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    def _fetch_fabric_loaders(self):
        version = self.version_var.get().strip()
        if not version:
            return
        self.fabric_combo.config(values=["Загрузка..."])
        self.fabric_var.set("Загрузка...")

        def fetch():
            loaders = self.core.get_fabric_loader_versions(version)
            def apply():
                if not loaders:
                    self.fabric_combo.config(values=["Fabric недоступен"])
                    self.fabric_var.set("Fabric недоступен")
                else:
                    vals = ["Без Fabric"] + loaders
                    self.fabric_combo.config(values=vals)
                    self.fabric_var.set("Без Fabric")
                    _pv = self._pending_loader_version.pop("fabric", None)
                    if _pv in loaders:
                        self.fabric_var.set(_pv)
            self.root.after(0, apply)

        threading.Thread(target=fetch, daemon=True).start()

    def build_profiles_tab(self, frame):
        hint = ttk.Label(
            frame,
            text="ℹ️ Профиль создаётся автоматически при входе в аккаунт на вкладке «Аккаунт».\n"
                 "Чтобы создать новый профиль — зарегистрируйтесь и войдите под нужным ником.",
            foreground="#7a8599", justify="left", wraplength=420
        )
        hint.pack(anchor="w", padx=10, pady=(10, 0))

        self.acc_listbox = tk.Listbox(frame, height=12, bg="#242830", fg="white",
                                       selectbackground="#43b581", font=("Segoe UI", 11))
        self.acc_listbox.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.acc_listbox.yview)
        scroll.pack(side="left", fill="y")
        self.acc_listbox.config(yscrollcommand=scroll.set)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(side="right", fill="y", padx=15, pady=10)
        ttk.Button(btn_frame, text="🗑️ Удалить", command=self.remove_account).pack(pady=5, fill="x")
        ttk.Button(btn_frame, text="✅ Выбрать", command=self.select_account).pack(pady=5, fill="x")

    def update_accounts_ui(self):
        if not hasattr(self, "acc_listbox"):
            return
        self.acc_listbox.delete(0, tk.END)
        for i, acc in enumerate(self.core.accounts):
            label = acc["name"]
            if i == self.core.selected_account:
                label += "  ✅ (активен)"
            self.acc_listbox.insert(tk.END, label)

    def update_player_from_accounts(self):
        acc = self.core.accounts[self.core.selected_account] if self.core.accounts else None
        if acc:
            name = acc["name"]
            self.player_var.set(name)
            if hasattr(self, "player_display"):
                self.player_display.config(text=name)
            if HAS_PIL and self.config.get("skin_auto_download", True):
                self.load_skin(name)
        else:
            if hasattr(self, "player_display"):
                self.player_display.config(text="(не выбран)")
        self._refresh_skin_tab()

    def load_skin(self, player_name):
        if not HAS_PIL:
            return
        acc = self._get_account_by_name(player_name)
        local_path = acc.get("skin_path") if acc else None
        if local_path and os.path.isfile(local_path):
            img = self._render_skin_face(local_path, size=60)
            if img is not None:
                self.skin_image = img
                self.skin_label.config(image=self.skin_image)
                self.skin_label.image = self.skin_image
                return
        try:
            url = f"https://mc-heads.net/avatar/{player_name}/60"
            resp = requests.get(url, stream=True, timeout=5)
            if resp.status_code == 200:
                img = Image.open(resp.raw)
                img = img.resize((60, 60), Image.LANCZOS)
                self.skin_image = ImageTk.PhotoImage(img)
                self.skin_label.config(image=self.skin_image)
                self.skin_label.image = self.skin_image
        except Exception as e:
            print("Не удалось загрузить скин:", e)

    def _get_account_by_name(self, player_name):
        for acc in self.core.accounts:
            if acc.get("name", "").lower() == (player_name or "").lower():
                return acc
        return None

    @staticmethod
    def _render_skin_face(skin_path, size=60):
        if not HAS_PIL:
            return None
        try:
            skin = Image.open(skin_path).convert("RGBA")
            if skin.size not in ((64, 64), (64, 32)):
                return None
            base_face = skin.crop((8, 8, 16, 16))
            face = base_face.copy()
            if skin.size == (64, 64):
                hat = skin.crop((40, 8, 48, 16))
                if hat.getbbox() is not None:
                    face.paste(hat, (0, 0), hat)
            face = face.resize((size, size), Image.NEAREST)
            return ImageTk.PhotoImage(face)
        except Exception as e:
            print("Не удалось отрисовать превью скина:", e)
            return None

    def _sync_profile_with_server(self, nickname):
        if not nickname:
            return
        idx = None
        for i, acc in enumerate(self.core.accounts):
            if acc.get("name", "").lower() == nickname.lower():
                idx = i
                break
        if idx is None:
            self.core.accounts.append({"name": nickname})
            idx = len(self.core.accounts) - 1
        self.core.selected_account = idx
        self.update_accounts_ui()
        self.update_player_from_accounts()
        self.save_config()

    def rename_account(self):
        messagebox.showinfo(
            "Недоступно",
            "Профиль привязан к вашему аккаунту на сервере и не может быть переименован вручную."
        )

    def remove_account(self):
        sel = self.acc_listbox.curselection()
        if sel:
            idx = sel[0]
            del self.core.accounts[idx]
            if self.core.selected_account >= len(self.core.accounts):
                self.core.selected_account = max(0, len(self.core.accounts) - 1)
            self.update_accounts_ui()
            self.update_player_from_accounts()
            self.save_config()

    def select_account(self):
        sel = self.acc_listbox.curselection()
        if sel:
            self.core.selected_account = sel[0]
            self.update_accounts_ui()
            self.update_player_from_accounts()
            self.save_config()

    def build_skin_tab(self, frame):
        ttk.Label(
            frame,
            text="ℹ️ Выберите PNG-скин (64×64 или 64×32). Он загружается на наш сервер скинов и\n"
                 "виден всем, кто играет через этот лаунчер, — на нашем сервере и на любых\n"
                 "пиратских (offline) серверах. Нужен вход в аккаунт лаунчера. Игроки с другими\n"
                 "лаунчерами видят стандартного Стива/Алекс.",
            foreground="#7a8599", justify="left", wraplength=520
        ).pack(anchor="w", padx=10, pady=(10, 15))

        top = ttk.Frame(frame)
        top.pack(anchor="w", padx=10, fill="x")

        preview_frame = ttk.Frame(top)
        preview_frame.pack(side="left", padx=(0, 20))
        if HAS_PIL:
            self.skin_tab_preview = ttk.Label(preview_frame, text="🧑")
            self.skin_tab_preview.pack()
        else:
            ttk.Label(preview_frame, text="(PIL не установлен —\nпревью недоступно)",
                       foreground="#555", justify="center").pack()

        info_frame = ttk.Frame(top)
        info_frame.pack(side="left", fill="both", expand=True)

        self.skin_tab_player_label = ttk.Label(
            info_frame, text="Игрок: —", font=("Segoe UI", 11, "bold"), foreground=self._accent
        )
        self.skin_tab_player_label.pack(anchor="w", pady=(0, 4))

        self.skin_tab_source_label = ttk.Label(info_frame, text="", foreground="#7a8599")
        self.skin_tab_source_label.pack(anchor="w", pady=(0, 10))

        btn_row = ttk.Frame(info_frame)
        btn_row.pack(anchor="w")
        ttk.Button(btn_row, text="📁 Выбрать файл скина...",
                   command=self._choose_custom_skin).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="♻️ Сбросить (авто по нику)",
                   command=self._reset_custom_skin).pack(side="left")

        model_row = ttk.Frame(info_frame)
        model_row.pack(anchor="w", pady=(10, 0))
        ttk.Label(model_row, text="Руки:").pack(side="left", padx=(0, 6))
        acc0 = self._current_account() or {}
        self.skin_model_var = tk.StringVar(
            value="Тонкие (Alex)" if acc0.get("skin_model") == "slim" else "Классические (Steve)")
        model_cb = ttk.Combobox(model_row, textvariable=self.skin_model_var, state="readonly", width=22,
                                values=("Классические (Steve)", "Тонкие (Alex)"))
        model_cb.pack(side="left")
        model_cb.bind("<<ComboboxSelected>>", lambda e: self._on_skin_model_change())

        self._refresh_skin_tab()

    def _current_account(self):
        if self.core.accounts and 0 <= self.core.selected_account < len(self.core.accounts):
            return self.core.accounts[self.core.selected_account]
        return None

    def _refresh_skin_tab(self):
        if not hasattr(self, "skin_tab_player_label"):
            return
        acc = self._current_account()
        if not acc:
            self.skin_tab_player_label.config(text="Игрок: (не выбран)")
            self.skin_tab_source_label.config(text="")
            if HAS_PIL and hasattr(self, "skin_tab_preview"):
                self.skin_tab_preview.config(image="", text="🧑")
            return

        name = acc.get("name", "")
        self.skin_tab_player_label.config(text=f"Игрок: {name}")
        skin_path = acc.get("skin_path")

        if HAS_PIL and hasattr(self, "skin_tab_preview"):
            img = None
            if skin_path and os.path.isfile(skin_path):
                img = self._render_skin_face(skin_path, size=110)
            if img is not None:
                self.skin_tab_preview_image = img
                self.skin_tab_preview.config(image=img, text="")
            else:
                self._load_remote_preview_into(self.skin_tab_preview, name, size=110)

        if skin_path and os.path.isfile(skin_path):
            self.skin_tab_source_label.config(text=f"📁 Свой файл: {os.path.basename(skin_path)}")
        else:
            self.skin_tab_source_label.config(text="🌐 Автозагрузка по нику (со стандартного сервиса скинов)")

    def _load_remote_preview_into(self, label, player_name, size=110):
        if not HAS_PIL or not player_name:
            return

        def work():
            try:
                url = f"https://mc-heads.net/avatar/{player_name}/{size}"
                resp = requests.get(url, stream=True, timeout=5)
                if resp.status_code != 200:
                    return
                img = Image.open(resp.raw).convert("RGBA")
                img = img.resize((size, size), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)

                def apply():
                    self.skin_tab_preview_image = photo
                    label.config(image=photo, text="")

                self.root.after(0, apply)
            except Exception as e:
                print("Не удалось загрузить превью скина:", e)

        threading.Thread(target=work, daemon=True).start()

    def _skin_model_value(self):
        var = getattr(self, "skin_model_var", None)
        return "slim" if var is not None and "Alex" in var.get() else "default"

    def _skin_upload_async(self, acc, notify=True):
        """Заливает скин аккаунта на сервер скинов (в фоне). Без входа в аккаунт лаунчера не работает."""
        path = acc.get("skin_path")
        name = acc.get("name", "")
        model = acc.get("skin_model", "default")
        if not path or not os.path.isfile(path) or not name:
            return
        if not self.api.is_logged_in():
            if notify:
                messagebox.showinfo("Скин", "Скин сохранён локально. Чтобы его видели другие игроки, "
                                            "войдите в аккаунт на вкладке «🖥️ Аккаунт» и выберите скин ещё раз "
                                            "(или он загрузится при следующем запуске игры после входа).")
            return

        def work():
            try:
                with open(path, "rb") as f:
                    data = f.read()
                status, resp = self.api.skin_upload(name, data, model)
            except Exception as e:
                status, resp = 0, {"error": str(e)}
            ok = status == 200 and resp.get("success")
            if ok:
                acc["skin_uploaded"] = f"{os.path.getmtime(path)}:{model}"
                self.root.after(0, self.save_config)
            if notify or not ok:
                msg = "Скин загружен на сервер: его видят другие игроки лаунчера." if ok else \
                      f"Не удалось загрузить скин на сервер: {resp.get('error', 'нет связи')}"
                self.root.after(0, lambda: (messagebox.showinfo if ok else messagebox.showwarning)("Скин", msg))
        threading.Thread(target=work, daemon=True).start()

    def _skin_needs_upload(self, acc):
        path = acc.get("skin_path")
        if not path or not os.path.isfile(path):
            return False
        return acc.get("skin_uploaded") != f"{os.path.getmtime(path)}:{acc.get('skin_model', 'default')}"

    def _on_skin_model_change(self):
        acc = self._current_account()
        if not acc:
            return
        acc["skin_model"] = self._skin_model_value()
        acc.pop("skin_uploaded", None)
        self.save_config()
        if acc.get("skin_path"):
            self._skin_upload_async(acc, notify=True)

    def _choose_custom_skin(self):
        acc = self._current_account()
        if not acc:
            messagebox.showerror("Ошибка", "Сначала выберите или создайте профиль на вкладке «Профили».")
            return
        if not HAS_PIL:
            messagebox.showerror("Ошибка", "Для работы со скинами нужен установленный Pillow.")
            return

        path = filedialog.askopenfilename(
            title="Выберите файл скина (PNG 64×64 или 64×32)",
            filetypes=[("PNG-изображения", "*.png"), ("Все файлы", "*.*")]
        )
        if not path:
            return

        try:
            img = Image.open(path)
            if img.size not in ((64, 64), (64, 32)):
                messagebox.showerror(
                    "Некорректный файл",
                    f"Скин должен быть PNG размером 64×64 или 64×32 пикселей.\n"
                    f"Выбранный файл: {img.size[0]}×{img.size[1]}."
                )
                return
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть файл как изображение: {e}")
            return

        skins_dir = os.path.join(self.core.minecraft_dir, "skins")
        try:
            os.makedirs(skins_dir, exist_ok=True)
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", acc.get("name", "player"))
            dest_path = os.path.join(skins_dir, f"{safe_name}.png")
            shutil.copyfile(path, dest_path)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить скин: {e}")
            return

        acc["skin_path"] = dest_path
        acc["skin_model"] = self._skin_model_value()
        acc.pop("skin_uploaded", None)
        self.save_config()
        self._refresh_skin_tab()
        if HAS_PIL and self.config.get("skin_auto_download", True):
            self.load_skin(acc.get("name", ""))
        self._bump_achievement("skins_changed", 1)
        self._skin_upload_async(acc, notify=True)

    def _reset_custom_skin(self):
        acc = self._current_account()
        if not acc:
            return
        if acc.pop("skin_path", None) is not None:
            acc.pop("skin_uploaded", None)
            self.save_config()
            if self.api.is_logged_in():
                name = acc.get("name", "")
                threading.Thread(target=lambda: self.api.skin_delete(name), daemon=True).start()
        self._refresh_skin_tab()
        if HAS_PIL and self.config.get("skin_auto_download", True):
            self.load_skin(acc.get("name", ""))

    def _modpack_dir(self, name):
        safe = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "pack"
        return os.path.join(self.core.minecraft_dir, "modpacks", safe)

    def _find_modpack(self, name):
        return next((pk for pk in self.config.get("modpacks", []) if pk.get("name") == name), None)

    def _pack_backup_mgr(self, pk=None):
        if pk:
            pack_dir = self._modpack_dir(pk["name"])
            bdir = os.path.join(self.core.minecraft_dir, "modpack_backups", os.path.basename(pack_dir))
        else:
            pack_dir = self.core.minecraft_dir
            bdir = os.path.join(self.core.minecraft_dir, "modpack_backups", "main")
        return ModpackBackupManager(pack_dir, bdir)

    def auto_pack_backup(self, reason):
        if not self.config.get("auto_pack_backup", True):
            return None
        try:
            active = self.config.get("active_modpack", "")
            mgr = self._pack_backup_mgr(self._find_modpack(active) if active else None)
            path = mgr.create(reason, auto=True)
            mgr.prune(int(self.config.get("pack_backup_keep", 5)))
            return path
        except Exception as e:
            print(f"Автобэкап сборки не удался: {e}")
            return None

    def _on_pack_backup_settings(self):
        self.config["auto_pack_backup"] = bool(self.pack_autobackup_var.get())
        try:
            keep = int(self.pack_backup_keep_var.get())
        except Exception:
            keep = 5
        self.config["pack_backup_keep"] = max(1, min(30, keep))
        save_json_file(CONFIG_FILE, self.config)

    def modpack_backups_dialog(self):
        pk, selected = self._selected_modpack()
        if not selected:
            messagebox.showinfo("Бэкапы", "Выберите сборку в списке (или «Основную»).")
            return
        name = pk["name"] if pk else "Основная"
        is_active = (pk["name"] if pk else "") == self.config.get("active_modpack", "")
        ModpackBackupWindow(self, f"Бэкапы — {name}", self._pack_backup_mgr(pk),
                            on_restored=self.refresh_mods if is_active else None)

    def _apply_pack_overrides(self):
        pk = self._find_modpack(self.config.get("active_modpack", "")) if self.config.get("active_modpack") else None
        mem = int(pk.get("memory_mb") or 0) if pk else 0
        self.core.pack_memory = mem if mem >= 512 else None
        self.core.pack_java = (pk.get("java_path") or "").strip() or None if pk else None

    def _rebuild_managers(self):
        self.mod_manager = ModManager(self.core.minecraft_dir, self.core.game_dir)
        self.rp_manager = ResourcePackManager(self.core.minecraft_dir, self.core.game_dir)
        self.world_backups.set_dir(self.core.game_path())
        for fn in ("refresh_mods", "refresh_rps", "refresh_worlds"):
            try:
                getattr(self, fn)()
            except Exception:
                pass

    def build_modpacks_tab(self, frame):
        self.modpack_active_label = ttk.Label(frame, text="", font=("Segoe UI", 10, "bold"))
        self.modpack_active_label.pack(anchor="w", padx=10, pady=(0, 6))

        bk_row = ttk.Frame(frame)
        bk_row.pack(fill="x", padx=10, pady=(0, 4))
        self.pack_autobackup_var = tk.BooleanVar(value=bool(self.config.get("auto_pack_backup", True)))
        ttk.Checkbutton(bk_row, text="Автобэкап сборки перед установкой/удалением модов",
                        variable=self.pack_autobackup_var, command=self._on_pack_backup_settings).pack(side="left")
        self.pack_backup_keep_var = tk.IntVar(value=int(self.config.get("pack_backup_keep", 5)))
        keep_spin = ttk.Spinbox(bk_row, from_=1, to=30, width=4, textvariable=self.pack_backup_keep_var,
                                command=self._on_pack_backup_settings)
        keep_spin.pack(side="right")
        keep_spin.bind("<FocusOut>", lambda e: self._on_pack_backup_settings())
        ttk.Label(bk_row, text="Хранить автокопий:").pack(side="right", padx=(0, 6))

        area = ttk.Frame(frame)
        area.pack(fill="both", expand=True, padx=10, pady=5)
        self.modpack_listbox = tk.Listbox(area, height=14, bg="#242830", fg="white",
                                          selectbackground="#43b581", font=("Consolas", 10),
                                          exportselection=False)
        self.modpack_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(area, orient="vertical", command=self.modpack_listbox.yview)
        scroll.pack(side="left", fill="y")
        self.modpack_listbox.config(yscrollcommand=scroll.set)
        self.modpack_listbox.bind("<Double-Button-1>", lambda e: self.modpack_activate())

        btns = ttk.Frame(area)
        btns.pack(side="right", fill="y", padx=10)
        ttk.Button(btns, text="▶ Сделать активной", style="Accent.TButton",
                   command=self.modpack_activate).pack(pady=4, fill="x")
        ttk.Button(btns, text="➕ Создать из текущих", command=self.modpack_create).pack(pady=4, fill="x")
        ttk.Button(btns, text="🌐 Каталог сборок", command=lambda: ModrinthBrowser(self, "modpacks")).pack(pady=4, fill="x")
        ttk.Button(btns, text="💾 Обновить настройки", command=self.modpack_update_settings).pack(pady=4, fill="x")
        ttk.Button(btns, text="⚙ ОЗУ и Java", command=self.modpack_resources_dialog).pack(pady=4, fill="x")
        ttk.Button(btns, text="🛡 Бэкапы", command=self.modpack_backups_dialog).pack(pady=4, fill="x")
        ttk.Button(btns, text="📋 Дублировать", command=self.modpack_duplicate).pack(pady=4, fill="x")
        ttk.Button(btns, text="📤 Экспорт в .zip", command=self.modpack_export).pack(pady=4, fill="x")
        ttk.Button(btns, text="📥 Импорт из .zip", command=self.modpack_import).pack(pady=4, fill="x")
        ttk.Button(btns, text="🗑️ Удалить", command=self.modpack_delete).pack(pady=4, fill="x")
        ttk.Button(btns, text="📂 Открыть папку", command=self.modpack_open_folder).pack(pady=4, fill="x")
        self.refresh_modpacks()

    def refresh_modpacks(self):
        if not hasattr(self, "modpack_listbox"):
            return
        active = self.config.get("active_modpack", "")
        self.modpack_listbox.delete(0, tk.END)
        mark = "▶" if not active else " "
        self.modpack_listbox.insert(tk.END, f" {mark} Основная (по умолчанию)")
        for pk in self.config.get("modpacks", []):
            mark = "▶" if pk["name"] == active else " "
            info = " ".join(x for x in (pk.get("loader", "") if pk.get("loader") != "vanilla" else "vanilla",
                                        pk.get("mc_version", "")) if x)
            if int(pk.get("memory_mb") or 0) >= 512:
                info += f" • {int(pk['memory_mb'])} МБ"
            if (pk.get("java_path") or "").strip():
                info += " • своя Java"
            self.modpack_listbox.insert(tk.END, f" {mark} {pk['name']}" + (f"   [{info}]" if info else ""))
        extra = ""
        if self.core.pack_memory:
            extra += f"  •  ОЗУ: {self.core.pack_memory} МБ"
        if self.core.pack_java:
            extra += "  •  своя Java"
        self.modpack_active_label.config(text=f"Активная сборка: {active or 'Основная (по умолчанию)'}{extra}")

    def _selected_modpack(self):
        sel = self.modpack_listbox.curselection()
        if not sel:
            return None, False
        idx = sel[0]
        if idx == 0:
            return None, True
        packs = self.config.get("modpacks", [])
        return (packs[idx - 1] if idx - 1 < len(packs) else None), True

    def modpack_create(self):
        ModpackCreateDialog(self)

    def _unique_pack_name(self, base):
        base = re.sub(r'[\\/:*?"<>|]+', "_", base).strip(" .") or "Сборка"
        name, n = base, 2
        while self._find_modpack(name) or os.path.exists(self._modpack_dir(name)):
            name, n = f"{base} {n}", n + 1
        return name

    def _finish_mrpack_install(self, name, info):
        self.config.setdefault("modpacks", []).append({
            "name": name, "mc_version": info["mc_version"],
            "loader": info["loader"], "loader_version": info["loader_version"],
        })
        save_json_file(CONFIG_FILE, self.config)
        self.refresh_modpacks()
        text = f"Сборка «{name}» установлена ({info['loader']} {info['mc_version']})."
        failed = info.get("failed") or []
        if failed:
            text += f"\n\nНе удалось скачать ({len(failed)}):\n" + "\n".join(failed[:8])
            text += "\n\nСборка может не запуститься, пока эти файлы не будут добавлены."
        text += "\n\nСделать её активной?"
        if messagebox.askyesno("Сборка установлена", text):
            self._activate_modpack(name)

    def _create_modpack(self, name, mc_version, loader, loader_version):
        path = self._modpack_dir(name)
        try:
            os.makedirs(os.path.join(path, "mods"), exist_ok=True)
        except OSError as e:
            messagebox.showerror("Ошибка", f"Не удалось создать папку сборки: {e}")
            return False
        self.config.setdefault("modpacks", []).append({
            "name": name, "mc_version": mc_version, "loader": loader, "loader_version": loader_version,
        })
        save_json_file(CONFIG_FILE, self.config)
        self.refresh_modpacks()
        if messagebox.askyesno("Сборка создана", f"Сборка «{name}» создана.\nСделать её активной?"):
            self._activate_modpack(name)
        return True

    def modpack_activate(self):
        pk, selected = self._selected_modpack()
        if not selected:
            return
        self._activate_modpack(pk["name"] if pk else "")

    def _activate_modpack(self, name):
        if self.installing or self.process is not None and self.process.poll() is None:
            messagebox.showwarning("Сборки", "Нельзя менять сборку во время установки или запущенной игры.")
            return
        self.config["active_modpack"] = name
        self.core.game_dir = self._modpack_dir(name) if name else None
        if self.core.game_dir:
            os.makedirs(os.path.join(self.core.game_dir, "mods"), exist_ok=True)
        self._apply_pack_overrides()
        self._rebuild_managers()
        pk = self._find_modpack(name) if name else None
        if pk:
            if pk.get("mc_version"):
                self.version_var.set(pk["mc_version"])
            loader = pk.get("loader", "vanilla")
            self.modloader_var.set(loader)
            if pk.get("loader_version"):
                self._pending_loader_version[loader] = pk["loader_version"]
            self._on_loader_change()
        save_json_file(CONFIG_FILE, self.config)
        self.refresh_modpacks()
        self.status_var.set(f"Активная сборка: {name or 'Основная'}")

    def modpack_update_settings(self):
        pk, selected = self._selected_modpack()
        if not pk:
            messagebox.showinfo("Сборки", "Выберите сборку (у «Основной» нет сохранённых настроек).")
            return
        pk["mc_version"] = self.version_var.get().strip()
        pk["loader"] = self.modloader_var.get()
        pk["loader_version"] = self._current_loader_version()
        save_json_file(CONFIG_FILE, self.config)
        self.refresh_modpacks()

    def modpack_duplicate(self):
        pk, selected = self._selected_modpack()
        if not pk:
            messagebox.showinfo("Сборки", "Выберите сборку для копирования.")
            return
        base = f"{pk['name']} (копия)"
        name, n = base, 2
        while self._find_modpack(name):
            name, n = f"{base} {n}", n + 1
        src, dst = self._modpack_dir(pk["name"]), self._modpack_dir(name)

        def work():
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                os.makedirs(os.path.join(dst, "mods"), exist_ok=True)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", f"Не удалось скопировать сборку: {error}")
                return
            new = dict(pk)
            new["name"] = name
            self.config["modpacks"].append(new)
            save_json_file(CONFIG_FILE, self.config)
            self.refresh_modpacks()

        self.status_var.set("Копирование сборки...")
        self._async(work, done)

    def modpack_delete(self):
        pk, selected = self._selected_modpack()
        if not pk:
            messagebox.showinfo("Сборки", "Выберите сборку для удаления (основную удалить нельзя).")
            return
        if not messagebox.askyesno("Удаление", f"Удалить сборку «{pk['name']}» вместе со всеми её файлами "
                                              f"(моды, конфиги, миры)?\nЭто необратимо."):
            return
        if self.config.get("active_modpack") == pk["name"]:
            self._activate_modpack("")
            if self.config.get("active_modpack"):
                return
        shutil.rmtree(self._modpack_dir(pk["name"]), ignore_errors=True)
        shutil.rmtree(self._pack_backup_mgr(pk).backup_dir, ignore_errors=True)
        self.config["modpacks"] = [x for x in self.config.get("modpacks", []) if x is not pk]
        save_json_file(CONFIG_FILE, self.config)
        self.refresh_modpacks()

    def modpack_resources_dialog(self):
        pk, selected = self._selected_modpack()
        if not pk:
            messagebox.showinfo("Сборки", "Выберите сборку (у «Основной» используются общие настройки).")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title(f"ОЗУ и Java — {pk['name']}")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)

        mem_var = tk.StringVar(value=str(int(pk.get("memory_mb") or 0) or ""))
        java_var = tk.StringVar(value=(pk.get("java_path") or "").strip())

        ttk.Label(frm, text="ОЗУ (МБ):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=mem_var, width=12).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(frm, text=f"общая: {self.core.memory} МБ", foreground="#7a8599").grid(row=1, column=2, sticky="w")

        ttk.Label(frm, text="Путь к Java:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=java_var, width=44).grid(row=2, column=1, columnspan=2, sticky="w", padx=6)

        def browse():
            f = filedialog.askopenfilename(
                parent=dlg, title="Выберите java / javaw",
                filetypes=[("Java", "java.exe javaw.exe java"), ("Все файлы", "*.*")])
            if f:
                java_var.set(f)
        ttk.Button(frm, text="Обзор", command=browse).grid(row=3, column=1, sticky="w", padx=6, pady=(0, 6))
        ttk.Label(frm, text=f"общая: {self.core.java_path or 'java'}", foreground="#7a8599").grid(
            row=3, column=2, sticky="w")

        def save():
            mem_txt, java_txt = mem_var.get().strip(), java_var.get().strip()
            mem = 0
            if mem_txt:
                if not mem_txt.isdigit() or int(mem_txt) < 512:
                    messagebox.showerror("ОЗУ", "Введите число не меньше 512 (в МБ) или оставьте пустым.", parent=dlg)
                    return
                mem = int(mem_txt)
            if java_txt and java_txt != "java" and not os.path.isfile(java_txt):
                messagebox.showerror("Java", "Файл Java не найден по указанному пути.", parent=dlg)
                return
            pk["memory_mb"] = mem
            pk["java_path"] = java_txt
            save_json_file(CONFIG_FILE, self.config)
            if self.config.get("active_modpack") == pk["name"]:
                self._apply_pack_overrides()
            self.refresh_modpacks()
            dlg.destroy()

        row = ttk.Frame(frm)
        row.grid(row=4, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(row, text="Отмена", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Сохранить", style="Accent.TButton", command=save).pack(side="right")

    _PACK_MANIFEST = "mafin_pack.json"
    _EXPORT_SKIP_DIRS = {"logs", "crash-reports", "screenshots", "backups", ".mixin.out", "webcache", ".cache"}

    _MAIN_EXPORT_ITEMS = ("mods", "config", "resourcepacks", "shaderpacks", "options.txt", "optionsof.txt", "servers.dat")

    @staticmethod
    def _count_mods(folder):
        mods_dir = os.path.join(folder, "mods")
        if not os.path.isdir(mods_dir):
            return 0
        return sum(1 for f in os.listdir(mods_dir) if f.lower().endswith((".jar", ".jar.disabled", ".litemod")))

    def modpack_export(self):
        pk, selected = self._selected_modpack()
        if not selected:
            messagebox.showinfo("Экспорт", "Выберите сборку (или «Основную») в списке.")
            return
        is_main = pk is None
        src = self.core.minecraft_dir if is_main else self._modpack_dir(pk["name"])
        if not os.path.isdir(src):
            messagebox.showerror("Экспорт", "Папка сборки не найдена.")
            return
        pack_name = "Основная" if is_main else pk["name"]
        is_active = (self.config.get("active_modpack", "") == (pk["name"] if pk else ""))

        if is_main or is_active:
            settings = {"mc_version": self.version_var.get().strip(), "loader": self.modloader_var.get(),
                        "loader_version": self._current_loader_version(),
                        "memory_mb": int(self.core.pack_memory or 0) if not is_main else 0}
            if pk and is_active:
                pk.update({k: settings[k] for k in ("mc_version", "loader", "loader_version")})
                save_json_file(CONFIG_FILE, self.config)
        else:
            settings = {"mc_version": pk.get("mc_version", ""), "loader": pk.get("loader", "vanilla"),
                        "loader_version": pk.get("loader_version", ""), "memory_mb": int(pk.get("memory_mb") or 0)}

        copy_from_main = False
        if self._count_mods(src) == 0:
            main_mods = self._count_mods(self.core.minecraft_dir) if not is_main else 0
            if main_mods:
                copy_from_main = messagebox.askyesno(
                    "Экспорт", f"В сборке «{pack_name}» нет модов, а в основной папке их {main_mods}.\n"
                               f"\nСкопировать моды из основной папки в сборку перед экспортом?")
            if not copy_from_main and not messagebox.askyesno(
                    "Экспорт", f"В сборке «{pack_name}» нет модов — в архив попадут только конфиги и настройки.\n"
                               f"Папка сборки: {os.path.join(src, 'mods')}\n\nПродолжить экспорт?"):
                return

        with_worlds = messagebox.askyesno(
            "Экспорт", "Включить в архив миры (папка saves)?")
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", pack_name).strip(" .") or "pack"
        path = filedialog.asksaveasfilename(
            title="Сохранить сборку", defaultextension=".zip", initialfile=f"{safe_name}.zip",
            filetypes=[("ZIP-архив", "*.zip")])
        if not path:
            return
        manifest = {"format": 1, "name": pack_name, "mc_version": settings["mc_version"],
                    "loader": settings["loader"], "loader_version": settings["loader_version"],
                    "memory_mb": settings["memory_mb"], "launcher": f"Mafin Launcher v{APP_VERSION}"}
        skip = set(self._EXPORT_SKIP_DIRS)
        if not with_worlds:
            skip.add("saves")
        out_abs = os.path.abspath(path)
        main_dir = self.core.minecraft_dir

        def entries():
            if is_main:
                items = list(self._MAIN_EXPORT_ITEMS) + (["saves"] if with_worlds else [])
                for item in items:
                    full_item = os.path.join(src, item)
                    if os.path.isfile(full_item):
                        yield full_item, item
                    elif os.path.isdir(full_item):
                        for root_dir, _d, files in os.walk(full_item):
                            for fn in files:
                                full = os.path.join(root_dir, fn)
                                yield full, os.path.relpath(full, src).replace(os.sep, "/")
                return
            for root_dir, dirs, files in os.walk(src):
                if os.path.abspath(root_dir) == os.path.abspath(src):
                    dirs[:] = [d for d in dirs if d not in skip]
                for fn in files:
                    full = os.path.join(root_dir, fn)
                    yield full, os.path.relpath(full, src).replace(os.sep, "/")

        def work():
            seen, count, mods = set(), 0, 0
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(self._PACK_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
                for full, rel in entries():
                    if os.path.abspath(full) == out_abs or full.endswith(".part"):
                        continue
                    seen.add(rel)
                    comp = zipfile.ZIP_STORED if rel.lower().endswith((".jar", ".jar.disabled")) else zipfile.ZIP_DEFLATED
                    zf.write(full, rel, compress_type=comp)
                    count += 1
                    if rel.startswith("mods/") and rel.lower().endswith((".jar", ".jar.disabled", ".litemod")):
                        mods += 1
                if copy_from_main:
                    for fn in sorted(os.listdir(os.path.join(main_dir, "mods"))):
                        if fn.lower().endswith((".jar", ".jar.disabled", ".litemod")) and f"mods/{fn}" not in seen:
                            zf.write(os.path.join(main_dir, "mods", fn), f"mods/{fn}", compress_type=zipfile.ZIP_STORED)
                            count += 1
                            mods += 1
            return count, os.path.getsize(path), mods

        def done(result, error):
            if error:
                try:
                    os.remove(path)
                except OSError:
                    pass
                messagebox.showerror("Экспорт", f"Не удалось создать архив: {error}")
                self.status_var.set("Готов")
                return
            count, size, mods = result
            self.status_var.set(f"«{pack_name}» экспортирована: {mods} модов, {count} файлов, {size / 1048576:.1f} МБ")
            self._tray_notify("Mafin Launcher", f"«{pack_name}» экспортирована ({mods} модов)")
            if mods == 0:
                messagebox.showwarning("Экспорт", "В архив не попало ни одного мода.\n"
                                                  "Проверьте, что моды лежат в папке mods нужной сборки.")
            self.refresh_modpacks()

        self.status_var.set(f"Экспорт «{pack_name}»...")
        self._async(work, done)

    def modpack_import(self):
        path = filedialog.askopenfilename(
            title="Выберите архив сборки",
            filetypes=[("Сборки (.zip, .mrpack)", "*.zip *.mrpack"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            with zipfile.ZipFile(path) as zf:
                is_mrpack = "modrinth.index.json" in zf.namelist()
        except Exception:
            is_mrpack = False
        if is_mrpack:
            self._import_mrpack(path)
            return
        try:
            with zipfile.ZipFile(path) as zf:
                manifest = {}
                if self._PACK_MANIFEST in zf.namelist():
                    manifest = json.loads(zf.read(self._PACK_MANIFEST).decode("utf-8"))
        except Exception as e:
            messagebox.showerror("Импорт", f"Не удалось прочитать архив: {e}")
            return
        base = (manifest.get("name") or os.path.splitext(os.path.basename(path))[0]).strip() or "Импортированная"
        name, n = base, 2
        while self._find_modpack(name) or os.path.exists(self._modpack_dir(name)):
            name, n = f"{base} {n}", n + 1
        dest = self._modpack_dir(name)

        def work():
            dest_real = os.path.realpath(dest)
            os.makedirs(dest, exist_ok=True)
            count = 0
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.filename == self._PACK_MANIFEST:
                        continue
                    target = os.path.realpath(os.path.join(dest, info.filename))
                    if target != dest_real and not target.startswith(dest_real + os.sep):
                        raise Exception(f"Небезопасный путь в архиве: {info.filename}")
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src_f, open(target, "wb") as out_f:
                        shutil.copyfileobj(src_f, out_f)
                    count += 1
            os.makedirs(os.path.join(dest, "mods"), exist_ok=True)
            return count

        def done(result, error):
            if error:
                shutil.rmtree(dest, ignore_errors=True)
                messagebox.showerror("Импорт", f"Не удалось импортировать сборку: {error}")
                self.status_var.set("Готов")
                return
            self.config.setdefault("modpacks", []).append({
                "name": name,
                "mc_version": manifest.get("mc_version", ""),
                "loader": manifest.get("loader", "vanilla"),
                "loader_version": manifest.get("loader_version", ""),
                "memory_mb": int(manifest.get("memory_mb") or 0),
            })
            save_json_file(CONFIG_FILE, self.config)
            self.refresh_modpacks()
            self.status_var.set(f"Сборка «{name}» импортирована: {result} файлов")
            self._tray_notify("Mafin Launcher", f"Сборка «{name}» импортирована")
            info = ""
            if manifest.get("mc_version"):
                info = (f"\n\nНужна версия: {manifest['mc_version']} "
                        f"{manifest.get('loader', '')} {manifest.get('loader_version', '')}".rstrip())
            if messagebox.askyesno("Импорт", f"Сборка «{name}» импортирована.{info}\n\nСделать её активной?"):
                self._activate_modpack(name)

        self.status_var.set("Импорт сборки...")
        self._async(work, done)

    def _import_mrpack(self, path):
        try:
            with zipfile.ZipFile(path) as zf:
                index = json.loads(zf.read("modrinth.index.json").decode("utf-8-sig"))
        except Exception as e:
            messagebox.showerror("Импорт .mrpack", f"Не удалось прочитать modrinth.index.json: {e}")
            return
        if index.get("game", "minecraft") != "minecraft":
            messagebox.showerror("Импорт .mrpack", "Это сборка не для Minecraft.")
            return
        deps = index.get("dependencies") or {}
        mc = str(deps.get("minecraft", ""))
        loader, loader_version = "vanilla", ""
        for key, lname in (("neoforge", "neoforge"), ("fabric-loader", "fabric"),
                           ("quilt-loader", "quilt"), ("forge", "forge")):
            if key in deps:
                loader, loader_version = lname, str(deps[key])
                break
        if loader == "forge" and mc and "-" not in loader_version:
            loader_version = f"{mc}-{loader_version}"
        files = [f for f in (index.get("files") or []) if (f.get("env") or {}).get("client") != "unsupported"]
        total_bytes = sum(int(f.get("fileSize") or 0) for f in files)

        base = (index.get("name") or os.path.splitext(os.path.basename(path))[0]).strip() or "Modrinth-сборка"
        name, n = base, 2
        while self._find_modpack(name) or os.path.exists(self._modpack_dir(name)):
            name, n = f"{base} {n}", n + 1
        dest = self._modpack_dir(name)
        loader_txt = f"{loader} {loader_version}".strip()
        if not messagebox.askyesno(
                "Импорт .mrpack",
                f"Сборка: {name}\nMinecraft: {mc or '?'}  •  {loader_txt}\n"
                f"Файлов для скачивания: {len(files)}"
                + (f" (~{total_bytes / 1048576:.0f} МБ)" if total_bytes else "") + "\n\nИмпортировать?"):
            return
        dest_real_holder = {}

        def safe_target(rel):
            rel = rel.replace("\\", "/").lstrip("/")
            target = os.path.realpath(os.path.join(dest, rel))
            root_real = dest_real_holder["root"]
            if not rel or ".." in rel.split("/") or (target != root_real and not target.startswith(root_real + os.sep)):
                raise Exception(f"Небезопасный путь в сборке: {rel}")
            return target

        def ui_progress(text, value=None):
            def apply():
                self.status_var.set(text)
                if value is not None:
                    self.progress.config(value=value)
            self.root.after(0, apply)

        def work():
            os.makedirs(dest, exist_ok=True)
            dest_real_holder["root"] = os.path.realpath(dest)
            done_bytes = 0
            for i, f in enumerate(files, 1):
                target = safe_target(f.get("path", ""))
                size = int(f.get("fileSize") or 0)
                label = os.path.basename(target)
                urls = [u for u in (f.get("downloads") or []) if str(u).startswith("https://")]
                if not urls:
                    raise Exception(f"Нет ссылки для скачивания: {label}")
                last_err = None
                for url in urls:
                    try:
                        def prog(d, t, base_done=done_bytes, idx=i, lbl=label):
                            overall = base_done + d
                            if total_bytes:
                                ui_progress(f"Импорт «{name}»: {idx}/{len(files)} • {lbl} • "
                                            f"{_fmt_mb(overall, total_bytes)}", overall * 100 / total_bytes)
                            else:
                                ui_progress(f"Импорт «{name}»: {idx}/{len(files)} • {lbl} • {_fmt_mb(overall)}")
                        modrinth_download_file({"url": url, "filename": label, "hashes": f.get("hashes") or {},
                                                "size": size}, "", prog, dest_path=target)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                if last_err:
                    raise Exception(f"{label}: {last_err}")
                done_bytes += size
            ui_progress(f"Импорт «{name}»: распаковываю настройки сборки…")
            extracted = 0
            with zipfile.ZipFile(path) as zf:
                for prefix in ("overrides/", "client-overrides/"):
                    for info in zf.infolist():
                        if not info.filename.startswith(prefix) or info.is_dir():
                            continue
                        target = safe_target(info.filename[len(prefix):])
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with zf.open(info) as src_f, open(target, "wb") as out_f:
                            shutil.copyfileobj(src_f, out_f)
                        extracted += 1
            os.makedirs(os.path.join(dest, "mods"), exist_ok=True)
            return len(files), extracted

        def done(result, error):
            self.progress.config(value=0)
            if error:
                shutil.rmtree(dest, ignore_errors=True)
                messagebox.showerror("Импорт .mrpack", f"Не удалось импортировать сборку: {error}")
                self.status_var.set("Готов")
                return
            self.config.setdefault("modpacks", []).append({
                "name": name, "mc_version": mc, "loader": loader,
                "loader_version": loader_version, "memory_mb": 0})
            save_json_file(CONFIG_FILE, self.config)
            self.refresh_modpacks()
            self.status_var.set(f"«{name}» импортирована: скачано {result[0]}, из настроек {result[1]} файлов")
            self._tray_notify("Mafin Launcher", f"Сборка «{name}» импортирована")
            if messagebox.askyesno(
                    "Импорт .mrpack",
                    f"Сборка «{name}» импортирована.\n\nНужна версия: {mc} {loader_txt}.\n\n"
                    f"Сделать сборку активной?"):
                self._activate_modpack(name)

        self.status_var.set(f"Импорт «{name}»…")
        self.progress.config(value=0)
        self._async(work, done)

    def modpack_open_folder(self):
        pk, selected = self._selected_modpack()
        path = self._modpack_dir(pk["name"]) if pk else self.core.minecraft_dir
        os.makedirs(path, exist_ok=True)
        self._open_folder(path)

    def build_mods_tab(self, frame):
        ttk.Label(frame, text="📦 Моды работают только с Forge/Fabric/NeoForge/Quilt.",
                  foreground="#7a8599").pack(anchor="w", padx=10, pady=(10, 5))
        list_area = ttk.Frame(frame)
        list_area.pack(fill="both", expand=True, padx=10, pady=5)

        self._mod_names = []
        self.mod_listbox = tk.Listbox(list_area, height=14, bg="#242830", fg="white",
                                       selectbackground="#43b581", font=("Consolas", 10),
                                       exportselection=False)
        self.mod_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_area, orient="vertical", command=self.mod_listbox.yview)
        scroll.pack(side="left", fill="y")
        self.mod_listbox.config(yscrollcommand=scroll.set)

        btn_frame = ttk.Frame(list_area)
        btn_frame.pack(side="right", fill="y", padx=10)
        ttk.Button(btn_frame, text="🔎 Каталог Modrinth", style="Accent.TButton",
                   command=lambda: ModrinthBrowser(self, "mods")).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="➕ Добавить мод", command=self.add_mod).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="✅ Включить", command=self.enable_mod).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="❌ Отключить", command=self.disable_mod).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="🗑️ Удалить", command=self.delete_mod).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="⬆ Обновить моды", command=lambda: ModUpdateWindow(self)).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="🔍 Проверить моды", command=lambda: ModCheckWindow(self)).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="🎯 Найти виновного", command=lambda: ModBisectWindow(self)).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="🔄 Обновить", command=self.refresh_mods).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="📂 Открыть папку", command=self._open_mods_folder).pack(pady=4, fill="x")
        self.refresh_mods()
        self.root.after(1500, self._bisect_check_leftover)

    def _open_mods_folder(self):
        self._open_folder(self.mod_manager.mods_dir)

    def refresh_mods(self):
        if not hasattr(self, "mod_listbox"):
            return
        self.mod_listbox.delete(0, tk.END)
        self._mod_names = []
        for name, enabled in self.mod_manager.list_mods():
            marker = "✅" if enabled else "❌"
            self._mod_names.append(name)
            self.mod_listbox.insert(tk.END, f" {marker}  {name}")

    def add_mod(self):
        files = filedialog.askopenfilenames(
            title="Выберите файлы модов",
            filetypes=[("Файлы модов", "*.jar *.litemod"), ("Все файлы", "*.*")]
        )
        added = 0
        for file in files:
            try:
                self.mod_manager.add_mod(file)
                added += 1
            except Exception as e:
                messagebox.showerror("Ошибка", str(e))
                return
        self.refresh_mods()
        if added:
            self._bump_achievement("mods_installed", added)

    def _get_selected_mod_name(self):
        sel = self.mod_listbox.curselection()
        if not sel:
            return None
        idx = sel[0]
        return self._mod_names[idx] if idx < len(self._mod_names) else None

    def enable_mod(self):
        name = self._get_selected_mod_name()
        if name:
            self.mod_manager.set_enabled(name, True)
            self.refresh_mods()

    def disable_mod(self):
        name = self._get_selected_mod_name()
        if name:
            self.mod_manager.set_enabled(name, False)
            self.refresh_mods()

    def delete_mod(self):
        name = self._get_selected_mod_name()
        if not name:
            messagebox.showinfo("Удаление", "Сначала выберите мод в списке.")
            return
        if not messagebox.askyesno("Удаление", f"Удалить мод {name}?"):
            return
        manager = self.mod_manager

        def work():
            self.auto_pack_backup("delete-mod")
            manager.delete_mod(name)

        def done(result, error):
            if error:
                messagebox.showerror("Удаление", f"Не удалось удалить {name}: {error}")
            self.refresh_mods()
        self.status_var.set(f"Удаляю {name}...")
        self._async(work, done)

    def build_backups_tab(self, frame):
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.auto_backup_var = tk.BooleanVar(value=bool(self.config.get("auto_backup_worlds", False)))
        ttk.Checkbutton(top, text="Автобэкап изменённых миров перед запуском игры",
                        variable=self.auto_backup_var, command=self._on_backup_settings).pack(side="left")
        self.backup_keep_var = tk.IntVar(value=int(self.config.get("backup_keep", 5)))
        spin = ttk.Spinbox(top, from_=1, to=30, width=4, textvariable=self.backup_keep_var,
                           command=self._on_backup_settings)
        spin.pack(side="right")
        spin.bind("<FocusOut>", lambda e: self._on_backup_settings())
        ttk.Label(top, text="Хранить копий на мир:").pack(side="right", padx=(0, 6))

        cols = ttk.Frame(frame)
        cols.pack(fill="both", expand=True, padx=10, pady=6)
        left = ttk.Frame(cols)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        ttk.Label(left, text="🌍 Миры").pack(anchor="w", pady=(0, 3))
        self.world_listbox = tk.Listbox(left, exportselection=False, font=("Consolas", 10))
        self.world_listbox.pack(fill="both", expand=True)
        self.world_listbox.bind("<<ListboxSelect>>", lambda e: self._refresh_backup_list())
        right = ttk.Frame(cols)
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        ttk.Label(right, text="💾 Резервные копии выбранного мира").pack(anchor="w", pady=(0, 3))
        self.backup_listbox = tk.Listbox(right, exportselection=False, font=("Consolas", 10))
        self.backup_listbox.pack(fill="both", expand=True)

        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=10, pady=(2, 4))
        ttk.Button(bar, text="💾 Создать копию", style="Accent.TButton",
                   command=self.backup_create).pack(side="left")
        ttk.Button(bar, text="♻ Восстановить", command=self.backup_restore).pack(side="left", padx=6)
        ttk.Button(bar, text="🗑️ Удалить копию", command=self.backup_delete).pack(side="left")
        ttk.Button(bar, text="🔄", width=3, command=self.refresh_worlds).pack(side="right")
        ttk.Button(bar, text="📂 Папка копий", command=lambda: self._open_folder(
            self.world_backups.backup_dir)).pack(side="right", padx=6)
        self.backup_status = tk.Label(frame, text="", anchor="w", fg="#7a8599")
        self.backup_status.pack(fill="x", padx=12, pady=(0, 8))
        self.refresh_worlds()

    def _on_backup_settings(self):
        self.config["auto_backup_worlds"] = bool(self.auto_backup_var.get())
        try:
            self.config["backup_keep"] = max(1, min(30, int(self.backup_keep_var.get())))
        except (tk.TclError, ValueError):
            self.config["backup_keep"] = 5
        self.save_config()

    def _backup_msg(self, text, error=False):
        self.backup_status.configure(text=text, fg="#ed4245" if error else "#7a8599")

    def refresh_worlds(self):
        self.world_backups.set_dir(self.core.game_dir or self.dir_var.get().strip())
        keep = self.world_listbox.get(self.world_listbox.curselection()[0]) \
            if self.world_listbox.curselection() else None
        self.world_listbox.delete(0, tk.END)
        worlds = self.world_backups.list_worlds()
        for w in worlds:
            self.world_listbox.insert(tk.END, w)
        if keep in worlds:
            self.world_listbox.selection_set(worlds.index(keep))
        elif worlds:
            self.world_listbox.selection_set(0)
        self._refresh_backup_list()
        if not worlds:
            self._backup_msg("Миров не найдено — они появятся после первой игры.")

    def _selected_world(self):
        sel = self.world_listbox.curselection()
        return self.world_listbox.get(sel[0]) if sel else None

    def _refresh_backup_list(self):
        self.backup_listbox.delete(0, tk.END)
        self._backup_items = self.world_backups.list_backups(self._selected_world()) \
            if self._selected_world() else []
        for item in self._backup_items:
            when = datetime.fromtimestamp(item["mtime"]).strftime("%d.%m.%Y %H:%M")
            self.backup_listbox.insert(tk.END, f" {when}   •   {item['size'] / 1048576:.1f} МБ")

    def backup_create(self):
        world = self._selected_world()
        if not world:
            self._backup_msg("Выберите мир слева", error=True)
            return
        self._backup_msg(f"Создаю копию «{world}»…")

        def work():
            path = self.world_backups.create(world)
            self.world_backups.prune(world, int(self.config.get("backup_keep", 5)))
            return path

        def done(result, error):
            if error:
                self._backup_msg(f"Ошибка: {error}", error=True)
            else:
                self._backup_msg(f"Готово: {os.path.basename(result)}")
                self._refresh_backup_list()

        self._async(work, done)

    def backup_restore(self):
        sel = self.backup_listbox.curselection()
        if not sel:
            self._backup_msg("Выберите копию справа", error=True)
            return
        item = self._backup_items[sel[0]]
        if not messagebox.askyesno(
                "Восстановление", f"Восстановить мир «{item['world']}» из этой копии?\n\n"
                "Текущая версия мира не пропадёт — она будет переименована и сохранена рядом."):
            return

        def done(result, error):
            if error:
                self._backup_msg(f"Ошибка: {error}", error=True)
                return
            owner, saved_as = result
            self._backup_msg(f"Мир «{owner}» восстановлен" + (f", прежний сохранён как «{saved_as}»" if saved_as else ""))
            self.refresh_worlds()

        self._async(lambda: self.world_backups.restore(item["path"]), done)

    def backup_delete(self):
        sel = self.backup_listbox.curselection()
        if not sel:
            self._backup_msg("Выберите копию справа", error=True)
            return
        item = self._backup_items[sel[0]]
        if messagebox.askyesno("Удаление", "Удалить эту резервную копию?"):
            try:
                self.world_backups.delete(item["path"])
            except OSError as e:
                self._backup_msg(f"Ошибка: {e}", error=True)
                return
            self._refresh_backup_list()

    def _auto_backup_worlds(self, minecraft_dir):
        if not self.config.get("auto_backup_worlds"):
            return
        manager = WorldBackupManager(minecraft_dir)
        keep = int(self.config.get("backup_keep", 5))

        def run():
            for world in manager.list_worlds():
                last = manager.list_backups(world)
                if last and manager.world_mtime(world) <= last[0]["mtime"]:
                    continue
                try:
                    manager.create(world)
                    manager.prune(world, keep)
                except Exception as e:
                    print(f"Автобэкап мира {world} не удался: {e}")
        threading.Thread(target=run, daemon=True).start()

    def build_versions_tab(self, frame):
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(top, text="📁 Скачанные версии:").pack(side="left")
        self.total_size_label = ttk.Label(top, text="", foreground=self._accent)
        self.total_size_label.pack(side="right")

        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.installed_listbox = tk.Listbox(list_frame, height=14, bg="#242830", fg="white",
                                             selectbackground="#43b581", font=("Consolas", 10))
        self.installed_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.installed_listbox.yview)
        scroll.pack(side="left", fill="y")
        self.installed_listbox.config(yscrollcommand=scroll.set)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="🔄 Обновить", command=self.refresh_installed_versions).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🗑️ Удалить версию", command=self.delete_selected_version).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🧹 Очистить кэш", command=self.clean_orphan_assets).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="✨ Умная очистка", command=self.run_smart_cleanup).pack(side="left", padx=5)
        self.refresh_installed_versions()

    def refresh_installed_versions(self):
        if not hasattr(self, "installed_listbox"):
            return
        self.installed_listbox.delete(0, tk.END)
        for v in self.core.get_installed_versions():
            self.installed_listbox.insert(tk.END, f"  {v['id']}   —   {v['size_mb']} МБ")
        total = self.core.get_total_game_size()
        if total > 1024 * 1024 * 1024:
            txt = f"📊 Общий размер: {total / (1024**3):.1f} ГБ"
        else:
            txt = f"📊 Общий размер: {total / (1024**2):.0f} МБ"
        self.total_size_label.config(text=txt)

    def _elevated_delete_plan(self, plan):
        if not sys.platform.startswith("win"):
            return None
        import ctypes

        tmp_dir = tempfile.gettempdir()
        token = uuid_module.uuid4().hex
        marker = os.path.join(tmp_dir, f"mafin_del_done_{token}.tmp")
        bat_path = os.path.join(tmp_dir, f"mafin_del_{token}.bat")

        lines = [f'rmdir /s /q "{plan["target"]}"']
        if plan["idx_file"]:
            lines.append(f'del /f /q "{plan["idx_file"]}"')

        for full in plan["lib_files"]:
            lines.append(f'del /f /q "{full}"')
        lib_dirs = set()
        for full in plan["lib_files"]:
            parent = os.path.dirname(full)
            while parent.startswith(plan["libs_dir"]) and parent != plan["libs_dir"]:
                lib_dirs.add(parent)
                parent = os.path.dirname(parent)
        for d in sorted(lib_dirs, key=len, reverse=True):
            lines.append(f'rmdir "{d}" 2>nul')

        for fpath in plan["asset_files"]:
            lines.append(f'del /f /q "{fpath}"')
        for d in sorted({os.path.dirname(f) for f in plan["asset_files"]}):
            lines.append(f'rmdir "{d}" 2>nul')

        lines.append(f'echo done > "{marker}"')

        try:
            with open(bat_path, "w", encoding="cp866", errors="replace") as f:
                f.write("\r\n".join(lines) + "\r\n")
        except Exception:
            with open(bat_path, "w", encoding="utf-8", errors="replace") as f:
                f.write("\r\n".join(lines) + "\r\n")

        try:
            ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f'/c "{bat_path}"', None, 0)
        except Exception:
            ret = 0
        if ret <= 32:
            try:
                os.remove(bat_path)
            except OSError:
                pass
            return None

        done = False
        for _ in range(300):
            if os.path.isfile(marker):
                done = True
                break
            time.sleep(0.1)

        removed_libs = sum(1 for f in plan["lib_files"] if not os.path.exists(f))
        removed_assets = sum(1 for f in plan["asset_files"] if not os.path.exists(f))

        for p in (marker, bat_path):
            try:
                os.remove(p)
            except OSError:
                pass

        if not done or os.path.exists(plan["target"]):
            return None
        return {"removed_libs": removed_libs, "removed_assets": removed_assets}

    def delete_selected_version(self):
        sel = self.installed_listbox.curselection()
        if not sel:
            return
        entry = self.installed_listbox.get(sel[0])
        version_id = entry.split("—")[0].strip()
        if not messagebox.askyesno("Удаление", f"Удалить версию {version_id}?"):
            return

        try:
            plan = self.core.build_delete_plan(version_id)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return

        try:
            result = self.core.execute_delete_plan(plan)
            self.refresh_installed_versions()
            msg = f"Версия {version_id} удалена.\n"
            msg += f"Очищено библиотек: {result['removed_libs']}\n"
            msg += f"Очищено ассетов: {result['removed_assets']}"
            messagebox.showinfo("Готово", msg)
        except PermissionError:
            if not messagebox.askyesno(
                "Нужны права администратора",
                f"Не удалось удалить версию {version_id} - не хватает прав доступа "
                f"(похоже, лаунчер установлен в защищённую папку Windows).\n\n"
                f"Запросить права администратора и повторить удаление "
                f"(версия + неиспользуемые библиотеки и ассеты)?"
            ):
                return
            self.status_var.set("Ожидание подтверждения UAC...")
            self.root.update_idletasks()
            result = self._elevated_delete_plan(plan)
            self.status_var.set("Готов")
            if result:
                self.refresh_installed_versions()
                msg = f"Версия {version_id} удалена с правами администратора.\n"
                msg += f"Очищено библиотек: {result['removed_libs']}\n"
                msg += f"Очищено ассетов: {result['removed_assets']}"
                messagebox.showinfo("Готово", msg)
            else:
                messagebox.showerror(
                    "Ошибка",
                    "Не удалось удалить версию даже с правами администратора "
                    "(UAC был отклонён или удаление не завершилось)."
                )
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def clean_orphan_assets(self):
        if not messagebox.askyesno("Очистка", "Удалить неиспользуемые файлы ассетов?"):
            return
        used_hashes = self.core._get_used_asset_hashes()
        objects_dir = os.path.join(self.core.minecraft_dir, "assets", "objects")
        removed = 0
        freed = 0
        if os.path.isdir(objects_dir):
            for d in os.listdir(objects_dir):
                sub = os.path.join(objects_dir, d)
                if not os.path.isdir(sub) or len(d) != 2:
                    continue
                for fname in os.listdir(sub):
                    if fname not in used_hashes:
                        fpath = os.path.join(sub, fname)
                        if os.path.isfile(fpath):
                            freed += os.path.getsize(fpath)
                            os.remove(fpath)
                            removed += 1
        messagebox.showinfo("Очистка", f"Удалено {removed} файлов, освобождено {freed / (1024**2):.1f} МБ")
        self.refresh_installed_versions()
        if removed:
            self._bump_achievement("orphan_asset_cleanups", 1)

    def run_smart_cleanup(self):
        self.status_var.set("🧹 Умная очистка...")

        def work():
            freed, removed = self.core.smart_cleanup(
                status_callback=lambda t: self.root.after(0, lambda: self.status_var.set(t))
            )

            def done():
                self.status_var.set("Готов")
                if removed:
                    messagebox.showinfo(
                        "Умная очистка",
                        f"Удалено объектов: {removed}\nОсвобождено: {freed / (1024**2):.1f} МБ"
                    )
                else:
                    messagebox.showinfo("Умная очистка", "Временных файлов не найдено, всё чисто.")
                self.refresh_installed_versions()
                self._bump_achievement("smart_cleanups", 1)

            self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def build_resourcepacks_tab(self, frame):
        ttk.Label(frame, text="🎨 Ресурспаки (текстуры, звуки и т.д.)",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=10, pady=(10, 5))

        list_area = ttk.Frame(frame)
        list_area.pack(fill="both", expand=True, padx=10, pady=5)

        self.rp_listbox = tk.Listbox(list_area, height=14, bg="#242830", fg="white",
                                      selectbackground="#43b581", font=("Consolas", 10))
        self.rp_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_area, orient="vertical", command=self.rp_listbox.yview)
        scroll.pack(side="left", fill="y")
        self.rp_listbox.config(yscrollcommand=scroll.set)

        btn_frame = ttk.Frame(list_area)
        btn_frame.pack(side="right", fill="y", padx=10)
        ttk.Button(btn_frame, text="🔎 Каталог Modrinth", style="Accent.TButton",
                   command=lambda: ModrinthBrowser(self, "resourcepacks")).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="➕ Добавить", command=self.add_rp).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="🗑️ Удалить", command=self.delete_rp).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="🔄 Обновить", command=self.refresh_rps).pack(pady=4, fill="x")
        ttk.Button(btn_frame, text="📂 Открыть папку", command=self._open_rp_folder).pack(pady=4, fill="x")
        self.refresh_rps()

    def _open_rp_folder(self):
        self._open_folder(self.rp_manager.rp_dir)

    def refresh_rps(self):
        if not hasattr(self, "rp_listbox"):
            return
        self.rp_listbox.delete(0, tk.END)
        for rp in self.rp_manager.list_packs():
            self.rp_listbox.insert(tk.END, f"  📦 {rp['name']}  ({rp['size_kb']} КБ)")

    def add_rp(self):
        files = filedialog.askopenfilenames(
            title="Выберите ресурспак",
            filetypes=[("ZIP архивы", "*.zip"), ("Все файлы", "*.*")]
        )
        added = 0
        for file in files:
            try:
                self.rp_manager.add_pack(file)
                added += 1
            except Exception as e:
                messagebox.showerror("Ошибка", str(e))
                return
        self.refresh_rps()
        if added:
            self._bump_achievement("resourcepacks_installed", added)

    def delete_rp(self):
        sel = self.rp_listbox.curselection()
        if not sel:
            return
        text = self.rp_listbox.get(sel[0])
        match = re.search(r"📦\s+(.+?)\s+\(", text)
        if match:
            name = match.group(1)
            if messagebox.askyesno("Удаление", f"Удалить {name}?"):
                self.rp_manager.delete_pack(name)
                self.refresh_rps()

    def build_servers_tab(self, frame):
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.server_listbox = tk.Listbox(list_frame, height=14, bg="#242830", fg="white",
                                          selectbackground="#43b581", font=("Consolas", 10))
        self.server_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.server_listbox.yview)
        scroll.pack(side="left", fill="y")
        self.server_listbox.config(yscrollcommand=scroll.set)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="▶ Играть на выбранном", style="Accent.TButton",
                   command=self.play_selected_server).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="➕ Добавить", command=self.add_server).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="✏ Изменить", command=self.edit_server).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🗑️ Удалить", command=self.remove_server).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="📶 Проверить", command=self.ping_server).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🧩 Сборка сервера", command=self.open_server_pack).pack(side="left", padx=5)
        self.server_listbox.bind("<Double-Button-1>", lambda e: self.play_selected_server())
        self._pinned_server = None
        self.refresh_servers_ui()
        self.refresh_pinned_server()
        self._schedule_pinned_refresh()

    def _pinned_label(self):
        p = self._pinned_server
        if p.get("ready"):
            state = f"🟢 {p.get('players_online', 0)}/{p.get('players_max', 0)}"
        elif p.get("running"):
            state = "🟡 запускается"
        else:
            state = "🔴 выключен"
        ver = format_server_versions({"version_from": p.get("version_min") or p.get("version"),
                                      "version_to": p.get("version_max") or p.get("version")})
        ver = f"  [{ver}]" if ver else ""
        pack = "  🧩" if p.get("modpack") else ""
        return f"  ⭐ {p.get('name') or 'Mafin Server'}  ({p['address']})  {state}{ver}{pack}"

    def _config_server_index(self, row):
        """Номер строки списка -> номер в config['servers'] (None для закреплённого сервера)."""
        offset = 1 if getattr(self, "_pinned_server", None) else 0
        idx = row - offset
        return idx if idx >= 0 else None

    PINNED_REFRESH_MS = 5000
    PINNED_FAILS_BEFORE_HIDE = 3

    def refresh_pinned_server(self):
        if getattr(self, "_pinned_busy", False):
            return
        self._pinned_busy = True

        def work():
            info, ok = None, False
            try:
                status, data = self.api.mc_status()
                if status == 200 and data.get("success") and data.get("address"):
                    info, ok = data, True
            except Exception:
                pass
            try:
                self.root.after(0, lambda: self._set_pinned_server(info, ok))
            except Exception:
                self._pinned_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _set_pinned_server(self, info, ok=True):
        self._pinned_busy = False
        if ok:
            self._pinned_fails = 0
        else:
            self._pinned_fails = getattr(self, "_pinned_fails", 0) + 1
            if self._pinned_fails < self.PINNED_FAILS_BEFORE_HIDE and self._pinned_server:
                return
            info = None
        if info == self._pinned_server:
            return
        self._pinned_server = info
        self.refresh_servers_ui()

    def _schedule_pinned_refresh(self):
        def tick():
            try:
                if self.root.winfo_exists():
                    self.refresh_pinned_server()
                    self.root.after(self.PINNED_REFRESH_MS, tick)
            except Exception:
                pass
        self.root.after(self.PINNED_REFRESH_MS, tick)

    def play_selected_server(self):
        sel = self.server_listbox.curselection()
        if not sel:
            if self.server_listbox.size():
                sel = (0,)
            else:
                messagebox.showinfo("Серверы", "Выбери сервер в списке или добавь новый.")
                return
        idx = self._config_server_index(sel[0])
        if idx is None:
            p = self._pinned_server
            if not p.get("ready"):
                messagebox.showinfo("Серверы", "Сервер выключен или ещё запускается. Попробуй позже.")
                return
            if not self.api.is_logged_in():
                messagebox.showwarning("Серверы", "Войди в аккаунт лаунчера на вкладке «Аккаунт»: "
                                                  "без этого сервер тебя не пустит.")
                return
            server = {"name": p.get("name") or "Mafin Server", "address": p["address"],
                      "version_from": p.get("version_min") or p.get("version") or "",
                      "version_to": p.get("version_max") or p.get("version") or ""}
        else:
            server = self.config["servers"][idx]
        if self.installing or (self.process is not None and self.process.poll() is None):
            messagebox.showwarning("Серверы", "Дождись окончания установки или закрой запущенную игру.")
            return
        if idx is None and self._pinned_server.get("modpack") \
                and self._server_pack_gate(self._pinned_server["modpack"], lambda: self._play_server_go(server)):
            return
        self._play_server_go(server)

    def _play_server_go(self, server):
        if self.installing or (self.process is not None and self.process.poll() is None):
            return
        lo, hi = server_version_range(server)
        current = mc_game_version(self.version_var.get())
        version, keep = pick_server_launch_version(lo, hi, current)
        text = f"Запустить Minecraft {version} и подключиться к «{server['name']}» ({server['address']})?".replace("Minecraft  ", "Minecraft ")
        if lo and not keep:
            text += f"\n\nВыбранная сейчас версия ({current or 'не выбрана'}) серверу не подходит, поэтому будет использована {version}."
        if not messagebox.askyesno("Играть на сервере", text):
            return
        if lo and not keep:
            self.version_var.set(version)
            if self.modloader_var.get() != "vanilla":
                self.modloader_var.set("vanilla")
                self._on_loader_change()
        self.config["last_server"] = server["address"]
        self.save_config()
        self._pending_join_address = server["address"]
        self.install_and_launch()

    def refresh_servers_ui(self):
        if not hasattr(self, "server_listbox"):
            return
        previous = getattr(self, "_server_row_addrs", [])
        selected = self.server_listbox.curselection()
        keep_addr = previous[selected[0]] if selected and selected[0] < len(previous) else None
        self.server_listbox.delete(0, tk.END)
        addrs = []
        if getattr(self, "_pinned_server", None):
            self.server_listbox.insert(tk.END, self._pinned_label())
            addrs.append(self._pinned_server["address"])
        for s in self.config.get("servers", []):
            ver = format_server_versions(s)
            self.server_listbox.insert(tk.END, f"  🌐 {s['name']}  ({s['address']})" + (f"  [{ver}]" if ver else ""))
            addrs.append(s["address"])
        self._server_row_addrs = addrs
        wanted = keep_addr or self.config.get("last_server")
        if wanted in addrs:
            self.server_listbox.selection_set(addrs.index(wanted))

    def add_server(self):
        ServerEditDialog(self, None, on_save=self._save_new_server)

    def _save_new_server(self, data):
        self.config.setdefault("servers", []).append(data)
        self.save_config()
        self.refresh_servers_ui()
        self._bump_achievement("servers_added_total", 1)

    def edit_server(self):
        sel = self.server_listbox.curselection()
        if not sel:
            messagebox.showinfo("Серверы", "Выбери сервер, который хочешь изменить.")
            return
        idx = self._config_server_index(sel[0])
        if idx is None:
            messagebox.showinfo("Серверы", "Закреплённый сервер настраивается администратором в панели сервера.")
            return

        def save(data):
            self.config["servers"][idx] = data
            self.save_config()
            self.refresh_servers_ui()
        ServerEditDialog(self, dict(self.config["servers"][idx]), on_save=save)

    def remove_server(self):
        sel = self.server_listbox.curselection()
        if not sel:
            return
        idx = self._config_server_index(sel[0])
        if idx is None:
            messagebox.showinfo("Серверы", "Этот сервер закреплён, его нельзя удалить.")
            return
        del self.config["servers"][idx]
        self.save_config()
        self.refresh_servers_ui()

    def ping_server(self):
        sel = self.server_listbox.curselection()
        if not sel:
            return
        idx = self._config_server_index(sel[0])
        if idx is None:
            p = self._pinned_server
            server = {"name": p.get("name") or "Mafin Server", "address": p["address"]}
        else:
            server = self.config["servers"][idx]
        address = server["address"]
        host, _, port_str = address.partition(":")
        port = int(port_str) if port_str.isdigit() else 25565

        def check():
            info = ping_minecraft_server(host, port)
            if info:
                msg = (f"{server['name']}: ✅ отвечает ({info['ms']} мс)\n"
                       f"Версия сервера: {info['version'] or 'неизвестна'}\n"
                       f"Игроки: {info['online']}/{info['max']}")
            else:
                start = time.time()
                try:
                    with socket.create_connection((host, port), timeout=3):
                        msg = f"{server['name']}: ✅ порт открыт ({int((time.time() - start) * 1000)} мс), но сервер не отдал статус"
                except Exception as e:
                    msg = f"{server['name']}: ❌ недоступен ({e})"
            self.root.after(0, lambda: messagebox.showinfo("Пинг", msg))
            self._bump_achievement("server_pings", 1)

        threading.Thread(target=check, daemon=True).start()

    def build_p2p_tab(self, frame):
        ttk.Label(frame, text="🔗 Peer-to-Peer мультиплеер (без VPN)",
                  font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        ttk.Label(
            frame,
            text="Один игрок создаёт комнату, другие подключаются по адресу.\n"
                 "UPnP автоматически пробрасывает порты на роутере.",
            foreground="#7a8599"
        ).pack(anchor="w", padx=10, pady=(0, 10))

        host_frame = ttk.LabelFrame(frame, text="🎮 Создать комнату (хост)")
        host_frame.pack(fill="x", padx=10, pady=5)
        row1 = ttk.Frame(host_frame)
        row1.pack(fill="x", padx=10, pady=5)
        ttk.Label(row1, text="Порт MC:").pack(side="left")
        self._p2p_mc_port_var = tk.StringVar(value="25565")
        ttk.Entry(row1, textvariable=self._p2p_mc_port_var, width=8).pack(side="left", padx=5)
        ttk.Label(row1, text="Внешний порт:").pack(side="left", padx=(15, 0))
        self._p2p_ext_port_var = tk.StringVar(value=str(self.config.get("p2p_last_port", 25565)))
        ttk.Entry(row1, textvariable=self._p2p_ext_port_var, width=8).pack(side="left", padx=5)
        self.p2p_create_btn = ttk.Button(
            row1, text="🚀 Создать", style="Accent.TButton", command=self.p2p_create_room
        )
        self.p2p_create_btn.pack(side="left", padx=10)

        addr_row = ttk.Frame(host_frame)
        addr_row.pack(fill="x", padx=10, pady=5)
        self.p2p_address_var = tk.StringVar(value="Комната не создана")
        self.p2p_address_label = ttk.Label(addr_row, textvariable=self.p2p_address_var,
                                            foreground=self._accent, font=("Segoe UI", 10, "bold"))
        self.p2p_address_label.pack(side="left")
        self.p2p_copy_host_btn = ttk.Button(addr_row, text="📋 Копировать",
                                             command=self.p2p_copy_host_address, state="disabled")
        self.p2p_copy_host_btn.pack(side="left", padx=10)

        join_frame = ttk.LabelFrame(frame, text="🔌 Подключиться")
        join_frame.pack(fill="x", padx=10, pady=5)
        row2 = ttk.Frame(join_frame)
        row2.pack(fill="x", padx=10, pady=5)
        ttk.Label(row2, text="Адрес (ip:порт):").pack(side="left")
        self._p2p_join_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self._p2p_join_var, width=25).pack(side="left", padx=5)
        self.p2p_join_btn = ttk.Button(row2, text="🔗 Подключиться", command=self.p2p_join_room)
        self.p2p_join_btn.pack(side="left", padx=5)

        join_status = ttk.Frame(join_frame)
        join_status.pack(fill="x", padx=10, pady=5)
        self.p2p_local_var = tk.StringVar(value="Не подключено")
        ttk.Label(join_status, textvariable=self.p2p_local_var).pack(side="left")
        self.p2p_copy_join_btn = ttk.Button(join_status, text="📋 Копировать",
                                             command=self.p2p_copy_join_address, state="disabled")
        self.p2p_copy_join_btn.pack(side="left", padx=10)

        self.p2p_stop_btn = ttk.Button(frame, text="⏹ Остановить P2P", command=self.p2p_stop)
        self.p2p_stop_btn.pack(padx=10, pady=5, anchor="w")

        ttk.Label(frame, text="Лог:").pack(anchor="w", padx=10, pady=(10, 0))
        self.p2p_log_text = scrolledtext.ScrolledText(
            frame, height=7, bg="#0d1117", fg="#58a6ff",
            font=("Consolas", 9), state="disabled"
        )
        self.p2p_log_text.pack(fill="both", expand=True, padx=10, pady=5)

    def p2p_create_room(self):
        try:
            mc_port = int(self._p2p_mc_port_var.get().strip())
            ext_port = int(self._p2p_ext_port_var.get().strip())
        except ValueError:
            messagebox.showerror("Ошибка", "Введите корректные номера портов")
            return
        self.p2p_create_btn.config(state="disabled")
        self.p2p_address_var.set("Создание...")
        self._p2p_host_address = None

        def do():
            result = self.p2p.create_room(mc_port=mc_port, external_port=ext_port)
            if result:
                self._p2p_host_address = result["address"]
                if result["upnp_ok"]:
                    text = f"✅ {result['address']}"
                else:
                    text = f"⚠ Только LAN: {result['address']}"
                self.root.after(0, lambda: self.p2p_address_var.set(text))
                self.root.after(0, lambda: self.p2p_copy_host_btn.config(state="normal"))
                self.config["p2p_last_port"] = ext_port
                self.save_config()
                self._bump_achievement("p2p_host_count", 1)
            else:
                self.root.after(0, lambda: self.p2p_address_var.set("❌ Ошибка"))
            self.root.after(0, lambda: self.p2p_create_btn.config(state="normal"))

        threading.Thread(target=do, daemon=True).start()

    def p2p_copy_host_address(self):
        addr = getattr(self, "_p2p_host_address", None)
        if addr:
            self.root.clipboard_clear()
            self.root.clipboard_append(addr)
            self.status_var.set("📋 Адрес скопирован!")

    def p2p_copy_join_address(self):
        addr = getattr(self, "_p2p_join_local_address", None)
        if addr:
            self.root.clipboard_clear()
            self.root.clipboard_append(addr)
            self.status_var.set("📋 Адрес скопирован!")

    def p2p_join_room(self):
        addr = self._p2p_join_var.get().strip()
        if not addr:
            messagebox.showerror("Ошибка", "Введите адрес комнаты")
            return
        self.p2p_join_btn.config(state="disabled")
        self.p2p_local_var.set("Подключение...")
        self._p2p_join_local_address = None

        def do():
            local = self.p2p.join_room(addr)
            if local:
                self._p2p_join_local_address = local
                self.root.after(0, lambda: self.p2p_local_var.set(f"🎮 MC → {local}"))
                self.root.after(0, lambda: self.p2p_copy_join_btn.config(state="normal"))
                self._bump_achievement("p2p_join_count", 1)
            else:
                self.root.after(0, lambda: self.p2p_local_var.set("❌ Ошибка"))
            self.root.after(0, lambda: self.p2p_join_btn.config(state="normal"))

        threading.Thread(target=do, daemon=True).start()

    def p2p_stop(self):
        self.p2p.stop()
        self.p2p_address_var.set("Комната не создана")
        self.p2p_local_var.set("Не подключено")
        self.p2p_copy_host_btn.config(state="disabled")
        self.p2p_copy_join_btn.config(state="disabled")

    def build_screenshots_tab(self, frame):
        ttk.Label(frame, text="🖼️ Скриншоты из папки screenshots:",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 5))
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.screenshot_listbox = tk.Listbox(list_frame, height=14, bg="#242830", fg="white",
                                              selectbackground="#43b581", font=("Consolas", 10))
        self.screenshot_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.screenshot_listbox.yview)
        scroll.pack(side="left", fill="y")
        self.screenshot_listbox.config(yscrollcommand=scroll.set)
        self.screenshot_listbox.bind("<Double-Button-1>", lambda e: self.open_selected_screenshot())

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="🔄 Обновить", command=self.refresh_screenshots).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🖼️ Открыть", command=self.open_selected_screenshot).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="📂 Папка", command=self._open_screenshots_folder).pack(side="left", padx=5)
        self.refresh_screenshots()

    def _open_screenshots_folder(self):
        self._open_folder(os.path.join(self.core.game_path(), "screenshots"))

    def refresh_screenshots(self):
        if not hasattr(self, "screenshot_listbox"):
            return
        self.screenshot_listbox.delete(0, tk.END)
        folder = os.path.join(self.core.game_path(), "screenshots")
        if os.path.isdir(folder):
            for f in sorted(os.listdir(folder), reverse=True):
                if f.lower().endswith((".png", ".jpg", ".jpeg")):
                    self.screenshot_listbox.insert(tk.END, f"  📷 {f}")

    def open_selected_screenshot(self):
        sel = self.screenshot_listbox.curselection()
        if not sel:
            return
        text = self.screenshot_listbox.get(sel[0])
        name = text.replace("  📷 ", "").strip()
        path = os.path.join(self.core.game_path(), "screenshots", name)
        self._open_file(path)

    def build_logs_tab(self, frame):
        ttk.Label(frame, text="📋 Логи лаунчера и игры",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=10, pady=(10, 5))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", padx=10, pady=5)
        ttk.Button(btn_frame, text="🔄 Обновить", command=self.refresh_logs).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🗑️ Очистить все", command=self.clear_logs).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="📂 Открыть папку", command=self._open_logs_folder).pack(side="left", padx=5)

        self.log_viewer = scrolledtext.ScrolledText(
            frame, bg="#0d1117", fg="#c9d1d9", font=("Consolas", 9),
            insertbackground="white", state="disabled"
        )
        self.log_viewer.pack(fill="both", expand=True, padx=10, pady=5)
        self.refresh_logs()

    def _open_logs_folder(self):
        log_dir = os.path.join(self.core.game_path(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        self._open_folder(log_dir)

    def refresh_logs(self):
        if not hasattr(self, "log_viewer"):
            return
        self.log_viewer.config(state="normal")
        self.log_viewer.delete("1.0", tk.END)

        log_dir = os.path.join(self.core.game_path(), "logs")
        if not os.path.isdir(log_dir):
            self.log_viewer.insert(tk.END, "Папка logs не найдена.\n")
            self.log_viewer.config(state="disabled")
            return

        latest = os.path.join(log_dir, "latest.log")
        if os.path.isfile(latest):
            self.log_viewer.insert(tk.END, "=== latest.log ===\n\n")
            try:
                with open(latest, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                    for line in lines[-500:]:
                        self.log_viewer.insert(tk.END, line)
            except Exception as e:
                self.log_viewer.insert(tk.END, f"Ошибка чтения: {e}\n")
        else:
            self.log_viewer.insert(tk.END, "latest.log не найден.\n")

        self.log_viewer.see(tk.END)
        self.log_viewer.config(state="disabled")

    def clear_logs(self):
        if not messagebox.askyesno("Очистка", "Удалить все файлы логов?"):
            return
        log_dir = os.path.join(self.core.game_path(), "logs")
        if os.path.isdir(log_dir):
            count = 0
            for f in os.listdir(log_dir):
                fpath = os.path.join(log_dir, f)
                if os.path.isfile(fpath):
                    try:
                        os.remove(fpath)
                        count += 1
                    except Exception:
                        pass
            messagebox.showinfo("Очистка", f"Удалено {count} файлов логов.")
            self.refresh_logs()
            if count:
                self._bump_achievement("logs_cleaned", 1)

    def _async(self, work_fn, done_fn):
        def runner():
            try:
                result = work_fn()
                error = None
            except Exception as e:
                result = None
                error = e
            try:
                self.root.after(0, lambda: done_fn(result, error))
            except RuntimeError:
                pass
        threading.Thread(target=runner, daemon=True).start()

    def _try_restore_server_session(self):
        token = self.config.get("server_token", "")
        if not token:
            return
        self.api.token = token
        self._achievements_restore_pending = True

        def work():
            return self.api.login_with_token(token)

        def done(result, error):
            transient = bool(error) or not result or (
                not result[0] and isinstance(result[1], dict) and result[1].get("_transient"))
            if transient:
                tries = getattr(self, "_restore_tries", 0) + 1
                self._restore_tries = tries
                self.root.after(min(5000 * tries, 30000), self._try_restore_server_session)
                return
            if not result[0]:
                self.api.drop_local()
                self.config["server_token"] = ""
                save_json_file(CONFIG_FILE, self.config)
                return
            self._restore_tries = 0
            self._on_server_login_success(persist=False)

        self._async(work, done)

    def _on_server_login_success(self, persist=True):
        self.achievements.ensure_owner(self.api.nickname)
        if hasattr(self, "_ach_list_frame"):
            self.refresh_achievements()
        self.server_status_var.set(
            f"✅ Вы вошли как {self.api.nickname}" + (" (админ)" if self.api.is_admin else "")
        )
        self.server_nick_var.set(self.api.nickname or "")
        if persist:
            self.config["server_token"] = self.api.token or ""
            self.config["server_nickname"] = self.api.nickname or ""
            save_json_file(CONFIG_FILE, self.config)
        if self.admin_tab_id is not None:
            self.notebook.tab(self.admin_tab_id, state="normal" if self.api.is_admin else "hidden")
            if self.api.is_admin:
                self.refresh_admin_panel()
        self._refresh_account_info_label()
        self._sync_profile_with_server(self.api.nickname)
        if hasattr(self, "_tg_rows"):
            self._tg_refresh()
        self._rebuild_friends_tab()
        self._rebuild_quests_tab()
        self._rebuild_gifts_tab()
        self._rebuild_privileges_tab()
        self._achievements_restore_pending = True
        threading.Thread(target=self._sync_achievements_from_server, daemon=True).start()
        self.refresh_news_admin_visibility()
        if hasattr(self, "news_text"):
            self.refresh_news(show_errors=False)
        self._start_heartbeat_loop()
        threading.Thread(target=self._flush_pending_sync, daemon=True).start()

    def _rebuild_friends_tab(self):
        frame = getattr(self, "friends_tab_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        self.build_friends_tab(frame)
        if self.api.is_logged_in():
            threading.Thread(target=self.refresh_friends, daemon=True).start()

    def _rebuild_quests_tab(self):
        frame = getattr(self, "quests_tab_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        self.build_quests_tab(frame)

    def _recommend_memory_now(self):
        total, free = get_system_memory_mb()
        mods = count_enabled_mods(self.mod_manager.mods_dir) if getattr(self, "mod_manager", None) else 0
        return recommend_memory(total, free, mods, self.modloader_var.get() or "vanilla",
                                mc_game_version(self.version_var.get()))

    def auto_memory_now(self):
        rec = self._recommend_memory_now()
        self.mem_var.set(str(rec["mb"]))
        preset = resolve_jvm_preset(self.config.get("jvm_preset", "default"), rec["mb"])
        extra = f"\n\nJVM-флаги: {JVM_PRESET_TITLES.get(preset, preset)}." if self.config.get("jvm_preset", "default") != "default" else ""
        if self.core.pack_memory:
            extra += f"\n\nУ активной сборки задано своё ОЗУ ({self.core.pack_memory} МБ), оно важнее."
        messagebox.showinfo("Автоподбор ОЗУ", rec["reason"] + extra)

    def _apply_auto_memory(self):
        if not self.auto_mem_var.get() or self.core.pack_memory:
            return
        try:
            self.mem_var.set(str(self._recommend_memory_now()["mb"]))
        except Exception:
            pass

    def _bisect_check_leftover(self):
        try:
            with open(bisect_state_path(self.mod_manager.mods_dir), "r", encoding="utf-8") as f:
                names = [n for n in json.load(f).get("enabled", []) if isinstance(n, str) and os.path.basename(n) == n]
        except Exception:
            return
        bisect_restore(self.mod_manager, names)
        self.refresh_mods()
        messagebox.showinfo("Поиск виновного мода", "Прошлый поиск был прерван. Все моды возвращены в прежнее состояние.")

    def _ensure_server_pack_entry(self, manifest):
        sp = self.config.setdefault("server_pack", {})
        pk = self._find_modpack(sp.get("pack_name", "")) if sp.get("pack_name") else None
        loader = manifest.get("loader") or "vanilla"
        if not pk:
            pk = {"name": self._unique_pack_name(manifest.get("name") or "Сборка сервера"), "mc_version": manifest.get("mc_version", ""),
                  "loader": loader, "loader_version": manifest.get("loader_version", ""), "memory_mb": 0}
            self.config.setdefault("modpacks", []).append(pk)
            sp["pack_name"] = pk["name"]
        else:
            pk.update({"mc_version": manifest.get("mc_version", ""), "loader": loader,
                       "loader_version": manifest.get("loader_version", "")})
        os.makedirs(os.path.join(self._modpack_dir(pk["name"]), "mods"), exist_ok=True)
        self.save_config()
        self.refresh_modpacks()
        return pk

    def _server_pack_status(self, summary):
        sp = self.config.get("server_pack") or {}
        pk = self._find_modpack(sp.get("pack_name", "")) if sp.get("pack_name") else None
        if not pk:
            return "none", None
        have = int(serverpack_load_state(self._modpack_dir(pk["name"])).get("revision") or 0)
        return ("ok" if have >= int(summary.get("revision") or 0) else "outdated"), pk

    def _server_pack_gate(self, summary, resume):
        """True, если дальше ведёт диалог сборки (или игрок отменил запуск); False — запускать сразу."""
        status, pk = self._server_pack_status(summary)
        name = summary.get("name") or "Сборка сервера"
        if status != "ok":
            verb = "Установить" if status == "none" else "Обновить"
            ans = messagebox.askyesnocancel(
                "Сборка сервера",
                f"Для этого сервера есть сборка «{name}» (ревизия {summary.get('revision')}, файлов: {summary.get('files')}).\n\n"
                f"{verb} её перед игрой?\n\nДа: {verb.lower()} и играть с ней\nНет: играть как есть\nОтмена: вернуться")
            if ans is None:
                return True
            if ans:
                ServerPackDialog(self, auto_start=True, activate=True, on_done=lambda ok: resume() if ok else None)
                return True
            return False
        if pk and self.config.get("active_modpack", "") != pk["name"]:
            if messagebox.askyesno("Сборка сервера", f"Сборка сервера «{pk['name']}» уже установлена, но активна другая. Переключиться на неё?"):
                self._activate_modpack(pk["name"])
        return False

    def open_server_pack(self):
        if not self.api.is_logged_in():
            messagebox.showwarning("Сборка сервера", "Войди в аккаунт лаунчера на вкладке «Аккаунт».")
            return
        ServerPackDialog(self)

    def _priv_mode(self, mode):
        for page in (self._priv_page, self._cos_page):
            page.pack_forget()
        if mode == "cos":
            self._cos_page.pack(fill="both", expand=True)
            self._load_cosmetics()
        else:
            self._priv_page.pack(fill="both", expand=True)
            self._load_privileges()

    def _build_cosmetics_page(self, page):
        top = ttk.Frame(page)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(top, text="🔄 Обновить", command=self._load_cosmetics).pack(side="left")
        self._cos_balance = ttk.Label(top, text="🪙 …", font=("Segoe UI Semibold", 11))
        self._cos_balance.pack(side="left", padx=12)
        ttk.Label(top, text="Шапку видят все игроки на нашем сервере", foreground="#7a8599").pack(side="left")
        self._cos_tree = ttk.Treeview(page, columns=("title", "price", "state"), show="headings", height=9, selectmode="browse")
        for col, text, width in (("title", "Шапка", 260), ("price", "Цена, 🪙", 90), ("state", "Статус", 150)):
            self._cos_tree.heading(col, text=text)
            self._cos_tree.column(col, width=width, anchor="w")
        self._cos_tree.tag_configure("worn", foreground="#43b581")
        self._cos_tree.pack(fill="x", padx=8, pady=4)
        self._cos_tree.bind("<<TreeviewSelect>>", lambda e: self._cos_show_description())
        self._cos_desc = ttk.Label(page, text="", foreground="#7a8599", wraplength=560, justify="left")
        self._cos_desc.pack(anchor="w", padx=10)
        btns = ttk.Frame(page)
        btns.pack(fill="x", padx=8, pady=6)
        ttk.Button(btns, text="🪙 Купить", style="Accent.TButton", command=self._cos_buy).pack(side="left")
        ttk.Button(btns, text="🎩 Надеть", command=self._cos_equip).pack(side="left", padx=6)
        ttk.Button(btns, text="Снять", command=lambda: self._cos_set_equipped(None)).pack(side="left")
        ttk.Button(btns, text="⚙ Настроить", command=self.open_hat_adjust).pack(side="left", padx=6)
        self._cos_status = ttk.Label(btns, text="", foreground="#7a8599")
        self._cos_status.pack(side="left", padx=10)
        ttk.Label(page, text="Шапка появляется над головой через несколько секунд после входа на сервер или смены. "
                             "Она привязана к нику, под которым ты сейчас играешь.",
                  foreground="#7a8599", wraplength=560, justify="left").pack(anchor="w", padx=10, pady=(8, 0))
        self._cos_items, self._cos_loading = {}, False

    def _load_cosmetics(self):
        if getattr(self, "_cos_loading", False):
            return
        self._cos_loading = True
        self._async(lambda: self.api.cosmetics_list(), self._apply_cosmetics)

    def _apply_cosmetics(self, result, error):
        self._cos_loading = False
        if not self._cos_tree.winfo_exists():
            return
        status, data = result if result else (0, {"error": str(error)})
        if status != 200 or not data.get("success"):
            self._cos_status.config(text=f"Не удалось загрузить: {data.get('error', 'нет связи с сервером')}")
            return
        self.api.coins = data.get("coins", self.api.coins)
        self._cos_balance.config(text=f"🪙 {data.get('coins', 0):g}")
        keep = self._cos_tree.selection()
        self._cos_tree.delete(*self._cos_tree.get_children())
        self._cos_items = {}
        for it in data.get("items", []):
            iid = str(it["id"])
            self._cos_items[iid] = it
            state = "✅ надета" if it["equipped"] else ("есть" if it["owned"] else "")
            self._cos_tree.insert("", "end", iid=iid, tags=("worn",) if it["equipped"] else (),
                                  values=(it["title"], "" if it["owned"] else f"{it['price']:g}", state))
        if keep and keep[0] in self._cos_items:
            self._cos_tree.selection_set(keep[0])
        self._cos_status.config(text="" if self._cos_items else "Шапок пока нет в продаже")
        self._cos_show_description()

    def _cos_selected(self):
        sel = self._cos_tree.selection()
        return self._cos_items.get(sel[0]) if sel else None

    def _cos_show_description(self):
        it = self._cos_selected()
        self._cos_desc.config(text=(it.get("description") or "") if it else "")

    def _cos_buy(self):
        it = self._cos_selected()
        if not it:
            messagebox.showinfo("Шапки", "Выбери шапку в списке.")
            return
        if it["owned"]:
            messagebox.showinfo("Шапки", "Эта шапка у тебя уже есть: нажми «Надеть».")
            return
        if not messagebox.askyesno("Покупка", f"Купить «{it['title']}» за {it['price']:g} монет?"):
            return
        self._cos_status.config(text="Покупаю…")

        def done(result, error):
            status, data = result if result else (0, {"error": str(error)})
            if status == 200 and data.get("success"):
                self.api.coins = data.get("total_coins", self.api.coins)
                self._cos_status.config(text=f"✅ Куплено: {it['title']}")
                self._cos_set_equipped(it["id"])
            else:
                self._cos_status.config(text="")
                messagebox.showerror("Шапки", data.get("error", "Не удалось купить"))
                self._load_cosmetics()
        self._async(lambda: self.api.cosmetics_buy(it["id"]), done)

    def _cos_equip(self):
        it = self._cos_selected()
        if not it:
            messagebox.showinfo("Шапки", "Выбери шапку в списке.")
        elif not it["owned"]:
            messagebox.showinfo("Шапки", "Сначала купи эту шапку.")
        else:
            self._cos_set_equipped(it["id"])

    def _cos_set_equipped(self, cid):
        def done(result, error):
            status, data = result if result else (0, {"error": str(error)})
            if status != 200 or not data.get("success"):
                messagebox.showerror("Шапки", data.get("error", "Не удалось сменить шапку"))
            else:
                self._cos_status.config(text="Шапка снята" if cid is None else "Шапка надета: появится в игре через несколько секунд")
            self._cos_loading = False
            self._load_cosmetics()
        self._async(lambda: self.api.cosmetics_equip(cid), done)

    def build_top_tab(self, frame):
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(top, text="🔄 Обновить", command=lambda: self._top_load(force=True)).pack(side="left")
        self._top_timer = ttk.Label(top, text="", font=("Segoe UI Semibold", 10))
        self._top_timer.pack(side="left", padx=12)
        self._top_rewards = ttk.Label(top, text="", foreground="#7a8599")
        self._top_rewards.pack(side="left")
        self._top_tree = ttk.Treeview(frame, columns=("rank", "nick", "time", "coins", "online"), show="headings",
                                      height=14, selectmode="browse")
        for col, text, width, anchor in (("rank", "#", 50, "center"), ("nick", "Игрок", 200, "w"),
                                         ("time", "Время за неделю", 140, "w"), ("coins", "Заработано, 🪙", 120, "w"),
                                         ("online", "Статус", 90, "w")):
            self._top_tree.heading(col, text=text)
            self._top_tree.column(col, width=width, anchor=anchor)
        self._top_tree.tag_configure("me", foreground="#43b581")
        self._top_tree.pack(fill="both", expand=True, padx=8, pady=4)
        self._top_tree.bind("<Double-1>", lambda e: self._top_open_profile())
        self._top_last = ttk.Label(frame, text="", foreground="#7a8599", wraplength=620, justify="left")
        self._top_last.pack(anchor="w", padx=10, pady=(2, 0))
        self._top_status = ttk.Label(frame, text="Двойной клик по игроку открывает его профиль.", foreground="#7a8599")
        self._top_status.pack(anchor="w", padx=10, pady=(0, 8))
        self._top_end_ts, self._top_loading, self._top_loaded_at = None, False, 0.0
        frame.bind("<Map>", lambda e: self._top_load())
        self._top_tick()
        self._top_load(force=True)

    @staticmethod
    def _fmt_minutes(minutes):
        m = int(round(minutes))
        return f"{m // 60} ч {m % 60:02d} мин" if m >= 60 else f"{m} мин"

    def _top_tick(self):
        try:
            if not self._top_tree.winfo_exists():
                return
            if self._top_end_ts:
                left = max(0, int(self._top_end_ts - time.time()))
                d, rem = divmod(left, 86400)
                h, rem = divmod(rem, 3600)
                self._top_timer.config(text=f"⏳ До конца недели: {d} д {h} ч {rem // 60} мин")
            self.root.after(1000, self._top_tick)
        except Exception:
            pass

    def _top_load(self, force=False):
        if self._top_loading or (not force and time.time() - self._top_loaded_at < 20):
            return
        self._top_loading = True
        self._async(lambda: self.api.top_weekly(20), self._top_apply)

    def _top_apply(self, result, error):
        self._top_loading = False
        if not self._top_tree.winfo_exists():
            return
        status, data = result if result else (0, {"error": str(error)})
        if status != 200 or not data.get("success"):
            self._top_status.config(text=f"Не удалось загрузить: {data.get('error', 'нет связи с сервером')}")
            return
        self._top_loaded_at = time.time()
        self._top_end_ts = time.time() + int(data.get("seconds_left", 0))
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        rewards = data.get("rewards") or []
        if rewards:
            self._top_rewards.config(text="Награда: " + " · ".join(f"{medals.get(i + 1, str(i + 1) + '.')} {r:g}" for i, r in enumerate(rewards))
                                     + f" 🪙 (от {self._fmt_minutes(data.get('min_minutes', 0))})")
        self._top_tree.delete(*self._top_tree.get_children())
        me = (self.api.nickname or "").lower()
        for r in data.get("top", []):
            self._top_tree.insert("", "end", iid=f"p{r['rank']}", tags=("me",) if r["nickname"].lower() == me else (),
                                  values=(medals.get(r["rank"], r["rank"]), r["nickname"], self._fmt_minutes(r["minutes"]),
                                          f"{r['coins']:g}", "🟢 онлайн" if r.get("is_online") else ""))
        last = data.get("last_week")
        if last and last.get("winners"):
            self._top_last.config(text="Прошлая неделя: " + " · ".join(
                f"{medals.get(w['rank'], w['rank'])} {w['nickname']} (+{w['coins']:g} 🪙)" for w in last["winners"]))
        else:
            self._top_last.config(text="Награды за прошлую неделю ещё не выдавались.")
        self._top_status.config(text="" if data.get("top") else "На этой неделе ещё никто не играл.")

    def _top_open_profile(self):
        sel = self._top_tree.selection()
        if sel:
            nick = self._top_tree.item(sel[0], "values")[1]
            ProfileWindow(self.root, self.api, str(nick))

    def build_privileges_tab(self, frame):
        if not self.api.is_logged_in():
            ttk.Label(
                frame, text="Войдите в аккаунт на вкладке «🖥️ Аккаунт», чтобы покупать привилегии за монеты.",
                foreground="#7a8599", wraplength=500, justify="left"
            ).pack(padx=20, pady=30)
            return
        self._build_privileges_tab(frame)

    def _rebuild_privileges_tab(self):
        frame = getattr(self, "privileges_tab_frame", None)
        if frame is None:
            return
        self._priv_gen = getattr(self, "_priv_gen", 0) + 1
        for child in frame.winfo_children():
            child.destroy()
        self.build_privileges_tab(frame)

    def _build_privileges_tab(self, frame):
        root_frame = frame
        mode_bar = ttk.Frame(root_frame)
        mode_bar.pack(fill="x", padx=8, pady=(6, 0))
        frame = ttk.Frame(root_frame)
        self._priv_page, self._cos_page = frame, ttk.Frame(root_frame)
        ttk.Button(mode_bar, text="👑 Привилегии", command=lambda: self._priv_mode("priv")).pack(side="left")
        ttk.Button(mode_bar, text="🎩 Шапки", command=lambda: self._priv_mode("cos")).pack(side="left", padx=6)
        frame.pack(fill="both", expand=True)
        self._build_cosmetics_page(self._cos_page)
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(top, text="🔄 Обновить", command=self._load_privileges).pack(side="left")
        self._priv_balance = ttk.Label(top, text="🪙 …", font=("Segoe UI Semibold", 11))
        self._priv_balance.pack(side="left", padx=12)
        ttk.Label(top, text="Привилегии выдаются на нашем сервере через LuckPerms",
                  foreground="#7a8599").pack(side="left")

        who = ttk.Frame(frame)
        who.pack(fill="x", padx=8, pady=4)
        ttk.Label(who, text="Ник в игре:").pack(side="left")
        self._priv_name_var = tk.StringVar(value=self.player_var.get().strip())
        ttk.Entry(who, textvariable=self._priv_name_var, width=20).pack(side="left", padx=6)
        ttk.Label(who, text="кому выдать (латиница, цифры, _)", foreground="#7a8599").pack(side="left")

        self._priv_tree = ttk.Treeview(frame, columns=("title", "duration", "price"), show="headings",
                                       height=7, selectmode="browse")
        for col, text, width in (("title", "Привилегия", 260), ("duration", "Срок", 110), ("price", "Цена, 🪙", 90)):
            self._priv_tree.heading(col, text=text)
            self._priv_tree.column(col, width=width, anchor="w")
        self._priv_tree.pack(fill="x", padx=8, pady=4)
        self._priv_tree.bind("<<TreeviewSelect>>", lambda e: self._priv_show_description())

        self._priv_desc = ttk.Label(frame, text="", foreground="#7a8599", wraplength=560, justify="left")
        self._priv_desc.pack(anchor="w", padx=10)
        btns = ttk.Frame(frame)
        btns.pack(fill="x", padx=8, pady=6)
        ttk.Button(btns, text="👑 Купить", style="Accent.TButton", command=self._buy_privilege).pack(side="left")
        self._priv_status = ttk.Label(btns, text="Загрузка...", foreground="#7a8599")
        self._priv_status.pack(side="left", padx=10)

        ttk.Label(frame, text="Мои покупки", font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=10, pady=(8, 2))
        self._priv_hist = ttk.Treeview(frame, columns=("when", "item", "player", "status"), show="headings",
                                       height=5, selectmode="none")
        for col, text, width in (("when", "Когда", 120), ("item", "Привилегия", 190),
                                 ("player", "Игрок", 120), ("status", "Статус", 130)):
            self._priv_hist.heading(col, text=text)
            self._priv_hist.column(col, width=width, anchor="w")
        self._priv_hist.pack(fill="x", padx=8, pady=(0, 8))
        self._priv_items = {}
        self._priv_loading = False
        self._priv_gen = getattr(self, "_priv_gen", 0) + 1
        self._load_privileges()
        self._schedule_privileges_refresh(self._priv_gen)

    def _schedule_privileges_refresh(self, gen):
        """Раз в 5 секунд обновляет вкладку, пока она открыта. Старые таймеры гасятся при пересборке вкладки."""
        def tick():
            if gen != getattr(self, "_priv_gen", None):
                return
            try:
                if not self._priv_tree.winfo_exists():
                    return
                if self._priv_tree.winfo_ismapped() and not self._priv_loading:
                    self._load_privileges()
                self.root.after(5000, tick)
            except Exception:
                pass
        self.root.after(5000, tick)

    def _load_privileges(self):
        if getattr(self, "_priv_loading", False):
            return
        self._priv_loading = True

        def work():
            try:
                status, data = self.api.shop_items()
            except Exception as e:
                status, data = 0, {"error": str(e)}
            self.root.after(0, lambda: self._apply_privileges(status, data))
        threading.Thread(target=work, daemon=True).start()

    def _apply_privileges(self, status, data):
        self._priv_loading = False
        if not hasattr(self, "_priv_tree") or not self._priv_tree.winfo_exists():
            return
        if status != 200 or not data.get("success"):
            self._priv_status.config(text=f"Не удалось загрузить: {data.get('error', 'нет связи с сервером')}")
            return
        self.api.coins = data.get("coins", self.api.coins)
        self._priv_balance.config(text=f"🪙 {data.get('coins', 0):g}")
        keep = self._priv_tree.selection()
        for row in self._priv_tree.get_children():
            self._priv_tree.delete(row)
        self._priv_items = {}
        for it in data.get("items", []):
            iid = str(it["id"])
            self._priv_items[iid] = it
            self._priv_tree.insert("", "end", iid=iid, values=(
                it["title"], shop_duration_text(it["duration_days"]), f"{it['price']:g}"))
        if keep and keep[0] in self._priv_items:
            self._priv_tree.selection_set(keep[0])
        self._priv_status.config(text="" if self._priv_items else "Привилегий пока нет в продаже")
        for row in self._priv_hist.get_children():
            self._priv_hist.delete(row)
        for p in data.get("purchases", []):
            self._priv_hist.insert("", "end", values=(
                str(p.get("created_at", ""))[:16], f"{p['item_title']} ({shop_duration_text(p['duration_days'])})",
                p["game_name"], SHOP_STATUS_TEXT.get(p.get("status"), p.get("status", ""))))
        self._priv_show_description()

    def _priv_show_description(self):
        sel = self._priv_tree.selection()
        it = self._priv_items.get(sel[0]) if sel else None
        self._priv_desc.config(text=(it.get("description") or "") if it else "")

    def _buy_privilege(self):
        sel = self._priv_tree.selection()
        if not sel or sel[0] not in self._priv_items:
            messagebox.showinfo("Привилегии", "Выбери привилегию в списке.")
            return
        item = self._priv_items[sel[0]]
        name = self._priv_name_var.get().strip()
        if not SHOP_NAME_RE.match(name):
            messagebox.showwarning("Привилегии", "Ник в игре: 3–16 символов, латиница, цифры и _.")
            return
        if self.api.coins < item["price"]:
            messagebox.showwarning("Привилегии", f"Не хватает монет: нужно {item['price']:g}, у тебя {self.api.coins:g}.")
            return
        if not messagebox.askyesno(
                "Покупка", f"Купить «{item['title']}» ({shop_duration_text(item['duration_days'])}) "
                           f"за {item['price']:g} монет?\n\nПривилегия будет выдана игроку {name} на сервере."):
            return
        self._priv_status.config(text="Покупаю...")

        def work():
            try:
                status, data = self.api.shop_buy(item["id"], name)
            except Exception as e:
                status, data = 0, {"error": str(e)}
            self.root.after(0, lambda: self._privilege_bought(status, data, item, name))
        threading.Thread(target=work, daemon=True).start()

    def _privilege_bought(self, status, data, item, name):
        if status != 200 or not data.get("success"):
            self._priv_status.config(text="")
            messagebox.showerror("Привилегии", data.get("error", "Не удалось купить"))
            self._load_privileges()
            return
        self.api.coins = data.get("total_coins", self.api.coins)
        if data.get("status") == "applied":
            messagebox.showinfo("Привилегии", f"Готово! «{item['title']}» выдана игроку {name}.\n"
                                              f"Зайди на сервер (если уже в игре, перезайди).")
        else:
            messagebox.showinfo("Привилегии", "Покупка оплачена. Привилегия будет выдана автоматически, "
                                              "когда сервер запущен и готов.")
        try:
            self._refresh_account_info_label()
        except Exception:
            pass
        self._load_privileges()

    def _rebuild_gifts_tab(self):
        frame = getattr(self, "gifts_tab_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        self.build_gifts_tab(frame)

    def _check_for_updates(self, silent=True):
        self._current_server_url()

        def work():
            return self.api.latest_update()

        def done(result, error):
            if error:
                if not silent:
                    messagebox.showerror(
                        "Ошибка проверки",
                        f"Не удалось подключиться к серверу для проверки обновлений:\n{error}"
                    )
                return
            status, data = result
            if status != 200 or not data.get("available"):
                if not silent:
                    messagebox.showinfo("Обновлений нет", f"У вас установлена последняя версия ({APP_VERSION}).")
                return
            remote_version = str(data.get("version", "")).strip()
            if not remote_version or _version_tuple(remote_version) <= _version_tuple(APP_VERSION):
                if not silent:
                    messagebox.showinfo("Обновлений нет", f"У вас установлена последняя версия ({APP_VERSION}).")
                return
            changelog = (data.get("changelog") or "").strip() or "Список изменений не указан."
            if messagebox.askyesno(
                "🚀 Доступно обновление",
                f"Прилетела новая версия лаунчера: {remote_version} (у вас {APP_VERSION}).\n\n"
                f"Что нового:\n{changelog}\n\nСкачать и установить сейчас?"
            ):
                self._download_and_apply_update(data)

        self._async(work, done)

    def check_for_updates_manual(self):
        self._check_for_updates(silent=False)

    def _download_and_apply_update(self, update_info):
        download_url = update_info.get("download_url")
        version = update_info.get("version")
        filename = update_info.get("filename") or f"MafinLauncher_{version}.exe"
        extra_download_url = update_info.get("extra_download_url")
        extra_filename = update_info.get("extra_filename")

        progress_win = tk.Toplevel(self.root)
        progress_win.title("Загрузка обновления...")
        progress_win.configure(bg=self._bg)
        progress_win.geometry("380x120")
        progress_win.resizable(False, False)
        progress_win.transient(self.root)

        ttk.Label(progress_win, text=f"Скачивание версии {version}...").pack(pady=(18, 8))
        bar = ttk.Progressbar(progress_win, length=320, mode="determinate")
        bar.pack(pady=4)
        pct_label = ttk.Label(progress_win, text="0%")
        pct_label.pack()

        dest_dir = os.path.join(tempfile.gettempdir(), "mafin_launcher_update")
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, filename)
        extra_dest_path = os.path.join(dest_dir, extra_filename) if extra_filename else None

        def progress_cb(done_bytes, total_bytes):
            if total_bytes:
                pct = int(done_bytes * 100 / total_bytes)
                self.root.after(0, lambda: (bar.config(value=pct), pct_label.config(text=f"{pct}%")))

        def work():
            result = self.api.download_update(download_url, dest_path, progress_cb=progress_cb)
            _verify_sha256(result, update_info.get("sha256"))
            if extra_download_url and extra_dest_path:
                try:
                    self.api.download_update(extra_download_url, extra_dest_path)
                    _verify_sha256(extra_dest_path, update_info.get("extra_sha256"))
                except Exception as e:
                    print(f"Не удалось скачать доп. файл обновления ({extra_filename}): {e}")
                    extra_result_holder["path"] = None
                else:
                    extra_result_holder["path"] = extra_dest_path
            return result

        extra_result_holder = {"path": None}

        def done(result, error):
            progress_win.destroy()
            if error:
                messagebox.showerror("Ошибка обновления", f"Не удалось скачать обновление:\n{error}")
                return
            self._install_update(result, version, extra_file_path=extra_result_holder["path"])

        self._async(work, done)

    def _install_update(self, new_file_path, version, extra_file_path=None):
        target_dir = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                      else os.path.dirname(os.path.abspath(__file__)))
        if extra_file_path and os.path.exists(extra_file_path):
            try:
                extra_dest = os.path.join(target_dir, os.path.basename(extra_file_path))
                shutil.copy2(extra_file_path, extra_dest)
            except Exception as e:
                print(f"Не удалось установить доп. файл обновления рядом с лаунчером: {e}")

        if not getattr(sys, "frozen", False):
            messagebox.showinfo(
                "Обновление скачано",
                f"Новая версия ({version}) скачана в:\n{new_file_path}\n\n"
                "Автозамена доступна только для собранного .exe. "
                "Замените текущий файл лаунчера этим вручную."
            )
            return

        current_exe = sys.executable
        try:
            if os.name == "nt":
                self._apply_update_windows(current_exe, new_file_path)
            else:
                self._apply_update_unix(current_exe, new_file_path)
        except Exception as e:
            messagebox.showerror("Ошибка обновления", f"Не удалось запустить установку обновления:\n{e}")
            return

        messagebox.showinfo("Обновление", "Лаунчер сейчас закроется и перезапустится с новой версией.")
        try:
            self.discord.clear()
            self.discord.close()
        except Exception:
            pass
        self.root.destroy()
        sys.exit(0)

    def _apply_update_windows(self, current_exe, new_file_path):
        pid = os.getpid()
        bat_path = os.path.join(tempfile.gettempdir(), "mafin_update.bat")
        bat_content = (
            "@echo off\r\n"
            ":wait\r\n"
            f"tasklist /FI \"PID eq {pid}\" 2>NUL | find /I \"{pid}\" >NUL\r\n"
            "if not errorlevel 1 (\r\n"
            "    timeout /t 1 /nobreak >NUL\r\n"
            "    goto wait\r\n"
            ")\r\n"
            "timeout /t 2 /nobreak >NUL\r\n"
            f"move /Y \"{new_file_path}\" \"{current_exe}\"\r\n"
            f"start \"\" \"{current_exe}\"\r\n"
            "timeout /t 3 /nobreak >NUL\r\n"
            "del \"%~f0\"\r\n"
        )
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write(bat_content)
        creationflags = 0x08000000
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("_PYI_") and k != "_MEIPASS2"}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen(["cmd", "/c", bat_path], creationflags=creationflags, env=env)

    def _apply_update_unix(self, current_exe, new_file_path):
        pid = os.getpid()
        sh_path = os.path.join(tempfile.gettempdir(), "mafin_update.sh")
        sh_content = (
            "#!/bin/sh\n"
            f"while kill -0 {pid} 2>/dev/null; do sleep 1; done\n"
            f"mv -f \"{new_file_path}\" \"{current_exe}\"\n"
            f"chmod +x \"{current_exe}\"\n"
            f"\"{current_exe}\" &\n"
            "rm -- \"$0\"\n"
        )
        with open(sh_path, "w", encoding="utf-8") as f:
            f.write(sh_content)
        os.chmod(sh_path, 0o755)
        subprocess.Popen(["/bin/sh", sh_path])

    def build_account_tab(self, frame):
        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True, padx=20, pady=20)

        ttk.Label(wrap, text="🖥️ Личный кабинет Mafin Launcher", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 15)
        )

        ttk.Label(wrap, text="Никнейм:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(wrap, textvariable=self.server_nick_var, width=38).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(wrap, text="Пароль:").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(wrap, textvariable=self.server_pass_var, width=38, show="*").grid(row=3, column=1, sticky="w", pady=4)

        btns = ttk.Frame(wrap)
        btns.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 15))
        ttk.Button(btns, text="Войти", command=self.server_login).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Регистрация", command=self.server_register).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Выйти", command=self.server_logout).pack(side="left")

        self.server_status_label = ttk.Label(wrap, textvariable=self.server_status_var, foreground=self._accent,
                                              font=("Segoe UI", 10, "bold"))
        self.server_status_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 10))

        info_frame = ttk.LabelFrame(wrap, text="Профиль")
        info_frame.grid(row=6, column=0, columnspan=2, sticky="we", pady=10)
        self.account_info_label = ttk.Label(info_frame, text="Вы не авторизованы на сервере.", justify="left")
        self.account_info_label.pack(anchor="w", padx=10, pady=10)

        ttk.Button(wrap, text="🔄 Обновить профиль", command=self._refresh_account_info_label).grid(
            row=7, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Button(wrap, text="🏆 Топ игроков", command=self._show_server_top).grid(
            row=7, column=1, sticky="w", pady=(4, 0)
        )
        tg_box = ttk.LabelFrame(wrap, text="✈️ Telegram-бот")
        tg_box.grid(row=8, column=0, columnspan=2, sticky="we", pady=(10, 0))
        ttk.Label(
            tg_box,
            text=("Что умеет бот:\n"
                  "• пишет, когда ваши друзья заходят в сеть;\n"
                  "• присылает новости лаунчера и сервера;\n"
                  "• /friends — показать, кто из друзей сейчас в сети;\n"
                  "• /notify on|off — включить или выключить уведомления о друзьях;\n"
                  "• /news on|off — включить или выключить новости;\n"
                  "• /unlink — отвязать аккаунт."),
            justify="left", foreground="#7a8599").pack(anchor="w", padx=10, pady=(8, 4))
        self._tg_rows = {}
        for bot, label in (("prod", "Основной бот"), ("test", "Тестовый бот (dc2)")):
            row = ttk.Frame(tg_box)
            row.pack(fill="x", padx=10, pady=3)
            ttk.Label(row, text=label, width=20).pack(side="left")
            st = ttk.Label(row, text="…", width=16)
            st.pack(side="left")
            btn = ttk.Button(row, text="✈️ Привязать", command=lambda b=bot: self._tg_toggle(b))
            btn.pack(side="left", padx=6)
            self._tg_rows[bot] = {"status": st, "btn": btn, "linked": False, "enabled": False}
        ttk.Button(tg_box, text="🎩 Купить и настроить шапку",
                   command=self.open_hat_adjust).pack(anchor="w", padx=10, pady=(6, 10))
        self._tg_polling = 0
        self.root.after(600, self._tg_refresh)

    def _tg_refresh(self):
        if not hasattr(self, "_tg_rows"):
            return
        if not self.api.is_logged_in():
            for r in self._tg_rows.values():
                r["status"].config(text="нужен вход")
                r["btn"].config(state="disabled")
            return

        def done(result, error):
            status, data = result if result else (0, {})
            if status in (401, 403):
                self._on_session_rejected()
                return
            for bot, r in self._tg_rows.items():
                info = (data or {}).get(bot) if status == 200 else None
                if not info:
                    r["status"].config(text="нет связи")
                    r["btn"].config(state="disabled")
                    continue
                r["enabled"] = bool(info.get("enabled"))
                r["linked"] = bool(info.get("linked"))
                if not r["enabled"]:
                    r["status"].config(text="не настроен")
                    r["btn"].config(state="disabled", text="✈️ Привязать")
                elif r["linked"]:
                    r["status"].config(text="✅ привязан")
                    r["btn"].config(state="normal", text="Отвязать")
                else:
                    r["status"].config(text="не привязан")
                    r["btn"].config(state="normal", text="✈️ Привязать")
        self._async(lambda: self.api.tg_status(), done)

    def _tg_toggle(self, bot):
        r = self._tg_rows.get(bot)
        if not r:
            return
        if r["linked"]:
            if not messagebox.askyesno("Telegram", "Отвязать Telegram от аккаунта?"):
                return
            self._async(lambda: self.api.tg_unlink(bot), lambda res, err: self._tg_refresh())
        else:
            self.link_telegram(bot)
            self._tg_polling = 60
            self._tg_poll_tick()

    def _tg_poll_tick(self):
        if self._tg_polling <= 0:
            return
        self._tg_polling -= 1
        self._tg_refresh()
        if any(r["enabled"] and not r["linked"] for r in self._tg_rows.values()):
            self.root.after(5000, self._tg_poll_tick)

    def open_hat_adjust(self):
        if not self.api.is_logged_in():
            messagebox.showerror("Шапка", "Сначала войдите в аккаунт.")
            return
        win = tk.Toplevel(self.root)
        win.title("Шапки: покупка и настройка")
        win.resizable(False, False)
        win.transient(self.root)
        st = {"items": {}, "sel": None, "y": 0.0, "s": 0.0, "timer": None, "skin": None, "hats": {}, "photo": None}
        preview = tk.Label(win, bg="#1a1d23", width=30, height=17)
        preview.grid(row=0, column=0, rowspan=4, padx=12, pady=12)
        right = ttk.Frame(win)
        right.grid(row=0, column=1, padx=(0, 12), pady=12, sticky="n")
        balance = ttk.Label(right, text="🪙 …", font=("Segoe UI Semibold", 11))
        balance.pack(anchor="w")
        tree = ttk.Treeview(right, columns=("title", "price", "state"), show="headings", height=6, selectmode="browse")
        for col, text, width in (("title", "Шапка", 150), ("price", "Цена", 60), ("state", "Статус", 90)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w")
        tree.pack(fill="x", pady=6)
        btns = ttk.Frame(right)
        btns.pack(fill="x")
        ctl = ttk.Frame(right)
        ctl.pack(fill="x", pady=8)
        note = ttk.Label(right, text="", foreground="#7a8599", wraplength=300, justify="left")
        note.pack(anchor="w")
        y_var, s_var = tk.StringVar(value="+0.00"), tk.StringVar(value="+0.00")

        def worn():
            return next((i for i in st["items"].values() if i.get("equipped")), None)

        def load_png(data):
            import io as _io
            return Image.open(_io.BytesIO(data)).convert("RGBA")

        def region(img, box):
            return img.crop(box)

        def compose():
            if not HAS_PIL or not win.winfo_exists():
                return
            u = 8
            canvas = Image.new("RGBA", (24 * u, 48 * u), (26, 29, 35, 255))
            skin = st["skin"]
            if skin is None:
                skin = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                d = ImageDraw.Draw(skin)
                d.rectangle([8, 8, 15, 15], fill=(197, 140, 99, 255))
                d.rectangle([20, 20, 27, 31], fill=(47, 143, 157, 255))
                d.rectangle([44, 20, 47, 31], fill=(197, 140, 99, 255))
                d.rectangle([36, 52, 39, 63], fill=(197, 140, 99, 255))
                d.rectangle([4, 20, 7, 31], fill=(59, 76, 192, 255))
                d.rectangle([20, 52, 23, 63], fill=(59, 76, 192, 255))
                d.rectangle([9, 11, 10, 12], fill=(30, 30, 30, 255))
                d.rectangle([13, 11, 14, 12], fill=(30, 30, 30, 255))
            old = skin.height < 64

            def put(box, over_box, x, y):
                base = region(skin, box)
                canvas.alpha_composite(base.resize(((box[2] - box[0]) * u, (box[3] - box[1]) * u), Image.NEAREST), (x * u, y * u))
                if over_box and not (old and over_box[1] >= 32 and over_box != (40, 8, 48, 16)):
                    ov = region(skin, over_box)
                    canvas.alpha_composite(ov.resize(((over_box[2] - over_box[0]) * u, (over_box[3] - over_box[1]) * u), Image.NEAREST), (x * u, y * u))
            left_arm = (44, 20, 48, 32) if old else (36, 52, 40, 64)
            left_leg = (4, 20, 8, 32) if old else (20, 52, 24, 64)
            put((8, 8, 16, 16), (40, 8, 48, 16), 8, 14)
            put((20, 20, 28, 32), None if old else (20, 36, 28, 48), 8, 22)
            put((44, 20, 48, 32), None if old else (44, 36, 48, 48), 4, 22)
            put(left_arm, None if old else (52, 52, 56, 64), 16, 22)
            put((4, 20, 8, 32), None if old else (4, 36, 8, 48), 8, 34)
            put(left_leg, None if old else (4, 52, 8, 64), 12, 34)
            it = worn()
            if it:
                size_units = max(3.0, min(max(it.get("scale", 0.7) + st["s"], 0.2), 2.0) * 16) / 2
                lift = (it.get("y_offset", 0) + st["y"]) * 16 / 2
                px = max(4, int(size_units * u))
                bottom = int((14 - lift) * u)
                left = int(12 * u - px / 2)
                hat_img = st["hats"].get(it["id"])
                if hat_img is not None:
                    face = region(hat_img, (8, 8, 16, 16)).resize((px, px), Image.NEAREST)
                    canvas.alpha_composite(face, (left, bottom - px))
                    if hat_img.height >= 16 and hat_img.width >= 48:
                        ov = region(hat_img, (40, 8, 48, 16)).resize((px, px), Image.NEAREST)
                        canvas.alpha_composite(ov, (left, bottom - px))
                else:
                    sq = Image.new("RGBA", (px, px), (217, 130, 43, 255))
                    ImageDraw.Draw(sq).rectangle([0, 0, px - 1, px - 1], outline=(122, 74, 16, 255), width=2)
                    canvas.alpha_composite(sq, (left, bottom - px))
            photo = ImageTk.PhotoImage(canvas.resize((24 * u * 3 // 2, 48 * u * 3 // 2), Image.NEAREST))
            st["photo"] = photo
            preview.config(image=photo, width=photo.width(), height=photo.height())

        def redraw():
            y_var.set(f"{st['y']:+.2f}")
            s_var.set(f"{st['s']:+.2f}")
            compose()

        def fill_tree():
            keep = st["sel"]
            tree.delete(*tree.get_children())
            for it in st["items"].values():
                state = "✅ надета" if it["equipped"] else ("есть" if it["owned"] else "")
                tree.insert("", "end", iid=str(it["id"]),
                            values=(it["title"], "" if it["owned"] else f"{it['price']:g}", state))
            if keep and str(keep) in tree.get_children():
                tree.selection_set(str(keep))

        def sel_item():
            sel = tree.selection()
            return st["items"].get(int(sel[0])) if sel else None

        def load_hat_images():
            for it in list(st["items"].values()):
                if it.get("has_texture") and it["id"] not in st["hats"]:
                    def work(i=it):
                        code, data = self.api.fetch_bytes(f"/api/cosmetics/texture/{i['id']}")
                        return i["id"], data if code == 200 else b""

                    def done(res, err):
                        if res and res[1] and win.winfo_exists():
                            try:
                                st["hats"][res[0]] = load_png(res[1])
                                compose()
                            except Exception:
                                pass
                    self._async(work, done)

        def reload():
            def done(result, error):
                if not win.winfo_exists():
                    return
                status, data = result if result else (0, {})
                if status != 200 or not data.get("success"):
                    note.config(text="Не удалось загрузить данные с сервера.")
                    return
                st["items"] = {i["id"]: i for i in data.get("items", [])}
                st["y"] = float(data.get("y_adj") or 0)
                st["s"] = float(data.get("scale_adj") or 0)
                self.api.coins = data.get("coins", self.api.coins)
                balance.config(text=f"🪙 {data.get('coins', 0):g}")
                fill_tree()
                load_hat_images()
                redraw()
                if not st["items"]:
                    note.config(text="Шапок пока нет в продаже.")
                elif not worn():
                    note.config(text="Выберите шапку, купите её и наденьте — затем настройте стрелками.")
            self._async(lambda: self.api.cosmetics_list(), done)

        def load_skin():
            name = (self.player_var.get() or "").strip()

            def work():
                code, data = self.api.skin_mine(name)
                skin = (data or {}).get("skin") if code == 200 else None
                if not skin:
                    return None
                code2, raw = self.api.fetch_bytes(f"/yggdrasil/textures/{skin['hash']}")
                return raw if code2 == 200 else None

            def done(raw, err):
                if raw and HAS_PIL and win.winfo_exists():
                    try:
                        st["skin"] = load_png(raw)
                    except Exception:
                        st["skin"] = None
                    compose()
            self._async(work, done)

        def do_buy():
            it = sel_item()
            if not it:
                note.config(text="Выберите шапку в списке.")
                return
            if it["owned"]:
                note.config(text="Эта шапка уже куплена — нажмите «Надеть».")
                return
            if not messagebox.askyesno("Покупка", f"Купить «{it['title']}» за {it['price']:g} монет?", parent=win):
                return

            def done(result, error):
                status, data = result if result else (0, {"error": str(error)})
                if status == 200 and data.get("success"):
                    note.config(text=f"✅ Куплено: {it['title']}")
                else:
                    note.config(text=(data or {}).get("error", "Не удалось купить"))
                reload()
            self._async(lambda: self.api.cosmetics_buy(it["id"]), done)

        def do_equip(cid):
            def done(result, error):
                status, data = result if result else (0, {"error": str(error)})
                if status == 200 and data.get("success"):
                    note.config(text="Шапка снята" if cid is None else "Шапка надета. В игре появится через несколько секунд.")
                else:
                    note.config(text=(data or {}).get("error", "Не удалось сменить шапку"))
                reload()
            self._async(lambda: self.api.cosmetics_equip(cid), done)

        def equip_selected():
            it = sel_item()
            if not it:
                note.config(text="Выберите шапку в списке.")
            elif not it["owned"]:
                note.config(text="Сначала купите эту шапку.")
            else:
                do_equip(it["id"])

        def save_later():
            redraw()
            if st["timer"]:
                win.after_cancel(st["timer"])

            def push():
                y, sc = st["y"], st["s"]

                def done(result, error):
                    status, data = result if result else (0, {})
                    note.config(text="Сохранено: в игре обновится через несколько секунд." if status == 200
                                else ((data or {}).get("error") or "Не удалось сохранить настройку."))
                self._async(lambda: self.api.cosmetics_adjust(y, sc), done)
            st["timer"] = win.after(450, push)

        def change(key, delta, lo, hi):
            if not worn():
                note.config(text="Сначала наденьте шапку.")
                return
            st[key] = round(min(max(st[key] + delta, lo), hi), 2)
            save_later()

        def reset():
            if worn():
                st["y"] = st["s"] = 0.0
                save_later()

        ttk.Button(btns, text="🪙 Купить", style="Accent.TButton", command=do_buy).pack(side="left")
        ttk.Button(btns, text="🎩 Надеть", command=equip_selected).pack(side="left", padx=6)
        ttk.Button(btns, text="Снять", command=lambda: do_equip(None)).pack(side="left")
        for r, (label, key, lo, hi, up_t, down_t, var) in enumerate((
                ("Высота", "y", -0.6, 0.6, "▲", "▼", y_var),
                ("Размер", "s", -0.5, 0.8, "▶", "◀", s_var))):
            ttk.Label(ctl, text=label, width=8).grid(row=r, column=0, pady=4)
            ttk.Button(ctl, text=down_t, width=3,
                       command=lambda k=key, a=lo, b=hi: change(k, -0.05, a, b)).grid(row=r, column=1)
            ttk.Label(ctl, textvariable=var, width=7, anchor="center").grid(row=r, column=2)
            ttk.Button(ctl, text=up_t, width=3,
                       command=lambda k=key, a=lo, b=hi: change(k, 0.05, a, b)).grid(row=r, column=3)
        ttk.Button(ctl, text="Сбросить", command=reset).grid(row=2, column=0, columnspan=4, pady=6)
        ttk.Button(win, text="Закрыть", command=win.destroy).grid(row=4, column=0, columnspan=2, pady=(0, 12))
        if not HAS_PIL:
            note.config(text="Для предпросмотра нужен Pillow (pip install pillow).")
        compose()
        reload()
        load_skin()

    def link_telegram(self, bot):
        """Привязка аккаунта к Telegram-боту (prod/test): сервер выдаёт одноразовую ссылку."""
        if not self.api.is_logged_in():
            messagebox.showerror("Telegram", "Сначала войдите в аккаунт.")
            return

        def work():
            return self.api._post("/api/tg/link", {"bot": bot})

        def done(result, error):
            if error or not result:
                messagebox.showerror("Telegram", "Сервер недоступен.")
                return
            status, data = result
            if status in (401, 403):
                self._on_session_rejected()
                return
            if status != 200 or not data.get("url"):
                messagebox.showerror("Telegram", (data or {}).get("error") or "Не удалось получить ссылку.")
                return
            import webbrowser
            webbrowser.open(data["url"])
            messagebox.showinfo("Telegram", "Открылся Telegram. Нажмите «Start» у бота — "
                                            "аккаунт привяжется (ссылка действует 10 минут).")

        self._async(work, done)

    def _current_server_url(self):
        self.api.set_base_url(SERVER_URL)
        return SERVER_URL

    def server_login(self):
        self._current_server_url()
        nickname = self.server_nick_var.get().strip()
        password = self.server_pass_var.get().strip()
        if not nickname or not password:
            messagebox.showerror("Ошибка", "Введите никнейм и пароль.")
            return
        self.server_status_var.set("⏳ Вход...")

        def work():
            return self.api.login(nickname, password)

        def done(result, error):
            if error:
                self.server_status_var.set("❌ Сервер недоступен")
                messagebox.showerror("Ошибка соединения", f"Не удалось подключиться к серверу:\n{error}")
                return
            status, data = result
            if status == 200 and data.get("success"):
                self._on_server_login_success(persist=True)
                save_json_file(CONFIG_FILE, self.config)
            else:
                self.server_status_var.set("Не авторизован")
                messagebox.showerror("Ошибка входа", data.get("error", "Не удалось войти"))

        self._async(work, done)

    def server_register(self):
        self._current_server_url()
        nickname = self.server_nick_var.get().strip()
        password = self.server_pass_var.get().strip()
        if not nickname or not password:
            messagebox.showerror("Ошибка", "Введите никнейм и пароль.")
            return
        self.server_status_var.set("⏳ Регистрация...")

        def work():
            return self.api.register(nickname, password)

        def done(result, error):
            if error:
                self.server_status_var.set("❌ Сервер недоступен")
                messagebox.showerror("Ошибка соединения", f"Не удалось подключиться к серверу:\n{error}")
                return
            status, data = result
            if status == 201 and data.get("success"):
                messagebox.showinfo("Готово", "Регистрация успешна! Теперь можно войти.")
                self.server_status_var.set("Регистрация успешна, войдите в аккаунт")
            else:
                self.server_status_var.set("Не авторизован")
                messagebox.showerror("Ошибка регистрации", data.get("error", "Не удалось зарегистрироваться"))

        self._async(work, done)

    def server_logout(self):
        self._stop_heartbeat_loop()
        self.achievements.clear_all()
        self.refresh_achievements()

        def work():
            self.api.logout()
            return True

        def done(result, error):
            self.server_pass_var.set("")
            self.config["server_token"] = ""
            self.config["server_nickname"] = ""
            save_json_file(CONFIG_FILE, self.config)
            self.server_status_var.set("Не авторизован")
            self.account_info_label.config(text="Вы не авторизованы на сервере.")
            if self.admin_tab_id is not None:
                self.notebook.tab(self.admin_tab_id, state="hidden")
            self._rebuild_friends_tab()
            self._rebuild_quests_tab()
            self._rebuild_gifts_tab()
            self._rebuild_privileges_tab()
            self.refresh_news_admin_visibility()
            if hasattr(self, "news_text"):
                self.refresh_news(show_errors=False)

        self._async(work, done)

    def _refresh_account_info_label(self):
        if not self.api.is_logged_in():
            self.account_info_label.config(text="Вы не авторизованы на сервере.")
            return

        def work():
            return self.api.me()

        def done(result, error):
            if error:
                return
            status, data = result
            if status != 200:
                return
            text = (
                f"Никнейм: {data.get('nickname', '-')}\n"
                f"Права: {'Администратор' if data.get('is_admin') else 'Пользователь'}\n"
                f"Коины: {data.get('coins', 0):.1f}\n"
                f"Наиграно: {data.get('total_playtime_minutes', 0):.1f} мин.\n"
                f"Запусков: {data.get('total_launches', 0)}"
            )
            self.account_info_label.config(text=text)

        self._async(work, done)

    def _show_server_top(self):
        self._current_server_url()

        def work():
            return self.api.top(10)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка соединения", f"Не удалось подключиться к серверу:\n{error}")
                return
            status, data = result
            if status != 200:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось получить топ"))
                return
            top = data.get("top", [])
            win = tk.Toplevel(self.root)
            win.title("🏆 Топ игроков")
            win.configure(bg=self._bg)
            win.geometry("420x400")
            cols = ("rank", "nickname", "coins", "playtime", "status")
            tv = ttk.Treeview(win, columns=cols, show="headings", height=15)
            for c, label in zip(cols, ["#", "Никнейм", "Коины", "Минут", "Статус"]):
                tv.heading(c, text=label)
                tv.column(c, width=80, anchor="center")
            tv.pack(fill="both", expand=True, padx=10, pady=10)
            for row in top:
                status = "🟢 онлайн" if row.get("is_online") else "🔴 оффлайн"
                tv.insert("", "end", values=(row["rank"], row["nickname"],
                                              f"{row['coins']:.1f}", f"{row['playtime']:.1f}", status))

        self._async(work, done)

    def build_quests_tab(self, frame):
        if not self.api.is_logged_in():
            ttk.Label(
                frame, text="Войдите в аккаунт на вкладке «🖥️ Аккаунт», чтобы видеть и выполнять задания.",
                foreground="#7a8599", wraplength=500, justify="left"
            ).pack(padx=20, pady=30)
            return
        panel = QuestsPanel(frame, self.api)
        panel.pack(fill="both", expand=True)
        self.quests_panel = panel

    def build_gifts_tab(self, frame):
        if not self.api.is_logged_in():
            ttk.Label(
                frame, text="Войдите в аккаунт на вкладке «🖥️ Аккаунт», чтобы дарить и получать подарки.",
                foreground="#7a8599", wraplength=500, justify="left"
            ).pack(padx=20, pady=30)
            return

        sub = ttk.Notebook(frame)
        sub.pack(fill="both", expand=True, padx=10, pady=10)

        shop_tab = ttk.Frame(sub)
        mine_tab = ttk.Frame(sub)
        sub.add(shop_tab, text="🛍️ Магазин")
        sub.add(mine_tab, text="🎁 Мои подарки")

        shop_top = ttk.Frame(shop_tab)
        shop_top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(shop_top, text="🔄 Обновить", command=lambda: self._load_gifts_shop_tab()).pack(side="left")
        ttk.Label(shop_top, text="Дарить можно любому игроку, включая себя (введите свой ник)",
                  foreground="#7a8599").pack(side="left", padx=(10, 0))

        self._gifts_shop_status = ttk.Label(shop_tab, text="Загрузка витрины...", foreground="#7a8599")
        self._gifts_shop_status.pack(anchor="w", padx=8)

        shop_canvas_wrap = ttk.Frame(shop_tab)
        shop_canvas_wrap.pack(fill="both", expand=True, padx=8, pady=4)
        shop_canvas = tk.Canvas(shop_canvas_wrap, bg=self._bg, highlightthickness=0)
        shop_scroll = ttk.Scrollbar(shop_canvas_wrap, orient="vertical", command=shop_canvas.yview)
        self._gifts_shop_holder = tk.Frame(shop_canvas, bg=self._bg)
        self._gifts_shop_holder.bind(
            "<Configure>", lambda e: shop_canvas.configure(scrollregion=shop_canvas.bbox("all")))
        shop_canvas.create_window((0, 0), window=self._gifts_shop_holder, anchor="nw")
        shop_canvas.configure(yscrollcommand=shop_scroll.set)
        shop_canvas.pack(side="left", fill="both", expand=True)
        shop_scroll.pack(side="right", fill="y")

        mine_top = ttk.Frame(mine_tab)
        mine_top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(mine_top, text="🔄 Обновить", command=lambda: self._load_gifts_mine_tab()).pack(side="left")

        self._gifts_mine_status = ttk.Label(mine_tab, text="Загрузка...", foreground="#7a8599")
        self._gifts_mine_status.pack(anchor="w", padx=8)

        self._gifts_mine_holder = ttk.Frame(mine_tab)
        self._gifts_mine_holder.pack(fill="both", expand=True, padx=8, pady=4)

        self._load_gifts_shop_tab()
        self._load_gifts_mine_tab()

    def _load_gifts_shop_tab(self):
        for w in self._gifts_shop_holder.winfo_children():
            w.destroy()

        def work():
            return self.api.list_gifts()

        def done(result, error):
            if error or not result or result[0] != 200:
                self._gifts_shop_status.config(text="Не удалось загрузить магазин подарков", foreground="#ed4245")
                return
            gifts = result[1].get("gifts", [])
            if not gifts:
                self._gifts_shop_status.config(text="Пока нет подарков в продаже", foreground="#7a8599")
                return
            self._gifts_shop_status.config(text="")
            for gift in gifts:
                self._render_shop_gift_card(gift)

        self._async(work, done)

    def _render_shop_gift_card(self, gift):
        row = tk.Frame(self._gifts_shop_holder, bg=self._bg2, bd=0)
        row.pack(fill="x", pady=3, padx=2)

        thumb = tk.Label(row, text="🎁", width=8, height=4, bg=self._bg2, fg="white")
        thumb.pack(side="left", padx=8, pady=8)
        self._load_gift_image_async(thumb, self.api.gift_image_url(gift["image_url"]), max_px=80)

        info = tk.Frame(row, bg=self._bg2)
        info.pack(side="left", fill="x", expand=True, padx=6)
        tk.Label(info, text=gift["name"], font=("Segoe UI", 11, "bold"), bg=self._bg2, fg="white").pack(anchor="w")
        qty_txt = "неограничено" if gift.get("quantity") is None else f"осталось: {gift['quantity']}"
        tk.Label(info, text=f"🪙 {gift['price']:g} монет · {qty_txt}",
                 bg=self._bg2, fg="#7a8599").pack(anchor="w")
        if gift.get("sale_until"):
            tk.Label(info, text=f"⏳ Продажа до {gift['sale_until']}",
                     bg=self._bg2, fg="#faa61a", font=("Segoe UI", 8)).pack(anchor="w")

        btns = tk.Frame(row, bg=self._bg2)
        btns.pack(side="right", padx=8)
        tk.Button(btns, text="🎁 Подарить", command=lambda g=gift: self._open_gift_send_dialog(g)).pack(pady=2)
        tk.Button(btns, text="Себе", command=lambda g=gift: self._open_gift_send_dialog(g, to=self.api.nickname)).pack(pady=2)

    def _load_gifts_mine_tab(self):
        for w in self._gifts_mine_holder.winfo_children():
            w.destroy()

        def work():
            return self.api.gifts_received(self.api.nickname)

        def done(result, error):
            if error or not result or result[0] != 200:
                self._gifts_mine_status.config(text="Не удалось загрузить подарки", foreground="#ed4245")
                return
            gifts = result[1].get("gifts", [])
            if not gifts:
                self._gifts_mine_status.config(text="Пока нет подарков", foreground="#7a8599")
                return
            self._gifts_mine_status.config(text="")
            for i, giftinfo in enumerate(gifts):
                cell = tk.Frame(self._gifts_mine_holder, bd=1, relief="solid", bg=self._bg2)
                cell.grid(row=i // 5, column=i % 5, padx=4, pady=4, sticky="n")
                label = tk.Label(cell, text="🎁", width=8, height=4, bg=self._bg2, fg="white")
                label.pack()
                self._load_gift_image_async(label, f"{self.api.base_url}{giftinfo['image_url']}", max_px=80)
                tk.Label(cell, text=giftinfo["name"], font=("Segoe UI", 8, "bold"),
                         wraplength=90, bg=self._bg2, fg="white").pack()
                tk.Label(cell, text=f"от {giftinfo['from']}", font=("Segoe UI", 7),
                         wraplength=90, bg=self._bg2, fg="#7a8599").pack()

        self._async(work, done)

    def _load_gift_image_async(self, label, full_url, max_px=80):
        def work():
            resp = requests.get(full_url, timeout=10)
            resp.raise_for_status()
            return resp.content

        def done(data, error):
            if error or not data:
                return
            try:
                if not label.winfo_exists():
                    return
            except Exception:
                return
            animate_image_on_label(label, data, max_px=max_px, circular=False)

        self._async(work, done)

    def _open_gift_send_dialog(self, gift, to=None):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Подарить: {gift['name']}")
        dialog.geometry("360x260")
        dialog.configure(bg=self._bg2)
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text=f"🎁 {gift['name']} — {gift['price']:g} монет",
                 bg=self._bg2, fg="white", font=("Segoe UI", 10, "bold")).pack(pady=(12, 8))

        tk.Label(dialog, text="Кому (ник):", bg=self._bg2, fg="#7a8599").pack(anchor="w", padx=14)
        nickname_var = tk.StringVar(value=to or "")
        tk.Entry(dialog, textvariable=nickname_var, bg=self._bg, fg="white",
                 insertbackground="white").pack(fill="x", padx=14, pady=(0, 8))

        tk.Label(dialog, text="Сообщение к подарку:", bg=self._bg2, fg="#7a8599").pack(anchor="w", padx=14)
        message_text = tk.Text(dialog, height=4, bg=self._bg, fg="white", insertbackground="white")
        message_text.pack(fill="x", padx=14, pady=(0, 10))

        status_lbl = tk.Label(dialog, text="", bg=self._bg2, fg="#ed4245")
        status_lbl.pack()

        send_btn = tk.Button(dialog, text="Подарить 🎁", bg="#43b581", fg="white")

        def do_send():
            recipient = nickname_var.get().strip()
            message = message_text.get("1.0", "end").strip()
            if not recipient:
                status_lbl.configure(text="Укажите ник получателя")
                return
            send_btn.configure(state="disabled")
            status_lbl.configure(text="Отправка...", fg="#7a8599")

            def work():
                return self.api.send_gift(gift["id"], recipient, message)

            def done(result, error):
                if error:
                    send_btn.configure(state="normal")
                    status_lbl.configure(text="Ошибка сети", fg="#ed4245")
                    return
                status, data = result
                if status == 200 and data.get("success"):
                    messagebox.showinfo("Готово", f"🎁 Подарок «{gift['name']}» отправлен игроку {recipient}!")
                    dialog.destroy()
                    self._load_gifts_shop_tab()
                    if recipient == self.api.nickname:
                        self._load_gifts_mine_tab()
                else:
                    send_btn.configure(state="normal")
                    status_lbl.configure(text=data.get("error", "Не удалось отправить подарок"), fg="#ed4245")

            self._async(work, done)

        send_btn.configure(command=do_send)
        send_btn.pack(pady=(0, 12))

    def build_friends_tab(self, frame):
        if not self.api.is_logged_in():
            ttk.Label(
                frame, text="Войдите в аккаунт на вкладке «🖥️ Аккаунт», чтобы добавлять друзей и переписываться.",
                foreground="#7a8599", wraplength=500, justify="left"
            ).pack(padx=20, pady=30)
            return

        root_pane = ttk.Frame(frame)
        root_pane.pack(fill="both", expand=True, padx=10, pady=10)

        left = ttk.Frame(root_pane, width=260)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        add_row = ttk.Frame(left)
        add_row.pack(fill="x", pady=(0, 8))
        self.friend_add_entry = ttk.Entry(add_row)
        self.friend_add_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(add_row, text="➕", width=3, command=self.send_friend_request).pack(side="left", padx=(4, 0))

        find_row = ttk.Frame(left)
        find_row.pack(fill="x", pady=(0, 8))
        ttk.Button(find_row, text="🔍 Найти людей", command=self.open_user_search).pack(side="left", fill="x", expand=True)
        ttk.Button(find_row, text="👤 Мой профиль", command=self.open_my_profile).pack(side="left", padx=(4, 0))

        ttk.Label(left, text="Входящие заявки:", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.friend_requests_frame = ttk.Frame(left)
        self.friend_requests_frame.pack(fill="x", pady=(2, 10))

        ttk.Label(left, text="Друзья:", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.friends_listbox = tk.Listbox(left, bg="#242830", fg="white",
                                           selectbackground="#43b581", font=("Segoe UI", 10))
        self.friends_listbox.pack(fill="both", expand=True, pady=(2, 5))
        self.friends_listbox.bind("<<ListboxSelect>>", lambda e: self._open_friend_chat())

        ttk.Button(left, text="🔄 Обновить", command=self.refresh_friends).pack(fill="x", pady=(0, 3))
        ttk.Button(left, text="👤 Профиль друга", command=self.open_selected_friend_profile).pack(fill="x", pady=(0, 3))
        ttk.Button(left, text="🗑️ Удалить из друзей", command=self.remove_selected_friend).pack(fill="x")

        right = ttk.Frame(root_pane)
        right.pack(side="left", fill="both", expand=True)

        chat_header = ttk.Frame(right)
        chat_header.pack(fill="x", pady=(0, 6))
        self.chat_with_label = ttk.Label(chat_header, text="Выберите друга слева, чтобы начать переписку",
                                          font=("Segoe UI", 11, "bold"))
        self.chat_with_label.pack(side="left", anchor="w")

        self.chat_text = scrolledtext.ScrolledText(
            right, bg="#1a1d23", fg="#c9d1d9", font=("Segoe UI", 10),
            insertbackground="white", state="disabled", wrap="word", height=16
        )
        self.chat_text.pack(fill="both", expand=True, pady=(0, 6))

        send_row = ttk.Frame(right)
        send_row.pack(fill="x")
        self.chat_entry = ttk.Entry(send_row)
        self.chat_entry.pack(side="left", fill="x", expand=True)
        self.chat_entry.bind("<Return>", lambda e: self.send_chat_message())
        self.chat_entry.bind("<KeyRelease>", self._on_chat_entry_keypress)
        ttk.Button(send_row, text="Отправить", command=self.send_chat_message).pack(side="left", padx=(6, 0))

        self._current_chat_friend = None
        self._last_message_id = 0
        self._last_message_friend = None
        self._friends_poll_tick = 0
        self._chat_embedded_widgets = []
        self.refresh_friends()
        self._poll_chat_loop()

    def refresh_friends(self):
        if not hasattr(self, "friends_listbox") or not self.api.is_logged_in():
            return

        def work():
            return self.api.friends_list()

        def done(result, error):
            if error:
                return
            status, data = result
            if status != 200:
                return

            for child in self.friend_requests_frame.winfo_children():
                child.destroy()
            incoming = data.get("incoming_requests", [])
            if not incoming:
                ttk.Label(self.friend_requests_frame, text="Нет заявок", foreground="#5c6270").pack(anchor="w")
            for req in incoming:
                row = ttk.Frame(self.friend_requests_frame)
                row.pack(fill="x", pady=1)
                ttk.Label(row, text=req["requester"]).pack(side="left")
                ttk.Button(row, text="✅", width=3,
                           command=lambda n=req["requester"]: self.respond_friend_request(n, True)).pack(side="right")
                ttk.Button(row, text="❌", width=3,
                           command=lambda n=req["requester"]: self.respond_friend_request(n, False)).pack(side="right")

            self.friends_listbox.delete(0, tk.END)
            friends_data = data.get("friends", [])
            self._friends_cache = [self.api.nickname] + [f["nickname"] for f in friends_data]
            self.friends_listbox.insert(tk.END, "⭐  Избранное")
            self.friends_listbox.itemconfig(0, fg="#faa61a")
            for f in friends_data:
                online = bool(f.get("is_online"))
                idx = self.friends_listbox.size()
                self.friends_listbox.insert(tk.END, f"●  {f['nickname']}")
                self.friends_listbox.itemconfig(
                    idx, fg="#43b581" if online else "#ed4245"
                )

        self._async(work, done)

    def send_friend_request(self):
        nickname = self.friend_add_entry.get().strip()
        if not nickname:
            return

        def work():
            return self.api.friend_request(nickname)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            status, data = result
            if status == 200 and data.get("success"):
                self.friend_add_entry.delete(0, tk.END)
                messagebox.showinfo("Заявка отправлена", f"Заявка в друзья отправлена игроку {nickname}.")
            else:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось отправить заявку"))

        self._async(work, done)

    def respond_friend_request(self, nickname, accept):
        def work():
            return self.api.friend_respond(nickname, accept)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            self.refresh_friends()

        self._async(work, done)

    def remove_selected_friend(self):
        sel = self.friends_listbox.curselection()
        if not sel:
            return
        nickname = self._friends_cache[sel[0]]
        if nickname == self.api.nickname:
            messagebox.showinfo("Избранное", "«Избранное» нельзя удалить - это ваш собственный чат.")
            return
        if not messagebox.askyesno("Удаление", f"Удалить {nickname} из друзей?"):
            return

        def work():
            return self.api.friend_remove(nickname)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            self._current_chat_friend = None
            self.refresh_friends()

        self._async(work, done)

    def open_user_search(self):
        UserSearchWindow(self.root, self.api, on_friend_action=self.refresh_friends)

    def open_my_profile(self):
        ProfileWindow(self.root, self.api, self.api.nickname, on_friend_action=self.refresh_friends)

    def open_selected_friend_profile(self):
        sel = self.friends_listbox.curselection()
        if not sel or not hasattr(self, "_friends_cache"):
            messagebox.showinfo("Профиль", "Сначала выберите друга в списке слева.")
            return
        nickname = self._friends_cache[sel[0]]
        ProfileWindow(self.root, self.api, nickname, on_friend_action=self.refresh_friends)

    def _open_friend_chat(self):
        sel = self.friends_listbox.curselection()
        if not sel or not hasattr(self, "_friends_cache"):
            return
        nickname = self._friends_cache[sel[0]]
        is_favorites = nickname == self.api.nickname
        self._current_chat_friend = nickname
        self._last_message_id = 0
        self._last_message_friend = None
        self._chat_fetch_in_flight = False
        self.chat_with_label.config(text="⭐ Избранное" if is_favorites else f"💬 Переписка с {nickname}")
        self.chat_text.config(state="normal")
        self.chat_text.delete("1.0", tk.END)
        self.chat_text.config(state="disabled")
        self._fetch_chat_messages()

    def send_chat_message(self):
        if not self._current_chat_friend:
            return
        text = self.chat_entry.get().strip()
        if not text:
            return
        to = self._current_chat_friend

        def work():
            return self.api.send_message(to, text)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            status, data = result
            if status == 200 and data.get("success"):
                self.chat_entry.delete(0, tk.END)
                self._fetch_chat_messages()
            else:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось отправить сообщение"))

        self._async(work, done)

    def _fetch_chat_messages(self):
        if not self._current_chat_friend:
            return
        if getattr(self, "_chat_fetch_in_flight", False):
            return
        self._chat_fetch_in_flight = True
        friend = self._current_chat_friend
        after_id = self._last_message_id if self._last_message_friend == friend else 0
        self._last_message_friend = friend

        def work():
            return self.api.get_messages_with(friend, after_id=after_id)

        def done(result, error):
            self._chat_fetch_in_flight = False
            if error or self._current_chat_friend != friend:
                return
            status, data = result
            if status != 200:
                return
            messages = data.get("messages", [])
            if not messages:
                return

            was_at_bottom = self.chat_text.yview()[1] >= 0.999

            self.chat_text.config(state="normal")
            for m in messages:
                who = "Я" if m["sender"] == self.api.nickname else m["sender"]
                if m.get("message_type") == "gift":
                    self._insert_gift_card(m)
                else:
                    self.chat_text.insert(tk.END, f"[{m['created_at']}] {who}: {m['text']}\n")
                self._last_message_id = max(self._last_message_id, m["id"])
            self.chat_text.config(state="disabled")
            if was_at_bottom:
                self.chat_text.see(tk.END)

        self._async(work, done)

    def _insert_gift_card(self, m):
        sender = m["sender"]
        receiver = m["receiver"]
        is_mine = sender == self.api.nickname
        gift_name = m.get("gift_name") or "Подарок"
        price = m.get("gift_price")
        note = m.get("gift_note") or ""
        image_url = m.get("gift_image_url")

        card = tk.Frame(self.chat_text, bg="#4c7a5d", padx=16, pady=14,
                         highlightthickness=1, highlightbackground="#3a5f48")

        price_txt = f" стоимостью {price:g} монет" if price is not None else ""
        header_txt = (f"Вы отправили подарок{price_txt}" if is_mine
                      else f"{sender} отправил(а) Вам подарок{price_txt}")
        tk.Label(card, text=header_txt, bg="#4c7a5d", fg="white",
                 font=("Segoe UI", 9, "bold"), wraplength=260, justify="center").pack(pady=(0, 10))

        img_holder = tk.Label(card, text="🎁", bg="#4c7a5d", fg="white", font=("Segoe UI", 42))
        img_holder.pack(pady=(0, 10))
        if image_url:
            self._load_gift_card_image(img_holder, image_url)

        tk.Frame(card, bg="#3a5f48", height=1).pack(fill="x", pady=(0, 8))

        bottom = tk.Frame(card, bg="#4c7a5d")
        bottom.pack(fill="x")
        avatar_letter = (sender[:1] if sender else "?").upper()
        tk.Label(bottom, text=avatar_letter, bg="#e0575a", fg="white",
                 font=("Segoe UI", 9, "bold"), width=2).pack(side="left", padx=(0, 6))
        from_txt = "Подарок от Вас" if is_mine else f"Подарок от {sender}"
        tk.Label(bottom, text=from_txt, bg="#4c7a5d", fg="white",
                 font=("Segoe UI", 9, "bold")).pack(side="left")

        to_txt = ("Кому: Себе" if sender == receiver else (f"Кому: {receiver}" if is_mine else "Кому: Вам"))
        tk.Label(card, text=to_txt, bg="#4c7a5d", fg="#d6e8da",
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 10))

        tk.Button(card, text="Просмотр", relief="flat", bg="#3a5f48", fg="white",
                  activebackground="#2f4e3a", activeforeground="white",
                  command=lambda: self._show_gift_card_details(gift_name, sender, receiver, price, note)
                  ).pack(fill="x")

        self.chat_text.window_create(tk.END, window=card)
        self.chat_text.insert(tk.END, "\n\n")
        self._chat_embedded_widgets.append(card)

    def _load_gift_card_image(self, label, image_url):
        try:
            was_at_bottom = self.chat_text.yview()[1] >= 0.999
        except Exception:
            was_at_bottom = False

        cache = getattr(self, "_gift_image_cache", None)
        if cache is None:
            cache = self._gift_image_cache = {}
        cached = cache.get(image_url)
        if cached is not None:
            animate_image_on_label(label, cached, max_px=96)
            if was_at_bottom:
                self.chat_text.see(tk.END)
            return

        def work():
            resp = requests.get(f"{self.api.base_url}{image_url}", timeout=10)
            resp.raise_for_status()
            return resp.content

        def done(data, error):
            if error or not data:
                return
            try:
                if not label.winfo_exists():
                    return
            except Exception:
                return
            cache[image_url] = data
            animate_image_on_label(label, data, max_px=96)
            if was_at_bottom:
                self.chat_text.see(tk.END)

        self._async(work, done)

    def _show_gift_card_details(self, gift_name, sender, receiver, price, note):
        lines = [f"🎁 {gift_name}"]
        if price is not None:
            lines.append(f"Стоимость: {price:g} монет")
        lines.append(f"От кого: {sender}")
        lines.append(f"Кому: {receiver}")
        if note:
            lines.append(f"\nСообщение:\n{note}")
        messagebox.showinfo("Подарок", "\n".join(lines))

    def _poll_typing_status(self):
        friend = self._current_chat_friend
        if not friend:
            return

        def work():
            return self.api.get_typing(friend)

        def done(result, error):
            if error or self._current_chat_friend != friend:
                return
            status, data = result
            if status != 200:
                return
            if data.get("is_typing"):
                self.chat_with_label.config(text=f"💬 {friend} печатает…")
            else:
                self.chat_with_label.config(text=f"💬 Переписка с {friend}")

        self._async(work, done)

    def _on_chat_entry_keypress(self, event=None):
        friend = self._current_chat_friend
        if not friend:
            return
        now = time.time()
        last_sent = getattr(self, "_last_typing_sent_at", 0)
        if now - last_sent < 2:
            return
        self._last_typing_sent_at = now

        def work():
            return self.api.set_typing(friend)

        def done(result, error):
            pass

        self._async(work, done)

    def _friends_tab_is_visible(self):
        frame = getattr(self, "friends_tab_frame", None)
        if frame is None:
            return False
        try:
            return self.notebook.select() == str(frame)
        except Exception:
            return True

    def _poll_chat_loop(self):
        visible = self._friends_tab_is_visible()
        if visible and getattr(self, "_current_chat_friend", None):
            self._fetch_chat_messages()
            self._poll_typing_status()
        self._friends_poll_tick = getattr(self, "_friends_poll_tick", 0) + 1
        if visible and self._friends_poll_tick % 3 == 0 and hasattr(self, "friends_listbox") and self.api.is_logged_in():
            self.refresh_friends()
        self.root.after(5000, self._poll_chat_loop)

    def _start_heartbeat_loop(self):
        self._heartbeat_generation = getattr(self, "_heartbeat_generation", 0) + 1
        self._heartbeat_loop(self._heartbeat_generation)

    def _stop_heartbeat_loop(self):
        self._heartbeat_generation = getattr(self, "_heartbeat_generation", 0) + 1

    def _heartbeat_loop(self, generation):
        if generation != getattr(self, "_heartbeat_generation", None):
            return
        if self.api.is_logged_in():
            threading.Thread(target=self._send_heartbeat, daemon=True).start()
        self.root.after(30000, lambda: self._heartbeat_loop(generation))

    def _send_heartbeat(self):
        try:
            status, _ = self.api.heartbeat()
            if status in (401, 403):
                self.root.after(0, self._on_session_rejected)
            else:
                self._flush_pending_sync()
                gp = getattr(self, "_active_game_player", None)
                proc = getattr(self, "process", None)
                if gp and proc is not None and proc.poll() is None:
                    try:
                        self.api.register_mc_session(gp)
                    except Exception:
                        pass
        except Exception:
            pass

    def _on_session_rejected(self):
        if not self.api.is_logged_in():
            return
        self._stop_heartbeat_loop()
        self.api.drop_local()
        self.config["server_token"] = ""
        save_json_file(CONFIG_FILE, self.config)
        try:
            self.server_status_var.set("Сессия истекла — войдите заново")
            self.account_info_label.config(text="Сессия истекла — войдите заново.")
        except Exception:
            pass
        try:
            if self.admin_tab_id is not None:
                self.notebook.tab(self.admin_tab_id, state="hidden")
        except Exception:
            pass
        try:
            messagebox.showwarning("Сессия истекла",
                                   "Сессия на сервере слетела. Войдите в аккаунт заново на вкладке «Аккаунт» — "
                                   "без этого запуск игры и вход на сервер недоступны.")
        except Exception:
            pass

    def build_admin_tab(self, frame):
        top_bar = ttk.Frame(frame)
        top_bar.pack(fill="x", padx=10, pady=(10, 5))
        ttk.Label(top_bar, text="🛡️ Админ-панель", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Button(top_bar, text="🔄 Обновить", command=self.refresh_admin_panel).pack(side="right")

        self.admin_stats_label = ttk.Label(frame, text="", foreground=self._accent)
        self.admin_stats_label.pack(anchor="w", padx=10, pady=(0, 10))

        users_frame = ttk.LabelFrame(frame, text="Пользователи")
        users_frame.pack(fill="both", expand=True, padx=10, pady=5)

        cols = ("nickname", "admin", "banned", "coins", "playtime", "launches", "last_login")
        headers = ["Никнейм", "Админ", "Бан", "Коины", "Минут", "Запусков", "Последний вход"]
        self.admin_tree = ttk.Treeview(users_frame, columns=cols, show="headings", height=10)
        for c, label in zip(cols, headers):
            self.admin_tree.heading(c, text=label)
            self.admin_tree.column(c, width=110, anchor="center")
        self.admin_tree.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=5)
        scroll = ttk.Scrollbar(users_frame, orient="vertical", command=self.admin_tree.yview)
        scroll.pack(side="right", fill="y")
        self.admin_tree.config(yscrollcommand=scroll.set)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", padx=10, pady=5)
        ttk.Button(actions, text="🚫 Забанить", command=self.admin_ban_selected).pack(side="left", padx=4)
        ttk.Button(actions, text="✅ Разбанить", command=self.admin_unban_selected).pack(side="left", padx=4)
        ttk.Button(actions, text="👑 Сделать админом", command=self.admin_make_admin_selected).pack(side="left", padx=4)
        ttk.Button(actions, text="⬇️ Снять права админа", command=self.admin_remove_admin_selected).pack(side="left", padx=4)
        ttk.Button(actions, text="🗑️ Удалить аккаунт", command=self.admin_delete_account_selected).pack(side="left", padx=4)

        logs_frame = ttk.LabelFrame(frame, text="Логи сервера")
        logs_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))
        self.admin_logs_text = scrolledtext.ScrolledText(
            logs_frame, height=8, bg="#242830", fg="white", insertbackground="white", wrap="word"
        )
        self.admin_logs_text.pack(fill="both", expand=True, padx=5, pady=5)

    def _selected_admin_nickname(self):
        sel = self.admin_tree.selection()
        if not sel:
            messagebox.showwarning("Внимание", "Выберите пользователя в списке.")
            return None
        return self.admin_tree.item(sel[0], "values")[0]

    def refresh_admin_panel(self):
        if not self.api.is_logged_in() or not self.api.is_admin:
            messagebox.showerror("Ошибка", "Нужны права администратора.")
            return

        def work():
            s1, profiles = self.api.admin_profiles()
            s2, stats = self.api.admin_stats()
            s3, logs = self.api.admin_logs(100)
            return (s1, profiles), (s2, stats), (s3, logs)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка соединения", f"Не удалось подключиться к серверу:\n{error}")
                return
            (s1, profiles), (s2, stats), (s3, logs) = result

            for row in self.admin_tree.get_children():
                self.admin_tree.delete(row)
            if s1 == 200 and isinstance(profiles, list):
                for p in profiles:
                    self.admin_tree.insert("", "end", values=(
                        p["nickname"],
                        "да" if p["is_admin"] else "нет",
                        "да" if p["is_banned"] else "нет",
                        f"{p.get('coins', 0):.1f}",
                        f"{p.get('total_playtime_minutes', 0):.1f}",
                        p.get("total_launches", 0),
                        p.get("last_login") or "-",
                    ))

            if s2 == 200 and isinstance(stats, dict):
                self.admin_stats_label.config(text=(
                    f"Всего пользователей: {stats.get('total_profiles', 0)}  |  "
                    f"Забанено: {stats.get('total_banned', 0)}  |  "
                    f"Админов: {stats.get('total_admins', 0)}  |  "
                    f"Суммарно коинов: {stats.get('total_coins', 0):.1f}"
                ))

            self.admin_logs_text.delete("1.0", tk.END)
            if s3 == 200 and isinstance(logs, list):
                for entry in logs:
                    line = f"[{entry.get('created_at', '')}] {entry.get('action', '')} — {entry.get('nickname') or '-'}"
                    if entry.get("details"):
                        line += f" ({entry['details']})"
                    self.admin_logs_text.insert(tk.END, line + "\n")

        self._async(work, done)

    def admin_ban_selected(self):
        nickname = self._selected_admin_nickname()
        if not nickname:
            return
        reason = simpledialog.askstring("Причина бана", f"Причина бана для {nickname}:", parent=self.root)
        if reason is None:
            return

        def work():
            return self.api.admin_ban(nickname, reason or "Нарушение правил")

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            status, data = result
            if status == 200 and data.get("success"):
                self.refresh_admin_panel()
            else:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось забанить"))

        self._async(work, done)

    def admin_unban_selected(self):
        nickname = self._selected_admin_nickname()
        if not nickname:
            return

        def work():
            return self.api.admin_unban(nickname)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            status, data = result
            if status == 200 and data.get("success"):
                self.refresh_admin_panel()
            else:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось разбанить"))

        self._async(work, done)

    def admin_make_admin_selected(self):
        nickname = self._selected_admin_nickname()
        if not nickname:
            return
        if not messagebox.askyesno("Подтверждение", f"Выдать права администратора пользователю {nickname}?"):
            return

        def work():
            return self.api.admin_make_admin(nickname)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            status, data = result
            if status == 200 and data.get("success"):
                self.refresh_admin_panel()
            else:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось выдать права"))

        self._async(work, done)

    def admin_remove_admin_selected(self):
        nickname = self._selected_admin_nickname()
        if not nickname:
            return
        if nickname == self.api.nickname:
            messagebox.showerror("Ошибка", "Нельзя снять права администратора с самого себя.")
            return
        if not messagebox.askyesno("Подтверждение", f"Снять права администратора у {nickname}?"):
            return

        def work():
            return self.api.admin_remove_admin(nickname)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            status, data = result
            if status == 200 and data.get("success"):
                self.refresh_admin_panel()
            else:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось снять права"))

        self._async(work, done)

    def admin_delete_account_selected(self):
        nickname = self._selected_admin_nickname()
        if not nickname:
            return
        if nickname == self.api.nickname:
            messagebox.showerror("Ошибка", "Нельзя удалить самого себя.")
            return
        if not messagebox.askyesno(
            "Удаление аккаунта",
            f"Удалить аккаунт {nickname} НАВСЕГДА?\n"
            f"Это удалит профиль, друзей, сообщения и статистику. Отменить нельзя."
        ):
            return

        def work():
            return self.api.admin_delete_account(nickname)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            status, data = result
            if status == 200 and data.get("success"):
                self.refresh_admin_panel()
            else:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось удалить аккаунт"))

        self._async(work, done)

    def build_settings_tab(self, frame):
        row = 0
        ttk.Label(frame, text="Путь к Java:").grid(row=row, column=0, sticky="w", pady=5, padx=10)
        self.java_entry = ttk.Entry(frame, width=45)
        self.java_entry.grid(row=row, column=1, pady=5, padx=5)
        self.java_entry.insert(0, self.core.java_path)
        ttk.Button(frame, text="Обзор", command=self.browse_java).grid(row=row, column=2, padx=5)

        row += 1
        ttk.Label(frame, text="Папка игры:").grid(row=row, column=0, sticky="w", pady=5, padx=10)
        self.dir_entry = ttk.Entry(frame, textvariable=self.dir_var, width=45)
        self.dir_entry.grid(row=row, column=1, pady=5, padx=5)
        ttk.Button(frame, text="Обзор", command=self.browse_dir).grid(row=row, column=2, padx=5)

        row += 1
        ttk.Label(frame, text="JVM-флаги:").grid(row=row, column=0, sticky="w", pady=5, padx=10)
        self.jvm_preset_var = tk.StringVar(value=JVM_PRESET_TITLES.get(self.config.get("jvm_preset", "default"), JVM_PRESET_TITLES["default"]))
        ttk.Combobox(frame, textvariable=self.jvm_preset_var, values=list(JVM_PRESET_TITLES.values()),
                     state="readonly", width=42).grid(row=row, column=1, pady=5, padx=5)

        row += 1
        ttk.Label(frame, text="ОЗУ (МБ):").grid(row=row, column=0, sticky="w", pady=5, padx=10)
        self.mem_settings_entry = ttk.Entry(frame, textvariable=self.mem_var, width=45)
        self.mem_settings_entry.grid(row=row, column=1, pady=5, padx=5)

        row += 1
        self.skin_auto_var = tk.BooleanVar(value=self.config.get("skin_auto_download", True))
        if HAS_PIL:
            ttk.Checkbutton(frame, text="Автозагрузка скинов",
                            variable=self.skin_auto_var).grid(row=row, column=0, columnspan=3, sticky="w", pady=5, padx=10)

        row += 1
        self.skin_system_var = tk.BooleanVar(value=self.config.get("skin_system_enabled", True))
        ttk.Checkbutton(frame, text="Показывать скины на серверах (модуль authlib-injector)",
                        variable=self.skin_system_var).grid(row=row, column=0, columnspan=3, sticky="w", pady=5, padx=10)

        row += 1
        self.show_alpha_beta_var = tk.BooleanVar(value=self.config.get("show_alpha_beta", False))
        ttk.Checkbutton(frame, text="Показывать альфа/бета версии",
                        variable=self.show_alpha_beta_var,
                        command=self.on_toggle_alpha_beta).grid(row=row, column=0, columnspan=3, sticky="w", pady=5, padx=10)

        row += 1
        self.auto_clean_var = tk.BooleanVar(value=self.config.get("auto_clean_logs", True))
        ttk.Checkbutton(frame, text="Автоочистка старых логов",
                        variable=self.auto_clean_var).grid(row=row, column=0, columnspan=3, sticky="w", pady=5, padx=10)

        row += 1
        self.tray_min_var = tk.BooleanVar(value=self.config.get("tray_on_minimize", True))
        self.tray_close_var = tk.BooleanVar(value=self.config.get("tray_on_close", False))
        if HAS_TRAY:
            ttk.Checkbutton(frame, text="Сворачивать в трей при сворачивании окна",
                            variable=self.tray_min_var, command=self._on_tray_settings
                            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=5, padx=10)
            row += 1
            ttk.Checkbutton(frame, text="Крестик закрывает в трей (выход — через меню иконки или Ctrl+Q)",
                            variable=self.tray_close_var, command=self._on_tray_settings
                            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=5, padx=10)

        row += 1
        ttk.Label(frame, text="Акцентный цвет:").grid(row=row, column=0, sticky="w", pady=5, padx=10)
        current_theme = self.config.get("launcher_theme", DEFAULT_LAUNCHER_THEME)
        self._theme_labels = {v["label"]: k for k, v in LAUNCHER_THEMES.items()}
        self.theme_var = tk.StringVar(
            value=LAUNCHER_THEMES.get(current_theme, LAUNCHER_THEMES[DEFAULT_LAUNCHER_THEME])["label"]
        )
        theme_combo = ttk.Combobox(
            frame, textvariable=self.theme_var, state="readonly", width=30,
            values=[v["label"] for v in LAUNCHER_THEMES.values()]
        )
        theme_combo.grid(row=row, column=1, sticky="w", pady=5, padx=5)
        theme_combo.bind("<<ComboboxSelected>>", lambda e: self._on_theme_selected())

        row += 1
        ttk.Label(frame, text="Материал окна:").grid(row=row, column=0, sticky="w", pady=5, padx=10)
        cur_mat = self.config.get("window_material", DEFAULT_WINDOW_MATERIAL)
        self._material_labels = {v["label"]: k for k, v in WINDOW_MATERIALS.items()}
        self.material_var = tk.StringVar(
            value=WINDOW_MATERIALS.get(cur_mat, WINDOW_MATERIALS["solid"])["label"])
        material_combo = ttk.Combobox(
            frame, textvariable=self.material_var, state="readonly", width=30,
            values=[v["label"] for v in WINDOW_MATERIALS.values()]
        )
        material_combo.grid(row=row, column=1, sticky="w", pady=5, padx=5)
        material_combo.bind("<<ComboboxSelected>>", lambda e: self._on_material_selected())

        row += 1
        ttk.Label(frame, text="Стиль меню слева:").grid(row=row, column=0, sticky="w", pady=5, padx=10)
        self._nav_labels = {v: k for k, v in self.NAV_STYLES.items()}
        self.nav_style_var = tk.StringVar(
            value=self.NAV_STYLES.get(self.config.get("nav_style", "list"), self.NAV_STYLES["list"]))
        nav_combo = ttk.Combobox(frame, textvariable=self.nav_style_var, state="readonly", width=30,
                                 values=list(self.NAV_STYLES.values()))
        nav_combo.grid(row=row, column=1, sticky="w", pady=5, padx=5)
        nav_combo.bind("<<ComboboxSelected>>",
                       lambda e: self.set_nav_style(self._nav_labels.get(self.nav_style_var.get())))

        row += 1
        ttk.Button(frame, text="📂 Открыть папку игры",
                   command=self.open_game_folder).grid(row=row, column=0, columnspan=3, sticky="w", pady=10, padx=10)

        row += 1
        java_info = self.core.get_java_version() or "❌ Java не найдена"
        ttk.Label(frame, text=f"☕ Java: {java_info}", foreground="#7a8599").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=2, padx=10)

        row += 1
        self.game_size_label = ttk.Label(frame, text="", foreground=self._accent)
        self.game_size_label.grid(row=row, column=0, columnspan=3, sticky="w", pady=2, padx=10)
        self._update_game_size_label()

        row += 1
        ttk.Label(frame, text=f"🔖 Версия лаунчера: {APP_VERSION}", foreground="#7a8599").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=2, padx=10)

        row += 1
        ttk.Button(frame, text="🔄 Проверить обновление",
                   command=self.check_for_updates_manual).grid(row=row, column=0, sticky="w", pady=(4, 0), padx=10)

        row += 1
        btn_row = ttk.Frame(frame)
        btn_row.grid(row=row, column=0, columnspan=3, pady=15)
        ttk.Button(btn_row, text="💾 Сохранить", command=self.save_settings).pack(side="left", padx=5)
        ttk.Button(btn_row, text="📤 Экспорт конфига", command=self.export_config).pack(side="left", padx=5)
        ttk.Button(btn_row, text="📥 Импорт конфига", command=self.import_config).pack(side="left", padx=5)

    def _update_game_size_label(self):
        total = self.core.get_total_game_size()
        if total > 1024 * 1024 * 1024:
            txt = f"📊 Размер игры: {total / (1024**3):.2f} ГБ"
        else:
            txt = f"📊 Размер игры: {total / (1024**2):.0f} МБ"
        if hasattr(self, "game_size_label"):
            self.game_size_label.config(text=txt)

    def _on_theme_selected(self):
        theme_key = self._theme_labels.get(self.theme_var.get())
        if theme_key:
            self.apply_theme(theme_key)

    def _on_material_selected(self):
        material = self._material_labels.get(self.material_var.get())
        if material:
            self.apply_material(material)

    def on_toggle_alpha_beta(self):
        self.config["show_alpha_beta"] = self.show_alpha_beta_var.get()
        self.save_config()
        self.load_versions()

    def open_game_folder(self):
        path = self.dir_var.get().strip()
        os.makedirs(path, exist_ok=True)
        self._open_folder(path)

    def browse_dir(self):
        folder = filedialog.askdirectory(title="Выберите папку для .minecraft")
        if folder:
            self.dir_var.set(folder)

    def browse_java(self):
        file = filedialog.askopenfilename(
            title="Выберите Java",
            filetypes=[("Исполняемые файлы", "*.exe" if os.name == "nt" else "*"), ("Все файлы", "*.*")]
        )
        if file:
            self.java_entry.delete(0, tk.END)
            self.java_entry.insert(0, file)

    def save_settings(self):
        old_java = self.config.get("java_path", "java")
        old_mem = self.config.get("memory_mb", 2048)
        old_dir = self.config.get("minecraft_dir", DEFAULT_CONFIG["minecraft_dir"])

        self.core.java_path = self.java_entry.get().strip()
        self.core.minecraft_dir = self.dir_var.get().strip()
        try:
            mem = int(self.mem_var.get().strip())
            if mem < 256:
                mem = 256
            self.core.memory = mem
        except ValueError:
            pass
        self.config["java_path"] = self.core.java_path
        self.config["minecraft_dir"] = self.core.minecraft_dir
        self.config["memory_mb"] = self.core.memory
        self.config["jvm_preset"] = {v: k for k, v in JVM_PRESET_TITLES.items()}.get(self.jvm_preset_var.get(), "default")
        self.core.jvm_preset = self.config["jvm_preset"]
        self.config["auto_clean_logs"] = self.auto_clean_var.get()
        if HAS_PIL:
            self.config["skin_auto_download"] = self.skin_auto_var.get()
        self.config["skin_system_enabled"] = self.skin_system_var.get()

        self.save_config()
        if self.config.get("active_modpack"):
            self.core.game_dir = self._modpack_dir(self.config["active_modpack"])
        self._rebuild_managers()
        self._update_game_size_label()

        if self.core.java_path and self.core.java_path != "java" and self.core.java_path != old_java:
            self._set_achievement_flag("java_path_customized", 1)
        if self.core.memory != old_mem and self.core.memory != DEFAULT_CONFIG.get("memory_mb"):
            self._set_achievement_flag("memory_customized", 1)
        if self.core.minecraft_dir != old_dir and self.core.minecraft_dir != DEFAULT_CONFIG["minecraft_dir"]:
            self._set_achievement_flag("dir_customized", 1)

        messagebox.showinfo("Настройки", "✅ Настройки сохранены")

    def export_config(self):
        path = filedialog.asksaveasfilename(
            title="Экспорт конфига",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")]
        )
        if path:
            safe = {k: v for k, v in self.config.items()
                    if k not in ("server_token", "server_nickname", "pending_sync")}
            save_json_file(path, safe)
            messagebox.showinfo("Экспорт", "✅ Конфиг экспортирован")
            self._bump_achievement("exports", 1)

    def import_config(self):
        path = filedialog.askopenfilename(
            title="Импорт конфига",
            filetypes=[("JSON", "*.json")]
        )
        if path:
            try:
                imported = load_json_file(path, DEFAULT_CONFIG)
                for k in ("server_token", "server_nickname", "pending_sync"):
                    imported.pop(k, None)
                self.config.update(imported)
                self.save_config()
                messagebox.showinfo("Импорт", "✅ Конфиг импортирован. Перезапустите лаунчер.")
                self._bump_achievement("imports", 1)
            except Exception as e:
                messagebox.showerror("Ошибка", str(e))

    def build_news_tab(self, frame):
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=10, pady=(10, 5))
        ttk.Label(top, text="📰 Новости Mafin Launcher", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Button(top, text="🔔 Проверить новости", command=self.check_for_news).pack(side="right")
        self.news_new_badge = ttk.Label(top, text="", foreground="#f04747", font=("Segoe UI", 10, "bold"))
        self.news_new_badge.pack(side="right", padx=10)

        self.news_text = scrolledtext.ScrolledText(
            frame, bg="#1a1d23", fg="#c9d1d9", font=("Segoe UI", 10),
            insertbackground="white", state="disabled", wrap="word"
        )
        self.news_text.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        self.news_admin_frame = ttk.LabelFrame(frame, text="Опубликовать новость (админ)")
        self.news_title_entry = ttk.Entry(self.news_admin_frame, width=60)
        self.news_title_entry.grid(row=0, column=0, columnspan=2, sticky="we", padx=8, pady=(8, 4))
        self.news_body_text = tk.Text(self.news_admin_frame, height=4, bg="#242830", fg="white",
                                       insertbackground="white", wrap="word")
        self.news_body_text.grid(row=1, column=0, columnspan=2, sticky="we", padx=8, pady=4)
        ttk.Button(self.news_admin_frame, text="📤 Опубликовать",
                   command=self.publish_news_entry).grid(row=2, column=0, sticky="w", padx=8, pady=(4, 8))
        self.news_admin_frame.columnconfigure(0, weight=1)
        self.news_admin_frame.pack_forget()
        self.refresh_news_admin_visibility()

        self.refresh_news(show_errors=False)

    def refresh_news_admin_visibility(self):
        if not hasattr(self, "news_admin_frame"):
            return
        if self.api.is_logged_in() and self.api.is_admin:
            self.news_admin_frame.pack(fill="x", padx=10, pady=(0, 10))
        else:
            self.news_admin_frame.pack_forget()

    def refresh_news(self, show_errors=True, _retry=0):
        def work():
            return self.api.get_news()

        def done(result, error):
            if error or not result:
                if show_errors:
                    messagebox.showerror("Ошибка", f"Не удалось загрузить новости с сервера:\n{error}")
                elif _retry < 2:
                    self.root.after(1500, lambda: self.refresh_news(show_errors=False, _retry=_retry + 1))
                else:
                    self.news_new_badge.config(text="⚠ не удалось обновить новости")
                return
            status, data = result
            if status != 200:
                if show_errors:
                    messagebox.showerror("Ошибка", data.get("error", "Не удалось загрузить новости"))
                elif _retry < 2:
                    self.root.after(1500, lambda: self.refresh_news(show_errors=False, _retry=_retry + 1))
                else:
                    self.news_new_badge.config(text="⚠ не удалось обновить новости")
                return
            items = data.get("news", [])
            self.news_text.config(state="normal")
            self.news_text.delete("1.0", tk.END)
            is_admin = self.api.is_logged_in() and self.api.is_admin
            if not items:
                self.news_text.insert(tk.END, "Пока новостей нет.")
            for entry in items:
                self.news_text.insert(tk.END, f"📌 {entry['title']}", "title")
                if is_admin:
                    edit_btn = ttk.Button(
                        self.news_text, text="✏️", width=3, style="Small.TButton",
                        command=lambda ent=entry: self._edit_news_entry(ent)
                    )
                    self.news_text.window_create(tk.END, window=edit_btn, padx=6)
                    del_btn = ttk.Button(
                        self.news_text, text="🗑", width=3, style="Small.TButton",
                        command=lambda nid=entry["id"]: self._delete_news_entry(nid)
                    )
                    self.news_text.window_create(tk.END, window=del_btn, padx=4)
                self.news_text.insert(tk.END, "\n")
                author = entry.get("author") or ""
                when = entry.get("created_at", "")
                meta_line = f"{when}" + (f" • {author}" if author else "")
                self.news_text.insert(tk.END, meta_line)
                if entry.get("edited"):
                    edited_at = entry.get("edited_at") or ""
                    suffix = f" ({edited_at})" if edited_at else ""
                    self.news_text.insert(tk.END, f"  ✅ изменено{suffix}", "edited")
                self.news_text.insert(tk.END, "\n\n")
                self.news_text.insert(tk.END, entry.get("body", "") + "\n")
                self.news_text.insert(tk.END, "\n" + ("─" * 60) + "\n\n")
            self.news_text.tag_config("title", font=("Segoe UI", 11, "bold"), foreground=self._accent)
            self.news_text.tag_config("edited", font=("Segoe UI", 8, "italic"), foreground="#7a8599")
            self.news_text.config(state="disabled")
            if items:
                self.config["last_seen_news_id"] = items[0]["id"]
                save_json_file(CONFIG_FILE, self.config)
            self.news_new_badge.config(text="")

        self._async(work, done)

    def _edit_news_entry(self, entry):
        dialog = tk.Toplevel(self.root)
        dialog.title("✏️ Редактировать новость")
        dialog.configure(bg=self._bg)
        dialog.geometry("520x360")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="Заголовок:").pack(anchor="w", padx=12, pady=(12, 2))
        title_entry = ttk.Entry(dialog, width=60)
        title_entry.pack(fill="x", padx=12)
        title_entry.insert(0, entry.get("title", ""))

        ttk.Label(dialog, text="Текст:").pack(anchor="w", padx=12, pady=(10, 2))
        body_text = tk.Text(dialog, bg="#242830", fg="white", insertbackground="white", wrap="word")
        body_text.pack(fill="both", expand=True, padx=12)
        body_text.insert("1.0", entry.get("body", ""))

        def save():
            new_title = title_entry.get().strip()
            new_body = body_text.get("1.0", tk.END).strip()
            if not new_title or not new_body:
                messagebox.showwarning("Внимание", "Заполните заголовок и текст новости.")
                return

            def work():
                return self.api.edit_news(entry["id"], new_title, new_body)

            def done(result, error):
                if error:
                    messagebox.showerror("Ошибка", str(error))
                    return
                status, data = result
                if status == 200 and data.get("success"):
                    dialog.destroy()
                    self.refresh_news(show_errors=False)
                else:
                    messagebox.showerror("Ошибка", data.get("error", "Не удалось сохранить изменения"))

            self._async(work, done)

        btn_row = ttk.Frame(dialog)
        btn_row.pack(fill="x", padx=12, pady=10)
        ttk.Button(btn_row, text="💾 Сохранить", command=save).pack(side="left")
        ttk.Button(btn_row, text="Отмена", command=dialog.destroy).pack(side="left", padx=8)

    def _delete_news_entry(self, news_id):
        if not messagebox.askyesno("Удаление новости", "Удалить эту новость безвозвратно?"):
            return

        def work():
            return self.api.delete_news(news_id)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            status, data = result
            if status == 200 and data.get("success"):
                self.refresh_news(show_errors=False)
            else:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось удалить новость"))

        self._async(work, done)

    def check_for_news(self):
        def work():
            return self.api.news_latest_id()

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", f"Не удалось проверить новости:\n{error}")
                return
            status, data = result
            if status != 200:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось проверить новости"))
                return
            latest_id = data.get("latest_id", 0)
            last_seen = self.config.get("last_seen_news_id", 0)
            if latest_id > last_seen:
                self.news_new_badge.config(text=f"🆕 Новые новости!")
                self.refresh_news(show_errors=False)
            else:
                messagebox.showinfo("Новости", "Новых новостей нет.")

        self._async(work, done)

    def publish_news_entry(self):
        title = self.news_title_entry.get().strip()
        body = self.news_body_text.get("1.0", tk.END).strip()
        if not title or not body:
            messagebox.showwarning("Внимание", "Заполните заголовок и текст новости.")
            return

        def work():
            return self.api.publish_news(title, body)

        def done(result, error):
            if error:
                messagebox.showerror("Ошибка", str(error))
                return
            status, data = result
            if status == 201 and data.get("success"):
                self.news_title_entry.delete(0, tk.END)
                self.news_body_text.delete("1.0", tk.END)
                self.refresh_news(show_errors=False)
            else:
                messagebox.showerror("Ошибка", data.get("error", "Не удалось опубликовать новость"))

        self._async(work, done)

    def build_about_tab(self, frame):
        ttk.Label(frame, text=f"⚡ Mafin Launcher v{APP_VERSION}",
                  font=("Segoe UI", 20, "bold")).pack(pady=(30, 5))
        ttk.Label(frame, text="Неофициальный лаунчер Minecraft в стиле TLauncher",
                  foreground="#7a8599").pack(pady=2)
        ttk.Label(frame, text="Установка: minecraft-launcher-lib + кастомные скрипты").pack(pady=2)
        ttk.Label(frame, text="Forge (вкл. 1.7.10) • Fabric • NeoForge • Quilt • Сборки • Modrinth • P2P • Моды • Ресурспаки").pack(pady=2)
        ttk.Label(frame, text="Python 3.10+ совместимость", foreground="#7a8599").pack(pady=10)

        sep = ttk.Separator(frame, orient="horizontal")
        sep.pack(fill="x", padx=50, pady=10)

        ttk.Label(frame, text="Горячие клавиши:", font=("Segoe UI", 11, "bold")).pack(pady=5)
        ttk.Label(frame, text="Ctrl+L — Запустить игру").pack()
        ttk.Label(frame, text="Ctrl+I — Установить и запустить").pack()
        ttk.Label(frame, text="Ctrl+Q — Выход").pack()

    def load_versions(self):
        def fetch():
            try:
                vlist = self.core.get_versions(
                    include_alpha_beta=self.config.get("show_alpha_beta", False)
                )
                self._all_versions = vlist

                def update_ui():
                    self.version_combo.config(values=vlist)
                    if vlist:
                        last = self.config.get("last_version", "")
                        if last in vlist:
                            self.version_var.set(last)
                        else:
                            self.version_var.set(vlist[0])
                        self.root.after(0, self.on_version_select)
                self.root.after(0, update_ui)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось загрузить версии: {e}"))

        self.root.after(100, lambda: threading.Thread(target=fetch, daemon=True).start())

    def on_version_select(self, event=None):
        loader = self.modloader_var.get()
        if loader == "forge":
            self._fetch_forge_versions()
        elif loader == "fabric":
            self._fetch_fabric_loaders()
        elif loader in self._LOADER_UI:
            self._fetch_loader_versions(loader)

    def install_and_launch(self):
        if self.installing:
            return
        if not self.api.is_logged_in():
            messagebox.showerror("Требуется вход", "Сессия не активна. Войдите в аккаунт на вкладке «Аккаунт», чтобы играть.")
            self._select_account_tab()
            return
        self._apply_auto_memory()
        self.installing = True
        self.install_btn.config(state="disabled")
        self.launch_btn.config(state="disabled")
        threading.Thread(target=self._install_and_launch, daemon=True).start()

    def _install_and_launch(self):
        try:
            version = self.version_var.get().strip()
            if not version:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Выберите версию"))
                return
            player = self.player_var.get().strip()
            if not player:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Профиль не выбран. Зарегистрируйтесь и войдите в аккаунт на вкладке «Аккаунт»."))
                return
            try:
                mem = int(self.mem_var.get().strip())
                if mem < 512:
                    self.root.after(0, lambda: messagebox.showwarning("ОЗУ", "Рекомендуется минимум 512 МБ"))
            except ValueError:
                self.root.after(0, lambda: messagebox.showerror("Ошибка", "Введите число для ОЗУ"))
                return

            minecraft_dir = self.dir_var.get().strip()
            os.makedirs(minecraft_dir, exist_ok=True)
            self.core.minecraft_dir = minecraft_dir
            self.core.memory = mem
            self.config["last_version"] = version
            self.save_config()

            meter = DownloadMeter(self.core.minecraft_dir, version)
            meter_state = {"stage": ""}

            def show_mb(done_b, total_b):
                text = f"{meter_state['stage'] or 'Скачивание'}: {_fmt_mb(done_b, total_b)}"
                pct = done_b * 100 / total_b
                self.root.after(0, lambda: (self.progress.config(value=pct), self.status_var.set(text)))

            def callback(progress, total, stage):
                meter_state["stage"] = stage
                if meter.active:
                    done_b, total_b = meter.snapshot()
                    if total_b:
                        show_mb(done_b, total_b)
                        return
                if total:
                    pct = int(progress / total * 100)
                    self.root.after(0, lambda: self.progress.config(value=pct))
                    self.root.after(0, lambda: self.status_var.set(f"{stage}: {progress}/{total}"))
                else:
                    self.root.after(0, lambda: self.status_var.set(stage))

            self.root.after(0, lambda: self.status_var.set(f"📥 Установка {version}..."))
            was_installed = os.path.isfile(os.path.join(minecraft_dir, "versions", version, f"{version}.json"))
            meter.start(show_mb)
            try:
                self.core.install_version(version, callback)
            finally:
                meter.stop()
            self._bump_achievement("versions_installed", 1)
            if not was_installed:
                self._add_launcher_server_to_game(version)

            loader = self.modloader_var.get()
            launch_version = version

            if loader == "forge":
                forge_ver = self.forge_var.get().strip()
                if forge_ver not in ("", "Без Forge", "Загрузка...", "Forge недоступен"):
                    self.root.after(0, lambda: self.status_var.set(f"⚡ Установка Forge {forge_ver}..."))
                    try:
                        self.core.install_forge(forge_ver, callback)
                        launch_version = self.core.get_forge_launch_version(forge_ver)
                        self._bump_achievement("forge_installs", 1)
                    except Exception as e:
                        self.root.after(0, lambda err=e: messagebox.showerror("Ошибка Forge", str(err)))
                        return

            elif loader == "fabric":
                fab_loader = self.fabric_var.get().strip()
                if fab_loader and fab_loader != "Без Fabric":
                    self.root.after(0, lambda: self.status_var.set(f"🧵 Установка Fabric {fab_loader}..."))
                    try:
                        self.core.install_fabric(version, fab_loader, callback)
                        launch_version = self.core.get_fabric_launch_version(version, fab_loader)
                        self._bump_achievement("fabric_installs", 1)
                    except Exception as e:
                        self.root.after(0, lambda err=e: messagebox.showerror("Ошибка Fabric", str(err)))
                        return

            elif loader in self._LOADER_UI:
                title, none_label, var_name, _c = self._LOADER_UI[loader]
                lv = getattr(self, var_name).get().strip()
                if lv and lv not in (none_label, "Загрузка...", f"{title} недоступен"):
                    self.root.after(0, lambda t=title, v=lv: self.status_var.set(f"📥 Установка {t} {v}..."))
                    try:
                        self.core.install_loader_generic(loader, title, version, lv, callback)
                        launch_version = self.core.get_loader_launch_version(loader, version, lv)
                    except Exception as e:
                        self.root.after(0, lambda err=e, t=title: messagebox.showerror(f"Ошибка {t}", str(err)))
                        return

            self.root.after(0, lambda: self.progress.config(value=100))
            self.root.after(0, lambda: self.status_var.set("✅ Установка завершена, запуск..."))
            self.root.after(0, self.refresh_installed_versions)
            self._launch_game(launch_version, player, minecraft_dir, mem)

        except Exception as e:
            self.root.after(0, lambda err=e: messagebox.showerror("Ошибка", str(err)))
        finally:
            self.installing = False
            self.root.after(0, lambda: self.install_btn.config(state="normal"))
            self.root.after(0, lambda: self.launch_btn.config(state="normal"))
            self.root.after(0, lambda: self.progress.config(value=0))
            self.root.after(0, lambda: self.status_var.set("Готов"))

    def _add_launcher_server_to_game(self, version):
        """После скачивания версии 1.20.4 и новее кладёт сервер лаунчера в «Сетевая игра»."""
        try:
            key = mc_version_key(mc_game_version(version))
            if not key or key < LAUNCHER_SERVER_MIN_VERSION:
                return
            info = None
            try:
                status, data = self.api.mc_status()
                if status == 200 and data.get("success") and data.get("address"):
                    info = data
            except Exception:
                info = None
            info = info or getattr(self, "_pinned_server", None)
            if not info or not info.get("address"):
                _log_install("Сервер лаунчера не добавлен в «Сетевая игра»: адрес сервера недоступен")
                return
            name = info.get("name") or "Mafin Server"
            if servers_dat_add(self.core.game_path(), name, info["address"]):
                _log_install(f"Сервер «{name}» ({info['address']}) добавлен в «Сетевая игра» для {version}")
        except Exception as e:
            _log_install(f"Не удалось добавить сервер лаунчера в «Сетевая игра»: {e}")

    def _select_account_tab(self):
        try:
            for name, tab in self._nav_defs:
                if "Аккаунт" in name:
                    self.notebook.select(tab)
                    break
        except Exception:
            pass

    def launch_only(self):
        if self.installing:
            return
        if not self.api.is_logged_in():
            messagebox.showerror("Требуется вход", "Сессия не активна. Войдите в аккаунт на вкладке «Аккаунт», чтобы играть.")
            self._select_account_tab()
            return
        version = self.version_var.get().strip()
        if not version:
            messagebox.showerror("Ошибка", "Выберите версию")
            return
        player = self.player_var.get().strip()
        if not player:
            messagebox.showerror("Ошибка", "Профиль не выбран. Зарегистрируйтесь и войдите в аккаунт на вкладке «Аккаунт».")
            return
        self._apply_auto_memory()
        try:
            mem = int(self.mem_var.get().strip())
        except ValueError:
            messagebox.showerror("Ошибка", "Введите число для ОЗУ")
            return

        minecraft_dir = self.dir_var.get().strip()
        if not os.path.exists(minecraft_dir):
            messagebox.showerror("Ошибка", "Папка игры не найдена. Сначала установите игру.")
            return
        self.core.minecraft_dir = minecraft_dir
        self._auto_backup_worlds(self.core.game_path())

        loader = self.modloader_var.get()
        launch_version = version

        if loader == "forge":
            forge_ver = self.forge_var.get().strip()
            if forge_ver not in ("", "Без Forge", "Загрузка...", "Forge недоступен"):
                try:
                    launch_version = self.core.get_forge_launch_version(forge_ver)
                except Exception as e:
                    messagebox.showerror("Ошибка", f"Некорректная версия Forge: {e}")
                    return
                if not os.path.isdir(os.path.join(minecraft_dir, "versions", launch_version)):
                    messagebox.showerror("Ошибка", "Forge не установлен. Нажмите «Установить и запустить».")
                    return

        elif loader == "fabric":
            fab_loader = self.fabric_var.get().strip()
            if fab_loader and fab_loader != "Без Fabric":
                launch_version = self.core.get_fabric_launch_version(version, fab_loader)
                if not os.path.isdir(os.path.join(minecraft_dir, "versions", launch_version)):
                    messagebox.showerror("Ошибка", "Fabric не установлен. Нажмите «Установить и запустить».")
                    return

        elif loader in self._LOADER_UI:
            title, none_label, var_name, _c = self._LOADER_UI[loader]
            lv = getattr(self, var_name).get().strip()
            if lv and lv not in (none_label, "Загрузка...", f"{title} недоступен"):
                try:
                    launch_version = self.core.get_loader_launch_version(loader, version, lv)
                except Exception as e:
                    messagebox.showerror("Ошибка", f"Некорректная версия {title}: {e}")
                    return
                if not os.path.isdir(os.path.join(minecraft_dir, "versions", launch_version)):
                    messagebox.showerror("Ошибка", f"{title} не установлен. Нажмите «Установить и запустить».")
                    return

        threading.Thread(
            target=self._launch_game,
            args=(launch_version, player, minecraft_dir, mem),
            daemon=True,
        ).start()

    def _classify_version_for_achievements(self, version):
        v = version.lower()
        is_legacy = v.startswith(("a", "b", "c0.", "rd-", "inf-"))
        is_prerelease = bool(
            re.match(r"^\d\dw\d\d[a-z]$", v)
            or "pre" in v or "-rc" in v or v.endswith("rc") or "release candidate" in v
        )
        return is_legacy, is_prerelease

    def _launch_game(self, version, player, minecraft_dir, mem):
        self.root.after(0, lambda: self.install_btn.config(state="disabled"))
        self.root.after(0, lambda: self.launch_btn.config(state="disabled"))
        try:
            self.core.minecraft_dir = minecraft_dir
            self.core.memory = mem
            self._current_world = None

            if not self.api.is_logged_in():
                raise SessionError("Сессия не активна. Войдите в аккаунт заново.")
            try:
                st_mc, data_mc = self.api.register_mc_session(player)
            except Exception as e:
                print(f"Не удалось зарегистрировать игровой ник на сервере: {e}")
                st_mc, data_mc = 0, {}
            if st_mc in (401, 403):
                self.root.after(0, self._on_session_rejected)
                raise SessionError("Сессия слетела. Войдите в аккаунт заново — запуск под этим ником запрещён.")
            if st_mc in (400, 409):
                err_txt = data_mc.get("error") if isinstance(data_mc, dict) else None
                raise SessionError(err_txt or "Сервер отклонил игровой ник.")
            if st_mc != 200:
                self.root.after(0, lambda: self.status_var.set("⚠️ Сервер недоступен: вход на сервер может не сработать"))

            skins_on = self.config.get("skin_system_enabled", True)
            self.core.skin_server_url = self.api.base_url if skins_on else ""
            if skins_on and self.api.is_logged_in():
                acc_skin = self._get_account_by_name(player)
                if acc_skin and self._skin_needs_upload(acc_skin):
                    try:
                        with open(acc_skin["skin_path"], "rb") as f:
                            st, rs = self.api.skin_upload(player, f.read(), acc_skin.get("skin_model", "default"))
                        if st == 200 and rs.get("success"):
                            acc_skin["skin_uploaded"] = (f"{os.path.getmtime(acc_skin['skin_path'])}:"
                                                         f"{acc_skin.get('skin_model', 'default')}")
                            self.root.after(0, self.save_config)
                    except Exception as e:
                        print(f"Не удалось загрузить скин на сервер: {e}")

            join = getattr(self, "_pending_join_address", None)
            self._pending_join_address = None
            proc = self.core.launch(
                version, player,
                extra_args=server_join_args(mc_game_version(version), join) if join else None,
                callback=lambda p: self.root.after(0, lambda: self.status_var.set("🎮 Игра запущена")),
                status_callback=lambda text: self.root.after(0, lambda: self.status_var.set(text)),
            )

            self.stats.record_launch(version)
            self._launch_start_time = time.time()
            self.root.after(0, self._update_stats_label)

            now = datetime.now()
            is_legacy, is_prerelease = self._classify_version_for_achievements(version)
            counter_deltas = {}
            if is_legacy:
                counter_deltas["legacy_launches"] = 1
            if is_prerelease:
                counter_deltas["prerelease_launches"] = 1
            if 0 <= now.hour < 5:
                counter_deltas["night_launches"] = 1
            if now.weekday() >= 5:
                counter_deltas["weekend_launches"] = 1
            if getattr(self, "_last_launch_failed", False):
                counter_deltas["crash_recoveries"] = 1
            self._last_launch_failed = False
            for name, amount in counter_deltas.items():
                self.achievements.bump(name, amount)
            if counter_deltas:
                self._sync_counter_to_server(deltas=counter_deltas)

            session = self._report_launch_to_server(version)

            self.process = proc
            self._start_log_readers(proc)
            self.root.after(0, lambda: self._tray_notify("Mafin Launcher", f"Игра запущена: {version}")
                            if self._tray_icon else self.show_log_window())
            self._active_game_version = version
            self._active_game_player = player
            if self.discord.connected:
                self.discord.set_playing_state(version, player)
            self._check_achievements()

            def wait_and_record():
                proc.wait()
                rc = proc.returncode
                for t in list(self._log_readers):
                    t.join(timeout=2)
                if rc not in (0, None) and self._launch_start_time and time.time() - self._launch_start_time < 120:
                    tail = "\n".join(list(self._log_tail)[-25:])
                    _log_install(f"Игра {version} завершилась с кодом {rc}. Последние строки вывода:\n{tail}")
                    self._last_launch_failed = True
                    self.root.after(0, lambda r=rc, t=tail: messagebox.showerror(
                        "Игра не запустилась",
                        f"Код выхода: {r}\n\n{t[-1500:] or 'Игра ничего не вывела.'}\n\n"
                        f"Полный вывод: {INSTALL_LOG_FILE}"))
                self.root.after(0, lambda: self._tray_notify(
                    "Mafin Launcher",
                    "Игра закрыта" if rc in (0, None) else f"Игра завершилась с ошибкой (код {rc})"))
                if self._launch_start_time:
                    elapsed_seconds = time.time() - self._launch_start_time
                    elapsed_minutes = round(elapsed_seconds / 60.0, 2)
                    self.stats.add_play_time(elapsed_minutes)
                    self.root.after(0, self._update_stats_label)
                    nick = self.api.nickname or self.config.get("server_nickname") or ""
                    if nick and self.config.get("server_token"):
                        try:
                            session["thread"].join(timeout=35)
                        except Exception:
                            pass
                        session["abandon"] = True
                        self._enqueue_pending_sync({
                            "nick": nick, "version": version,
                            "minutes": elapsed_minutes if elapsed_minutes >= 0.1 else 0,
                            "launch_counted": bool(session["reported"]),
                        })
                        threading.Thread(target=self._flush_pending_sync, daemon=True).start()
                self._active_game_version = None
                self._active_game_player = None
                if self.discord.connected:
                    self.discord.set_menu_state()
                self.root.after(0, lambda: self.install_btn.config(state="normal"))
                self.root.after(0, lambda: self.launch_btn.config(state="normal"))
                self.root.after(0, lambda: self.status_var.set("Готов"))
                self._check_achievements()

            threading.Thread(target=wait_and_record, daemon=True).start()

        except SessionError as e:
            self.root.after(0, lambda err=e: messagebox.showerror("Нужен вход", str(err)))
            self.root.after(0, lambda: self.install_btn.config(state="normal"))
            self.root.after(0, lambda: self.launch_btn.config(state="normal"))
            self.root.after(0, lambda: self.status_var.set("Готов"))
        except Exception as e:
            self._last_launch_failed = True
            self.root.after(0, lambda err=e: messagebox.showerror("Ошибка запуска", str(err)))
            self.root.after(0, lambda: self.install_btn.config(state="normal"))
            self.root.after(0, lambda: self.launch_btn.config(state="normal"))
            self.root.after(0, lambda: self.status_var.set("Готов"))

    def _enqueue_pending_sync(self, entry):
        if entry.get("launch_counted") and not entry.get("minutes"):
            return
        with self._pending_sync_lock:
            q = list(self.config.get("pending_sync", []))
            q.append(entry)
            self.config["pending_sync"] = q[-200:]
            save_json_file(CONFIG_FILE, self.config)

    def _flush_pending_sync(self):
        if not self.api.is_logged_in() or not self._pending_sync_lock.acquire(blocking=False):
            return
        try:
            nick = (self.api.nickname or "").lower()
            q = list(self.config.get("pending_sync", []))
            if not q:
                return
            left, changed, stop = [], False, False
            for e in q:
                if stop or str(e.get("nick", "")).lower() != nick:
                    left.append(e)
                    continue
                try:
                    minutes = float(e.get("minutes") or 0)
                    if minutes > 0:
                        status, data = self.api.report_playtime(
                            minutes, e.get("version", "unknown"), launch_counted=bool(e.get("launch_counted")))
                    elif not e.get("launch_counted"):
                        status, data = self.api.report_launch(e.get("version", "unknown"))
                    else:
                        changed = True
                        continue
                except Exception as ex:
                    print(f"Синхронизация отложена: {ex}")
                    left.append(e)
                    stop = True
                    continue
                if status == 200:
                    changed = True
                    if isinstance(data, dict):
                        self.stats.adopt_server_totals(data.get("total_launches"), data.get("total_playtime"))
                elif status in (401, 403) or status >= 500 or status == 429:
                    left.append(e)
                    stop = True
                else:
                    changed = True
            if changed:
                self.config["pending_sync"] = left
                save_json_file(CONFIG_FILE, self.config)
                self.root.after(0, self._update_stats_label)
                self.root.after(0, self._refresh_account_info_label)
        finally:
            self._pending_sync_lock.release()

    def _report_launch_to_server(self, version):
        session = {"reported": False, "abandon": False, "thread": None}

        def work():
            for attempt in range(1, 4):
                if session["abandon"] or not self.api.is_logged_in():
                    return
                try:
                    status, data = self.api.report_launch(version)
                    if status == 200 and data.get("success"):
                        session["reported"] = True
                        server_total = data.get("total_launches")
                        self.stats.adopt_server_totals(server_total, None)
                        self.root.after(0, self._update_stats_label)
                        self.root.after(0, self._refresh_account_info_label)
                        return
                    if status in (401, 403):
                        return
                    print(f"Не удалось отправить запуск на сервер (попытка {attempt}): {status} {data}")
                except Exception as e:
                    print(f"Не удалось отправить запуск на сервер (попытка {attempt}): {e}")
                time.sleep(2 * attempt)

        t = threading.Thread(target=work, daemon=True)
        session["thread"] = t
        t.start()
        return session

    def _detect_server_from_log_line(self, line):
        if not self.discord.connected:
            return
        m = _SERVER_CONNECT_RE.search(line)
        if m:
            host = m.group(1).strip()
            port = m.group(2).strip()
            label = self._resolve_server_label(host, port)
            self.root.after(0, lambda: self.discord.set_server(label))
            return
        if _SERVER_DISCONNECT_HINT_RE.search(line):
            self.root.after(0, lambda: self.discord.set_server(None))

    def _detect_world_from_log_line(self, line):
        if not self.discord.connected:
            return

        m = _WORLD_LOAD_RE.search(line)
        if m:
            world_name = m.group(1).strip()
            self._current_world = world_name

            def resolve_and_set():
                game_type, hardcore = read_level_dat_gametype(self.core.minecraft_dir, world_name)
                if hardcore:
                    mode_label = "Хардкор"
                elif game_type is not None:
                    mode_label = _GAME_TYPE_NAMES.get(game_type)
                else:
                    mode_label = None
                self.root.after(0, lambda: self.discord.set_world(world_name, mode_label))

            threading.Thread(target=resolve_and_set, daemon=True).start()
            return

        gm = _GAMEMODE_CHANGE_RE.search(line)
        if gm and getattr(self, "_current_world", None):
            raw_mode = gm.group(1).strip().lower()
            mode_label = _GAME_TYPE_NAMES_EN.get(raw_mode, gm.group(1).strip().capitalize())
            world_name = self._current_world
            self.root.after(0, lambda: self.discord.set_world(world_name, mode_label))

    def _resolve_server_label(self, host, port):
        pinned = getattr(self, "_pinned_server", None)
        if pinned and pinned.get("address", "").split(":")[0].strip().lower() == host.lower():
            return pinned.get("name") or f"{host}:{port}"
        for s in self.config.get("servers", []):
            saved_host = s.get("address", "").split(":")[0].strip()
            if saved_host and saved_host.lower() == host.lower():
                return s.get("name", f"{host}:{port}")
        return f"{host}:{port}"

    def _start_log_readers(self, proc):
        self._log_tail.clear()
        self._log_readers = []

        def reader(stream):
            try:
                for line in iter(stream.readline, ""):
                    self._log_queue.put(line)
                    self._log_tail.append(line.rstrip("\n"))
                    self._detect_server_from_log_line(line)
                    self._detect_world_from_log_line(line)
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        for stream in (proc.stdout, proc.stderr):
            if stream:
                t = threading.Thread(target=reader, args=(stream,), daemon=True)
                t.start()
                self._log_readers.append(t)

    def show_log_window(self):
        if getattr(self, "log_window", None) and self.log_window.winfo_exists():
            self.log_window.lift()
            return
        self.log_window = tk.Toplevel(self.root)
        self.log_window.title("📋 Логи игры")
        self.log_window.geometry("650x420")
        self.log_window.configure(bg="#0d1117")
        self.log_text = scrolledtext.ScrolledText(
            self.log_window, bg="#0d1117", fg="#c9d1d9",
            font=("Consolas", 9), insertbackground="white"
        )
        self.log_text.pack(fill="both", expand=True)
        if self._log_poll_id:
            try:
                self.root.after_cancel(self._log_poll_id)
            except Exception:
                pass
        self.update_logs()

    def update_logs(self):
        self._log_poll_id = None
        if not getattr(self, "log_window", None) or not self.log_window.winfo_exists():
            return
        drained = False
        try:
            while True:
                self.log_text.insert(tk.END, self._log_queue.get_nowait())
                drained = True
        except queue.Empty:
            pass
        if drained:
            self.log_text.see(tk.END)
        running = self.process is not None and self.process.poll() is None
        reading = any(t.is_alive() for t in self._log_readers)
        if running or reading or not self._log_queue.empty():
            self._log_poll_id = self.root.after(150, self.update_logs)
        else:
            self.log_text.insert(tk.END, "\n--- Процесс завершён ---\n")
            self.log_text.see(tk.END)

    def _open_folder(self, path):
        os.makedirs(path, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть: {e}")

    def _open_file(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть: {e}")


def main():
    root = tk.Tk()
    root.withdraw()

    splash = SplashScreen(root)
    splash.update_status("Проверка зависимостей...", 10)
    splash.pump(0.5)
    splash.update_status("Загрузка конфигурации...", 30)
    splash.pump(0.5)
    splash.update_status("Инициализация лаунчера...", 60)
    splash.pump(0.4)
    splash.update_status("Подготовка интерфейса...", 80)
    splash.pump(0.3)
    splash.update_status("Готово!", 100)
    splash.pump(0.3)
    splash.close()

    root.deiconify()
    app = LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
