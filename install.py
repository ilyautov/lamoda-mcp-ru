#!/usr/bin/env python3
"""Прописать lamoda-mcp в конфиг MCP-клиента.

Поддерживаются четыре клиента:
  claude-desktop — правим конфиг напрямую (с резервной копией)
  claude-code    — печатаем команду `claude mcp add`
  codex          — печатаем команду `codex mcp add`
  opencode       — правим ~/.config/opencode/opencode.json

Примеры:
    python3 install.py --client claude-desktop
    python3 install.py --client claude-desktop --client-id XXX --client-secret YYY
    python3 install.py --client claude-code
    python3 install.py --list          # показать найденные конфиги и выйти

Ключи, если они переданы, сохраняются в ~/.lamoda-mcp/cabinets.json (права 600),
а НЕ в конфиг клиента: конфиги нередко попадают в бэкапы и репозитории.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER_NAME = "lamoda"
SERVE = ROOT / "serve.py"


def claude_desktop_config() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if system == "Windows":
        base = os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))
        return Path(base) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def opencode_config() -> Path:
    return Path.home() / ".config/opencode/opencode.json"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(
            f"Не удалось прочитать {path}: {exc}\n"
            f"Файл существует, но повреждён. Исправьте или уберите его — "
            f"перезаписывать вслепую я не буду, чтобы не потерять ваши настройки."
        )


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    n = 0
    while True:
        dest = path.with_name(f"{path.name}.backup-{n}")
        if not dest.exists():
            break
        n += 1
    shutil.copy2(path, dest)
    return dest


def save_credentials(client_id: str, client_secret: str) -> None:
    sys.path.insert(0, str(ROOT))
    from core.credentials import CredentialStore

    CredentialStore().add_cabinet(
        "main", {"client_id": client_id, "client_secret": client_secret},
        make_active=True,
    )
    print("Ключи сохранены в ~/.lamoda-mcp/cabinets.json (права 600).")


def server_entry() -> dict:
    return {"command": sys.executable, "args": [str(SERVE)]}


def install_claude_desktop() -> int:
    path = claude_desktop_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    config = load_json(path)
    saved = backup(path)

    servers = config.setdefault("mcpServers", {})
    servers[SERVER_NAME] = server_entry()
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Прописан сервер '{SERVER_NAME}' в {path}")
    if saved:
        print(f"Резервная копия прежнего конфига: {saved}")
    print("\nПерезапустите Claude Desktop, чтобы он подхватил сервер.")
    return 0


def install_opencode() -> int:
    path = opencode_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    config = load_json(path)
    saved = backup(path)

    servers = config.setdefault("mcp", {})
    servers[SERVER_NAME] = {
        "type": "local",
        "command": [sys.executable, str(SERVE)],
        "enabled": True,
    }
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Прописан сервер '{SERVER_NAME}' в {path}")
    if saved:
        print(f"Резервная копия прежнего конфига: {saved}")
    return 0


def print_cli_command(tool: str) -> int:
    """Claude Code и Codex настраиваются своей CLI — печатаем готовую команду.

    Писать в их конфиги напрямую нельзя: формат принадлежит инструменту и
    меняется между версиями, а `mcp add` всегда актуален.
    """
    cmd = f'{tool} mcp add {SERVER_NAME} -- "{sys.executable}" "{SERVE}"'
    print("Выполните команду:\n")
    print(f"    {cmd}\n")
    print("После этого сервер появится в списке инструментов.")
    return 0


def show_paths() -> int:
    print("Пути конфигов на этой машине:")
    for label, path in (("claude-desktop", claude_desktop_config()),
                        ("opencode", opencode_config())):
        mark = "есть" if path.exists() else "нет"
        print(f"  {label:16} {path}  [{mark}]")
    print(f"  {'claude-code':16} настраивается командой `claude mcp add`")
    print(f"  {'codex':16} настраивается командой `codex mcp add`")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Установка lamoda-mcp в MCP-клиент")
    ap.add_argument("--client",
                    choices=["claude-desktop", "claude-code", "codex", "opencode"],
                    default="claude-desktop")
    ap.add_argument("--client-id", default="", help="client_id кабинета Lamoda")
    ap.add_argument("--client-secret", default="", help="client_secret кабинета Lamoda")
    ap.add_argument("--list", action="store_true", help="показать пути конфигов и выйти")
    args = ap.parse_args()

    if args.list:
        return show_paths()

    if not SERVE.exists():
        sys.exit(f"Не найден {SERVE} — запускайте install.py из папки репозитория.")

    if args.client_id and args.client_secret:
        save_credentials(args.client_id, args.client_secret)
    elif args.client_id or args.client_secret:
        sys.exit("Нужны оба ключа сразу: --client-id и --client-secret.")

    if args.client == "claude-desktop":
        rc = install_claude_desktop()
    elif args.client == "opencode":
        rc = install_opencode()
    elif args.client == "claude-code":
        rc = print_cli_command("claude")
    else:
        rc = print_cli_command("codex")

    if not args.client_id:
        print(
            "\nКлючи Lamoda пока не заданы. Либо перезапустите с "
            "--client-id и --client-secret, либо попросите агента: "
            "«сохрани кабинет Lamoda» — он вызовет lamoda_add_cabinet."
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
