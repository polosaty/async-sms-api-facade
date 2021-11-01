import asyncio

import aioredis
import anyio
import trio_asyncio


async def test_redis():

    async with trio_asyncio.open_loop() as loop:
        asyncio._set_running_loop(loop)

        async def _test_redis():
            redis = await aioredis.create_redis_pool("redis://redis")

            try:
                await redis.set("my-key", "value", expire=1)
                value1 = await redis.get("my-key")
                await anyio.sleep(2)
                value2 = await redis.get("my-key")
                return value1, value2
            finally:
                redis.close()
                await redis.wait_closed()

        value1, value2 = await trio_asyncio.aio_as_trio(_test_redis)()
        assert value1 == b'value'
        assert value2 is None
