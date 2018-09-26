import pytest

from pyhap.loader import Loader


@pytest.fixture(scope='session')
def mock_driver():
    yield MockDriver()


class MockDriver():

    def __init__(self):
        self.loader = Loader()

    def publish(self, data):
        pass
