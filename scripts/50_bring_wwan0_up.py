#!/usr/bin/env python3

import sys
import os
import subprocess
import time
import logging
import logging.handlers
import re

# --- Configuration ---
IFACE = "wwan0"
APN = "vzwinternet"  # Your APN
LOG_TAG = "wwan-connect-all" # Tag for journalctl -t
PING_TARGET = "8.8.8.8"      # Address to ping for connectivity check
PING_TIMEOUT = 5             # Seconds to wait for ping reply
LOW_SIGNAL_THRESHOLD = 5     # Signal quality (%) below which we suspect a zombie state

# --- Set up logging to systemd-journal ---
try:
    logger = logging.getLogger(LOG_TAG)
    logger.setLevel(logging.INFO)
    handler = logging.handlers.SysLogHandler(address='/dev/log')
    formatter = logging.Formatter(f'%(name)s: %(message)s')
    handler.setFormatter(formatter)
    
    if not logger.hasHandlers():
        logger.addHandler(handler)
    
except Exception as e:
    print(f"FATAL: Logging setup failed: {e}", file=sys.stderr)
    raise

# --- Network Check Functions ---

def ping_check():
    """Pings the target address to check for internet connectivity."""
    logger.info(f"Checking connectivity with ping to {PING_TARGET}...")
    try:
        # Use subprocess.run for better error handling and timeout
        # '-c 1': Send only one packet
        # '-W PING_TIMEOUT': Wait PING_TIMEOUT seconds for a reply
        subprocess.run(
            ["ping", "-c", "1", f"-W{PING_TIMEOUT}", PING_TARGET],
            check=True,       # Raises CalledProcessError on non-zero exit code
            capture_output=True # Hides stdout/stderr unless there's an error
        )
        logger.info("Ping successful.")
        return True
    except subprocess.CalledProcessError as e:
        # Ping command returned non-zero (e.g., host unreachable)
        logger.warning(f"Ping failed: {e.stderr.decode().strip() if e.stderr else 'No stderr'}")
        return False
    except subprocess.TimeoutExpired:
        # Ping command timed out
        logger.warning(f"Ping timed out after {PING_TIMEOUT} seconds.")
        return False
    except FileNotFoundError:
        # ping command not found
        logger.error("Ping command not found. Cannot check connectivity.")
        return False # Treat as failure if we can't test
    except Exception as e:
        # Catch any other unexpected errors
        logger.error(f"Error during ping check: {e}")
        return False

def check_ip_address():
    """Checks if the interface already has an IPv4 address."""
    try:
        ip_addr = subprocess.run(
            ["ip", "-4", "addr", "show", IFACE], # -4 ensures we only check IPv4
            check=True, encoding="utf-8", 
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5
        )
        # Check for 'inet ' (with a space) which indicates an IPv4 address
        if "inet " in ip_addr.stdout:
            # Extract the IP address for logging
            ip_line = [line for line in ip_addr.stdout.splitlines() if "inet " in line][0]
            ip = ip_line.strip().split()[1].split('/')[0] # Gets 'X.X.X.X'
            logger.info(f"{IFACE} already has IP address: {ip}")
            return True
        else:
            logger.info(f"{IFACE} does not have an IP address.")
            return False
    except Exception as e:
        logger.error(f"Failed to check IP address: {e}")
        return False # Assume no IP if check fails

# --- Hardware/Modem Functions ---

def check_raw_ip():
    """Checks if the wwan0 interface is in raw_ip mode."""
    try:
        with open(f"/sys/class/net/{IFACE}/qmi/raw_ip", "r") as f:
            val = f.read().strip().upper()
        logger.info(f"raw_ip mode is currently: {val}")
        return val == "Y"
    except FileNotFoundError:
        logger.warning("raw_ip file not found. Assuming 'N'.")
        return False
    except Exception as e:
        logger.error(f"Error checking raw_ip: {e}")
        return False

def set_raw_ip_mode(modem_number):
    """
    Puts the modem in the correct raw_ip mode.
    This requires disabling the modem and taking the interface down.
    """
    logger.warning("raw_ip mode is not set. Starting reconfiguration...")
    try:
        # 1. Disable modem
        logger.info("Disabling modem...")
        subprocess.run(["mmcli", "-m", modem_number, "--disable"], check=True, capture_output=True, timeout=30)
        time.sleep(2) # Give modem time to disable

        # 2. Take interface down
        logger.info(f"Taking {IFACE} down...")
        subprocess.run(["ip", "link", "set", IFACE, "down"], check=True, capture_output=True, timeout=10)
        time.sleep(1)

        # 3. Write 'Y' to raw_ip
        logger.info(f"Setting raw_ip=Y for {IFACE}...")
        with open(f"/sys/class/net/{IFACE}/qmi/raw_ip", "w") as f:
            f.write("Y")
        
        # 4. Bring interface up
        logger.info(f"Bringing {IFACE} up...")
        subprocess.run(["ip", "link", "set", IFACE, "up"], check=True, capture_output=True, timeout=10)
        logger.info("raw_ip mode set successfully.")
        return True

    except Exception as e:
        if isinstance(e, subprocess.CalledProcessError):
            logger.error(f"Failed to set raw_ip mode. CMD: {e.cmd}, RC: {e.returncode}, STDOUT: {e.stdout.decode()}, STDERR: {e.stderr.decode()}")
        else:
            logger.error(f"Failed to set raw_ip mode: {e}")
        return False

def find_modem(retries=15, delay=2):
    """Finds the modem index, retrying if not found."""
    logger.info("Searching for modem...")
    for i in range(retries):
        try:
            modem_list = subprocess.run(
                ["mmcli", "-L"], check=True, encoding="utf-8", 
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
            )

            if not modem_list.stdout:
                raise IndexError("No modems found in mmcli -L output (empty stdout).")

            first_line = modem_list.stdout.strip().splitlines()[0]

            if not first_line.strip().startswith('/org/freedesktop/ModemManager1/Modem/'):
                raise IndexError(f"Unexpected mmcli -L output, probably still scanning: {first_line}")
            
            modem_path = first_line.strip().split(' ')[0]
            modem_number = modem_path.split('/')[-1]
            
            if modem_number.isdigit():
                logger.info(f"Found modem at index: {modem_number}")
                return modem_number
            else:
                raise ValueError(f"Found non-numeric modem index: {modem_number}")

        except (subprocess.CalledProcessError, FileNotFoundError, IndexError, ValueError) as e:
            if "IndexError" in str(e):
                logger.warning(f"ModemManager is running but hasn't found modem yet. Retrying... ({i+1}/{retries}) - Msg: {e}")
            else:
                logger.warning(f"mmcli command failed or not found. Retrying... ({i+1}/{retries}) - Error: {e}")
            
            time.sleep(delay)
            
    logger.error("Failed to find modem after all retries.")
    return None

def get_signal_quality(modem_number):
    """
    Runs 'mmcli -m [num]' and parses the 'signal quality' percentage.
    Returns the quality as an integer (0-100) or -1 if parsing fails.
    """
    try:
        status = subprocess.run(
            ["mmcli", "-m", modem_number],
            check=True, 
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
        )
        
        output_bytes = status.stdout + status.stderr
        output_to_parse = output_bytes.decode('utf-8', errors='ignore')
        
        quality_line = [line for line in output_to_parse.splitlines() if "signal quality:" in line]
        
        if quality_line:
            # Line looks like: |  signal quality: 54% (recent)
            match = re.search(r"(\d+)%", quality_line[0])
            if match:
                quality = int(match.group(1))
                logger.info(f"Parsed signal quality: {quality}%")
                return quality
            else:
                logger.error(f"PARSING FAILED: Could not find percentage in signal line: {quality_line[0]}")
                return -1 # Indicate failure
        else:
            logger.error(f"PARSING FAILED: Could not find 'signal quality:' line in mmcli output.")
            return -1 # Indicate failure

    except Exception as e:
        logger.error(f"Failed to get signal quality: {e}")
        return -1 # Indicate failure

def get_modem_state(modem_number):
    """
    Runs 'mmcli -m [num]' and parses the 'state' line.
    Returns the state as a string (e.g., 'connected', 'registered')
    or None if parsing fails.
    """
    try:
        status = subprocess.run(
            ["mmcli", "-m", modem_number],
            check=True, 
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
        )
        
        output_bytes = status.stdout + status.stderr
        output_to_parse = output_bytes.decode('utf-8', errors='ignore')
        
        state_line = [line for line in output_to_parse.splitlines() if "state:" in line and "power" not in line]
        
        if state_line:
            # Regex finds "state:" followed by spaces, then captures non-spaces
            match = re.search(r"state:\s+(\S+)", state_line[0], re.IGNORECASE)
            
            if match:
                dirty_state = match.group(1).strip("'")
                # Strip ANSI codes
                ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
                current_state = ansi_escape.sub('', dirty_state)
                
                # Log only if different from previous, or first time (to reduce noise)
                # This requires storing previous state - let's skip for now and keep logging
                logger.info(f"Parser result: >{current_state}< (Length: {len(current_state)})")
                return current_state
            else:
                logger.error(f"PARSING FAILED: Could not find state word in line: {state_line[0]}")
                return None
        else:
            # Only log this as warning, sometimes mmcli output is transiently empty
            logger.warning(f"PARSING WARNING: Could not find 'state:' line in mmcli output.")
            return None

    except Exception as e:
        if isinstance(e, subprocess.CalledProcessError):
            logger.error(f"Failed to check modem status. CMD: {e.cmd}, RC: {e.returncode}, STDOUT: {e.stdout.decode()}, STDERR: {e.stderr.decode()}")
        else:
            logger.error(f"Failed to check modem status: {e}")
        return None

def force_disconnect(modem_number):
    """Explicitly disconnects the modem bearer."""
    logger.warning("Forcing modem disconnect...")
    try:
        # Find the bearer path first
        bearer_cmd = subprocess.run(
            ["mmcli", "-m", modem_number],
            check=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
        )
        bearer_path = None
        for line in bearer_cmd.stdout.splitlines():
            if "Bearer/" in line:
                # Line looks like: |   Bearer  | dbus path: /org/.../Bearer/1
                bearer_path = line.split()[-1]
                break
        
        if bearer_path:
            logger.info(f"Found bearer: {bearer_path}. Disconnecting...")
            bearer_index = bearer_path.split('/')[-1]
            subprocess.run(
                ["mmcli", "-b", bearer_index, "--disconnect"],
                check=True, capture_output=True, timeout=30
            )
            logger.info("Bearer disconnected successfully.")
            time.sleep(3) # Give modem time to process disconnect
            return True
        else:
            logger.warning("Could not find an active bearer to disconnect.")
            # If no bearer, maybe it's already disconnected internally? Treat as okay.
            return True 
            
    except Exception as e:
        if isinstance(e, subprocess.CalledProcessError):
            err_str = e.stderr.decode()
            # If bearer doesn't exist, that's okay, it's already disconnected
            if "doesn't exist" in err_str or "GDBus.Error.InvalidArgs" in err_str:
                 logger.warning(f"Bearer disconnect unnecessary or failed harmlessly: {err_str.strip()}")
                 return True
            else:
                logger.error(f"Failed to force disconnect. CMD: {e.cmd}, RC: {e.returncode}, STDOUT: {e.stdout.decode()}, STDERR: {err_str}")
        else:
            logger.error(f"Failed to force disconnect: {e}")
        return False

def connect_modem(modem_number, retries=10, delay=2):
    """Connects the modem and verifies the 'connected' state."""
    
    # --- 1. Enable the modem first ---
    # Moved enabling logic to main() to ensure it happens before state check
    
    # --- 2. Send the simple-connect command ---
    try:
        logger.info(f"Sending simple-connect command with APN: {APN}...")
        subprocess.run(
            ["mmcli", "-m", modem_number, f"--simple-connect=apn={APN}"],
            check=True, capture_output=True, timeout=45
        )
    except Exception as e:
        if isinstance(e, subprocess.CalledProcessError):
            err_str = e.stderr.decode()
            if "TooMany" in err_str or "all existing bearers are connected" in err_str:
                logger.info("Modem reported already connected (TooMany bearers error). Verifying state.")
                # Don't return True here, fall through to verification
            else:
                logger.warning(f"simple-connect command failed (but checking status anyway). CMD: {e.cmd}, RC: {e.returncode}, STDOUT: {e.stdout.decode()}, STDERR: {err_str}")
        else:
            logger.warning(f"simple-connect command failed (but checking status anyway): {e}")


    # --- 3. Verify the "connected" state ---
    logger.info("Verifying connection status...")
    for i in range(retries):
        current_state = get_modem_state(modem_number)
        logger.info(f"Modem state check ({i+1}/{retries}): '{current_state}'")
        
        if current_state == "connected":
            logger.info("Modem is successfully connected.")
            return True
        
        if current_state in ["failed", "disabled"]:
            logger.error(f"Modem entered '{current_state}' state during connection attempt. Aborting.")
            return False
        
        # If state is None, 'registered', 'connecting', 'searching', etc., sleep and retry.
        time.sleep(delay)

    logger.error("Failed to verify 'connected' state after connection attempt.")
    return False

def run_dhcp_client():
    """Runs udhcpc to get an IP address."""
    logger.info(f"Running udhcpc for {IFACE}...")
    try:
        # Give the interface a moment after connection before DHCP
        time.sleep(3) 
        subprocess.run(
            ["udhcpc", "-i", IFACE, "-q", "-s", "/etc/udhcpc/default.script"],
            check=True, capture_output=True, timeout=30
        )
        logger.info("udhcpc completed successfully.")
        return True
    except Exception as e:
        if isinstance(e, subprocess.CalledProcessError):
            logger.error(f"udhcpc failed. CMD: {e.cmd}, RC: {e.returncode}, STDOUT: {e.stdout.decode()}, STDERR: {e.stderr.decode()}")
        else:
            logger.error(f"udhcpc failed to get an IP address: {e}")
        return False

# --- Main Execution Logic ---

def main():
    
    logger.info(f"--- {LOG_TAG} script started ---")
    
    # --- Initial Check: Is connection already working? ---
    if ping_check():
        logger.info("Connectivity OK. Nothing to do.")
        sys.exit(0)
        
    logger.warning("Connectivity check failed. Starting recovery process...")

    # --- Step 1: Find the modem ---
    modem_number = find_modem()
    if not modem_number:
        sys.exit(1) # Error already logged

    # --- Step 2: Ensure raw_ip mode and interface is up ---
    # This must happen before checking state or connecting
    if not check_raw_ip():
        if not set_raw_ip_mode(modem_number):
            sys.exit(1) # Error logged
    else:
        logger.info("raw_ip is 'Y'. Ensuring interface is 'up'.")
        try:
            subprocess.run(["ip", "link", "set", IFACE, "up"], check=True, capture_output=True, timeout=10)
        except Exception as e:
            logger.error(f"Failed to run 'ip link set {IFACE} up': {e}", exc_info=True)
            sys.exit(1)
            
    # --- Step 3: Enable the modem (safe to run even if already enabled) ---
    try:
        logger.info(f"Ensuring modem {modem_number} is enabled...")
        subprocess.run(
            ["mmcli", "-m", modem_number, "--enable"],
            check=True, capture_output=True, timeout=30
        )
    except Exception as e:
        logger.error(f"Failed to ensure modem is enabled: {e}", exc_info=True)
        # Don't exit here, maybe it was already enabled and connect will still work

    # --- Step 4: Check Signal Quality for Zombie State ---
    signal_quality = get_signal_quality(modem_number)
    
    if signal_quality >= 0 and signal_quality <= LOW_SIGNAL_THRESHOLD:
        logger.warning(f"Low signal detected ({signal_quality}%). Potential zombie state. Forcing disconnect.")
        if not force_disconnect(modem_number):
             logger.error("Failed to force disconnect. Attempting connect anyway.")
             # Continue to attempt connection even if disconnect fails
        # After force disconnect, proceed to full connect attempt below

    # --- Step 5: Check current state and act accordingly ---
    current_state = get_modem_state(modem_number)
    logger.info(f"Current modem state: '{current_state}'")

    if current_state == "connected":
        logger.info("Modem state is 'connected'. Checking for IP.")
        # Check IP again, as DHCP might have failed previously
        if not check_ip_address():
            logger.warning(f"{IFACE} is connected but has no IP. Running DHCP.")
            if not run_dhcp_client():
                sys.exit(1)
        else:
            logger.info("Modem is connected and has IP. Re-running ping check just in case.")
            # Final check - if ping still fails despite connected state and IP, something else is wrong
            if not ping_check():
                 logger.error("Modem connected with IP, but ping still fails. External issue suspected.")
                 # Optionally, force a disconnect here too? For now, just log.
                 # force_disconnect(modem_number) 
                 sys.exit(1) # Exit with error if final ping fails

    elif current_state in ["failed", "disabled", "registered", "connecting", "searching", None]:
        # Includes None for cases where state parsing failed transiently
        logger.info("Modem is not connected. Starting full connection process...")
        if not connect_modem(modem_number):
            sys.exit(1) # Error logged by connect_modem()
        
        # Now get IP address
        if not run_dhcp_client():
            sys.exit(1) # Error logged by run_dhcp_client()
            
    else:
        # Should not happen, but catch unexpected states
        logger.error(f"Modem in unexpected state: '{current_state}'. Exiting.")
        sys.exit(1)


    # --- Final Verification ---
    logger.info("Final verification after recovery attempt...")
    if ping_check():
        logger.info(f"--- {LOG_TAG} script finished successfully. ---")
        sys.exit(0)
    else:
        logger.error(f"--- {LOG_TAG} script failed to establish connection. ---")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"--- SCRIPT CRASHED (UNHANDLED EXCEPTION) ---")
        logger.error(f"Exception: {e}", exc_info=True)
        sys.exit(1)


