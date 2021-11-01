from typing import Coroutine

import trio_asyncio


async def run_asyncio(coroutine: Coroutine):
    return await trio_asyncio.run_aio_coroutine(coroutine)
