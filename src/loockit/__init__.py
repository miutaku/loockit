"""loockit — local-first SESAME4 / SESAME Bot1 controller.

Provides:
- Local BLE control of SesameOS2 devices (via ``pysesameos2``).
- A gRPC API for lock/unlock/toggle/click and status (incl. streaming).
- Real-time state monitoring driven by SesameOS2 notifications.
- A Matter bridge that exposes the locally-controlled devices to smart homes.
"""

__version__ = "0.1.0"
