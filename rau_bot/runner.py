"""Планировщик: следит за делами, считает ставку, подаёт или предлагает её."""
from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .calc import CENT, Application, d, decide, effective_bids


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
    au: list = field(default_factory=list)  # [{"fio": "...", "email": "..."}, ...]
    journal: str = "journal.csv"
    cdp_url: str = "http://127.0.0.1:9222"

    @classmethod
    def load(cls, path: str) -> "Config":
        raw = json.load(open(path, encoding="utf8"))
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
                item = {"inn": fio} if fio.isdigit() else {"fio": fio}
                if mail:
                    item["email"] = mail
                au.append(item)
        out[inn] = Target(inn, min(caps), au or cfg.au,
                          d(r["initial"]) if r.get("initial") else cfg.initial_points,
                          d(r["step"]) if r.get("step") else None)
    return out


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
        self.now = now or (lambda: datetime.now(ZoneInfo(cfg.tz)))
        self.our_bid: dict[str, Decimal | None] = {}

    def step(self, t: "Target") -> str:
        """'done' приём закончен, 'slow' ждём, 'fast' идёт финальное окно."""
        cfg, inn = self.cfg, t.inn
        info = self.cab.open_case(inn)
        if info.period_end is None:
            self.j.write(inn=inn, event="error", detail="не найден срок окончания")
            return "slow"
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
        if cfg.bid_not_before_s and left > cfg.bid_not_before_s:
            return "fast"                    # смотрим, но ещё не ставим
        self.j.write(inn=inn, event=dec.action, points=dec.points or "", detail=dec.reason)
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


def run(cfg: Config, targets: dict[str, "Target"]):
    from .browser import Cabinet
    cab = Cabinet(cfg.cdp_url, cfg.tz)
    print(f"Бот запущен. Дел: {len(targets)}. Режим: "
          f"{'ТРЕНИРОВКА (без подписи)' if cfg.dry_run else 'БОЕВОЙ (подписывает)'}. Остановка: Ctrl+C")
    w = Watcher(cfg, cab, Journal(cfg.journal))
    active = dict(targets)
    try:
        while active:
            fast = False
            for inn, t in list(active.items()):
                try:
                    state = w.step(t)
                    if state == "done":
                        del active[inn]
                    fast = fast or state == "fast"
                except Exception as e:      # ошибка по одному делу не должна ронять остальные
                    w.j.write(inn=inn, event="error", detail=repr(e))
            time.sleep(cfg.poll_fast_s if fast else cfg.poll_slow_s)
    except KeyboardInterrupt:
        print("Остановлено.")
    finally:
        cab.close()
