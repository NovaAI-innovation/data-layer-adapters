"""data_management plugin user script.

Invoked from the Plugins UI (or directly: `python execute.py <action>`).

Actions:
  ingest [CHATS_DIR]    Ingest every <chat_id>/chat.json into Postgres.
  ingest-chat <DIR>     Ingest a single chat directory.
  stats                 Show current Postgres content counts.
  schema                Show table list.

DSN comes from EVENT_LEDGER_DSN env var (preferred) or data_management.dsn.
Re-running ingest is idempotent (ON CONFLICT in PostgresAdapter).

Exit codes:
  0  success
  1  bad arguments / missing dependency
  2  runtime error during ingest
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback


def _add_a0_to_path() -> None:
    """Make `from usr.plugins.<name>...` importable from this script.

    The framework runtime adds /a0 to sys.path; a standalone script does
    not, so we do it ourselves. Without this, `usr.plugins.data_management`
    cannot be imported as a top-level package.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # /a0/usr/plugins/data_management/execute.py -> /a0
    a0_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if a0_root not in sys.path:
        sys.path.insert(0, a0_root)


_add_a0_to_path()


def _resolve_dsn() -> str:
    env = os.environ.get("EVENT_LEDGER_DSN", "").strip()
    if env:
        return env
    from helpers.plugins import get_plugin_config
    cfg = get_plugin_config("data_management") or {}
    dsn = (cfg.get("dsn") or "").strip()
    if not dsn:
        raise RuntimeError(
            "No DSN: set EVENT_LEDGER_DSN or data_management.dsn"
        )
    return dsn


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="data_management",
        description="data_management plugin script",
    )
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("stats", help="Show current Postgres content counts.")
    sub.add_parser("schema", help="Show table list.")

    pa = sub.add_parser("ingest", help="Ingest every chat directory.")
    pa.add_argument(
        "chats_root", nargs="?",
        default="/a0/usr/chats",
        help="Path to usr/chats (default: /a0/usr/chats)",
    )
    pa.add_argument("--quiet", "-q", action="store_true")

    pc = sub.add_parser("ingest-chat", help="Ingest one chat directory.")
    pc.add_argument("chat_dir", help="Path to <chat_id>/ containing chat.json")
    pc.add_argument("--quiet", "-q", action="store_true")

    return p


def cmd_schema() -> int:
    from usr.plugins.data_management.helpers.db import execute_sql
    rs = execute_sql(
        _resolve_dsn(),
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name",
        readonly=True,
    )
    for row in rs["rows"]:
        print(f"  {row[0]}.{row[1]}")
    return 0


def cmd_stats() -> int:
    from usr.plugins.data_management.helpers.db import execute_sql
    tables = [
        "projects", "agent_frameworks", "agents", "sessions",
        "messages", "tool_executions", "available_tools",
        "agent_skills", "agent_plugins", "hooks",
    ]
    print(f"{'table':24s} {'count':>10s}")
    print("-" * 24 + " " + "-" * 10)
    dsn = _resolve_dsn()
    for t in tables:
        try:
            rs = execute_sql(dsn, f"SELECT count(*) FROM {t}", readonly=True)
            print(f"{t:24s} {rs['rows'][0][0]:>10d}")
        except Exception as exc:
            print(f"{t:24s} ERR: {exc}")
    return 0


def _print_stats(stats, quiet: bool) -> None:
    if quiet:
        return
    d = stats.as_dict()
    print(f"summary: {d}")
    if d["first_errors"]:
        print("Errors:")
        for e in d["first_errors"]:
            print(f"  ! {e}")


def cmd_ingest(chats_root: str, quiet: bool) -> int:
    from usr.plugins.data_management.adapters.chat_json import ChatJsonAdapter
    a = ChatJsonAdapter()
    stats = a.ingest_all(chats_root)
    d = stats.as_dict()
    print(
        f"ingest_all({chats_root}) "
        f"chats={d['chats_ingested']}/{d['chats_total']} "
        f"skipped={d['chats_skipped']} "
        f"sessions={d['sessions_opened']} "
        f"messages={d['messages_recorded']}/{d['messages_total']} "
        f"tool_calls={d['tool_calls_recorded']}/{d['tool_calls_total']} "
        f"parallel_parents={d['parallel_parents']} "
        f"parallel_children={d['parallel_children']} "
        f"errors={d['errors']}"
    )
    _print_stats(stats, quiet)
    return 0 if d["errors"] == 0 else 2


def cmd_ingest_chat(chat_dir: str, quiet: bool) -> int:
    from usr.plugins.data_management.adapters.chat_json import (
        ChatJsonAdapter, IngestStats,
    )
    a = ChatJsonAdapter()
    stats = IngestStats()
    ok = a.ingest_chat(chat_dir, stats)
    d = stats.as_dict()
    print(
        f"ingest_chat({chat_dir}) ok={ok} "
        f"msgs={d['messages_recorded']}/{d['messages_total']} "
        f"tool_calls={d['tool_calls_recorded']}/{d['tool_calls_total']} "
        f"errors={d['errors']}"
    )
    _print_stats(stats, quiet)
    return 0 if ok and d["errors"] == 0 else 2


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "schema":
            return cmd_schema()
        if args.action == "stats":
            return cmd_stats()
        if args.action == "ingest":
            return cmd_ingest(args.chats_root, args.quiet)
        if args.action == "ingest-chat":
            return cmd_ingest_chat(args.chat_dir, args.quiet)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        traceback.print_exc()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
