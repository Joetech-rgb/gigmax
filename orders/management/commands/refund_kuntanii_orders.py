from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ('Refund agents for Kuntanii-held orders. Dry run unless --apply is given. '
            'Safe to re-run: each order is credited at most once.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually move money.')
        parser.add_argument('--agent', help='Only this agent phone number.')
        parser.add_argument('--limit', type=int, help='Only the first N orders.')

    def handle(self, *args, **opts):
        from orders.kuntanii_refunds import run_sweep
        run_sweep(apply=opts['apply'], agent_phone=opts.get('agent'),
                  limit=opts.get('limit'), out=self.stdout.write)
