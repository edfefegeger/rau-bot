"""Запуск:
  python -m rau_bot check                     проверка расчёта на протоколах из tests/data
  python -m rau_bot calc 0.02 10 15 200       ставка при баллах конкурентов
  python -m rau_bot dump <ИНН должника>       сохранить страницу дела для отладки
  python -m rau_bot run config.json targets.csv
"""
import sys
from pathlib import Path

from .calc import d, decide, effective_bids, min_bid


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "calc":
        plan = min_bid([d(x) for x in argv[2:]])
        print(f"ставка {plan.points}; участников {plan.participants}; "
              f"среднее {plan.avg_exact:.4f} (округл. {plan.avg_rounded}); "
              f"сильнейший конкурент {plan.strongest}")
    elif cmd == "check":
        from .protocol import read_protocol
        our = "7730309884"
        for f in sorted(Path(__file__).resolve().parent.parent.glob("tests/data/*.xlsx")):
            p = read_protocol(str(f))
            rivals = [v for k, v in effective_bids(p.applications).items() if k != our]
            print(f"{f.name}: среднее в протоколе {p.average}, минимальная ставка {min_bid(rivals).points}")
    elif cmd == "dump":
        from .browser import Cabinet
        from .runner import Config
        cfg = Config()
        cab = Cabinet(cfg.cdp_url, cfg.tz)
        info = cab.dump(argv[2], f"dump_{argv[2]}")
        print("срок:", info.period_end, "ставок:", len(info.bids))
        cab.close()
    elif cmd == "run":
        from .runner import Config, read_targets, run
        cfg = Config.load(argv[2])
        run(cfg, read_targets(argv[3], cfg))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
