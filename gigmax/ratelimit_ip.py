def client_ip(request):
    """Trust X-Forwarded-For only when the direct peer is a local reverse proxy."""
    remote = request.META.get('REMOTE_ADDR', '')
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff and remote in ('127.0.0.1', '::1'):
        return xff.split(',')[-1].strip()
    return remote