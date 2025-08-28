import logging
import tibber
import asyncio
import threading
import time  # Added for sleep functionality
from .base import Powermeter

logger = logging.getLogger(__name__)

class TibberPowermeter(Powermeter):
    def __init__(self, config):
        self.access_token = config.get("ACCESS_TOKEN")
        self.home_id = config.get("HOME_ID")
        self.user_agent = "home-assistant/b2505-meter"
        self.account = tibber.Account(self.access_token)
        self.home = next((h for h in self.account.homes if h.id == self.home_id), None)
        if not self.home:
            raise ValueError(f"Home with ID {self.home_id} not found in Tibber account.")
        
        self.power_consumption = 0.0
        self.power_production = 0.0
        self.lock = threading.Lock()  # Add lock for thread safety
        
        # Store reference to handler to avoid garbage collection
        @self.home.event("live_measurement")
        async def _update_power_data(data):
            logger.debug(f"Received live measurement data: {data.power}, production: {data.power_production}")
            with self.lock:
                self.power_consumption = getattr(data, 'power', 0.0)
                self.power_production = getattr(data, 'power_production', 0.0)
        self._update_power_data = _update_power_data
        
        self._start_live_feed_in_thread()

    def _start_live_feed_in_thread(self):
        def run_async():
            #loop = asyncio.new_event_loop()
            #asyncio.set_event_loop(loop)
            while True:  # Loop to keep attempting connection
                try:
                    self.home.start_live_feed(user_agent=self.user_agent)
                except Exception as e:
                    logger.error(f"Failed to start Tibber live feed in thread: {e}")
                    time.sleep(30)  # Wait 30 seconds before retry
        threading.Thread(target=run_async, daemon=True).start()

    def get_powermeter_watts(self):
        logger.debug(f"Calculating power: consumption={self.power_consumption}, production={self.power_production}")
        return [self.power_consumption - self.power_production - 100]

    def wait_for_message(self, timeout=5):
        """WebSocket connection is already maintained by Tibber library"""
        pass
