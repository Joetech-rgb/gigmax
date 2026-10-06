from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):
    help = (
        'Finds paid, pending MTN orders that were never dispatched to a '
        'processor (no kuntanii_reference, no gati_order_id) and retries '
        'dispatch. Covers cases where verify_beneficiary() or the dispatch '
        'call failed transiently and was left "untouched for retry" with '
        'no actual retry mechanism (found 2026-09-18).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--older-than-minutes', type=int, default=5,
            help='Only retry orders older than this many minutes (avoid racing a just-placed order).'
        )

    def handle(self, *args, **options):
        from orders.models import Order
        from orders.dispatch import dispatch_mtn_order

        cutoff = timezone.now() - timedelta(minutes=options['older_than_minutes'])

        stuck = Order.objects.filter(
            network='MTN',
            status='pending',
            payment_status='paid',
            kuntanii_reference__isnull=True,
            gati_order_id__isnull=True,
            created_at__lt=cutoff,
        )

        count = stuck.count()
        self.stdout.write(f'Found {count} undispatched paid MTN orders older than {options["older_than_minutes"]} min.')

        for o in stuck:
            self.stdout.write(f'Retrying {o.order_id} ({o.recipient})...')
            try:
                result = dispatch_mtn_order(
                    recipient=o.recipient,
                    gb_size=float(o.gb_package.gb_size) if o.gb_package else 0,
                    order_id=o.order_id,
                )
                o.refresh_from_db()
                self.stdout.write(f'  -> status now: {o.status}, kuntanii_ref: {o.kuntanii_reference or "-"}')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  -> FAILED: {e}'))
