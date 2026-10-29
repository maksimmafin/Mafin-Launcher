import os
import sys
import json
import base64
import shutil
import zipfile
import ctypes
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

APP_NAME = "Mafin Launcher"
APP_ID = "MafinLauncher"
APP_EXE = "MafinLauncher.exe"
APP_VERSION = "1.9.8"
PUBLISHER = "Mafin"
JAVA_MAJOR_MIN = 17
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s" % APP_ID
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

BG, BG2, LN, AC, TX, MU, FIELD = "#14181a", "#1a2022", "#33403d", "#43b581", "#e6e9ee", "#8f9b97", "#0f1314"
FONT = ("Segoe UI", 10)
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def payload(name):
    base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload"))
    p = os.path.join(base, "payload", name) if hasattr(sys, "_MEIPASS") else os.path.join(base, name)
    return p if os.path.exists(p) else None


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def default_dir(all_users):
    if all_users:
        return os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), APP_NAME)
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Programs", APP_NAME)


def java_major(exe):
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10, creationflags=NOWIN)
        out = (r.stderr or r.stdout).splitlines()[0]
        v = out.split('"')[1]
        parts = v.split(".")
        return int(parts[1]) if parts[0] == "1" else int(parts[0].split("-")[0].split("+")[0])
    except Exception:
        return None


def find_system_java():
    cands = []
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cands.append(os.path.join(jh, "bin", "java.exe"))
    w = shutil.which("java")
    if w:
        cands.append(w)
    try:
        import winreg
        for sub in (r"SOFTWARE\JavaSoft\JDK", r"SOFTWARE\JavaSoft\JRE", r"SOFTWARE\JavaSoft\Java Runtime Environment",
                    r"SOFTWARE\JavaSoft\Java Development Kit"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                    cur, _ = winreg.QueryValueEx(k, "CurrentVersion")
                    with winreg.OpenKey(k, cur) as kv:
                        home, _ = winreg.QueryValueEx(kv, "JavaHome")
                        cands.append(os.path.join(home, "bin", "java.exe"))
            except OSError:
                continue
    except ImportError:
        pass
    for c in cands:
        if os.path.isfile(c):
            m = java_major(c)
            if m:
                return c, m
    return None, None


def read_installed():
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, UNINSTALL_KEY) as k:
                    loc, _ = winreg.QueryValueEx(k, "InstallLocation")
                    return hive, loc
            except OSError:
                continue
    except ImportError:
        pass
    return None, None


def ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                       text=True, creationflags=NOWIN, timeout=60)
    return r.stdout.strip()


def special_dir(name):
    return ps("[Environment]::GetFolderPath('%s')" % name)


def make_shortcut(lnk, target, workdir, icon):
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    cmd = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');$s.TargetPath='%s';"
           "$s.WorkingDirectory='%s';$s.IconLocation='%s';$s.Save()") % (lnk, target, workdir, icon)
    ps(cmd)


def shortcut_paths(all_users):
    desk = special_dir("CommonDesktopDirectory" if all_users else "Desktop")
    prog = special_dir("CommonPrograms" if all_users else "Programs")
    return os.path.join(desk, APP_NAME + ".lnk"), os.path.join(prog, APP_NAME + ".lnk")


def data_home(all_users):
    d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MafinLauncher")
    return d


def validate_dir(path):
    path = os.path.abspath(os.path.expandvars(path.strip().strip('"')))
    if not path or os.path.dirname(path) == path:
        path = os.path.join(path, APP_NAME)
    drive = os.path.splitdrive(path)[0]
    if drive and not os.path.isdir(drive + os.sep):
        return None, "Диск %s не найден." % drive
    need = 200 * 1048576
    for n in (APP_EXE, "jre.zip"):
        p = payload(n)
        if p:
            need += os.path.getsize(p) * (3 if n == "jre.zip" else 1)
    try:
        os.makedirs(path, exist_ok=True)
        test = os.path.join(path, ".write_test")
        with open(test, "w") as f:
            f.write("1")
        os.remove(test)
    except Exception as e:
        return None, "В эту папку нельзя записывать: %s" % e
    try:
        free = shutil.disk_usage(path).free
    except Exception:
        free = need
    if free < need:
        return None, "Мало места на диске: нужно около %d МБ, свободно %d МБ." % (need // 1048576, free // 1048576)
    return path, ""


def do_install(opts, progress, log):
    import winreg
    d = opts["dir"]
    all_users = opts["all_users"]
    os.makedirs(d, exist_ok=True)
    steps = []
    exe_src = payload(APP_EXE)
    if not exe_src:
        raise RuntimeError("В установщике нет %s (неверная сборка)" % APP_EXE)
    opus = payload("opus.dll")
    jre = payload("jre.zip") if opts["java"] else None
    total = 1 + (1 if opus else 0) + 1
    zf = zipfile.ZipFile(jre) if jre else None
    names = zf.namelist() if zf else []
    total += len(names)
    done = [0]

    def tick(msg=None):
        done[0] += 1
        progress(done[0] / total * 100)
        if msg:
            log(msg)

    dst = os.path.join(d, APP_EXE)
    try:
        shutil.copy2(exe_src, dst)
    except PermissionError:
        raise RuntimeError("Не удалось записать %s. Закройте лаунчер и повторите." % dst)
    tick("Mafin Launcher.exe")
    if opus:
        shutil.copy2(opus, os.path.join(d, "opus.dll"))
        tick("opus.dll")
    if zf:
        jdir = os.path.join(d, "java")
        if os.path.isdir(jdir):
            shutil.rmtree(jdir, ignore_errors=True)
        root = os.path.realpath(d)
        for i, n in enumerate(names):
            target = os.path.realpath(os.path.join(d, "java", n))
            if not target.startswith(root):
                continue
            zf.extract(n, jdir)
            if i % 200 == 0:
                log("Java: %d / %d" % (i, len(names)))
            tick()
        log("Встроенная Java установлена")
    me = payload("uninstall.exe") or (sys.executable if getattr(sys, "frozen", False) else None)
    unin = os.path.join(d, "uninstall.exe")
    if me:
        shutil.copy2(me, unin)
    if is_admin():
        try:
            subprocess.run(["icacls", d, "/grant", "*S-1-5-32-545:(OI)(CI)M"], capture_output=True, creationflags=NOWIN, timeout=60)
        except Exception:
            pass
    tick("Деинсталлятор")
    home = data_home(all_users)
    os.makedirs(home, exist_ok=True)
    workdir = d if not all_users else home
    desk, prog = shortcut_paths(all_users)
    if opts["desktop"]:
        make_shortcut(desk, dst, workdir, dst)
        log("Ярлык на рабочем столе")
    if opts["menu"]:
        make_shortcut(prog, dst, workdir, dst)
        log("Ярлык в меню Пуск")
    hive = winreg.HKEY_LOCAL_MACHINE if all_users else winreg.HKEY_CURRENT_USER
    with winreg.CreateKey(hive, UNINSTALL_KEY) as k:
        for n, v in (("DisplayName", APP_NAME), ("DisplayVersion", APP_VERSION), ("Publisher", PUBLISHER),
                     ("InstallLocation", d), ("DisplayIcon", dst),
                     ("UninstallString", '"%s" --uninstall' % (unin if me else dst))):
            winreg.SetValueEx(k, n, 0, winreg.REG_SZ, v)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(d) for f in fs) // 1024
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, int(size))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if opts["autostart"]:
            winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, '"%s" --autostart' % dst)
            log("Автозапуск включён")
        else:
            try:
                winreg.DeleteValue(k, APP_ID)
            except OSError:
                pass
    progress(100)
    return dst, workdir


def do_uninstall(delete_data=False):
    import winreg
    hive, loc = read_installed()
    if not loc:
        return False
    all_users = hive == winreg.HKEY_LOCAL_MACHINE
    for lnk in shortcut_paths(all_users):
        try:
            os.remove(lnk)
        except OSError:
            pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_ID)
    except OSError:
        pass
    try:
        subprocess.run(["schtasks", "/delete", "/tn", APP_ID, "/f"], capture_output=True, creationflags=NOWIN)
    except Exception:
        pass
    subprocess.run(["taskkill", "/f", "/im", APP_EXE], capture_output=True, creationflags=NOWIN)
    try:
        winreg.DeleteKey(hive, UNINSTALL_KEY)
    except OSError:
        pass
    for n in (APP_EXE, "opus.dll"):
        try:
            os.remove(os.path.join(loc, n))
        except OSError:
            pass
    shutil.rmtree(os.path.join(loc, "java"), ignore_errors=True)
    if delete_data:
        for n in (".minecraft_mafinlauncher",):
            shutil.rmtree(os.path.join(loc, n), ignore_errors=True)
        try:
            os.remove(os.path.join(loc, "launcher_config.json"))
        except OSError:
            pass
        shutil.rmtree(data_home(False), ignore_errors=True)
    me = os.path.abspath(sys.executable) if getattr(sys, "frozen", False) else ""
    if me and os.path.dirname(me).lower() == os.path.abspath(loc).lower():
        cmd = 'ping 127.0.0.1 -n 3 >nul & del /f /q "%s" & rmdir "%s"' % (me, loc)
        subprocess.Popen(["cmd", "/c", cmd], creationflags=NOWIN)
    else:
        try:
            os.remove(os.path.join(loc, "uninstall.exe"))
        except OSError:
            pass
        try:
            os.rmdir(loc)
        except OSError:
            pass
    return True


class Btn(tk.Label):
    def __init__(self, parent, text, cmd, primary=False):
        bg = AC if primary else "#232d2f"
        fg = "#0b1a12" if primary else TX
        super().__init__(parent, text=text, bg=bg, fg=fg, font=("Segoe UI Semibold", 10), padx=18, pady=6,
                         highlightthickness=2, highlightbackground="#000", cursor="hand2")
        self._cmd, self._bg, self._on = cmd, bg, True
        self.bind("<Button-1>", lambda e: self._cmd() if self._on else None)

    def enable(self, on):
        self._on = on
        self.config(fg=(self["fg"] if on else MU), cursor="hand2" if on else "arrow")


class Check(tk.Frame):
    def __init__(self, parent, text, var):
        super().__init__(parent, bg=BG)
        self.var = var
        self.box = tk.Label(self, width=2, bg=FIELD, highlightthickness=2, highlightbackground="#000")
        self.box.pack(side="left")
        self.lbl = tk.Label(self, text=text, bg=BG, fg=TX, font=FONT, anchor="w")
        self.lbl.pack(side="left", padx=8)
        for w in (self.box, self.lbl):
            w.bind("<Button-1>", lambda e: self.toggle())
        var.trace_add("write", lambda *a: self.paint())
        self.paint()

    def toggle(self):
        self.var.set(not self.var.get())

    def paint(self):
        self.box.config(bg=AC if self.var.get() else FIELD)


class App(tk.Tk):
    STEPS = ["Приветствие", "Параметры", "Установка", "Готово"]

    def __init__(self, resume=None, uninstall=False):
        super().__init__()
        self.title("Установка %s" % APP_NAME if not uninstall else "Удаление %s" % APP_NAME)
        self.configure(bg="#000")
        w, h = 600, 400
        self.geometry("%dx%d+%d+%d" % (w, h, (self.winfo_screenwidth() - w) // 2, (self.winfo_screenheight() - h) // 2))
        self.resizable(False, False)
        ico = payload("icon.ico")
        if ico:
            try:
                self.iconbitmap(ico)
            except tk.TclError:
                pass
        self.uninstall_mode = uninstall
        self.step = 0
        self.v_dir = tk.StringVar(value=default_dir(False))
        self.v_desktop, self.v_menu = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.v_auto = tk.BooleanVar(value=True)
        self.v_java = tk.BooleanVar(value=True)
        self.v_run = tk.BooleanVar(value=True)
        self.hive, self.loc = read_installed()
        self.v_deldata = tk.BooleanVar(value=False)
        if self.loc and not uninstall and not os.path.exists(os.path.join(self.loc, APP_EXE)):
            self.loc = None
        self.java_path, self.java_ver = (None, None)
        self.has_jre = bool(payload("jre.zip"))
        self.result = None
        self.err = None
        outer = tk.Frame(self, bg=BG, highlightthickness=1, highlightbackground=LN)
        outer.pack(fill="both", expand=True, padx=2, pady=2)
        self.side = tk.Frame(outer, bg=BG2, width=160)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        tk.Frame(outer, bg=LN, width=1).pack(side="left", fill="y")
        right = tk.Frame(outer, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        self.foot = tk.Frame(right, bg=BG, height=54, highlightthickness=0)
        self.foot.pack(side="bottom", fill="x")
        tk.Frame(right, bg=LN, height=1).pack(side="bottom", fill="x")
        self.body = tk.Frame(right, bg=BG)
        self.body.pack(fill="both", expand=True, padx=24, pady=20)
        self.logo_img = None
        png = payload("logo.png")
        if png:
            try:
                self.logo_img = tk.PhotoImage(file=png)
            except tk.TclError:
                self.logo_img = None
        if self.logo_img:
            tk.Label(self.side, image=self.logo_img, bg=BG2).pack(anchor="w", padx=14, pady=(18, 14))
        else:
            tk.Frame(self.side, bg=AC, width=56, height=56, highlightthickness=2, highlightbackground="#000").pack(anchor="w", padx=14, pady=(18, 14))
        self.step_lbls = []
        for s in self.STEPS:
            l = tk.Label(self.side, text=s, bg=BG2, fg=MU, font=FONT, anchor="w", padx=10, pady=5)
            l.pack(fill="x", padx=(8, 0))
            self.step_lbls.append(l)
        tk.Label(self.side, text="v%s" % APP_VERSION, bg=BG2, fg=MU, font=("Segoe UI", 8)).pack(side="bottom", anchor="w", padx=14, pady=10)
        threading.Thread(target=self._detect_java, daemon=True).start()
        if resume:
            self.apply_opts(resume)
            self.go_install()
        elif uninstall:
            self.page_uninstall()
        else:
            self.page_welcome()

    def _detect_java(self):
        self.java_path, self.java_ver = find_system_java()
        self.java_checked = True


    def apply_opts(self, o):
        self.v_dir.set(o["dir"])
        self.v_desktop.set(o["desktop"])
        self.v_menu.set(o["menu"])
        self.v_auto.set(o["autostart"])
        self.v_java.set(o["java"])

    def opts(self):
        return {"all_users": False, "dir": os.path.normpath(self.v_dir.get().strip()),
                "desktop": self.v_desktop.get(), "menu": self.v_menu.get(), "autostart": self.v_auto.get(),
                "java": self.v_java.get() and self.has_jre}

    def clear(self):
        for c in self.body.winfo_children():
            c.destroy()
        for c in self.foot.winfo_children():
            c.destroy()

    def mark(self, idx):
        self.step = idx
        for i, l in enumerate(self.step_lbls):
            l.config(fg=AC if i < idx else (TX if i == idx else MU), bg="#202a2c" if i == idx else BG2)

    def h1(self, t):
        tk.Label(self.body, text=t, bg=BG, fg=TX, font=("Segoe UI Semibold", 15), anchor="w").pack(fill="x")

    def p(self, t, fg=MU, **kw):
        l = tk.Label(self.body, text=t, bg=BG, fg=fg, font=kw.pop("font", FONT), anchor="w", justify="left", wraplength=380, **kw)
        l.pack(fill="x", pady=(8, 0))
        return l

    def buttons(self, *btns):
        for b in reversed(btns):
            b.pack(side="right", padx=(8, 16), pady=11)

    def page_welcome(self):
        self.clear()
        self.mark(0)
        self.h1("Установка %s" % APP_NAME)
        self.p("Лаунчер Minecraft с друзьями, чатом, скинами и привилегиями. Программа установит лаунчер на этот компьютер.")
        if self.loc:
            self.p("Лаунчер уже установлен в папке %s." % self.loc, fg=TX)
            self.buttons(Btn(self.foot, "Отмена", self.destroy), Btn(self.foot, "Удалить", self.page_uninstall, True))
        else:
            self.buttons(Btn(self.foot, "Отмена", self.destroy), Btn(self.foot, "Далее", self.page_options, True))

    def page_options(self):
        self.clear()
        self.mark(1)
        self.h1("Параметры")
        tk.Label(self.body, text="ПАПКА УСТАНОВКИ", bg=BG, fg=MU, font=("Segoe UI", 8)).pack(anchor="w", pady=(12, 2))
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x")
        tk.Entry(row, textvariable=self.v_dir, bg=FIELD, fg=TX, insertbackground=TX, relief="flat", font=FONT,
                 highlightthickness=1, highlightbackground=LN, highlightcolor=AC).pack(side="left", fill="x", expand=True, ipady=4)
        Btn(row, "Обзор", self.browse).pack(side="left", padx=(8, 0))
        Check(self.body, "Ярлык на рабочем столе", self.v_desktop).pack(anchor="w", pady=(8, 0))
        Check(self.body, "Ярлык в меню «Пуск»", self.v_menu).pack(anchor="w")
        Check(self.body, "Запускать при входе в Windows", self.v_auto).pack(anchor="w")
        if self.has_jre:
            Check(self.body, "Установить встроенную Java", self.v_java).pack(anchor="w")
        self.jl = tk.Label(self.body, text="Проверка Java…", bg=BG, fg=MU, font=("Segoe UI", 9), anchor="w", wraplength=380, justify="left")
        self.jl.pack(fill="x", pady=(4, 0))
        self.after(200, self.paint_java)
        self.buttons(Btn(self.foot, "Назад", self.page_welcome), Btn(self.foot, "Установить", self.go_install, True))

    def paint_java(self):
        if not self.jl.winfo_exists():
            return
        if not self.java_checked:
            self.after(300, self.paint_java)
            return
        if self.java_ver:
            ok = self.java_ver >= JAVA_MAJOR_MIN
            if ok and not self.java_auto:
                self.java_auto = True
                self.v_java.set(False)
            self.jl.config(text="Java %d найдена в системе%s" % (self.java_ver, "" if ok else " (старая, нужна %d+)" % JAVA_MAJOR_MIN))
        else:
            self.jl.config(text="Java в системе не найдена" + (": будет установлена встроенная" if self.has_jre else ""))

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.v_dir.get(), parent=self)
        if d:
            self.v_dir.set(os.path.join(os.path.normpath(d), APP_NAME) if not os.path.basename(d).lower().startswith("mafin") else os.path.normpath(d))

    def go_install(self):
        path, err = validate_dir(self.v_dir.get())
        if not path:
            messagebox.showwarning(APP_NAME, err, parent=self)
            return
        self.v_dir.set(path)
        o = self.opts()
        if o["all_users"] and not is_admin():
            blob = base64.b64encode(json.dumps(o).encode()).decode()
            exe = sys.executable
            params = ('--resume %s' % blob) if getattr(sys, "frozen", False) else '"%s" --resume %s' % (os.path.abspath(__file__), blob)
            r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
            if r > 32:
                self.destroy()
            else:
                messagebox.showwarning(APP_NAME, "Права администратора не получены. Выберите режим «Только для меня».", parent=self)
            return
        self.clear()
        self.mark(2)
        self.h1("Установка")
        self.pl = self.p("Копирование файлов…")
        self.bar = tk.Canvas(self.body, height=18, bg=FIELD, highlightthickness=2, highlightbackground="#000")
        self.bar.pack(fill="x", pady=(10, 0))
        self.logbox = tk.Text(self.body, height=7, bg=FIELD, fg=MU, relief="flat", font=("Consolas", 9), highlightthickness=1,
                              highlightbackground=LN, state="disabled")
        self.logbox.pack(fill="both", expand=True, pady=(10, 0))
        self.cancel_btn = Btn(self.foot, "Отмена", self.destroy)
        self.buttons(self.cancel_btn)
        self.pct = 0
        threading.Thread(target=self._run_install, args=(o,), daemon=True).start()
        self.after(100, self._poll)

    def _run_install(self, o):
        try:
            self.result = do_install(o, lambda v: setattr(self, "pct", v), lambda m: self.after(0, self.addlog, m))
        except Exception as e:
            self.err = str(e)
        self.finished = True

    finished = False
    java_checked = False
    java_auto = False

    def addlog(self, m):
        if not self.logbox.winfo_exists():
            return
        self.logbox.config(state="normal")
        self.logbox.insert("end", "OK  " + m + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def _poll(self):
        w = self.bar.winfo_width()
        self.bar.delete("all")
        n = int(w * self.pct / 100 / 12)
        for i in range(n):
            self.bar.create_rectangle(i * 12, 0, i * 12 + 10, 20, fill=AC, outline="")
        if self.finished:
            if self.err:
                messagebox.showerror(APP_NAME, self.err, parent=self)
                self.page_options()
            else:
                self.page_done()
            return
        self.after(80, self._poll)

    def page_done(self):
        self.clear()
        self.mark(3)
        self.h1("Установка завершена")
        self.p("Лаунчер установлен в папку %s." % self.v_dir.get())
        Check(self.body, "Запустить лаунчер", self.v_run).pack(anchor="w", pady=(14, 0))
        self.p("Удалить программу можно в «Параметры → Приложения».", font=("Segoe UI", 9))
        self.buttons(Btn(self.foot, "Готово", self.finish, True))

    def finish(self):
        if self.v_run.get() and self.result:
            exe, work = self.result
            try:
                subprocess.Popen(["explorer.exe", exe])
            except Exception:
                try:
                    subprocess.Popen([exe], cwd=work if os.path.isdir(work) else os.path.dirname(exe))
                except Exception:
                    pass
        self.destroy()

    def page_uninstall(self):
        self.clear()
        self.mark(0)
        self.h1("Удаление %s" % APP_NAME)
        if not self.loc:
            self.p("Установленная копия не найдена.")
            self.buttons(Btn(self.foot, "Закрыть", self.destroy, True))
            return
        self.p("Будет удалён лаунчер из папки %s вместе с встроенной Java, ярлыками и автозапуском." % self.loc, fg=TX)
        Check(self.body, "Удалить папку игры Minecraft и настройки", self.v_deldata).pack(anchor="w", pady=(14, 0))
        self.p("Если не отмечено, миры, моды и настройки сохранятся на диске.", font=("Segoe UI", 9))
        self.buttons(Btn(self.foot, "Отмена", self.destroy), Btn(self.foot, "Удалить", self.run_uninstall, True))

    def run_uninstall(self):
        try:
            if self.hive is not None and self.hive != 0:
                import winreg
                if self.hive == winreg.HKEY_LOCAL_MACHINE and not is_admin():
                    exe = sys.executable
                    params = "--uninstall" if getattr(sys, "frozen", False) else '"%s" --uninstall' % os.path.abspath(__file__)
                    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
                    self.destroy()
                    return
            do_uninstall(self.v_deldata.get())
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
            return
        self.clear()
        self.mark(3)
        self.h1("Лаунчер удалён")
        self.buttons(Btn(self.foot, "Закрыть", self.destroy, True))


def main():
    resume = None
    if "--resume" in sys.argv:
        try:
            resume = json.loads(base64.b64decode(sys.argv[sys.argv.index("--resume") + 1]).decode())
        except Exception:
            resume = None
    uninstall = "--uninstall" in sys.argv or os.path.basename(sys.executable).lower().startswith("uninstall")
    App(resume=resume, uninstall=uninstall).mainloop()


if __name__ == "__main__":
    main()
