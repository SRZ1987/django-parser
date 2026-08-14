"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.contrib.sitemaps.views import index, sitemap
from django.views.decorators.cache import cache_page
from django.urls import include, path

from main.sitemaps import sitemaps

urlpatterns = [
    path('', include('main.urls')),
    path(
        'sitemap.xml',
        cache_page(21600)(index),
        {'sitemaps': sitemaps},
        name='sitemap-index',
    ),
    path(
        'sitemap-<section>.xml',
        cache_page(21600)(sitemap),
        {'sitemaps': sitemaps},
        name='django.contrib.sitemaps.views.sitemap',
    ),
    path('i18n/', include('django.conf.urls.i18n')),
    path('admin/', admin.site.urls),
]
