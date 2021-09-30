import asyncio
from contextlib import suppress
from functools import wraps
import json
import logging
import os
import uuid

import aioredis
import anyio
import asyncclick as click
from quart import request
from quart import send_from_directory
from quart import websocket
from quart_trio import QuartTrio
import trio
import trio_asyncio

from db import Database
from utils import mock_asks_request_for_dry_run
from utils import request_smsc
from utils import run_asyncio
from utils import SmscApiError
from utils import SmsSendResponse

logger = logging.getLogger('server')

CHANNEL_BROADCAST_KEY = 'channel:broadcast'

app = QuartTrio(__name__)


@app.route('/', methods=['GET', 'POST'])
async def index():
    return await send_from_directory('static', 'index.html', mimetype='text/html')


@app.route('/send/', methods=['POST'])
async def send():
    form = await request.form
    logger.debug('send form: %r', form)
    try:
        text = form['text']
    except AttributeError:
        return {'success': False, "errorMessage": "Bad params"}, 400

    try:
        phones = app.phones
        smsc_login, smsc_password = app.smsc_login, app.smsc_password
    except KeyError:
        return {'success': False, "errorMessage": "Server misconfigured"}, 500

    try:
        response = await request_smsc('send', smsc_login, smsc_password, {"phones": phones, "mes": text})
        logger.debug('response %r', response)
        response_obj = SmsSendResponse.validate(response)
        sms_id = response_obj.id
        await run_asyncio(app.db.add_sms_mailing(sms_id=sms_id, phones=phones, text=text))
        await broadcast_sms_update_to_redis_channels(app.db, sms_id)
    except SmscApiError as ex:
        return {'success': False, "errorMessage": ex.response.json() if ex.response else ex.message}

    return {'success': True, 'response': response}


def collect_websocket(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        send_channel: trio.MemorySendChannel
        receive_channel: trio.MemoryReceiveChannel
        send_channel, receive_channel = trio.open_memory_channel(0)
        app.websockets.add(send_channel)
        try:
            async with receive_channel, send_channel:
                return await func(receive_channel, *args, **kwargs)
        finally:
            app.websockets.remove(send_channel)
    return wrapper


async def broadcast(message):
    for send_channel in app.websockets:
        await send_channel.send(message)


async def make_ws_data(db, *sms_ids):
    ws_data = {
        "msgType": "SMSMailingStatus",
        "SMSMailings": []
    }
    mailings = await run_asyncio(db.get_sms_mailings(*sms_ids))
    logger.debug('mailings: %r', mailings)
    for mailing in sorted(mailings, key=lambda m: m['created_at']):
        delivered = 0
        failed = 0
        for phone_status in mailing['phones'].values():
            if phone_status == 'delivered':
                delivered += 1
            elif phone_status == 'failed':
                failed += 1

        ws_data["SMSMailings"].append({
            "timestamp": mailing['created_at'],
            "SMSText": mailing['text'],
            "mailingId": str(mailing['sms_id']),
            "totalSMSAmount": mailing['phones_count'],
            "deliveredSMSAmount": delivered,
            "failedSMSAmount": failed,
        })
    return ws_data


@app.websocket('/ws')
@collect_websocket
async def ws(receive_channel):

    sms_ids = await run_asyncio(app.db.list_sms_mailings())
    ws_data = await make_ws_data(app.db, *sms_ids)

    await websocket.send_json(ws_data)
    async for message in receive_channel:
        await websocket.send_json(message)


async def redis_channel_keepalive(channel_key):
    while True:
        await run_asyncio(app.redis.setex(f'{CHANNEL_BROADCAST_KEY}:{channel_key}:keepalive', 10, channel_key))
        await anyio.sleep(5)


async def listen_redis_channel(channel_key):
    async with trio.open_nursery() as nursery:
        channel, = await run_asyncio(app.redis.subscribe(channel_key))
        nursery.start_soon(redis_channel_keepalive, channel_key)
        while await run_asyncio(channel.wait_message()):
            message = await run_asyncio(channel.get_json())
            await broadcast(message)


async def broadcast_sms_update_to_redis_channels(db, *sms_ids):
    ws_data = json.dumps(await make_ws_data(db, *sms_ids))
    await broadcast_to_redis_channels(db, ws_data)


async def broadcast_error_to_redis_channels(db, error):
    ws_data = json.dumps(error)
    await broadcast_to_redis_channels(db, ws_data)


async def broadcast_to_redis_channels(db, ws_data):
    channel_key_wildcard = f'{CHANNEL_BROADCAST_KEY}:*'
    for channel_keepalive_key in await run_asyncio(db.redis.keys(channel_key_wildcard)):
        channel_key = await run_asyncio(db.redis.get(channel_keepalive_key))
        await run_asyncio(db.redis.publish(channel_key, ws_data))


@app.after_serving
async def close_db_pool():
    await run_asyncio(app.redis.delete(f'{CHANNEL_BROADCAST_KEY}:{app.channel_key}:keepalive'))
    app.redis.close()
    await run_asyncio(app.redis.wait_closed())


@click.command()
@click.option("--port", envvar='PORT', default=5000, type=click.INT, help="порт для браузера")
@click.option("--wet_run", is_flag=True, help="отправлять реальные запросы (противоположность dry run)")
@click.option("--flushdb", is_flag=True, help="очистить базу")
@click.option("--redis_uri", envvar='REDIS_URI', default='redis://redis', type=click.STRING, help="redis uri")
@click.option("--redis_password", envvar='REDIS_PASSWORD', default=None, type=click.STRING, help="пароль от redis")
@click.option("--smsc_login", envvar='LOGIN', default='login', type=click.STRING, help="логин от smsc.ru")
@click.option("--smsc_password", envvar='PASSWORD', default='pass', type=click.STRING, help="пароль от smsc.ru")
@click.option('-v', '--verbose', count=True, help="настройка логирования")
async def main(port, wet_run, flushdb, redis_uri, redis_password, smsc_login, smsc_password, verbose):
    logging.basicConfig(
        level={
            1: logging.WARNING,
            2: logging.INFO,
            3: logging.DEBUG,
        }.get(verbose, os.getenv('LOG_LEVEL', logging.ERROR))
    )
    app.smsc_login = smsc_login
    app.smsc_password = smsc_password

    app.websockets = set()
    app.phones = ["+79995190557", '112', '911', '+7 999 519 05 57']

    if not wet_run:
        mock_asks_request_for_dry_run()

    async with trio_asyncio.open_loop() as loop:
        asyncio._set_running_loop(loop)
        app.redis = await run_asyncio(
            aioredis.create_redis_pool(
                redis_uri,
                password=redis_password,
                encoding='utf-8',
            )
        )
        if flushdb:
            await run_asyncio(app.redis.flushdb())
        app.db = Database(app.redis)
        with suppress(aioredis.errors.PoolClosedError):
            async with trio.open_nursery() as nursery:
                channel_key = f'{CHANNEL_BROADCAST_KEY}:{uuid.uuid4()}'
                app.channel_key = channel_key
                nursery.start_soon(listen_redis_channel, channel_key)
                await app.run_task(host='0.0.0.0', port=port)


if __name__ == "__main__":
    main(_anyio_backend="trio")
