#!/usr/bin/env bash
# Opens the mirror page full-screen on the hidden TV.
# Add to the desktop session's autostart on the brain computer.

URL="http://localhost:8080/mirror"

# wait for the show controller's web server
until curl -s -o /dev/null "$URL"; do sleep 1; done

# never blank or sleep the screen (X11; on Wayland disable blanking in settings)
xset s off 2>/dev/null; xset -dpms 2>/dev/null; xset s noblank 2>/dev/null
# hide the mouse cursor (sudo apt install unclutter)
command -v unclutter >/dev/null && unclutter -idle 0 &

BROWSER=$(command -v chromium || command -v chromium-browser || command -v google-chrome)
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --autoplay-policy=no-user-gesture-required --check-for-update-interval=31536000 \
  --incognito "$URL"
