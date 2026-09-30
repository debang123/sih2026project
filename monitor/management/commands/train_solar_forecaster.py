import os
import math
import joblib
import pandas as pd
import numpy as np
from django.core.management.base import BaseCommand
from monitor.models import EnergyData
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.preprocessing import LabelEncoder

class Command(BaseCommand):
    help = 'Train a Random Forest model to forecast solar generation (1h, 6h, 24h ahead)'

    def handle(self, *args, **options):
        self.stdout.write("Fetching solar data from the database...")
        
        # Load historical data
        qs = EnergyData.objects.all().order_by('timestamp').values(
            'timestamp', 'solar_power', 'outside_temperature', 'weather_condition'
        )
        df = pd.DataFrame.from_records(qs)
        
        if len(df) < 1000:
            self.stdout.write(self.style.ERROR("Not enough data to train. Please generate data first."))
            return
            
        self.stdout.write(f"Loaded {len(df)} records. Engineering solar-specific features...")
        
        # Sort by timestamp chronologically
        df.sort_values('timestamp', inplace=True)
        df.reset_index(drop=True, inplace=True)
        
        # Create standard time features
        df['hour'] = df['timestamp'].dt.hour
        df['month'] = df['timestamp'].dt.month
        
        # 1. Sunlight Availability Feature (Proxy using a sine wave based on daylight hours 6AM to 6PM)
        # 0 if night, peak at noon
        df['sunlight_availability'] = df['hour'].apply(
            lambda h: max(0.0, math.sin((h - 6) / 12.0 * math.pi)) if 6 <= h <= 18 else 0.0
        )
        
        # 2. Cloud Cover proxy from weather conditions
        cloud_map = {
            'Sunny': 0.1,
            'Clear': 0.1,
            'Cloudy': 0.6,
            'Overcast': 0.8,
            'Rainy': 0.9,
            'Blizzard': 1.0
        }
        df['cloud_cover'] = df['weather_condition'].map(lambda w: cloud_map.get(w, 0.5))
        
        # Encode weather condition as backup feature
        le = LabelEncoder()
        df['weather_encoded'] = le.fit_transform(df['weather_condition'])
        
        # Target Variables (Mean solar generation over the next window)
        # Next 1 hour = next 12 intervals
        # Next 6 hours = next 72 intervals
        # Next 24 hours = next 288 intervals
        
        df['target_1h'] = df['solar_power'].rolling(window=12, min_periods=1).mean().shift(-12)
        df['target_6h'] = df['solar_power'].rolling(window=72, min_periods=1).mean().shift(-72)
        df['target_24h'] = df['solar_power'].rolling(window=288, min_periods=1).mean().shift(-288)
        
        # Drop end of dataset where we don't have future targets
        df.dropna(subset=['target_1h', 'target_6h', 'target_24h'], inplace=True)
        
        features = ['solar_power', 'hour', 'month', 'outside_temperature', 'sunlight_availability', 'cloud_cover', 'weather_encoded']
        targets = ['target_1h', 'target_6h', 'target_24h']
        
        X = df[features]
        y = df[targets]
        
        # Chronological Train-Test Split (80% train, 20% test)
        split_idx = int(len(df) * 0.8)
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
        
        self.stdout.write("Training Random Forest Solar Forecaster...")
        # Initialize and train Multi-Output Regressor
        model = RandomForestRegressor(n_estimators=50, max_depth=10, random_state=42, n_jobs=-1)
        model.fit(X_train, y_train)
        
        self.stdout.write("Evaluating solar model on test data...")
        predictions = model.predict(X_test)
        
        target_labels = ['Next 1 Hour', 'Next 6 Hours', 'Next 24 Hours']
        for i, label in enumerate(target_labels):
            mae = mean_absolute_error(y_test.iloc[:, i], predictions[:, i])
            rmse = np.sqrt(mean_squared_error(y_test.iloc[:, i], predictions[:, i]))
            self.stdout.write(f"  {label} -> MAE: {mae:.2f} W | RMSE: {rmse:.2f} W")
            
        # Save model and label encoder
        model_dir = os.path.join('monitor', 'ml_models')
        os.makedirs(model_dir, exist_ok=True)
        
        model_path = os.path.join(model_dir, 'solar_forecaster_rf.joblib')
        le_path = os.path.join(model_dir, 'solar_weather_encoder.joblib')
        
        joblib.dump(model, model_path)
        joblib.dump(le, le_path)
        
        self.stdout.write(self.style.SUCCESS(f"\nSolar forecaster successfully saved to {model_path}"))
