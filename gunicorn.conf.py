workers = 3
threads = 2
worker_class = "gthread"
max_requests = 500
max_requests_jitter = 50
timeout = 120
errorlog = "/var/log/gunicorn/gigmax-error.log"
accesslog = "/var/log/gunicorn/gigmax-access.log"
capture_output = True
loglevel = "info"


