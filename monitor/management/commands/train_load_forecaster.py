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
    help = 'Train a Random Forest model to forecast load (1h, 6h, 24h ahead)'

    def handle(self, *args, **options):
        self.stdout.write("Fetching data from the database...")
        
        # Load historical data
        qs = EnergyData.objects.all().order_by('timestamp').values(
            'timestamp', 'load_power', 'outside_temperature', 'weather_condition', 'battery_soc'
        )
        df = pd.DataFrame.from_records(qs)
        
        if len(df) < 1000:
            self.stdout.write(self.style.ERROR("Not enough data to train. Please generate data first."))
            return
            
        self.stdout.write(f"Loaded {len(df)} records. Preparing features and targets...")
        
        # Sort by timestamp chronologically
        df.sort_values('timestamp', inplace=True)
        df.reset_index(drop=True, inplace=True)
        
        # Create features
        df['hour'] = df['timestamp'].dt.hour
        df['dayofweek'] = df['timestamp'].dt.dayofweek
        
        le = LabelEncoder()
        df['weather_encoded'] = le.fit_transform(df['weather_condition'])
        
        # Since data is at 5-minute intervals:
        # Next 1 hour = next 12 intervals
        # Next 6 hours = next 72 intervals
        # Next 24 hours = next 288 intervals
        
        # Target: Mean load over the upcoming window
        df['target_1h'] = df['load_power'].rolling(window=12, min_periods=1).mean().shift(-12)
        df['target_6h'] = df['load_power'].rolling(window=72, min_periods=1).mean().shift(-72)
        df['target_24h'] = df['load_power'].rolling(window=288, min_periods=1).mean().shift(-288)
        
        # Drop end of dataset where we don't have future targets
        df.dropna(subset=['target_1h', 'target_6h', 'target_24h'], inplace=True)
        
        features = ['load_power', 'hour', 'dayofweek', 'outside_temperature', 'weather_encoded', 'battery_soc']
        targets = ['target_1h', 'target_6h', 'target_24h']
        
        X = df[features]
        y = df[targets]
        
        # Chronological Train-Test Split (80% train, 20% test)
        split_idx = int(len(df) * 0.8)
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
        
        self.stdout.write("Training Random Forest model (this might take a few moments)...")
        # Initialize and train Multi-Output Regressor
        model = RandomForestRegressor(n_estimators=50, max_depth=10, random_state=42, n_jobs=-1)
        model.fit(X_train, y_train)
        
        self.stdout.write("Evaluating model on test data...")
        predictions = model.predict(X_test)
        
        target_labels = ['Next 1 Hour', 'Next 6 Hours', 'Next 24 Hours']
        for i, label in enumerate(target_labels):
            mae = mean_absolute_error(y_test.iloc[:, i], predictions[:, i])
            rmse = np.sqrt(mean_squared_error(y_test.iloc[:, i], predictions[:, i]))
            self.stdout.write(f"  {label} -> MAE: {mae:.2f} W | RMSE: {rmse:.2f} W")
            
        # Save model and label encoder
        model_dir = os.path.join('monitor', 'ml_models')
        os.makedirs(model_dir, exist_ok=True)
        
        model_path = os.path.join(model_dir, 'load_forecaster_rf.joblib')
        le_path = os.path.join(model_dir, 'weather_encoder.joblib')
        
        joblib.dump(model, model_path)
        joblib.dump(le, le_path)
        
        self.stdout.write(self.style.SUCCESS(f"\nModel successfully saved to {model_path}"))
