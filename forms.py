class AfaApplyForm(forms.Form):
    ghana_card = forms.CharField(...)
    # ... other fields ...

    def clean_ghana_card(self):
        value = self.cleaned_data.get('ghana_card', '').strip().upper()
        pattern = r'^GHA-\d{9}-\d$'  # Must match afa/models.py validator
        if not re.match(pattern, value):
            raise ValidationError('Ghana Card must be in format GHA-XXXXXXX-X')
        return value