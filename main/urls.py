from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('search/suggestions/', views.search_suggestions, name='search_suggestions'),
]
