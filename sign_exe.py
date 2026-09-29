"""
sign_exe.py — Подпись exe-файла через signtool.exe (Windows SDK).

Поддерживает два режима:
  - PFX: подпись сертификатом из .pfx файла с паролем
  - STORE: автовыбор сертификата из хранилища Windows (signtool /a)

Запуск:
  python sign_exe.py --exe path/to/exe --pfx path/to.pfx --pfx-pass password
  python sign_exe.py --exe path/to/exe --store
  python sign_exe.py --exe path/to/exe --pfx path/to.pfx --pfx-pass password --tsa-url https://timestamp.digicert.com

Параметры:
  --exe        путь к exe-файлу
  --pfx        путь к .pfx файлу (обязательно, если не --store)
  --pfx-pass   пароль от .pfx
  --store      использовать сертификат из личного хранилища Windows (автовыбор)
  --tsa-url    URL сервера отметки времени (по умолчанию: https://timestamp.digicert.com)
  --verify     только проверить подпись
  --allow-untrusted  при --verify принять подпись с недоверенным корнем
                    (целостность проверяется, доверие к издателю - нет)
  --verbose    подробный вывод

Требования:
  - Windows 10/11
  - Windows SDK (signtool.exe в PATH) или удалённый компилятор
  - Для --store: установленный сертификат подписи кода в личном хранилище пользователя
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import applog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sign_exe")


def find_signtool() -> Path:
    """Найти signtool.exe в системе."""
    signtool = Path("signtool.exe")
    if signtool.exists():
        return signtool
    try:
        result = subprocess.run(
            ["where", "signtool.exe"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except FileNotFoundError:
        pass
    raise FileNotFoundError(
        "signtool.exe не найден в PATH. Установите Windows SDK или добавьте "
        "C:\\Program Files (x86)\\Windows Kits\\10\\bin\\<version>\\x64 в PATH"
    )


def sign_with_pfx(
    exe_path: Path,
    pfx_path: Path,
    pfx_pass: str,
    tsa_url: str | None = None,
    signtool: Path | None = None,
) -> bool:
    """Подписать exe-файл сертификатом из .pfx."""
    st = signtool or find_signtool()
    cmd = [
        str(st),
        "sign",
        "/f",
        str(pfx_path),
        "/p",
        pfx_pass,
        "/fd",
        "SHA256",
        "/td",
        "SHA256",
    ]
    if tsa_url:
        cmd.extend(["/tr", tsa_url, "/td", "SHA256"])
    cmd.append(str(exe_path))
    logger.info("Подпись через PFX: %s", exe_path)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        # Проверено на живой машине 29.09.2026: без доступа к сети signtool
        # висит на метке времени и падает по таймауту через 60 с. Трассировка
        # в этом месте ничего не объясняла бы - непрочитанный TimeoutExpired
        # обрывал шаг стеком. Причина всегда одна: сервер отметки времени
        # недоступен (частая блокировка сети или файрвол).
        logger.error(
            "❌ Подпись не удалась: истёк таймаут 60 с. Если задан --tsa-url, "
            "проверьте доступность сервера отметки времени; в оффлайн-сборке "
            "подпишите без него (--tsa-url \"\")"
        )
        return False
    if result.returncode == 0:
        logger.info("✅ Подпись успешна")
        return True
    logger.error("❌ Подпись не удалась: %s", result.stderr)
    return False


def sign_with_store(
    exe_path: Path,
    tsa_url: str | None = None,
    signtool: Path | None = None,
) -> bool:
    """
    Подписать exe-файл сертификатом из хранилища Windows (автовыбор).

    Использует signtool /a — автоматический выбор лучшего сертификата
    подписи кода из личного хранилища текущего пользователя.
    Требует установленного сертификата подписи кода.
    """
    st = signtool or find_signtool()
    cmd = [
        str(st),
        "sign",
        "/a",
        "/fd",
        "SHA256",
        "/td",
        "SHA256",
    ]
    if tsa_url:
        cmd.extend(["/tr", tsa_url, "/td", "SHA256"])
    cmd.append(str(exe_path))
    logger.info("Подпись из хранилища Windows: %s", exe_path)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        logger.error(
            "❌ Подпись не удалась: истёк таймаут 60 с (сервер отметки времени "
            "недоступен?)"
        )
        return False
    if result.returncode == 0:
        logger.info("✅ Подпись успешна")
        return True
    logger.error("❌ Подпись не удалась: %s", result.stderr)
    return False


#: Статусы Get-AuthenticodeSignature, при которых подпись на месте, но
#: цепочка не доверена. Измерено на живой машине 29.09.2026 для
#: самоподписанного сертификата: UnknownError с сообщением «Цепочка
#: сертификатов обработана, но обработка прервана на корневом сертификате, у
#: которого отсутствует отношение доверия с поставщиком доверия».
#: Подделанный файл даёт NotSigned, поэтому эти статусы означают именно
#: «подпись есть, доверия нет», а не «подписи нет».
_UNTRUSTED_STATUSES = frozenset({"UnknownError", "NotTrusted"})

#: Статус подписи, означающий «подпись действительна и издателю доверяют».
_TRUSTED_STATUS = "Valid"

#: PowerShell для чтения состояния подписи: 7 предпочтительнее 5.1.
_PS_CANDIDATES = ("pwsh", "powershell")

#: Куски PSModulePath, которые принадлежат только PowerShell 7. Наследуя их,
#: Windows PowerShell 5.1 находит в них свой модуль Microsoft.PowerShell.Security
#: в несовместимом формате и не может его загрузить. Сравнение идёт по
#: нормализованному пути (нижний регистр, прямые слэши): в разных источниках
#: один и тот же путь записывается и с обратными слэшами, и с прямыми.
_PS7_MODULE_HINTS = (
    "/powershell/7/",  # C:\Program Files\PowerShell\7\Modules
    "microsoft.powershell_7",  # WindowsApps\Microsoft.PowerShell_7.x_...\Modules
    "windowsapps/microsoft.powershell",
)

_POWERSHELL_SCRIPT = (
    "$ErrorActionPreference = 'Stop';"
    "$s = Get-AuthenticodeSignature -LiteralPath $env:KLC_EXE_PATH;"
    "$thumb = ''; $subject = '';"
    "if ($s.SignerCertificate) {"
    "  $thumb = $s.SignerCertificate.Thumbprint;"
    "  $subject = $s.SignerCertificate.Subject"
    "};"
    "[pscustomobject]@{"
    "  status = [string]$s.Status;"
    "  has_signer = [bool]$s.SignerCertificate;"
    "  type = [string]$s.SignatureType;"
    "  thumb = $thumb;"
    "  subject = $subject"
    "} | ConvertTo-Json -Compress"
)


def find_powershell() -> str:
    """Найти PowerShell, в котором реально работает Get-AuthenticodeSignature.

    PowerShell 7 (``pwsh``) предпочтительнее, и это не вкусовое предпочтение.
    Проверено на живой машине 29.09.2026: Windows PowerShell 5.1, запущенный
    из Python, отвечает «команда Get-AuthenticodeSignature найдена в модуле
    Microsoft.PowerShell.Security, но загрузить этот модуль не удалось» — то
    есть нужного cmdlet'а просто нет, и проверка подписи молча вырождается в
    отказ. С ``pwsh`` тот же вызов возвращает
    ``{"status": "UnknownError", "has_signer": true}``. Полный путь к
    powershell.exe и ``-ExecutionPolicy`` ничего не меняют: дело в модуле.
    """
    for name in _PS_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "Не найден ни pwsh, ни powershell - нечем прочитать состояние подписи"
    )


def _powershell_env(exe_path: Path) -> dict[str, str]:
    """Окружение для дочернего PowerShell без путей модулей PowerShell 7.

    Нужно только для отката на Windows PowerShell 5.1: унаследованный
    ``PSModulePath`` от PowerShell 7 указывает 5.1 на несовместимые модули
    (см. :func:`find_powershell`). Путь к exe передаётся через переменную
    окружения, а не в строке команды: путь к файлу — пользовательские данные.
    """
    env = {**os.environ, "KLC_EXE_PATH": str(exe_path)}
    # На Windows os.environ приводит имена переменных к верхнему регистру, и
    # поиск «PSModulePath» в обычном dict просто ничего не находит: санитинг
    # молча не срабатывал бы, а дочерний процесс наследовал ровно то, что мы
    # собирались вычистить. Ключ ищем регистронезависимо и перезаписываем
    # под тем же именем.
    key = next((k for k in env if k.upper() == "PSMODULEPATH"), "PSModulePath")
    path = env.get(key, "")
    if path:
        kept = [
            part
            for part in path.split(";")
            if part
            and not any(
                hint in part.replace("\\", "/").lower() for hint in _PS7_MODULE_HINTS
            )
        ]
        if kept:
            env[key] = ";".join(kept)
    return env


def read_authenticode_status(exe_path: Path) -> dict[str, object]:
    """Структурные сведения о подписи из Get-AuthenticodeSignature.

    Нужны там, где ``signtool verify /pa`` не может завершиться успехом по
    существу: для самоподписанного сертификата он честно отвечает «цепочка
    прервана на недоверенном корне». Единственный способ «починить» это —
    вписать корень в хранилище доверия, а он проверен и ПОВЕШЕНО на
    неинтерактивной сессии (``certutil -addstore -user Root`` не возвращает
    управление без диалога подтверждения), то есть такой шаг просто остановил
    бы релизную сборку.

    Поэтому доверие и целостность разнесены: целостность (подпись есть, файл
    не менялся после подписи) читается здесь, а доверие к издателю —
    отдельное решение вызывающей стороны (:func:`verify_signature`).
    """
    env = _powershell_env(exe_path)
    try:
        result = subprocess.run(
            [
                find_powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _POWERSHELL_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Не удалось прочитать состояние подписи: %s", exc)
        return {}
    if result.returncode != 0:
        logger.warning(
            "Get-AuthenticodeSignature вернул %s: %s",
            result.returncode,
            (result.stderr or "").strip()[:200],
        )
        return {}
    try:
        data = json.loads((result.stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        logger.warning("Ответ Get-AuthenticodeSignature не разобран: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "status": str(data.get("status", "")),
        "has_signer": bool(data.get("has_signer")),
        "type": str(data.get("type", "")),
        "thumb": str(data.get("thumb", "")),
        "subject": str(data.get("subject", "")),
    }


def verify_signature(
    exe_path: Path,
    signtool: Path | None = None,
    *,
    allow_untrusted: bool = False,
) -> bool:
    """Проверить цифровую подпись exe-файла.

    Сначала — строгая проверка ``signtool verify /pa``: она доказывает и
    целостность, и доверие к издателю. Если она не прошла, решение о
    допустимости принимает :paramref:`allow_untrusted`, и оно принимается
    ТОЛЬКО когда подпись структурно на месте (см.
    :func:`read_authenticode_status`): сертификат есть, тип Authenticode,
    статус означает «цепочка не доверена». Подделанный файл и файл без
    подписи отсеиваются этим же условием, а не проходят optimistically.

    По умолчанию строго: самоподписанный сертификат без явного разрешения
    проверку не проходит. Молчаливое «подпись есть, значит всё хорошо» здесь
    означало бы ложное обещание доверия, а его никто не давал.
    """
    st = signtool or find_signtool()
    cmd = [str(st), "verify", "/pa", str(exe_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        logger.info("✅ Подпись действительна и доверена: %s", exe_path)
        return True

    info = read_authenticode_status(exe_path)
    status = str(info.get("status", ""))
    if (
        allow_untrusted
        and info.get("has_signer")
        and info.get("type") == "Authenticode"
        and status in _UNTRUSTED_STATUSES
    ):
        logger.warning(
            "⚠ Подпись на месте, но цепочка не доверена (статус %s): %s "
            "[отпечаток %s]. Целостность файла проверена, доверия к "
            "издателю нет - так ведёт себя самоподписанный сертификат.",
            status,
            info.get("subject", ""),
            info.get("thumb", ""),
        )
        return True

    reason = (result.stdout or result.stderr or "").strip().replace("\n", " ")[:200]
    logger.error(
        "❌ Подпись не прошла проверку: %s (signtool: %s; "
        "Get-AuthenticodeSignature: %s)",
        exe_path,
        reason or f"returncode={result.returncode}",
        status or "нет данных",
    )
    return False


def main() -> None:
    # FIX-32: сообщения журнала по-русски, а консоль может быть cp1252 —
    # печать тогда падает. Кодировку не меняем, разрешаем замену символа.
    applog.make_output_safe()

    parser = argparse.ArgumentParser(
        description="Подпись exe-файла через signtool.exe (Windows SDK)"
    )
    parser.add_argument("--exe", required=True, help="Путь к exe-файлу")
    parser.add_argument("--pfx", help="Путь к .pfx файлу")
    parser.add_argument("--pfx-pass", help="Пароль от .pfx")
    parser.add_argument(
        "--store",
        action="store_true",
        help="Использовать сертификат из хранилища Windows (автовыбор /a)",
    )
    parser.add_argument(
        "--tsa-url",
        default="https://timestamp.digicert.com",
        help="URL сервера отметки времени (default: https://timestamp.digicert.com)",
    )
    parser.add_argument(
        "--verify", action="store_true", help="Только проверить подпись"
    )
    parser.add_argument(
        "--allow-untrusted",
        action="store_true",
        help=(
            "При --verify разрешить подпись с недоверенным корнем "
            "(самоподписанный сертификат): целостность файла всё равно "
            "проверяется, но доверия к издателю это не даёт"
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Подробный вывод")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    exe_path = Path(args.exe).resolve()
    if not exe_path.exists():
        logger.error("Файл не найден: %s", exe_path)
        sys.exit(1)

    try:
        signtool = find_signtool()
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)

    if args.verify:
        ok = verify_signature(exe_path, signtool, allow_untrusted=args.allow_untrusted)
        sys.exit(0 if ok else 1)

    if args.store:
        ok = sign_with_store(exe_path, args.tsa_url, signtool)
    elif args.pfx and args.pfx_pass:
        ok = sign_with_pfx(
            exe_path, Path(args.pfx), args.pfx_pass, args.tsa_url, signtool
        )
    else:
        logger.error(
            "Необходимо указать способ подписи:\n"
            "  --pfx <файл> --pfx-pass <пароль>  — подпись из .pfx файла\n"
            "  --store                            — подпись из хранилища Windows (требует сертификат подписи кода)\n"
            "Используйте --verify для проверки подписи без изменения файла."
        )
        sys.exit(1)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
