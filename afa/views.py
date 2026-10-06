import uuid
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.utils import timezone
from .models import AFAApplication, AFAPrice
from .forms import AFAApplicationForm
from wallet.gateway import initialize_transaction, verify_transaction


def _get_afa_price(user):
    role = 'agent_base' if (user.is_authenticated and user.role != 'customer') else 'customer_base'
    try:
        return Decimal(str(AFAPrice.objects.get(role=role).price))
    except:
        return Decimal('50.00')


def afa_apply_view(request):
    form       = AFAApplicationForm(request.POST or None)
    amount_ghs = _get_afa_price(request.user)
    today      = timezone.now().date().isoformat()

    if request.method == 'POST':
        method = request.POST.get('payment_method')

        # Only block wallet for guests
        if method == 'wallet' and not request.user.is_authenticated:
            messages.error(request, 'You must be logged in to pay with wallet.')
            return redirect('afa_apply')

        if form.is_valid():

            if method == 'paystack':
                ref = f"AFA-{uuid.uuid4().hex[:8].upper()}"

                if request.user.is_authenticated:
                    email = request.user.email or f'{request.user.phone_number}@skplug.com'
                else:
                    email = request.POST.get('email', '') or 'guest@skplug.com'

                # ✅ Save application BEFORE redirecting — so we can find it on callback
                app                    = form.save(commit=False)
                app.payment_reference  = ref
                app.status             = 'pending'
                if request.user.is_authenticated:
                    app.agent = request.user
                app.save()

                res = initialize_transaction(
                    email,
                    float(amount_ghs),
                    ref,
                    request.build_absolute_uri('/afa/pay/'),
                )

                if res and res.get('status'):
                    return redirect(res['data']['authorization_url'])
                else:
                    # Paystack failed — remove the pending application
                    app.delete()
                    messages.error(request, f"Paystack Error: {res.get('message', 'Check connection')}")

            elif method == 'wallet':
                User       = get_user_model()
                fresh_user = User.objects.get(pk=request.user.pk)
                balance    = Decimal(str(fresh_user.wallet_balance))
                amount_ghs = _get_afa_price(fresh_user)

                if balance >= amount_ghs:
                    app       = form.save(commit=False)
                    app.agent  = fresh_user
                    app.status = 'submitted'
                    app.save()
                    fresh_user.wallet_balance = balance - amount_ghs
                    fresh_user.save(update_fields=['wallet_balance'])
                    messages.success(request, 'Application submitted successfully!')
                    return redirect('agent_afa')
                else:
                    messages.error(
                        request,
                        f'Insufficient Balance. Your balance: GHS {balance}, Required: GHS {amount_ghs}'
                    )
            else:
                messages.error(request, 'Please select a payment method.')
        else:
            messages.error(request, f'Please fill all required fields. Errors: {form.errors}')

    return render(request, 'customer/afa_apply.html', {
        'form'     : form,
        'afa_price': amount_ghs,
        'today'    : today,
    })


def afa_success_view(request, pk):
    application = get_object_or_404(AFAApplication, pk=pk)
    orders      = AFAApplication.objects.filter(agent=request.user).order_by('-created_at')
    return render(request, 'customer/afa_success.html', {
        'application': application,
        'orders'     : orders,
    })


def afa_pay_view(request):
    """Paystack callback after MoMo/card payment."""
    ref = request.GET.get('reference') or request.GET.get('trxref')

    if not ref:
        messages.error(request, 'No payment reference found.')
        return redirect('home')

    # ✅ Verify with Paystack first
    verification = verify_transaction(ref)

    if not (
        verification
        and verification.get('data', {}).get('status') == 'success'
    ):
        messages.error(request, 'Payment could not be verified. Please contact support.')
        return redirect('home')

    # ✅ Find the application saved before the redirect
    try:
        app = AFAApplication.objects.get(payment_reference=ref)
    except AFAApplication.DoesNotExist:
        messages.error(request, 'Application not found. Please contact support.')
        return redirect('home')

    # ✅ Only update if still pending (avoid double-processing)
    if app.status == 'pending':
        app.status = 'submitted'
        app.save(update_fields=['status'])

    if request.user.is_authenticated:
        messages.success(request, 'Payment confirmed! Your AFA application has been submitted.')
        return redirect('agent_afa')
    else:
        messages.success(request, 'Your AFA application has been submitted successfully! Our team will process it shortly.')
        return redirect('home')


def afa_copy_record(request, pk):
    if request.method == 'POST':
        app        = get_object_or_404(AFAApplication, pk=pk)
        app.status = 'processing'
        app.save(update_fields=['status'])
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'Invalid'}, status=400)


def afa_mark_delivered(request, pk):
    if request.method == 'POST':
        app        = get_object_or_404(AFAApplication, pk=pk)
        app.status = 'delivered'
        app.save(update_fields=['status'])
        return JsonResponse({'ok': True})
    return JsonResponse({'error': 'Invalid'}, status=400)