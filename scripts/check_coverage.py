"""
check_coverage.py — проверка покрытия по модулям (FIX-23).

Coverage не умеет порог «cleaner.py не ниже общего» из коробки, а критерий
из плана задачи именно такой: модуль, который ПИШЕТ в реестр, не должен
покрываться хуже среднего по проекту.

Читает coverage.xml (создаётся pytest --cov) и завершается с ненулевым
кодом, если инвариант нарушен. Отсутствие файла — не ошибка: локальный
прогон без --cov не должен падать.

Запуск:
    python -m pytest -m "not live" --cov --cov-report=xml
    python scripts/check_coverage.py
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COVERAGE_XML = ROOT / "coverage.xml"

# Модуль не должен покрываться хуже общего по проекту.
MUST_NOT_UNDERPERFORM = ("cleaner.py",)


def _rate(element: ET.Element) -> float:
    """Процент покрытия по элементу отчёта."""
    line_rate = float(element.get("line-rate", "0"))
    return line_rate * 100


def main() -> int:
    if not COVERAGE_XML.is_file():
        print(  # noqa: T201 — скрипт для CLI
            f"{COVERAGE_XML.name} не найден — прогон сначала с --cov-report=xml"
        )
        return 0

    root = ET.parse(COVERAGE_XML).getroot()
    total = _rate(root)

    rates: dict[str, float] = {}
    for cls in root.iter("class"):
        filename = (cls.get("filename") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if filename in MUST_NOT_UNDERPERFORM:
            rates[filename] = _rate(cls)

    if not rates:
        print("Не найдены строки для проверяемых модулей — проверка пропущена")  # noqa: T201
        return 0

    failed = False
    for name, value in sorted(rates.items()):
        verdict = "OK  " if value >= total else "ПРОВАЛ"
        print(f"{verdict} {name}: {value:.1f}% (общее {total:.1f}%)")  # noqa: T201
        if value < total:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
