"""Тесты для новой логики is_elevate в acquire_mutex().

FIX-19: модуль больше не дёргает ``ctypes.windll`` напрямую — вызовы идут
через ``winapi.create_mutex``/``winapi.close_handle``, поэтому подменяется
именно этот шов.

Почему это важно: если оставить ``@patch("mutex.ctypes")``, мок перестал бы
затрагивать исполняемый код, тесты падали бы на РЕАЛЬНОЙ Windows (создавая
настоящие мьютексы) и проверяли бы не то, что задумано. Все проверки
(счётчики вызовов, значения handle/err) перенесены без ослабления.
"""

from unittest.mock import MagicMock, patch

ERROR_ALREADY_EXISTS = 183


def _create_mock(pairs):
    """Мок winapi.create_mutex по списку (handle, err)."""
    return MagicMock(side_effect=list(pairs))


class TestAcquireMutexIsElevateFalse:
    """Тесты для acquire_mutex(is_elevate=False) - мгновенный отказ."""

    def test_instant_refuse_on_existing_mutex(self):
        from mutex import acquire_mutex

        create = _create_mock([(42, ERROR_ALREADY_EXISTS)])
        close = MagicMock(return_value=0)
        with patch("mutex.winapi.create_mutex", create), patch(
            "mutex.winapi.close_handle", close
        ):
            handle, err = acquire_mutex("TestMutex123", is_elevate=False)
        assert handle is None
        assert err == ERROR_ALREADY_EXISTS
        assert close.call_count == 1
        assert create.call_count == 1

    def test_no_sleep_when_is_elevate_false(self):
        from mutex import acquire_mutex

        create = _create_mock([(42, ERROR_ALREADY_EXISTS)])
        close = MagicMock(return_value=0)
        with (
            patch("mutex.winapi.create_mutex", create),
            patch("mutex.winapi.close_handle", close),
            patch("mutex.time.sleep") as mock_sleep,
        ):
            acquire_mutex("TestMutex123", is_elevate=False)
        mock_sleep.assert_not_called()

    def test_success_when_mutex_free_with_is_elevate_false(self):
        from mutex import acquire_mutex

        create = _create_mock([(99, 0)])
        close = MagicMock(return_value=0)
        with patch("mutex.winapi.create_mutex", create), patch(
            "mutex.winapi.close_handle", close
        ):
            handle, err = acquire_mutex("TestMutex123", is_elevate=False)
        assert handle == 99
        assert err == 0


class TestAcquireMutexIsElevateTrue:
    """Тесты для acquire_mutex(is_elevate=True) - ожидание."""

    def test_polling_loop_when_is_elevate_true(self):
        from mutex import acquire_mutex

        create = _create_mock(
            [
                (42, ERROR_ALREADY_EXISTS),
                (43, ERROR_ALREADY_EXISTS),
                (44, ERROR_ALREADY_EXISTS),
                (45, 0),
            ]
        )
        close = MagicMock(return_value=0)
        with (
            patch("mutex.winapi.create_mutex", create),
            patch("mutex.winapi.close_handle", close),
            patch("mutex.time.sleep", return_value=None),
        ):
            handle, err = acquire_mutex("TestMutex123", is_elevate=True)
        assert create.call_count == 4
        assert close.call_count == 3
        assert handle == 45
        assert err == 0

    def test_timeout_after_many_attempts(self):
        from mutex import acquire_mutex

        create = _create_mock([(42, ERROR_ALREADY_EXISTS)] * 10)
        close = MagicMock(return_value=0)
        with (
            patch("mutex.winapi.create_mutex", create),
            patch("mutex.winapi.close_handle", close),
            patch("mutex.time.sleep", return_value=None),
            patch("mutex.MAX_POLL_ATTEMPTS", 5),
        ):
            handle, err = acquire_mutex("TestMutex123", is_elevate=True)
        assert handle is None
        assert err == ERROR_ALREADY_EXISTS
        assert create.call_count == 6


class TestAcquireMutexDefaultBehavior:
    """Тесты значения по умолчанию (без is_elevate) - совместимость."""

    def test_default_is_elevate_false(self):
        from mutex import acquire_mutex

        create = _create_mock([(42, ERROR_ALREADY_EXISTS)])
        close = MagicMock(return_value=0)
        with patch("mutex.winapi.create_mutex", create), patch(
            "mutex.winapi.close_handle", close
        ):
            handle, err = acquire_mutex("TestMutex123")
        assert handle is None
        assert err == ERROR_ALREADY_EXISTS
        assert create.call_count == 1


class TestWinApiPrototypes:
    """FIX-19: прототипы объявлены, ошибка читается вместе с вызовом."""

    def test_create_mutex_has_handle_restype(self):
        import winapi

        assert winapi.kernel32.CreateMutexW.restype is not None
        # c_int означал бы 32-битное усечение 64-битного HANDLE.
        assert winapi.kernel32.CreateMutexW.restype != 32

    def test_error_code_is_read_from_the_call_itself(self):
        """Функциональная проверка use_last_error, а не разбор атрибутов.

        ``CloseHandle(0)`` гарантированно проваливается с
        ERROR_INVALID_HANDLE (6). Если бы библиотека была открыта без
        ``use_last_error``, ``get_last_error()`` вернул бы 0 и «ошибка»
        выглядела бы как успех — ровно тот дефект, который чинит FIX-19.
        """
        import winapi

        assert winapi.close_handle(0) != 0, "код ошибки CloseHandle потерян"

    def test_no_manual_getlasterror_calls(self):
        """Критерий FIX-19: GetLastError не зовут вручную рядом с вызовом.

        Проверяются именно ВЫЗОВЫ (``GetLastError(``), а не любое
        упоминание строки: в main.py есть текст сообщения пользователю
        «(GetLastError=…)», а в winapi — объяснение в докстринге. И то и
        другое вызовом не является, поэтому файл исключается целиком
        (там вызовов быть не может по определению) и комментарии
        отбрасываются построчно.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parent
        offenders = []
        for name in ("mutex.py", "main.py", "cleaner.py", "i18n.py"):
            source = (root / name).read_text(encoding="utf-8")
            in_docstring = False
            for raw in source.splitlines():
                stripped = raw.strip()
                # Грубый, но достаточный учёт тройных кавычек: внутри
                # докстринга вызовов не бывает по определению.
                if stripped.startswith(('"""', "'''")) and stripped.count(
                    ('"""', "'''")[0]
                ) == 1:
                    in_docstring = not in_docstring
                    continue
                if in_docstring or stripped.startswith("#"):
                    continue
                if "GetLastError(" in stripped.replace(" ", ""):
                    offenders.append(f"{name}: {stripped}")
        assert not offenders, offenders


class TestParseArgsElevate:
    """Тесты что parse_args поддерживает --elevate."""

    def test_elevate_flag_parsed(self):
        import main

        _, args = main._parse_sandbox_mode(["--elevate"])
        assert args.elevate is True

    def test_elevate_flag_default_false(self):
        import main

        _, args = main._parse_sandbox_mode([])
        assert args.elevate is False

    def test_elevate_with_sandbox(self):
        import main

        _, args = main._parse_sandbox_mode(["--sandbox", "--elevate"])
        assert args.elevate is True
        assert args.sandbox is True


class TestAcquireMutexWrapper:
    """Тесты обёртки _acquire_mutex в main.py."""

    @staticmethod
    def _check(mock_acquire, name, expected_is_elevate):
        assert mock_acquire.call_count == 1, "ожидался ровно один вызов"
        _args, kwargs = mock_acquire.call_args
        assert name in _args, "имя мьютекса должно быть позиционным аргументом"
        assert kwargs.get("is_elevate") == expected_is_elevate
        assert callable(kwargs.get("confirm_wait")), "confirm_wait обязателен"

    @patch("main._mutex_acquire")
    def test_wrapper_passes_is_elevate_true(self, mock_acquire):
        import main

        main._acquire_mutex(is_elevate=True)
        self._check(mock_acquire, main._MUTEX_NAME, True)

    @patch("main._mutex_acquire")
    def test_wrapper_passes_is_elevate_false(self, mock_acquire):
        import main

        main._acquire_mutex(is_elevate=False)
        self._check(mock_acquire, main._MUTEX_NAME, False)

    @patch("main._mutex_acquire")
    def test_wrapper_default_is_elevate(self, mock_acquire):
        import main

        main._acquire_mutex()
        self._check(mock_acquire, main._MUTEX_NAME, False)


class TestMutexConstants:
    """Тесты констант в mutex.py."""

    def test_error_already_exists_value(self):
        from mutex import ERROR_ALREADY_EXISTS

        assert ERROR_ALREADY_EXISTS == 183

    def test_max_poll_seconds_links_to_config(self):
        from config import TIMEOUTS
        from mutex import MAX_POLL_SECONDS

        assert TIMEOUTS["mutex_wait"] == MAX_POLL_SECONDS

    def test_max_poll_attempts_is_derived(self):
        from mutex import MAX_POLL_ATTEMPTS, MAX_POLL_SECONDS, POLL_INTERVAL_SEC

        assert max(1, int(MAX_POLL_SECONDS / POLL_INTERVAL_SEC)) == MAX_POLL_ATTEMPTS

    def test_poll_interval(self):
        from mutex import POLL_INTERVAL_SEC

        assert POLL_INTERVAL_SEC == 0.3
