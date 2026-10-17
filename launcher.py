#!/usr/bin/env python3
"""Единый запуск для Windows / Linux / macOS (вместо .bat и .sh).

  python launcher.py install              создать .venv и установить пакеты (один раз)
  python launcher.py install --system     запасной вариант без venv (pip --user --break-system-packages)
  python launcher.py browser                  НОВОЕ окно браузера с отладочным портом, отдельный профиль
                                              ./browser_profile (обычные вкладки не затрагиваются;
                                              войти через МЧД и поставить расширение ЭЦП нужно один раз)
  python launcher.py browser --default-profile   основной профиль (все окна браузера предварительно закрыть)
  python launcher.py check                проверка расчёта на протоколах
  python launcher.py selftest             связь с браузером и вход в кабинет
  python launcher.py dump <ИНН>           сохранить страницу дела для отладки
  python launcher.py inspect <имя>        снимок текущей вкладки в debug/
  python launcher.py run                  запуск бота по config.json и targets.csv
Любая другая команда передаётся как есть в `python -m rau_bot ...`.
"""
import os
import platform
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
PORT = 9222
URL = "https://rau.nalog.gov.ru"


def _venv_exe():
    return os.path.join(VENV, "Scripts", "python.exe") if platform.system() == "Windows" \
        else os.path.join(VENV, "bin", "python")


def venv_ok(py):
    """Настоящее ли это виртуальное окружение (а не огрызок после неудачного создания)."""
    try:
        out = subprocess.run([py, "-c", "import sys;print(sys.prefix != sys.base_prefix)"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() == "True"
    except (OSError, subprocess.SubprocessError):
        return False


def venv_python():
    """Python из рабочего .venv; иначе тот, которым запущен launcher."""
    exe = _venv_exe()
    return exe if os.path.exists(exe) and venv_ok(exe) else sys.executable


def install(system=False):
    """Создаёт .venv и ставит пакеты туда (на Debian/Ubuntu pip в системный Python не пускает)."""
    req = [sys.executable, "-m", "pip", "install"]
    if system:  # запасной вариант: в пользовательские пакеты системного Python
        code = subprocess.call(req + ["--user", "--break-system-packages", "-r", "requirements.txt"])
        print("Готово (системный режим)." if code == 0 else "Не удалось.")
        return code
    if venv_python() == sys.executable:      # рабочего окружения нет: пересоздаём с нуля
        shutil.rmtree(VENV, ignore_errors=True)
        print("Создаю виртуальное окружение .venv ...")
        ok = subprocess.call([sys.executable, "-m", "venv", VENV]) == 0 and venv_ok(_venv_exe())
        if ok:
            ok = subprocess.call([_venv_exe(), "-m", "pip", "--version"],
                                 stdout=subprocess.DEVNULL) == 0
        if not ok:
            shutil.rmtree(VENV, ignore_errors=True)
            print("\nНе получилось создать .venv (на Debian/Ubuntu нет пакета python3-venv).\n"
                  "Выполните:  sudo apt install python3-venv python3-full\n"
                  "и повторите:  python3 launcher.py install\n"
                  "Запасной вариант без venv:  python3 launcher.py install --system")
            return 1
    code = subprocess.call([venv_python(), "-m", "pip", "install", "-r", "requirements.txt"])
    if code == 0:
        print("Готово. Дальше: python3 launcher.py check  (и launcher.py browser, launcher.py run)")
    return code


def candidates():
    """Возможные пути браузера по ОС. Яндекс.Браузер первым: на нём заказчица работает."""
    system = platform.system()
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = [os.environ.get("ProgramFiles", r"C:\Program Files"),
              os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
        out = [os.path.join(local, r"Yandex\YandexBrowser\Application\browser.exe")]
        out += [os.path.join(p, r"Yandex\YandexBrowser\Application\browser.exe") for p in pf]
        out += [os.path.join(p, r"Google\Chrome\Application\chrome.exe") for p in pf]
        out += [os.path.join(p, r"Microsoft\Edge\Application\msedge.exe") for p in pf]
        return out
    if system == "Darwin":
        return ["/Applications/Yandex.app/Contents/MacOS/Yandex",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    names = ["yandex-browser", "yandex-browser-stable", "google-chrome", "google-chrome-stable",
             "chromium", "chromium-browser", "microsoft-edge"]
    return [shutil.which(n) for n in names] + ["/opt/yandex/browser/yandex-browser"]


def find_browser():
    for path in candidates():
        if path and os.path.exists(path):
            return path
    return None


def port_open(port=PORT, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def browser_args(exe, use_profile=True):
    """Аргументы запуска. Отдельный профиль = отдельный экземпляр браузера: новое окно,
    не связанное с вашими обычными вкладками, и отладочный порт гарантированно открывается."""
    args = [exe, f"--remote-debugging-port={PORT}", "--new-window", "--no-first-run",
            "--no-default-browser-check"]
    if use_profile:
        args.append(f"--user-data-dir={os.path.join(HERE, 'browser_profile')}")
    args.append(URL)
    return args


def start_browser(use_profile=True):
    if port_open():
        print(f"Отладочный порт {PORT} уже открыт: браузер запущен правильно.")
        return 0
    exe = os.environ.get("RAU_BROWSER") or find_browser()
    if not exe:
        print("Браузер не найден. Укажите путь в переменной RAU_BROWSER или установите Яндекс/Chrome.")
        return 1
    if use_profile:
        print("Открываю НОВОЕ окно браузера с отдельным профилем (ваши обычные вкладки не затрагиваются).")
    else:
        print("Режим основного профиля: сначала закройте ВСЕ окна браузера, иначе порт не откроется.")
    args = browser_args(exe, use_profile)
    kw = {}
    if platform.system() == "Windows":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
    for _ in range(30):
        if port_open():
            print("Браузер запущен. Войдите в кабинет РАУ через МЧД как обычно.")
            return 0
        time.sleep(0.5)
    print(f"Порт {PORT} не открылся. Если запускали с --default-profile: новые версии Chromium не "
          "разрешают отладочный порт\nв основном профиле; запустите без этого флага (отдельный профиль).")
    return 2


def main(argv):
    os.chdir(HERE)
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[1], argv[2:]
    if cmd == "install":
        return install(system="--system" in rest)
    if cmd == "browser":
        return start_browser(use_profile="--default-profile" not in rest)
    if cmd == "run":
        if venv_python() == sys.executable:
            print("(!) Нет рабочего .venv: сначала выполните  python3 launcher.py install")
        if not port_open():
            print(f"Браузер с отладочным портом {PORT} не запущен.\n"
                  "1) Закройте все окна браузера  2) python3 launcher.py browser\n"
                  "3) войдите в кабинет РАУ через МЧД  4) снова python3 launcher.py run")
            return 1
        return subprocess.call([venv_python(), "-m", "rau_bot", "run", "config.json", "targets.csv"])
    if venv_python() == sys.executable:
        print("(!) Нет рабочего .venv: сначала выполните  python3 launcher.py install")
    if cmd in ("selftest", "dump", "inspect") and not port_open():
        print(f"Браузер с отладочным портом {PORT} не запущен: сначала  python3 launcher.py browser")
        return 1
    return subprocess.call([venv_python(), "-m", "rau_bot", cmd, *rest])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
