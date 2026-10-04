"""
test_confirm_dialog.py — регрессии локализации диалога подтверждения (FIX-41).

Инцидент: приложение умеет переключать язык интерфейса (English, Русский,
Español, Deutsch, 简体中文, Português), но окно подтверждения удаления
открывалось через ``tkinter.messagebox.askyesno``. Кнопки «Да»/«Нет» в нём
рисует САМА Windows, поэтому они всегда были на языке системы — при
выбранном English окно говорило на двух языках сразу.

Тесты проверяют контракт, а не конкретную реализацию окна:
  - в локалях есть переведённые подписи кнопок;
  - main.py больше НЕ зовёт messagebox.askyesno (иначе баг вернётся);
  - ask_confirm подставляет подписи из активной локали, а не из en.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

import i18n

BASE = Path(__file__).parent
LOCALES_DIR = BASE / "locales"
ALL_LOCALES = sorted(p.stem for p in LOCALES_DIR.glob("*.json"))


# ---------------------------------------------------------------------------
# Ключи локализации
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ALL_LOCALES)
@pytest.mark.parametrize("key", ["dlg_btn_yes", "dlg_btn_no"])
def test_confirm_button_keys_exist(lang: str, key: str) -> None:
    """Подписи кнопок диалога обязаны быть в каждой локали.

    Без них i18n.fmt вернул бы сам ключ, и пользователь увидел бы в окне
    «dlg_btn_yes» — ровно та ошибка, ради устранения которой всё затевалось.
    """
    data = json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    assert key in data, f"{lang}: нет ключа {key}"
    assert data[key].strip(), f"{lang}: {key} пуст"


@pytest.mark.parametrize("lang", ALL_LOCALES)
def test_confirm_buttons_are_translated(lang: str) -> None:
    """Кнопки обязаны быть переведены, а не оставлены на английском.

    Слово-«ложный друг» исключено явно и поимённо: испанское «No» и
    английское «No» — одно и то же слово. Список закрыт, а не «всё, что не
    совпало с en»: иначе тест тихо пропускал бы непереведённую строку,
    стоив ему смысла.
    """
    if lang == "en":
        pytest.skip("базовая локаль — источник истины")
    # Подписи, которые в этом языке законно совпадают с английскими.
    same_spelling = {"es": {"dlg_btn_no"}}
    english = json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8"))
    data = json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    for key in ("dlg_btn_yes", "dlg_btn_no"):
        if key in same_spelling.get(lang, set()):
            continue
        assert data[key].lower() != english[key].lower(), (
            f"{lang}: {key} осталась на английском ({data[key]!r}). Если это "
            "законное совпадение — добавь ключ в same_spelling с обоснованием."
        )


def test_confirm_buttons_differ_within_locale() -> None:
    """Да/Нет — разные кнопки; одинаковые подписи читались бы как опечатка."""
    for lang in ALL_LOCALES:
        data = json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        assert data["dlg_btn_yes"] != data["dlg_btn_no"], (
            f"{lang}: подтверждение и отмена подписаны одинаково"
        )


# ---------------------------------------------------------------------------
# main.py не возвращается к системным кнопкам
# ---------------------------------------------------------------------------


def test_main_has_no_askyesno_calls() -> None:
    """``askyesno`` в main.py — это возврат бага с подписями языка системы.

    Проверяется исходником, а не аст-обходом вызовов: важно, что в файле
    нет ни одного упоминания, потому что переименованный импорт или
    ``getattr``-обёртка тоже дали бы системные кнопки.
    """
    source = (BASE / "main.py").read_text(encoding="utf-8")
    assert "askyesno" not in source, (
        "main.py снова использует messagebox.askyesno — кнопки «Да»/«Нет» "
        "вернутся на языке системы (FIX-41)"
    )


def test_main_source_is_valid_python() -> None:
    """main.py разбирается: проверка выше не должна ломаться на опечатке."""
    ast.parse((BASE / "main.py").read_text(encoding="utf-8"))


def test_ask_confirm_is_used_by_main() -> None:
    """main.py обязан использовать наш диалог — иначе тест выше проходит
    вхолостую, а пользователь по-прежнему видит системное окно."""
    source = (BASE / "main.py").read_text(encoding="utf-8")
    assert "ask_confirm" in source, "main.py не использует ask_confirm"
# ---------------------------------------------------------------------------
# ask_confirm берёт подписи из активной локали
# ---------------------------------------------------------------------------


def _capture_button_labels(**kwargs) -> list[str]:
    """Вызвать настоящий gui_widgets.ask_confirm и вернуть подписи кнопок.

    Модули грузятся ЗДЕСЬ, внутри функции, а не в шапке теста. Причина
    именно в порядке: gui_widgets и ui_theme — долгоживущие модули. Если
    импортировать их до подмены sys.modules["customtkinter"], они навсегда
    запомнят настоящий customtkinter, и чужие GUI-тесты (main.py зовёт
    theme.body_font) потом упадут с «no default root window» — уже в другом
    файле и по причине, никак не связанной с их логикой.
    """
    import sys

    captured: list[str] = []
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setitem(sys.modules, "customtkinter", _mocked_ctk(captured))
        monkey.delitem(sys.modules, "gui_widgets", raising=False)
        import gui_widgets

        gui_widgets.ask_confirm(_FakeParent(), "Заголовок", "Текст", **kwargs)
    finally:
        # Возвращаем МОДУЛЬ и CUSTOMTKINTER в sys.modules как были: иначе
        # следующий тест унаследовал бы мок, у которого вырезан CTkToplevel,
        # и упал бы с «no default root window» в другом файле.
        _restore_module(monkey, "gui_widgets")
        _restore_module(monkey, "customtkinter")
    return captured


def _restore_module(monkey: pytest.MonkeyPatch, name: str) -> None:
    """Убрать подменённый модуль из sys.modules, чтобы он грузился заново.

    Кэш импорта Python кэширует и МОДУЛЬ, и его внешние ссылки. Удаление
    ключа заставляет следующий ``import`` построить модуль заново — уже с
    настоящим customtkinter. Именно это и требуется: подмена нужна была
    ровно на время вызова ask_confirm.
    """
    monkey.delitem(sys.modules, name, raising=False)


def _mocked_ctk(captured: list[str]):
    """Мок customtkinter, который помнит подписи кнопок и умеет Toplevel."""
    from conftest import _make_ctk

    fake_ctk = _make_ctk()
    real_button = fake_ctk.CTkButton

    class _Button(real_button):
        def __init__(self, *args, **kw):
            captured.append(kw.get("text"))
            super().__init__(*args, **kw)

    class _Toplevel(_FakeParent):
        """CTkToplevel нет в общем моке conftest — окно диалога нужно."""

        def title(self, _text):
            return None

        def geometry(self, _text):
            return None

        def minsize(self, *args):
            return None

        def configure(self, **kwargs):
            return None

        def bind(self, *_args, **_kwargs):
            return None

        def protocol(self, *_args, **_kwargs):
            return None

        def destroy(self):
            return None

    fake_ctk.CTkButton = _Button
    fake_ctk.CTkToplevel = _Toplevel
    return fake_ctk


class _FakeParent:
    """Минимальный родитель для диалога."""

    def __init__(self, *_args, **_kwargs):
        pass

    def transient(self, _child):
        return None

    def after(self, _ms, _func):
        return None

    def wait_window(self, _child):
        return None


@pytest.mark.parametrize("lang", ALL_LOCALES)
def test_ask_confirm_uses_active_locale(lang: str) -> None:
    """Подписи берутся из i18n, а не из en.

    Проверяем, какие строки ПЕРЕДАЛИ в кнопки, не поднимая настоящего окна.
    """
    previous = i18n.current_lang
    try:
        i18n.set_language(lang)
        data = json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        labels = _capture_button_labels()
    finally:
        i18n.set_language(previous)

    assert data["dlg_btn_yes"] in labels, f"{lang}: нет кнопки «{data['dlg_btn_yes']}»"
    assert data["dlg_btn_no"] in labels, f"{lang}: нет кнопки «{data['dlg_btn_no']}»"


def test_ask_confirm_custom_labels_win() -> None:
    """Явные подписи (например, «Восстановить») важнее локализованных."""
    labels = _capture_button_labels(
        confirm_text="Восстановить",
        cancel_text="Отмена",
    )
    assert "Восстановить" in labels
    assert "Отмена" in labels


# ---------------------------------------------------------------------------
# Подпись галочки не вводит в заблуждение
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ALL_LOCALES)
def test_cloud_checkbox_label_does_not_claim_onedrive(lang: str) -> None:
    """Галочка пишет SettingSync\\Groups\\Language — это НЕ OneDrive.

    OneDrive синхронизирует файлы и к раскладкам отношения не имеет: подпись
    обещала пользователю блокировку OneDrive, которой в приложении нет, и
    поэтому вопрос «что делает галочка» не имел ответа.
    """
    data = json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    label = data["chk_block_cloud_sync"].lower()
    assert "onedrive" not in label, f"{lang}: подпись упоминает OneDrive"


def test_settings_module_does_not_touch_onedrive() -> None:
    """Код блокировки не ходит в OneDrive — подпись обязана соответствовать."""
    source = (BASE / "settings.py").read_text(encoding="utf-8")
    assert "onedrive" not in source.lower()


def test_settings_sync_group_is_the_documented_key() -> None:
    """Блокируется ровно та ветка, которой управляет флажок в Windows.

    «Языковые предпочитания и словарь» на странице резервного копирования
    Windows — это группа Language в ветке SettingSync. Подпись про
    синхронизацию с учётной записью верна именно для этой ветки.
    """
    import settings

    assert settings._SETTINGSYNC_GROUPS_SUBKEY.endswith(
        "SettingSync\\Groups\\Language"
    )
