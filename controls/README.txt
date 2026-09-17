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

Phase A / growth:
  RESTART_SCHEDULER.bat     Reload schedule after config changes
  ENABLE_SURGE_ANCIENT.bat  Flag for Phase B hourly Ancient surge
  DISABLE_SURGE_ANCIENT.bat Turn surge flag off
  flags\phase_a.enabled     1 = quality-lock phase active

Ready Queue (prep ahead / upload at slot):
  flags\ready_queue.enabled  0 = classic build-at-slot (default)
                             1 = day pack + free-time render + slot publish
  Also set YT_READY_QUEUE and YT_DASHBOARD_TOKEN in .env
  Docs: docs\ready_queue_ops.md
  Dashboard pages: Home · Queue · Channels · Analytics · Suggestions · Settings · Runs
