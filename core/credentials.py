"""Хранилище кредов кабинетов Lamoda.

Один продавец может вести несколько кабинетов и переключаться между ними прямо
из чата. Файл: ``~/.lamoda-mcp/cabinets.json`` (вне репозитория, chmod 600).

Форма::

    {
      "lamoda": {
        "active": "main",
        "cabinets": {
          "main":   {"client_id": "...", "client_secret": "..."},
          "second": {"client_id": "...", "client_secret": "..."}
        }
      }
    }

Порядок разрешения кредов:
  1. активный кабинет из cabinets.json, если он заполнен целиком;
  2. иначе переменные окружения (установка «только через env» ведёт себя как
     единственный кабинет с именем "env").

ГРАБЛИ, пойманные в marketplace-mcp: активный кабинет имеет приоритет НАД env.
Тестовая установка с фиктивными ключами создаёт кабинет, который затеняет
настоящие переменные окружения, и дальше всё падает с невнятной ошибкой
авторизации. При необъяснимом 401 первым делом смотрите, какой кабинет активен
(`lamoda_list_cabinets`), а не перевыпускайте ключ.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

try:  # advisory-локи POSIX; на Windows отсутствуют
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

SERVICE = "lamoda"
STORE_DIR = Path(os.environ.get("LAMODA_MCP_HOME", Path.home() / ".lamoda-mcp"))
STORE_PATH = STORE_DIR / "cabinets.json"

FIELDS = ["client_id", "client_secret"]
ENV_MAP = {
    "client_id": "LAMODA_CLIENT_ID",
    "client_secret": "LAMODA_CLIENT_SECRET",
}


class CredentialStore:
    """Читает и пишет файл кабинетов, разрешает активные креды."""

    def __init__(self, path: Path = STORE_PATH):
        self.path = path

    # --- низкоуровневый ввод-вывод ------------------------------------------
    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)  # тут лежат живые ключи

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            self._backup_corrupt(raw)
            return {}
        if not isinstance(data, dict):
            self._backup_corrupt(raw)
            return {}
        return data

    def _backup_corrupt(self, raw: str) -> None:
        """Сохранить нечитаемый файл секретов, чтобы следующая запись не затёрла
        его пустым хранилищем."""
        try:
            self._ensure_dir()
            n = 0
            while True:
                dest = self.path.with_name(f"{self.path.name}.corrupt-{n}")
                if not dest.exists():
                    break
                n += 1
            dest.write_text(raw, encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(dest, 0o600)
            print(
                f"[lamoda-mcp] ВНИМАНИЕ: {self.path} не читается; файл сохранён "
                f"как {dest}, хранилище начато заново. Старые ключи есть в копии.",
                file=sys.stderr,
            )
        except OSError:
            pass  # сбой резервного копирования не должен ронять чтение

    def _save(self, data: dict) -> None:
        self._ensure_dir()
        # Атомарно: временный файл 0600 рядом, затем os.replace — падение
        # посреди записи не оставит обрезанный файл секретов.
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False, indent=2))
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def _locked(self):
        """Сериализовать цикл чтение-правка-запись между процессами."""
        self._ensure_dir()
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_fd = None
        try:
            if fcntl is not None:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            if lock_fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)

    def _mutate(self, fn: Callable[[dict], object]) -> object:
        with self._locked():
            data = self._load()  # свежая загрузка внутри лока
            result = fn(data)
            self._save(data)
            return result

    # --- управление кабинетами ----------------------------------------------
    def list_cabinets(self) -> dict:
        svc = self._load().get(SERVICE, {})
        return {
            "active": svc.get("active"),
            "cabinets": sorted((svc.get("cabinets") or {}).keys()),
        }

    def add_cabinet(self, name: str, creds: dict, make_active: bool = True) -> None:
        def apply(data: dict) -> None:
            svc = data.setdefault(SERVICE, {"active": None, "cabinets": {}})
            svc["cabinets"][name] = creds
            if make_active or not svc.get("active"):
                svc["active"] = name

        self._mutate(apply)

    def remove_cabinet(self, name: str) -> bool:
        def apply(data: dict) -> bool:
            svc = data.get(SERVICE, {})
            cabs = svc.get("cabinets", {})
            if name not in cabs:
                return False
            del cabs[name]
            if svc.get("active") == name:
                svc["active"] = next(iter(cabs), None)
            return True

        return bool(self._mutate(apply))

    def set_active(self, name: str) -> bool:
        def apply(data: dict) -> bool:
            svc = data.get(SERVICE, {})
            if name not in (svc.get("cabinets") or {}):
                return False
            svc["active"] = name
            return True

        return bool(self._mutate(apply))

    # --- разрешение кредов ---------------------------------------------------
    def resolve(self, fields: Optional[list[str]] = None) -> tuple[dict, str]:
        """Вернуть (креды, источник). Активный кабинет важнее env.

        В кредах всегда присутствуют все запрошенные поля (возможно, пустые).
        Источник — имя кабинета, "env" или "none".
        """
        fields = fields or FIELDS
        svc = self._load().get(SERVICE, {})
        active = svc.get("active")
        cabs = svc.get("cabinets") or {}
        if active and active in cabs:
            stored = cabs[active]
            creds = {f: stored.get(f, "") for f in fields}
            if all(creds[f] for f in fields):
                return creds, active
        env_creds = {f: os.environ.get(ENV_MAP.get(f, ""), "") for f in fields}
        if all(env_creds[f] for f in fields):
            return env_creds, "env"
        merged = {
            f: (cabs.get(active, {}).get(f, "") if active else "") or env_creds.get(f, "")
            for f in fields
        }
        source = (
            active if (active and active in cabs)
            else ("env" if any(env_creds.values()) else "none")
        )
        return merged, source

    def missing(self, fields: Optional[list[str]] = None) -> list[str]:
        fields = fields or FIELDS
        creds, _ = self.resolve(fields)
        return [f for f in fields if not creds.get(f)]
