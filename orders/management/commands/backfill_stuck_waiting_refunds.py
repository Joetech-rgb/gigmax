from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from orders.models import Order
from orders.utils import calculate_owed_amount, _apply_refund_credit

# Locked to this exact known list — deliberately NOT a broad queryset,
# so this command can never silently pick up unrelated orders later.
TARGET_ORDER_IDS = [
    'SKP57611795',
    'SKP65051513',
    'SKP51553498',
    'SKP71313291',
    'SKP99556864',
    'SKP23956022',
    'SKP52095146',
    'SKP90422724',
    'SKP40257281',
    'SKP61584216',
    'SKP37048896',
]


class Command(BaseCommand):
    help = (
        'One-time backfill: credits agents for orders that were created '
        'directly at status=waiting (never transitioned from '
        'processing/delivered), so the pre_save auto-refund signal never '
        'fired for them. Dry-run by default — pass --commit to actually write.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Actually apply the credits. Without this flag, nothing is written.')

    def handle(self, *args, **options):
        commit = options['commit']
        mode = 'COMMIT' if commit else 'DRY RUN'
        self.stdout.write(self.style.WARNING(f'=== {mode} ==='))

        orders = Order.objects.filter(
            order_id__in=TARGET_ORDER_IDS,
        ).select_related('placed_by')

        found_ids = set(o.order_id for o in orders)
        missing = set(TARGET_ORDER_IDS) - found_ids
        if missing:
            self.stdout.write(self.style.ERROR(f'WARNING: {len(missing)} target order(s) not found at all: {missing}'))

        for o in orders:
            self.stdout.write(f'{o.order_id}: current status={o.status!r}, refund_credited={o.refund_credited}')

        total_credited = Decimal('0')

        for o in orders:
            if o.refund_credited:
                self.stdout.write(self.style.WARNING(f'{o.order_id}: SKIP — refund_credited already True'))
                continue

            if not o.placed_by:
                self.stdout.write(self.style.ERROR(f'{o.order_id}: SKIP — no placed_by agent on record'))
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
                    refund_credited=True,
                    refund_requested=True,
                    refund_paid=True,
                )
                self.stdout.write(self.style.SUCCESS(
                    f'  -> COMMITTED: new wallet={new_wallet} debt={new_debt} (status left as-is: {o.status})'
                ))
            else:
                # Simulate the math without writing anything
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
        self.stdout.write(self.style.WARNING(f'Total amount {"credited" if commit else "that would be credited"}: {total_credited}'))
        if not commit:
            self.stdout.write(self.style.WARNING('This was a DRY RUN. Re-run with --commit to actually apply.'))
