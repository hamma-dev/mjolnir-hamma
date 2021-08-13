#!/usr/bin/env python3

"""
Send messages to Slack (hsvltg.slack.com)
"""

import argparse
from pathlib import Path
import urllib.request
import urllib.parse
from configparser import ConfigParser
import platform

SLACK_KEY_FILE = Path('~/.slack').expanduser()

config = ConfigParser()
config.read(SLACK_KEY_FILE)

# Build the URL used to send the message
base_url = 'https://hooks.slack.com/services/'

# We're going to send it to the testing channel....
slack_url = urllib.parse.urljoin(base_url, config['channel']['testing'])

# Set up the request
req = urllib.request.Request(url=str(slack_url),
                             headers={'Content-type': 'application/json'}, 
                             method='POST')


def send_message(message=''):
    """ Send a generic message """
        
    # req shadows outer scope...
    req.data = '{{"text": "{0}"}}'.format(message).encode('utf-8')
    
    with urllib.request.urlopen(req) as f:
        pass
    
    if f.reason != 'OK':
        print('bad send')


def construct_message(args):
    """ 
    Construct a message from the arguments.
    
    Mostly intended to be called from main, but if called directly, `args`
    is a Namespace that at least has an attribute sensor.
    
    """
    
    msg = 'Message from ' + args.sensor + ':'
    
    if hasattr(args, 'offline') and args.offline:
        msg = '\n'.join([msg, '************* OFFLINE *************'])
    
    if hasattr(args, 'message'):
        msg = '\n'.join([msg, 'Status: ' + args.message])
    
    if hasattr(args, 'battery'):
        msg = '\n'.join([msg, 'Battery Voltage: ' + args.battery])
    
    return msg


def main():
    """
    Parse the arguments and send a message to Slack
    """
    arg_parser = argparse.ArgumentParser(description="Message slack")
    
    arg_parser.add_argument("-m", "--message", 
                            help="Custom message to send",
                            default=argparse.SUPPRESS)
    
    arg_parser.add_argument("--hello", 
                            help="Testing only",
                            dest='message', 
                            action='store_const', const='Hello World', 
                            default=argparse.SUPPRESS)
    
    arg_parser.add_argument('--battery', 
                            help="Send arbitrary battery message", 
                            dest='battery', 
                            action='store', 
                            default=argparse.SUPPRESS)
    
    arg_parser.add_argument('--lowbattery', 
                            help="Send low battery message", 
                            dest='battery', 
                            action='store', 
                            nargs='?', 
                            const='low',
                            default=argparse.SUPPRESS)
    
    arg_parser.add_argument('--sensor', 
                            help='The sensor that is sending the message', 
                            dest='sensor', 
                            action='store', 
                            default=platform.uname().node)  # We need at least this.
    
    arg_parser.add_argument('--offline', 
                            help='Send sensor offline message', 
                            action='store_true', 
                            default=argparse.SUPPRESS)
        
    parsed_args = arg_parser.parse_args()
    
    msg = construct_message(parsed_args)

    send_message(msg)


if __name__ == '__main__':
    main()
