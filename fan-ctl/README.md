# GPU-VRAM Fan Control (`fan-ctl`)

Keeps the **ASUS Pro WS WRX80E-SAGE SE** chassis fans driven by real **GPU VRAM
temperature**, by pushing duty to the board's BMC (AMI MegaRAC) over its web API.

This board's BMC can only auto-drive fans off **on-board** sensors (CPU / DIMM /
PCIe-slot / an optional `T_Sensor` thermistor). It **cannot** read GPU VRAM, so a
host-side loop does the measurement and writes the `CHA_FAN1` fan zone.

## How it works

```
gputemps --json --once   ->   maxVRAM = max(vram of all GPUs)
   -> piecewise curve -> hysteresis/rate-limit -> PUT /api/fanctrl/PWM (zone CHA_FAN1)
```

Default curve (GDDR6X on RTX 3090): `<=60C -> 30%`, `70C -> 50%`, `>=80C -> 100%`.

Actuation uses the BMC web API: `POST /api/session` (login) then
`PUT /api/fanctrl/PWM` to set the zone's PWM curve. Only the chassis zone is written.

### State sync (read-back)
The controller never trusts that a previous command is still in effect:
- On start it **forces one write** so its internal duty matches the BMC (the BMC
  may hold a stale value from an earlier run's fail-safe).
- Every `SYNC_S` seconds it reads the commanded duty back with
  `GET /api/fanctrl/PWM`. If it differs from the internal duty by more than
  `SYNC_TOL`, it re-asserts its value and logs `DRIFT`. Read failures are logged
  but deliberately **do not** trigger the fail-safe (a flaky read API must not
  slam the fans to 100%). Set `SYNC_S=0` to disable.

### Safety
The BMC has **no GPU-aware automatic mode to fall back to**, so the controller
fails safe **HIGH**:
- Invalid/stale/missing telemetry (>=2 in a row) -> ramps to `FAIL_DUTY` (100%).
- Clean stop (`systemctl stop`) -> `FAIL_DUTY`.
- systemd `Restart=always` relaunches within `RestartSec` if it crashes.

> Limitation: a host process is not a hardware fail-safe. If the host is
> powered off or the process is `SIGKILL`-ed, the BMC keeps applying the last
> written (flat) duty. Tune `MIN_DUTY` for an acceptable idle floor.

## Requirements
- Ubuntu host with root (the loop runs as root; `gputemps` requires it).
- `gputemps` built in this repo (see the top-level README).
- A BMC user that has **Web/HTTP access** and **Administrator** privilege
  (Operator can read sensors but is denied fan control).
- Working `sudo`.

## Install

1. Build the telemetry binary (already built in this repo):
   ```
   gcc gputemps.c -o gputemps -O3 -lnvidia-ml -lpci
   ```

2. Install the controller:
   ```
   sudo install -m755 fan-ctl/gpu_fan_ctl.py /usr/local/bin/gpu_fan_ctl.py
   ```

3. Create the config (root-only, holds the BMC password):
   ```
   sudo install -d -m700 /etc/gpu-fan
   sudo cp fan-ctl/env.example /etc/gpu-fan/env
   sudo chmod 600 /etc/gpu-fan/env
   sudo vi /etc/gpu-fan/env        # set BMC_PASS, GPU_BIN, ZONE, curve limits
   ```

4. Install and start the service:
   ```
   sudo cp fan-ctl/gpu-fan.service /etc/systemd/system/gpu-fan.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now gpu-fan.service
   ```

## Verify
```
sudo systemctl status gpu-fan.service
sudo journalctl -u gpu-fan.service -f          # live maxVRAM / duty decisions
sudo ipmitool sdr type Fan | grep CHA_FAN1     # RPM follows the duty
```

## Dry run (no hardware writes)
```
sudo BMC_USER=fan_controller BMC_PASS=... \
     python3 /usr/local/bin/gpu_fan_ctl.py --iterations 5   # omit --apply
```

## Tuning
- Curve: edit `VRAM_CURVE` in `/usr/local/bin/gpu_fan_ctl.py`.
- Floor/ceiling/limits/interval: `/etc/gpu-fan/env`.
- Then `sudo systemctl restart gpu-fan.service`.

## Fan zone map (this board)
| `ZONE` | BMC name |
|---|---|
| 0 | Zone1 (CPU_FAN & CPU_OPT) |
| 1 | Zone2 (CHA_FAN 1)  <- default target |
| 2-6 | Zone3..7 (CHA_FAN 2..6) |

## Security notes
- Keep `/etc/gpu-fan/env` mode `600`; **never** commit it.
- The BMC password lives only in that file (read via env at runtime).
