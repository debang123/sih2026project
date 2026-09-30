import json
import random
from datetime import timedelta
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Avg
from django.db.models.functions import TruncHour, TruncDay
import math
import math
from django.utils import timezone
from .models import SystemReading, RelayState, EnergyData
from .optimizer import run_ai_optimizer, generate_schedule, detect_anomalies

# --- IN-MEMORY FALLBACK FOR VERCEL / READ-ONLY DB ---
IN_MEMORY_RELAY_STATE = {
    'home': True,
    'sell': False,
    'import': False
}

IN_MEMORY_SIMULATION_STATE = {
    'temperature': -5,
    'cloud_cover': 20,
    'solar_availability': 100,
    'wind_speed': 10,
    'snow': 'None',
    'scenario': 'Normal',
    'fuel_level': 100.0  # 100 Liters tank
}

def get_current_relay_state():
    try:
        relay_state, _ = RelayState.objects.get_or_create(pk=1)
        return {
            'home': relay_state.home_load_active,
            'sell': relay_state.grid_sell_active,
            'import': relay_state.grid_import_active,
            'obj': relay_state
        }
    except Exception:
        return {
            'home': IN_MEMORY_RELAY_STATE['home'],
            'sell': IN_MEMORY_RELAY_STATE['sell'],
            'import': IN_MEMORY_RELAY_STATE['import'],
            'obj': None
        }

# --- PAGES ---
def dashboard_view(request):
    return render(request, 'monitor/dashboard.html')

def economics_view(request):
    return render(request, 'monitor/economics.html')

# --- DUMMY DATA GENERATORS (FALLBACK WHEN REAL DATA NOT AVAILABLE) ---
def generate_dummy_reading(now):
    hour = now.hour
    sim = IN_MEMORY_SIMULATION_STATE
    
    # 1. Base solar logic
    if 6 <= hour <= 18:
        peak_factor = 1.0 - abs(hour - 12.5) / 6.5
        solar_w = max(50.0, round(peak_factor * 5000.0 + random.uniform(-50, 50), 1))
    else:
        solar_w = 0.0

    # Apply Simulation modifiers to solar
    solar_w = solar_w * (float(sim['solar_availability']) / 100.0)
    if sim['scenario'] == 'Solar failure':
        solar_w = 0.0
    
    # Cloud and snow impacts
    cloud_penalty = float(sim['cloud_cover']) / 100.0 * 0.7  # Up to 70% reduction
    solar_w = solar_w * (1.0 - cloud_penalty)
    if sim['snow'] == 'Heavy': solar_w *= 0.5
    if sim['snow'] == 'Blizzard': solar_w *= 0.1
    
    # 2. Base Load Logic (Divided into 3 Categories)
    
    # CRITICAL: Medical, Comm, Heating, Emergency lighting
    critical_load_w = 150.0 + random.uniform(-10, 10)
    
    # IMPORTANT: Normal lighting, Refrigeration, Water systems
    important_load_w = 200.0 + random.uniform(-20, 20)
    if 7 <= hour <= 9 or 18 <= hour <= 22:
        important_load_w += 400.0  # Evening/Morning water and cooking
        
    # NON-CRITICAL: Workshop, EV charging, Optional
    non_critical_load_w = 50.0
    if 18 <= hour <= 22:
        non_critical_load_w += 1500.0 # EV charging

    # Apply Simulation modifiers to load
    temp = float(sim['temperature'])
    if temp < 10:
        # Heating load increases as temp drops (HEATING IS CRITICAL)
        heating_load = (10 - temp) * 150 # e.g. -40C means 50 * 150 = 7500W
        critical_load_w += heating_load
        
    if sim['scenario'] == 'High load':
        # Massive spike in non-critical (e.g. workshop tools + dual EV charging)
        non_critical_load_w += 3000

    # --- BATTERY TEMPERATURE MODEL (LiFePO4 Specs) ---
    battery_temp = float(sim['temperature']) + 5.0
    if battery_temp >= 25: capacity_multiplier = 1.0
    elif battery_temp >= 0: capacity_multiplier = 0.8 + (battery_temp / 25.0) * 0.2
    elif battery_temp >= -20: capacity_multiplier = 0.5 + ((battery_temp + 20) / 20.0) * 0.3
    else: capacity_multiplier = max(0.1, 0.5 + ((battery_temp + 20) / 20.0) * 0.4)
    
    # Base True SOC calculation
    if 6 <= hour <= 17:
        true_soc = min(98.0, round(45.0 + (hour - 6) * 4.8 + random.uniform(-1, 1), 1))
    else:
        true_soc = max(25.0, round(85.0 - (hour - 18 if hour >= 18 else hour + 6) * 4.5 + random.uniform(-1, 1), 1))
        
    usable_soc = true_soc * capacity_multiplier
    charge_allowed = battery_temp > 0
    discharge_allowed = battery_temp > -20

    # Prepare current state for AI Optimizer
    current_state = {
        'load_power': critical_load_w + important_load_w + non_critical_load_w,
        'solar_power': solar_w,
        'hour': hour,
        'dayofweek': now.weekday(),
        'month': now.month,
        'outside_temperature': float(sim['temperature']),
        'battery_soc': true_soc,
        'usable_soc': usable_soc,
        'sunlight_availability': max(0.0, math.sin((hour - 6) / 12.0 * math.pi)) if 6 <= hour <= 18 else 0.0,
        'cloud_cover': float(sim['cloud_cover']) / 100.0,
        'fuel_level': float(sim['fuel_level']),
        'generator_available': (sim['scenario'] != 'Generator failure') and (sim['fuel_level'] > 0),
        'battery_temp': battery_temp,
        'snow': sim['snow']
    }

    # RUN AI OPTIMIZER
    forecast, ai_decisions = run_ai_optimizer(current_state)
    ai_schedule, ai_6h_summary = generate_schedule(current_state)
    anomalies = detect_anomalies(current_state, forecast)

    # Apply AI Load Shedding Decisions
    if ai_decisions['shed_non_critical']:
        non_critical_load_w = 0.0
    if ai_decisions['shed_important']:
        important_load_w = 0.0

    load_w = critical_load_w + important_load_w + non_critical_load_w
    load_w = round(load_w, 1)

    # Apply AI Energy Routing Decisions
    generator_w = 0.0
    bat_out_w = 0.0
    
    if ai_decisions['start_generator']:
        generator_w = load_w
        if ai_decisions['charge_battery']:
            generator_w += 1000.0 # extra 1kW for charging
            
    if ai_decisions['discharge_battery']:
        bat_out_w = max(0.0, load_w - solar_w)
        
    if not ai_decisions['use_solar']:
        solar_w = 0.0

    # Fallbacks for physical realities
    if not discharge_allowed: bat_out_w = 0.0
    if not charge_allowed and (solar_w > load_w): solar_w = load_w # Curtail solar if can't charge
    
    # 5kW Generator Fuel Model: 0.5 L/hr idle/base + up to 1.5 L/hr on load
    consumption_lph = 0.0
    if generator_w > 0:
        consumption_lph = 0.5 + (min(generator_w, 5000.0) / 5000.0) * 1.5
        
    # Subtract fuel (API is called approx every 2 seconds by dashboard)
    # 2 seconds = 2 / 3600 hours
    time_delta_hr = 2.0 / 3600.0
    fuel_used = consumption_lph * time_delta_hr
    sim['fuel_level'] = max(0.0, sim['fuel_level'] - fuel_used)
    
    # Refuel if requested via scenario reset (hack for sim reset)
    if sim['scenario'] == 'Normal' and sim['fuel_level'] < 10:
        sim['fuel_level'] = 100.0

    # Calculate runtime
    estimated_runtime_hr = 0.0
    if consumption_lph > 0:
        estimated_runtime_hr = sim['fuel_level'] / consumption_lph

    return {
        'solar': solar_w,
        'grid': generator_w, # We map generator to grid for legacy charts
        'gen': generator_w,
        'fuel_pct': round(sim['fuel_level'], 1),
        'fuel_rate': round(consumption_lph, 2),
        'fuel_time': round(estimated_runtime_hr, 1),
        'load_critical': round(critical_load_w, 1),
        'load_important': round(important_load_w, 1),
        'load_non_critical': round(non_critical_load_w, 1),
        'shed_important': ai_decisions['shed_important'],
        'shed_non_critical': ai_decisions['shed_non_critical'],
        'total_load': load_w,
        'battery_out': bat_out_w,
        'battery_pct': true_soc,
        'usable_soc': usable_soc,
        'battery_temp': round(battery_temp, 1),
        'charge_allowed': charge_allowed,
        'discharge_allowed': discharge_allowed,
        'ai_decisions': ai_decisions,
        'ai_forecast': forecast,
        'ai_6h_summary': ai_6h_summary,
        'ai_schedule': ai_schedule,
        'anomalies': anomalies,
        'home_status': True,
        'grid_status': False,
        'import_status': (generator_w > 0),
        'time': now.strftime('%H:%M:%S')
    }

def generate_dummy_history(period):
    now = timezone.now()
    labels, solar_data, grid_data = [], [], []

    if period == 'week':
        for i in range(6, -1, -1):
            day_time = now - timedelta(days=i)
            labels.append(day_time.strftime('%b %d'))
            solar_data.append(round(random.uniform(420, 680), 1))
            grid_data.append(round(random.uniform(150, 320), 1))
    elif period == 'month':
        for i in range(29, -1, -1):
            day_time = now - timedelta(days=i)
            labels.append(day_time.strftime('%b %d'))
            solar_data.append(round(random.uniform(380, 720), 1))
            grid_data.append(round(random.uniform(140, 350), 1))
    else: # day (24 hours)
        for h in range(23, -1, -1):
            t = now - timedelta(hours=h)
            hour = t.hour
            labels.append(t.strftime('%I %p'))
            
            if 6 <= hour <= 18:
                peak_factor = 1.0 - abs(hour - 12.5) / 6.5
                solar_w = max(0.0, round(peak_factor * 1100.0 + random.uniform(-40, 40), 1))
            else:
                solar_w = 0.0

            if 7 <= hour <= 9 or 18 <= hour <= 22:
                grid_w = round(random.uniform(300, 600), 1)
            else:
                grid_w = round(random.uniform(80, 200), 1)

            solar_data.append(solar_w)
            grid_data.append(grid_w)

    return {
        'labels': labels,
        'solar': solar_data,
        'grid': grid_data
    }

def generate_dummy_financial(period):
    if period == 'week':
        solar_u = 124.8
        bought_u = 21.5
        sold_u = 18.2
        fuel_saved = 45.2
    elif period == 'year':
        solar_u = 6380.0
        bought_u = 1120.0
        sold_u = 950.0
        fuel_saved = 2145.0
    else: # month
        solar_u = 578.4
        bought_u = 92.6
        sold_u = 78.0
        fuel_saved = 185.5

    BUY_RATE = 8.0
    SELL_RATE = 4.0
    FUEL_RATE = 105.0
    CO2_PER_LITER = 2.68

    return {
        'solar_units': round(solar_u, 2),
        'solar_value': round(solar_u * BUY_RATE, 2),
        'bought_units': round(bought_u, 2),
        'bought_cost': round(bought_u * BUY_RATE, 2),
        'sold_units': round(sold_u, 2),
        'sold_profit': round(sold_u * SELL_RATE, 2),
        'fuel_saved_liters': round(fuel_saved, 2),
        'fuel_savings_rs': round(fuel_saved * FUEL_RATE, 2),
        'co2_avoided_kg': round(fuel_saved * CO2_PER_LITER, 2),
        'ai_total_savings': round((solar_u * BUY_RATE) + (sold_u * SELL_RATE) + (fuel_saved * FUEL_RATE), 2)
    }

# --- ESP32 SYNC (RECEIVE DATA / SEND COMMANDS) ---
@csrf_exempt
def handle_esp_communication(request):
    relays = get_current_relay_state()
    
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            try:
                SystemReading.objects.create(
                    solar_power_watts=data.get('solar_w', 0),
                    battery_discharge_watts=data.get('bat_out_w', 0),
                    grid_power_watts=data.get('grid_w', 0),
                    battery_percentage=data.get('bat_pct', 0),
                    relay_home_status=relays['home'],
                    relay_grid_status=relays['sell'],
                    relay_import_status=relays['import']
                )
            except Exception:
                pass # SQLite database write exception on Vercel lambda ignored gracefully

            return JsonResponse({
                "status": "success",
                "relay_home": relays['home'],
                "relay_grid": relays['sell'],
                "relay_import": relays['import']
            })
        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)}, status=400)

    return JsonResponse({"status": "ok"})

# --- LIVE DATA API ---
def get_latest_reading(request):
    try:
        reading = SystemReading.objects.latest('timestamp')
        relays = get_current_relay_state()
        return JsonResponse({
            'solar': reading.solar_power_watts,
            'grid': reading.grid_power_watts,
            'gen': reading.grid_power_watts,
            'fuel_pct': 100.0,
            'fuel_rate': 0.0,
            'fuel_time': 0.0,
            'battery_out': reading.battery_discharge_watts,
            'battery_pct': reading.battery_percentage,
            'usable_soc': reading.battery_percentage, # real data fallback
            'battery_temp': 20.0,
            'charge_allowed': True,
            'discharge_allowed': True,
            'home_status': relays['home'],
            'grid_status': relays['sell'],
            'import_status': relays['import'],
            'time': reading.timestamp.strftime('%H:%M:%S')
        })
    except Exception:
        # Fallback to dynamic realistic dummy reading when real sensor data is not available
        return JsonResponse(generate_dummy_reading(timezone.now()))

# --- HISTORY CHART API ---
def get_history_data(request):
    period = request.GET.get('period', 'day')
    now = timezone.now()
    
    try:
        if period == 'week':
            start_date = now - timedelta(days=7)
            trunc_func = TruncDay('timestamp')
            fmt = '%b %d'
        elif period == 'month':
            start_date = now - timedelta(days=30)
            trunc_func = TruncDay('timestamp')
            fmt = '%b %d'
        else: 
            start_date = now - timedelta(hours=24)
            trunc_func = TruncHour('timestamp')
            fmt = '%I %p'

        data = SystemReading.objects.filter(timestamp__gte=start_date)\
            .annotate(date=trunc_func)\
            .values('date')\
            .annotate(avg_solar=Avg('solar_power_watts'), avg_grid=Avg('grid_power_watts'))\
            .order_by('date')

        data_list = list(data)
        if not data_list:
            return JsonResponse(generate_dummy_history(period))

        return JsonResponse({
            'labels': [entry['date'].strftime(fmt) for entry in data_list],
            'solar': [round(entry['avg_solar'] or 0, 1) for entry in data_list],
            'grid': [round(entry['avg_grid'] or 0, 1) for entry in data_list]
        })
    except Exception:
        return JsonResponse(generate_dummy_history(period))

# --- FINANCIAL REPORT API ---
def get_financial_data(request):
    period = request.GET.get('period', 'month')
    now = timezone.now()
    
    try:
        if period == 'week': start_date = now - timedelta(days=7)
        elif period == 'year': start_date = now - timedelta(days=365)
        else: start_date = now - timedelta(days=30)

        readings = SystemReading.objects.filter(timestamp__gte=start_date)
        readings_count = readings.count()

        if readings_count == 0:
            return JsonResponse(generate_dummy_financial(period))

        TIME_INTERVAL_HOURS = 2 / 3600.0 
        
        total_solar_kwh = 0.0
        total_grid_bought_kwh = 0.0
        total_sold_kwh = 0.0
        
        for r in readings:
            total_solar_kwh += (r.solar_power_watts * TIME_INTERVAL_HOURS) / 1000.0
            total_grid_bought_kwh += (r.grid_power_watts * TIME_INTERVAL_HOURS) / 1000.0
            
            if r.relay_grid_status:
                total_sold_kwh += (r.battery_discharge_watts * TIME_INTERVAL_HOURS) / 1000.0

        if total_solar_kwh == 0 and total_grid_bought_kwh == 0 and total_sold_kwh == 0:
            return JsonResponse(generate_dummy_financial(period))

        BUY_RATE = 8.0; SELL_RATE = 4.0; FUEL_RATE = 105.0; CO2_PER_LITER = 2.68
        
        # Approximate AI fuel savings based on solar generated (assuming solar offset diesel)
        # roughly 1 liter diesel = 3 kWh
        fuel_saved_liters = total_solar_kwh / 3.0
        
        return JsonResponse({
            'solar_units': round(total_solar_kwh, 2),
            'solar_value': round(total_solar_kwh * BUY_RATE, 2),
            'bought_units': round(total_grid_bought_kwh, 2),
            'bought_cost': round(total_grid_bought_kwh * BUY_RATE, 2),
            'sold_units': round(total_sold_kwh, 2),
            'sold_profit': round(total_sold_kwh * SELL_RATE, 2),
            'fuel_saved_liters': round(fuel_saved_liters, 2),
            'fuel_savings_rs': round(fuel_saved_liters * FUEL_RATE, 2),
            'co2_avoided_kg': round(fuel_saved_liters * CO2_PER_LITER, 2),
            'ai_total_savings': round((total_solar_kwh * BUY_RATE) + (total_sold_kwh * SELL_RATE) + (fuel_saved_liters * FUEL_RATE), 2)
        })
    except Exception:
        return JsonResponse(generate_dummy_financial(period))

# --- TOGGLE RELAYS (UPDATED 4-MODE LOGIC WITH VERCEL IN-MEMORY FALLBACK) ---
def toggle_relays(request):
    mode = request.GET.get('mode')
    
    if mode == 'home':
        IN_MEMORY_RELAY_STATE['home'] = True
        IN_MEMORY_RELAY_STATE['sell'] = False
        IN_MEMORY_RELAY_STATE['import'] = False
    elif mode == 'grid_sell':
        IN_MEMORY_RELAY_STATE['home'] = False
        IN_MEMORY_RELAY_STATE['sell'] = True
        IN_MEMORY_RELAY_STATE['import'] = False
    elif mode == 'grid_import':
        IN_MEMORY_RELAY_STATE['home'] = False
        IN_MEMORY_RELAY_STATE['sell'] = False
        IN_MEMORY_RELAY_STATE['import'] = True
    elif mode == 'charge':
        IN_MEMORY_RELAY_STATE['home'] = False
        IN_MEMORY_RELAY_STATE['sell'] = False
        IN_MEMORY_RELAY_STATE['import'] = False

    try:
        state, _ = RelayState.objects.get_or_create(pk=1)
        state.home_load_active = IN_MEMORY_RELAY_STATE['home']
        state.grid_sell_active = IN_MEMORY_RELAY_STATE['sell']
        state.grid_import_active = IN_MEMORY_RELAY_STATE['import']
        state.save()
    except Exception:
        pass

    return JsonResponse({
        'mode': mode, 
        'home': IN_MEMORY_RELAY_STATE['home'], 
        'sell': IN_MEMORY_RELAY_STATE['sell'],
        'import': IN_MEMORY_RELAY_STATE['import']
    })

@csrf_exempt
def update_simulator(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            IN_MEMORY_SIMULATION_STATE.update({
                'temperature': data.get('temperature', -5),
                'cloud_cover': data.get('cloud_cover', 20),
                'solar_availability': data.get('solar_availability', 100),
                'wind_speed': data.get('wind_speed', 10),
                'snow': data.get('snow', 'None'),
                'scenario': data.get('scenario', 'Custom')
            })
            
            if 'fuel_pct' in data:
                IN_MEMORY_RELAY_STATE['fuel_pct'] = float(data['fuel_pct'])
                
            return JsonResponse({"status": "success", "state": IN_MEMORY_SIMULATION_STATE})
        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)}, status=400)
    return JsonResponse({"status": "ok"})

# --- ENERGY DATA API ---
def get_energy_data(request):
    try:
        limit = int(request.GET.get('limit', 100))
        offset = int(request.GET.get('offset', 0))
        
        # Get data ordered by timestamp descending (most recent first)
        data = EnergyData.objects.all()[offset:offset+limit]
        
        result = []
        for d in data:
            result.append({
                'id': d.id,
                'timestamp': d.timestamp.isoformat(),
                'solar_power': d.solar_power,
                'load_power': d.load_power,
                'battery_voltage': d.battery_voltage,
                'battery_current': d.battery_current,
                'battery_soc': d.battery_soc,
                'battery_temperature': d.battery_temperature,
                'grid_power': d.grid_power,
                'generator_power': d.generator_power,
                'fuel_level': d.fuel_level,
                'outside_temperature': d.outside_temperature,
                'weather_condition': d.weather_condition
            })
        
        return JsonResponse({
            'count': EnergyData.objects.count(),
            'limit': limit,
            'offset': offset,
            'results': result
        })
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)