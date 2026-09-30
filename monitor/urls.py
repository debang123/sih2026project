from django.urls import path
from . import views

urlpatterns = [
    # Pages
    path('', views.dashboard_view, name='dashboard'),
    path('economics/', views.economics_view, name='economics'),
    
    # APIs
    path('api/esp-sync/', views.handle_esp_communication, name='esp_sync'),
    path('api/latest/', views.get_latest_reading, name='get_latest'),
    path('api/history/', views.get_history_data, name='get_history'),
    path('api/economics/', views.get_financial_data, name='api_economics'),
    path('api/toggle/', views.toggle_relays, name='toggle_relays'),
    path('api/energy-data/', views.get_energy_data, name='get_energy_data'),
    path('api/simulator/', views.update_simulator, name='update_simulator'),
]