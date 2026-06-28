import asyncio

import pytest

from ai_dev_assistant.orchestration.session_pool import SessionPool


def _pool(**kw):
    defaults = dict(
        max_concurrent=2,
        idle_ttl=0.05,
        reaper_interval=0.02,
        agent_provider=lambda name: object(),
    )
    defaults.update(kw)
    return SessionPool(**defaults)


async def test_reaper_terminates_idle_sessions():
    pool = _pool()
    pool.start()
    sess = await pool.acquire("researcher")
    pool.release(sess)  # now idle
    await asyncio.sleep(0.2)  # > idle_ttl + reaper_interval
    assert pool.reaped_total >= 1
    await pool.stop()


async def test_concurrency_cap_blocks_third_acquire():
    pool = _pool(max_concurrent=1, idle_ttl=100, reaper_interval=100)
    pool.start()
    s1 = await pool.acquire("a")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pool.acquire("a"), timeout=0.1)
    pool.release(s1)
    s2 = await asyncio.wait_for(pool.acquire("a"), timeout=0.5)  # now unblocked
    assert s2 is not None
    await pool.stop()


async def test_warm_session_is_reused():
    pool = _pool(max_concurrent=1, idle_ttl=100, reaper_interval=100)
    pool.start()
    s1 = await pool.acquire("coder")
    pool.release(s1)
    s2 = await pool.acquire("coder")  # should reuse the warm idle session
    assert pool.created_total == 1
    assert s2.id == s1.id
    pool.release(s2)
    await pool.stop()
