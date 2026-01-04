# Dockerfile for testing mjolnir-hamma install scripts
# Uses Debian Bookworm as a close approximation to Raspberry Pi OS

FROM debian:bookworm-slim

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install packages that would be on a Pi
RUN apt-get update && apt-get install -y \
    bash \
    sudo \
    systemd \
    python3 \
    python3-venv \
    python3-pip \
    git \
    curl \
    wget \
    iproute2 \
    iputils-ping \
    network-manager \
    modemmanager \
    udhcpc \
    libqmi-utils \
    openssh-client \
    openssh-server \
    udisks2 \
    util-linux \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Create pi user (like on real Pi)
RUN useradd -m -s /bin/bash pi && \
    echo "pi:raspberry" | chpasswd && \
    usermod -aG sudo pi && \
    echo "pi ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Create expected directory structure
RUN mkdir -p /home/pi/dev && \
    mkdir -p /home/pi/.ssh && \
    mkdir -p /etc/systemd/network && \
    mkdir -p /etc/networkd-dispatcher/carrier.d && \
    mkdir -p /etc/networkd-dispatcher/degraded.d && \
    mkdir -p /usr/local/bin && \
    mkdir -p /mnt/usb && \
    mkdir -p /boot && \
    chown -R pi:pi /home/pi

# Create mock network interfaces (can't create real ones in container)
RUN mkdir -p /sys/class/net/wwan0/qmi && \
    echo "N" > /sys/class/net/wwan0/qmi/raw_ip && \
    chmod 644 /sys/class/net/wwan0/qmi/raw_ip

# Create mock config.txt
RUN echo "# Raspberry Pi config" > /boot/config.txt

# Create mock hostname files
RUN echo "raspberrypi" > /etc/hostname

# Copy repository (will be mounted as volume in practice)
WORKDIR /home/pi/dev/mjolnir-hamma

# Default to running as pi user
USER pi

# Health check
HEALTHCHECK --interval=30s --timeout=3s \
    CMD echo "Container running" || exit 1

CMD ["/bin/bash"]
