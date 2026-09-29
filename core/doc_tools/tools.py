import asyncio
from .location import _err, _file_arg, _locate, _locate_common
from .operations import _create, _edit, _inspect


async def tool_doc_inspect(args: dict) -> str:
    try:
        loc, data = await _locate(_file_arg(args))
        return _inspect(loc, data, args)
    except Exception as e:
        return _err(e)


async def tool_doc_edit(args: dict) -> str:
    try:
        loc, data = await _locate(_file_arg(args))
        return await _edit(loc, data, args)
    except Exception as e:
        return _err(e)


async def tool_doc_create(args: dict) -> str:
    try:
        return await _create(args, "device")
    except Exception as e:
        return _err(e)


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # called from inside the event loop's thread: common-space writes are plain file IO,
    # so drive the coroutine to completion synchronously
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError("common-space doc tool unexpectedly awaited")


def tool_doc_inspect_common(args: dict) -> str:
    try:
        loc, data = _locate_common(_file_arg(args))
        return _inspect(loc, data, args)
    except Exception as e:
        return _err(e)


def tool_doc_edit_common(args: dict) -> str:
    try:
        loc, data = _locate_common(_file_arg(args))
        return _run(_edit(loc, data, args))
    except Exception as e:
        return _err(e)


def tool_doc_create_common(args: dict) -> str:
    try:
        return _run(_create(args, "common"))
    except Exception as e:
        return _err(e)


AGENT_IMPLS = {
    "doc_inspect": tool_doc_inspect,
    "doc_edit": tool_doc_edit,
    "doc_create": tool_doc_create,
}
CHAT_IMPLS = {
    "doc_inspect": tool_doc_inspect_common,
    "doc_edit": tool_doc_edit_common,
    "doc_create": tool_doc_create_common,
}
