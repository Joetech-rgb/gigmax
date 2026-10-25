import json
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from orders.models import Order
from wallet.models import WalletTransaction


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except ValueError:
        return None


class Command(BaseCommand):
    help = ('List successful Paystack payments with no matching order or wallet credit. '
            'Report only, unless --apply is given (then orders are created from payment metadata).')

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=2)
        parser.add_argument('--min-age-minutes', type=int, default=15)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        key = settings.PAYSTACK_SECRET_KEY
        if not key:
            self.stdout.write('PAYSTACK_SECRET_KEY is empty. Nothing to check.')
            return
        now = datetime.now(dt_tz.utc)
        since = now - timedelta(days=opts['days'])
        cutoff = now - timedelta(minutes=opts['min_age_minutes'])
        headers = {'Authorization': 'Bearer ' + key}

        txs = []
        for page in range(1, 21):
            r = requests.get('https://api.paystack.co/transaction', headers=headers, timeout=30,
                             params={'status': 'success', 'perPage': 100, 'page': page,
                                     'from': since.strftime('%Y-%m-%dT%H:%M:%S.000Z')})
            try:
                j = r.json()
            except ValueError:
                j = {}
            if r.status_code != 200 or not j.get('status'):
                self.stdout.write('Paystack error %s: %s' % (r.status_code, j.get('message')))
                return
            data = j.get('data') or []
            if not data:
                break
            txs.extend(data)
        self.stdout.write('Successful payments in the last %d day(s): %d' % (opts['days'], len(txs)))

        from wallet.views import _create_order_from_metadata
        problems = fixed = 0
        for tx in txs:
            ref = tx.get('reference') or ''
            paid = _parse(tx.get('paid_at') or tx.get('created_at'))
            if paid is not None and paid.tzinfo is not None and paid > cutoff:
                continue
            amount = Decimal(str(tx.get('amount', 0))) / 100
            if ref.startswith('SKP-'):
                if not WalletTransaction.objects.filter(reference=ref).exists():
                    problems += 1
                    self.stdout.write('WALLET NOT CREDITED  %s  GHS %s  (replay the webhook from the Paystack dashboard)' % (ref, amount))
                continue
            order = Order.objects.filter(payment_reference=ref).first()
            if order:
                if order.payment_status != 'paid':
                    problems += 1
                    self.stdout.write('ORDER NOT MARKED PAID  %s  order %s  GHS %s' % (ref, order.order_id, amount))
                continue
            meta = tx.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except ValueError:
                    meta = {}
            tx['metadata'] = meta if isinstance(meta, dict) else {}
            ok = bool(tx['metadata'].get('phone') and tx['metadata'].get('package_id') and tx['metadata'].get('price'))
            problems += 1
            if ok and opts['apply']:
                _create_order_from_metadata(tx, ref)
                if Order.objects.filter(payment_reference=ref).exists():
                    fixed += 1
                    self.stdout.write('CREATED ORDER  %s  GHS %s' % (ref, amount))
                else:
                    self.stdout.write('COULD NOT CREATE  %s  GHS %s  (see the server log)' % (ref, amount))
            else:
                self.stdout.write('NO ORDER  %s  GHS %s  %s' % (ref, amount,
                    'recoverable from metadata (run with --apply)' if ok else 'no usable metadata, handle manually'))
        self.stdout.write('Problems: %d | fixed: %d' % (problems, fixed))
