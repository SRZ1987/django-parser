from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('price-comparisons/', views.price_comparisons, name='price_comparisons'),
    path('accounts/login/', LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('accounts/logout/', LogoutView.as_view(), name='logout'),
    path('accounts/register/', views.register, name='register'),
    path('accounts/confirm-email/<uidb64>/<token>/', views.confirm_email, name='confirm_email'),
    path('accounts/resend-confirmation/', views.resend_confirmation, name='resend_confirmation'),
    path('my-list/', views.shopping_list, name='shopping_list'),
    path('my-list/price-alerts/', views.update_shopping_list_price_alerts, name='update_shopping_list_price_alerts'),
    path('my-list/clear/', views.clear_shopping_list, name='clear_shopping_list'),
    path('my-list/add/<int:offer_pk>/', views.add_to_shopping_list, name='add_to_shopping_list'),
    path('my-list/quantity/<int:item_pk>/', views.update_shopping_list_item_quantity, name='update_shopping_list_item_quantity'),
    path('my-list/replace/<int:item_pk>/', views.replace_with_best_offer, name='replace_with_best_offer'),
    path('my-list/toggle/<int:item_pk>/', views.toggle_shopping_list_item, name='toggle_shopping_list_item'),
    path('my-list/remove/<int:item_pk>/', views.remove_from_shopping_list, name='remove_from_shopping_list'),
    path('group-purchases/', views.group_purchase_list, name='group_purchase_list'),
    path('group-purchases/<int:group_pk>/join/', views.join_group_purchase, name='join_group_purchase'),
    path('group-purchases/<int:group_pk>/chat/', views.group_purchase_chat, name='group_purchase_chat'),
    path('group-purchases/<int:group_pk>/messages/', views.group_purchase_messages, name='group_purchase_messages'),
    path('shared-list/<uuid:share_token>/', views.shared_shopping_list, name='shared_shopping_list'),
    path('shared-list/<uuid:share_token>/print/', views.print_shopping_list, name='print_shopping_list'),
    path('search/', views.product_search_view, name='product_search'),
    path('products/', views.product_search_view, name='products'),
    path('catalog/', views.catalog_view, name='catalog'),
    path(
        'catalog/<slug:shop_code>/category/<int:category_pk>/',
        views.catalog_view,
        name='category_catalog',
    ),
    path('product/ean/<str:barcode>/', views.barcode_product_detail, name='barcode_product_detail'),
    path('offer/<int:pk>/', views.offer_detail, name='offer_detail'),
    path('out/<int:offer_pk>/', views.store_click, name='store_click'),
    path('statistics/', views.statistics_dashboard, name='statistics_dashboard'),
    path('search/suggestions/', views.search_suggestions, name='search_suggestions'),
    path('robots.txt', views.robots_txt, name='robots_txt'),
]
