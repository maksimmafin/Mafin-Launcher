import os
import sys
import shutil
import zipfile
import argparse
import subprocess
import urllib.request

APP_EXE = "MafinLauncher.exe"
INSTALLER_NAME = "MafinLauncherSetup"
LAUNCHER_SCRIPT = "launcher.py"
INSTALLER_SCRIPT = "installer.py"
OPUS_DLL = "opus.dll"
ACHIEVEMENT_SOUND = "achievement.wav"
PAYLOAD_DIR = "payload"
DIST_DIR = "dist"
JRE_URL = "https://api.adoptium.net/v3/binary/latest/%d/ga/windows/x64/jre/hotspot/normal/eclipse"
RUNTIME_DEPENDENCIES = [
    ("requests", "requests"),
    ("minecraft_launcher_lib", "minecraft-launcher-lib"),
    ("PIL", "Pillow"),
    ("pypresence", "pypresence"),
]


def pip_install(name):
    subprocess.check_call([sys.executable, "-m", "pip", "install", name, "--quiet"])


def ensure_deps():
    try:
        import PyInstaller
    except ImportError:
        print("PyInstaller не найден, устанавливаю...")
        pip_install("pyinstaller")
    for imp, pip in RUNTIME_DEPENDENCIES:
        try:
            __import__(imp)
        except ImportError:
            print(f"{pip} не найден, устанавливаю...")
            try:
                pip_install(pip)
            except subprocess.CalledProcessError:
                if pip != "pypresence":
                    sys.exit(f"Не удалось поставить {pip}")


def pyinstaller(args):
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed"] + args)


def build_launcher(icon):
    if not os.path.exists(LAUNCHER_SCRIPT):
        sys.exit(f"Не найден {LAUNCHER_SCRIPT} рядом с билдером.")
    cmd = ["--name", "MafinLauncher"]
    if icon:
        os.makedirs(PAYLOAD_DIR, exist_ok=True)
        shutil.copy2(icon, os.path.join(PAYLOAD_DIR, "icon.ico"))
        cmd += ["--icon", icon, "--add-data", f"{os.path.join(PAYLOAD_DIR, 'icon.ico')}{os.pathsep}."]
    cmd.append("--uac-admin")
    if os.path.exists(ACHIEVEMENT_SOUND):
        cmd += ["--add-data", f"{ACHIEVEMENT_SOUND}{os.pathsep}."]
    for h in ("minecraft_launcher_lib", "minecraft_launcher_lib.forge", "minecraft_launcher_lib.fabric",
              "minecraft_launcher_lib.runtime", "requests", "PIL", "PIL.Image", "PIL.ImageTk", "pypresence"):
        cmd += ["--hidden-import", h]
    cmd.append(LAUNCHER_SCRIPT)
    print("=== Шаг 1/3: лаунчер (PyInstaller) ===")
    pyinstaller(cmd)
    out = os.path.join(DIST_DIR, APP_EXE)
    if not os.path.exists(out):
        sys.exit("Лаунчер не собрался")
    return out


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def fetch(url, dest=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=120) as r:
        if dest is None:
            return r.read()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f)


def download_jre(major, cache, url=None):
    tmp = cache + ".dl"
    errors = []
    if url:
        try:
            fetch(url, tmp)
            os.replace(tmp, cache)
            return
        except Exception as e:
            errors.append(f"{url}: {e}")
    try:
        fetch(JRE_URL % major, tmp)
        os.replace(tmp, cache)
        return
    except Exception as e:
        errors.append(f"Adoptium API: {e}")
    try:
        import json
        api = f"https://api.github.com/repos/adoptium/temurin{major}-binaries/releases/latest"
        rel = json.loads(fetch(api).decode("utf-8"))
        for a in rel.get("assets", []):
            n = a["name"].lower()
            if n.endswith(".zip") and "jre" in n and "x64" in n and "windows" in n and "debugimage" not in n and "testimage" not in n:
                fetch(a["browser_download_url"], tmp)
                os.replace(tmp, cache)
                return
        errors.append("GitHub: нет подходящего архива JRE x64 windows")
    except Exception as e:
        errors.append(f"GitHub: {e}")
    sys.exit("Не удалось скачать Java автоматически:\n  " + "\n  ".join(errors) +
             f"\nСкачайте JRE {major} (Windows x64, .zip) на https://adoptium.net/temurin/releases/ и запустите:\n"
             "  python build_installer.py --icon icon.ico --skip-launcher --jre-zip путь\\к\\файлу.zip")


def prepare_jre(major, jre_zip, no_java, jre_url=None):
    target = os.path.join(PAYLOAD_DIR, "jre.zip")
    if no_java:
        if os.path.exists(target):
            os.remove(target)
        print("--no-java: Java в установщик не вшивается")
        return
    if jre_zip:
        shutil.copy2(jre_zip, target)
        return
    cache = os.path.join("build_cache", f"jre{major}.zip")
    if not os.path.exists(cache):
        os.makedirs("build_cache", exist_ok=True)
        print(f"Скачиваю Temurin JRE {major} (≈50 МБ)...")
        download_jre(major, cache, jre_url)
    with zipfile.ZipFile(cache) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
        tops = {n.split("/")[0] for n in zin.namelist() if n.strip("/")}
        prefix = (tops.pop() + "/") if len(tops) == 1 else ""
        for info in zin.infolist():
            name = info.filename[len(prefix):] if prefix and info.filename.startswith(prefix) else info.filename
            if not name or name.endswith("/"):
                continue
            zout.writestr(name, zin.read(info.filename))
    print("JRE подготовлена:", target)


def make_logo(icon):
    if not icon:
        return
    try:
        from PIL import Image
        im = Image.open(icon)
        try:
            im = im.ico.getimage(max(im.ico.sizes()))
        except Exception:
            pass
        im.convert("RGBA").resize((56, 56), Image.LANCZOS).save(os.path.join(PAYLOAD_DIR, "logo.png"))
    except Exception as e:
        print("Не удалось сделать logo.png:", e)


def build_uninstaller(icon):
    print("=== Деинсталлятор (маленький exe) ===")
    up = os.path.join("build_cache", "upayload")
    shutil.rmtree(up, ignore_errors=True)
    os.makedirs(up)
    for n in ("icon.ico", "logo.png"):
        if os.path.exists(os.path.join(PAYLOAD_DIR, n)):
            shutil.copy2(os.path.join(PAYLOAD_DIR, n), up)
    cmd = ["--name", "uninstall", "--exclude-module", "PIL", "--exclude-module", "numpy", "--uac-admin"]
    if icon:
        cmd += ["--icon", icon]
    cmd += ["--add-data", f"{up}{os.pathsep}{PAYLOAD_DIR}", INSTALLER_SCRIPT]
    pyinstaller(cmd)
    out = os.path.join(DIST_DIR, "uninstall.exe")
    shutil.copy2(out, os.path.join(PAYLOAD_DIR, "uninstall.exe"))
    print(f"Деинсталлятор: {os.path.getsize(out) // 1048576} МБ")


def build_installer(icon):
    if not os.path.exists(INSTALLER_SCRIPT):
        sys.exit(f"Не найден {INSTALLER_SCRIPT}")
    cmd = ["--name", INSTALLER_NAME, "--exclude-module", "PIL", "--exclude-module", "numpy", "--uac-admin"]
    if icon:
        cmd += ["--icon", icon]
    cmd += ["--add-data", f"{PAYLOAD_DIR}{os.pathsep}{PAYLOAD_DIR}", INSTALLER_SCRIPT]
    print("=== Шаг 3/3: установщик (PyInstaller) ===")
    pyinstaller(cmd)
    out = os.path.join(DIST_DIR, INSTALLER_NAME + ".exe")
    print("Готово:", out, f"({os.path.getsize(out) // 1048576} МБ)")


def main():
    ap = argparse.ArgumentParser(description="Билдер установщика Mafin Launcher (exe лаунчера + Java + setup.exe)")
    ap.add_argument("--icon", help="Путь к .ico", default=None)
    ap.add_argument("--java", type=int, default=25, help="Версия встроенной Java (по умолчанию 25)")
    ap.add_argument("--jre-zip", help="Готовый zip с JRE вместо скачивания", default=None)
    ap.add_argument("--jre-url", help="Прямая ссылка на zip с JRE (Windows x64)", default=None)
    ap.add_argument("--no-java", action="store_true", help="Не вшивать Java")
    ap.add_argument("--skip-launcher", action="store_true", help="Не пересобирать лаунчер, взять dist/MafinLauncher.exe")
    args = ap.parse_args()
    if args.icon and not os.path.exists(args.icon):
        sys.exit(f"Иконка не найдена: {args.icon}")
    ensure_deps()
    os.makedirs(PAYLOAD_DIR, exist_ok=True)
    if args.skip_launcher and os.path.exists(os.path.join(DIST_DIR, APP_EXE)):
        exe = os.path.join(DIST_DIR, APP_EXE)
    else:
        for f in ("build", DIST_DIR):
            shutil.rmtree(f, ignore_errors=True)
        exe = build_launcher(args.icon)
    shutil.copy2(exe, os.path.join(PAYLOAD_DIR, APP_EXE))
    if os.path.exists(OPUS_DLL):
        shutil.copy2(OPUS_DLL, os.path.join(PAYLOAD_DIR, OPUS_DLL))
    else:
        print(f"Предупреждение: нет {OPUS_DLL}, звонки работать не будут")
    if args.icon:
        shutil.copy2(args.icon, os.path.join(PAYLOAD_DIR, "icon.ico"))
    make_logo(args.icon)
    os.makedirs("build_cache", exist_ok=True)
    build_uninstaller(args.icon)
    print("=== Шаг 2/3: Java ===")
    prepare_jre(args.java, args.jre_zip, args.no_java, args.jre_url)
    build_installer(args.icon)


if __name__ == "__main__":
    main()
