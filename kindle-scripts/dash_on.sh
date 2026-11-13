export PATH="$PATH:/sbin:/usr/sbin:/mnt/us/usbnet/bin"
lipc-set-prop com.lab126.pillow disableEnablePillow disable; echo "pillow disable rc=$?"
stop framework >/dev/null 2>&1; status framework
sleep 1
killall -STOP awesome; echo "awesome SIGSTOP rc=$?"
lipc-set-prop com.lab126.powerd preventScreenSaver 1
sleep 1
fbink -q -c -f -g file=/tmp/test.png && echo DRAWN
