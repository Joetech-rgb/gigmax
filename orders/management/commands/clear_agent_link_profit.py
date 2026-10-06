from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from accounts.models import User
from wallet.models import Wallet, WalletTransaction


class Command(BaseCommand):
    help = (
        'One-off: zeroes out a specific agent\'s link_profit_balance (available '
        'balance / profit card). Does NOT touch wallet_balance, debt_balance, or '
        'total_link_profit_earned (lifetime stat left intact — this only clears '
        'what is currently available to withdraw). Dry-run by default.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--agent-id', required=True, help='User pk (UUID) of the agent')
        parser.add_argument('--reason', required=True, help='Why this is being cleared (for the audit trail)')
        parser.add_argument('--commit', action='store_true', help='Actually apply. Without this, nothing is written.')

    def handle(self, *args, **options):
        commit = options['commit']
        agent_id = options['agent_id']
        reason = options['reason']
        mode = 'COMMIT' if commit else 'DRY RUN'
        self.stdout.write(self.style.WARNING(f'=== {mode} ==='))

        try:
            agent = User.objects.get(pk=agent_id)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'No user with id {agent_id}'))
            return

        current = Decimal(str(agent.link_profit_balance or 0))
        self.stdout.write(f'Agent: {agent.full_name} | id: {agent.pk}')
        self.stdout.write(f'Current link_profit_balance: {current}')
        self.stdout.write(f'Current total_link_profit_earned: {agent.total_link_profit_earned} (will NOT change)')
        self.stdout.write(f'Reason: {reason}')

        if current <= 0:
            self.stdout.write(self.style.WARNING('Balance is already zero or negative — nothing to clear.'))
            return

        if commit:
            with transaction.atomic():
                wallet, _ = Wallet.objects.get_or_create(user=agent)
                WalletTransaction.objects.create(
                    wallet=wallet,
                    amount=current,
                    transaction_type='debit',
                    status='completed',
                    description=f'Admin manual clearance of link profit balance — {reason}',
                )
                User.objects.filter(pk=agent.pk).update(link_profit_balance=Decimal('0'))
            self.stdout.write(self.style.SUCCESS(f'COMMITTED: link_profit_balance cleared from {current} to 0.00'))
        else:
            self.stdout.write(f'Would clear: {current} -> 0.00')
            self.stdout.write(self.style.WARNING('This was a DRY RUN. Re-run with --commit to actually apply.'))
