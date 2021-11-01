import collections
import itertools
import logging
import random
import socket
from typing import Dict, Iterable, List, Union
from unittest.mock import patch
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlunparse

import asks
from asks.response_objects import Response
from pydantic import BaseModel
from pydantic import root_validator
from pydantic import ValidationError
from pydantic import validator

logger = logging.getLogger('smsc_api')


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


class SmscPayloadModel(BaseModel):
    base_url = 'https://smsc.ru/sys/status.php'

    def get_url(self, login, password):
        return replace_url_params(self.base_url, dict(
            login=login,
            psw=password,
            **self.dict(exclude={'base_url'})
        ))


class StatusPayload(SmscPayloadModel):
    base_url = 'https://smsc.ru/sys/status.php'
    phone: Union[str, Iterable[str]]
    id: Union[int, Iterable[int]]
    fmt = 3  # json

    @validator('id')
    def validate_id(cls, v):
        if isinstance(v, int):
            return f"{v},"
        elif isinstance(v, collections.Iterable):
            return f"{','.join(map(str, v))},"
        else:
            raise ValueError('must be int or iterable of ints')

    @validator('phone')
    def validate_phone(cls, v):
        if isinstance(v, str):
            return f"{v},"
        elif isinstance(v, collections.Iterable):
            return f"{','.join(v)},"
        else:
            raise ValueError('must be str or iterable of strs')


class SendPayload(SmscPayloadModel):
    base_url = 'https://smsc.ru/sys/send.php'
    phones: Union[str, Iterable[str]]
    charset = 'utf-8'  # 'windows-1251', 'koi8-r'
    mes: str
    fmt = 3  # json

    @validator('phones')
    def validate_phones(cls, v):
        if isinstance(v, str):
            return v
        elif isinstance(v, collections.Iterable):
            return ','.join(v)
        else:
            raise ValueError('must be str or iterable of strs')

    @root_validator
    def validate_mess(cls, values):
        if 'mes' not in values:
            raise ValueError('mes is required')
        return dict(values, mes=values['mes'].encode(values['charset']))


class SmsSendResponse(BaseModel):
    id: int
    cnt: int


async def request_smsc(
        method: str,
        login: str,
        password: str,
        payload: Dict[str, Union[str, Iterable[str]]]
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

    SMSC_METHOD_TO_PAYLOAD = {
        'status': StatusPayload,
        'send': SendPayload,
    }

    if method not in SMSC_METHOD_TO_PAYLOAD:
        raise SmscApiError(f'unknown method {method}')

    Payload = SMSC_METHOD_TO_PAYLOAD.get(method)

    try:
        _payload = Payload.validate(payload)
    except ValidationError as ex:
        raise SmscApiError(f'wrong payload {ex!r}')

    try:
        response: Response = await asks.request('GET', _payload.base_url, params=dict(
            login=login,
            psw=password,
            **_payload.dict(exclude={'base_url'})
        ))
    except socket.gaierror:
        raise SmscApiError('smsc.ru api inaccessible')

    if response.status_code != 200:
        raise SmscApiError(f'status_code: {response.status_code}', response,)

    response_json = response.json()
    if isinstance(response_json, dict) and 'error' in response_json:
        raise SmscApiError(f'response has error: {response_json["error"]}', response,)

    return response_json


def mock_asks_request_for_dry_run():
    sms_id_gen = itertools.count(start=1, step=1)

    async def request_side_effect(method, uri, **kwargs):
        logger.debug('call request_side_effect(method=%r, uri=%r, kwargs=%r)', method, uri, kwargs)
        sms_status = {'status': random.choice([0, 1]), 'last_date': '28.12.2019 19:20:22', 'last_timestamp': 1577550022}

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
            sms_send = {"cnt": 1, "id": next(sms_id_gen)}
            return MockResponse(sms_send)

    asks_request_patcher = patch('asks.request', side_effect=request_side_effect)
    asks_request_patcher.start()


def replace_url_params(url, params):
    parsed_url = urlparse(url)
    new_url_params = urlencode(dict(parse_qsl(parsed_url.query), **params))
    new_url = urlunparse(parsed_url._replace(query=new_url_params))
    return new_url
