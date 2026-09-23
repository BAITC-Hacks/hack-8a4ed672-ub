"""hattama CLI: python -m hattama.cli <command>."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from hattama.config import REPO_ROOT, get_settings


def _alembic_config():  # type: ignore[no-untyped-def]
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "apps" / "api" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "apps" / "api" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().resolved_database_url())
    return cfg


def cmd_migrate(_: argparse.Namespace) -> int:
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    print("БД обновлена до последней миграции")
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from hattama.db.models import User
    from hattama.db.session import session_scope
    from hattama.domain.enums import UserRole
    from hattama.security.passwords import hash_password

    if args.role not in [r.value for r in UserRole]:
        print(f"Роль должна быть одной из: {[r.value for r in UserRole]}", file=sys.stderr)
        return 2
    password = os.environ.get("HATTAMA_NEW_USER_PASSWORD") or getpass.getpass("Пароль (>=10 символов): ")
    with session_scope() as s:
        if s.scalar(select(User).where(func.lower(User.email) == args.email.lower())):
            print("Пользователь уже существует", file=sys.stderr)
            return 1
        s.add(User(email=args.email, display_name=args.name, role=args.role, password_hash=hash_password(password)))
    print(f"Создан пользователь {args.email} ({args.role})")
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    from hattama.api.app import create_app

    # single process: ingestion keeps per-connection state (see docs/architecture.md)
    uvicorn.run(create_app(), host=args.host, port=args.port, workers=1, proxy_headers=args.proxy_headers,
                forwarded_allow_ips=args.forwarded_allow_ips, log_config=None, ws_max_size=1024 * 1024)
    return 0


def cmd_asr_worker(_: argparse.Namespace) -> int:
    from hattama.workers.asr_worker import main

    return main()


def cmd_pipeline_worker(_: argparse.Namespace) -> int:
    from hattama.workers.pipeline_worker import main

    return main()


def cmd_doctor(args: argparse.Namespace) -> int:
    from hattama.diagnostics.doctor import main

    return main(["--json"] if args.json else [])


def cmd_export_openapi(args: argparse.Namespace) -> int:
    from hattama.api.app import create_app

    spec = create_app().openapi()
    Path(args.out).write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"OpenAPI -> {args.out}")
    return 0


def cmd_export_schemas(args: argparse.Namespace) -> int:
    from hattama_contracts.messages import LiveEvent, client_message_adapter, server_message_adapter

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    schemas = {
        "ingest-client-messages.schema.json": client_message_adapter.json_schema(),
        "ingest-server-messages.schema.json": server_message_adapter.json_schema(),
        "live-event.schema.json": LiveEvent.model_json_schema(),
    }
    for name, schema in schemas.items():
        (out / name).write_text(json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        print(f"schema -> {out / name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hattama")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    p = sub.add_parser("create-user")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", required=True)
    p.set_defaults(fn=cmd_create_user)
    p = sub.add_parser("api")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--proxy-headers", action="store_true")
    p.add_argument("--forwarded-allow-ips", default="127.0.0.1")
    p.set_defaults(fn=cmd_api)
    sub.add_parser("asr-worker").set_defaults(fn=cmd_asr_worker)
    sub.add_parser("pipeline-worker").set_defaults(fn=cmd_pipeline_worker)
    p = sub.add_parser("doctor")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("export-openapi")
    p.add_argument("--out", default=str(REPO_ROOT / "packages" / "contracts" / "openapi.json"))
    p.set_defaults(fn=cmd_export_openapi)
    p = sub.add_parser("export-schemas")
    p.add_argument("--out", default=str(REPO_ROOT / "packages" / "contracts" / "schemas"))
    p.set_defaults(fn=cmd_export_schemas)
    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
