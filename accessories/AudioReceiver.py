"""A sample audio receiver (AV receiver) accessory.

Demonstrates the ``CATEGORY_AUDIO_RECEIVER`` category together with the
``Speaker`` service, so the accessory shows up with the receiver tile in
the Home app.
"""
import logging
import signal

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_AUDIO_RECEIVER


class AudioReceiver(Accessory):
    """Fake audio receiver, logs what the client sets."""

    category = CATEGORY_AUDIO_RECEIVER

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.set_info_service(
            manufacturer='HaPK',
            model='Sample AV Receiver',
            firmware_revision='1.0',
            serial_number='1'
        )

        speaker_service = self.add_preload_service(
            'Speaker', ['Mute', 'Volume', 'Active'],
        )
        self._mute = speaker_service.configure_char(
            'Mute', value=False, setter_callback=self._on_mute,
        )
        self._volume = speaker_service.configure_char(
            'Volume', value=25, setter_callback=self._on_volume,
        )
        speaker_service.configure_char('Active', value=1)

    def _on_mute(self, value):
        print('Mute' if value else 'Unmute')

    def _on_volume(self, value):
        print('Set volume to %s' % value)


def main():
    logging.basicConfig(level=logging.INFO)

    driver = AccessoryDriver(port=51826)
    driver.add_accessory(accessory=AudioReceiver(driver, 'Audio Receiver'))

    signal.signal(signal.SIGTERM, driver.signal_handler)
    driver.start()


if __name__ == '__main__':
    main()
