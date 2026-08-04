"""visitcar driver export shim. Implementation in `iparking_lib/visitcar/driver.py`.

`parents[2]` is the app root: Homey imports this file by path and does not put the app
directory on `sys.path`, so without the insert `from iparking_lib...` fails at import time —
which presents as a driver that simply does not appear on the hub.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from iparking_lib.visitcar.driver import VisitCarDriver


class Driver(VisitCarDriver):
    """iParking visitor-parking driver: one device per parking lot."""


homey_export = Driver
