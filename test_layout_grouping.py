"""FIX-34: одна строка списка = одна раскладка.

Проблема, которую закрывает файл. Windows хранит одну и ту же раскладку в
разных форматах: ``00000419`` в Preload и ``68748313`` (HKL 0x04190419) в
CTF. Сканер показывал каждую форму отдельной строкой, из-за чего список
раздувался вдвое, а вторая строка оставалась с кодом вместо названия:
пользователь видел «Layout (04190419)» и не понимал, что это за раскладка
и трогать ли её.

Инварианты, которые здесь защищаются:

1. Строка списка — канонический KLID; сырой код не теряется, а живёт
   в поле ``form`` и показывается в подробностях.
2. Строка не врёт: если сканер показал запись, план удаления по этому же
   KLID обязан её покрывать. Объединение без этого было бы враньём.
3. Статус прямо говорит, фантом это или рабочая раскладка.

Тесты не трогают живой реестр: используется FakeWinreg, а PowerShell
заглушен (обход реальной системы — только на VM).
"""

import pytest

import cleaner
import layout_ids
import scanner
from conftest import FakeWinreg, fake_root_consts, registry_modules

CTF = "Software\\Microsoft\\CTF"
PRELOAD = "Keyboard Layout\\Preload"

# Ключ профиля TSF: 0x<langid> — язык, KLID берётся из младшего слова
GUID = "{34745C63-B2F0-4784-8B67-5E12C8701A31}"


def _install_fake_winreg(monkeypatch, fake):
    """Подменить winreg во всех модулях реестра + заглушить PowerShell."""
    roots = fake_root_consts(fake)
    for module in registry_modules():
        monkeypatch.setattr(module, "winreg", fake)
        if hasattr(module, "_ROOT_CONST"):
            monkeypatch.setattr(module, "_ROOT_CONST", roots)

    def _no_powershell(*args, **kwargs):
        # OSError, а не AssertionError: сканер трактует OSError как
        # «PowerShell недоступен» и продолжает. Исключение, которое не
        # поймано, сорвало бы тест на ровном месте.
        raise OSError("PowerShell недоступен в тесте")

    monkeypatch.setattr(scanner, "run_hidden", _no_powershell)
    return fake


def _seed_both_forms(fake, klid="00000419", langid="00000419"):
    """Одна раскладка в двух формах: KLID в Preload и HKL в CTF."""
    fake.set(fake.HKEY_CURRENT_USER, PRELOAD, values={"1": klid})
    # CTF пишет HKL десятичным числом: (LANGID << 16) | base_low
    hkl_decimal = str((int(langid, 16) << 16) | int(klid[4:], 16))
    fake.set(
        fake.HKEY_CURRENT_USER,
        f"{CTF}\\SortOrder\\AssemblyItem\\0x{langid}\\{GUID}\\00000000",
        values={"KeyboardLayout": hkl_decimal},
    )
    return fake


def _plan_hits(plan):
    """Все значения и профильные ключи из плана удаления."""
    return [
        hit
        for entry in plan["branches"].values()
        for hit in (entry.get("values") or []) + (entry.get("profile_keys") or [])
    ]


class TestOneRowPerLayout:
    """Одна раскладка = одна строка, сырые коды сохранены."""

    def test_hkl_record_joins_the_klid_row(self, monkeypatch):
        _install_fake_winreg(monkeypatch, _seed_both_forms(FakeWinreg()))
        result = scanner.scan_keyboard_layouts()
        # Раньше было две строки: «00000419» и «04190419»
        assert list(result) == ["00000419"], result
        assert len(result["00000419"]) == 2

    def test_raw_forms_are_preserved(self, monkeypatch):
        _install_fake_winreg(monkeypatch, _seed_both_forms(FakeWinreg()))
        result = scanner.scan_keyboard_layouts()
        forms = {loc["form"] for loc in result["00000419"]}
        # Обе исходные формы на месте: при объединении ничего не выброшено
        assert forms == {"00000419", "68748313"}

    def test_english_hkl_joins_english_row(self, monkeypatch):
        fake = FakeWinreg()
        fake.set(fake.HKEY_CURRENT_USER, PRELOAD, values={"1": "00000409"})
        fake.set(
            fake.HKEY_CURRENT_USER,
            f"{CTF}\\SortOrder\\AssemblyItem\\0x00000409\\{GUID}\\00000000",
            values={"KeyboardLayout": "67699721"},  # 0x04090409
        )
        _install_fake_winreg(monkeypatch, fake)
        result = scanner.scan_keyboard_layouts()
        assert list(result) == ["00000409"], result

    def test_german_leftover_gets_real_name(self, monkeypatch):
        """04070407 -> German (00000407), а не «Layout (04070407)».

        Запись 0x04070407 в CTF — служебная запись немецкой раскладки.
        Свёрнутая до 00000407, она находится в каталоге Windows.
        """
        fake = FakeWinreg()
        fake.set(
            fake.HKEY_CURRENT_USER,
            f"{CTF}\\SortOrder\\AssemblyItem\\0x00000407\\{GUID}\\00000000",
            values={"KeyboardLayout": "67568647"},  # 0x04070407
        )
        _install_fake_winreg(monkeypatch, fake)
        result = scanner.scan_keyboard_layouts()
        assert list(result) == ["00000407"], result
        assert result["00000407"][0]["form"] == "67568647"

    def test_phantom_keeps_its_own_identity(self, monkeypatch):
        """Фантом d001dead не сворачивается в 0000dead.

        Сворачивать можно только настоящие HKL из CTF: у фантома в коде нет
        старшего слова типа клавиатуры, и сворачивание отняло бы у него имя.
        """
        fake = FakeWinreg()
        fake.set(fake.HKEY_CURRENT_USER, PRELOAD, values={"1": "d001dead"})
        _install_fake_winreg(monkeypatch, fake)
        result = scanner.scan_keyboard_layouts()
        assert list(result) == ["d001dead"], result


class TestRowDoesNotLie:
    """Строка, которую нашёл сканер, обязана быть удалима по её KLID."""

    def test_plan_covers_every_record_of_the_merged_row(self, monkeypatch):
        _install_fake_winreg(monkeypatch, _seed_both_forms(FakeWinreg()))
        scanner.scan_keyboard_layouts()
        plan = cleaner.plan_layout_removal("00000419")
        # Запись CTF найдена сканером и обязана быть в плане по этому же KLID
        assert any("AssemblyItem" in hit for hit in _plan_hits(plan)), plan["branches"]

    def test_plan_matches_under_admin_rights(self, monkeypatch):
        """То же при полных правах: HKU-ветка не должна ломать паритет."""
        _install_fake_winreg(monkeypatch, _seed_both_forms(FakeWinreg()))
        monkeypatch.setattr(cleaner, "is_admin", lambda: True)
        plan = cleaner.plan_layout_removal("00000419")
        assert any("AssemblyItem" in hit for hit in _plan_hits(plan)), plan["branches"]


class TestHklHelpers:
    """HKL-формы: сворачиваются и матчатся по младшему слову."""

    def test_hkl_to_klid_is_idempotent(self):
        assert layout_ids.hkl_to_klid("00000419") == "00000419"

    def test_hkl_to_klid_drops_keyboard_type_word(self):
        assert layout_ids.hkl_to_klid("04070407") == "00000407"
        assert layout_ids.hkl_to_klid("04190419") == "00000419"

    # Про фантома: hkl_to_klid ничего не знает о контексте и свернёт любой
    # токен с ненулевым младшим словом. Защита живёт в вызывающем коде —
    # он сворачивает только записи ветки CTF. Инвариант проверяется на
    # уровне скана в TestOneRowPerLayout.test_phantom_keeps_its_own_identity.

    def test_matches_any_keyboard_type(self):
        # Одна и та же русская раскладка при разных типах клавиатуры
        for hkl in ("04190419", "04090419"):
            assert layout_ids.hkl_matches_klid(hkl, {"00000419"})

    def test_does_not_match_other_layout(self):
        assert not layout_ids.hkl_matches_klid("0409040c", {"00000419"})

    def test_plain_klid_is_not_treated_as_hkl(self):
        # 00000419 — обычный KLID, не HKL; его ловит обычное сравнение
        assert not layout_ids.hkl_matches_klid("00000419", {"00000419"})


class TestStatusLabel:
    """Статус отвечает на вопрос «фантом или рабочая раскладка?»."""

    def test_preload_and_language_list_means_in_use(self):
        import main

        data = {
            "00000419": [
                {"path": f"HKCU\\{PRELOAD}", "value": "00000419", "form": "00000419"},
                {
                    "path": "PowerShell\\Get-WinUserLanguageList",
                    "value": "LanguageTag=ru, KLID=00000419",
                    "form": "00000419",
                },
            ]
        }
        key = main._layout_status_key("00000419", data, {"00000419"})
        assert key == main._STATUS_IN_LIST

    def test_ctf_only_is_leftover_not_active_layout(self):
        import main

        data = {
            "00000407": [
                {
                    "path": f"HKCU\\{CTF}",
                    "value": "SortOrder\\AssemblyItem\\0x00000407\\K\\K=67568647",
                    "form": "67568647",
                }
            ]
        }
        # Языка нет в списке языков Windows → перед нами остаток, а не кеш
        # рабочей раскладки
        key = main._layout_status_key("00000407", data, {"00000409"})
        assert key == main._STATUS_CACHE_ONLY

    def test_ctf_only_with_active_language_is_cache(self, monkeypatch):
        import main

        data = {
            "00000419": [
                {
                    "path": f"HKCU\\{CTF}",
                    "value": "Assemblies\\0x00000419\\K\\KeyboardLayout=68748313",
                    "form": "68748313",
                }
            ],
            "00000409": [
                {
                    "path": "PowerShell\\Get-WinUserLanguageList",
                    "value": "LanguageTag=en-US, KLID=00000409",
                    "form": "00000409",
                }
            ],
        }
        # Тот же LANGID есть в списке языков → кеш активной раскладки,
        # Windows пересоздаст его сам. Называть остатком нельзя.
        monkeypatch.setattr(main, "_active_lang_cache", lambda k, d: True)
        key = main._layout_status_key("00000419", data, {"00000419"})
        assert key == main._STATUS_ACTIVE_CACHE

    def test_in_settings_but_not_switchable(self):
        import main

        data = {
            "00000419": [
                {"path": f"HKCU\\{PRELOAD}", "value": "00000419", "form": "00000419"},
            ]
        }
        # В Preload есть, а в сессии Windows — нет
        key = main._layout_status_key("00000419", data, {"00000409"})
        assert key == main._STATUS_NOT_SWITCHABLE

    def test_unreadable_session_is_not_diagnosed(self):
        """None = состояние не прочитано. Это НЕ «раскладка сломана»."""
        import main

        data = {
            "00000419": [
                {"path": f"HKCU\\{PRELOAD}", "value": "00000419", "form": "00000419"},
            ]
        }
        key = main._layout_status_key("00000419", data, None)
        assert key == main._STATUS_IN_LIST


@pytest.mark.parametrize("locale", ["ru", "en", "de", "es", "pt", "zh"])
def test_status_keys_translated_in_every_locale(locale):
    """Метка статуса обязана быть переведена, а не показана ключом."""
    import i18n

    previous = i18n.current_lang
    i18n.set_language(locale)
    try:
        for key in ("status_in_list", "status_cache_only", "status_not_switchable"):
            assert i18n.fmt(key) not in ("", key), f"{locale}: ключ {key} пуст"
    finally:
        i18n.set_language(previous)
