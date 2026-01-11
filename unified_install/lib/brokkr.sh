#!/bin/bash
# Brokkr installation and configuration for HAMMA Pi
#
# Based on original install_brokkr.sh and setup_brokkr.sh
#
# This script:
#   1. Creates Python virtual environment at /home/pi/dev/ltgenv
#   2. Clones required repositories (brokkr, serviceinstaller, notifiers)
#   3. Installs packages into the venv
#   4. Configures Brokkr for the sensor
#   5. Installs Brokkr systemd services
#
# Requirements:
#   - common.sh must be sourced first
#   - mjolnir-hamma repository already cloned (from bootstrap.sh)
#
# Functions:
#   install_brokkr <sensor_number>
#   configure_brokkr <sensor_number>

# --- Configuration ---
INSTALL_PATH="/home/pi/dev"
VENV_NAME="ltgenv"
VENV_PATH="$INSTALL_PATH/$VENV_NAME"

# --- Install Brokkr ---
# Creates venv, clones repos, installs packages
install_brokkr() {
    local sensor_num="$1"

    if ! validate_sensor_num "$sensor_num"; then
        log_error "Invalid sensor number: $sensor_num"
        return 1
    fi

    local sensor_formatted=$(format_sensor_num "$sensor_num")
    local hostname="mjolnir$sensor_formatted"

    log_step "Installing Brokkr for $hostname..."

    # --- Step 1: Create virtual environment ---
    log_step "[Brokkr 1/4] Creating Python virtual environment..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "python3 -m venv $VENV_PATH (as pi user)"
        log_dry_run "ln -sf $VENV_PATH/bin/activate /home/pi/$VENV_NAME"
        manifest_add "command" "cmd" "python3 -m venv $VENV_PATH" "user" "pi"
        manifest_add "symlink" "target" "$VENV_PATH/bin/activate" "link" "/home/pi/$VENV_NAME"
    else
        if [[ ! -d "$VENV_PATH" ]]; then
            # Run as pi user to ensure correct ownership
            sudo -u pi HOME=/home/pi python3 -m venv "$VENV_PATH"
            sudo -u pi HOME=/home/pi ln -sf "$VENV_PATH/bin/activate" "/home/pi/$VENV_NAME"
            log_success "Created $VENV_PATH"
        else
            log_warn "Virtual environment already exists"
        fi
    fi

    # --- Step 2: Upgrade pip and setuptools ---
    log_step "[Brokkr 2/4] Upgrading pip and setuptools..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "pip install --upgrade pip setuptools wheel (as pi user)"
        manifest_add "pip_install" "package" "pip setuptools wheel" "upgrade" "true" "user" "pi"
    else
        # Run as pi user to ensure correct ownership
        sudo -u pi HOME=/home/pi bash -c "source '$VENV_PATH/bin/activate' && pip install --upgrade pip setuptools wheel"
        log_success "pip and setuptools upgraded"
    fi

    # --- Step 3: Clone repositories ---
    log_step "[Brokkr 3/4] Cloning/updating repositories..."

    clone_repo() {
        local repo_url=$1
        local repo_name=$2
        local repo_branch=${3:-}  # Optional branch
        local repo_path="$INSTALL_PATH/$repo_name"

        if [[ "$DRY_RUN" == "true" ]]; then
            if [[ -n "$repo_branch" ]]; then
                log_dry_run "git clone -b $repo_branch $repo_url $repo_path"
                manifest_add "git_clone" "repo" "$repo_url" "dest" "$repo_path" "branch" "$repo_branch"
            else
                log_dry_run "git clone $repo_url $repo_path"
                manifest_add "git_clone" "repo" "$repo_url" "dest" "$repo_path"
            fi
        else
            if [[ -d "$repo_path" ]]; then
                log_info "  $repo_name: already exists"
            else
                log_info "  $repo_name: cloning..."
                # Run as pi user to ensure correct ownership
                if [[ -n "$repo_branch" ]]; then
                    sudo -u pi HOME=/home/pi git -C "$INSTALL_PATH" clone -b "$repo_branch" "$repo_url"
                else
                    sudo -u pi HOME=/home/pi git -C "$INSTALL_PATH" clone "$repo_url"
                fi
            fi
        fi
    }

    clone_repo "https://github.com/hamma-dev/brokkr.git" "brokkr" "0.4.x"
    clone_repo "https://github.com/hamma-dev/serviceinstaller.git" "serviceinstaller"
    clone_repo "https://github.com/pbitzer/notifiers.git" "notifiers"

    if [[ "$DRY_RUN" != "true" ]]; then
        log_success "Repositories ready"
    fi

    # --- Step 4: Install Python packages ---
    log_step "[Brokkr 4/4] Installing Python packages..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "pip install $INSTALL_PATH/brokkr (as pi user)"
        log_dry_run "pip install $INSTALL_PATH/serviceinstaller (as pi user)"
        log_dry_run "pip install $INSTALL_PATH/notifiers (as pi user)"
        log_dry_run "pip install gpiozero RPi.GPIO (as pi user)"
        manifest_add "pip_install" "package" "$INSTALL_PATH/brokkr" "user" "pi"
        manifest_add "pip_install" "package" "$INSTALL_PATH/serviceinstaller" "user" "pi"
        manifest_add "pip_install" "package" "$INSTALL_PATH/notifiers" "user" "pi"
        manifest_add "pip_install" "package" "gpiozero RPi.GPIO" "user" "pi"
    else
        # Run as pi user to ensure correct ownership
        # Use non-editable installs to avoid .pth file issues with sudo
        sudo -u pi HOME=/home/pi bash -c "source '$VENV_PATH/bin/activate' && pip install '$INSTALL_PATH/brokkr'"
        sudo -u pi HOME=/home/pi bash -c "source '$VENV_PATH/bin/activate' && pip install '$INSTALL_PATH/serviceinstaller'"
        sudo -u pi HOME=/home/pi bash -c "source '$VENV_PATH/bin/activate' && pip install '$INSTALL_PATH/notifiers'"

        # GPIO packages for relay control
        sudo -u pi HOME=/home/pi bash -c "source '$VENV_PATH/bin/activate' && pip install gpiozero RPi.GPIO"

        log_success "Python packages installed"
    fi

    log_success "Brokkr installation complete!"
}

# --- Configure Brokkr ---
# Runs brokkr configure-system, configure-unit, and install-all
configure_brokkr() {
    local sensor_num="$1"

    if ! validate_sensor_num "$sensor_num"; then
        log_error "Invalid sensor number: $sensor_num"
        return 1
    fi

    local sensor_formatted=$(format_sensor_num "$sensor_num")
    local hostname="mjolnir$sensor_formatted"

    log_step "Configuring Brokkr for $hostname..."

    # Important: Ensure proper environment for brokkr commands
    # When running via sudo, SUDO_USER might be set to root which causes
    # brokkr to look in wrong config directory
    export HOME=/home/pi
    unset SUDO_USER

    # --- Step 1: Configure system ---
    # Run as pi user to ensure config directory is owned by pi, not root
    log_step "[Brokkr Config 1/3] Configuring system..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "brokkr configure-system hamma $INSTALL_PATH/mjolnir-hamma"
        manifest_add "command" "cmd" "brokkr configure-system hamma $INSTALL_PATH/mjolnir-hamma"
    else
        sudo -u pi HOME=/home/pi bash -c "source '$VENV_PATH/bin/activate' && brokkr configure-system hamma '$INSTALL_PATH/mjolnir-hamma'"
        log_success "System configured for hamma"
    fi

    # --- Step 2: Configure unit ---
    log_step "[Brokkr Config 2/3] Configuring unit..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "brokkr configure-unit $sensor_formatted --site-description 'Deployed site'"
        manifest_add "command" "cmd" "brokkr configure-unit $sensor_formatted"
    else
        sudo -u pi HOME=/home/pi bash -c "source '$VENV_PATH/bin/activate' && brokkr configure-unit '$sensor_formatted' --site-description 'Deployed site description - unit.toml'"
        log_success "Unit configured for sensor $sensor_formatted"
    fi

    # --- Step 3: Install dependencies ---
    log_step "[Brokkr Config 3/3] Installing dependencies..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "brokkr install-dependencies"
        manifest_add "command" "cmd" "brokkr install-dependencies"
    else
        sudo -u pi HOME=/home/pi bash -c "source '$VENV_PATH/bin/activate' && brokkr install-dependencies"
        log_success "Dependencies installed"
    fi

    # --- Step 4: Install services (requires sudo) ---
    log_step "[Brokkr Config 4/4] Installing services..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "sudo brokkr install-all"
        manifest_add "command" "cmd" "brokkr install-all" "sudo" "true"
    else
        # Need to preserve HOME so brokkr reads the right config
        sudo HOME=/home/pi "$VENV_PATH/bin/brokkr" install-all
        log_success "Brokkr services installed"
    fi

    log_success "Brokkr configuration complete!"
    echo ""
    log_info "To verify:"
    echo "  source /home/pi/$VENV_NAME"
    echo "  brokkr status"
    echo ""
    log_info "To start service:"
    echo "  sudo systemctl start brokkr-hamma-default.service"
}
