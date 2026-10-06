from django.contrib.auth.backends import ModelBackend
from .models import User


class PhoneBackend(ModelBackend):
    def authenticate(self, request, phone_number=None, password=None, **kwargs):
        if not phone_number or not password:
            return None
        try:
            user = User.objects.get(phone_number=phone_number)
            if user.check_password(password):
                return user
        except User.DoesNotExist:
            return None
        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None