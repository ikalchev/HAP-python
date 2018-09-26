"""
An Accessory wrapper for the SDS011 air particulate density sensor.

The density sensor implementation can be found here:
https://github.com/ikalchev/py-sds011
Place the file under a package named sensors in your python path,
or change the import altogether.
"""
import time
import logging

from sensors.SDS011 import SDS011 as AirSensor
from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR

logger = logging.getLogger(__name__)


class SDS011(Accessory):
    """Accessory wrapper for SDS011.
    """

    category = CATEGORY_SENSOR

    SORTED_PM_QUALITY_MAP = ((200, 5), (150, 4), (100, 3), (50, 2), (0, 1))
    """
    Threshold-to-state tuples. These show the state for which the threshold is
    lower boundary. Uses something like Air Quality Index (AQI).

    The UI shows:
        1 - Excellent
        2 - Good
        3 - Fair
        4 - Inferior
        5 - Poor
    """

    def __init__(self, serial_port, *args, sleep_duration_s=15*60,
                 calib_duration_s=15, **kwargs):
        """Initialize and start SDS011 on the given port.

        @param serial_port: The SDS011 port, e.g. /dev/ttyUSB0.
        @type serial_port: str

        @param sleep_duration_s: The amount of time in seconds the sensor sleeps before
            waking it up for reading. Defaults to 15 minutes.
        @type sleep_duration_s: float

        @param calib_duration_s: The amount of time in seconds to wait after the sensor
            has been waken up before reading its data. Defaults to 15 seconds.
        @type calib_duration_s: float
        """
        self.pm25_quality = None
        self.pm25_density = None
        self.pm10_quality = None
        self.pm10_density = None
        super().__init__(*args, **kwargs)

        # PM2.5
        air_quality_pm25 = self.add_preload_service(
            'AirQualitySensor', chars=['Name', 'AirParticulateSize',
                                       'AirParticulateDensity'])
        air_quality_pm25.configure_char('AirParticulateSize', value=0)
        air_quality_pm25.configure_char('Name', value='PM2.5')
        self.pm25_quality = air_quality_pm25.configure_char('AirQuality')
        self.pm25_density = air_quality_pm25.configure_char(
            'AirParticulateDensity')

        # PM10
        air_quality_pm10 = self.add_preload_service(
            'AirQualitySensor', chars=['Name', 'AirParticulateSize',
                                       'AirParticulateDensity'])
        air_quality_pm10.configure_char('AirParticulateSize', value=1)
        air_quality_pm10.configure_char('Name', value='PM10')
        self.pm10_quality = air_quality_pm10.configure_char('AirQuality')
        self.pm10_density = air_quality_pm10.configure_char(
            'AirParticulateDensity')

        self.sleep_duration_s = sleep_duration_s
        self.calib_duration_s = calib_duration_s
        self.serial_port = serial_port
        self.sensor = AirSensor(serial_port)
        self.sensor.sleep(sleep=False)

    def __getstate__(self):
        """Get the state, less the sensor.
        """
        state = super(SDS011, self).__getstate__()
        state['sensor'] = None
        return state

    def __setstate__(self, state):
        """Set the state of this Accessory and initialize the sensor
            with the serial port in the state.
        """
        self.__dict__.update(state)
        self.sensor = AirSensor(self.serial_port)

    def get_quality_classification(self, pm, is_pm25=False):
        """Get the air quality classification based on the PM density.

        Uses Air Quality Index (AQI), without averaging for an hour.

        @see: SDS011.SORTED_PM_QUALITY_MAP

        @rtype: int
        """
        assert pm >= 0
        return next(state for threshold, state in self.SORTED_PM_QUALITY_MAP
                    if threshold <= pm)

    def run(self):
        """Start updating the air quality readings.

        Initially, we read from the sensor and update the values. Then we put
        it in sleep mode and while the sentinel is not set:
            - Sleep for `self.sleep_duration_s`.
            - Wake up and wait `self.calib_duration_s` seconds.
            - Get the sensor's readings and update.
        """
        pm25, pm10 = self.sensor.query()
        self.pm25_density.set_value(pm25)
        self.pm25_quality.set_value(
            self.get_quality_classification(pm25, is_pm25=True))
        self.pm10_density.set_value(pm10)
        self.pm10_quality.set_value(
            self.get_quality_classification(pm10, is_pm25=False))
        self.sensor.sleep()
        while not self.driver.stop_event.wait(self.sleep_duration_s):
            logger.debug("Waking up sensor.")
            self.sensor.sleep(sleep=False)
            time.sleep(self.calib_duration_s)
            pm25, pm10 = self.sensor.query()
            self.pm25_density.set_value(pm25)
            self.pm25_quality.set_value(
                self.get_quality_classification(pm25, is_pm25=True))
            self.pm10_density.set_value(pm10)
            self.pm10_quality.set_value(
                self.get_quality_classification(pm10, is_pm25=False))
            self.sensor.sleep()
            logger.debug("Read cycle done. Sleeping.")
