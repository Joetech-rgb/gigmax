from decimal import Decimal
from django.core.management.base import BaseCommand
from orders.models import Order
from orders.utils import calculate_owed_amount, _apply_refund_credit
from wallet.models import WalletTransaction


class Command(BaseCommand):
    help = (
        'Backfill: credits an agent for their refunded orders that were '
        'never actually credited (refund_credited=False and/or missing '
        'AUTOREFUND transaction), regardless of when they occurred. '
        'Dry-run by default — pass --commit to actually write.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--agent-id', required=True, help='User pk (UUID) of the agent')
        parser.add_argument('--commit', action='store_true', help='Actually apply. Without this, nothing is written.')

    def handle(self, *args, **options):
        commit = options['commit']
        agent_id = options['agent_id']
        mode = 'COMMIT' if commit else 'DRY RUN'
        self.stdout.write(self.style.WARNING(f'=== {mode} ==='))

        from accounts.models import User
        try:
            agent = User.objects.get(pk=agent_id)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'No user with id {agent_id}'))
            return

        orders = Order.objects.filter(placed_by=agent, status='refunded').order_by('created_at')

        total_credited = Decimal('0')

        for o in orders:
            txn_exists = WalletTransaction.objects.filter(reference=f'AUTOREFUND-{o.order_id}').exists()
            if o.refund_credited and txn_exists:
                continue  # already properly credited, skip

            amount = calculate_owed_amount(o)
            before_wallet = Decimal(str(agent.wallet_balance or 0))
            before_debt = Decimal(str(agent.debt_balance or 0))

            self.stdout.write(
                f'{o.order_id} | amount={amount} | before: wallet={before_wallet} debt={before_debt} '
                f'| refund_credited={o.refund_credited} | AUTOREFUND_txn={txn_exists}'
            )

            if commit:
                new_wallet, new_debt = _apply_refund_credit(agent, amount, o.order_id, o.source)
                Order.objects.filter(pk=o.pk).update(refund_credited=True)
                agent.refresh_from_db()
                self.stdout.write(self.style.SUCCESS(f'  -> COMMITTED: new wallet={new_wallet} debt={new_debt}'))
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
                # simulate the running balance for next iteration in dry-run display
                agent.wallet_balance = sim_wallet
                agent.debt_balance = sim_debt

            total_credited += amount

        self.stdout.write('')
        self.stdout.write(self.style.WARNING(f'Total {"credited" if commit else "that would be credited"}: {total_credited}'))
        if not commit:
            self.stdout.write(self.style.WARNING('DRY RUN — re-run with --commit to apply.'))
