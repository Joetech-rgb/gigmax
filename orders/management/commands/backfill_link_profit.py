from django.core.management.base import BaseCommand
from django.db.models import F
from decimal import Decimal
from orders.models import Order, GeneratedLink


class Command(BaseCommand):
    help = 'Backfill link_profit_balance for all agents from historical paid link orders'

    def handle(self, *args, **kwargs):
        from django.contrib.auth import get_user_model
        User = get_user_model()

        agents_updated = 0
        total_profit_backfilled = Decimal('0')

        for agent in User.objects.filter(role__in=['small_agent','moderate','mega','super_mega','extreme']):
            agent_links = GeneratedLink.objects.filter(agent=agent).prefetch_related('prices')

            link_orders = Order.objects.filter(
                generated_link__in=agent_links,
                payment_status='paid'
            ).select_related('gb_package', 'generated_link')

            profit = Decimal('0')
            for o in link_orders:
                link_price = o.generated_link.prices.filter(bundle=o.gb_package).first()
                if link_price:
                    agent_cost = Decimal(str(o.gb_package.get_price_for_user(agent)))
                    earned = Decimal(str(link_price.retail_price)) - agent_cost
                    if earned > 0:
                        profit += earned

            if profit > 0:
                User.objects.filter(pk=agent.pk).update(link_profit_balance=profit)
                self.stdout.write(f'  ✅ {agent.full_name} → GHS {profit:.2f}')
                agents_updated += 1
                total_profit_backfilled += profit

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. {agents_updated} agents updated. '
            f'Total backfilled: GHS {total_profit_backfilled:.2f}'
        ))