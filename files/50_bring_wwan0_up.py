#!/usr/bin/env python3

import os
import subprocess
import time
import sys

if os.environ["IFACE"] != "wwan0":
    sys.exit()
print("Detecting wwan0 going up, sleeping for 60s...")
time.sleep(60)
print("Connecting to wwan0...")
modem_list = subprocess.run(["mmcli", "-L"], check=True, encoding="utf-8", stdout=subprocess.PIPE)
modem_number = modem_list.stdout.strip().split(" ")[0].split("/")[-1]
subprocess.run(["mmcli", "-m", modem_number, "--enable"], check=True)
subprocess.run(["mmcli", "-m", modem_number, "--simple-connect=apn=h2g2"], check=True)
subprocess.run(["ip", "link", "set", "wwan0", "down"], check=True)
with open("/sys/class/net/wwan0/qmi/raw_ip", "w") as f:
	subprocess.run(["echo", "Y"], stdout=f, check=True)
subprocess.run(["ip", "link", "set", "wwan0", "up"], check=True)
subprocess.run(["udhcpc", "-i", "wwan0"], check=True)

print("Connected to wwan0")
sys.exit()
