from django.db import models

class SystemReading(models.Model):
    # Sensor Data
    solar_power_watts = models.FloatField(default=0.0)
    battery_discharge_watts = models.FloatField(default=0.0)
    grid_power_watts = models.FloatField(default=0.0)
    battery_percentage = models.FloatField(default=0.0)
    
    # Relay Status Logs (History)
    relay_home_status = models.BooleanField(default=False)   # Relay 1
    relay_grid_status = models.BooleanField(default=False)   # Relay 2
    relay_import_status = models.BooleanField(default=False) # Relay 3 (NEW)
    
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.timestamp.strftime('%Y-%m-%d %H:%M')} - Solar: {self.solar_power_watts}W"

class RelayState(models.Model):
    # Singleton model: Only row ID=1 is used
    home_load_active = models.BooleanField(default=False)
    grid_sell_active = models.BooleanField(default=False)
    grid_import_active = models.BooleanField(default=False) # Relay 3 (NEW)

    def save(self, *args, **kwargs):
        self.pk = 1
        super(RelayState, self).save(*args, **kwargs)

from django.utils import timezone

class EnergyData(models.Model):
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    solar_power = models.FloatField(help_text="Solar power generation in Watts", default=0.0)
    load_power = models.FloatField(help_text="Power consumption of the load in Watts", default=0.0)
    battery_voltage = models.FloatField(help_text="Battery voltage in Volts", default=0.0)
    battery_current = models.FloatField(help_text="Battery current in Amperes", default=0.0)
    battery_soc = models.FloatField(help_text="Battery State of Charge (%)", default=0.0)
    battery_temperature = models.FloatField(help_text="Battery temperature in Celsius", default=0.0)
    grid_power = models.FloatField(help_text="Grid power in Watts", default=0.0)
    generator_power = models.FloatField(help_text="Generator power in Watts", default=0.0)
    fuel_level = models.FloatField(help_text="Fuel level (%)", default=0.0)
    outside_temperature = models.FloatField(help_text="Outside temperature in Celsius", default=0.0)
    weather_condition = models.CharField(max_length=100, help_text="Weather condition (e.g. Sunny, Cloudy)", default="Unknown")

    class Meta:
        ordering = ['-timestamp']
        verbose_name_plural = "Energy Data"

    def __str__(self):
        return f"{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')} - Solar: {self.solar_power}W, Load: {self.load_power}W"