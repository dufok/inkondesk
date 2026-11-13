#!/bin/sh
# [kindle-dash] watchdog, runs on the Kindle (/mnt/us/kindle-dash/watchdog.sh).
# The PC program touches /tmp/kd_heartbeat every ~20 s. If no heartbeat arrives for
# ~60 s (PC off / asleep / program stopped), show the screensaver and switch the light off.
# The PC program sees /tmp/kd_screensaver_on when it comes back and redraws the key map.
export PATH="$PATH:/sbin:/usr/sbin:/mnt/us/usbnet/bin"
DIR=/mnt/us/kindle-dash
HB=/tmp/kd_heartbeat
SS=/tmp/kd_screensaver_on
FL=/sys/devices/system/fl_tps6116x/fl_tps6116x0/fl_intensity
LIMIT=12        # 12 x 5 s = 60 s without a heartbeat
miss=0
while true; do
    if [ -f $HB ]; then
        rm -f $HB
        miss=0
    else
        miss=$((miss + 1))
    fi
    if [ $miss -ge $LIMIT ] && [ ! -f $SS ]; then
        touch $SS
        lipc-set-prop com.lab126.powerd flIntensity 0
        echo 0 > $FL
        fbink -q -c -f -g file=$DIR/screensaver.png
    fi
    sleep 5
done
