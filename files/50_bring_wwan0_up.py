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
subprocess.run(["mmcli", "-m", modem_number, "--simple-connect=apn=h2g2"], check=True)
print("Connected to wwan0")
sys.exit()
