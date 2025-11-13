# monochromator


**_TO PROPERLY RUN THE CODE THESE REQUAIREMENTS MUST BE FULFILLED_**


English — Windows setup (step by step)
What you’ll install (once):
Python (64-bit) from python.org
Packages: pyserial, matplotlib, nidaqmx
Driver: NI-DAQmx (or NI-DAQmx Runtime) for NI data-acquisition devices
1) Install Python (64-bit)
Download Python 3.10–3.12 (64-bit) from python.org (Windows installer).
Run the installer and check “Add Python to PATH”. Keep “tcl/tk and IDLE” enabled (default).
Verify install: open Command Prompt and run:
py --version
You should see something like Python 3.11.x.
2) Install the required Python packages
Run these commands in Command Prompt (copy–paste all at once is fine):
py -m pip install -U pip
py -m pip install pyserial matplotlib nidaqmx
pyserial lets the app talk to the motor over COM ports. 
PyPI
pyserial.readthedocs.io
Discussions on Python.org
matplotlib is used for plotting. 
Matplotlib
PyPI
nidaqmx is the official NI Python API. 
nidaqmx-python.readthedocs.io
PyPI
3) Install the NI-DAQmx driver (needed for nidaqmx)
Install NI-DAQmx or the NI-DAQmx Runtime from NI.
The Python nidaqmx package requires this driver to be present. 
NI Knowledge Center
PyPI
Tip: If you don’t use NI hardware on a given PC, you can skip this; but the program will only work fully (DAQ readings) where NI-DAQmx is installed. NI’s docs and user manual are here if needed.
nidaqmx-python.readthedocs.io
ni.com


**German— Windows setup (step by step)** 

1) Python (64-Bit) installieren
Python 3.10–3.12 (64-Bit) von python.org (Windows-Installer) herunterladen.
Installer starten und „Add Python to PATH“ anhaken. „tcl/tk and IDLE“ aktiviert lassen (Standard).
Prüfung: Eingabeaufforderung öffnen und ausführen:
py --version
Es sollte z. B. Python 3.11.x erscheinen.
2) Benötigte Python-Pakete installieren
In der Eingabeaufforderung:
py -m pip install -U pip
py -m pip install pyserial matplotlib nidaqmx
pyserial für die serielle (COM-)Kommunikation. 
PyPI
pyserial.readthedocs.io
matplotlib für Diagramme. 
Matplotlib
nidaqmx ist das offizielle NI-Python-Paket. 
nidaqmx-python.readthedocs.io
PyPI
3) NI-DAQmx-Treiber installieren (erforderlich für nidaqmx)
NI-DAQmx oder NI-DAQmx Runtime von NI installieren.
Das Python-Paket nidaqmx benötigt diesen Treiber. 
NI Knowledge Center
PyPI
Hinweis: Ohne NI-Hardware kann man NI-DAQmx weglassen; die DAQ-Funktionen der Software arbeiten jedoch nur auf Systemen mit installiertem NI-DAQmx. Doku/Handbuch siehe hier.
nidaqmx-python.readthedocs.io

