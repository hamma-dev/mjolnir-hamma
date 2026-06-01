# server/

Scripts that run on the HAMMA VPS (`hamma.dev`), not on the sensor Pis.

| Script | Purpose |
| ------ | ------- |
| `mjol_array.py` | Drive sensor power on/off across an array by SSHing into each Pi and running `sensors.py`. Also reports per-Pi status. |
| `webgen.py` | Regenerate the per-array status HTML pages served at `hamma.dev`. |
| `install.sh` | Symlink the above onto `PATH` (default `/usr/local/bin`). Idempotent. |

## Install

```bash
cd ~/dev/mjolnir-hamma
bash server/install.sh
```

After install, `mjol_array` and `webgen` resolve from anywhere on `PATH`.

## Usage

End-user guide (commands, examples, troubleshooting):
**[mjol_array — Sensor On/Off from the VPS](https://hsvltg.atlassian.net/wiki/spaces/HAMMA/pages/497221633)** on Confluence.

For what `sensors.py` does on each Pi when `mjol_array` calls it:
**[sensors.py — Sensor Power Control](https://hsvltg.atlassian.net/wiki/spaces/HAMMA/pages/489914369)**.
