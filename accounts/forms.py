from django import forms
from .models import User, AgentSignupRequest


class LoginForm(forms.Form):
    phone_number = forms.CharField(max_length=20, label='Phone Number')
    password     = forms.CharField(widget=forms.PasswordInput, label='Password')


class AgentSignupForm(forms.Form):
    full_name        = forms.CharField(max_length=200)
    phone_number     = forms.CharField(max_length=20)
    email            = forms.EmailField(required=False)
    password         = forms.CharField(widget=forms.PasswordInput, min_length=6)
    confirm_password = forms.CharField(widget=forms.PasswordInput, label='Confirm Password')

    def clean_phone_number(self):
        phone = self.cleaned_data['phone_number']
        if User.objects.filter(phone_number=phone).exists():
            raise forms.ValidationError('An account with this number already exists.')
        if AgentSignupRequest.objects.filter(phone_number=phone, status='pending').exists():
            raise forms.ValidationError('A request for this number is already pending.')
        return phone

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get('password')
        p2 = cleaned.get('confirm_password')
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError('Passwords do not match.')
        return cleaned


class PasswordResetRequestForm(forms.Form):
    phone_number = forms.CharField(max_length=20, label='Phone Number')