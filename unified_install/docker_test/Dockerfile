# Dockerfile for testing HAMMA Pi install scripts
# Uses Debian Bullseye (current Raspberry Pi OS base)

FROM debian:bullseye-slim

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install packages that would be on a fresh Pi
RUN apt-get update && apt-get install -y \
    sudo \
    systemd \
    systemd-sysv \
    iproute2 \
    iputils-ping \
    git \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    wget \
    procps \
    wpasupplicant \
    rfkill \
    && rm -rf /var/lib/apt/lists/*

# Create pi user (matches Raspberry Pi default)
RUN useradd -m -s /bin/bash -G sudo pi && \
    echo "pi:raspberry" | chpasswd && \
    echo "pi ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# Create directory structure matching Pi
RUN mkdir -p /home/pi/dev /mnt/usb /boot /boot/firmware \
    /etc/systemd/network \
    /etc/wpa_supplicant \
    /run/systemd/resolve \
    /etc/systemd/system/wpa_supplicant@wlan0.service.d \
    /usr/local/bin

# Create mock resolv.conf files (systemd-resolved would create these)
RUN echo "nameserver 127.0.0.53" > /run/systemd/resolve/stub-resolv.conf && \
    echo "nameserver 8.8.8.8" > /run/systemd/resolve/resolv.conf

# Create /boot/config.txt (Raspberry Pi config)
RUN echo "# Raspberry Pi config" > /boot/config.txt

# Set ownership
RUN chown -R pi:pi /home/pi

# Set working directory
WORKDIR /home/pi

# Default to pi user
USER pi

CMD ["/bin/bash"]
