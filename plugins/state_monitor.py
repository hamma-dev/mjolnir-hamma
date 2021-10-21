"""
Plugin to monitor state variables from the charge controller.
"""

# Third party imports
from notifiers.slack import SlackSender

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
        slack_channel=None,
        slack_key_file=None,
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
        slack_channel : str, optional
            If `method=='slack'`, then this is the channel in Slack to post notifications.
        slack_key_file : str or pathlib.Path, optional
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
        self.low_space = low_space
        self.sender = None  # Make sure we "initialize" the attribute
        if method == 'slack':
            try:
                self.sender = SlackSender(  # todo how to import?
                    slack_key_file, slack_channel, logger=self.logger)
            except FileNotFoundError as e:
                self.logger.error(
                    "%e initializing Slack sender: %s Is the Slack key file in the right place?",
                    type(e).__name__, e)
                self.logger.info("Error details:", exc_info=True)
            except Exception as e:  # if anything goes wrong, don't set the sender class
                self.logger.error("Unexpected %s initializing SlackSender: %s", type(e).__name__, e)
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

        def now_then(key):
            # Simple little helper to extract current and previous value for
            # a single attribute. Variable input_data shadows outer scope
            return input_data[key].value, self._previous_data[key].value

        try:
            # Go through several state variables.
            # If something is hinky, log it and send a message

            # Power drops
            load_now, load_pre = now_then('adc_vl_f')
            curr_now, curr_pre = now_then('adc_il_f')

            power_now, power_pre = load_now * curr_now, load_pre * curr_pre
            if (power_now < self.power_delim) and (power_pre > self.power_delim):
                msg = f"Power has dropped from {power_pre:.2f} to {power_now:.2f}."
                self.logger.info(msg)
                self.send_message(msg)

            # Sensor communication
            comm_now, comm_pre = now_then('ping')
            # If the ping !=0, then we can't reach the sensor
            if comm_now and not comm_pre:
                msg = "No communication with sensor!"
                self.logger.info(msg)
                self.send_message(msg)

            # Remaining triggers
            space_now, space_pre = now_then('bytes_remaining')
            if (space_now < self.low_space) and (space_pre > self.low_space):
                msg = f"Remaining GB on drive is {space_now:.1f}"
                self.logger.info(msg)
                self.send_message(msg)

            # todo: Low voltage
            # CRITICAL_VOLTAGE = input_data['v_lvd'].value + 0.1
            # batt_now, batt_pre = now_then('adc_vb_f')
            # if (batt_now <= CRITICAL_VOLTAGE) and (batt_pre > CRITICAL_VOLTAGE):
            #     msg = f"Battery voltage critically low {batt_now:.3f}"
            #     self.logger.info(msg)
            #     self.send_message(msg)

            # LED state
            # todo detect this more reliably by checking array_fault and load_fault bitfields are non-zero,
            state_now, state_pre = now_then('led_state')
            if (state_now >= 12) and (state_now != state_pre):
                msg = f"Critical failure with charge controller. LED state: {state_now}."
                self.logger.info(msg)
                self.send_message(msg)

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

        finally:
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

        if self.sender is not None:
            self.sender.send(msg)

    @staticmethod
    def construct_message(msg):
        """Construct the message detailing the state notification."""
        from brokkr.config.unit import UNIT_CONFIG
        from brokkr.config.metadata import METADATA

        sensor_name = f"{METADATA['name']}{UNIT_CONFIG['number']:02d}"

        header = f"Message from sensor {sensor_name}"

        msg = '\n'.join([header, msg])

        return msg
