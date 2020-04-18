"""Test fictures and mocks."""

import asyncio
import pytest

from pyhap.loader import Loader


@pytest.fixture(scope='session')
def mock_driver():
    yield MockDriver()


class MockDriver():

    def __init__(self):
        self.loader = Loader()

    def publish(self, data, client_addr=None):
        pass

    def add_job(self, target, *args):
        asyncio.get_event_loop().run_until_complete(target(*args))
