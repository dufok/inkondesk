export PATH="$PATH:/sbin:/usr/sbin:/mnt/us/usbnet/bin"
killall -CONT awesome; echo "awesome SIGCONT rc=$?"
start framework >/dev/null 2>&1; status framework
sleep 5
lipc-set-prop com.lab126.pillow disableEnablePillow enable; echo "pillow enable rc=$?"
