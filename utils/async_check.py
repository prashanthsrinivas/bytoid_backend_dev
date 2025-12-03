import asyncio


def run_async(coro):
    """Safe sync → async wrapper that always awaits the coroutine."""
    if asyncio.iscoroutine(coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result()
        else:
            return asyncio.run(coro)

    return coro
