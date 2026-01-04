"""
Mock SSH-related operations for testing.

This module specifically tracks ssh-keygen calls which are CRITICAL
for the WiFi path - the previous failed attempt missed this.
"""

from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, field


@dataclass
class SSHKeygenCall:
    """Record of an ssh-keygen call."""
    keytype: str = "rsa"
    keyfile: Optional[str] = None
    passphrase: str = ""
    comment: str = ""


class SSHKeygenTracker:
    """Track ssh-keygen calls with detailed information.

    This is critical for testing because:
    1. WiFi path (setup_uah_wireless.sh) MUST call ssh-keygen to generate id_rsa
    2. Cellular path should NOT call ssh-keygen (no id_rsa needed)
    3. install_hamma.sh -k generates id_ed25519 for GitHub access

    The previous failed attempt missed the id_rsa generation entirely.
    """

    def __init__(self, ssh_dir: Path):
        self.ssh_dir = ssh_dir
        self.ssh_dir.mkdir(parents=True, exist_ok=True)
        self.calls: List[SSHKeygenCall] = []

    def record_call(
        self,
        keytype: str = "rsa",
        keyfile: Optional[str] = None,
        passphrase: str = "",
        comment: str = ""
    ) -> Path:
        """Record an ssh-keygen call and create mock keys.

        Returns the path to the generated private key.
        """
        call = SSHKeygenCall(
            keytype=keytype,
            keyfile=keyfile,
            passphrase=passphrase,
            comment=comment,
        )
        self.calls.append(call)

        # Determine key file path
        if keyfile:
            key_path = Path(keyfile)
        else:
            # Default locations based on key type
            if keytype == "rsa":
                key_path = self.ssh_dir / "id_rsa"
            elif keytype == "ed25519":
                key_path = self.ssh_dir / "id_ed25519"
            elif keytype == "ecdsa":
                key_path = self.ssh_dir / "id_ecdsa"
            else:
                key_path = self.ssh_dir / f"id_{keytype}"

        # Create mock key files
        key_path.parent.mkdir(parents=True, exist_ok=True)

        # Private key
        key_path.write_text(
            f"-----BEGIN OPENSSH PRIVATE KEY-----\n"
            f"MOCK_{keytype.upper()}_PRIVATE_KEY_FOR_TESTING\n"
            f"-----END OPENSSH PRIVATE KEY-----\n"
        )

        # Public key
        pub_path = key_path.with_suffix(".pub")
        user_comment = comment if comment else "pi@mjolnirNN"
        pub_path.write_text(f"ssh-{keytype} AAAAB3MockPublicKey {user_comment}\n")

        return key_path

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def was_rsa_generated(self) -> bool:
        """Check if RSA key was generated (required for WiFi/server access)."""
        return any(call.keytype == "rsa" for call in self.calls)

    def was_ed25519_generated(self) -> bool:
        """Check if ED25519 key was generated (required for GitHub/hamma)."""
        return any(call.keytype == "ed25519" for call in self.calls)

    def get_id_rsa_path(self) -> Optional[Path]:
        """Get path to id_rsa if it was generated."""
        for call in self.calls:
            if call.keytype == "rsa":
                if call.keyfile:
                    return Path(call.keyfile)
                return self.ssh_dir / "id_rsa"
        return None

    def get_id_ed25519_path(self) -> Optional[Path]:
        """Get path to id_ed25519 if it was generated."""
        for call in self.calls:
            if call.keytype == "ed25519":
                if call.keyfile:
                    return Path(call.keyfile)
                return self.ssh_dir / "id_ed25519"
        return None

    def id_rsa_exists(self) -> bool:
        """Check if id_rsa file exists in ssh_dir."""
        return (self.ssh_dir / "id_rsa").exists()

    def id_ed25519_exists(self) -> bool:
        """Check if id_ed25519 file exists in ssh_dir."""
        return (self.ssh_dir / "id_ed25519").exists()

    def get_call_summary(self) -> str:
        """Get human-readable summary of all calls."""
        if not self.calls:
            return "No ssh-keygen calls recorded"

        lines = ["ssh-keygen calls:"]
        for i, call in enumerate(self.calls, 1):
            lines.append(
                f"  {i}. type={call.keytype}, "
                f"file={call.keyfile or '(default)'}, "
                f"passphrase={'(empty)' if not call.passphrase else '(set)'}"
            )
        return "\n".join(lines)


def parse_ssh_keygen_args(args: List[str]) -> SSHKeygenCall:
    """Parse ssh-keygen command line arguments.

    Example: ssh-keygen -t rsa -f /path/to/key -N "" -C "comment"
    """
    call = SSHKeygenCall()

    i = 0
    while i < len(args):
        arg = args[i]

        if arg == "-t" and i + 1 < len(args):
            call.keytype = args[i + 1]
            i += 2
        elif arg == "-f" and i + 1 < len(args):
            call.keyfile = args[i + 1]
            i += 2
        elif arg == "-N" and i + 1 < len(args):
            call.passphrase = args[i + 1]
            i += 2
        elif arg == "-C" and i + 1 < len(args):
            call.comment = args[i + 1]
            i += 2
        elif arg == "ssh-keygen":
            i += 1  # Skip the command itself
        else:
            i += 1

    return call
