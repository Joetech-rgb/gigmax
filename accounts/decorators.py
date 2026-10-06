from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages

def agent_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, 'Please login first.')
            return redirect('/accounts/login/')
        
        if not hasattr(request.user, 'agent_profile'):
            messages.error(request, 'Agent access required.')
            return redirect('/accounts/login/')
        
        return view_func(request, *args, **kwargs)
    return wrapper

def top_tier_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, 'Please login first.')
            return redirect('/accounts/login/')
        
        if not hasattr(request.user, 'agent_profile'):
            messages.error(request, 'Agent access required.')
            return redirect('/accounts/login/')
        
        top_tiers = ['mega', 'super_mega', 'extreme']
        if request.user.agent_profile.agent_tier not in top_tiers:
            messages.error(request, 'This feature requires Mega Agent or higher tier.')
            return redirect('/agent/dashboard/')
        
        return view_func(request, *args, **kwargs)
    return wrapper

def afa_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, 'Please login first.')
            return redirect('/accounts/login/')
        
        if hasattr(request.user, 'agent_profile'):
            if not getattr(request.user.agent_profile, 'has_afa_access', False):
                messages.error(request, 'AFA Bundle access required. Please contact admin to upgrade your account.')
                return redirect('/agent/dashboard/')
        else:
            messages.error(request, 'Agent account required.')
            return redirect('/agent/dashboard/')
        
        return view_func(request, *args, **kwargs)
    return wrapper

def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, 'Please login first.')
            return redirect('/accounts/login/')
        
        if not request.user.is_staff:
            messages.error(request, 'Admin access required.')
            return redirect('/agent/dashboard/')
        
        return view_func(request, *args, **kwargs)
    return wrapper
