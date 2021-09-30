import logging
import random
import socket
from typing import Coroutine, Dict, List, Union
from unittest.mock import patch
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlunparse

import asks
from asks.response_objects import Response
from pydantic import BaseModel
from pydantic import ValidationError
import trio_asyncio

logger = logging.getLogger('utils')


async def run_asyncio(coroutine: Coroutine):
    return await trio_asyncio.run_aio_coroutine(coroutine)


class MockResponse(Response):
    def __init__(self, json, status_code=200):
        self._json = json
        self.status_code = status_code

    def json(self, **kwargs):
        return self._json

    def __repr__(self):
        """For pretty assertion error"""
        return f'<Response {self.status_code}>'


class SmscApiError(Exception):
    def __init__(self, message, response=None):
        self.response = response
        self.message = message

    def __repr__(self):
        return f'<SmscApiError({self.message!r}, {self.response!r})>'


def update_url(url, params):
    parsed_url = urlparse(url)
    url_dict = dict(parse_qsl(parsed_url.query))
    url_dict.update(params)
    url_new_query = urlencode(url_dict)
    new_url = urlunparse(parsed_url._replace(query=url_new_query))
    return new_url


class GetUrlFromPayloadMixin:
    def get_url(self, login, password):
        return update_url(self.base_url, dict(
            login=login,
            psw=password,
            **self.dict(exclude={'base_url'})
        ))


class StatusPayload(GetUrlFromPayloadMixin, BaseModel):
    base_url = 'https://smsc.ru/sys/status.php'
    phone: Union[str, List[str]]
    id: Union[int, List[int]]
    fmt = 3  # json

    def dict(self, *args, **kwargs):
        d = super().dict(*args, **kwargs)
        id_ = d.get('id')
        phone = d.get('phone')
        if isinstance(id_, list):
            d['id'] = ','.join(map(str, id_))

        d['id'] = f"{d['id']},"
        if isinstance(phone, list):
            d['phone'] = ','.join(phone)
        d['phone'] += ','
        return d


class SendPayload(GetUrlFromPayloadMixin, BaseModel):
    base_url = 'https://smsc.ru/sys/send.php'
    phones: Union[str, List[str]]
    mes: str
    charset = 'utf-8'  # 'windows-1251', 'koi8-r'
    fmt = 3  # json
    # cost = 1  # don't send only cost

    def dict(self, *args, **kwargs):
        d = super().dict(*args, **kwargs)
        phones = d.get('phones')
        if isinstance(phones, list):
            d['phone'] = ','.join(phones)

        d['mes'] = self.mes.encode(self.charset)
        return d


class SmsSendResponse(BaseModel):
    id: int
    cnt: int


async def request_smsc(
        method: str,
        login: str,
        password: str,
        payload: Dict[str, Union[str, List[str]]]
) -> Union[Dict, List]:
    """Send request to SMSC.ru service.

    Args:
        method (str): API method. E.g. 'send' or 'status'.
        login (str): Login for account on SMSC.
        password (str): Password for account on SMSC.
        payload (dict): Additional request params, override default ones.
    Returns:
        dict: Response from SMSC API.
    Raises:
        SmscApiError: If SMSC API response status is not 200 or it has `"ERROR" in response.

    Examples:
        >>> request_smsc("send", "my_login", "my_password", {"phones": "+79123456789", "mes": "test"})
        {"cnt": 1, "id": 24}
        >>> request_smsc("status", "my_login", "my_password", {"phone": "+79123456789", "id": "24"})
        {'status': 1, 'last_date': '28.12.2019 19:20:22', 'last_timestamp': 1577550022}
    """

    if method == 'status':
        Payload = StatusPayload
    elif method == 'send':
        Payload = SendPayload
    else:
        raise SmscApiError(f'unknown method {method}')

    try:
        _payload = Payload.validate(payload)
    except ValidationError as ex:
        raise SmscApiError(f'wrong payload {ex!r}')

    url = _payload.get_url(login=login, password=password)

    try:
        response: Response = await asks.request('GET', url)
    except socket.gaierror:
        raise SmscApiError(f'smsc.ru api inaccessible')

    if response.status_code != 200:
        raise SmscApiError(f'status_code: {response.status_code}', response,)

    response_json = response.json()
    if isinstance(response_json, dict) and 'error' in response_json:
        raise SmscApiError(f'response has error: {response_json["error"]}', response,)

    return response_json


def mock_asks_request_for_dry_run():
    def get_sms_id():
        _id = 24
        while True:
            _id += 1
            yield _id

    sms_id_gen = get_sms_id()

    async def request_side_effect(method, uri, **kwargs):
        logger.debug('call request_side_effect(method=%r, uri=%r, kwargs=%r)', method, uri, kwargs)
        sms_status = {'status': random.choice([0, 1]), 'last_date': '28.12.2019 19:20:22', 'last_timestamp': 1577550022}

        sms_send = {"cnt": 1, "id": next(sms_id_gen)}
        if 'status.php' in uri:
            get_params = dict(parse_qsl(urlparse(uri).query))
            ids = get_params['id'].split(',')
            phones = get_params['phone'].split(',')

            response = [
                dict(
                    sms_status,
                    id=ids[indx],
                    phone=phone,
                    status=random.choice([1, 2, 4, -3, 3, 20, 22, 23, 24, 25, 0]),
                )
                for indx, phone in enumerate(phones)
                if phone
            ]

            return MockResponse(response)
        if 'send.php' in uri:
            return MockResponse(sms_send)

    asks_request_patcher = patch('asks.request', side_effect=request_side_effect)
    asks_request_patcher.start()
