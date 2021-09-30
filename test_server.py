import asyncio
from contextlib import suppress
import json
import os
from unittest.mock import AsyncMock
from unittest.mock import call
from unittest.mock import patch

import aioredis
import pytest
import trio
import trio_asyncio

from server import CHANNEL_BROADCAST_KEY
from status_checker import handle_status_result
from utils import MockResponse
from utils import run_asyncio


async def test_index(testapp):
    client = testapp.test_client()
    response = await client.get('/')
    assert response.status_code == 200


class RedisMock:
    keys = AsyncMock()
    get = AsyncMock()
    publish = AsyncMock()


class DatabaseMock:
    redis = RedisMock()
    add_sms_mailing = AsyncMock()
    get_sms_mailings = AsyncMock()
    list_sms_mailings = AsyncMock()


@pytest.fixture()
def db_mock():
    return DatabaseMock()


@pytest.fixture(name='testapp')
async def testapp(db_mock: DatabaseMock):
    from server import app
    async with trio_asyncio.open_loop() as loop:
        asyncio._set_running_loop(loop)
        app.db = db_mock
        app.websockets = set()
        yield app


async def test_send(testapp):
    client = testapp.test_client()
    request_mock: AsyncMock = AsyncMock()
    sms_text = 'some text'
    sms_id = 1
    sms_send = {"cnt": 1, "id": sms_id}
    request_mock.return_value = MockResponse(sms_send)

    testapp.smsc_login, testapp.smsc_password = 'my_login', 'my_password'
    testapp.phones = ['123', '456']

    testapp.db.get_sms_mailings.return_value = [
        {'sms_id': sms_id, 'text': sms_text, 'created_at': 1633015729.2770965,
         'phones_count': len(testapp.phones),
         'phones': {phone: 'pending' for phone in testapp.phones}}]

    channel_keepalive_key = f'{CHANNEL_BROADCAST_KEY}:some-uuid:keepalive'
    channel_key = f'{CHANNEL_BROADCAST_KEY}:some-uuid'
    testapp.db.redis.keys.return_value = [channel_keepalive_key]
    testapp.db.redis.get.side_effect = lambda wildcard: {
        channel_keepalive_key: channel_key
    }.get(wildcard)

    with patch('asks.request') as asks_request_patcher:
        asks_request_patcher.side_effect = request_mock

        response = await client.post('/send/', form=dict(text=sms_text))

    # Запрос на /send/ возвращает статус 200
    assert response.status_code == 200
    assert (await response.json) == {'response': sms_send, 'success': True}

    # Он вызывает fetch_smsc
    assert request_mock.await_count == 1
    request_mock.assert_has_awaits(
        [call('GET', 'https://smsc.ru/sys/send.php?login=my_login&psw=my_password'
                     '&phones=%5B%27123%27%2C+%27456%27%5D'
                     '&mes=some+text&charset=utf-8&fmt=3&phone=123%2C456')])

    # Вызывает app.redis.publish
    assert testapp.db.redis.publish.await_count == 1
    testapp.db.redis.publish.assert_has_awaits(
        [call('channel:broadcast:some-uuid',
              json.dumps({"msgType": "SMSMailingStatus", "SMSMailings": [
                  {"timestamp": 1633015729.2770965, "SMSText": sms_text,
                   "mailingId": str(sms_id), "totalSMSAmount": 2,
                   "deliveredSMSAmount": 0, "failedSMSAmount": 0}]}))]
    )


async def test_websocket(testapp):
    test_client = testapp.test_client()
    testapp.db.get_sms_mailings.return_value = []
    async with test_client.websocket('/ws') as test_websocket:
        await test_websocket.send('что-нибудь')
        result = await test_websocket.receive_json()
    # Вы проверите, что если написать что-нибудь вашему вебсокету, он ответит вам этим:
    assert result == {"msgType": "SMSMailingStatus", "SMSMailings": []}


@pytest.fixture()
async def testapp_with_redis():
    from db import Database
    from server import app
    from server import listen_redis_channel

    async with trio_asyncio.open_loop() as loop:
        asyncio._set_running_loop(loop)
        app.redis = await run_asyncio(
            aioredis.create_redis_pool(
                os.getenv('REDIS_URI', 'redis://redis'),
                password=os.getenv('REDIS_PASSWORD'),
                encoding='utf-8',
            )
        )
        await run_asyncio(app.redis.flushdb())
        app.db = Database(app.redis)
        app.websockets = set()
        with suppress(aioredis.errors.PoolClosedError):
            async with trio.open_nursery() as nursery:
                channel_key = f'{CHANNEL_BROADCAST_KEY}:some_uuid'
                app.channel_key = channel_key
                nursery.start_soon(listen_redis_channel, channel_key)
                yield app
                app.redis.close()
                await run_asyncio(app.redis.wait_closed())
                nursery.cancel_scope.cancel()


async def test_subscribe(testapp_with_redis):
    app = testapp_with_redis
    test_client = app.test_client()
    request_mock: AsyncMock = AsyncMock()
    sms_text = 'some text'
    sms_id = 1
    sms_send = {"cnt": 1, "id": sms_id}
    request_mock.return_value = MockResponse(sms_send)

    app.smsc_login, app.smsc_password = 'my_login', 'my_password'
    app.phones = ['123', '456']

    async with test_client.websocket('/ws') as test_websocket:
        await test_websocket.send('что-нибудь')
        result = await test_websocket.receive_json()
        assert result == {"msgType": "SMSMailingStatus", "SMSMailings": []}

        with patch('asks.request') as asks_request_patcher:
            asks_request_patcher.side_effect = request_mock
            # Если в базе появится рассылка
            response = await test_client.post('/send/', form=dict(text=sms_text))
            assert response.status_code == 200
            assert (await response.json) == {'response': sms_send, 'success': True}

        # Server отправит её по вебсокету
        result = await test_websocket.receive_json()
        sms_timestamp = result['SMSMailings'][0]['timestamp']
        assert result == {'SMSMailings': [{'SMSText': 'some text', 'deliveredSMSAmount': 0, 'failedSMSAmount': 0,
                                           'mailingId': '1', 'timestamp': sms_timestamp, 'totalSMSAmount': 2}],
                          'msgType': 'SMSMailingStatus'}

        # и будет отправлять по ней обновления.
        response = [{'status': 1, 'id': str(sms_id), 'phone': app.phones[0]}]
        await handle_status_result(app.db, response, {phone: phone for phone in app.phones})

        result = await test_websocket.receive_json()
        assert result == {'SMSMailings': [{'SMSText': 'some text', 'deliveredSMSAmount': 1, 'failedSMSAmount': 0,
                                           'mailingId': '1', 'timestamp': sms_timestamp, 'totalSMSAmount': 2}],
                          'msgType': 'SMSMailingStatus'}
