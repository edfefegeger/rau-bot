"""Чтение xlsx-протокола выбора СРО (скачивается со страницы дела после завершения).

Нужен для проверки расчёта на реальных итогах и для будущих проверок бота.
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from decimal import Decimal

import openpyxl

from .calc import Application, d


@dataclass
class Protocol:
    debtor: str
    applications: list[Application]
    average: Decimal            # как написано в протоколе
    selected: set[str]          # ИНН СРО со значением "СРО выбрана = Да"


def _cell(v) -> str:
    return "" if v is None else str(v).strip()


def read_protocol(path: str) -> Protocol:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ws = openpyxl.load_workbook(path).active
    rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]

    debtor = ""
    for r in rows:
        if r and r[0].startswith("Наименование /ФИО должника"):
            debtor = next((c for c in r[1:] if c), "")
            break

    # таблица «Поступившие заявления СРО»: колонки ищем по заголовку
    head = next(i for i, r in enumerate(rows) if "Наименование СРО АУ" in r)
    col = {name: j for j, name in enumerate(rows[head]) if name}
    apps: list[Application] = []
    for r in rows[head + 1:]:
        if not r[0].isdigit():
            break
        apps.append(Application(
            sro_inn=r[col["ИНН СРО"]],
            points=d(r[col["Количество баллов в заявлении"]]),
            withdrawn=r[col["Отозвано"]].lower() == "да",
            rejected=r[col["Отклонено"]].lower() == "да",
            name=r[col["Наименование СРО АУ"]],
        ))

    # «(.02 + 10 + 20)/6=57,47»
    avg = None
    for r in rows:
        line = r[0] if r else ""
        m = re.search(r"\)\s*/\s*\d+\s*=\s*([\d,\.]+)\s*$", line)
        if line.startswith("(") and m:
            avg = d(m.group(1))
            break
    if avg is None:
        raise ValueError("не найдена строка «Расчет среднего балла»")

    # таблица «Выбор СРО...»: колонка «СРО выбрана»
    sel_head = next(i for i, r in enumerate(rows) if "СРО выбрана" in r)
    scol = {name: j for j, name in enumerate(rows[sel_head]) if name}
    selected: set[str] = set()
    for r in rows[sel_head + 1:]:
        if not r[0].isdigit():
            break
        if r[scol["СРО выбрана"]].lower() == "да":
            selected.add(r[scol["ИНН СРО"]])

    return Protocol(debtor, apps, avg, selected)
