"""visitcar device export shim. Implementation in `iparking_lib/visitcar/device.py`.

See the sibling `driver.py` for why the `sys.path` insert is required rather than tidy.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from iparking_lib.visitcar.device import VisitCarDevice_


class Device(VisitCarDevice_):
    """One paired parking lot: the 주차장명 sensor plus the register Flow action."""


homey_export = Device
