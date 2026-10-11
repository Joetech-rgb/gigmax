from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = ('Re-checks MTN orders that exist at Kuntanii (processing or unverified, not yet '
            'refunded). Delivered orders are updated, unreadable replies start the refund '
            'clock. Money is moved only by refund_kuntanii_orders.')

    def add_arguments(self, parser):
        parser.add_argument('--older-than-minutes', type=int, default=15)

    def handle(self, *args, **options):
        from orders.models import Order
        from orders.kuntanii import check_kuntanii_status

        cutoff = timezone.now() - timedelta(minutes=options['older_than_minutes'])
        qs = (Order.objects.filter(
                network='MTN', status__in=['processing', 'kuntanii_unverified'],
                kuntanii_reference__isnull=False, refund_credited=False,
                updated_at__lt=cutoff)
              .exclude(kuntanii_reference=''))
        ids = list(qs.values_list('order_id', flat=True))
        self.stdout.write('Checking %d orders' % len(ids))
        for oid in ids:
            try:
                check_kuntanii_status(oid)
            except Exception as e:
                self.stdout.write(self.style.ERROR('%s: %s' % (oid, e)))
