#!/usr/bin/env python3
"""Точка входа, которая сама поднимает окружение и запускает MCP-сервер.

Зачем. MCP-клиент (Claude Desktop, Cowork, Codex, OpenCode) запускает сервер как
обычный процесс и не умеет ставить зависимости. Этот файл при первом запуске
создаёт виртуальное окружение рядом с репозиторием, доставляет туда зависимости
и перезапускает себя уже внутри него. Пользователю не нужен терминал.

КРИТИЧНО ПРО STDOUT. Транспорт stdio использует стандартный вывод для протокола
MCP. Любая посторонняя строка в stdout ломает соединение с клиентом, поэтому
всё, что печатается при установке, идёт в stderr.

    python3 serve.py              # запустить сервер
    python3 serve.py --selfcheck  # проверить окружение и выйти
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIRED = ["mcp[cli]>=1.2.0", "httpx>=0.27", "pyyaml>=6.0"]
# Отметка, что уже перезапускались: страховка от бесконечного цикла, если
# окружение поднялось, а импорт всё равно не проходит.
GUARD_ENV = "LAMODA_MCP_BOOTSTRAPPED"


def log(message: str) -> None:
    """Только stderr: stdout принадлежит протоколу MCP."""
    print(f"[lamoda-mcp] {message}", file=sys.stderr, flush=True)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def deps_present() -> bool:
    try:
        import httpx  # noqa: F401
        import mcp  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def create_venv() -> bool:
    log(f"Создаю окружение в {VENV} (первый запуск, это разово)...")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(VENV)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError, OSError) as exc:
        log(f"Не удалось создать окружение: {exc}")
        return False
    return True


def install_deps() -> bool:
    py = venv_python()
    log("Ставлю зависимости...")
    try:
        subprocess.run([str(py), "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check", *REQUIRED],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[:500]
        log(f"Установка не удалась: {detail}")
        return False
    return True


def bootstrap() -> None:
    """Поднять окружение и перезапустить себя внутри него."""
    if os.environ.get(GUARD_ENV):
        log("Зависимости не подхватились даже после установки. "
            "Поставьте вручную: pip install " + " ".join(REQUIRED))
        raise SystemExit(1)

    if not venv_python().exists() and not create_venv():
        raise SystemExit(1)
    if not install_deps():
        raise SystemExit(1)

    log("Окружение готово, запускаю сервер.")
    env = dict(os.environ, **{GUARD_ENV: "1"})
    os.execve(str(venv_python()), [str(venv_python()), __file__, *sys.argv[1:]], env)


def selfcheck() -> int:
    """Проверить, что каталог и рецепты читаются. Без запуска сервера."""
    sys.path.insert(0, str(ROOT))
    from core.registry import Catalog

    catalog = Catalog.from_yaml(ROOT / "lamoda_mcp" / "endpoints.yaml")
    log(f"Каталог: {len(catalog.all())} методов, {catalog.counts_by_safety()}")
    log(f"Разделов: {len(catalog.sections())}")
    log(f"Скрытых служебных: {sum(1 for s in catalog.all() if s.internal)}")
    unresolved = catalog.items_path_unresolved
    if unresolved:
        log(f"Постраничных без пути к строкам: {len(unresolved)} "
            f"(обход работает через запасные пути)")
    log("Проверка пройдена.")
    return 0


def main() -> int:
    if not deps_present():
        bootstrap()  # не возвращается: заменяет процесс

    sys.path.insert(0, str(ROOT))
    if "--selfcheck" in sys.argv:
        return selfcheck()

    from lamoda_mcp.server import main as run_server

    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
