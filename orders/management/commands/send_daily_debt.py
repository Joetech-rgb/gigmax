from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Sum
from accounts.models import User
from orders.models import Order
from orders.notifications import notify_daily_debt

class Command(BaseCommand):
    help = 'Send daily debt summary emails to all agents with debt'

    def handle(self, *args, **kwargs):
        today = timezone.now().date()
        agents = User.objects.filter(
            role__in=['small_agent','moderate','mega','super_mega','extreme'],
            debt_balance__gt=0
        )
        for agent in agents:
            today_debt = Order.objects.filter(
                placed_by=agent,
                created_at__date=today,
                source='debt'
            ).aggregate(t=Sum('price_paid'))['t'] or 0
            try:
                notify_daily_debt(agent, float(today_debt), float(agent.debt_balance))
                self.stdout.write(f'Sent debt email to {agent.full_name}')
            except Exception as e:
                self.stdout.write(f'Failed for {agent.full_name}: {e}')
        self.stdout.write('Daily debt emails done.')
