"""Mounted under /api/<version>/ by config/urls.py.

    google/              POST  a Google ID token -> our JWT pair
    profile/             GET   people search (?search=, min 2 chars)
    profile/me/          GET | PUT | PATCH  the signed-in profile
    profile/<username>/  GET   a public profile
"""

from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

urlpatterns = [
    path('google/', views.GoogleAuthAPIView.as_view(), name='google-auth'),
]

router = DefaultRouter()
router.register('profile', views.ProfileViewSet, basename='profile')
urlpatterns += router.urls
