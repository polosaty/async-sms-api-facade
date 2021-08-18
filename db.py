import time
import json
from typing import Optional


def _clean_key(key):
    cleaned_key = str(key)
    if '_' in cleaned_key:
        raise ValueError('Forbidden symbol `_` found in database key.')
    return cleaned_key


def _clean_sms_status(value):
    cleaned_value = str(value).lower()
    if cleaned_value not in ['delivered', 'failed', 'pending']:
        raise ValueError(f'Unknown status found: `{cleaned_value}`. Wanted one of delivered, failed or pending.')

    return cleaned_value


class Database:
    """База данных Redis, хранит данные об SMS рассылках.

    Схема ключей в базе данных:

        tracked_sms_{sms_id}_{phone} —> timestamp (когда начали следить за SMS)
        sms_mailing_{sms_id} —> JSON с информацией о рассылке
        phones_for_sms_mailing_{sms_id} —> hset {phone}:{status} (статус доставки)
    """

    def __init__(self, redis):
        self.redis = redis

    async def add_sms_mailing(self, sms_id: str, phones: list, text: str, created_at: Optional[float] = None):
        """Add to Redis all records required to represent new SMS mailing."""
        sms_id_key = _clean_key(sms_id)

        mailing_key = f'sms_mailing_{sms_id_key}'
        mailing_phones_key = f'phones_for_sms_mailing_{sms_id_key}'

        tr = self.redis.multi_exec()
        tr.set(mailing_key, json.dumps({
            'sms_id': sms_id,
            'text': text,
            'created_at': float(created_at or time.time()),
            'phones_count': len(phones),
        }, ensure_ascii=False))

        for phone in phones:
            # escaping for phone number is not required here, any string is acceptable
            tr.hset(mailing_phones_key, phone, 'pending')

        await tr.execute()

    async def get_pending_sms_list(self):
        """Get from Redis all pending messages."""
        keys = await self.redis.keys('phones_for_sms_mailing_*')

        pipe = self.redis.pipeline()
        for key in keys:
            pipe.hgetall(key)

        phones_pairs_groups = await pipe.execute()

        pending_sms_list = []
        for key, phone2status in zip(keys, phones_pairs_groups):
            *_, sms_id_key = key.split('_')

            pending_phones = (phone for phone, status in phone2status.items() if status == 'pending')
            pending_sms_list.extend((sms_id_key, phone) for phone in pending_phones)

        return pending_sms_list

    async def update_sms_status_in_bulk(self, sms_list):
        """Receives list of tuples (sms_id, phone, status)."""
        tr = self.redis.multi_exec()

        for sms_id, phone, status in sms_list:
            sms_id_key = _clean_key(sms_id)
            cleaned_status = _clean_sms_status(status)
            mailing_phones_key = f'phones_for_sms_mailing_{sms_id_key}'
            tr.hset(mailing_phones_key, phone, cleaned_status)

        await tr.execute()

    async def get_sms_mailings(self, *sms_ids: str) -> list:
        """For each mailing in sms_ids load all data from Redis and return dict."""
        pipe = self.redis.pipeline()
        for sms_id in sms_ids:
            sms_id_key = _clean_key(sms_id)
            mailing_key = f'sms_mailing_{sms_id_key}'
            phones_key = f'phones_for_sms_mailing_{sms_id_key}'

            pipe.get(mailing_key)
            pipe.hgetall(phones_key)

        values = await pipe.execute()

        mailings = []
        for json_text, phones in zip(values[::2], values[1::2]):

            if not json_text:
                # SMS mailing was not found
                continue

            mailings.append({
                **json.loads(json_text),
                'phones': phones,
            })

        return mailings

    async def list_sms_mailings(self):
        """Return list of sms_id for all registered SMS mailings."""
        keys = await self.redis.keys(f'sms_mailing_*')
        return [key.split('_')[-1] for key in keys]
