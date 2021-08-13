"""
Plugin to monitor state varaibles from the charge controller.
"""

# Standard library imports
import subprocess
import sys
from pathlib import Path

# Local imports
import brokkr.pipeline.base
import brokkr.pipeline.decode

# Provide the script that will be called to send the messages about the states
SCRIPT = Path("~/dev/mjolnir-hamma/scripts/status_bot.py").expanduser()
POWER_DELIM = 20  # Delimiter between normal power and low power


class StateNotifier(brokkr.pipeline.base.OutputStep):
    """Demo a Mjolnir output plugin, executing an action on a condition."""

    def __init__(self, **output_step_kwargs):
        """
        Demo a Mjolnir output plugin, executing an action on a condition.
        Parameters
        ----------
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

    def execute(self, input_data=None):
        """
        Execute an action upon detection an arbitrary condition in the data.
        Parameters
        ----------
        input_data : any, optional
            Per iteration input data passed to this function from previous
            PipelineSteps. Not used here but retained for compatibility with
            the generalized PipelineStep API. The default is None.
        Returns
        -------
        input_data : same as input_data
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
            # Go through several states.
            # If something is hinky, log it and send a message

            # Power drops            
            load_now, load_pre = now_then('adc_vl_f')
            curr_now, curr_pre = now_then('adc_il_f')
            
            power_now, power_pre = (load_now * curr_now, load_pre * curr_pre)

            if (power_now < POWER_DELIM) and (power_pre > POWER_DELIM):
                msg = f"Power has dropped from {power_pre:.2f} to {power_now:.2f}."
                self.logger.info(msg)
                subprocess.call([SCRIPT, "-m " + msg])

            # Sensor communication
            comm_now, comm_pre = now_then('ping')
            # If the ping !=0, then we can't reach the sensor
            if (comm_now != 0) and (comm_pre == 0):
                msg = "No communication with sensor!"            
                self.logger.info(msg)
                subprocess.call([SCRIPT, "-m " + msg])

            # todo: Remaining triggers
            # input_data['bytes_remaining']

            # todo: Low voltage
            # CRITICAL_VOLTAGE = input_data['v_lvd'].value + 0.1
            # batt_now, batt_pre = now_then('adc_vb_f')
            # if (batt_now <= CRITICAL_VOLTAGE) and (batt_pre > CRITICAL_VOLTAGE):
            #     msg = f"Battery voltage critically low {batt_now:.3f}"
            #     self.logger.info(msg)
            #     subprocess.call([SCRIPT, "-m " + msg])

            # LED state
            state_now, state_pre = now_then('led_state')
            if (state_now >= 12) and (state_now != state_pre):
                msg = f"Critical failure with charge controller. LED state: {state_now}."
                self.logger.info(msg)
                subprocess.call([SCRIPT, "-m " + msg])

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
                
            # todo: also a status bot error message?
            # subprocess.call([SCRIPT, "-m " + "Unknown failure in state monitor"])

        # Update state for next pass through the pipeline
        self._previous_data = input_data

        # Passthrough the input for consumption by any further steps
        return input_data
