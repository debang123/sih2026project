import os
import math
import joblib
import pandas as pd

# Load models in memory once when module is imported
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(BASE_DIR, 'monitor', 'ml_models')

try:
    load_model = joblib.load(os.path.join(MODEL_DIR, 'load_forecaster_rf.joblib'))
    weather_encoder = joblib.load(os.path.join(MODEL_DIR, 'weather_encoder.joblib'))
    
    solar_model = joblib.load(os.path.join(MODEL_DIR, 'solar_forecaster_rf.joblib'))
    solar_weather_encoder = joblib.load(os.path.join(MODEL_DIR, 'solar_weather_encoder.joblib'))
    AI_READY = True
except Exception as e:
    print("Warning: ML models not found. AI running in fallback heuristic mode.", e)
    AI_READY = False

def get_weather_string(cloud_cover_pct, snow, temp):
    if snow == 'Blizzard': return 'Blizzard'
    if snow == 'Heavy': return 'Overcast'
    if cloud_cover_pct > 80: return 'Overcast'
    if cloud_cover_pct > 40: return 'Cloudy'
    return 'Sunny'

def run_ai_optimizer(current_state):
    """
    The Brain: Takes all environmental and system states, forecasts the next hour,
    and returns explicit operational decisions.
    """
    weather_str = get_weather_string(current_state['cloud_cover'] * 100, current_state['snow'], current_state['outside_temperature'])
    
    forecast = {
        'load_1h': current_state['load_power'],
        'solar_1h': current_state['solar_power']
    }
    
    if AI_READY:
        try:
            # Predict Load 1h
            w_enc = weather_encoder.transform([weather_str])[0]
            X_load = pd.DataFrame([{
                'load_power': current_state['load_power'],
                'hour': current_state['hour'],
                'dayofweek': current_state['dayofweek'],
                'outside_temperature': current_state['outside_temperature'],
                'weather_encoded': w_enc,
                'battery_soc': current_state['battery_soc']
            }])
            forecast['load_1h'] = load_model.predict(X_load)[0][0]
            
            # Predict Solar 1h
            sw_enc = solar_weather_encoder.transform([weather_str])[0]
            X_solar = pd.DataFrame([{
                'solar_power': current_state['solar_power'],
                'hour': current_state['hour'],
                'month': current_state['month'],
                'outside_temperature': current_state['outside_temperature'],
                'sunlight_availability': current_state['sunlight_availability'],
                'cloud_cover': current_state['cloud_cover'],
                'weather_encoded': sw_enc
            }])
            forecast['solar_1h'] = solar_model.predict(X_solar)[0][0]
        except Exception:
            pass # fallback to current values if prediction fails

    # --- DECISION ENGINE ---
    decisions = {
        'use_solar': True,
        'charge_battery': False,
        'discharge_battery': False,
        'use_grid': False,  # Synonymous with generator if off-grid
        'start_generator': False,
        'stop_generator': True,
        'shed_non_critical': False,
        'shed_important': False
    }
    
    usable_soc = current_state['usable_soc']
    bat_temp = current_state['battery_temp']
    gen_available = current_state['generator_available']
    
    # Hardware protection locks
    charge_allowed = bat_temp > 0
    discharge_allowed = bat_temp > -20
    
    # Forecasted net power
    net_1h = forecast['solar_1h'] - forecast['load_1h']
    
    # 1. Generator & Battery Logic
    if not discharge_allowed:
        # Extreme cold lockout
        decisions['discharge_battery'] = False
        decisions['start_generator'] = gen_available
        decisions['use_grid'] = gen_available
        decisions['stop_generator'] = not gen_available
    else:
        # Battery operational
        if net_1h < 0 or current_state['solar_power'] < current_state['load_power']:
            decisions['discharge_battery'] = True
            
            # Will battery survive the gap? (Assume 10kWh battery)
            deficit_wh = abs(net_1h)
            battery_wh = (usable_soc / 100.0) * 10000.0
            
            if (battery_wh < deficit_wh * 2) or (usable_soc < 25):
                # Start generator if battery is getting low or forecast looks bad
                decisions['start_generator'] = gen_available
                decisions['use_grid'] = gen_available
                decisions['stop_generator'] = not gen_available
        else:
            decisions['discharge_battery'] = False
            decisions['start_generator'] = False
            decisions['stop_generator'] = True
            decisions['use_grid'] = False
            
    # 2. Charging Logic
    if charge_allowed:
        if net_1h > 100:
            decisions['charge_battery'] = True # Charge from excess solar
        elif decisions['start_generator'] and usable_soc < 80:
            decisions['charge_battery'] = True # Charge from generator
            
    if not charge_allowed:
        decisions['charge_battery'] = False
        
    # 3. Solar Logic
    # Always use solar if available, unless battery is full and load is 0
    decisions['use_solar'] = current_state['solar_power'] > 0
    
    # 4. Load Shedding Logic (Forward-Looking)
    total_expected_load = forecast['load_1h']
    
    if not gen_available:
        if usable_soc < 40 or (usable_soc < 50 and net_1h < -2000):
            decisions['shed_non_critical'] = True
        if usable_soc < 20 or (usable_soc < 30 and net_1h < -4000):
            decisions['shed_important'] = True
            
    if current_state['load_power'] > 5000 or total_expected_load > 5000:
        decisions['shed_non_critical'] = True
        if (current_state['load_power'] * 0.5) > 5000: # rough approx if shedding NC isn't enough
            decisions['shed_important'] = True
            
            
    return forecast, decisions

def generate_schedule(current_state):
    """
    Simulates the next 24 hours to generate an automated energy schedule.
    Filters out consecutive identical states to provide a clean, readable schedule.
    """
    schedule = []
    six_hour_summary = {
        'total_load': 0,
        'total_solar': 0,
        'expected_soc': current_state['usable_soc']
    }
    
    sim_state = dict(current_state)
    weather_str = get_weather_string(sim_state['cloud_cover'] * 100, sim_state['snow'], sim_state['outside_temperature'])
    
    current_time_hr = current_state['hour']
    
    for offset in range(1, 25):
        future_hour = (current_time_hr + offset) % 24
        sim_state['hour'] = future_hour
        
        # Calculate future sunlight availability for the dummy solar model
        sun = max(0.0, math.sin((future_hour - 6) / 12.0 * math.pi)) if 6 <= future_hour <= 18 else 0.0
        sim_state['sunlight_availability'] = sun
        
        forecast = {
            'load_1h': sim_state['load_power'], 
            'solar_1h': 0
        }
        
        if AI_READY:
            try:
                w_enc = weather_encoder.transform([weather_str])[0]
                X_load = pd.DataFrame([{
                    'load_power': sim_state['load_power'],
                    'hour': future_hour,
                    'dayofweek': sim_state['dayofweek'],
                    'outside_temperature': sim_state['outside_temperature'],
                    'weather_encoded': w_enc,
                    'battery_soc': sim_state['usable_soc']
                }])
                forecast['load_1h'] = load_model.predict(X_load)[0][0]
                
                sw_enc = solar_weather_encoder.transform([weather_str])[0]
                X_solar = pd.DataFrame([{
                    'solar_power': sim_state['solar_power'],
                    'hour': future_hour,
                    'month': sim_state['month'],
                    'outside_temperature': sim_state['outside_temperature'],
                    'sunlight_availability': sun,
                    'cloud_cover': sim_state['cloud_cover'],
                    'weather_encoded': sw_enc
                }])
                forecast['solar_1h'] = solar_model.predict(X_solar)[0][0]
            except:
                pass
                
        # Simulate decisions for this future hour
        sim_state['solar_power'] = forecast['solar_1h']
        sim_state['load_power'] = forecast['load_1h']
        _, decisions = run_ai_optimizer(sim_state)
        
        # Translate decisions into human readable Source and Action
        source = "Battery"
        action = "Supply Load"
        
        if decisions['start_generator']:
            source = "Generator"
            if decisions['charge_battery']:
                action = "Load + Charge"
            else:
                action = "Supply Load"
        elif forecast['solar_1h'] > forecast['load_1h']:
            source = "Solar"
            if decisions['charge_battery']:
                action = "Load + Charge"
            else:
                action = "Supply Load"
        elif forecast['solar_1h'] > 100:
            source = "Solar + Battery"
            action = "Supply Load"
            
        if decisions['shed_important']:
            action = "Supply Critical Only"
        elif decisions['shed_non_critical']:
            action = "Shed Non-Critical"
            
        # Very rough SOC update for the simulation (assume 10kWh battery)
        net = forecast['solar_1h'] - forecast['load_1h']
        if decisions['start_generator']: net += 3000
        
        soc_change = (net / 10000.0) * 100
        sim_state['usable_soc'] = max(0, min(100, sim_state['usable_soc'] + soc_change))
        
        if offset <= 6:
            six_hour_summary['total_load'] += forecast['load_1h']
            six_hour_summary['total_solar'] += forecast['solar_1h']
            six_hour_summary['expected_soc'] = sim_state['usable_soc']
        
        schedule.append({
            'time': f"{future_hour:02d}:00",
            'source': source,
            'action': action
        })
        
    # Compress schedule: only show when source/action changes, or at regular intervals
    compressed_schedule = []
    last_combo = None
    
    for entry in schedule:
        combo = entry['source'] + entry['action']
        if combo != last_combo:
            compressed_schedule.append(entry)
            last_combo = combo
        if len(compressed_schedule) >= 6:
            break
            
    six_hour_summary['deficit'] = max(0, six_hour_summary['total_load'] - six_hour_summary['total_solar'])
            
    return compressed_schedule, six_hour_summary

def detect_anomalies(current_state, forecast):
    anomalies = []
    
    # 1. Solar Anomaly
    exp_solar = forecast['solar_1h']
    act_solar = current_state['solar_power']
    if exp_solar > 800 and act_solar < (exp_solar * 0.4):
        anomalies.append({
            'title': 'Solar Output Anomalously Low',
            'expected': f"{exp_solar/1000:.1f} kW",
            'actual': f"{act_solar/1000:.1f} kW",
            'reason': 'Snow accumulation, heavy soiling, or panel fault.'
        })
        
    # 2. Load Anomaly
    exp_load = forecast['load_1h']
    act_load = current_state['load_power']
    if exp_load > 500 and act_load > (exp_load * 1.5):
        anomalies.append({
            'title': 'Unexpected High Load',
            'expected': f"{exp_load/1000:.1f} kW",
            'actual': f"{act_load/1000:.1f} kW",
            'reason': 'Equipment malfunction, short circuit, or unauthorized heavy machinery.'
        })
        
    # 3. Battery Anomaly
    b_temp = current_state.get('battery_temp', 20)
    if b_temp > 45 or b_temp < -25:
        anomalies.append({
            'title': 'Battery Thermal Warning',
            'expected': "-20°C to 45°C",
            'actual': f"{b_temp:.1f} °C",
            'reason': 'BMS heating failure or extreme environmental exposure.'
        })
        
    # 4. Generator Anomaly
    act_gen = current_state.get('generator_w', 0)
    if act_gen > 1000:
        exp_fuel = 1.2 + (act_gen / 1000.0) * 0.6
        act_fuel = current_state.get('fuel_rate', 0)
        if act_fuel > exp_fuel * 1.3:
            anomalies.append({
                'title': 'Abnormal Fuel Consumption',
                'expected': f"{exp_fuel:.1f} L/hr",
                'actual': f"{act_fuel:.1f} L/hr",
                'reason': 'Fuel leak, clogged filters, or inefficient combustion.'
            })
            
    return anomalies
