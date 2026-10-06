from django import forms
from .models import AFAApplication

GHANA_REGIONS = [
    ('', '-- Select Region --'),
    ('Ahafo', 'Ahafo'),
    ('Ashanti', 'Ashanti'),
    ('Bono', 'Bono'),
    ('Bono East', 'Bono East'),
    ('Central', 'Central'),
    ('Eastern', 'Eastern'),
    ('Greater Accra', 'Greater Accra'),
    ('North East', 'North East'),
    ('Northern', 'Northern'),
    ('Oti', 'Oti'),
    ('Savannah', 'Savannah'),
    ('Upper East', 'Upper East'),
    ('Upper West', 'Upper West'),
    ('Volta', 'Volta'),
    ('Western', 'Western'),
    ('Western North', 'Western North'),
]

class AFAApplicationForm(forms.ModelForm):
    region = forms.ChoiceField(choices=GHANA_REGIONS)

    class Meta:
        model = AFAApplication
        fields = ['full_name', 'phone_number', 'ghana_card', 'date_of_birth', 'location', 'region']
        widgets = {
            'date_of_birth': forms.DateInput(attrs={'type': 'date'}),
        }
