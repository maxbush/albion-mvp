"""Архитектурные ограждения (T02) — храповик нарушений.

Каждое правило из docs/phase1/ARCHITECTURE.md §2.2 считается статически по
исходникам src/. Текущее число нарушений зафиксировано в baseline.json.
Тест падает, если нарушений СТАЛО БОЛЬШЕ. Если стало меньше — тест тоже
падает с подсказкой уменьшить baseline (чтобы достигнутое не откатилось).

Агентам: редактировать этот файл нельзя; baseline.json — только уменьшать.
"""

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
BASELINE = Path(__file__).with_name("baseline.json")


def _py_files():
    return sorted(p for p in SRC.rglob("*.py") if "alembic" not in p.parts)


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _imports(tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def _in(rel: str, *prefixes: str) -> bool:
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in prefixes)


# ── правила ─────────────────────────────────────────────────────────────

def rule_datetime_now_outside_clock(rel, text, tree):
    if rel == "src/core/clock.py":
        return 0
    return len(re.findall(r"datetime\.(?:now|utcnow)\(", text))


def rule_telegram_import_outside_adapters(rel, text, tree):
    if _in(rel, "src/bot", "src/channels/telegram.py", "src/app.py", "src/main.py"):
        return 0
    return sum(1 for m in _imports(tree) if m == "telegram" or m.startswith("telegram."))


def rule_httpx_outside_integrations(rel, text, tree):
    if _in(rel, "src/integrations", "src/channels", "src/ai"):
        return 0
    return sum(1 for m in _imports(tree) if m == "httpx" or m.startswith("httpx."))


_SQL_RE = re.compile(r"""["'](?:\s*)(SELECT|INSERT|UPDATE|DELETE)\s""", re.IGNORECASE)


def rule_raw_sql_outside_db(rel, text, tree):
    if _in(rel, "src/db"):
        return 0
    return len(_SQL_RE.findall(text)) + len(re.findall(r"\._(?:execute|fetchone|fetchall)\(", text))


_SQLITE_ISMS = re.compile(r"julianday\(|json_extract\(|datetime\('now'|\.lastrowid|INSERT OR (?:REPLACE|IGNORE)")


def rule_sqlite_isms(rel, text, tree):
    return len(_SQLITE_ISMS.findall(text))


def rule_workflow_imports_bot(rel, text, tree):
    if not _in(rel, "src/workflows", "src/api", "src/services", "src/actions", "src/domain"):
        return 0
    return sum(1 for m in _imports(tree) if m.startswith("src.bot"))


def rule_domain_impure_imports(rel, text, tree):
    if not _in(rel, "src/domain"):
        return 0
    bad = ("src.db", "src.bot", "src.events", "src.config", "src.integrations",
           "src.channels", "telegram", "httpx", "aiosqlite", "sqlalchemy")
    return sum(1 for m in _imports(tree) if m.startswith(bad))


def rule_telegram_id_in_workflows(rel, text, tree):
    """Адресация людей по telegram_id в бизнес-слое (P1). Цель — 0."""
    if not _in(rel, "src/workflows", "src/services", "src/actions"):
        return 0
    return len(re.findall(r"telegram_id", text))


def rule_notification_requested_publish(rel, text, tree):
    """Прямые publish(NOTIFICATION_REQUESTED) вместо notify() (P4). Цель — 0."""
    if rel == "src/events/types.py":
        return 0
    return len(re.findall(r"EventTypes\.NOTIFICATION_REQUESTED,\s*\{", text))


def rule_file_over_800_lines(rel, text, tree):
    """Файлы > 800 строк плохо помещаются в контекст агента. Считаем «лишние» сотни строк."""
    n = text.count("\n") + 1
    return max(0, (n - 800 + 99) // 100)


RULES = {
    "datetime_now_outside_clock": rule_datetime_now_outside_clock,
    "telegram_import_outside_adapters": rule_telegram_import_outside_adapters,
    "httpx_outside_integrations": rule_httpx_outside_integrations,
    "raw_sql_outside_db": rule_raw_sql_outside_db,
    "sqlite_isms": rule_sqlite_isms,
    "workflow_imports_bot": rule_workflow_imports_bot,
    "domain_impure_imports": rule_domain_impure_imports,
    "telegram_id_in_workflows": rule_telegram_id_in_workflows,
    "notification_requested_publish": rule_notification_requested_publish,
    "file_over_800_lines": rule_file_over_800_lines,
}


def measure() -> dict[str, dict]:
    result = {name: {"total": 0, "files": {}} for name in RULES}
    for p in _py_files():
        text = p.read_text(encoding="utf-8")
        tree = ast.parse(text)
        rel = _rel(p)
        for name, fn in RULES.items():
            n = fn(rel, text, tree)
            if n:
                result[name]["total"] += n
                result[name]["files"][rel] = n
    return result


@pytest.mark.parametrize("rule", sorted(RULES))
def test_rule_does_not_regress(rule):
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = measure()[rule]
    allowed = baseline[rule]
    assert current["total"] <= allowed, (
        f"Архитектурное правило '{rule}': нарушений стало {current['total']} "
        f"(допустимо {allowed}). По файлам: {current['files']}. "
        f"См. docs/phase1/ARCHITECTURE.md §2.2."
    )
    assert current["total"] == allowed, (
        f"Отлично: '{rule}' уменьшилось {allowed} → {current['total']}. "
        f"Обновите tests/arch/baseline.json (только в меньшую сторону)."
    )


if __name__ == "__main__":  # python -m tests.arch.test_boundaries — печать текущих значений
    m = measure()
    print(json.dumps({k: v["total"] for k, v in m.items()}, indent=2))
    for k, v in m.items():
        if v["files"]:
            print(f"\n{k}:")
            for f, n in sorted(v["files"].items(), key=lambda x: -x[1]):
                print(f"  {n:4d}  {f}")
