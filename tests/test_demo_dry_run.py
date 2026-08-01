"""Smoke-тест сухого прогона демо (scripts/demo_dry_run.py).

Гарантирует, что DEMO_RUNBOOK-сценарий остаётся рабочим после любых правок:
расшифровка содержит ключевые события каждой сцены. Сам скрипт гоняет
реальные workflow на временной БД — если расшифровка меняется намеренно,
обновляйте и DEMO_TRANSCRIPT.md (--out).
"""

import importlib.util
import os

import pytest


def _load_dry_run():
    spec = importlib.util.spec_from_file_location(
        "demo_dry_run",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "demo_dry_run.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_dry_run_covers_all_scenes(tmp_path):
    mod = _load_dry_run()
    old_cwd = os.getcwd()
    try:
        md = await mod.run_demo(workdir=str(tmp_path))
    finally:
        os.chdir(old_cwd)  # run_demo chdir'ит в рабочую папку — возвращаемся

    # Сцены присутствуют
    for scene in ("Сцена 2a", "Сцена 2b", "Сцена 2c", "Сцена 2d", "Сцена 2e", "Финал"):
        assert f"## {scene}" in md, scene

    # 2a: родитель получил кнопки и закрыл ситуацию одной
    assert "[✅ Всё в порядке] [❌ Сегодня не будет] [⏰ Опоздаем]" in md
    assert "ситуация #1 закрыта" in md
    assert "✅ Родитель подтвердил: всё в порядке" in md

    # 2b/2c: координатор узнал об опоздании (кнопка + свободный текст)
    assert md.count("⏰ Родитель сообщил: ученик опоздает") == 2
    assert "Ответ: Мы опоздаем на 15 минут — пробки" in md  # исходный текст дошёл
    assert "⏰ Спасибо! Отметили, что ученик опоздает" in md

    # 2d: эскалация с кнопками действий, закрыта координатором одним тапом
    assert "🚨 Эскалация: инцидент" in md
    assert "[✅ Закрыть ситуацию] [👤 Написать родителю ↗]" in md
    assert "Закрыто (Ольга" in md

    # 2e: отмена дошла до репетитора и координатора; неизвестный урок — честный
    # «не найден» БЕЗ противоречивого «передана» (регрессия бага сухого прогона)
    assert "📅 Отмена: Миша Иванов — mathematics" in md
    assert "❌ Урок unknown_lesson не найден" in md
    assert md.count("передана репетитору и координаторам") == 1

    # Финал: статистика сходится — 3 решил родитель, 1 координатор.
    # Резолюции — человекочитаемые (R7-3), а не сырые коды.
    assert "✅ Закрыто: 4" in md
    assert md.count("закрыто координатором") == 1
    assert md.count("опоздали") == 2
    assert "parent_late" not in md and "coordinator_closed" not in md
