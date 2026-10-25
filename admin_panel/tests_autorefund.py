from unittest import mock
from django.test import TestCase


class AutoRefundToggleTests(TestCase):
    def test_default_is_on(self):
        from admin_panel.models import AutoRefundSettings
        self.assertTrue(AutoRefundSettings.get().is_enabled)

    def test_refund_if_new_is_held_when_off(self):
        from admin_panel.models import AutoRefundSettings
        from orders import kuntanii_refunds as kr
        s = AutoRefundSettings.get()
        s.is_enabled = False
        s.save()
        with mock.patch.object(kr, 'refund_kuntanii_order') as credit:
            self.assertEqual(kr.refund_if_new('GMX00000000'), 'held')
            credit.assert_not_called()

    def test_sweep_credits_nothing_when_off(self):
        from admin_panel.models import AutoRefundSettings
        from orders import kuntanii_refunds as kr
        s = AutoRefundSettings.get()
        s.is_enabled = False
        s.save()
        lines = []
        self.assertIsNone(kr.run_sweep(apply=True, out=lines.append))
        self.assertTrue(any('OFF' in l for l in lines))

    def test_moolre_cannot_be_active_until_configured(self):
        from admin_panel.models import PaymentGatewaySettings
        from wallet import gateway
        s = PaymentGatewaySettings.get()
        s.active_gateway = 'moolre'
        s.save()
        self.assertEqual(gateway.get_active_gateway(), 'paystack')
