from django import template
from decimal import Decimal

register = template.Library()

@register.filter
def get_price(package, user):
    return package.get_price_for_user(user)

@register.filter
def format_gb(value):
    """Display 10 instead of 10.00, but 1.5 stays 1.5"""
    try:
        d = Decimal(str(value))
        if d == d.to_integral_value():
            return str(int(d))
        return str(d.normalize())
    except:
        return value