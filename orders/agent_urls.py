from django.urls import path
from . import agent_views as views

urlpatterns = [
    path('dashboard/export-refunded/', views.export_refunded_csv_view, name='agent_export_refunded'),
    path('request-api/',            views.request_api_access,  name='agent_request_api'),
    path('dashboard/',              views.dashboard_view,      name='agent_dashboard'),
    path('dashboard/export-waiting-refund/', views.export_waiting_refund_csv_view, name='agent_export_waiting_refund'),
    path('dashboard/export-unverified/', views.export_unverified_csv_view, name='agent_export_unverified'),
    path('buy/single/',             views.buy_single_view,     name='agent_buy_single'),
    path('buy/single/place/',       views.place_single_view,   name='agent_place_single'),
    path('buy/bulk/',               views.buy_bulk_view,       name='agent_buy_bulk'),
    path('buy/bulk/validate/',      views.validate_bulk_view,  name='agent_validate_bulk'),
    path('buy/bulk/bundles/',       views.bundle_list_view,    name='agent_bulk_bundles'),  # ← add this
    path('buy/bulk/bundles/json/', views.bundles_json_view, name='agent_bundles_json'),
    path('buy/bulk/submit/',        views.submit_bulk_view,    name='agent_submit_bulk'),
    path('buy/excel/',              views.buy_excel_view,      name='agent_buy_excel'),
    path('buy/excel/upload/',       views.upload_excel_view,   name='agent_upload_excel'),
    path('buy/excel/submit/',       views.submit_excel_view,   name='agent_submit_excel'),
    path('orders/',                 views.my_orders_view,      name='agent_my_orders'),
    path('orders/bulk-mark-delivered/', views.bulk_mark_delivered_view, name='agent_bulk_mark_delivered'),
    path('wallet/',                 views.wallet_view,         name='agent_wallet'),
    path('links/',                  views.manage_links_view,   name='agent_links'),
    path('links/create/',           views.create_link_view,    name='agent_create_link'),
    path('links/<int:pk>/edit/',    views.edit_link_view,      name='agent_edit_link'),
    path('links/<int:pk>/toggle/',  views.toggle_link_view,    name='agent_toggle_link'),
    path('links/<int:pk>/delete/',  views.delete_link_view,    name='agent_delete_link'),
    path('api-docs/',               views.api_docs_view,       name='agent_api_docs'),
    path('api-docs/verify-password/', views.verify_password_for_token_view, name='agent_verify_token_password'),
    path('api-request/',            views.api_request_view,    name='agent_api_request'),
    path('afa/',                    views.afa_view,            name='agent_afa'),
    path('debt/',                   views.debt_view,           name='agent_debt'),
    path('debt/request/',           views.debt_request_view,   name='agent_debt_request'),
    path('complaint/',              views.complaint_view,      name='agent_complaint'),
    path('logout/',                 views.logout_view,         name='agent_logout'),
    
    path('dashboard/refund-history/', views.refund_history_view, name='agent_refund_history'),
    path('dashboard/refund-detail-json/', views.refund_detail_json_view, name='agent_refund_detail_json'),
]
