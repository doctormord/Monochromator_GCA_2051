"""
main.py — Entry point of the VRS41 monochromator controller (Qt6).

Starts the application. The GUI is built when gui_main is imported (there the
QApplication + all widgets are created and the modules are wired together); here
only the Qt event loop is started via gui_main.run().

Start:  python3 main.py

All modules (device_constants, app_context, app_config, protocol_faulhaber, daq,
motion, scan_engine, gui_main) must live in the same directory. Required packages:
PyQt6, pyqtgraph, pyserial (+ optionally nidaqmx; without it daq falls back to the
simulator).
"""

import gui_main


def run():
    """Start the Qt event loop (blocks until the window is closed)."""
    gui_main.run()


if __name__ == "__main__":
    run()
