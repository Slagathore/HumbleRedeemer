"""Selenium service builders shared by the Humble/GOG clients.

When the app runs windowless (tray mode via pythonw, or the --windowed exe),
a plain webdriver launch makes chromedriver/geckodriver pop their own console
windows on Windows. CREATE_NO_WINDOW on the service process keeps them hidden;
elsewhere it's a no-op.
"""
import os

from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.service import Service as FirefoxService

CREATE_NO_WINDOW = 0x08000000


def _hide(service):
    if os.name == "nt":
        try:
            service.creation_flags = CREATE_NO_WINDOW
        except Exception:
            pass  # older selenium — visible driver console, but functional
    return service


def chrome_service():
    return _hide(ChromeService())


def firefox_service():
    return _hide(FirefoxService())
