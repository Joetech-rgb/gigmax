"""
orders/notifications.py
-----------------------
All email notifications for SK Plug.
Falls back silently if email not configured.
"""
from django.core.mail import send_mail
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


def _send(to_email, subject, message):
    """Send email, log errors silently - never crash the order flow."""
    if not to_email:
        return
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@skplug.com'),
            recipient_list=[to_email],
            fail_silently=False,
        )
        logger.info(f"[notify] Email sent to {to_email} - {subject}")
    except Exception as e:
        logger.warning(f"[notify] Email failed to {to_email}: {e}")


def _get_email(order):
    if order.placed_by and order.placed_by.email:
        return order.placed_by.email
    return None


# ──────────────────────────────────────────
# ORDER NOTIFICATIONS
# ──────────────────────────────────────────

def notify_order_placed(order):
    email = _get_email(order)
    if not email:
        return
    subject = f"Order {order.order_id} Received - SK Plug"
    message = f"""Hi {order.placed_by.full_name if order.placed_by else 'Customer'},

Your order has been received and is being processed.

Order Details:
--------------
Order ID  : {order.order_id}
Network   : {order.network}
Recipient : {order.recipient}
Package   : {order.gb_package.gb_size}GB
Amount    : GHS {order.price_paid}
Status    : Processing
--------------

You will be notified once your bundle is delivered.

Thank you for using SK Plug!
"""
    _send(email, subject, message)


def notify_order_delivered(order):
    email = _get_email(order)
    if not email:
        return
    subject = f"Order {order.order_id} Delivered - SK Plug"
    message = f"""Hi {order.placed_by.full_name if order.placed_by else 'Customer'},

Great news! Your bundle has been delivered successfully.

Order Details:
--------------
Order ID  : {order.order_id}
Network   : {order.network}
Recipient : {order.recipient}
Package   : {order.gb_package.gb_size}GB
Amount    : GHS {order.price_paid}
Status    : Delivered
--------------

Thank you for choosing SK Plug!
"""
    _send(email, subject, message)


def notify_payment_confirmed(order):
    email = _get_email(order)
    if not email:
        return
    subject = f"Payment Confirmed - Order {order.order_id}"
    message = f"""Hi {order.placed_by.full_name if order.placed_by else 'Customer'},

Your payment has been confirmed and your bundle is being processed.

Order Details:
--------------
Order ID  : {order.order_id}
Network   : {order.network}
Recipient : {order.recipient}
Package   : {order.gb_package.gb_size}GB
Amount    : GHS {order.price_paid}
--------------

Thank you for using SK Plug!
"""
    _send(email, subject, message)


# ──────────────────────────────────────────
# WALLET NOTIFICATIONS
# ──────────────────────────────────────────

def notify_wallet_topup(user, amount, method='Manual'):
    """Email sent when wallet is topped up (manual or auto/Paystack)."""
    if not user.email:
        return
    subject = "Wallet Top-Up Successful - SK Plug"
    message = f"""Hi {user.full_name},

Your wallet has been topped up successfully.

Top-Up Details:
--------------
Amount    : GHS {amount:.2f}
Method    : {method}
New Balance: GHS {user.wallet_balance:.2f}
--------------

If you did not initiate this top-up, please contact admin immediately.

Thank you for using SK Plug!
"""
    _send(user.email, subject, message)


# ──────────────────────────────────────────
# GENERATED LINK PROFIT NOTIFICATIONS
# ──────────────────────────────────────────

def notify_link_profit(user, amount, link_label):
    """Email sent when a generated link earns profit."""
    if not user.email:
        return
    subject = "Generated Link Profit Received - SK Plug"
    message = f"""Hi {user.full_name},

You have received a profit from your generated link!

Profit Details:
--------------
Link      : {link_label}
Profit    : GHS {amount:.2f}
--------------

Your wallet has been credited with GHS {amount:.2f}.

Thank you for using SK Plug!
"""
    _send(user.email, subject, message)


# ──────────────────────────────────────────
# RESELLER REQUEST NOTIFICATIONS
# ──────────────────────────────────────────

def notify_reseller_approved(user):
    """Email sent when an agent request is approved."""
    if not user.email:
        return
    subject = "Your Agent Request Has Been Approved - SK Plug"
    message = f"""Hi {user.full_name},

Congratulations! Your request to become an agent on SK Plug has been approved.

You can now log in and start placing orders.

Login here: https://skplug.com/accounts/login/

Welcome to the SK Plug team!
"""
    _send(user.email, subject, message)


def notify_reseller_declined(user):
    """Email sent when an agent request is declined."""
    if not user.email:
        return
    subject = "Your Agent Request Has Been Declined - SK Plug"
    message = f"""Hi {user.full_name},

Unfortunately, your request to become an agent on SK Plug has been declined at this time.

If you believe this is a mistake or would like more information, please contact our admin via WhatsApp.

Thank you for your interest in SK Plug.
"""
    _send(user.email, subject, message)


# ──────────────────────────────────────────
# DEBT NOTIFICATIONS
# ──────────────────────────────────────────

def notify_daily_debt(user, daily_amount, total_debt):
    """Email sent at end of day summarising debt incurred."""
    if not user.email:
        return
    subject = "Your Daily Debt Summary - SK Plug"
    message = f"""Hi {user.full_name},

Here is your debt summary for today.

Debt Summary:
--------------
Today's Debt  : GHS {daily_amount:.2f}
Total Debt    : GHS {total_debt:.2f}
--------------

Please ensure you clear your debt to continue placing orders.

Contact admin if you have any questions.

SK Plug Team
"""
    _send(user.email, subject, message)


def notify_debt_deducted(user, amount, remaining_debt, wallet_balance):
    """Email sent when a top-up is used to deduct from debt."""
    if not user.email:
        return
    subject = "Debt Deducted from Top-Up - SK Plug"
    message = f"""Hi {user.full_name},

A top-up you made has been applied towards your outstanding debt.

Deduction Details:
--------------
Amount Deducted : GHS {amount:.2f}
Remaining Debt  : GHS {remaining_debt:.2f}
Wallet Balance  : GHS {wallet_balance:.2f}
--------------

Thank you for clearing your debt.

SK Plug Team
"""
    _send(user.email, subject, message)


# ──────────────────────────────────────────
# API STATUS NOTIFICATIONS
# ──────────────────────────────────────────

def notify_api_turned_on(user):
    """Email sent when API access is turned on for a user."""
    if not user.email:
        return
    subject = "API Access Activated - SK Plug"
    message = f"""Hi {user.full_name},

Your API access has been turned ON by the admin.

You can now use the SK Plug API to place orders programmatically.

Visit your API Docs page for your token and integration details.

SK Plug Team
"""
    _send(user.email, subject, message)


def notify_api_turned_off(user):
    """Email sent when API access is turned off for a user."""
    if not user.email:
        return
    subject = "API Access Deactivated - SK Plug"
    message = f"""Hi {user.full_name},

Your API access has been turned OFF by the admin.

Orders via API will no longer be accepted until access is restored.

Please contact admin if you believe this is an error.

SK Plug Team
"""
    _send(user.email, subject, message)