from django import forms

class WithdrawalForm(forms.Form):
    amount = forms.DecimalField(max_digits=10, decimal_places=2, min_value=10)
    mobile_number = forms.CharField(max_length=10, min_length=10)
    network = forms.ChoiceField(choices=[
        ('MTN',     'MTN'),
        ('TELECEL', 'Telecel'),
        ('AT',      'AirtelTigo'),
    ])

    def clean_mobile_number(self):
        number = self.cleaned_data.get('mobile_number', '').strip()
        if not number.isdigit():
            raise forms.ValidationError('Phone number must contain digits only.')
        if not number.startswith('0'):
            raise forms.ValidationError('Phone number must start with 0.')
        if len(number) != 10:
            raise forms.ValidationError(f'Phone number must be exactly 10 digits (you entered {len(number)}).')
        return number