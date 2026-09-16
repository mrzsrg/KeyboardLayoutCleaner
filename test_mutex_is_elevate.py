"""Тесты для новой логики is_elevate в acquire_mutex()."""

from unittest.mock import MagicMock, patch


class TestAcquireMutexIsElevateFalse:
    """Тесты для acquire_mutex(is_elevate=False) - мгновенный отказ."""

    @patch("mutex.ctypes")
    def test_instant_refuse_on_existing_mutex(self, mock_ctypes):
        from mutex import acquire_mutex

        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42
        mock_kernel32.GetLastError.return_value = 183
        mock_ctypes.windll.kernel32 = mock_kernel32
        handle, err = acquire_mutex("TestMutex123", is_elevate=False)
        assert handle is None
        assert err == 183
        assert mock_kernel32.CloseHandle.call_count == 1
        assert mock_kernel32.CreateMutexW.call_count == 1

    @patch("mutex.ctypes")
    def test_no_sleep_when_is_elevate_false(self, mock_ctypes):
        from mutex import acquire_mutex

        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42
        mock_kernel32.GetLastError.return_value = 183
        mock_ctypes.windll.kernel32 = mock_kernel32
        with patch("mutex.time.sleep") as mock_sleep:
            acquire_mutex("TestMutex123", is_elevate=False)
        mock_sleep.assert_not_called()

    @patch("mutex.ctypes")
    def test_success_when_mutex_free_with_is_elevate_false(self, mock_ctypes):
        from mutex import acquire_mutex

        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 99
        mock_kernel32.GetLastError.return_value = 0
        mock_ctypes.windll.kernel32 = mock_kernel32
        handle, err = acquire_mutex("TestMutex123", is_elevate=False)
        assert handle == 99
        assert err == 0


class TestAcquireMutexIsElevateTrue:
    """Тесты для acquire_mutex(is_elevate=True) - ожидание."""

    @patch("mutex.ctypes")
    def test_polling_loop_when_is_elevate_true(self, mock_ctypes):
        from mutex import acquire_mutex

        mock_kernel32 = MagicMock()
        mock_kernel32.GetLastError.side_effect = [183, 183, 183, 0]
        mock_kernel32.CreateMutexW.side_effect = [42, 43, 44, 45]
        mock_ctypes.windll.kernel32 = mock_kernel32
        handle, err = acquire_mutex("TestMutex123", is_elevate=True)
        assert mock_kernel32.CreateMutexW.call_count == 4
        assert mock_kernel32.CloseHandle.call_count == 3
        assert handle == 45
        assert err == 0

    @patch("mutex.ctypes")
    def test_timeout_after_many_attempts(self, mock_ctypes):
        from mutex import acquire_mutex

        mock_kernel32 = MagicMock()
        mock_kernel32.GetLastError.return_value = 183
        mock_kernel32.CreateMutexW.return_value = 42
        mock_ctypes.windll.kernel32 = mock_kernel32
        with (
            patch("mutex.time.sleep", return_value=None),
            patch("mutex.MAX_POLL_ATTEMPTS", 5),
        ):
            handle, err = acquire_mutex("TestMutex123", is_elevate=True)
        assert handle is None
        assert err == 183
        assert mock_kernel32.CreateMutexW.call_count == 6


class TestAcquireMutexDefaultBehavior:
    """Тесты значения по умолчанию (без is_elevate) - совместимость."""

    @patch("mutex.ctypes")
    def test_default_is_elevate_false(self, mock_ctypes):
        from mutex import acquire_mutex

        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42
        mock_kernel32.GetLastError.return_value = 183
        mock_ctypes.windll.kernel32 = mock_kernel32
        handle, err = acquire_mutex("TestMutex123")
        assert handle is None
        assert err == 183
        assert mock_kernel32.CreateMutexW.call_count == 1


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
