import asyncio

import aioredis
import anyio
import trio_asyncio

# from asyncio import events as _aio_event
# try:
#     _orig_run_get = _aio_event.get_running_loop
# except AttributeError:
#     pass
# else:
#     def _new_run_get():
#         return _aio_event._get_running_loop()
#     _aio_event.get_running_loop = _new_run_get


async def test_redis():

    async with trio_asyncio.open_loop() as loop:
        # import aioredis.util as aioredis_util
        # aioredis.stream.get_event_loop = lambda: loop
        # _aio_event.get_running_loop = lambda: loop
        # aioredis_util.get_event_loop = lambda: loop
        # asyncio.set_event_loop(loop)

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
