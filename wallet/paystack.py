import requests
from decimal import Decimal
from django.conf import settings

HEADERS = {
    'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}',
    'Content-Type' : 'application/json',
}

NETWORK_CODES = {
    'MTN'       : 'MTN',
    'TELECEL'   : 'VOD',
    'AIRTELTIGO': 'ATL',
    'AT'        : 'ATL',
}

# Timeout for all Paystack API calls (seconds)
API_TIMEOUT = 20


def initialize_transaction(email, amount_ghc, reference, callback_url):
    """Amount in Ghana Cedis — converted to pesewas for Paystack."""
    response = requests.post(
        'https://api.paystack.co/transaction/initialize',
        headers=HEADERS,
        timeout=API_TIMEOUT,
        json={
            'email'       : email,
            'amount'      : int(float(amount_ghc) * 100),
            'reference'   : reference,
            'callback_url': callback_url,
            'currency'    : 'GHS',
        }
    )
    return response.json()


def verify_transaction(reference):
    response = requests.get(
        f'https://api.paystack.co/transaction/verify/{reference}',
        headers=HEADERS,
        timeout=API_TIMEOUT,
    )
    return response.json()


def resolve_momo_name(phone, bank_code):
    """Resolve the registered MoMo account name directly from the network.
    Returns the account name string, or None if unresolvable.
    This prevents MTN from rejecting transfers due to name mismatch.
    """
    code = NETWORK_CODES.get(bank_code.upper(), 'MTN')
    try:
        res  = requests.get(
            f'https://api.paystack.co/bank/resolve?account_number={phone}&bank_code={code}',
            headers=HEADERS,
            timeout=API_TIMEOUT,
        )
        data = res.json()
        if data.get('status') and data.get('data', {}).get('account_name'):
            return data['data']['account_name']
    except Exception:
        pass
    return None


def create_momo_recipient(name, phone, bank_code):
    """Register a MoMo number as a Paystack transfer recipient.
    Always resolves the real MoMo account name first — using the name
    from the DB causes MTN to reject transfers when it doesn't exactly
    match the registered MoMo account name.
    Returns the recipient_code string, or raises on failure.
    """
    code          = NETWORK_CODES.get(bank_code.upper(), 'MTN')
    resolved      = resolve_momo_name(phone, bank_code)
    verified_name = resolved or name
    if resolved:
        print(f'[create_momo_recipient] Resolved name for {phone}: {resolved}')
    else:
        print(f'[create_momo_recipient] Could not resolve name for {phone}, using DB name: {name}')
    res  = requests.post(
        'https://api.paystack.co/transferrecipient',
        headers=HEADERS,
        timeout=API_TIMEOUT,
        json={
            'type'          : 'mobile_money',
            'name'          : verified_name,
            'account_number': phone,
            'bank_code'     : code,
            'currency'      : 'GHS',
        }
    )
    data = res.json()
    if data.get('status'):
        return data['data']['recipient_code']
    raise Exception(data.get('message', 'Recipient creation failed'))


def initiate_transfer(recipient_code, amount_ghc, reference, reason='Withdrawal'):
    """Send money to a MoMo recipient. Returns transfer data or raises on failure."""
    res = requests.post(
        'https://api.paystack.co/transfer',
        headers=HEADERS,
        timeout=API_TIMEOUT,
        json={
            'source'   : 'balance',
            'amount'   : int(Decimal(str(amount_ghc)) * 100),
            'recipient': recipient_code,
            'reference': reference,
            'reason'   : reason,
            'currency' : 'GHS',
        }
    )
    data = res.json()
    if data.get('status'):
        return data['data']
    raise Exception(data.get('message', 'Transfer failed'))