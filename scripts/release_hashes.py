"""
release_hashes.py — публикация SHA256 релиза без участия человека (FIX-33).

Зачем. Хеш в README и в описании релиза — единственное, что позволяет
пользователю отличить подлинный файл от подменённого. Вручную это перестаёт
работать ровно тогда, когда нужно: при выпуске новой версии хеш забывают
обновить, и инструкция в README начинает врать. Врать хуже, чем молчать:
пользователь сверяет файл с устаревшим хешем и решает, что ему подсунули
чужой архив.

Три команды, вызываемые по отдельности (так сделано не для красоты):

  compute — посчитать хеши, записать ``SHA256SUMS.txt`` рядом с архивом и
            сохранить таблицу в файл. Не трогает ни README, ни GitHub.
  readme  — подставить таблицу в README.md между маркерами.
  notes   — добавить ту же таблицу в описание релиза на GitHub.

Разделение на три шага сделано из-за ветки: ``compute`` выполняется на
checkout'е тега релиза, а править README нужно в ветке по умолчанию —
иначе коммит автоматизации попал бы в сам тег. Промежуточная таблица лежит
во временной папке раннера и переживает смену ветки.

Идемпотентность. Повторный запуск с тем же архивом не размножает блок:
старый блок между маркерами заменяется целиком, а если маркеров нет —
блок дописывается в конец файла. Один и тот же вход даёт один и тот же
выход, поэтому шаг безопасно перезапускать.

Почему хеш EXE, а не только архива. Онлайн-сканеры портативный архив не
берут: у Dr.Web лимит 10 МБ, а архив больше. Пользователю нужен хеш того
файла, который он действительно загружает, поэтому EXE читается прямо из
архива и никуда не распаковывается.

Запуск вручную (для проверки, без GitHub):

    python scripts/release_hashes.py compute --archive KeyboardLayoutCleaner-1.1.0-portable.zip --tag v1.1.0
    python scripts/release_hashes.py readme --archive KeyboardLayoutCleaner-1.1.0-portable.zip --tag v1.1.0
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

#: Маркеры автоматически поддерживаемого блока. Тест test_release_hashes
#: требует их наличие в README: без маркеров команда readme молча дописала бы
#: таблицу в конец файла, и хеш оказался бы не там, где его ищет читатель.
BEGIN = "<!-- klc:hashes:begin -->"
END = "<!-- klc:hashes:end -->"

#: Имя исполняемого файла внутри портативного архива.
EXE_NAME = "KeyboardLayoutCleaner.exe"

#: Строка для файла SHA256SUMS.txt — формат принят в Linux-мире, проверяется
#: утилитой `sha256sum -c` и любым файловым менеджером.
SUMS_NAME = "SHA256SUMS.txt"


def sha256_bytes(data: bytes) -> str:
    """SHA256 в виде шестнадцатеричной строки."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """SHA256 файла — потоково, архив не влезает в память целиком."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect(archive: Path) -> list[tuple[str, int, str]]:
    """Строки таблицы: (имя, размер в байтах, sha256) для архива и EXE.

    Returns
    -------
    list[tuple[str, int, str]]
        Всегда начинается с самого архива; EXE добавляется, только если
        архив — zip и внутри действительно есть такой файл.
    """
    rows = [(archive.name, archive.stat().st_size, sha256_file(archive))]
    if not zipfile.is_zipfile(archive):
        return rows
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            if name.rsplit("/", 1)[-1].lower() == EXE_NAME.lower():
                data = bundle.read(name)
                rows.append((f"{name} (из архива)", len(data), sha256_bytes(data)))
                break
    return rows


def _size_label(size: int) -> str:
    """Размер с разделителем тысяч пробелом: 14 037 527 байт."""
    return f"{size:,}".replace(",", " ")


def render(rows: list[tuple[str, int, str]], tag: str) -> str:
    """Готовый блок между маркерами — то, что увидит читатель."""
    lines = [
        BEGIN,
        "",
        f"Релиз **{tag}**. Хеши посчитаны автоматически при публикации.",
        "",
        "| Файл | Размер | SHA256 |",
        "| --- | --- | --- |",
    ]
    for name, size, digest in rows:
        # ВЕРХНИЙ регистр — намеренно: PowerShell так показывает Get-FileHash,
        # и пользователь сверяет хеш глазами именно с ним. В SHA256SUMS.txt
        # остаётся нижний — там формат фиксирован утилитой sha256sum.
        lines.append(f"| `{name}` | {_size_label(size)} байт | `{digest.upper()}` |")
    lines += ["", END]
    return "\n".join(lines)


def replace_block(text: str, block: str) -> str:
    """Заменить блок между маркерами, а если маркеров нет — дописать в конец.

    Идемпотентно: вызов дважды даёт тот же результат, блок не дублируется.
    """
    if BEGIN in text and END in text:
        start = text.index(BEGIN)
        finish = text.index(END) + len(END)
        return text[:start] + block + text[finish:]
    separator = "" if text.endswith("\n") else "\n"
    return f"{text}{separator}\n{block}\n"


def write_sums(archive: Path) -> Path:
    """Создать SHA256SUMS.txt рядом с архивом (формат sha256sum -c)."""
    sums = archive.parent / SUMS_NAME
    body = "".join(f"{digest} *{name}\n" for name, _size, digest in collect(archive))
    sums.write_text(body, encoding="utf-8")
    return sums


def _table_from(args: argparse.Namespace) -> str:
    """Таблица из готового файла или посчитанная по архиву."""
    if args.table:
        return Path(args.table).read_text(encoding="utf-8").strip()
    return render(collect(Path(args.archive)), args.tag)


def cmd_compute(args: argparse.Namespace) -> int:
    """Посчитать хеши, записать SHA256SUMS.txt и таблицу."""
    archive = Path(args.archive)
    sums = write_sums(archive)
    table = render(collect(archive), args.tag)
    if args.out:
        Path(args.out).write_text(table + "\n", encoding="utf-8")
    print(table)  # noqa: T201 — скрипт для CLI
    print(f"\n{SUMS_NAME}: {sums}")  # noqa: T201 — скрипт для CLI
    return 0


def cmd_readme(args: argparse.Namespace) -> int:
    """Подставить таблицу в README между маркерами."""
    table = _table_from(args)
    text = README.read_text(encoding="utf-8")
    updated = replace_block(text, table)
    if updated == text:
        print("README уже актуален — изменений нет")  # noqa: T201 — скрипт для CLI
        return 0
    README.write_text(updated, encoding="utf-8")
    print(f"README обновлён: {BEGIN} … {END}")  # noqa: T201 — скрипт для CLI
    return 0


def _gh(*args: str) -> str:
    """Вызвать gh CLI и вернуть stdout; ошибка — исключение с текстом."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"gh {' '.join(args)}: {message}")
    return result.stdout


def cmd_notes(args: argparse.Namespace) -> int:
    """Добавить таблицу в описание релиза на GitHub (идемпотентно)."""
    table = _table_from(args)
    body = _gh("release", "view", args.tag, "--json", "body", "--jq", ".body")
    updated = replace_block(body.rstrip("\n"), table)
    if updated.rstrip("\n") == body.rstrip("\n"):
        print("Описание релиза уже актуально — изменений нет")  # noqa: T201 — CLI
        return 0
    notes_file = Path(args.notes_file)
    notes_file.write_text(updated.rstrip("\n") + "\n", encoding="utf-8")
    _gh("release", "edit", args.tag, "--notes-file", str(notes_file))
    print(f"Описание релиза {args.tag} обновлено")  # noqa: T201 — скрипт для CLI
    return 0


def main(argv: list[str] | None = None) -> int:
    """Точка входа: разбор аргументов и запуск нужной команды."""
    parser = argparse.ArgumentParser(
        description="Публикация SHA256 релиза в README и в описание релиза",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--tag", default="v?", help="тег релиза, например v1.1.0")
        p.add_argument("--archive", default="", help="путь к портативному zip")
        p.add_argument("--table", default="", help="готовая таблица из файла")

    p_compute = sub.add_parser("compute", help="посчитать хеши")
    add_common(p_compute)
    p_compute.add_argument("--out", default="", help="куда сохранить таблицу")
    p_compute.set_defaults(func=cmd_compute)

    p_readme = sub.add_parser("readme", help="обновить README.md")
    add_common(p_readme)
    p_readme.set_defaults(func=cmd_readme)

    p_notes = sub.add_parser("notes", help="обновить описание релиза")
    add_common(p_notes)
    p_notes.add_argument(
        "--notes-file", default="release-notes.md", help="временный файл для gh"
    )
    p_notes.set_defaults(func=cmd_notes)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)  # noqa: T201 — скрипт для CLI
        return 1


if __name__ == "__main__":
    sys.exit(main())

