"""
Plugin to monitor state variables from the charge controller.
"""

from math import nan

# Third party imports
from notifiers.slack import SlackSender
from notifiers.google_chat import GoogleChatSender

# Local imports
import brokkr.pipeline.base
import brokkr.pipeline.decode


class StateMonitor(brokkr.pipeline.base.OutputStep):
    """Handle notifications for changes in state variables."""

    def __init__(
        self,
        method=None,
        power_delim=1,
        low_space=100,
        ping_max=3,
        channel=None,
        key_file=None,
        **output_step_kwargs,
        ):
        """
        Handle notifications for changes in state variables.

        Parameters
        ----------
        method : str
            The method of how we're going to send notifications. If `None`, then
            we'll only log them.
        power_delim : numeric, optional
            The delimiter between normal power and low power.
            If power falls below this value, it is considered low
            and a notification will be generated.
        low_space : numeric, optional
            If the number of gigabytes remaining falls below this threshold, generate
            a notification.
        ping_max : int, optional
            The maximum number of consecutive ping errors before we send an error message
            via `method`. Any ping errors are still logged locally.
        channel : str, optional
            The chat channel in which to post notifications.
        key_file : str or pathlib.Path, optional
            The path to the file that contains the secret/webhook key for the given `method`.
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
        self.low_space = low_space
        self.ping_max = ping_max
        self.bad_ping = 0  # Track the number of bad pings
        self.sender = None  # Make sure we "initialize" the attribute

        sender_class = {"slack": SlackSender, "gchat": GoogleChatSender}[method]
        try:
            self.sender = sender_class(key_file, channel=channel, logger=self.logger)
        except FileNotFoundError as e:
            self.logger.error(
                "%s initializing %s: %s\nIs the key file in the right place?",
                type(e).__name__, sender_class.__name__, e)
            self.logger.info("Error details:", exc_info=True)
        except Exception as e:  # if anything goes wrong, don't set the sender class
            self.logger.error(
                "Unexpected %s initializing %s: %s", type(e).__name__, e, sender_class.__name__)
            self.logger.info("Error details:", exc_info=True)

    def execute(self, input_data=None):
        """
        Execute an action upon detection an arbitrary condition in the data.

        Parameters
        ----------
        input_data : Mapping[str, DataValue], optional
            Per iteration input data passed to this function from previous
            PipelineSteps. Used to extract the data values to report.
            The default is None.

        Returns
        -------
        input_data : same as `input_data`
            Input data passed through unchanged, for further steps to consume.
        """

        # Handle first iteration
        if self._previous_data is None:
            self._previous_data = input_data

        # Go through several state variables.
        # If something is hinky, log it and send a message
        self.run_checks(input_data)

        # todo detect this more reliably by checking array_fault and load_fault bitfields are non-zero,

        # Update state for next pass through the pipeline
        self._previous_data = input_data

        # Pass through the input for consumption by any further steps
        return input_data

    def now_then(self, input_data, key):
        """
        Simple method to extract the current (now) value and previous (then)
        value for the data structure passed around.

        Useful for getting any value from `input_data`, since this we turn
        to sting 'NA's to numeric NaNs.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.
        key : str
            The key of `input_data` you want the value of.

        Returns
        -------
        now_then_values : tuple
            Two element tuple of the (now value, then value)

        """

        now_val = input_data[key].value
        then_val = self._previous_data[key].value

        if now_val == 'NA':
            now_val = nan

        if then_val == 'NA':
            then_val = nan

        return now_val, then_val

    def log_error(self, input_data, exception_inst):
        """
        Log an error.

        Use this when catching Exceptions, especially in the methods of the class.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        exception_inst : Exception
            The exception you wish to log.

        """

        self.logger.error(
            "%s evaluating in %s on step %s: %s",
            type(exception_inst).__name__, type(self), self.name, exception_inst)
        self.logger.info("Error details:", exc_info=True)
        for pretty_name, data in [("Current", input_data),
                                  ("Previous", self._previous_data)]:
            self.logger.info(
                "%s data: %r", pretty_name,
                {key: str(value) for key, value in data.items()})


    def run_checks(self, input_data):
        """
        Run the monitoring checks and log/send messages for the results.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        """
        for check_fn in [
                self.check_power,
                self.check_ping,
                self.check_sensor_drive,
                self.check_battery_voltage,
                ]:
            try:
                msg = check_fn(input_data)
                if msg:
                    self.logger.info(msg)
                    self.send_message(msg)
            except Exception as e:
                self.log_error(input_data, e)

    def check_power(self, input_data):
        """
        Check the power of the sensor.

        This will check to see of the power load of a sensor drops below
        the value given by the class attribute `power_delim`.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Message to send depending on the check, or None if no message.

        """
        load_now, load_pre = self.now_then(input_data, 'adc_vl_f')
        curr_now, curr_pre = self.now_then(input_data, 'adc_il_f')

        power_now, power_pre = load_now * curr_now, load_pre * curr_pre
        if (power_now < self.power_delim) and (power_pre > self.power_delim):
            return f"Power has dropped from {power_pre:.2f} to {power_now:.2f}."
        return None

    def check_ping(self, input_data):
        """
        Check the ping status of the sensor.

        This will check to see if can communicate with a sensor.
        If we can't an error is logged. If we can't communicate several
        consecutive times, given by the class attribute `bad_ping`, send a message.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Message to send depending on the check, or None if no message.

        """
        no_comm_now, no_comm_pre = self.now_then(input_data, 'ping')
        # If the ping !=0, then we can't reach the sensor
        if no_comm_now:
            # If any bad ping, increment the counter.
            self.bad_ping += 1
            if not no_comm_pre:
                # If we pinged fine before, but not now, log it.
                self.logger.info("Sensor unable to be pinged!")
            # Once we reach the critical value, send an alert and log it.
            if self.bad_ping == self.ping_max:
                return f"No communication with sensor (consecutive bad pings: {self.bad_ping})"
        else:  # if we can communicate now, reset the counter
            self.bad_ping = 0
        return None

    def check_sensor_drive(self, input_data):
        """
        Check the remaining space on sensor.

        This will check to see how much space is remaining on a sensor USB drive.
        If it falls below the value given by the class attribute `low_space`,
        send a message.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Message to send depending on the check, or None if no message.

        """
        space_now, space_pre = self.now_then(input_data, 'bytes_remaining')
        if (space_now < self.low_space) and (space_pre > self.low_space):
            return f"Remaining GB on drive is {space_now:.1f}"
        return None

    def check_battery_voltage(self, input_data):
        """
        Check the battery voltage.

        This will check to see if the battery voltage falls below a critical value.
        Right now, this is hardwired to be 0.5 V above the low voltage disconnect
        defined by the charge controller. If it falls below this value,
        send a message.

        Parameters
        ----------
        input_data : Mapping[str, DataValue]
            Same as argument of `execute`.

        Returns
        -------
        str | None
            Message to send depending on the check, or None if no message.

        """
        low_voltage_val, _ = self.now_then(input_data, 'v_lvd')
        CRITICAL_VOLTAGE = low_voltage_val + 0.5

        batt_now, batt_pre = self.now_then(input_data, 'adc_vb_f')
        if (batt_now <= CRITICAL_VOLTAGE) and (batt_pre > CRITICAL_VOLTAGE):
            return f"Battery voltage critically low ({batt_now:.3f} V)"
        return None

    def send_message(self, msg):
        """
        Send a message via the method defined the class attribute `method`.

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

        if self.sender is not None:
            self.sender.send(msg)

    @staticmethod
    def construct_message(msg):
        """Construct the message detailing the state notification."""
        from brokkr.config.unit import UNIT_CONFIG
        from brokkr.config.metadata import METADATA

        sensor_name = f"{METADATA['name']}{UNIT_CONFIG['number']:02d}"

        header = f"Message from sensor {sensor_name} at {UNIT_CONFIG['site_description']}"

        msg = '\n'.join([header, msg])

        return msg
