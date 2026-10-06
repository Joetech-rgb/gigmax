from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/',          views.login_view,          name='login'),
    path('logout/',         views.logout_view,          name='logout'),
    path('signup/',         views.agent_signup_view,    name='agent_signup'),
    path('signup/success/', views.signup_success_view,  name='signup_success'),
    path('password-reset/', views.password_reset_view,  name='password_reset'),
]