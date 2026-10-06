from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0002_paystackwebhooklog'),
    ]

    operations = [
        migrations.AddField(
            model_name='wallettransaction',
            name='debt_before',
            field=models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='debt_after',
            field=models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='wallet_before',
            field=models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='wallet_after',
            field=models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='order_source',
            field=models.CharField(max_length=30, blank=True, default=''),
        ),
    ]
