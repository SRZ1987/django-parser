from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('catalog/', views.catalog_view, name='catalog'),
    path('offer/<int:pk>/', views.offer_detail, name='offer_detail'),
    path('search/suggestions/', views.search_suggestions, name='search_suggestions'),
]
