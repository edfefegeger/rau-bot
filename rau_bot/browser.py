"""Работа с кабинетом РАУ через браузер, где вы уже вошли (Playwright, CDP).

ВАЖНО: эта часть написана по скриншотам и записи экрана, на живом сайте не
проверялась. Тексты кнопок взяты с экрана, структуру страницы дела нужно
сверить на сеансе: команда `python -m rau_bot dump <ИНН>` сохраняет текст и HTML
страницы, по ним правятся parse_case_text() и локаторы.

Браузер запускается вручную с отладочным портом (в нём вы входите через МЧД):
    "C:\\...\\Yandex\\YandexBrowser\\Application\\browser.exe" --remote-debugging-port=9222
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .calc import Application, d, get_tz

BASE = "https://rau.nalog.gov.ru"
SEARCH_URL = f"{BASE}/lk/bankruptcy-case-choice/legal-cases"  # «Площадка выбора АУ» (адрес дела: .../legal-cases/<id>)


@dataclass
class CaseInfo:
    inn: str
    url: str
    period_end: datetime | None
    bids: list[Application]          # заявления СРО в текущей процедуре
    text: str = ""


# --- разбор текста страницы (проверяется тестами без сайта) -----------------

_PERIOD = re.compile(
    r"Период проведения\s+(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})\s*-\s*(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})")
# блок участника, как на экране: «СРО <название> ИНН <10 цифр> ОГРН <число>
#                                  Количество кандидатов <n> Количество баллов <x>»
_PARTICIPANT = re.compile(
    r"(?<!от )(?<!Прием заявлений от )СРО\s+(?P<name>[^\n]+?)\s+ИНН\s+(?P<inn>\d{10})\s+ОГРН\s+\d+\s+"
    r"Количество кандидатов\s+\d+\s+Количество баллов\s+(?P<pts>\d+(?:[.,]\d+)?)",
    re.S)


def parse_case_text(text: str, tz: str = "Europe/Moscow") -> tuple[datetime | None, list[Application]]:
    """Из текста страницы дела берёт ПОСЛЕДНЮЮ процедуру: конец срока и ставки СРО."""
    flat = re.sub(r"\s+", " ", text)
    # процедуры на странице идут от новых к старым: текущая это первый блок
    starts = [m.start() for m in re.finditer(r"Процедура Прием заявлений", flat)]
    if not starts:
        block = flat
    else:
        block = flat[starts[0]:starts[1]] if len(starts) > 1 else flat[starts[0]:]
    end = None
    m = _PERIOD.search(block)
    if m:
        end = datetime.strptime(m.group(2), "%d.%m.%Y %H:%M").replace(tzinfo=get_tz(tz))
    apps = [Application(sro_inn=p["inn"], points=d(p["pts"]), name=p["name"])
            for p in _PARTICIPANT.finditer(block)]
    return end, apps


# --- управление страницей ----------------------------------------------------

_CONTROLS_JS = """() => {
  const out = [];
  document.querySelectorAll('button, a, input, select, textarea, [role=button], label').forEach(e => {
    const r = e.getBoundingClientRect();
    out.push({tag: e.tagName.toLowerCase(), type: e.type || '', text: (e.innerText || '').trim().slice(0, 80),
              id: e.id || '', name: e.name || '', placeholder: e.placeholder || '',
              aria: e.getAttribute('aria-label') || '', value: (e.value || '').toString().slice(0, 60),
              visible: r.width > 0 && r.height > 0});
  });
  return out;
}"""


class Cabinet:
    def __init__(self, cdp_url: str = "http://127.0.0.1:9222", tz: str = "Europe/Moscow",
                 debug_dir: str = "debug", attach: bool = False):
        """attach=True: работать в уже открытой вкладке (для команды inspect)."""
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            self._pw.stop()
            raise SystemExit(
                f"Не удалось подключиться к браузеру ({cdp_url}): {str(e).splitlines()[0]}\n"
                "Откройте браузер кнопкой «Открыть браузер» в панели (или python launcher.py browser), "
                "войдите в кабинет и повторите.") from None
        ctx = self._browser.contexts[0]
        rau = [pg for pg in ctx.pages if "rau.nalog.gov.ru" in pg.url]
        if attach and ctx.pages:
            self.page, self._own = ctx.pages[-1], False
        elif rau:                       # работаем во вкладке, где вы уже вошли в кабинет
            self.page, self._own = rau[-1], False
        else:
            self.page, self._own = ctx.new_page(), True
            self.page.goto(BASE)
        self.tz = tz
        self.debug_dir = debug_dir
        self.last_avail: list[str] = []
        self.page.set_default_timeout(20000)

    def close(self):
        if self._own:
            self.page.close()
        self._pw.stop()

    def snapshot(self, name: str) -> str:
        """Скриншот + текст + список кнопок и полей текущей страницы в папку debug/.
        Нужен, чтобы быстро поправить локаторы, если сайт устроен не так, как на скриншотах."""
        import json, os, time
        os.makedirs(self.debug_dir, exist_ok=True)
        base = os.path.join(self.debug_dir, f"{time.strftime('%H%M%S')}_{name}")
        try:
            self.page.screenshot(path=base + ".png", full_page=True)
            open(base + ".txt", "w", encoding="utf8").write(self.page.url + "\n" + self.page.inner_text("body"))
            json.dump(self.page.evaluate(_CONTROLS_JS), open(base + ".controls.json", "w", encoding="utf8"),
                      ensure_ascii=False, indent=1)
        except Exception as e:  # отладка не должна ронять работу
            print("snapshot не удался:", e)
        return base

    def _inn_field(self):
        """Поле «ФИО/Наименование, ИНН/ОГРН/ОГРНИП должника»: по подписи, иначе второе поле поиска на странице."""
        p = self.page
        loc = p.locator("xpath=//label[contains(normalize-space(.),'ФИО/Наименование')]/following::input[1]")
        return loc if loc.count() else p.locator("input[type=search]").nth(1)

    def _goto_search(self):
        """Страница «Площадка выбора АУ»: по адресу, запасной путь через меню."""
        p = self.page
        if p.get_by_text("ФИО/Наименование").count() == 0:
            p.goto(SEARCH_URL)
            p.wait_for_load_state("networkidle")
        if p.get_by_text("ФИО/Наименование").count() == 0:
            item = p.get_by_text("Площадка выбора АУ", exact=True).locator("visible=true")
            if item.count():
                item.first.hover()
                item.last.click()
        try:
            p.get_by_text("ФИО/Наименование").first.wait_for(timeout=15000)
        except Exception:
            self.snapshot("search_form_missing")
            raise LookupError("не открылась страница поиска «Площадка выбора АУ»") from None

    def _search(self, inn: str):
        p = self.page
        self._goto_search()
        self._inn_field().fill(inn)
        p.get_by_role("button", name="Найти", exact=True).click()
        p.wait_for_load_state("networkidle")
        link = p.locator("a", has_text=re.compile(r"^(Без номера|[А-ЯA-Z0-9\-/]+)$")).first
        try:
            link.wait_for(timeout=10000)
        except Exception:
            self.snapshot(f"not_found_{inn}")
            raise LookupError(f"дело с ИНН {inn} не найдено среди активных процедур (проверьте ИНН)") from None

    def open_case(self, inn: str) -> CaseInfo:
        p = self.page
        self._search(inn)
        p.locator("a", has_text=re.compile(r"^(Без номера|[А-ЯA-Z0-9\-/]+)$")).first.click()
        p.wait_for_load_state("networkidle")
        p.get_by_text("Ход процедуры выбора").wait_for()
        try:                                    # блок процедур подгружается позже заголовка
            p.get_by_text("Период проведения").first.wait_for(timeout=15000)
        except Exception:
            pass
        text = p.inner_text("body")
        end, bids = parse_case_text(text, self.tz)
        if end is None:
            self.snapshot(f"no_period_{inn}")
        return CaseInfo(inn, p.url, end, bids, text)

    def dump(self, inn: str, out_prefix: str):
        """Сохраняет страницу дела для отладки разбора."""
        info = self.open_case(inn)
        open(f"{out_prefix}.txt", "w", encoding="utf8").write(info.text)
        open(f"{out_prefix}.html", "w", encoding="utf8").write(self.page.content())
        return info

    def create_and_send(self, inn: str, points: Decimal, au: list[dict], dry_run: bool,
                        check_au_balance: bool = True) -> bool:
        """Подаёт заявление с баллами `points` и кандидатурами АУ (ФИО или ИНН).

        check_au_balance: если ставка больше «Доступных баллов» любого из выбранных АУ,
        заявление не отправляется (исключение). Точное правило площадки уточнить.

        dry_run=True: заполняет и сохраняет форму, но не подписывает.
        Возвращает True, если заявление подписано и отправлено.
        """
        p = self.page
        self.last_avail = []
        try:
            return self._fill_and_send(inn, points, au, dry_run, check_au_balance)
        except Exception:
            self.snapshot(f"error_{inn}")
            raise

    def _fill_and_send(self, inn, points, au, dry_run, check_au_balance) -> bool:
        p = self.page
        self._search(inn)          # ссылка «Подать заявление об участии» есть в карточке дела в результатах поиска
        self.snapshot(f"{inn}_0_search")
        p.get_by_text("Подать заявление об участии").first.click()
        p.wait_for_load_state("networkidle")
        self.snapshot(f"{inn}_1_step1")
        # шаг 1: дело — просто «Далее»
        p.get_by_role("button", name="Далее").first.click()
        # шаг 2: баллы (запятая как десятичный разделитель, как в форме)
        p.get_by_label(re.compile("Количество баллов")).fill(str(points).replace(".", ","))
        p.get_by_role("button", name="Далее").first.click()
        self.snapshot(f"{inn}_2_step3")
        # шаг 3: АУ по ФИО или ИНН
        for i, one in enumerate(au):
            if i > 0:
                p.get_by_text("добавить арбитражного управляющего").click()
            box = p.get_by_label(re.compile("ФИО или ИНН АУ")).nth(i)
            box.fill(one.get("fio") or one["inn"])
            p.get_by_role("button", name="Далее").nth(i).click()
            p.get_by_role("button", name="Выбрать").first.click()
            if one.get("email"):
                p.get_by_label("Электронная почта").nth(i).fill(one["email"])
            if check_au_balance:
                avail = d(p.get_by_label("Доступные баллы").nth(i).input_value())
                self.last_avail.append(str(avail))
                if points > avail:
                    raise ValueError(f"ставка {points} больше доступных баллов АУ {avail}")
        self.snapshot(f"{inn}_3_before_save")
        p.get_by_role("button", name="Сохранить").click()
        p.wait_for_load_state("networkidle")
        self.snapshot(f"{inn}_4_saved")
        if dry_run:
            return False
        p.get_by_role("button", name="Подписать и отправить").click()
        # окно «Выберите сертификат»: один сертификат, выбираем кликом по владельцу
        p.get_by_text("Выберите сертификат").wait_for(timeout=15000)
        p.locator("text=Выберите сертификат").locator("xpath=..").get_by_text(
            re.compile("Представитель|Действителен")).first.click()
        p.get_by_text(re.compile("успешно подписано и отправлено")).wait_for(timeout=30000)
        self.snapshot(f"{inn}_5_signed")
        return True
