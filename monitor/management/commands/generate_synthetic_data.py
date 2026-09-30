import random
import math
from datetime import timedelta
from django.utils import timezone
from django.core.management.base import BaseCommand
from monitor.models import EnergyData

class Command(BaseCommand):
    help = 'Generates synthetic historical energy data, including extreme polar scenarios.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=90, help='Number of days of data to generate')

    def handle(self, *args, **options):
        days = options['days']
        self.stdout.write(f"Deleting old data...")
        EnergyData.objects.all().delete()
        
        end_date = timezone.now()
        start_date = end_date - timedelta(days=days)
        
        current_date = start_date
        records = []
        
        self.stdout.write(f"Generating {days} days of data (5-minute intervals)...")
        interval = timedelta(minutes=5)
        
        while current_date <= end_date:
            hour = current_date.hour
            
            # Simulated 5-day extreme polar blizzard somewhere in the middle
            days_passed = (current_date - start_date).days
            is_extreme = False
            if (days // 3) <= days_passed <= (days // 3) + 5:
                is_extreme = True

            if is_extreme:
                weather = "Blizzard"
                temp = random.uniform(-40, -20)
                solar_irradiance_factor = 0.1
            else:
                weather = random.choice(["Sunny", "Cloudy", "Overcast", "Clear", "Rainy"])
                base_temp = 10 if weather in ["Sunny", "Clear"] else 5
                temp = base_temp + 10 * math.sin((hour - 8) / 24.0 * 2 * math.pi) + random.uniform(-2, 2)
                solar_irradiance_factor = 1.0 if weather in ["Sunny", "Clear"] else 0.4
            
            # Solar power (e.g., 5kW max array)
            if 6 <= hour <= 18:
                hour_factor = math.sin((hour - 6) / 12.0 * math.pi)
                solar_power = max(0, 5000 * hour_factor * solar_irradiance_factor * random.uniform(0.8, 1.2))
            else:
                solar_power = 0
            
            # Load power (Base load ~500W, Peaks up to 3kW)
            base_load = 500
            if 7 <= hour <= 9 or 18 <= hour <= 22:
                base_load += 2000
            if is_extreme:
                base_load += 3000 # Heaters running full blast
                
            load_power = base_load * random.uniform(0.8, 1.2)
            
            # Simple Battery SOC simulation
            if 6 <= hour <= 18:
                battery_soc = 50 + 50 * math.sin((hour - 6) / 12.0 * math.pi)
            else:
                if hour > 18:
                    battery_soc = 100 - ((hour - 18) / 12.0) * 50
                else:
                    battery_soc = 50 - (hour / 6.0) * 20
            
            # In extreme weather, batteries deplete faster and charge less
            if is_extreme:
                battery_soc -= 20
                
            battery_soc = max(0, min(100, battery_soc + random.uniform(-2, 2)))
            
            # Battery specs
            battery_voltage = 48.0 + (battery_soc / 100.0) * 8.0 # 48V - 56V
            net_power = solar_power - load_power
            battery_current = net_power / battery_voltage
            battery_temperature = temp + 5 + (abs(battery_current) * 0.1)
            
            # Generators and Grid logic
            grid_power = 0
            generator_power = 0
            fuel_level = 100 - (days_passed % 14) * 7 # Refill every 14 days
            
            if is_extreme:
                # Need backup power
                if random.random() < 0.3:
                    grid_power = load_power
                else:
                    generator_power = load_power
                    fuel_level = max(0, fuel_level - 0.5)
            elif battery_soc < 20:
                grid_power = load_power
            
            records.append(EnergyData(
                timestamp=current_date,
                solar_power=round(solar_power, 2),
                load_power=round(load_power, 2),
                battery_voltage=round(battery_voltage, 2),
                battery_current=round(battery_current, 2),
                battery_soc=round(battery_soc, 2),
                battery_temperature=round(battery_temperature, 2),
                grid_power=round(grid_power, 2),
                generator_power=round(generator_power, 2),
                fuel_level=round(fuel_level, 2),
                outside_temperature=round(temp, 2),
                weather_condition=weather
            ))
            
            # Batch insert
            if len(records) >= 2000:
                EnergyData.objects.bulk_create(records)
                records = []
                
            current_date += interval
            
        if records:
            EnergyData.objects.bulk_create(records)
            
        self.stdout.write(self.style.SUCCESS(f"Successfully generated data from {start_date.date()} to {end_date.date()}."))
