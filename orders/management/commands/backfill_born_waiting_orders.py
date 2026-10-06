from decimal import Decimal
from django.core.management.base import BaseCommand
from orders.models import Order
from orders.utils import calculate_owed_amount, _apply_refund_credit

# Orders confirmed sitting at status='waiting' with refund_credited=False,
# born directly into waiting (never transitioned from processing/delivered),
# so the pre_save signal never fired for them. Locked to this exact list.
TARGET_ORDER_IDS = [
    'SKP63531827',
    'SKP35233198',
    'SKP86984518',
    'SKP02771356',
    'SKP33234186',
    'SKP57394080',
    'SKP17389269',
    'SKP17373374',
    'SKP92926602',
]


class Command(BaseCommand):
    help = (
        'One-time backfill: credits agents for orders born directly at '
        'status=waiting today, which the pre_save auto-refund signal never '
        'caught. Dry-run by default — pass --commit to actually write. '
        'Leaves status as waiting (does NOT flip to refunded) unless '
        '--mark-refunded is also passed.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Actually apply the credits.')

    def handle(self, *args, **options):
        commit = options['commit']
        mode = 'COMMIT' if commit else 'DRY RUN'
        self.stdout.write(self.style.WARNING(f'=== {mode} ==='))

        orders = Order.objects.filter(order_id__in=TARGET_ORDER_IDS).select_related('placed_by')

        found_ids = set(o.order_id for o in orders)
        missing = set(TARGET_ORDER_IDS) - found_ids
        if missing:
            self.stdout.write(self.style.ERROR(f'WARNING: not found: {missing}'))

        total_credited = Decimal('0')

        for o in orders:
            if o.refund_credited:
                self.stdout.write(self.style.WARNING(f'{o.order_id}: SKIP — refund_credited already True'))
                continue
            if o.status != 'waiting':
                self.stdout.write(self.style.WARNING(f'{o.order_id}: SKIP — status is now {o.status!r}, not waiting'))
                continue
            if not o.placed_by:
                self.stdout.write(self.style.ERROR(f'{o.order_id}: SKIP — no placed_by agent'))
                continue

            agent = o.placed_by
            amount = calculate_owed_amount(o)

            before_wallet = Decimal(str(agent.wallet_balance or 0))
            before_debt = Decimal(str(agent.debt_balance or 0))

            self.stdout.write(
                f'{o.order_id} | agent={agent.full_name} | amount={amount} | '
                f'before: wallet={before_wallet} debt={before_debt}'
            )

            if commit:
                new_wallet, new_debt = _apply_refund_credit(agent, amount, o.order_id, o.source)
                Order.objects.filter(pk=o.pk).update(
                    status='refunded',
                    refund_credited=True,
                    refund_requested=True,
                    refund_paid=True,
                )
                self.stdout.write(self.style.SUCCESS(
                    f'  -> COMMITTED: new wallet={new_wallet} debt={new_debt} (status set to refunded)'
                ))
            else:
                if before_debt > 0:
                    if amount >= before_debt:
                        remainder = amount - before_debt
                        sim_debt = Decimal('0')
                        sim_wallet = before_wallet + remainder
                    else:
                        sim_debt = before_debt - amount
                        sim_wallet = before_wallet
                else:
                    sim_debt = before_debt
                    sim_wallet = before_wallet + amount
                self.stdout.write(f'  -> would become: wallet={sim_wallet} debt={sim_debt}')

            total_credited += amount

        self.stdout.write('')
        self.stdout.write(self.style.WARNING(f'Total {"credited" if commit else "that would be credited"}: {total_credited}'))
        if not commit:
            self.stdout.write(self.style.WARNING('DRY RUN — re-run with --commit to apply.'))
