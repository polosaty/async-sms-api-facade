from unittest.mock import AsyncMock
from unittest.mock import call
from unittest.mock import patch

import pytest

from utils import MockResponse
from utils import request_smsc
from utils import SmscApiError

# async def test_real_request_smsc():
#     # api = SmscApi(login, password)
#     # response = await api.send(['89995190557'], 'test проверка 🦝🦝')
#     """
#     {
#     "id": 182,
#     "cnt": 1
#     }
#     """
#     # response = await api.status([182, 181], ['89995190557'])
#     """
#     [{'status': 1, 'last_date': '15.08.2021 20:27:22', 'last_timestamp': 1629048442, 'id': 182, 'phone': '89995190557'},
#      {'status': 1, 'last_date': '15.08.2021 20:02:03', 'last_timestamp': 1629046923, 'id': 181, 'phone': '89995190557'}]
#     """
#
#     """
#     [{"status": -3, "id": 138, "phone": "89995190557"}]
#     """
#
#     """{'error': 'duplicate request, wait a minute', 'error_code': 9}"""
#
#     # response = await request_smsc('status', login, password, {"phone": "+79995190557", "id": "181"})


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
        [call('GET', ('https://smsc.ru/sys/status.php'
                      '?login=my_login&psw=my_password&phone=%2B79123456789&id=25&fmt=3'))])

    request_mock.reset_mock()
    request_mock.return_value = MockResponse(sms_send)
    result = await request_smsc('send', 'my_login', 'my_password', {"phones": "+79123456789",
                                                                    "mes": "test проверка 🦝🦝"})
    assert result == sms_send
    assert request_mock.await_count == 1
    request_mock.assert_awaited_once_with(
        'GET',
        ('https://smsc.ru/sys/send.php?login=my_login&psw=my_password'
         '&phones=%2B79123456789'
         '&mes=test+%D0%BF%D1%80%D0%BE%D0%B2%D0%B5%D1%80%D0%BA%D0%B0+%F0%9F%A6%9D%F0%9F%A6%9D&fmt=3'))

    with pytest.raises(SmscApiError) as ex:
        await request_smsc('send', 'my_login', 'my_password', {"phones": "+79123456789"})

    assert ex.value.message == ("wrong payload ValidationError(model='SendPayload', "
                                "errors=[{'loc': ('mes',), 'msg': 'field required', "
                                "'type': 'value_error.missing'}])")

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
