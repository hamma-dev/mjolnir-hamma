"""
Plugin to monitor state variables from the charge controller.
"""

# Standard library imports
from pathlib import Path
import urllib.request  # for SlackSender
import urllib.parse  # for SlackSender
from configparser import ConfigParser  # for SlackSender

# Local imports
import brokkr.pipeline.base
import brokkr.pipeline.decode


class StateMonitor(brokkr.pipeline.base.OutputStep):
    """ Handle notifications for changes in state variables. """

    def __init__(self, method=None, power_delim=1,
                 slack_channel=None,
                 slack_key_file=None,
                 **output_step_kwargs):
        """

        Parameters
        ----------
        method : str
            The method of how we're going to send notifications. If `None`, then
            we'll only log them.
        power_delim : numeric
            The delimiter between normal power and low power. If power falls below this
            value, it is considered low and a notification will be generated.
        slack_channel : str
            If `method=='slack'`, then this is the channel in Slack to post notifications.
        slack_key_file : str
            If `method=='slack'`, then the path to the file that contains the Slack key
        output_step_kwargs : **kwargs, optional
            Keyword arguments to pass to the OutputStep constructor.

        Returns
        -------
        None.
        """
        # Pass arguments to superclass init
        super().__init__(**output_step_kwargs)

        # Setup simpleeval parser and class initial state
        self._previous_data = None
        self.power_delim = power_delim
        self.method_cls = None  # Make sure we "initialize" the attribute
        if method == 'slack':
            try:
                self.method_cls = SlackSender(slack_key_file, slack_channel,
                                              logger=self.logger)
                # todo: pass in logger instance or use logging.getLogger(__name__)?
            except FileNotFoundError:
                self.logger.error("File not found error. Is the Slack key file in right place?")
            except Exception as exp:  # if anything goes wrong, don't set the sender class
                self.logger.error(f"Unknown error initializing SlackSender: {type(exp)}")

    def execute(self, input_data=None):
        """
        Executing an action upon detection an arbitrary condition in the data.

        Parameters
        ----------
        input_data : any, optional
            Per iteration input data passed to this function from previous
            PipelineSteps. Not used here but retained for compatibility with
            the generalized PipelineStep API. The default is None.

        Returns
        -------
        input_data : same as `input_data`
            Input data passed through unchanged, for further steps to consume.
        """

        # Handle first iteration
        if self._previous_data is None:
            self._previous_data = input_data

        def now_then(key):
            # Simple little helper to extract current and previous value for
            # a single attribute. Variable input_data shadows outer scope
            return input_data[key].value, self._previous_data[key].value

        try:
            # Go through several state variables.
            # If something is hinky, log it and send a message

            # # Power drops
            # load_now, load_pre = now_then('adc_vl_f')
            # curr_now, curr_pre = now_then('adc_il_f')
            #
            # power_now, power_pre = [load_now*curr_now, load_pre*curr_pre]
            power_now = 10; power_pre = 25
            if (power_now < self.power_delim) and (power_pre > self.power_delim):
                msg = f"Power has dropped from {power_pre:.2f} to {power_now:.2f}."
                self.logger.info(msg)
                self.send_message(msg)

            # # Sensor communication
            # comm_now, comm_pre = now_then('ping')
            # # If the ping !=0, then we can't reach the sensor
            # if (comm_now != 0) and (comm_pre == 0):
            #     msg = "No communication with sensor!"
            #     self.logger.info(msg)
            #     self.send_message(msg)
            #
            # # todo: Remaining triggers
            # # input_data['bytes_remaining']
            #
            # # todo: Low voltage
            # # CRITICAL_VOLTAGE = input_data['v_lvd'].value + 0.1
            # # batt_now, batt_pre = now_then('adc_vb_f')
            # # if (batt_now <= CRITICAL_VOLTAGE) and (batt_pre > CRITICAL_VOLTAGE):
            # #     msg = f"Battery voltage critically low {batt_now:.3f}"
            # #     self.logger.info(msg)
            # #     self.send_message(msg)
            #
            # # LED state
            # # todo detect this more reliably by checking array_fault and load_fault bitfields are non-zero,
            # state_now, state_pre = now_then('led_state')
            # if (state_now >= 12) and (state_now != state_pre):
            #     msg = f"Critical failure with charge controller. LED state: {state_now}."
            #     self.logger.info(msg)
            #     self.send_message(msg)

        # If expression evaluation fails, presumably due to bad data values
        except Exception as e:
            self.logger.error(
                "%s evaluating in %s on step %s: %s",
                type(e).__name__, type(self), self.name, e)
            self.logger.info("Error details:", exc_info=True)
            for pretty_name, data in [("Current", input_data),
                                      ("Previous", self._previous_data)]:
                self.logger.info(
                    "%s data: %r", pretty_name,
                    {key: str(value) for key, value in data.items()})

        # Update state for next pass through the pipeline
        self._previous_data = input_data

        # Pass through the input for consumption by any further steps
        return input_data

    def send_message(self, msg):
        """
        Send a message via the given method.

        This is a shepherd method. Given a generic message, we'll send it
        using the method specified when initializing the class. Before sending, we'll
        add in which sensor is sending the message.

        Note: Any "sender class" must have a send method.

        Parameters
        ----------
        msg : str
            The message to be sent.

        """
        msg = self.construct_message(msg)

        if self.method_cls is not None:
            self.method_cls.send(msg)

    @classmethod
    def construct_message(cls, msg):
        """ Construct the message detailing the state notification. """
        from brokkr.config.unit import UNIT_CONFIG
        from brokkr.config.metadata import METADATA

        sensor_name = f"{METADATA['name']}{UNIT_CONFIG['number']:02d}"

        header = f"Message from sensor {sensor_name}"

        msg = '\n'.join([header, msg])

        return msg


class SlackSender:
    def __init__(self, key_file, channel, logger=None):
        """ 
        Handle sending messages in Slack. 
        
        Parameters
        ----------
        key_file : str
            The path to the file that contains the Slack key for the
            workspace to send the message.
        channel : str
            The channel to send the message to. It should be defined in file
            provided by `key_file`.
        logger : Python logger
            If not None, if message send fails, log it here.

        """

        self.logger = logger

        slack_key_file = Path(key_file).expanduser()

        if not slack_key_file.exists():
            raise FileNotFoundError

        config = ConfigParser()
        config.read(slack_key_file)

        # This should be constant...
        BASE_URL = 'https://hooks.slack.com/services/'

        # Define the url, including the channel....
        slack_url = urllib.parse.urljoin(BASE_URL, config['channel'][channel])

        self.request = urllib.request.Request(url=str(slack_url),
                                              headers={'Content-type': 'application/json'},
                                              method='POST')

    def send(self, msg):
        """
        Send a Slack message.

        If the message sending fails and there is a logger associated with
        the class, log it. Otherwise, raise a `RuntimeWarning`.
        """

        self.request.data = f'{{"text": "{msg}"}}'.encode('utf-8')

        resp = urllib.request.urlopen(self.request)

        if resp.status != 200:
            warning_msg = f"'Sending to slack failed. Message: {msg}"

            if self.logger:
                self.logger.warning(warning_msg)
            else:
                raise RuntimeWarning(warning_msg)
