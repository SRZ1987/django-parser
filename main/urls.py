from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('search/', views.product_search_view, name='product_search'),
    path('products/', views.product_search_view, name='products'),
    path('catalog/', views.catalog_view, name='catalog'),
    path('offer/<int:pk>/', views.offer_detail, name='offer_detail'),
    path('search/suggestions/', views.search_suggestions, name='search_suggestions'),
]
