#!/bin/bash
# Software installation for HAMMA Pi
#
# Based on original install_packages.sh, install_sindri.sh, install_pyltg.sh, install_hamma.sh
#
# This script installs:
#   1. System packages (apt)
#   2. Sindri (website builder) in sindrienv
#   3. PyLtg (lightning data processing) in ltgenv
#   4. HAMMA (private repo) in ltgenv
#
# Requirements:
#   - common.sh must be sourced first
#   - For HAMMA: ed25519 key must be added to GitHub first
#
# Functions:
#   install_system_packages
#   install_sindri <sensor_number>
#   install_pyltg
#   install_hamma

# --- Configuration ---
INSTALL_PATH="/home/pi/dev"

# --- Install System Packages ---
install_system_packages() {
    log_step "Installing system packages..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "apt-get update && apt-get dist-upgrade"
        log_dry_run "apt-get install imagemagick eject udisks2 python3-venv git build-essential..."
        manifest_add "command" "cmd" "apt-get update" "sudo" "true"
        manifest_add "command" "cmd" "apt-get dist-upgrade -y" "sudo" "true"
        manifest_add "apt_install" "packages" "imagemagick eject udisks2 python3-venv git build-essential python3-dev gfortran networkd-dispatcher modemmanager udhcpc libqmi-utils"
        log_success "System packages would be installed"
        return 0
    fi

    # Check for root
    if [[ $EUID -ne 0 ]]; then
        log_error "System package installation requires root (use sudo)"
        return 1
    fi

    # Fix Buster EOL repos if needed (only for actual Buster systems)
    if grep -q "buster" /etc/apt/sources.list 2>/dev/null; then
        log_info "Fixing EOL Debian Buster repositories..."
        sed -i 's|deb.debian.org/debian |archive.debian.org/debian |g' /etc/apt/sources.list
        sed -i 's|deb.debian.org/debian-security |archive.debian.org/debian-security |g' /etc/apt/sources.list
        sed -i '/buster-updates/d' /etc/apt/sources.list
    fi

    # Update package lists
    log_info "Updating package lists..."
    apt-get update -y

    # Upgrade existing packages
    log_info "Upgrading existing packages..."
    apt-get dist-upgrade -y
    apt-get autoremove -y

    # Install required packages
    log_info "Installing required packages..."
    apt-get install -y \
        imagemagick \
        eject \
        udisks2 \
        python3-venv \
        git \
        build-essential \
        python3-dev \
        gfortran \
        networkd-dispatcher \
        modemmanager \
        udhcpc \
        libqmi-utils

    log_success "System packages installed"
}

# --- Install Sindri ---
# Creates sindrienv and installs sindri for website building
install_sindri() {
    local sensor_num="$1"

    if ! validate_sensor_num "$sensor_num"; then
        log_error "Invalid sensor number: $sensor_num"
        return 1
    fi

    local sensor_formatted=$(format_sensor_num "$sensor_num")
    local venv_name="sindrienv"
    local venv_path="$INSTALL_PATH/$venv_name"
    local server_name="hamma$sensor_num"

    log_step "Installing Sindri..."

    # --- Step 1: Create virtual environment ---
    log_step "[Sindri 1/4] Creating virtual environment..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "python3 -m venv $venv_path (as pi user)"
        log_dry_run "cp $venv_path/bin/activate /home/pi/$venv_name"
        manifest_add "command" "cmd" "python3 -m venv $venv_path" "user" "pi"
        manifest_add "copy" "src" "$venv_path/bin/activate" "dst" "/home/pi/$venv_name"
    else
        if [[ ! -d "$venv_path" ]]; then
            # Run as pi user to ensure correct ownership
            sudo -H -u pi python3 -m venv "$venv_path"
            sudo -H -u pi cp "$venv_path/bin/activate" "/home/pi/$venv_name"
            log_success "Created $venv_path"
        else
            log_warn "Virtual environment already exists"
        fi
    fi

    # --- Step 2: Upgrade pip ---
    log_step "[Sindri 2/4] Upgrading pip..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "pip install --upgrade pip setuptools wheel (as pi user)"
        manifest_add "pip_install" "package" "pip setuptools wheel" "upgrade" "true" "venv" "$venv_name" "user" "pi"
    else
        # Run as pi user to ensure correct ownership
        sudo -H -u pi bash -c "source '$venv_path/bin/activate' && pip install --upgrade pip setuptools wheel"
        log_success "pip upgraded"
    fi

    # --- Step 3: Clone and install sindri ---
    log_step "[Sindri 3/4] Installing sindri..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "git clone -b 0.3.x --recursive https://github.com/hamma-dev/sindri.git (as pi user)"
        log_dry_run "pip install $INSTALL_PATH/sindri (as pi user)"
        log_dry_run "pip install $INSTALL_PATH/serviceinstaller (as pi user)"
        manifest_add "git_clone" "repo" "https://github.com/hamma-dev/sindri.git" "dest" "$INSTALL_PATH/sindri" "branch" "0.3.x" "recursive" "true" "user" "pi"
        manifest_add "pip_install" "package" "$INSTALL_PATH/sindri" "venv" "$venv_name" "user" "pi"
        manifest_add "pip_install" "package" "$INSTALL_PATH/serviceinstaller" "venv" "$venv_name" "user" "pi"
    else
        if [[ ! -d "$INSTALL_PATH/sindri" ]]; then
            log_info "Cloning sindri (this may take a while)..."
            # Run as pi user to ensure correct ownership
            sudo -H -u pi git -C "$INSTALL_PATH" clone -b "0.3.x" --recursive "https://github.com/hamma-dev/sindri.git"
        else
            log_warn "Sindri already exists, skipping clone"
        fi

        log_info "Installing sindri (this may take a while)..."
        # Run as pi user, use non-editable install
        sudo -H -u pi bash -c "source '$venv_path/bin/activate' && pip install '$INSTALL_PATH/sindri'"
        sudo -H -u pi bash -c "source '$venv_path/bin/activate' && pip install '$INSTALL_PATH/serviceinstaller'"
        log_success "Sindri installed"
    fi

    # --- Step 4: Configure for this sensor ---
    log_step "[Sindri 4/4] Configuring for sensor $sensor_num..."

    local lektor_file="$INSTALL_PATH/sindri/src/sindri/website/mjolnir-website/mjolnir-website.lektorproject"
    local lektor_key="target = rsync://pi@hamma.dev/var/www/hamma.dev/public_html/"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "sed lektor target to $server_name in $lektor_file"
        manifest_add "sed" "path" "$lektor_file" "pattern" "$lektor_key.*" "replacement" "$lektor_key$server_name"
    else
        if [[ -f "$lektor_file" ]]; then
            sed -i "s%$lektor_key.*%$lektor_key$server_name%" "$lektor_file"
            log_success "Configured for sensor $sensor_num"
        else
            log_warn "Lektor file not found, skipping configuration"
        fi
    fi

    log_success "Sindri installation complete!"
}

# --- Install PyLtg ---
# Installs PyLtg for lightning data processing
install_pyltg() {
    local venv_name="ltgenv"
    local venv_path="$INSTALL_PATH/$venv_name"

    log_step "Installing PyLtg..."

    # --- Step 1: Install system dependencies ---
    log_step "[PyLtg 1/3] Installing system dependencies..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "apt-get install libhdf5-dev libnetcdf-dev proj-bin libproj-dev libgeos-dev"
        manifest_add "apt_install" "packages" "libhdf5-dev libnetcdf-dev proj-bin libproj-dev libgeos-dev" "sudo" "true"
    else
        sudo apt-get update
        sudo apt-get install -y libhdf5-dev libnetcdf-dev proj-bin libproj-dev libgeos-dev
        log_success "System dependencies installed"
    fi

    # --- Step 2: Install cartopy (version pinned for compatibility) ---
    # Note: cartopy version depends on Python version
    #   - Python 3.7 (Buster): use cartopy 0.20.3 (last 3.7-compatible)
    #   - Python 3.8+ (Bullseye+): use cartopy 0.21.1
    log_step "[PyLtg 2/3] Installing cartopy..."

    # Detect Python version to select compatible cartopy
    # - Python 3.7 (Buster): use cartopy 0.19.0.post1 (compatible with GEOS 3.7.1)
    # - Python 3.8+ (Bullseye+): use cartopy 0.21.1
    local python_minor
    python_minor=$(python3 -c "import sys; print(sys.version_info.minor)")
    local cartopy_version="0.21.1"
    if [[ "$python_minor" -le 7 ]]; then
        cartopy_version="0.19.0.post1"
        log_info "Python 3.7 detected, using cartopy $cartopy_version"
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "pip install cartopy==$cartopy_version (as pi user)"
        manifest_add "pip_install" "package" "cartopy==$cartopy_version" "venv" "$venv_name" "user" "pi"
    else
        sudo -H -u pi bash -c "source '$venv_path/bin/activate' && pip install cartopy==$cartopy_version"
        log_success "Cartopy installed"
    fi

    # --- Step 3: Clone and install pyltg ---
    log_step "[PyLtg 3/3] Installing pyltg..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "git clone https://github.com/pbitzer/pyltg (as pi user)"
        log_dry_run "pip install $INSTALL_PATH/pyltg (as pi user)"
        manifest_add "git_clone" "repo" "https://github.com/pbitzer/pyltg" "dest" "$INSTALL_PATH/pyltg" "user" "pi"
        manifest_add "pip_install" "package" "$INSTALL_PATH/pyltg" "venv" "$venv_name" "user" "pi"
    else
        if [[ ! -d "$INSTALL_PATH/pyltg" ]]; then
            # Run as pi user to ensure correct ownership
            sudo -H -u pi git -C "$INSTALL_PATH" clone "https://github.com/pbitzer/pyltg"
        else
            log_warn "PyLtg already exists, skipping clone"
        fi

        # Fix setup.py bug: uses setuptools functions without importing setuptools
        # This breaks with modern pip's PEP 517 build isolation
        if ! grep -q "^import setuptools" "$INSTALL_PATH/pyltg/setup.py" 2>/dev/null; then
            log_info "Patching pyltg setup.py for compatibility..."
            # Add import setuptools after the first import line
            sed -i '/^from pathlib import Path/a import setuptools' "$INSTALL_PATH/pyltg/setup.py"
            # Change setup( to setuptools.setup( if needed
            sed -i 's/^setup(/setuptools.setup(/g' "$INSTALL_PATH/pyltg/setup.py"
        fi

        # Run as pi user
        sudo -H -u pi bash -c "source '$venv_path/bin/activate' && pip install '$INSTALL_PATH/pyltg'"
        log_success "PyLtg installed"
    fi

    log_success "PyLtg installation complete!"
}

# --- Install HAMMA ---
# Installs HAMMA from private GitHub repository
# First run with --generate-key to create ed25519 key for GitHub
# Then add key to GitHub and run again without --generate-key
install_hamma() {
    local generate_key=false

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            -k|--generate-key)
                generate_key=true
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    local venv_name="ltgenv"
    local venv_path="$INSTALL_PATH/$venv_name"
    local ssh_dir="/home/pi/.ssh"
    local ed25519_key="$ssh_dir/id_ed25519"
    local ssh_config="$ssh_dir/config"

    if [[ "$generate_key" == "true" ]]; then
        # --- Generate SSH key for GitHub ---
        log_step "Generating SSH key for GitHub..."

        if [[ "$DRY_RUN" == "true" ]]; then
            log_dry_run "ssh-keygen -t ed25519 -f $ed25519_key"
            log_dry_run "Add github-hamma host to SSH config"
            manifest_add "ssh-keygen" "keytype" "ed25519" "keyfile" "$ed25519_key"
            manifest_add "append" "path" "$ssh_config" "content" "Host github-hamma config"
        else
            # Run as pi user to ensure correct ownership
            sudo -H -u pi bash -c "
                if [[ -f '$ed25519_key' ]]; then
                    echo 'ED25519 key already exists'
                else
                    ssh-keygen -f '$ed25519_key' -t ed25519 -C 'bitzerp@uah.edu' -N ''
                fi
            "

            if [[ -f "$ed25519_key" ]]; then
                log_success "SSH key ready at $ed25519_key"
            else
                log_warn "ED25519 key already exists at $ed25519_key"
            fi

            # Add GitHub host to SSH config (as pi user)
            if ! grep -q "github-hamma" "$ssh_config" 2>/dev/null; then
                # Use tee to append as pi user (heredoc in outer shell, tee handles permissions)
                sudo -H -u pi tee -a "$ssh_config" > /dev/null <<EOT

Host github-hamma
   HostName github.com
   AddKeysToAgent yes
   PreferredAuthentications publickey
   IdentityFile $ed25519_key
EOT
                log_success "Added github-hamma to SSH config"
            else
                log_warn "github-hamma already in SSH config"
            fi

            echo ""
            log_info "Public key to add to GitHub:"
            cat "$ed25519_key.pub"
            echo ""
            log_warn "Add this key to GitHub, then run install_hamma again without -k"
        fi
    else
        # --- Install HAMMA from GitHub ---
        log_step "Installing HAMMA..."

        if [[ "$DRY_RUN" == "true" ]]; then
            log_dry_run "git clone -b 0.3.x git@github-hamma:pbitzer/hamma.git (as pi user)"
            log_dry_run "pip install $INSTALL_PATH/hamma (as pi user)"
            log_dry_run "pip install future (as pi user)"
            manifest_add "git_clone" "repo" "git@github-hamma:pbitzer/hamma.git" "dest" "$INSTALL_PATH/hamma" "branch" "0.3.x" "user" "pi"
            manifest_add "pip_install" "package" "$INSTALL_PATH/hamma" "venv" "$venv_name" "user" "pi"
            manifest_add "pip_install" "package" "future" "venv" "$venv_name" "user" "pi"
        else
            if [[ ! -d "$INSTALL_PATH/hamma" ]]; then
                log_info "Cloning hamma from GitHub..."
                # Run as pi user to ensure correct ownership and use pi's SSH key
                sudo -H -u pi git -C "$INSTALL_PATH" clone -b "0.3.x" git@github-hamma:pbitzer/hamma.git
            else
                log_warn "HAMMA already exists, skipping clone"
            fi

            # Run as pi user, use non-editable install
            sudo -H -u pi bash -c "source '$venv_path/bin/activate' && pip install '$INSTALL_PATH/hamma'"
            sudo -H -u pi bash -c "source '$venv_path/bin/activate' && pip install future"  # Dependency for lmfit
            log_success "HAMMA installed"
        fi

        log_success "HAMMA installation complete!"
    fi
}
