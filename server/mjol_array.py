#!/usr/bin/env python

import subprocess
import argparse
from pathlib import Path

import pandas as pd
import numpy as np

# Define constants that hold the "mjolnir numbers" for each array.
# We should only ever need to pass this into the class,
# so having it as a global constant is overkill.
# But, it gives us a place up top to change/add if necessary.
HAMMA_SENSORS = list(range(1, 10))
PAMMA_SENSORS = [50, 51, 52, 53, 54, 56]
AUMMA_SENSORS = [41, 42, 43, ]


class MjolnirArray():

    def __init__(self, sensors, sensor_name='Mjolnir'):
        # sensors should be numeric, and correspond to Mjolnir hostname number.

        self.sensors = sensors
        self.sensor_name = sensor_name


    @staticmethod
    def _pi_ssh_cmd(port):
        # We build a ssh command to the pi's in several places.
        # The command is usually passed to subprocess.
        # port is a numeric, fully qualified
        # returns list

        cmd = ['ssh', '-o', 'ConnectTimeout=5', 'pi@localhost', '-p', str(port)]
        return cmd

    @staticmethod
    def status(port):
        # Determine the status of a Pi
        # port is fully qualified integer
        # Return a boolean if the Pi is up (True) or down (False)

        cmd = ['nc', '-w', '1', 'localhost', str(port)]

        out = subprocess.run(cmd, stdout=subprocess.DEVNULL, timeout=5, stderr=subprocess.DEVNULL)
        nc_code = out.returncode

        return not nc_code

    @staticmethod
    def status_services(port):
        # port is fully qualified
        services = ['brokkr-hamma-default', 'sindri-hamma-client']
        base_cmd = ['systemctl', 'is-active', '--quiet', ]

        cmd = MjolnirArray._pi_ssh_cmd(port) + base_cmd

        ret = list()
        for _s in services:
            out = subprocess.run(cmd + [_s], stdout=subprocess.PIPE)
            ret.append(not out.returncode)

        return ret

    @staticmethod
    def status_latest_trigger(port):
        # port is fully qualified

        import ast

        cmd = MjolnirArray._pi_ssh_cmd(port)
        cmd = cmd + ['/home/pi/dev/mjolnir-hamma/scripts/latest_trigger.py']

        try:
            out = subprocess.run(cmd, stdout=subprocess.PIPE, universal_newlines=True)
            if out.returncode:
                raise Exception
            ret_val = ast.literal_eval(out.stdout)
            ret_val['time'] = np.datetime64(int(ret_val['time']), 's')
        except Exception:
            ret_val = dict()
            ret_val['threshold'] = np.nan
            ret_val['num_sat'] = np.nan
            ret_val['time'] = np.nan

        return ret_val

    @staticmethod
    def updown(port, bring_up, quiet=False):
        # Here port is fully qualified

        # First, make sure Pi is up...
        is_pi_up = MjolnirArray.status(port)

        if not is_pi_up:
            if not quiet:
                print(f"Pi on port {port} is down. Sensor not changed.")
            return

        flag = "--on" if bring_up else "--off"

        cmd = MjolnirArray._pi_ssh_cmd(port)
        cmd = cmd + ['/home/pi/dev/mjolnir-hamma/scripts/sensors.py', flag]

        try:
            out = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=120)
        except subprocess.TimeoutExpired:
            if not quiet:
                print(f"Timeout: sensors.py on port {port} did not complete in 120s.")
        except Exception as e:
            if not quiet:
                print(f"Error running sensors.py on port {port}: {e}")

    @staticmethod
    def status_fcm(port):
        # Return a boolean if the FCM sensor is up (True) or down (False)
        # port is fully qualified

        cmd = MjolnirArray._pi_ssh_cmd(port)
        cmd = cmd + ['/home/pi/dev/ltgenv/bin/brokkr', 'status']

        try:
            out = subprocess.run(cmd, stdout=subprocess.PIPE)
            retval = out.stdout.decode()
            retval = retval.split('\n')

            ping_code = next((x for x in retval if 'Ping Retcode' in x))
            ping_code = int(ping_code.split(':')[-1])
        except Exception as e:
            # If anything goes wrong, we'll assume we don't see the sensor
            ping_code = 1

        return not ping_code

    def updown_array(self, bring_up, ports=None):
        # Bring one or more sensors up or down.
        # bring up is boolean
        # ports is mod 10000.
        #    Can be string, but if it is, in needs to be in a list
        #    Even a one element list

        if ports is None:
            ports = [10000 + i for i in self.sensors]
        else:
            # Because sensor nums start at 1, we need to subtract one when indexing
            # local_sensor_nums = [self.sensors[int(p) - 1] for p in ports]
            ports = [10000 + int(p) for p in ports]  # Make this an integer

        for p in ports:
            MjolnirArray.updown(p, bring_up)

    def status_array(self, ports=None, quiet=False):
        # Get status report for a number of sensors in an array
        # ports is a list (even a one element list)

        # todo: break out pi up to a standalone function

        if ports is None:
            local_sensor_nums = self.sensors
            ports = [10000 + i for i in self.sensors]
        else:
            # Because sensor nums start at 1, we need to subtract one when indexing
            # local_sensor_nums = [self.sensors[int(p) - 1] for p in ports]
            local_sensor_nums = [int(p) for p in ports]
            ports = [10000 + int(p) for p in ports]  # Make this an integer

        is_up = list()
        is_sensor_up = list()
        is_brokkr_up = list()
        is_sindri_up = list()
        trig_attr = list()

        for p in ports:
            this_up = MjolnirArray.status(p)

            is_up.append(this_up)

            if this_up:
                this_brokkr_up, this_sindri_up = MjolnirArray.status_services(p)
            else:
                this_brokkr_up = False
                this_sindri_up = False

            # This will populate a dummy set of attrs if not up....
            this_trig_attr = MjolnirArray.status_latest_trigger(p)

            is_brokkr_up.append(this_brokkr_up)
            is_sindri_up.append(this_sindri_up)
            trig_attr.append(this_trig_attr)

            if this_up:
                this_sensor_up = MjolnirArray.status_fcm(p)
            else:
                this_sensor_up = False

            is_sensor_up.append(this_sensor_up)

        if not quiet:
            def _convert_updown(val):
                return 'Up' if val else 'Down'

            zipped = zip(is_up, is_sensor_up, local_sensor_nums, is_brokkr_up, is_sindri_up, trig_attr)

            for up, sensor_up, s, brokkr_up, sindri_up, t_attr in zipped:
                # status_up = 'Up' if up else 'Down'
                # sensor_up = 'Up' if sensor_up else 'Down'
                print(f"{self.sensor_name}{s:02} is {_convert_updown(up):>6}; "
                      f"FCM is {_convert_updown(sensor_up):>6}; "
                      f"Brokkr {_convert_updown(brokkr_up)!s:>6}; "
                      f"Sindri {_convert_updown(sindri_up)!s:>6}; "
                      f"Trig time {t_attr['time']!s:>20}; "
                      f"Num Sat {t_attr['num_sat']!s:>3}; "
                      f"Threshold {t_attr['threshold']!s:>6}; "
                      )
                # print(t_attr)

        return is_up, is_sensor_up, is_brokkr_up, is_sindri_up, trig_attr

    def collect_data(self):
        # TODO: only get some - subset sensor_nums
        # Provide an easy to collect a bunch of data about the array
        hamma_ports = self.sensors

        mjol_up, sensor_up, brokkr_up, sindri_up, trig_attr = self.status_array(ports=hamma_ports, quiet=True)

        # n_sensor = len(sensor_nums)
        # print(trig_attr[0])

        thresh = [t['threshold'] for t in trig_attr]
        sat = [t['num_sat'] for t in trig_attr]
        trig_time = [t['time'] for t in trig_attr]

        v = {'Mjolnir Up': mjol_up,
             'Brokkr Up': brokkr_up,
             'Sindri Up': sindri_up,
             'Sensor Up': sensor_up,
             'Last trigger': trig_time,
             'Num GPS': sat,
             'Threshold': thresh,
             }

        df = pd.DataFrame(v)
        df.index = [self.sensor_name + f"{_i:02}" for _i in hamma_ports]

        # Apply some formatting. Note that df.style requires Jinja2, which is optional dep
        df['Threshold'] = [f"{val:.2f}" for val in df['Threshold']]

        return df


def main():
    arg_parser = argparse.ArgumentParser(description="Bring the array up or down")

    arg_parser.add_argument(
        "-a",
        dest="array",
        help="Which array (e.g., hamma, pamma)",
        default=None

    )

    arg_parser.add_argument("-p",
                            dest="ports",
                            action="append",  # if argparse 3.8, then can use extend
                            help="Which ports to bring up/down, mod 10000"
    )
    # NOTE: PASS multiple -p for multiple ports until argparse -> 3.8

    # grp = arg_parser.add_mutually_exclusive_group(required=True)

    arg_parser.add_argument("--status",
                            help="Get a status report",
                            dest="do_status",
                            action='store_true',
                            default=False,
                            )

    # arg_parser.add_argument("--trig",
    #                  help="Trig statsus",
    #                  dest="do_trig",
    #                  action='store_true',
    #                  default=False,
    #                  )

    grp = arg_parser.add_mutually_exclusive_group()
    grp.add_argument("--up",
                     dest='bring_up',
                     action="store_true",
                     default=False,
                     help="Bring array up",
                     )

    grp.add_argument("--down",
                     dest='bring_down',
                     action="store_true",
                     default=False,
                     help="Bring array down",
                     )

    parsed_args = arg_parser.parse_args()

    if parsed_args.ports is None:
        if parsed_args.array == 'hamma':
            mj_array = MjolnirArray(sensors=HAMMA_SENSORS)
        elif parsed_args.array == 'pamma':
            mj_array = MjolnirArray(sensors=PAMMA_SENSORS)
        elif parsed_args.array == 'aumma':
            mj_array = MjolnirArray(sensors=AUMMA_SENSORS)
        elif parsed_args.array is None:
            print('You must pass either the sensors/ports or the array')
            return
        else:
            print('Invalid array name.')
            return
    else:
        mj_array = MjolnirArray(sensors=parsed_args.ports)

    if parsed_args.do_status:
        _ = mj_array.status_array(ports=parsed_args.ports)
    # elif parsed_args.do_trig:
    #     _ = status_latest_trigger(port=parsed_args.ports)
    elif parsed_args.bring_up | parsed_args.bring_down:
        # Is it up or down?
        # bring_up = parsed_args.bring_up

        mj_array.updown_array(parsed_args.bring_up, ports=parsed_args.ports)
    else:
        pass


if __name__ == '__main__':
    main()
