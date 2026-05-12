#!/usr/bin/env python
# DEPRECATED: This script is scheduled for deprecation.
# - HAM-78: Track connection_status.py in mjolnir-hamma
# - HAM-79: Re-evaluate kill switch now that server keepalives are enabled
# No functional changes are planned until HAM-79 is resolved.

#### CHANGE SCRIPT
#### RENABLE REBOOT

import time
import os
import socket
import logging
import datetime

HOSTNAME = 'www.hamma.dev'
LOG_FILE = '/home/pi/connection.log'


def test_conn():
    try:
        with socket.create_connection(('www.hamma.dev', 80)) as sock:
            return True
    except Exception as e:
        # If anything goes wrong, assume we can't connect
        return False


if __name__ == '__main__':

    # Set up a logger
    file_handle = logging.FileHandler(LOG_FILE)

    logger = logging.getLogger("connection_status")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handle)

    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    # First check
    is_up = test_conn()

    if not is_up:
        time.sleep(60)
        is_up = test_conn()  # check again
    else:
        logger.info(f'{now} Pi is up')

    if not is_up:
        logger.error(f'{now} Rebooting')

        os.system('sudo reboot')
