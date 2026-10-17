from decimal import Decimal as D
from pathlib import Path

import pytest

from rau_bot.calc import (CENT, Application, average, decide, d, effective_bids,
                          min_bid, winners)
from rau_bot.protocol import read_protocol

DATA = Path(__file__).parent / "data"
OUR_INN = "7730309884"  # Ассоциация ПАУ «Гарант»
# ожидаемый результат: файл -> (среднее по протоколу, минимальная безопасная ставка)
CASES = {
    "management_city.xlsx": (D("57.47"), D("75.22")),
    "dsk.xlsx": (D("56.26"), D("35.00")),
    "gatchina.xlsx": (D("65.78"), D("14.92")),
    "solar.xlsx": (D("24.81"), D("26.19")),
}


@pytest.mark.parametrize("name", CASES)
def test_protocol_reproduced(name):
    """Наш расчёт даёт то же среднее и тех же победителей, что и площадка."""
    p = read_protocol(str(DATA / name))
    bids = effective_bids(p.applications)
    assert average(bids.values()) == p.average
    assert winners(bids) == p.selected == {OUR_INN}


@pytest.mark.parametrize("name", CASES)
def test_min_bid_is_sole_winner_and_minimal(name):
    p = read_protocol(str(DATA / name))
    bids = effective_bids(p.applications)
    rivals = [v for k, v in bids.items() if k != OUR_INN]

    plan = min_bid(rivals)
    assert plan.points == CASES[name][1]
    # на найденной ставке проходим единственными и при точном, и при округлённом среднем
    with_us = {f"c{i}": v for i, v in enumerate(rivals)} | {OUR_INN: plan.points}
    assert winners(with_us) == winners(with_us, exact=True) == {OUR_INN}
    # на 0,01 ниже уже небезопасно: есть риск, что конкурент тоже пройдёт
    lower = {f"c{i}": v for i, v in enumerate(rivals)} | {OUR_INN: plan.points - CENT}
    assert winners(lower) != {OUR_INN} or winners(lower, exact=True) != {OUR_INN}


def test_client_example_146():
    """Пример заказчицы: 5 СРО, сумма 226, сильнейший 61: ставка 146 даёт среднее 62."""
    rivals = [D("61"), D("55"), D("50"), D("40"), D("20")]
    assert sum(rivals) == 226
    plan = min_bid(rivals)
    assert plan.points == D("140.03")  # строгий минимум; 146 у заказчицы это запас ~6 баллов
    # ставка 146: среднее ровно 62, сильнейший 61 строго ниже
    assert average(rivals + [D("146")]) == D("62.00")
    assert winners({f"c{i}": v for i, v in enumerate(rivals)} | {"our": D("146")}) == {"our"}


def test_tie_means_both_win():
    """Равенство со средним = проходит. Ставка 146 при сильнейшем 62 НЕ достаточна."""
    rivals = [D("62"), D("41"), D("41"), D("41"), D("41")]  # сумма 226, максимум 62
    bids = {f"c{i}": v for i, v in enumerate(rivals)} | {"our": D("146")}
    assert len(winners(bids)) == 2          # проходит и конкурент с 62
    assert min_bid(rivals).points == D("146.03")


def test_rounding_half_up():
    assert average([D("0.02"), D("10"), D("15"), D("200")]) == D("56.26")  # 56,255


def test_effective_bids_withdrawn_and_duplicates():
    apps = [
        Application("1", d("0,02"), withdrawn=True),
        Application("1", d("3,1")),
        Application("1", d("5")),
        Application("2", d("7")),
        Application("3", d("9"), rejected=True),
    ]
    assert effective_bids(apps) == {"1": D("5"), "2": D("7")}
    assert effective_bids(apps, mode="last")["1"] == D("5")


def test_no_competitors_minimal_bid():
    assert min_bid([]).points == CENT


def test_decide_keep_bid_over_cap():
    rivals = [D("3"), D("3.1"), D("7")]               # нужно 14,92
    assert decide(None, rivals, D("100")).action == "bid"
    assert decide(None, rivals, D("100")).points == D("14.92")
    assert decide(D("20"), rivals, D("100")).action == "keep"   # понижать не пытаемся
    assert decide(D("0.01"), rivals, D("10")).action == "over_cap"
