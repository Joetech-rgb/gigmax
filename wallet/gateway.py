"""
Central payment gateway dispatcher.

Every place in the codebase that needs to talk to a payment gateway
(initialize a transaction, verify one, create a MoMo transfer recipient,
or send a payout) should call the functions here instead of hitting
Paystack or Moolre directly. This guarantees the active-gateway toggle
(admin_panel.models.PaymentGatewaySettings) is respected everywhere,
with no call site silently bypassing it.

Webhooks are NOT routed through here — each gateway keeps its own
permanent webhook endpoint regardless of which one is "active", since a
transaction started under gateway A must remain verifiable via gateway
A's webhook even after the toggle switches to gateway B mid-flight.
"""


def get_active_gateway():
    from admin_panel.models import PaymentGatewaySettings
    return PaymentGatewaySettings.get().active_gateway


def initialize_transaction(email, amount_ghc, reference, callback_url):
    gateway = get_active_gateway()
    if gateway == 'moolre':
        from wallet import moolre
        return moolre.initialize_transaction(email, amount_ghc, reference, callback_url)
    from wallet import paystack
    return paystack.initialize_transaction(email, amount_ghc, reference, callback_url)


def verify_transaction(reference):
    gateway = get_active_gateway()
    if gateway == 'moolre':
        from wallet import moolre
        return moolre.verify_transaction(reference)
    from wallet import paystack
    return paystack.verify_transaction(reference)


def create_momo_recipient(name, phone, bank_code):
    gateway = get_active_gateway()
    if gateway == 'moolre':
        from wallet import moolre
        return moolre.create_momo_recipient(name, phone, bank_code)
    from wallet import paystack
    return paystack.create_momo_recipient(name, phone, bank_code)


def initiate_transfer(recipient_code, amount_ghc, reference, reason='Withdrawal'):
    gateway = get_active_gateway()
    if gateway == 'moolre':
        from wallet import moolre
        return moolre.initiate_transfer(recipient_code, amount_ghc, reference, reason)
    from wallet import paystack
    return paystack.initiate_transfer(recipient_code, amount_ghc, reference, reason)
