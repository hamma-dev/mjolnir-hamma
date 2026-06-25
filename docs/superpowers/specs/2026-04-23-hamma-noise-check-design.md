# hamma_noise.py — On-Demand Sensor Noise Check

**Date:** 2026-04-23
**Location:** `mjolnir-hamma/scripts/hamma_noise.py`
**Runs on:** mj Pi (has `hamma` package installed)

## Purpose

Provide an on-demand noise level check for deployed HAMMA sensors. The script runs locally on the mj Pi, analyzes recent trigger files, and reports whether the sensor's noise floor is healthy relative to its AGS trigger threshold. This avoids transferring full waveforms over cellular — only the operator SSHs in and reads the output.

## How It Works

1. Discover DATA drives at `--mj-path` (default `/media/pi`), glob `DATA??`
2. Recursively find `.bin` files across all drives/hourly subdirectories, sorted by mtime (newest first), take the first `--count`
3. For each file, attempt to create a `hamma.Header` object; catch and count failures, continue with remaining files
4. Extract the AGS trigger threshold from `Header.data.threshold` (already in volts)
5. Compute per-trigger diagnostics using the same approach as `hamma.header.core._diagnostic_data`:
   - Median offset from the first ~20k slow samples (~10x for fast)
   - Peak-to-peak noise via 0.01/99.9 percentiles of the pre-trigger portion
6. Aggregate across triggers: median, max, and IQR of each metric
7. Compare **max** noise Vpp to threshold, flag if ratio exceeds a configurable warning level
8. Print summary to stdout
9. Save results to a small JSON file

## Output

```
=== Noise Check: mj05 | 2026-04-23 14:32 UTC ===
Files analyzed: 8
Threshold: 0.042V

Channel    Median(Vpp)  Max(Vpp)   IQR(Vpp)   Noise/Thresh(max)
slow       0.003V       0.005V     0.001V       11.9%
fast       0.012V       0.028V     0.006V       66.7%

Channel    Median(Off)  Max(Off)   IQR(Off)
slow       0.015V       0.018V     0.002V
fast      -0.002V       0.004V     0.003V

Status: OK
```

If max noise/threshold ratio exceeds the warning level:

```
Status: WARNING - fast channel noise at 92% of threshold
```

## CLI Interface

```
python hamma_noise.py [options]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--mj-path PATH` | `/media/pi` | Base path for DATA drive discovery (globs `DATA??`) |
| `--count N` | 10 | Number of most recent files to analyze |
| `--warn-pct N` | 80 | Noise/threshold percentage that triggers a warning (based on max) |
| `--output FILE` | `/tmp/noise_check.json` | Path for the JSON results file |
| `--no-save` | False | Skip saving JSON, print only |

## JSON Output

```json
{
  "sensor": "mj05",
  "timestamp": "2026-04-23T14:32:00Z",
  "files_analyzed": 8,
  "threshold_V": 0.042,
  "slow": {
    "noise_vpp_median": 0.003,
    "noise_vpp_max": 0.005,
    "noise_vpp_iqr": 0.001,
    "offset_median": 0.015,
    "offset_max": 0.018,
    "offset_iqr": 0.002,
    "noise_thresh_pct": 11.9
  },
  "fast": {
    "noise_vpp_median": 0.012,
    "noise_vpp_max": 0.028,
    "noise_vpp_iqr": 0.006,
    "offset_median": -0.002,
    "offset_max": 0.004,
    "offset_iqr": 0.003,
    "noise_thresh_pct": 66.7
  },
  "status": "OK",
  "warnings": []
}
```

## Implementation Details

### File Discovery

- Discover DATA drives: glob `DATA??` under `--mj-path` (default `/media/pi`)
- Recursively glob `**/*.bin` across all discovered drives
- Sort by modification time (most recent first)
- Take the first `--count` files
- If fewer than `--count` files exist, analyze all available and report actual count
- If zero files found, print error and exit with nonzero code

### Noise Measurement

Reuse the logic from `hamma.header.core._diagnostic_data`:

```python
MEDSIZE = 20000  # samples for slow channel
perc = [0.01, 99.9]

# For each trigger:
noise_pp = np.percentile(volt[0:MEDSIZE], perc)
noise = noise_pp[1] - noise_pp[0]
offset = np.median(volt[0:MEDSIZE])
```

Fast channel uses `MEDSIZE * 10` (10x sample rate).

### Threshold Extraction

The threshold is stored in the header DataFrame as `Header.data.threshold` (already converted to volts by the version20 converter: `(5./4096) * rawHdr['thresh1'] / 6.024`). Use the median threshold across analyzed triggers for the ratio computation. If thresholds vary across triggers (operator changed it), note this in the output.

### Aggregation

For each metric (noise Vpp, offset) across all analyzed triggers:
- **Median** — typical value
- **Max** — worst case (used for threshold comparison)
- **IQR** — variability (Q75 - Q25)

This reveals both the typical noise floor and intermittent spikes.

### Error Handling

- **Zero files found:** Print error, exit code 1.
- **All files fail to read:** Print error with count of failures, exit code 1.
- **Some files fail:** Skip bad files, report count of successful reads, continue with remaining.
- **Threshold is zero:** Report noise values but skip ratio computation, print "N/A" for noise/threshold percentage.

### Sensor ID

Extract from the filename convention: first field before `_` in the `.bin` filename (e.g., `mj05_20260423_...bin` -> `mj05`).

## Dependencies

- `hamma` (installed on mj Pi)
- `numpy` (transitive via hamma)
- Standard library: `argparse`, `json`, `glob`, `os`, `datetime`

## Future Enhancements

- `--trigger` flag: fire a manual trigger via `das_manual_trigger` (AGS script on mj Pi) before analysis, providing a real-time noise measurement during quiet conditions
- Integration with brokkr status reporting
- Threshold trend tracking over time

## Non-Goals

- No data transfer / sync of results
- No temperature, humidity, or GPS reporting (already handled by brokkr)
- No waveform plotting (this is a quick operational check, not analysis)
