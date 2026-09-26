"""FIX-10, шаг 1: границы модулей после выноса backup.py.

Рефакторинг без смены поведения опасен не самим переносом, а тем, что
следующий человек (или следующая правка) не заметит дубля и не поймёт,
какой модуль исполняется. Тесты ниже фиксируют КОНТРАКТ границы:

* ``cleaner`` реэкспортирует публичные имена бэкапа — внешний код
  (``cleaner.backup_registry``) продолжает работать;
* определения живые в ``backup`` и ИМЕННО ТАМ, а не в ``cleaner``
  (иначе появился бы второй источник истины);
* модули, работающие с реестром, перечислены явно — новый модуль,
  забытый в этом списке, обнаруживается этим же тестом.
"""

import ast
import re
from pathlib import Path

import backup
import cleaner
import langlist
import mutate

ROOT = Path(__file__).resolve().parent

# Публичный API бэкапа, который обязан остаться доступен из cleaner.
PUBLIC_BACKUP_API = (
    "BackupError",
    "backup_registry",
    "get_backup_dir",
    "is_admin",
    "list_backups",
    "restore_registry_backup",
)

# Публичные предикаты/мутаторы, которые cleaner обязан реэкспортировать
# после FIX-10 шага 2. Список не пустой намеренно: подмена cleaner._value_matches
# после переноса не действует, и «тест на дрейф» проверял бы ничто.
PUBLIC_MUTATE_API = (
    "_build_manifest",
    "_clean_intl_profile",
    "_clean_preload_keys",
    "_clean_substitutes_keys",
    "_snapshot_values_before_delete",
    "_value_matches",
)

# Публичные функции списка языков, которые cleaner обязан реэкспортировать
# после FIX-10 шага 3.
PUBLIC_LANGLIST_API = (
    "_backup_language_list",
    "_run_cleanup_script",
    "_sync_language_list_via_powershell",
    "restore_language_list",
)

# Модули, работающие с реестром: для них тесты подменяют winreg.
REGISTRY_MODULES = ("backup", "cleaner", "mutate", "scanner", "settings")

# Модули, работающие со списком языков: для них тесты подменяют run_hidden.
# langlist НЕ входит в REGISTRY_MODULES: он не обращается к реестру.
LANGLIST_MODULES = ("cleaner", "langlist")

# Что должно лежать в backup, а не в cleaner.
MOVED_TO_BACKUP = (
    *PUBLIC_BACKUP_API,
    "_ROOT_CONST",
    "_branch_exists",
    "_check_reg_backup_file",
    "_export_single_key_via_reg",
    "_release_reserved_backup",
    "_reserve_backup_file",
    "REG_BACKUP_MAX_BYTES",
    "REG_BACKUP_MIN_BYTES",
)

# Модули, которым в тестах подменяют winreg/_ROOT_CONST. Пополняется
# вместе с новыми модулями, читающими реестр.
REGISTRY_MODULES = ("backup", "cleaner", "mutate", "scanner", "settings")

# Модули, работающие со списком языков, подменяют run_hidden — это отдельный
# список (LANGLIST_MODULES): ставить langlist в REGISTRY_MODULES было бы
# неверно, он к реестру не обращается.

# Хелперы подмены winreg в тестах. Каждый обязан звать conftest
# .registry_modules(), а не перечислять модули сам.
REGISTRY_PATCH_HELPERS = (
    "_install_fake_winreg",
    "_patch_reg",
    "_patch",
    "_ensure_main",
)


def _top_level_names(path: Path) -> set[str]:
    """Имена верхнего уровня: функции, классы и присваивания.

    Учитываются ОБА вида присваиваний: ``X = 1`` (``Assign``) и
    аннотированное ``X: int = 1`` (``AnnAssign``). Первый вариант разбора
    проглатывал аннотированные константы — ровно тот дефект, который
    уже ловил тест FIX-18 на словаре ``error_messages``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            names.add(node.target.id)
    return names


def test_cleaner_reexports_public_backup_api() -> None:
    """Обратная совместимость: cleaner.backup_registry и далее работает."""
    for name in PUBLIC_BACKUP_API:
        assert hasattr(cleaner, name), f"cleaner потерял {name}"
        # Реэкспорт, а не копия: реализация должна быть из backup.
        assert getattr(cleaner, name) is getattr(backup, name), name


def test_cleaner_reexports_mutate_api() -> None:
    """Предикаты и мутаторы остаются доступны через cleaner (FIX-10 шаг 2)."""
    for name in PUBLIC_MUTATE_API:
        assert hasattr(cleaner, name), f"cleaner потерял {name}"
        assert getattr(cleaner, name) is getattr(mutate, name), (
            f"{name} в cleaner — не реэкспорт mutate, а второй определитель"
        )


def test_cleaner_reexports_langlist_api() -> None:
    """Функции списка языков остаются доступны через cleaner (FIX-10 шаг 3)."""
    for name in PUBLIC_LANGLIST_API:
        assert hasattr(cleaner, name), f"cleaner потерял {name}"
        assert getattr(cleaner, name) is getattr(langlist, name), (
            f"{name} в cleaner — не реэкспорт langlist, а второй определитель"
        )


def test_moved_definitions_live_only_in_backup() -> None:
    """Нет второй копии: определения переехали, а не продублированы."""
    in_backup = _top_level_names(ROOT / "backup.py")
    in_cleaner = _top_level_names(ROOT / "cleaner.py")
    for name in MOVED_TO_BACKUP:
        assert name in in_backup, f"{name} отсутствует в backup.py"
        assert name not in in_cleaner, (
            f"{name} объявлен и в cleaner.py — получится два источника истины"
        )


def test_registry_helpers_are_not_hardcoded() -> None:
    """Хелперы подмены winreg обязаны брать модули динамически.

    FIX-19/FIX-10 показали одну ловушку четыре раза, и на шаге 4 она впервые
    ударила по НАСТОЯЩЕМУ реестру машины, на которой шли тесты: ``settings``
    выпал из перечисления, и ``disable_language_sync()`` выполнился против
    реального winreg.

    Прежний вариант этого теста сравнивал хелперы с захардкоженным
    ``EXPECTED_PATCH_TARGETS`` — то есть проверял, что список в тесте совпал
    со списком в тесте. Новый модуль в этом случае проходил бы молча, а
    именно это и было нужно ловить. Теперь проверяется другое: хелпер
    обязан звать ``conftest.registry_modules()``, а в его теле не должно
    быть литеральных имён модулей приложения.
    """
    from conftest import registry_modules

    known = {module.__name__ for module in registry_modules()}
    for test_name in ("test_scanner.py", "test_manifest_drift.py",
                      "test_review_regressions.py", "conftest.py"):
        source = (ROOT / test_name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        helpers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name in REGISTRY_PATCH_HELPERS
        ]
        assert helpers, f"{test_name}: не найден ни один хелпер подмены"
        for helper in helpers:
            called = {
                n.func.id
                for n in ast.walk(helper)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            assert "registry_modules" in called, (
                f"{test_name}:{helper.name} не зовёт registry_modules() — "
                f"новый модуль с реестром снова выпадет из подмены"
            )
            # Запрещено НЕ любое упоминание модуля, а литеральный модуль в
            # вызове подмены winreg: patch.object(scanner, "winreg", fake).
            # Упоминание scanner в той же функции законно — им подменяют
            # run_hidden, и это другой объект.
            literals: set[str] = set()
            for node in ast.walk(helper):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                target_attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
                if target_attr != "object" or not node.args[1:]:
                    continue
                attr = node.args[1]
                if not isinstance(attr, ast.Constant) or attr.value != "winreg":
                    continue
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in known:
                    literals.add(first.id)
            assert not literals, (
                f"{test_name}:{helper.name} подменяет winreg у перечисленных "
                f"вручную модулей {sorted(literals)} — новый модуль выпадет"
            )


def test_registry_helpers_cover_settings() -> None:
    """Регрессия шага 4: settings обязан попадать в подмену winreg.

    Отдельная проверка, а не часть предыдущей: ``registry_modules()`` находит
    модуль по наличию глобального ``winreg``, и если перенос вдруг сделает
    импорт в settings ленивым, функция вернёт список БЕЗ него — тихо и
    правильно с точки зрения своей логики. Тест фиксирует ожидаемое.
    """
    from conftest import registry_modules

    assert "settings" in {m.__name__ for m in registry_modules()}, (
        "settings держит winreg, но registry_modules() его не вернул — "
        "disable/enable_language_sync уйдут в настоящий реестр"
    )
    assert "settings" in REGISTRY_MODULES


def test_language_list_patches_target_langlist() -> None:
    """Подмены PS-вызовов списка языков идут в langlist, а не в cleaner.

    Шаг 3 FIX-10 перенёс ``restore_language_list`` и компанию в
    ``langlist.py``. Подмена ``cleaner.run_hidden`` после переноса не
    действует: тест либо звал бы настоящий PowerShell, либо проверял бы
    пустоту. Проверяется текст исходника: остаточные подмены
    ``cleaner.run_hidden`` рядом с вызовами списка языков запрещены.
    """
    for test_name in ("test_scanner.py", "test_restore_powershell.py",
                      "test_review_regressions.py", "test_manifest_drift.py"):
        source = (ROOT / test_name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            body_names = {
                n.id for n in ast.walk(node) if isinstance(n, ast.Name)
            }
            if not body_names & {"restore_language_list", "_run_cleanup_script"}:
                continue
            # Именно splitlines(): обход символов строки не нашёл бы
            # подстроку 'run_hidden' никогда, и проверка молча проходила бы.
            for line in (ast.get_source_segment(source, node) or "").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if (
                    "run_hidden" in stripped
                    and "cleaner" in stripped
                    and "langlist" not in stripped
                ):
                    offenders.append(f"{test_name}:{node.lineno}: {stripped}")
        assert not offenders, offenders


class TestPackagingCoversEveryModule:
    """FIX-29: ни один модуль приложения не должен потеряться при сборке.

    PyInstaller анализирует граф импортов статически. Модуль, который
    никто не импортирует по имени (только упоминает в комментарии или
    достаётся по строке), в сборку не попадёт — и упадёт не в CI, а у
    пользователя на exe.

    Именно так едва не случилось с ``capabilities``: он импортируется из
    пяти модулей, но перечисление hiddenimports в спеке ведётся руками и
    про него забыли. Тест делает это невозможным.
    """

    # Модули, которых НЕТ в поставке намеренно. Это инструменты разработки
    # и сборки, а не часть приложения; тянуть их в exe незачем.
    # Список явный, а не «всё подряд»: иначе проверка тихо перестанет
    # что-либо проверять, стоит добавить служебный скрипт.
    DEV_ONLY_MODULES = frozenset(
        {
            "sign_exe",  # подпись exe (Windows SDK), вызывается вручную
        }
    )

    @staticmethod
    def _app_modules() -> set[str]:
        return {
            p.stem
            for p in ROOT.glob("*.py")
            if not p.stem.startswith(("test_", "conftest", "__", "setup"))
        } - TestPackagingCoversEveryModule.DEV_ONLY_MODULES

    @staticmethod
    def _imports_of(module: str) -> set[str]:
        """Модули верхнего уровня, импортируемые данным (статически)."""
        source = (ROOT / f"{module}.py").read_text(encoding="utf-8")
        found: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        return found

    @staticmethod
    def _hidden_imports() -> set[str]:
        spec = (ROOT / "keyboard_cleaner.spec").read_text(encoding="utf-8")
        start = spec.index("hiddenimports=[")
        end = spec.index("]", start)
        block = spec[start:end]
        return set(re.findall(r'"([a-z_][a-z0-9_]*)"', block))

    def test_every_module_reachable_from_main_or_listed_explicitly(self):
        app = self._app_modules()
        listed = self._hidden_imports()
        # Транзитивное замыкание от точки входа: PyInstaller дотянется до
        # этих модулей сам, перечислять их вручную не нужно.
        reachable: set[str] = set()
        frontier = ["main"]
        while frontier:
            current = frontier.pop()
            if current in reachable or current not in app:
                continue
            reachable.add(current)
            frontier.extend(self._imports_of(current) & app)
        missing = app - reachable - listed
        assert not missing, (
            f"Модули не попадут в сборку: {sorted(missing)}. "
            "Добавьте их в hiddenimports спекы или импортируйте по имени."
        )

    def test_capabilities_is_listed_explicitly(self):
        """capabilities перечисляется руками: на него нельзя полагаться."""
        assert "capabilities" in self._hidden_imports()
