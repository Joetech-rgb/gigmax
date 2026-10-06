from django.shortcuts import render

# Create your views here.
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.conf import settings
from .models import User, AgentSignupRequest
from .forms import LoginForm, AgentSignupForm, PasswordResetRequestForm

def login_view(request):
    if request.user.is_authenticated:
        return _redirect_by_role(request.user)

    form = LoginForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        phone = form.cleaned_data['phone_number']
        password = form.cleaned_data['password']
        user = authenticate(request, phone_number=phone, password=password)

        if user:
            # 1. Check for Approval Status
            if user.is_agent and not user.is_approved:
                messages.error(request, 'Your account is awaiting Admin approval.')
                return render(request, 'accounts/login.html', {'form': form})

            # 2. Check for Paused Status
            if user.is_paused:
                messages.error(request, 'Your account has been paused. Contact Admin.')
                return render(request, 'accounts/login.html', {'form': form})

            # 3. Log the user in FIRST
            # This creates the session and cycles the ID for security
            login(request, user)

            # 4. Handle "Remember Me" logic SECOND
            # This ensures set_expiry isn't overwritten by the login() signal
            remember = request.POST.get('remember_me')
            if not remember:
                # 0 means the session cookie expires when the browser is closed
                request.session.set_expiry(0)
            else:
                # Set to a specific duration (e.g., 2 weeks) or leave as default
                # 1209600 seconds = 2 weeks
                request.session.set_expiry(1209600)

            return _redirect_by_role(user)
            
        else:
            messages.error(request, 'Invalid phone number or password.')

    return render(request, 'accounts/login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('/')


def agent_signup_view(request):
    """
    Only Small Agents sign up here.
    Mega and above are upgraded manually by Admin.
    """
    form = AgentSignupForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        AgentSignupRequest.objects.create(
            full_name    = form.cleaned_data['full_name'],
            phone_number = form.cleaned_data['phone_number'],
            email        = form.cleaned_data.get('email', ''),
            password_raw = form.cleaned_data['password'],
        )
        return redirect('accounts:signup_success')

    return render(request, 'accounts/signup.html', {'form': form})


def signup_success_view(request):
    """
    After signup:
    - Show 'You have successfully requested to be an Agent'
    - Show 'Wait for Approval — Contact Admin' with WhatsApp link
    - Show 'Join Community' button
    """
    return render(request, 'accounts/signup_success.html', {
        'admin_whatsapp'  : settings.ADMIN_WHATSAPP_NUMBER,
        'community_link'  : settings.WHATSAPP_COMMUNITY_LINK,
    })


def password_reset_view(request):
    """All agents have access to Forget Password"""
    form = PasswordResetRequestForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        phone = form.cleaned_data['phone_number']
        try:
            user = User.objects.get(phone_number=phone)
            messages.success(
                request,
                'Contact Admin via WhatsApp to reset your password.'
            )
        except User.DoesNotExist:
            messages.error(request, 'No account found with that phone number.')

    return render(request, 'accounts/password_reset.html', {
        'form'           : form,
        'admin_whatsapp' : settings.ADMIN_WHATSAPP_NUMBER,
    })


# ── Helpers ──────────────────────────────────────────────────────────────────

def _redirect_by_role(user):
    if user.role == User.Role.ADMIN:
        return redirect('/admin-panel/dashboard/')
    elif user.is_agent:
        return redirect('/agent/dashboard/')
    else:
        return redirect('/')