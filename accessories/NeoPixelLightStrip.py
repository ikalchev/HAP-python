# An Accessory for Adafruit NeoPixels attached to GPIO Pin18
# Tested using Python 3.6 Raspberry Pi
# This device uses all available services for the Homekit Lightbulb API
# Note: set your neopixels settings under the #NeoPixel constructor arguments
# Note: RPi GPIO must be PWM. Neopixels.py will warn if wrong GPIO is used
#       at runtime
# Note: This Class requires the installation of rpi_ws281x lib
#       Follow the instllation instructions;
#           git clone https://github.com/jgarff/rpi_ws281x.git
#           cd rpi_ws281x
#           scons
#
#           cd python
#           sudo python3.6 setup.py install
# https://learn.adafruit.com/neopixels-on-raspberry-pi/software

# Apple Homekit API Call Order
# User changes light settings on iOS device
# Changing Brightness - State - Hue - Brightness
# Changing Color      - Saturation - Hue
# Changing Temp/Sat   - Saturation - Hue
# Changing State      - State

# import logging
from neopixel import *

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_LIGHTBULB


class NeoPixelLightStrip(Accessory):

    category = CATEGORY_LIGHTBULB

    __accessoryState = 0  # State of the neo light On/Off
    __hue = 0  # Hue Value 0 - 360 Homekit API
    __saturation = 100  # Saturation Values 0 - 100 Homekit API
    __brightness = 100  # Brightness value 0 - 100 Homekit API

    # NeoPixel constructor arguments
    LED_COUNT = 8
    LED_PIN = 18
    LED_FREQ_HZ = 800000
    LED_DMA = 10
    LED_BRIGHTNESS = 255  # Note this is for the neopixel object construct only
    LED_INVERT = False
    __neo_strip = Adafruit_NeoPixel(LED_COUNT, LED_PIN, LED_FREQ_HZ,
                                    LED_DMA, LED_INVERT, LED_BRIGHTNESS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Set our neopixel API services up using Lightbulb base
        serv_light = self.add_preload_service(
            'Lightbulb', chars=['On', 'Hue', 'Saturation', 'Brightness'])

        # Configure our callbacks
        self.char_hue = serv_light.configure_char(
            'Hue', setter_callback=self.set_hue)
        self.char_saturation = serv_light.configure_char(
            'Saturation', setter_callback=self.set_saturation)
        self.char_on = serv_light.configure_char(
            'On', setter_callback=self.set_bulb)
        self.char_on = serv_light.configure_char(
            'Brightness', setter_callback=self.set_brightness)

        # Must be called before any colors can be applied to neoPixels
        self.__neo_strip.begin()

    # def __setstate__(self, state):
    #     print("___ setstate ___")
    #     self.__dict__.update(state)

    def set_bulb(self, value):
        self.__accessoryState = value
        if value == 1:  # On
            self.set_hue(self.__hue)
        else:
            self.Update_NeoPixel_With_Color(0, 0, 0)  # Off

    def set_hue(self, value):
        # Lets only write the new RGB values if the power is on
        # otherwise update the hue value only
        if self.__accessoryState == 1:
            self.__hue = value
            rgb_tuple = self.hsv_to_rgb(
                self.__hue, self.__saturation, self.__brightness)
            self.Update_NeoPixel_With_Color(
                rgb_tuple[0], rgb_tuple[1], rgb_tuple[2])
        else:
            self.__hue = value

    def set_brightness(self, value):
        self.__brightness = value
        self.set_hue(self.__hue)

    def set_saturation(self, value):
        self.__saturation = value
        self.set_hue(self.__hue)

    def Update_NeoPixel_With_Color(self, red, green, blue):
        # For some reason the neopixels I have are G-R-B
        # or it could be the neopixel.py library
        # Change the setPixelColor inputs for yourself below
        for i in range(self.LED_COUNT):
            self.__neo_strip.setPixelColor(
                i, Color(int(green), int(red), int(blue)))

        self.__neo_strip.show()

    def stop(self):
        super().stop()

    def hsv_to_rgb(self, h, s, v):
        # This function takes
        # h - 0 - 360 Deg
        # s - 0 - 100 %
        # v - 0 - 100 %

        hPri = h / 60
        s = s / 100
        v = v / 100

        if s <= 0.0:
            return int(0), int(0), int(0)

        C = v * s  # Chroma
        X = C * (1 - abs(hPri % 2 - 1))

        RGB_Pri = [0.0, 0.0, 0.0]

        if 0 <= hPri <= 1:
            RGB_Pri = [C, X, 0]
        elif 1 <= hPri <= 2:
            RGB_Pri = [X, C, 0]
        elif 2 <= hPri <= 3:
            RGB_Pri = [0, C, X]
        elif 3 <= hPri <= 4:
            RGB_Pri = [0, X, C]
        elif 4 <= hPri <= 5:
            RGB_Pri = [X, 0, C]
        elif 5 <= hPri <= 6:
            RGB_Pri = [C, 0, X]
        else:
            RGB_Pri = [0, 0, 0]

        m = v - C
        return int((RGB_Pri[0] + m) * 255), int((RGB_Pri[1] + m) * 255), int((RGB_Pri[2] + m) * 255)  
