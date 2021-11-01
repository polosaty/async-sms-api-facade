from unittest.mock import AsyncMock
from unittest.mock import call
from unittest.mock import patch

import pytest

from smsc_api import MockResponse
from smsc_api import request_smsc
from smsc_api import SmscApiError


@patch('asks.request')
async def test_request_smsc(asks_request_patcher):
    request_mock: AsyncMock = AsyncMock()
    asks_request_patcher.side_effect = request_mock

    with pytest.raises(SmscApiError) as ex:
        _ = await request_smsc('noexist', 'login', 'pass', {'some': 'thing'})
    assert ex.match(r'unknown method noexist')

    sms_status = {'status': 1, 'last_date': '28.12.2019 19:20:22', 'last_timestamp': 1577550022}
    sms_send = {"cnt": 1, "id": 24}

    request_mock.return_value = MockResponse(sms_status)
    result = await request_smsc('status', 'my_login', 'my_password', {"phone": "+79123456789", "id": "25"})
    assert result == sms_status
    assert request_mock.await_count == 1
    request_mock.assert_has_awaits(
        [call('GET', 'https://smsc.ru/sys/status.php',
              params={'login': 'my_login', 'psw': 'my_password',
                      'phone': '+79123456789,', 'id': '25,', 'fmt': 3})])

    request_mock.reset_mock()
    request_mock.return_value = MockResponse(sms_send)
    result = await request_smsc('send', 'my_login', 'my_password', {"phones": "+79123456789",
                                                                    "mes": "test проверка 🦝🦝"})
    assert result == sms_send
    assert request_mock.await_count == 1
    request_mock.assert_awaited_once_with(
        'GET',
        'https://smsc.ru/sys/send.php',
        params={'login': 'my_login', 'psw': 'my_password', 'phones': '+79123456789',
                'mes': b'test \xd0\xbf\xd1\x80\xd0\xbe\xd0\xb2\xd0\xb5\xd1\x80\xd0\xba\xd0\xb0 '
                       b'\xf0\x9f\xa6\x9d\xf0\x9f\xa6\x9d',
                'charset': 'utf-8', 'fmt': 3})

    with pytest.raises(SmscApiError) as ex:
        await request_smsc('send', 'my_login', 'my_password', {"phones": "+79123456789"})

    assert ex.value.message == ("wrong payload ValidationError(model='SendPayload', errors=[{'loc': ('mes',), "
                                "'msg': 'field required', 'type': 'value_error.missing'}, {'loc': "
                                "('__root__',), 'msg': 'mes is required', 'type': 'value_error'}])")

    sms_error = {'error': 'duplicate request, wait a minute', 'error_code': 9}
    with pytest.raises(SmscApiError) as ex:
        request_mock.return_value = MockResponse(sms_error, status_code=400)
        await request_smsc('send', 'my_login', 'my_password', {"phones": "+79123456789", "mes": 'test'})

    assert (ex.value.response,
            ex.value.message) == (request_mock.return_value, 'status_code: 400')

    with pytest.raises(SmscApiError) as ex:
        request_mock.return_value = MockResponse(sms_error, status_code=200)
        await request_smsc('send', 'my_login', 'my_password', {"phones": "+79123456789", "mes": 'test'})

    assert (ex.value.response,
            ex.value.message) == (request_mock.return_value,
                                  'response has error: duplicate request, wait a minute')
