#!/usr/bin/env python3
"""Панель управления ботом: открывается в браузере, большие кнопки, без командной строки.

Запуск:  python launcher.py          (или двойной клик по ЗАПУСК.bat / ./start.sh)
Работает только на этом компьютере (127.0.0.1) и требует секретный ключ в адресе.
"""
import collections
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import launcher

HERE = launcher.HERE
HOST, PORT = "127.0.0.1", int(os.environ.get("RAU_PANEL_PORT", "8765"))
TOKEN = secrets.token_urlsafe(16)
CONFIG, TARGETS = os.path.join(HERE, "config.json"), os.path.join(HERE, "targets.csv")
RUN_CONFIG = os.path.join(HERE, "config.run.json")


def ensure_files():
    for target, example in ((CONFIG, "config.example.json"), (TARGETS, "targets.example.csv")):
        if os.path.exists(target):
            continue
        src = os.path.join(HERE, example)
        if os.path.exists(src):
            shutil.copy(src, target)
        elif target == TARGETS:         # шаблона нет в архиве: создаём пустой список с заголовком
            open(target, "w", encoding="utf-8").write("inn;max_bid;initial;step;au1;au2;au_cap\n")


class Runner:
    """Запускает команды launcher.py как отдельные процессы и собирает их вывод."""

    def __init__(self):
        self.lines = collections.deque(maxlen=3000)
        self.total = 0
        self.proc = None
        self.mode = ""
        self.lock = threading.Lock()

    def _push(self, text):
        with self.lock:
            for line in text.splitlines():
                self.lines.append(line)
                self.total += 1

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, args, mode=""):
        """args: аргументы после python. Бота запускаем напрямую (-m rau_bot), без промежуточного
        launcher, чтобы «Стоп» завершал именно бота."""
        if self.running:
            return False, "Бот уже работает. Сначала остановите."
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.mode = mode
        self._push(f"--- {' '.join(os.path.basename(a) for a in args)} ---")
        self.proc = subprocess.Popen([launcher.venv_python(), *args],
                                     cwd=HERE, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        return True, "запущено"

    def _reader(self, proc):
        for line in proc.stdout:
            self._push(line.rstrip("\n"))
        self._push(f"--- завершено (код {proc.wait()}) ---")

    def stop(self):
        if self.running:
            self.proc.terminate()
            self._push("--- остановлено вручную ---")
            return True
        return False

    def since(self, n):
        with self.lock:
            skip = max(0, n - (self.total - len(self.lines)))
            return self.total, list(self.lines)[skip:]


runner = Runner()


def make_run_config(live: bool) -> str:
    cfg = json.load(open(CONFIG, encoding="utf8"))
    cfg["dry_run"] = not live
    json.dump(cfg, open(RUN_CONFIG, "w", encoding="utf8"), ensure_ascii=False, indent=2)
    return RUN_CONFIG


def journal_tail(n=40):
    try:
        cfg = json.load(open(CONFIG, encoding="utf8"))
        path = os.path.join(HERE, cfg.get("journal", "journal.csv"))
        return "".join(open(path, encoding="utf-8-sig").readlines()[-n:]) or "(журнал пуст)"
    except OSError:
        return "(журнала ещё нет)"


def do_action(name):
    if name == "browser":
        return runner.start([os.path.join(HERE, "launcher.py"), "browser"])
    if name in ("selftest", "check"):
        return runner.start(["-m", "rau_bot", name])
    if name in ("run_dry", "run_live"):
        ok, msg = False, ""
        try:
            path = make_run_config(live=(name == "run_live"))
        except (OSError, ValueError) as e:
            return False, f"Ошибка в config.json: {e}"
        ok, msg = runner.start(["-u", "-m", "rau_bot", "run", path, TARGETS],
                               mode="БОЕВОЙ" if name == "run_live" else "ТРЕНИРОВКА")
        return ok, msg
    if name == "stop":
        ok = runner.stop()
        return ok, "остановлено" if ok else "ничего не запущено"
    return False, "неизвестная команда"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok_host(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _auth(self):
        q = parse_qs(urlparse(self.path).query)
        return self._ok_host() and (q.get("k") or [""])[0] == TOKEN

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        if not self._auth():
            return self._send(403, "Нет доступа: откройте панель через launcher.py", "text/plain; charset=utf-8")
        if u.path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._json({"browser": launcher.port_open(), "running": runner.running,
                               "mode": runner.mode if runner.running else ""})
        if u.path == "/api/log":
            n = int((parse_qs(u.query).get("since") or ["0"])[0])
            total, lines = runner.since(n)
            return self._json({"total": total, "lines": lines})
        if u.path == "/api/targets":
            return self._json({"text": open(TARGETS, encoding="utf-8-sig").read()})
        if u.path == "/api/journal":
            return self._json({"text": journal_tail()})
        if u.path == "/summary.xlsx":
            from rau_bot.summary import build_xlsx
            folder = os.path.join(HERE, "protocols")
            os.makedirs(folder, exist_ok=True)
            data, _ = build_xlsx(folder)
            return self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if u.path == "/journal.xlsx":
            from rau_bot.export import journal_to_xlsx
            try:
                cfg = json.load(open(CONFIG, encoding="utf8"))
                data = journal_to_xlsx(os.path.join(HERE, cfg.get("journal", "journal.csv")))
            except OSError:
                return self._send(404, "журнала ещё нет", "text/plain; charset=utf-8")
            return self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if not self._auth():
            return self._send(403, "forbidden", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json({"ok": False, "msg": "плохой запрос"}, 400)
        if u.path == "/api/action":
            ok, msg = do_action(body.get("name", ""))
            return self._json({"ok": ok, "msg": msg})
        if u.path == "/api/targets":
            open(TARGETS, "w", encoding="utf-8", newline="\n").write(body.get("text", ""))
            return self._json({"ok": True, "msg": "список сохранён"})
        self._send(404, "not found", "text/plain")


PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Бот ставок РАУ</title><style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mut:#6b7686;--line:#dfe3e8;--acc:#1f5eff;--ok:#1a8f4c;--bad:#c93232;--warn:#b86a00}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2229;--ink:#e8ecf1;--mut:#97a2b1;--line:#2d343d;--acc:#6b9bff;--ok:#4cc27f;--bad:#ff7a7a;--warn:#f0a040}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:920px;margin:0 auto;padding:20px 16px 40px}h1{font-size:22px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 10px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:10px;padding:12px 18px;cursor:pointer;min-height:48px}
button:hover{border-color:var(--acc)}button.p{background:var(--acc);border-color:var(--acc);color:#fff}
button.live{background:var(--bad);border-color:var(--bad);color:#fff}button:disabled{opacity:.45;cursor:not-allowed}
.pill{display:inline-flex;gap:8px;align-items:center;padding:6px 12px;border-radius:99px;border:1px solid var(--line);font-size:14px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}.on .dot{background:var(--ok)}.warn .dot{background:var(--warn)}.bad .dot{background:var(--bad)}
pre,textarea{width:100%;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:10px;font:13px/1.45 ui-monospace,Consolas,monospace}
pre{height:260px;overflow:auto;margin:0;white-space:pre-wrap}textarea{height:150px;resize:vertical}
.hint{color:var(--mut);font-size:14px;margin:6px 0 0}.step{font-weight:600;color:var(--mut);margin-right:2px}
#msg{min-height:22px;color:var(--mut);margin-top:8px;font-size:14px}
</style></head><body><main>
<h1>Бот ставок РАУ</h1>
<div class="card"><div class="row">
<span class="pill" id="pb"><i class="dot"></i><span>Браузер</span></span>
<span class="pill" id="pr"><i class="dot"></i><span>Бот остановлен</span></span></div></div>

<div class="card"><h2>Подготовка</h2><div class="row">
<button onclick="act('browser')"><span class="step">1</span> Открыть браузер</button>
<button onclick="act('selftest')"><span class="step">2</span> Проверить вход в кабинет</button></div>
<p class="hint">В открывшемся окне войдите в кабинет РАУ через МЧД, затем нажмите «Проверить вход».</p></div>

<div class="card"><h2>Работа</h2><div class="row">
<button class="p" id="bd" onclick="act('run_dry')">▶ Тренировка (без подписи)</button>
<button class="live" id="bl" onclick="live()">▶ БОЕВОЙ режим (подписывает)</button>
<button onclick="act('stop')">■ Стоп</button></div>
<p class="hint">Тренировка заполняет форму и считает ставку, но ничего не отправляет. Боевой режим подписывает и отправляет заявки.</p>
<div id="msg"></div></div>

<div class="card"><h2>Список дел</h2>
<textarea id="tg" spellcheck="false"></textarea>
<div class="row" style="margin-top:10px"><button onclick="saveT()">Сохранить список</button></div>
<p class="hint">Одна строка на дело: ИНН; предел; старт; шаг; АУ 1; АУ 2; лимит АУ. Пустое поле = значение из настроек.</p></div>

<div class="card"><h2>Что происходит</h2><pre id="log"></pre>
<div class="row" style="margin-top:10px"><button onclick="act('check')">Проверить расчёт на протоколах</button>
<button onclick="journal()">Показать журнал ставок</button>
<button onclick="location.href='/journal.xlsx?k='+K">Скачать журнал в Excel</button>
<button onclick="location.href='/summary.xlsx?k='+K">Сводка по протоколам (Excel)</button></div></div>
</main><script>
const K="__TOKEN__";let since=0;
const $=id=>document.getElementById(id);
async function api(p,o){const r=await fetch(p+(p.includes('?')?'&':'?')+'k='+K,o);return r.json()}
async function act(n){const r=await api('/api/action',{method:'POST',body:JSON.stringify({name:n})});$('msg').textContent=r.msg||''}
function live(){if(confirm('БОЕВОЙ режим: бот будет подписывать и отправлять заявки от имени СРО.\nВы уверены?'))act('run_live')}
async function saveT(){const r=await api('/api/targets',{method:'POST',body:JSON.stringify({text:$('tg').value})});$('msg').textContent=r.msg}
async function journal(){const r=await api('/api/journal');const l=$('log');l.textContent+='\n--- журнал ---\n'+r.text;l.scrollTop=l.scrollHeight}
async function poll(){try{
 const s=await api('/api/status');
 $('pb').className='pill '+(s.browser?'on':'bad');$('pb').lastChild.textContent=s.browser?'Браузер подключён':'Браузер не запущен';
 $('pr').className='pill '+(s.running?(s.mode=='БОЕВОЙ'?'bad':'warn'):'');
 $('pr').lastChild.textContent=s.running?('Бот работает: '+s.mode):'Бот остановлен';
 $('bd').disabled=$('bl').disabled=s.running;
 const l=await api('/api/log?since='+since);if(l.lines.length){const e=$('log');const bottom=e.scrollTop+e.clientHeight>=e.scrollHeight-20;
  e.textContent+=l.lines.join('\n')+'\n';if(bottom)e.scrollTop=e.scrollHeight}since=l.total;
}catch(e){}}
api('/api/targets').then(r=>$('tg').value=r.text);setInterval(poll,1500);poll();
</script></body></html>"""


def main():
    ensure_files()
    url = f"http://{HOST}:{PORT}/?k={TOKEN}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Порт {PORT} занят: панель уже запущена? Закройте старое окно консоли и повторите.")
        return 1
    print(f"Панель: {url}\nНе закрывайте это окно, пока работаете. Остановить: Ctrl+C.")
    if "--no-open" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
