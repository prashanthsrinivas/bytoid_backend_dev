import asyncio


# def run_async(coro):
#     """Safe sync → async wrapper that always awaits the coroutine."""
#     if asyncio.iscoroutine(coro):
#         try:
#             loop = asyncio.get_running_loop()
#         except RuntimeError:
#             loop = None

#         if loop and loop.is_running():
#             future = asyncio.run_coroutine_threadsafe(coro, loop)
#             return future.result()
#         else:
#             return asyncio.run(coro)

#     return coro


def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, loop).result()
    else:
        return loop.run_until_complete(coro)
