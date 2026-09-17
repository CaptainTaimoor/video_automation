Portable one-click setup
========================

1. Copy this whole project folder to the new PC (or git clone).
2. Copy secrets separately if needed:
     - .env
     - client_secrets.json
     - secrets\tokens\*.json
3. Windows: double-click INSTALL.bat (Run as Administrator when prompted).
   Linux/macOS: run `bash setup/install.sh` instead -- it does the venv,
   dependency and .env steps only, and skips the auto-start/firewall steps
   below, which are Windows-specific.
4. It will:
     - Install Python 3.12 and Node.js LTS via winget if missing
     - Create .venv and install requirements.txt
     - Ensure FFmpeg via imageio-ffmpeg
     - Create auto-start tasks
     - Open LAN firewall for dashboard port 8787
     - Start the bot

Then use the controls\ folder to enable/disable autostart and LAN.

Note: Large model weights (gfpgan\weights\*.pth, piper voices) are local-only.
Copy them from the old PC if those features are needed.