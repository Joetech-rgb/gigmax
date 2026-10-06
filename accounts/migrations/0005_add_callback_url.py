from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_user_afa_access_user_note'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='callback_url',
            field=models.URLField(blank=True, null=True, help_text='Webhook URL for order status updates'),
        ),
    ]
