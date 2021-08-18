import asyncio
import logging
import os

import aioredis
import anyio
import asyncclick as click
from quart import request
from quart import send_from_directory
from quart import websocket
from quart_trio import QuartTrio
import trio_asyncio

from db import Database
from utils import mock_asks_request_for_dry_run
from utils import request_smsc
from utils import run_asyncio
from utils import SmscApiError
from utils import SmsSendResponse

logger = logging.getLogger('server')


app = QuartTrio(__name__)


@app.route('/', methods=['GET', 'POST'])
async def index():
    return await send_from_directory('static', 'index.html', mimetype='text/html')


@app.route('/send/', methods=['POST'])
async def send():
    form = await request.form
    logger.debug('send form: %r', form)
    try:
        phones = ["+79995190557"]
        text = form['text']
        response = await request_smsc('send', app.smsc_login, app.smsc_password, {"phones": phones, "mes": text})
        logger.debug('response %r', response)
        response_obj = SmsSendResponse.validate(response)
        await run_asyncio(app.db.add_sms_mailing(sms_id=response_obj.id, phones=phones, text=text))
        mailings = await run_asyncio(app.db.list_sms_mailings())
        logger.debug('mailings %r', mailings)
    except SmscApiError as ex:
        return {'success': False, 'response': ex.response.json() if ex.response else ex.message}
    return {'success': True, 'response': response}


@app.websocket('/ws')
async def ws():

    while True:
        sms_ids = await run_asyncio(app.db.list_sms_mailings())
        mailings = await run_asyncio(app.db.get_sms_mailings(*sms_ids))
        logger.debug('mailings: %r', mailings)
        ws_data = {
            "msgType": "SMSMailingStatus",
            "SMSMailings": []
        }
        for mailing in sorted(mailings):
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

        await websocket.send_json(ws_data)
        await anyio.sleep(3)


@app.after_serving
async def close_db_pool():
    app.redis.close()
    await run_asyncio(app.redis.wait_closed())


@click.command()
@click.option("--port", envvar='PORT', default=5000, type=click.INT, help="порт для браузера")
@click.option("--wet_run", is_flag=True, help="отправлять реальные запросы (противоположность dry run)")
@click.option("--redis_uri", envvar='REDIS_URI', default='redis://redis', type=click.STRING, help="redis uri")
@click.option("--redis_password", envvar='REDIS_PASSWORD', default=None, type=click.STRING, help="пароль от redis")
@click.option("--smsc_login", envvar='LOGIN', default='login', type=click.STRING, help="логин от smsc.ru")
@click.option("--smsc_password", envvar='PASSWORD', default='pass', type=click.STRING, help="пароль от smsc.ru")
@click.option('-v', '--verbose', count=True, help="настройка логирования")
async def main(port, wet_run, redis_uri, redis_password, smsc_login, smsc_password, verbose):
    logging.basicConfig(
        level={
            1: logging.WARNING,
            2: logging.INFO,
            3: logging.DEBUG,
        }.get(verbose, os.getenv('LOG_LEVEL', logging.ERROR))
    )
    app.smsc_login = smsc_login
    app.smsc_password = smsc_password

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
        app.db = Database(app.redis)
        await app.run_task(host='0.0.0.0', port=port)


if __name__ == "__main__":
    main(_anyio_backend="trio")
