#!/bin/bash
if ! ping -c1 -W3 1.1.1.1 &>/dev/null; then
    logger "wifi_watchdog: no connectivity, restarting NetworkManager"
    systemctl restart NetworkManager
    sleep 10
fi
