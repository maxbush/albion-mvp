import asyncio, pytest
from src.events.bus import EventBus
from src.events.types import Event

@pytest.mark.asyncio
async def test_pub_sub():
    b = EventBus(); r = []
    async def h(e): r.append(e)
    b.subscribe("t", h)
    await b.publish(Event("t", {"x":1}))
    assert len(r) == 1 and r[0].data["x"] == 1

@pytest.mark.asyncio
async def test_wildcard():
    b = EventBus(); s = []
    async def h(e): s.append(e.type)
    b.subscribe("*", h)
    await b.publish(Event("a",{})); await b.publish(Event("b",{}))
    assert s == ["a","b"]

@pytest.mark.asyncio
async def test_handler_exception():
    """Упавший хендлер не ломает шину, остальные продолжаются."""
    b = EventBus(); s = []
    async def fail(e): raise ValueError("test error")
    async def good(e): s.append(e)
    b.subscribe("t", fail)
    b.subscribe("t", good)
    await b.publish(Event("t", {}))
    assert len(s) == 1

@pytest.mark.asyncio
async def test_handler_fast():
    """Хендлер быстрее таймаута — норм."""
    b = EventBus(); s = []
    async def fast(e): s.append(e)
    b.subscribe("t", fast)
    await b.publish(Event("t", {}))
    assert len(s) == 1
