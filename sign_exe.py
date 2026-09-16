"""
sign_exe.py — Подпись exe-файла через signtool.exe (Windows SDK).

Поддерживает два режима:
  - PFX: подпись сертификатом из .pfx файла с паролем
  - ESTS: подпись через Microsoft Authenticode ESTS (без сертификата, SmartScreen trusted)

Запуск:
  python sign_exe.py --exe path/to/exe --pfx path/to.pfx --pfx-pass password
  python sign_exe.py --exe path/to/exe --ests
  python sign_exe.py --exe path/to/exe --pfx path/to.pfx --pfx-pass password --tsa-url https://timestamp.digicert.com

Параметры:
  --exe        путь к exe-файлу
  --pfx        путь к .pfx файлу (обязательно, если не --ests)
  --pfx-pass   пароль от .pfx
  --ests       использовать Microsoft Authenticode ESTS (timestamp без сертификата)
  --tsa-url    URL сервера отметки времени (по умолчанию: https://timestamp.digicert.com)
  --verbose    подробный вывод

Требования:
  - Windows 10/11
  - Windows SDK (signtool.exe в PATH) или удалённый компилятор
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode == 0:
        logger.info("✅ Подпись успешна")
        return True
    logger.error("❌ Подпись не удалась: %s", result.stderr)
    return False


def sign_with_ests(
    exe_path: Path,
    tsa_url: str,
    signtool: Path | None = None,
) -> bool:
    """
    Подписать exe-файл через Microsoft Authenticode ESTS.

    ESTS позволяет получить доверенную отметку времени без сертификата —
    SmartTrust / Sectigo / DigiCert доверяют таким отметкам, и SmartScreen
    не будет блокировать файл.
    """
    st = signtool or find_signtool()
    cmd = [
        str(st),
        "sign",
        "/tr",
        tsa_url,
        "/td",
        "SHA256",
        "/fd",
        "SHA256",
        "/d",
        "Keyboard Layout Cleaner",
        "/du",
        "https://github.com/mrzsrg/KeyboardLayoutCleaner",
    ]
    cmd.append(str(exe_path))
    logger.info("Подпись через ESTS: %s", exe_path)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode == 0:
        logger.info("✅ Подпись ESTS успешна")
        return True
    logger.error("❌ Подпись ESTS не удалась: %s", result.stderr)
    return False


def verify_signature(exe_path: Path, signtool: Path | None = None) -> bool:
    """Проверить цифровую подпись exe-файла."""
    st = signtool or find_signtool()
    cmd = [str(st), "verify", "/pa", str(exe_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    ok = result.returncode == 0
    logger.info(
        "Проверка подписи %s: %s",
        exe_path,
        "✅ OK" if ok else "❌ FAILED",
    )
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Подпись exe-файла через signtool.exe (Windows SDK)"
    )
    parser.add_argument("--exe", required=True, help="Путь к exe-файлу")
    parser.add_argument("--pfx", help="Путь к .pfx файлу")
    parser.add_argument("--pfx-pass", help="Пароль от .pfx")
    parser.add_argument(
        "--ests",
        action="store_true",
        help="Использовать Microsoft Authenticode ESTS (timestamp без PFX)",
    )
    parser.add_argument(
        "--tsa-url",
        default="https://timestamp.digicert.com",
        help="URL сервера отметки времени (default: https://timestamp.digicert.com)",
    )
    parser.add_argument(
        "--verify", action="store_true", help="Только проверить подпись"
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
        ok = verify_signature(exe_path, signtool)
        sys.exit(0 if ok else 1)

    if args.ests:
        ok = sign_with_ests(exe_path, args.tsa_url, signtool)
    elif args.pfx and args.pfx_pass:
        ok = sign_with_pfx(
            exe_path, Path(args.pfx), args.pfx_pass, args.tsa_url, signtool
        )
    else:
        logger.error("Укажите --pfx + --pfx-pass или --ests для ESTS-подписи")
        sys.exit(1)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
