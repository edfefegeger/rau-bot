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
    case_no: str = ""                # номер дела (А76-9594/2023)
    debtor: str = ""                 # ФИО или наименование должника


def parse_case_meta(text: str) -> tuple[str, str]:
    """Номер дела и должник с карточки дела."""
    m = re.search(r"Номер дела\s*\n\s*([^\n]+)", text)
    no = m.group(1).strip() if m else ""
    m = re.search(r"Должник\s*\n\s*(?:ФИО|Полное наименование)\s*\n\s*([^\n]+)", text)
    return no, (m.group(1).strip() if m else "")


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


TAB_MARK = "rau_bot_worker"          # метка вкладок бота (window.name), чтобы находить и закрывать «забытые»
TEST_TAB_MARK = "rau_bot_test_worker"
ADOPTED_TAB_MARK = "rau_bot_adopted"
ADOPTED_TAB_BUSY_MARK = "rau_bot_adopted_busy"


class CaseClosed(Exception):
    """По делу нельзя подать заявление (приём закончен или процедура уже в другом статусе)."""


class Cabinet:
    def __init__(self, cdp_url: str = "http://127.0.0.1:9222", tz: str = "Europe/Moscow",
                 debug_dir: str = "debug", attach: bool = False, new_tab: bool = False):
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
        if new_tab:                     # отдельная вкладка для параллельной работы (вход в кабинет общий)
            self.page = self._claim_manual_tab(ctx)
            self._adopted = self.page is not None
            self._own = not self._adopted
            if self.page is None:
                self.page = ctx.new_page()
                self._mark()
            self.page.set_default_navigation_timeout(60000)
            if not self._adopted:
                self.page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
                self._mark()
        elif attach and ctx.pages:
            self.page, self._own = ctx.pages[-1], False
            self._adopted = False
        elif rau:                       # работаем во вкладке, где вы уже вошли в кабинет
            self.page, self._own = rau[-1], False
            self._adopted = False
        else:
            self.page, self._own = ctx.new_page(), True
            self._adopted = False
            self.page.goto(BASE)
        self.tz = tz
        self.debug_dir = debug_dir
        self.last_avail: list[str] = []
        self.page.set_default_timeout(20000)

    def _mark(self):
        """Помечаем вкладку бота, чтобы при следующем запуске закрыть оставшиеся."""
        try:
            mark = ADOPTED_TAB_BUSY_MARK if getattr(self, "_adopted", False) else TAB_MARK
            self.page.evaluate(f"window.name = '{mark}'")
        except Exception:
            pass

    @staticmethod
    def _claim_manual_tab(ctx):
        """Claim an available bot tab or an open rau.nalog.gov.ru tab."""
        for pg in ctx.pages:
            try:
                name = pg.evaluate("window.name")
                if name in (ADOPTED_TAB_MARK, TAB_MARK, TEST_TAB_MARK):
                    pg.evaluate(f"window.name = '{ADOPTED_TAB_BUSY_MARK}'")
                    return pg
                if not name and "rau.nalog.gov.ru" in pg.url:
                    pg.evaluate(f"window.name = '{ADOPTED_TAB_BUSY_MARK}'")
                    return pg
            except Exception:
                continue
        return None

    def ready(self) -> bool:
        """Новая вкладка загрузила форму поиска (две попытки с перезагрузкой). False — вкладка не открылась."""
        for attempt in range(2):
            try:
                self.page.get_by_text("ФИО/Наименование").first.wait_for(timeout=25000)
                return True
            except Exception:
                try:
                    self.page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=40000)
                    self._mark()
                except Exception:
                    pass
        return False

    @staticmethod
    def close_stale_tabs(cdp_url: str = "http://127.0.0.1:9222") -> int:
        """Закрывает вкладки бота, оставшиеся от прошлого запуска (после «Стоп» они не закрываются сами)."""
        from playwright.sync_api import sync_playwright
        closed = 0
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            for ctx in browser.contexts:
                for pg in list(ctx.pages):
                    try:
                        name = pg.evaluate("window.name")
                        if name in (TAB_MARK, TEST_TAB_MARK):
                            pg.close()
                            closed += 1
                        elif name == ADOPTED_TAB_BUSY_MARK:
                            pg.evaluate(f"window.name = '{ADOPTED_TAB_MARK}'")
                    except Exception:
                        pass
        finally:
            pw.stop()
        return closed

    @staticmethod
    def open_test_tabs(count: int, cdp_url: str = "http://127.0.0.1:9222") -> list[str]:
        """Open visible site tabs to test browser tab startup without requiring cabinet login."""
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 15:
            raise ValueError("Количество тестовых вкладок должно быть целым числом от 1 до 15")
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        results = []
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0]
            for index in range(1, count + 1):
                try:
                    page = ctx.new_page()
                    page.evaluate(f"window.name = '{TEST_TAB_MARK}'")
                    try:
                        page.goto(BASE, wait_until="commit", timeout=10000)
                        results.append(f"Тестовая вкладка {index}: открыта")
                    except Exception as e:
                        results.append(f"Тестовая вкладка {index}: создана, но сайт не загрузился: {e}")
                except Exception as e:
                    results.append(f"Тестовая вкладка {index}: не удалось создать: {e}")
        finally:
            pw.stop()
        return results

    @staticmethod
    def prepare_tabs(cdp_url: str = "http://127.0.0.1:9222") -> int:
        """Make tabs left by a stopped bot available for reuse without closing them."""
        from playwright.sync_api import sync_playwright
        prepared = 0
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            for ctx in browser.contexts:
                for pg in list(ctx.pages):
                    try:
                        if pg.evaluate("window.name") in (TAB_MARK, ADOPTED_TAB_BUSY_MARK):
                            pg.evaluate(f"window.name = '{ADOPTED_TAB_MARK}'")
                            prepared += 1
                    except Exception:
                        pass
        finally:
            pw.stop()
        return prepared

    def close(self):
        if self._own:
            try:
                self.page.close()
            except Exception:
                pass
        elif getattr(self, "_adopted", False):
            try:
                self.page.evaluate(f"window.name = '{ADOPTED_TAB_MARK}'")
            except Exception:
                pass
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
            p.goto(SEARCH_URL, wait_until="domcontentloaded")
            if self._own:
                self._mark()
        if p.get_by_text("ФИО/Наименование").count() == 0:
            cta = p.get_by_role("link", name="Перейти на площадку", exact=True)
            if cta.count():
                cta.first.click()
            else:
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
        no, debtor = parse_case_meta(text)
        return CaseInfo(inn, p.url, end, bids, text, no, debtor)

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

        dry_run=True: заполняет форму до кнопки «Сохранить» и останавливается (на площадке ничего не сохраняется).
        Возвращает True, если заявление подписано и отправлено.
        """
        p = self.page
        self.last_avail = []
        try:
            return self._fill_and_send(inn, points, au, dry_run, check_au_balance)
        except Exception:
            self.snapshot(f"error_{inn}")
            raise

    def _next(self):
        """Кнопка «Далее» текущего шага (скрытые кнопки предыдущих шагов пропускаем)."""
        btn = self.page.get_by_role("button", name="Далее").locator("visible=true")
        btn.first.click()
        self.page.wait_for_timeout(600)

    def _fill_and_send(self, inn, points, au, dry_run, check_au_balance) -> bool:
        p = self.page
        self._search(inn)          # ссылка «Подать заявление об участии» есть в карточке дела в результатах поиска
        self.snapshot(f"{inn}_0_search")
        apply = p.get_by_text("Подать заявление об участии")
        try:
            apply.first.wait_for(state="visible", timeout=10000)
        except Exception:
            card = p.inner_text("body")
            m = re.search(r"Статус процедуры выбора(.{0,120})", card, re.S)
            st = re.sub(r"\s+", " ", m.group(1)).strip() if m else "?"
            raise CaseClosed(f"нет кнопки «Подать заявление об участии»; {st}")
        apply.first.click()
        p.wait_for_load_state("networkidle")
        self.snapshot(f"{inn}_1_step1")
        # шаг 1: дело, поля уже заполнены самим сайтом: просто «Далее»
        self._next()
        # шаг 2: баллы (запятая как десятичный разделитель, как в форме)
        box = p.locator('input[id^="au-number"]')
        box.wait_for()
        box.fill(str(points).replace(".", ","))
        self.snapshot(f"{inn}_2_points")
        self._next()
        self.snapshot(f"{inn}_3_au_step")
        # шаг 3: АУ. Блок на каждого АУ: поле «Укажите ФИО или ИНН АУ» -> «Далее» -> строка результата
        # «ФИО, ИНН, Доступные баллы N, [Выбрать]»
        for i, one in enumerate(au):
            # выбранный АУ превращается в заполненную анкету, поле поиска остаётся только у ещё не выбранных
            fields = p.locator('input[id^="au-typeahead"]:visible:not([readonly]):not([disabled])')
            if fields.count() == 0:
                p.get_by_text("добавить арбитражного управляющего").first.click()
                p.wait_for_timeout(500)
                fields = p.locator('input[id^="au-typeahead"]:visible:not([readonly]):not([disabled])')
            field = fields.first
            field.scroll_into_view_if_needed()
            field.click()
            field.fill("")
            field.press_sequentially(one.get("fio") or one["inn"], delay=40)
            p.wait_for_timeout(700)
            self.snapshot(f"{inn}_3_au{i + 1}_typed")
            field.press("Enter")                 # то же, что кнопка «Далее» рядом с полем (submit формы)
            pick = field.locator("xpath=following::button[normalize-space()='Выбрать'][1]")
            try:
                pick.wait_for(state="visible", timeout=8000)
            except Exception:
                nxt = field.locator("xpath=following::button[normalize-space()='Далее'][1]")
                if nxt.count():
                    nxt.first.click()
                pick.wait_for(state="visible", timeout=8000)
            self.snapshot(f"{inn}_3_au{i + 1}_found")
            row = pick.locator("xpath=ancestor::*[contains(.,'Доступные баллы')][1]").inner_text()
            m = re.search(r"Доступные баллы\s*([\d\s\xa0]*\d(?:[.,]\d+)?)", row)
            if m and check_au_balance:
                avail = d(re.sub(r"[\s\xa0]", "", m.group(1)))
                self.last_avail.append(str(avail))
                if points > avail:
                    raise ValueError(f"ставка {points} больше доступных баллов АУ {avail} ({one.get('fio') or one['inn']})")
            elif check_au_balance:
                print(f"(!) у АУ {one.get('fio') or one['inn']} не видно доступных баллов ({row[:80]!r}): проверка пропущена")
            pick.click()
            p.wait_for_timeout(800)
            self.snapshot(f"{inn}_3_au{i + 1}_chosen")
            mail = p.locator("xpath=//label[contains(normalize-space(.),'Электронная почта')]/following::input[1]")
            email = one.get("email", "")
            if (email and "укажите" not in email.lower() and mail.count() > i
                    and not mail.nth(i).input_value().strip()):      # почту, подставленную сайтом, не трогаем
                mail.nth(i).fill(email)
            if mail.count() > i and not mail.nth(i).input_value().strip():
                print(f"(!) у АУ {one.get('fio') or one['inn']} не заполнена электронная почта: укажите её в config.json (email)")
        self.snapshot(f"{inn}_3_before_save")
        if dry_run:                    # тренировка: заявление на площадке не сохраняем и не отправляем
            return False
        p.get_by_role("button", name="Сохранить").click()
        p.wait_for_load_state("networkidle")
        p.get_by_text("Просмотр заявления").first.wait_for(timeout=20000)   # ждём, пока откроется страница просмотра
        p.get_by_text("Количество баллов").nth(1).wait_for(timeout=10000)
        self.snapshot(f"{inn}_4_saved")
        # страница «Просмотр заявления»: перед подписью сверяем баллы и число АУ с тем, что хотели подать
        page = p.inner_text("body")
        m = re.search(r"Количество баллов\s*\n?\s*([\d.,]+)", page)
        if not m or d(m.group(1)) != points:
            raise ValueError(f"в сохранённом заявлении баллы {m.group(1) if m else '?'}, ожидалось {points}: не подписываем")
        if page.count("Доступные баллы") < len(au):
            raise ValueError("в сохранённом заявлении меньше АУ, чем нужно: не подписываем")
        if dry_run:
            return False
        p.get_by_role("button", name="Подписать и отправить").first.click()
        # окно «Выберите сертификат»: один сертификат, выбираем кликом по владельцу
        p.get_by_text("Выберите сертификат").wait_for(timeout=15000)
        p.locator("text=Выберите сертификат").locator("xpath=..").get_by_text(
            re.compile("Представитель|Действителен")).first.click()
        p.get_by_text(re.compile("успешно подписано и отправлено")).wait_for(timeout=30000)
        self.snapshot(f"{inn}_5_signed")
        return True
