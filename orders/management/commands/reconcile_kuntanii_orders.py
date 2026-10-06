from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):
    help = (
        'Finds MTN orders sitting in "processing" with a real kuntanii_reference '
        'for too long, and re-checks their live status directly with Kuntanii. '
        'Mirrors the inline sweep previously only triggered when an admin loaded '
        'the main MTN orders admin page — this puts it on its own schedule so '
        'orders resolve consistently instead of depending on admin activity. '
        'Any order whose Kuntanii status is unrecognized is now flagged '
        '"kuntanii_unverified" for manual review, rather than auto-refunded.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--older-than-minutes', type=int, default=15,
            help='Only re-check orders that have been stuck in "processing" longer than this.'
        )

    def handle(self, *args, **options):
        from orders.models import Order
        from orders.kuntanii import check_kuntanii_status

        cutoff = timezone.now() - timedelta(minutes=options['older_than_minutes'])

        stuck = Order.objects.filter(
            network='MTN',
            status='processing',
            kuntanii_reference__isnull=False,
            updated_at__lt=cutoff,
        ).exclude(kuntanii_reference='')

        count = stuck.count()
        self.stdout.write(f'Found {count} MTN orders stuck in processing older than {options["older_than_minutes"]} min.')

        for o in stuck:
            self.stdout.write(f'Checking {o.order_id} (ref: {o.kuntanii_reference})...')
            try:
                kt_status = check_kuntanii_status(o.order_id)
                o.refresh_from_db()
                self.stdout.write(f'  -> Kuntanii status: {kt_status}, our status now: {o.status}')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  -> FAILED: {e}'))
