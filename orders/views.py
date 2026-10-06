from django.shortcuts import render

# Create your views here.

def export_users_csv(request):
    import csv
    from django.http import HttpResponse
    from accounts.models import User
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="registered_users.csv"'
    writer = csv.writer(response)
    writer.writerow(['Full Name', 'Phone', 'Email', 'Role', 'Date Joined'])
    for u in User.objects.all().order_by('-date_joined'):
        writer.writerow([u.full_name, u.phone_number, u.email or '', u.get_role_display(), u.date_joined.strftime('%d %b %Y')])
    return response
