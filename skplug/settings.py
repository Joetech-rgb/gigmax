import os
from pathlib import Path
from decouple import config
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY', default='django-insecure-gigmax-change-in-production')

DEBUG = config('DEBUG', default=False, cast=bool)

_allowed = config('ALLOWED_HOSTS', default='*')
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(',')]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders',
    'accounts',
    'orders',
    'bundles',
    'wallet',
    'api_access',
    'afa',
    'admin_panel',
    'customer',
    'agents',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'skplug.middleware.SiteClosedMiddleware',
]

ROOT_URLCONF = 'skplug.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'skplug.wsgi.application'

APPEND_SLASH = False

DATABASES = {
    'default': dj_database_url.config(
        default=f"postgresql://{config('DB_USER', default='gigmax_user')}:{config('DB_PASSWORD', default='')}@{config('DB_HOST', default='localhost')}:{config('DB_PORT', default='5432')}/{config('DB_NAME', default='gigmax_db')}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'gigmax-cache',
    }
}

AUTH_USER_MODEL = 'accounts.User'

AUTHENTICATION_BACKENDS = [
    'accounts.backends.PhoneBackend',
]

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Accra'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': [],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
}

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = False

PAYSTACK_SECRET_KEY = config('PAYSTACK_SECRET_KEY', default='')
PAYSTACK_PUBLIC_KEY = config('PAYSTACK_PUBLIC_KEY', default='')

GATI_API_URL = config('GATI_API_URL', default='')
GATI_API_KEY = config('GATI_API_KEY', default='')

KUNTANII_API_URL = config('KUNTANII_API_URL', default='https://kuntanii.com')
KUNTANII_API_KEY = config('KUNTANII_API_KEY', default='')
KUNTANII_WEBHOOK_SECRET = config('KUNTANII_WEBHOOK_SECRET', default='')
GATI_ACCOUNT_ID = config('GATI_ACCOUNT_ID', default='')

ADMIN_WHATSAPP_NUMBER = config('ADMIN_WHATSAPP_NUMBER', default='')
WHATSAPP_COMMUNITY_LINK = config('WHATSAPP_COMMUNITY_LINK', default='')

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

_csrf_origins = config('CSRF_TRUSTED_ORIGINS', default='http://localhost:8000,http://127.0.0.1:8000')
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(',')]

MESSAGE_STORAGE = 'django.contrib.messages.storage.fallback.FallbackStorage'

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='noreply@gigmax.com')

SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_AGE = 86400
SESSION_SAVE_EVERY_REQUEST = False

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    CSRF_TRUSTED_ORIGINS += [
        'https://gigmax.com',
        'https://www.gigmax.com',
    ]

GATI_SYNC_SECRET = config('GATI_SYNC_SECRET', default='')
AT_SYNC_SECRET = config('AT_SYNC_SECRET', default='')
AT_API_KEY  = config('AT_API_KEY', default='')

# DCS Elite (AT bundle reseller) integration
DCS_API_KEY         = config('DCS_API_KEY', default='')
DCS_BASE_URL        = config('DCS_BASE_URL', default='https://www.dcselite.com/api/v1')
DCS_WEBHOOK_SECRET  = config('DCS_WEBHOOK_SECRET', default='')
AT_USERNAME = config('AT_USERNAME', default='')
AT_SENDER_ID = config('AT_SENDER_ID', default='GigMax')
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'file': {
            'level': 'ERROR',
            'class': 'logging.FileHandler',
            'filename': ('/var/log/gunicorn/gigmax-django-errors.log' if os.name != 'nt' else str(BASE_DIR / 'logs' / 'django-errors.log')),
        },
        'console': {
            'level': 'ERROR',
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'django.request': {
            'handlers': ['file', 'console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}

RATELIMIT_IP_META_KEY = 'HTTP_X_FORWARDED_FOR'
