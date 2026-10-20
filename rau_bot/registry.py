"""Реестр АУ (xlsx от заказчика): ФИО -> электронная почта. Нужен, чтобы бот сам вписывал почту АУ."""
from __future__ import annotations

import os
import re


def norm_fio(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("ё", "е").replace("Ё", "Е")).strip().upper()


def load_registry(path: str) -> dict[str, str]:
    """Строки вида: № п/п | ФИО | почта (первая строка заголовок). Возвращает {ФИО в верхнем регистре: почта}.
    Если у ФИО две записи (полные тёзки), почта не берётся: неоднозначно."""
    if not path or not os.path.exists(path):
        return {}
    import openpyxl
    ws = openpyxl.load_workbook(path, data_only=True).active
    out: dict[str, str] = {}
    dup: set[str] = set()
    for r in ws.iter_rows(values_only=True):
        if len(r) < 3 or not str(r[0] or "").strip().isdigit():
            continue
        fio, mail = norm_fio(r[1]), str(r[2] or "").strip()
        if not fio or "@" not in mail:
            continue
        if fio in out and out[fio] != mail:
            dup.add(fio)
        out[fio] = mail
    for k in dup:
        out.pop(k, None)
    return out
