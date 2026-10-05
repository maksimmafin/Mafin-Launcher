import os
import sys
import shutil
import argparse
import subprocess

APP_NAME = "MafinLauncher"
ENTRY_POINT = "launcher.py"
INSTALLER_SCRIPT = "installer.iss"
DIST_DIR = "dist"
OUTPUT_DIR = "Output"
ACHIEVEMENT_SOUND = "achievement.wav"
OPUS_DLL = "opus.dll"


def ensure_pyinstaller():
    try:
        import PyInstaller
    except ImportError:
        print("PyInstaller не найден, устанавливаю...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller", "--quiet"])


RUNTIME_DEPENDENCIES = [
    ("requests", "requests"),
    ("minecraft_launcher_lib", "minecraft-launcher-lib"),
    ("PIL", "Pillow"),
    ("pypresence", "pypresence"),
]


def ensure_runtime_dependencies():
    for import_name, pip_name in RUNTIME_DEPENDENCIES:
        try:
            __import__(import_name)
        except ImportError:
            print(f"{pip_name} не найден, устанавливаю...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "--quiet"])
            except subprocess.CalledProcessError as e:
                if pip_name == "pypresence":
                    print(f"Не удалось поставить {pip_name}, exe соберётся без Discord RPC: {e}")
                else:
                    print(f"Не удалось поставить обязательную зависимость {pip_name}: {e}")
                    sys.exit(1)


def build_exe(icon_path=None, require_admin=True):
    if not os.path.exists(ENTRY_POINT):
        print(f"Не найден {ENTRY_POINT}. Положите build.py рядом с launcher.py.")
        sys.exit(1)
    if icon_path and not os.path.exists(icon_path):
        print(f"Указанный файл иконки не найден: {icon_path}")
        sys.exit(1)
    if icon_path and not icon_path.lower().endswith(".ico"):
        print(f"Предупреждение: PyInstaller и Inno Setup на Windows ожидают именно .ico "
              f"(указан {icon_path}) - если сборка иконки не подхватит, сконвертируйте "
              f"файл в .ico (например, через https://icoconvert.com).")
    if not os.path.exists(ACHIEVEMENT_SOUND):
        print(f"Предупреждение: не найден {ACHIEVEMENT_SOUND} рядом с build2.py - "
              f"exe соберётся, но звук ачивок работать не будет (тост всё равно покажется).")
    if not os.path.exists(OPUS_DLL):
        print(f"Предупреждение: не найден {OPUS_DLL} рядом с build2.py - "
              f"exe соберётся, но звонки будут недоступны (opuslib не найдёт libopus).")

    ensure_pyinstaller()
    ensure_runtime_dependencies()

    for folder in ("build", DIST_DIR):
        if os.path.isdir(folder):
            shutil.rmtree(folder)

    spec_file = f"{APP_NAME}.spec"
    if os.path.exists(spec_file):
        os.remove(spec_file)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
    ]
    if sys.platform.startswith("win") and icon_path:
        cmd += ["--icon", icon_path]
    if sys.platform.startswith("win") and require_admin:
        cmd.append("--uac-admin")

    if os.path.exists(ACHIEVEMENT_SOUND):
        cmd += ["--add-data", f"{ACHIEVEMENT_SOUND}{os.pathsep}."]

    if icon_path and os.path.basename(icon_path).lower() == "icon.ico":
        cmd += ["--add-data", f"{icon_path}{os.pathsep}."]

    hidden_imports = [
        "minecraft_launcher_lib",
        "minecraft_launcher_lib.forge",
        "minecraft_launcher_lib.fabric",
        "minecraft_launcher_lib.runtime",
        "requests",
        "PIL",
        "PIL.Image",
        "PIL.ImageTk",
        "pypresence",
    ]
    for h in hidden_imports:
        cmd += ["--hidden-import", h]

    cmd.append(ENTRY_POINT)

    print("=== Шаг 1/2: сборка launcher.py в exe (PyInstaller) ===")
    print(" ", " ".join(cmd))
    subprocess.check_call(cmd)

    built = os.path.join(DIST_DIR, f"{APP_NAME}.exe" if sys.platform.startswith("win") else APP_NAME)
    if not os.path.exists(built):
        print("PyInstaller завершился, но итоговый файл не найден:", built)
        sys.exit(1)
    print(f"Готово: {built}")

    if os.path.exists(OPUS_DLL):
        dest = os.path.join(DIST_DIR, OPUS_DLL)
        shutil.copy2(OPUS_DLL, dest)
        print(f"opus.dll скопирована рядом с exe: {dest}")

    return built


def find_iscc():
    from shutil import which
    found = which("ISCC") or which("ISCC.exe")
    if found:
        return found

    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    if sys.platform.startswith("win"):
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for subkey in (r"SOFTWARE\WOW6432Node\JR.Inno Setup", r"SOFTWARE\JR.Inno Setup"):
                    try:
                        with winreg.OpenKey(hive, subkey) as key:
                            path, _ = winreg.QueryValueEx(key, "InstallLocation")
                            candidate = os.path.join(path, "ISCC.exe")
                            if os.path.exists(candidate):
                                return candidate
                    except OSError:
                        continue
        except ImportError:
            pass
    return None


def build_installer(exe_path, icon_path=None):
    if not os.path.exists(INSTALLER_SCRIPT):
        print(f"Не найден {INSTALLER_SCRIPT} - пропускаю сборку установщика.")
        return

    iscc = find_iscc()
    if not iscc:
        print()
        print("=== Шаг 2/2: сборка установщика (Inno Setup) - ПРОПУЩЕНО ===")
        print("Не найден ISCC.exe (компилятор Inno Setup).")
        print("Установите Inno Setup 6: https://jrsoftware.org/isinfo.php")
        print(f"...и запустите build.py ещё раз, либо вручную скомпилируйте {INSTALLER_SCRIPT}.")
        return

    print()
    print("=== Шаг 2/2: сборка установщика (Inno Setup) ===")
    cmd = [iscc]
    if icon_path:
        abs_icon = os.path.abspath(icon_path)
        cmd.append(f"/DMyIconFile={abs_icon}")
    cmd.append(INSTALLER_SCRIPT)
    subprocess.check_call(cmd)
    print(f"Готово! Установщик собран в папке {OUTPUT_DIR}\\")


def main():
    parser = argparse.ArgumentParser(description="Билдер Mafin Launcher (exe + установщик)")
    parser.add_argument("--icon", help="Путь к .ico файлу для exe и установщика", default=None)
    parser.add_argument("--exe-only", action="store_true", help="Собрать только exe, без установщика")
    parser.add_argument("--no-admin", action="store_true",
                        help="Не запрашивать права администратора при запуске exe")
    args = parser.parse_args()

    exe_path = build_exe(icon_path=args.icon, require_admin=not args.no_admin)
    if not args.exe_only:
        build_installer(exe_path, icon_path=args.icon)
    else:
        print("--exe-only: сборка установщика пропущена по запросу.")


if __name__ == "__main__":
    main()
