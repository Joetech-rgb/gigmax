CONFIGURED = False
MSG = 'Moolre is not configured yet. Ask an admin to switch the active gateway back to Paystack.'

def initialize_transaction(email, amount_ghc, reference, callback_url, metadata=None):
    return {'status': False, 'message': MSG}

def verify_transaction(reference):
    return {'status': False, 'message': MSG,
            'data': {'status': 'unavailable', 'gateway_response': MSG}}

def create_momo_recipient(name, phone, bank_code):
    raise Exception(MSG)

def initiate_transfer(recipient_code, amount_ghc, reference, reason='Withdrawal'):
    raise Exception(MSG)
