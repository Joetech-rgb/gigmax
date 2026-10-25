from django.shortcuts import render


class SiteClosedMiddleware:
    """
    Blocks all non-admin users when the site is closed.
    Admin panel URLs are always open.
    The closed page itself is always open (to avoid redirect loops).
    """

    ALWAYS_OPEN = [
        '/admin-panel/',
        '/accounts/login/',
        '/accounts/logout/',
        '/site-closed/',
        '/static/',
        '/media/',
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Always allow admin panel and auth URLs
        path = request.path
        if any(path.startswith(p) for p in self.ALWAYS_OPEN):
            return self.get_response(request)

        # Check if site is closed
        try:
            from admin_panel.models import SiteSettings
            site = SiteSettings.get()
            if not site.is_open:
                # Only admin users can pass through
                if request.user.is_authenticated and getattr(request.user, 'role', '') == 'admin':
                    return self.get_response(request)
                # Everyone else sees the closed page
                return render(request, 'site_closed.html', status=503)
        except Exception:
            pass

        return self.get_response(request)