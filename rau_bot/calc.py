"""Расчёт минимальной ставки по правилам выбора СРО на площадке РАУ ФНС.

Правила (выведены из четырёх протоколов, см. tests/):
  1. От каждой СРО учитывается одно заявление: отозванные и отклонённые не считаются.
     Если у СРО несколько действующих заявлений, берётся наибольшее (см. effective_bids).
  2. Средний балл = сумма баллов этих заявлений / число СРО, округление до сотых
     (половина вверх: 56,255 -> 56,26).
  3. Выбираются СРО, чей балл НЕ МЕНЬШЕ среднего. Значит, равенство с конкурентом
     означает, что конкурент тоже проходит.

Чтобы пройти единственной, нужен балл, при котором все конкуренты СТРОГО ниже
среднего. Неизвестно, сравнивает ли площадка с округлённым или с точным средним,
поэтому min_bid() ищет ставку, безопасную при обоих вариантах.
Все расчёты в Decimal, шаг ставки 0,01.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def d(x) -> Decimal:
    """'0,01' / 0.01 / '12' -> Decimal."""
    return Decimal(str(x).replace(",", ".").replace("\xa0", "").strip())


def r2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Application:
    sro_inn: str
    points: Decimal
    withdrawn: bool = False
    rejected: bool = False
    name: str = ""


def effective_bids(apps: list[Application], mode: str = "max") -> dict[str, Decimal]:
    """СРО (ИНН) -> учитываемый балл. mode: 'max' (наибольшее) или 'last' (последнее)."""
    result: dict[str, Decimal] = {}
    for a in apps:
        if a.withdrawn or a.rejected:
            continue
        if a.sro_inn not in result or mode == "last":
            result[a.sro_inn] = a.points
        else:
            result[a.sro_inn] = max(result[a.sro_inn], a.points)
    return result


def average(points) -> Decimal:
    pts = list(points)
    return r2(sum(pts) / len(pts))


def winners(bids: dict[str, Decimal], exact: bool = False) -> set[str]:
    """СРО, прошедшие по баллу (не меньше среднего)."""
    pts = list(bids.values())
    avg = sum(pts) / len(pts) if exact else average(pts)
    return {inn for inn, p in bids.items() if p >= avg}


@dataclass(frozen=True)
class BidPlan:
    points: Decimal          # ставка, которую нужно подать
    participants: int        # число СРО вместе с нами
    avg_exact: Decimal
    avg_rounded: Decimal
    strongest: Decimal       # балл сильнейшего конкурента


def min_bid(competitors: list[Decimal]) -> BidPlan:
    """Минимальная ставка, при которой мы проходим единственными.

    competitors: учитываемые баллы остальных СРО (без нас, по одному на СРО).
    """
    if not competitors:
        return BidPlan(CENT, 1, CENT, CENT, Decimal(0))

    n = len(competitors) + 1
    s = sum(competitors)
    m = max(competitors)

    # Точная граница: (s + x) / n > m  <=>  x > m*n - s. Начинаем с неё.
    x = ((m * n - s) / CENT).to_integral_value(ROUND_CEILING) * CENT
    x = max(x, CENT)

    for _ in range(100_000):
        avg_e = (s + x) / n
        avg_r = r2(avg_e)
        if m < avg_e and m < avg_r and x >= avg_e and x >= avg_r:
            return BidPlan(x, n, avg_e, avg_r, m)
        x += CENT
    raise RuntimeError("min_bid: не удалось подобрать ставку")


def round_up_to_grid(x: Decimal, start: Decimal, step: Decimal) -> Decimal:
    """Ближайшая вверх ставка вида start + k*step (режим «стартовая ставка и шаг»)."""
    if x <= start:
        return start
    k = ((x - start) / step).to_integral_value(ROUND_CEILING)
    return start + k * step


@dataclass(frozen=True)
class Decision:
    action: str              # 'bid' | 'keep' | 'over_cap'
    points: Decimal | None
    plan: BidPlan
    reason: str


def decide(our_current: Decimal | None, competitors: list[Decimal],
           max_bid: Decimal, start: Decimal | None = None,
           step: Decimal | None = None) -> Decision:
    """Что делать сейчас. Ставки можно только повышать, поэтому понижать не пытаемся.

    start/step: если заданы, ставка округляется вверх до start + k*step
    (так бот «перебивает» конкурентов с заданным шагом, но не выше max_bid).
    """
    plan = min_bid(competitors)
    if step:
        plan = BidPlan(round_up_to_grid(plan.points, start or CENT, step), plan.participants,
                       plan.avg_exact, plan.avg_rounded, plan.strongest)
    if our_current is not None and our_current >= plan.points:
        return Decision("keep", None, plan,
                        f"наша ставка {our_current} уже не ниже нужной {plan.points}")
    if plan.points > max_bid:
        return Decision("over_cap", None, plan,
                        f"нужно {plan.points}, предел {max_bid}: не ставим")
    return Decision("bid", plan.points, plan,
                    f"нужно {plan.points} (среднее {plan.avg_rounded}, "
                    f"сильнейший конкурент {plan.strongest})")
