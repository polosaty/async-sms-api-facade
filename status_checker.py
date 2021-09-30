import asyncio
from contextlib import suppress
import logging
import os

import aioredis
import anyio
import asyncclick as click
import trio_asyncio

from db import Database
from server import broadcast_error_to_redis_channels
from server import broadcast_sms_update_to_redis_channels
from utils import mock_asks_request_for_dry_run
from utils import request_smsc
from utils import run_asyncio
from utils import SmscApiError
from utils import StatusPayload

MAXIMUM_URL_LEN = 2000

logger = logging.getLogger('status_checker')


async def update_status(db, login, password):
    while True:
        try:
            pending_sms_list = await run_asyncio(db.get_pending_sms_list())
            logger.debug('pending_sms_list: %r', pending_sms_list)

            sms_ids = []
            phones_map = {}
            url_len = len(StatusPayload(phone='', id='1').get_url(login, password)) - 1
            for sms_id, phone in pending_sms_list:
                phone_adapted = phone.replace('+7', '7').replace(' ', '')
                additional_url_len = len(sms_id) + 1 + len(phone_adapted) + 1

                if url_len + additional_url_len > MAXIMUM_URL_LEN:
                    response = await request_smsc(
                        'status', login, password, {"phone": list(phones_map.keys()), "id": sms_ids})
                    await handle_status_result(db, response, phones_map)
                    sms_ids = []
                    phones_map = {}
                    url_len = len(StatusPayload(phone='', id='1').get_url(login, password)) - 1

                sms_ids.append(sms_id)
                phones_map[phone_adapted] = phone
                url_len += additional_url_len

            if sms_ids:
                response = await request_smsc(
                    'status', login, password, {"phone": list(phones_map.keys()), "id": sms_ids})
                await handle_status_result(db, response, phones_map)
        except SmscApiError:
            await broadcast_error_to_redis_channels(db, {"errorMessage": "Связь SMSC потеряна"})
        await anyio.sleep(5)


async def handle_status_result(db, response, phones_map):
    delivered_status = (1, 2, 4)
    failed_status = (-3, 3, 20, 22, 23, 24, 25)
    updates = []
    update_sms_ids = []
    for status_message in response:
        status = status_message['status']
        sms_id = status_message['id']
        phone = phones_map.get(status_message['phone'])
        if not phone:
            logger.warning('can\'t find mapping for phone %r', status_message['phone'])
            continue

        if status in delivered_status:
            updates.append([sms_id, phone, 'delivered'])
            update_sms_ids.append(sms_id)
        if status in failed_status:
            updates.append([sms_id, phone, 'failed'])
            update_sms_ids.append(sms_id)

    await run_asyncio(db.update_sms_status_in_bulk(updates))
    await broadcast_sms_update_to_redis_channels(db, *update_sms_ids)


@click.command()
@click.option("--fake", is_flag=True, help="не отправлять реальные запросы (dry run)")
@click.option("--redis_uri", envvar='REDIS_URI', default='redis://redis', type=click.STRING, help="redis uri")
@click.option("--redis_password", envvar='REDIS_PASSWORD', default=None, type=click.STRING, help="пароль от redis")
@click.option("--smsc_login", envvar='LOGIN', default='login', type=click.STRING, help="логин от smsc.ru")
@click.option("--smsc_password", envvar='PASSWORD', default='pass', type=click.STRING, help="пароль от smsc.ru")
@click.option('-v', '--verbose', count=True, help="настройка логирования")
async def main(fake, redis_uri, redis_password, smsc_login, smsc_password, verbose):
    logging.basicConfig(
        level={
            1: logging.WARNING,
            2: logging.INFO,
            3: logging.DEBUG,
        }.get(verbose, os.getenv('LOG_LEVEL', logging.ERROR))
    )
    async with trio_asyncio.open_loop() as loop:
        asyncio._set_running_loop(loop)
        redis = await run_asyncio(
            aioredis.create_redis_pool(
                redis_uri,
                password=redis_password,
                encoding='utf-8',
            )
        )
        if fake:
            mock_asks_request_for_dry_run()

        await update_status(Database(redis), login=smsc_login, password=smsc_password)


if __name__ == "__main__":
    main(_anyio_backend="trio")
