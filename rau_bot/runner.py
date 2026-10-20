"""Планировщик: следит за делами, считает ставку, подаёт или предлагает её."""
from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .calc import CENT, Application, d, decide, effective_bids, get_tz


@dataclass
class Config:
    our_inn: str = "7730309884"            # ИНН нашей СРО (Гарант)
    tz: str = "Europe/Moscow"
    dry_run: bool = True                    # True: считаем и заполняем, но не подписываем
    place_initial: bool = True              # сразу подать 0,01, чтобы быть участником
    initial_points: Decimal = CENT
    final_window_min: int = 10              # за сколько минут до конца включаем быстрый режим
    bid_not_before_s: int = 180             # расчётную ставку подаём не раньше, чем за столько секунд до конца (0 = сразу)
                                            # (экономия баллов: конкуренты ставят в конце, мы после них)
    poll_slow_s: int = 120
    poll_fast_s: int = 15
    extra_margin: Decimal = Decimal("0")    # запас баллов сверху к расчётному минимуму
    default_max_bid: Decimal = Decimal("805")   # баллы СРО целиком
    check_au_balance: bool = True                # не подавать, если ставка больше доступных баллов АУ
    registry: str = "au_registry.xlsx"           # реестр АУ с почтами (необязательно)
    default_au_email: str = ""                   # почта, если у АУ нет своей в форме и в реестре
    au: list = field(default_factory=list)  # [{"fio": "...", "email": "..."}, ...]
    journal: str = "journal.csv"
    cdp_url: str = "http://127.0.0.1:9222"

    @classmethod
    def load(cls, path: str) -> "Config":
        return cls.load_dict(json.load(open(path, encoding="utf8")))

    @classmethod
    def load_dict(cls, raw: dict) -> "Config":
        raw = dict(raw)
        for k in ("initial_points", "extra_margin", "default_max_bid"):
            if k in raw:
                raw[k] = d(raw[k])
        return cls(**raw)


@dataclass
class Target:
    inn: str                          # ИНН должника
    max_bid: Decimal                  # предел ставки по делу (не выше баллов СРО и лимитов АУ)
    au: list                          # АУ: [{"fio": ...} или {"inn": ...}, ...]
    initial: Decimal = CENT           # стартовая ставка
    step: Decimal | None = None       # шаг повышения (режим «физики»); None = точный минимум


def read_targets(path: str, cfg: "Config") -> dict[str, Target]:
    """CSV с заголовком (разделитель ; или ,). Колонки, все кроме inn необязательны:
        inn;max_bid;initial;step;au1;au2;au_cap
    max_bid: предел ставки; au_cap: лимит баллов на дело, который АУ согласились потратить
    (итоговый предел = минимум из max_bid, au_cap и баллов СРО).
    au1/au2: ФИО или ИНН; почта через «|». Пустое берётся из config.json."""
    import csv as _csv
    lines = [l for l in open(path, encoding="utf-8-sig").read().splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    delim = ";" if ";" in lines[0] else ","
    out: dict[str, Target] = {}
    from .registry import load_registry
    reg = load_registry(cfg.registry)
    for r in _csv.DictReader(lines, delimiter=delim):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        inn = r.get("inn", "")
        if not inn.isdigit():
            continue
        caps = [cfg.default_max_bid]
        for key in ("max_bid", "au_cap"):
            if r.get(key):
                caps.append(d(r[key]))
        au = []
        for key in ("au1", "au2", "au3", "au4"):
            if r.get(key):
                fio, _, mail = r[key].partition("|")
                fio = fio.strip()
                item = {"inn": fio} if fio.isdigit() else {"fio": fio}
                if mail.strip():
                    item["email"] = mail.strip()
                au.append(item)
        out[inn] = Target(inn, min(caps), _with_emails(au or cfg.au, reg, cfg),
                          d(r["initial"]) if r.get("initial") else cfg.initial_points,
                          d(r["step"]) if r.get("step") else None)
    return out


def _with_emails(au: list, reg: dict, cfg: "Config") -> list:
    """Почта АУ для формы: заданная явно, иначе из реестра, иначе общая по умолчанию.
    (Бот вписывает её, только если сайт сам не подставил почту.)"""
    from .registry import norm_fio
    res = []
    for a in au:
        a = dict(a)
        if not a.get("email") or "укажите" in a["email"].lower():
            a.pop("email", None)
            mail = reg.get(norm_fio(a.get("fio", ""))) or cfg.default_au_email
            if mail:
                a["email"] = mail
        res.append(a)
    return res


class Journal:
    def __init__(self, path: str):
        self.path = path

    def write(self, **kw):
        row = {"time": datetime.now().isoformat(timespec="seconds"), **kw}
        new = not _exists(self.path)
        with open(self.path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["time", "inn", "event", "points", "detail"])
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in w.fieldnames})
        print(row)


def _exists(p):
    import os
    return os.path.exists(p)


class Watcher:
    """Один тик на одно дело. cabinet и now подменяются в тестах."""

    def __init__(self, cfg: Config, cabinet, journal: Journal, now=None):
        self.cfg, self.cab, self.j = cfg, cabinet, journal
        self.now = now or (lambda: datetime.now(get_tz(cfg.tz)))
        self.our_bid: dict[str, Decimal | None] = {}
        self.end: dict[str, datetime] = {}      # срок окончания приёма по делу (для расписания)
        self._last: dict[str, tuple] = {}       # последняя запись решения по делу (чтобы не повторять)
        self.extra_lead_s = 0                   # добавка к bid_not_before_s на очередь из многих дел

    def step(self, t: "Target") -> str:
        """'done' приём закончен, 'slow' ждём, 'fast' идёт финальное окно."""
        cfg, inn = self.cfg, t.inn
        info = self.cab.open_case(inn)
        if info.period_end is None:
            self.j.write(inn=inn, event="error", detail="не найден срок окончания")
            return "slow"
        self.end[inn] = info.period_end
        left = (info.period_end - self.now()).total_seconds()
        if left <= 0:
            self.j.write(inn=inn, event="finished", detail=f"срок вышел {info.period_end}")
            return "done"

        bids = effective_bids(info.bids)
        ours = bids.get(cfg.our_inn)
        self.our_bid[inn] = ours
        rivals = [v for k, v in bids.items() if k != cfg.our_inn]

        if ours is None:
            if cfg.place_initial and left > cfg.final_window_min * 60:
                self._send(t, t.initial, "initial")
                return "slow"
        if left > cfg.final_window_min * 60:
            return "slow"                    # ждём финального окна

        dec = decide(ours, rivals, t.max_bid - cfg.extra_margin, t.initial, t.step)
        if cfg.bid_not_before_s and left > cfg.bid_not_before_s + self.extra_lead_s:
            return "fast"                    # смотрим, но ещё не ставим
        key = (dec.action, dec.points, dec.reason)
        if dec.action == "bid" or self._last.get(inn) != key:   # одинаковое «не ставим» пишем один раз
            self.j.write(inn=inn, event=dec.action, points=dec.points or "", detail=dec.reason)
        self._last[inn] = key
        if dec.action == "bid":
            self._send(t, dec.points + cfg.extra_margin, "bid")
        return "fast"

    def _send(self, t, points, kind):
        inn = t.inn
        ok = self.cab.create_and_send(inn, points, t.au, self.cfg.dry_run,
                                      self.cfg.check_au_balance)
        avail = getattr(self.cab, "last_avail", None)   # «Доступные баллы» АУ после заморозки
        self.j.write(inn=inn, event=f"{kind}_sent" if ok else f"{kind}_prepared", points=points,
                     detail=("подписано" if ok else "dry-run: не подписано")
                     + (f"; баллы АУ в форме: {avail}" if avail else ""))
        self.our_bid[inn] = points if ok else self.our_bid.get(inn)


def next_delay(left_s: float, cfg: Config) -> float:
    """Через сколько секунд снова смотреть дело, если до конца приёма left_s секунд."""
    if left_s > 3600:
        return max(cfg.poll_slow_s, 600)          # до конца далеко: раз в 10 минут
    if left_s > cfg.final_window_min * 60:
        return cfg.poll_slow_s
    return cfg.poll_fast_s


def lead_for_queue(n_in_window: int, step_s: float = 20.0) -> float:
    """Запас времени на очередь: дел в финальном окне много, а бот обрабатывает их по одному."""
    return max(0, n_in_window - 1) * step_s


def run(cfg: Config, targets: dict[str, "Target"]):
    from .browser import Cabinet
    cab = Cabinet(cfg.cdp_url, cfg.tz)
    print(f"Бот запущен. Дел: {len(targets)}. Режим: "
          f"{'ТРЕНИРОВКА (без подписи)' if cfg.dry_run else 'БОЕВОЙ (подписывает)'}. Остановка: Ctrl+C")
    w = Watcher(cfg, cab, Journal(cfg.journal))
    active = dict(targets)
    due = {inn: 0.0 for inn in active}              # когда в следующий раз смотреть (time.monotonic)
    try:
        while active:
            now = time.monotonic()
            ready = [inn for inn in active if due[inn] <= now]
            # дела, у которых конец ближе, обрабатываем первыми
            ready.sort(key=lambda i: w.end.get(i).timestamp() if w.end.get(i) else 0)
            in_window = [i for i in active if w.end.get(i) and
                         (w.end[i] - w.now()).total_seconds() <= cfg.final_window_min * 60]
            w.extra_lead_s = lead_for_queue(len(in_window))
            for inn in ready:
                t = active[inn]
                t0 = time.monotonic()
                try:
                    state = w.step(t)
                except Exception as e:      # ошибка по одному делу не должна ронять остальные
                    from .browser import CaseClosed
                    if isinstance(e, CaseClosed):
                        w.j.write(inn=inn, event="skipped", detail=str(e))
                        del active[inn]      # подать нельзя: дело больше не опрашиваем
                        continue
                    w.j.write(inn=inn, event="error", detail=repr(e))
                    due[inn] = time.monotonic() + cfg.poll_fast_s
                    continue
                if state == "done":
                    del active[inn]
                    continue
                left = (w.end[inn] - w.now()).total_seconds() if inn in w.end else 0
                due[inn] = time.monotonic() + next_delay(left, cfg)
            if active:
                time.sleep(max(1.0, min(due.values()) - time.monotonic()))
    except KeyboardInterrupt:
        print("Остановлено.")
    finally:
        cab.close()
