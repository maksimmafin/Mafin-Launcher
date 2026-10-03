# ⚡ Mafin Launcher

Современный лаунчер для Minecraft на Python (tkinter) с оформлением в стиле Windows 11: **Mica**, **Mica Alt** и **Acrylic**.

*A modern Minecraft launcher written in Python (tkinter) with Windows 11 Mica / Mica Alt / Acrylic themes.*

## ✨ Возможности

- 🎮 Установка и запуск Vanilla, Forge и Fabric
- 👤 Профили, скины, моды, ресурспаки, менеджер версий
- 🌐 Серверы, P2P, скриншоты, логи
- 🏆 Ачивки, друзья, подарки, задания
- 🎨 Темы: материал окна (Mica / Mica Alt / Acrylic) + 5 акцентных цветов
- 🧭 Боковое меню в трёх стилях: список, пилюли, компактное
- ⌨️ Палитра команд (`Ctrl+K`), сворачивание панели (`Ctrl+B`), запуск (`Ctrl+L`), установка (`Ctrl+I`)
- 💬 Discord Rich Presence

## 📸 Скриншоты

<p align="center">
<img src="screenshots/mica.png" width="48%">
  <img src="screenshots/mica2.png" width="48%">
  <img src="screenshots/acrylic.png" width="48%">
<img src="screenshots/acrylic2.png" width="48%">
</p>


## 🗂 Состав репозитория

| Файл | Что это |
|------|---------|
| `launcher.py` | Сам лаунчер (клиент) |
| `app.py` | Сервер: аккаунты, ачивки, друзья, подарки, новости, обновления |
| `build2.py` | Билдер: собирает `MafinLauncher.exe` и установщик |
| `installer.iss` | Скрипт установщика для Inno Setup |
| `achievement.wav` | Звук ачивок (зашивается в exe) |

## 🚀 Запуск лаунчера из исходников

Нужны Python 3.12+ (с tcl/tk) и Java.

```bash
python -m pip install requests minecraft-launcher-lib pillow pypresence setuptools
python launcher.py
```

## 🔨 Сборка (build2.py)

Билдер собирает всё за один запуск: ставит недостающие зависимости, собирает `MafinLauncher.exe` через PyInstaller и, если установлен [Inno Setup 6](https://jrsoftware.org/isinfo.php), компилирует `installer.iss` в готовый установщик `MafinLauncherSetup.exe`. **Собирать нужно на Windows.**

```bash
python build2.py                          # exe + установщик
python build2.py --icon myicon.ico        # со своей иконкой (.ico)
python build2.py --exe-only               # только exe, без установщика
python build2.py --no-admin               # exe не будет запрашивать права администратора
```

Что нужно положить рядом с `build2.py`:
- `launcher.py` и `installer.iss` (обязательно);
- `achievement.wav` — звук ачивок, зашивается в exe;
- `opus.dll` — если нужна, копируется в `dist\` рядом с exe.

Результат: `dist\MafinLauncher.exe` и установщик в папке `Output\`. По умолчанию exe запрашивает права администратора (флаг `--no-admin` отключает это).

## 🖥 Сервер (app.py)

Бэкенд на **Flask + SQLite**: регистрация и вход, профили, достижения и топ, друзья и сообщения, подарки, задания, новости, раздача обновлений лаунчера и веб-админка (`/admin`).

```bash
python -m pip install flask
python app.py
```

Сервер слушает порт `10074` (`0.0.0.0`). База `mafin_launcher.db` создаётся рядом при первом запуске, там же появляются папки `updates/` и `gift_images/`.

Настройки задаются переменными окружения:

| Переменная | Назначение |
|------------|------------|
| `MAFIN_SECRET_KEY` | Ключ Flask-сессий веб-админки (если не задан, генерируется случайный при каждом запуске) |
| `MAFIN_ADMIN_PASSWORD` | Пароль для создания первого админа через веб-форму |
| `MAFIN_DEFAULT_ADMIN_NICKNAME` | Ник админа, который создаётся при первом запуске |
| `MAFIN_DEFAULT_ADMIN_PASSWORD` | Пароль этого админа (смените после первого входа) |
| `MAFIN_DB_PATH` | Путь к файлу базы данных |

> ⚠️ На рабочем сервере обязательно задайте свои значения переменных и поставьте сервер за HTTPS (например, через nginx или Caddy).

Адрес сервера для лаунчера задаётся в `launcher.py` (константа `SERVER_URL`).

## 🪟 Про темы

Mica Alt работает на Windows 11 22H2 и новее, Mica — на Windows 11, Acrylic — на Windows 10 1803+. Если эффект не поддерживается, лаунчер автоматически переключается на классический фон.

## 📄 Лицензия

MIT — см. файл [LICENSE](LICENSE).
