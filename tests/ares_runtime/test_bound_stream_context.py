"""A lazy SDK stream may be driven in copied contexts/tasks.

The sentinel is a scope witness, not an admitted materialization or permit.
No provider request is made by these fixtures.
"""
import asyncio
import contextvars
import pytest
from ares_runtime import physical_dispatch as physical


def test_sync_iterator_steps_restore_scope_before_context_handoff():
    basis=object();seen=[]
    def chunks():
        for item in ('a','b'):
            seen.append(physical._call.get())
            yield item
    wrapped=physical.bind_deferred_result(chunks(),basis)
    iterator=iter(wrapped)
    contexts=[contextvars.Context() for _ in range(3)]
    assert contexts[0].run(next,iterator)=='a'
    assert contexts[1].run(next,iterator)=='b'
    with pytest.raises(StopIteration):contexts[2].run(next,iterator)
    assert seen==[basis,basis]
    assert all(ctx.get(physical._call,None) is None for ctx in contexts)


def test_sync_send_throw_and_close_keep_basis_without_leaking_it():
    basis=object();seen=[]
    def source():
        try:
            seen.append(physical._call.get())
            value=yield 'start'
            seen.append((value,physical._call.get()))
            try:yield 'sent'
            except ValueError:
                seen.append(physical._call.get())
                yield 'caught'
        finally:seen.append(('closed',physical._call.get()))
    wrapped=physical.bind_deferred_result(source(),basis)
    iterator=iter(wrapped);contexts=[contextvars.Context() for _ in range(4)]
    try:
        assert contexts[0].run(next,iterator)=='start'
        assert contexts[1].run(iterator.send,'value')=='sent'
        assert contexts[2].run(iterator.throw,ValueError('fixture'))=='caught'
    finally:
        contexts[0].run(iterator.close)
    assert seen==[basis,('value',basis),basis,('closed',basis)]
    assert all(ctx.get(physical._call,None) is None for ctx in contexts)


def test_async_iterator_can_advance_in_distinct_tasks():
    basis=object();seen=[]
    async def source():
        for item in ('a','b'):
            seen.append(physical._call.get())
            yield item
    async def run():
        wrapped=physical.bind_deferred_result(source(),basis)
        iterator=wrapped.__aiter__()
        async def one():
            value=await iterator.__anext__()
            return value,physical._call.get()
        try:
            first=await asyncio.create_task(one())
            second=await asyncio.create_task(one())
            with pytest.raises(StopAsyncIteration):await asyncio.create_task(iterator.__anext__())
            assert first==('a',None) and second==('b',None)
            assert seen==[basis,basis]
        finally:await wrapped.aclose()
    asyncio.run(run())


def test_async_close_uses_its_own_task_scope():
    basis=object();seen=[]
    async def source():
        try:
            seen.append(physical._call.get())
            yield 'one'
        finally:seen.append(('closed',physical._call.get()))
    async def run():
        wrapped=physical.bind_deferred_result(source(),basis)
        iterator=wrapped.__aiter__()
        await asyncio.create_task(iterator.__anext__())
        await asyncio.create_task(wrapped.aclose())
        assert seen==[basis,('closed',basis)]
        assert physical._call.get() is None
    asyncio.run(run())
