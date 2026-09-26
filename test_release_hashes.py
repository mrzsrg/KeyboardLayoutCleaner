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


@pytest.mark.parametrize("command", ["compute", "readme", "notes"])
def test_commands_are_registered(command):
    """Все три команды существуют: иначе опечатка в workflow уйдёт в CI."""
    with pytest.raises(SystemExit) as exit_info:
        rh.main([command, "--help"])
    assert exit_info.value.code == 0

