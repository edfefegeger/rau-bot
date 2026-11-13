import re

from rau_bot.browser import Cabinet, SEARCH_URL


class FakeLocator:
    def __init__(self, count, on_click=None):
        self._count = count
        self._on_click = on_click
        self.first = self

    def count(self):
        return self._count() if callable(self._count) else self._count

    def is_visible(self):
        return True

    def click(self):
        if self._on_click:
            self._on_click()

    def wait_for(self, timeout):
        assert timeout == 15000
        assert self.count() > 0

    def filter(self, has_text):
        assert isinstance(has_text, re.Pattern)
        return self


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.form_open = False
        self.cta_clicked = False

    def get_by_text(self, text):
        return FakeLocator(lambda: int(self.form_open))

    def goto(self, url, wait_until):
        assert url == SEARCH_URL
        assert wait_until == "domcontentloaded"
        self.url = "https://rau.nalog.gov.ru/home/main"

    def get_by_role(self, role, name, exact):
        assert role == "link"
        assert name == "Перейти на площадку"
        assert exact is True
        return FakeLocator(1, self.open_search_form)

    def locator(self, selector):
        assert selector == "a:visible"
        return FakeLocator(0)

    def open_search_form(self):
        self.cta_clicked = True
        self.form_open = True
        self.url = SEARCH_URL


def test_search_navigation_uses_landing_page_call_to_action_after_redirect():
    cabinet = Cabinet.__new__(Cabinet)
    cabinet.page = FakePage()
    cabinet.debug_dir = ""
    cabinet._own = False

    cabinet._goto_search()

    assert cabinet.page.cta_clicked
    assert cabinet.page.form_open


def test_parallel_worker_claims_manual_search_tabs_without_taking_other_tabs():
    from rau_bot.browser import ADOPTED_TAB_BUSY_MARK, ADOPTED_TAB_MARK

    class ManualPage:
        def __init__(self, url, name="", has_search=False):
            self.url = url
            self.name = name
            self.has_search = has_search

        def evaluate(self, script):
            if script == "window.name":
                return self.name
            self.name = script.split("'")[1]

        def get_by_text(self, text):
            assert text == "ФИО/Наименование"
            return FakeLocator(int(self.has_search))

    class Context:
        def __init__(self, pages):
            self.pages = pages

    unrelated = ManualPage("https://example.com/")
    manual = ManualPage("https://rau.nalog.gov.ru/home/main")
    previously_adopted = ManualPage("https://rau.nalog.gov.ru/legal-cases/123", ADOPTED_TAB_MARK)
    ctx = Context([unrelated, manual, previously_adopted])

    assert Cabinet._claim_manual_tab(ctx) is manual
    assert manual.name == ADOPTED_TAB_BUSY_MARK
    assert Cabinet._claim_manual_tab(ctx) is previously_adopted
    assert previously_adopted.name == ADOPTED_TAB_BUSY_MARK
    assert Cabinet._claim_manual_tab(ctx) is None


def test_open_test_tabs_does_not_require_login(monkeypatch):
    import sys
    import types
    from rau_bot.browser import BASE, TEST_TAB_MARK

    opened = []

    class Page:
        def evaluate(self, script):
            assert script == f"window.name = '{TEST_TAB_MARK}'"

        def goto(self, url, wait_until, timeout):
            assert url == BASE
            assert wait_until == "commit"
            assert timeout == 10000
            opened.append(url)

    class Context:
        def new_page(self):
            return Page()

    class Browser:
        contexts = [Context()]

    class Playwright:
        chromium = types.SimpleNamespace(connect_over_cdp=lambda url: Browser())

        def stop(self):
            pass

    api = types.ModuleType("playwright.sync_api")
    api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: Playwright())
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)

    assert Cabinet.open_test_tabs(4) == [f"Тестовая вкладка {i}: открыта" for i in range(1, 5)]
    assert opened == [BASE] * 4


def test_open_test_tabs_rejects_invalid_count():
    import pytest

    with pytest.raises(ValueError, match="от 1 до 15"):
        Cabinet.open_test_tabs(0)
