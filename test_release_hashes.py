"""
test_release_hashes.py — регрессии автоматической публикации SHA256 (FIX-33).

Зачем эти тесты. Автоматизация, которая молча ничего не делает, опаснее
отсутствия автоматизации: README продолжает показывать хеш прошлого релиза,
и человек, сверивший файл, получит ложное «файл подменён». Поэтому здесь
есть проверки на ОБЕ стороны — что механизм считает правильно и что он
заметен, когда его отключили (маркеры в README).
"""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent


def _load_module():
    """Загрузить scripts/release_hashes.py как модуль.

    Скрипт лежит не в корне, а пакетом он не является, поэтому обычный
    импорт не работает. Путь берём из __file__, а не из CWD: тест не должен
    зависеть от того, откуда запустили pytest.
    """
    path = ROOT / "scripts" / "release_hashes.py"
    spec = importlib.util.spec_from_file_location("release_hashes", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rh = _load_module()


def _make_zip(path: Path, *, with_exe: bool = True) -> bytes:
    """Создать zip и вернуть байты «исполняемого файла»."""
    payload = b"MZ\x90\x00 fake portable build"
    with zipfile.ZipFile(path, "w") as bundle:
        if with_exe:
            bundle.writestr("KeyboardLayoutCleaner/KeyboardLayoutCleaner.exe", payload)
        bundle.writestr("KeyboardLayoutCleaner/README.txt", b"x")
    return payload


class TestCollect:
    """Что именно попадает в таблицу."""

    def test_archive_and_inner_exe(self, tmp_path):
        """Строк две: архив и EXE внутри него; хеши — настоящие."""
        archive = tmp_path / "portable.zip"
        payload = _make_zip(archive)

        rows = rh.collect(archive)

        assert [name for name, _s, _h in rows] == [
            "portable.zip",
            "KeyboardLayoutCleaner/KeyboardLayoutCleaner.exe (из архива)",
        ]
        assert rows[0][2] == hashlib.sha256(archive.read_bytes()).hexdigest()
        assert rows[1][2] == hashlib.sha256(payload).hexdigest()
        assert rows[1][1] == len(payload)

    def test_zip_without_exe(self, tmp_path):
        """Нет EXE — остаётся только архив: пустых строк быть не должно."""
        archive = tmp_path / "portable.zip"
        _make_zip(archive, with_exe=False)

        rows = rh.collect(archive)

        assert len(rows) == 1
        assert rows[0][0] == "portable.zip"

    def test_plain_file_is_not_unpacked(self, tmp_path):
        """Не-zip не распаковывается, но хеш всё равно считается."""
        archive = tmp_path / "notes.txt"
        archive.write_bytes(b"just a file")

        rows = rh.collect(archive)

        assert len(rows) == 1
        assert rows[0][2] == hashlib.sha256(b"just a file").hexdigest()


class TestCliEncoding:
    """Скрипт запускается процессом, а не импортом — это другая среда.

    Регрессия FIX-35, пойманная на первом же реальном релизе: шаг
    ``compute`` печатает таблицу с русскими подписями, кодировка консоли
    на раннере — cp1252, и печать падала UnicodeEncodeError уже после того,
    как хеши посчитаны. Релиз остался без архива и без хешей.

    Все прочие тесты файла вызывают функции напрямую, где вывод
    перехватывает pytest, — поэтому условие воспроизводится здесь и
    только здесь: запуск настоящего подпроцесса.
    """

    def test_compute_survives_cp1252_console(self, tmp_path):
        archive = tmp_path / "portable.zip"
        _make_zip(archive)
        out = tmp_path / "table.md"
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "release_hashes.py"),
                "compute",
                "--archive", str(archive),
                "--tag", "v1.2.0",
                "--out", str(out),
            ],
            capture_output=True,
            env=env,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        # Таблица на диске обязана быть валидным UTF-8 с русскими подписями:
        # именно она потом попадёт в README.
        assert "Релиз **v1.2.0**" in out.read_text(encoding="utf-8")

    def test_force_utf8_is_idempotent(self):
        """Повторный вызов не ломает уже перенастроенный поток."""
        rh._force_utf8_output()
        rh._force_utf8_output()

    def test_notes_reads_cyrillic_body_under_cp1252(self, tmp_path):
        """Описание релиза на русском должно читаться под cp1252.

        Второй баг того же класса, пойманный следом: ``gh`` возвращает
        описание релиза с русским текстом, а ``text=True`` без явной
        кодировки декодировал его в локаль процесса. Падение происходило в
        потоке-читателе subprocess, и наружу выходило
        ``AttributeError: 'NoneType' object has no attribute 'rstrip'`` —
        сообщение, не указывающее на настоящую причину.
        """
        fake_gh = tmp_path / "fake_gh.py"
        fake_gh.write_text(
            "import sys\n"
            "if 'view' in sys.argv:\n"
            "    sys.stdout.reconfigure(encoding='utf-8')\n"
            "    print('## Что изменилось')\n"
            "    print()\n"
            "    print('Одна строка = одна раскладка.')\n",
            encoding="utf-8",
        )
        table = tmp_path / "table.md"
        table.write_text(
            f"{rh.BEGIN}\n\n| Файл | Размер | SHA256 |\n"
            "| --- | --- | --- |\n"
            "| `portable.zip` | 1 000 байт | `ABC` |\n\n" + rh.END + "\n",
            encoding="utf-8",
        )
        notes_file = tmp_path / "notes.md"
        env = {
            **os.environ,
            "PYTHONIOENCODING": "cp1252",
            "KLC_GH": sys.executable,
            "KLC_GH_ARGS": json.dumps([str(fake_gh)]),
        }

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "release_hashes.py"),
                "notes",
                "--tag", "v1.2.0",
                "--table", str(table),
                "--notes-file", str(notes_file),
            ],
            capture_output=True,
            env=env,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        written = notes_file.read_text(encoding="utf-8")
        # Русский текст описания не потерялся и таблица в него вставлена
        assert "Одна строка = одна раскладка." in written
        assert "| `portable.zip` |" in written


class TestRenderAndReplace:
    """Формат таблицы и идемпотентность подстановки."""
    def test_render_has_markers_and_uppercase_hash(self):
        """Хеш в верхнем регистре: так его показывает PowerShell."""
        digest = "a" * 64
        table = rh.render([("f.zip", 1_000_000, digest)], "v1.2.3")

        assert table.startswith(rh.BEGIN)
        assert table.rstrip().endswith(rh.END)
        assert digest.upper() in table
        assert "1 000 000 байт" in table
        assert "v1.2.3" in table

    def test_replace_block_is_idempotent(self):
        """Второй запуск с тем же блоком ничего не меняет."""
        first = rh.render([("f.zip", 1, "a" * 64)], "v1")
        second = rh.render([("f.zip", 2, "b" * 64)], "v2")

        once = rh.replace_block("текст\n", first)
        twice = rh.replace_block(once, second)

        assert "a" * 64 not in twice, "старый блок должен исчезнуть целиком"
        assert twice.count(rh.BEGIN) == 1
        assert rh.replace_block(twice, second) == twice

    def test_block_appended_when_markers_missing(self):
        """Без маркеров блок дописывается в конец — иначе пропадёт молча."""
        block = rh.render([("f.zip", 1, "a" * 64)], "v1")

        updated = rh.replace_block("инструкция\n", block)

        assert block in updated
        assert updated.startswith("инструкция\n")
        assert updated.count(rh.BEGIN) == 1


class TestSumsFile:
    """Формат SHA256SUMS.txt."""

    def test_standard_sha256sum_format(self, tmp_path):
        """Строки вида ``<hash> *<имя>`` — их понимает ``sha256sum -c``."""
        archive = tmp_path / "portable.zip"
        _make_zip(archive)

        sums = rh.write_sums(archive)
        lines = sums.read_text(encoding="utf-8").splitlines()

        assert sums.name == "SHA256SUMS.txt"
        assert len(lines) == 2
        assert all(len(line.split(" *", 1)) == 2 for line in lines)
        assert lines[0].split(" *", 1)[0] == hashlib.sha256(
            archive.read_bytes()
        ).hexdigest()


class TestReadmeCommand:
    """Команда readme правит файл и не трогает его зря."""

    def test_writes_block_and_reports_no_change_on_second_run(
        self, tmp_path, capsys, monkeypatch
    ):
        readme = tmp_path / "README.md"
        readme.write_text("начало\n", encoding="utf-8")
        archive = tmp_path / "portable.zip"
        _make_zip(archive)
        monkeypatch.setattr(rh, "README", readme)

        code = rh.main(["readme", "--archive", str(archive), "--tag", "v9.9.9"])
        first = readme.read_text(encoding="utf-8")
        rh.main(["readme", "--archive", str(archive), "--tag", "v9.9.9"])
        second = readme.read_text(encoding="utf-8")

        assert code == 0
        assert "v9.9.9" in first
        assert first.count(rh.BEGIN) == 1
        assert first == second, "повторный запуск изменил файл"
        assert "изменений нет" in capsys.readouterr().out


def test_readme_still_has_markers():
    """Маркеры в README обязаны быть: без них автоматика допишет хеш в конец.

    Самая неочевидная поломка этой автоматизации — удалить маркеры при
    правке текста. Скрипт отработает без ошибки, а читатель не найдёт хеш
    там, где ищет. Тест превращает тихую поломку в громкую.
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert rh.BEGIN in text, "в README нет маркера начала блока хешей"
    assert rh.END in text, "в README нет маркера конца блока хешей"
    assert text.index(rh.BEGIN) < text.index(rh.END), "маркеры переставлены местами"


def test_readme_does_not_hardcode_stale_release_version():
    """В прозе README не должно быть имени архива конкретной версии.

    Найдено 29.09.2026: таблица хешей в автоматическом блоке говорила про
    v1.3.1, а команда проверки на 12 строк ниже — про v1.1.0. Пользователь,
    скопировавший команду дословно, не находил файл, а README противоречил
    сам себе в пределах одного экрана.

    Блок хешей обновляется автоматически при релизе, а проза — нет, и
    расходятся они тихо: ни один тест этого не замечал. Поэтому вне блока
    запрещено имя вида ``KeyboardLayoutCleaner-1.2.3-portable.zip`` — команда
    проверки должна быть версионно-независимой (``*-portable.zip``).
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    begin, end = text.index(rh.BEGIN), text.index(rh.END) + len(rh.END)
    outside = text[:begin] + text[end:]
    stale = re.findall(
        r"KeyboardLayoutCleaner-\d+\.\d+\.\d+-portable\.zip", outside
    )
    assert not stale, f"в прозе README зашита версия: {stale}"


def test_readme_hash_block_and_command_agree():
    """Команда проверки обязана работать для архива из таблицы хешей.

    Связывает прошлую поломку с её последствием: если команда и таблица
    описывают разные файлы, сверка всегда даёт ложное «файл подменён».
    Конкретную версию в блок хешей подставляет автоматика, а команда в
    прозе обязана быть шаблонной — иначе она рассинхронизируется снова.
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    block = text[text.index(rh.BEGIN):text.index(rh.END)]
    assert "KeyboardLayoutCleaner-*-portable.zip" in text, (
        "команда проверки должна быть версионно-независимой"
    )
    assert re.search(
        r"KeyboardLayoutCleaner-\d+\.\d+\.\d+-portable\.zip", block
    ), "в блоке хешей нет конкретного имени архива"


@pytest.mark.parametrize("command", ["compute", "readme", "notes"])
def test_commands_are_registered(command):
    """Все три команды существуют: иначе опечатка в workflow уйдёт в CI."""
    with pytest.raises(SystemExit) as exit_info:
        rh.main([command, "--help"])
    assert exit_info.value.code == 0

