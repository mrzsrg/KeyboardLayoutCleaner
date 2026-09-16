"""__main__.py — запуск каталога: ``python .`` (эквивалент ``python main.py``).

Примечание: ``python -m keyboard-layout-cleaner`` НЕ работает — имя пакета
содержит дефис и не является импортируемым модулем, а установка через
``pip install .`` не поддерживается (см. pyproject.toml).
"""

from main import main

if __name__ == "__main__":
    main()
