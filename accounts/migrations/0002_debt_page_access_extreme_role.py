from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]
    operations = [
        migrations.AddField(model_name="user", name="debt_page_access", field=models.BooleanField(default=False)),
        migrations.AlterField(model_name="user", name="role", field=models.CharField(choices=[("customer","Customer"),("small_agent","Small Agent"),("moderate","Moderate Agent"),("mega","Mega Agent"),("super_mega","Super-Mega Agent"),("extreme","Extreme Agent"),("admin","Admin")], default="customer", max_length=20)),
    ]
