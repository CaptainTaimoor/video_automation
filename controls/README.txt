YT Automation - Controls
========================

Double-click these to toggle features (no coding needed):

  START_BOT.bat                 Start scheduler + dashboard + watchdog
  STOP_BOT.bat                  Stop those services

  ENABLE_AUTOSTART.bat          Start bot automatically at Windows logon
  DISABLE_AUTOSTART.bat         Turn off automatic start

  ENABLE_BOOT_WITHOUT_LOGIN.bat Try to start at Windows boot (before login)
  DISABLE_BOOT_WITHOUT_LOGIN.bat Remove boot-start task

  ENABLE_LAN.bat                Allow phone/other PCs on same Wi-Fi to open dashboard
  DISABLE_LAN.bat               Dashboard only on this PC (127.0.0.1)

  ENABLE_SERVICE_GUARD.bat      Recovery task every 5 minutes
  DISABLE_SERVICE_GUARD.bat     Remove recovery task

Flags (advanced): controls\flags\*.enabled  contain 1 or 0

First-time / new PC setup:
  Run setup\INSTALL.bat as Administrator (one click).