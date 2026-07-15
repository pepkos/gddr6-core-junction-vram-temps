# AGENTS.md — gddr6-core-junction-vram-temps

## What this repo does

Single-file C CLI tool that reads **core**, **junction (hotspot)**, and **VRAM** temperatures from NVIDIA GDDR6/GDDR6X GPUs on Linux. It accesses undocumented GPU registers via direct memory mapping of **BAR0 MMIO space** through `/dev/mem` + PCI enumeration, and uses NVIDIA NVML for core temperature.

**Author**: ThomasBaruzier (Copyright 2025, Apache 2.0)

## Architecture

- **Single file**: `gputemps.c` (427 lines of C, no build system)
- Dependencies: `libpci-dev`, `nvidia-cuda-toolkit` (for `nvml.h` and `-lnvidia-ml`)
- Requires **root** privileges (accesses `/dev/mem`)

### Temperature sources

| Source    | Method                                    | Offset       |
|-----------|-------------------------------------------|-------------|
| Core      | NVML `nvmlDeviceGetTemperature`          | —           |
| Junction  | Direct BAR0 MMIO space mmap via `/dev/mem` | `0x0002046C`|
| VRAM      | Direct BAR0 MMIO space mmap via `/dev/mem` | `0x0000E2A8`|

**Note**: Access is through BAR0 memory-mapped I/O space (`dev->base_addr[0]`), **not** PCI configuration space.

### Register read mechanism (`read_register_temp`, `gputemps.c:210-238`)

1. Opens `/dev/mem` with `O_RDWR | O_SYNC` (per-read, not reused)
2. Computes register absolute address: `dev->base_addr[0] & 0xFFFFFFFF + offset` (32-bit truncation of BAR0 base)
3. Page-aligns the base: `reg_addr & ~(PG_SZ-1)`
4. `mmap()` with `PROT_READ, MAP_SHARED`, size = page size
5. Reads one `uint32_t` from the computed page offset
6. Validates: rejects values `>= 0x7f` (127°C) as sensor-invalid
7. `munmap` + `close`

### Field extraction logic

| Register | Offset | Extraction | Resolution |
|---|---|---|---|
| Junction | `0x0002046C` | `(reg_value >> 8) & 0xff` (byte 1) | 1°C |
| VRAM | `0x0000E2A8` | `(reg_value & 0x00000fff) / 0x20` (lower 12 bits / 32) | 0.0625°C |

### PCI device matching (`get_gpu_temps`, `gputemps.c:246-252`)

- Iterates libpci bus device list
- Matches on: `(device_id << 16 | vendor_id) == pciDeviceId`, domain, bus, device/function
- Uses BAR0 (`base_addr[0]`) as the mapping base

### Output modes

- **Table** (default): Continuous colored terminal UI, refreshes every 1s
- `--once`: Print once and exit
- `--json`: JSONL output: `{"timestamp":1700000000,"gpus":[{"index":0,"core":55,"junction":68,"vram":72}]}`

### Terminal handling (continuous mode)

- `select()` on STDIN with `REFRESH_DURATION` timeout — any keypress breaks the loop
- Raw terminal mode: disables ICANON and ECHO, sets VMIN=0, VTIME=0
- Curses-like cursor: hides cursor, clears and re-prints using `\033[nA` (cursor up) escape sequences
- `atexit(reset_terminal)` and `atexit(restore_cursor)` for cleanup
- Signal handling: SIGINT, SIGTERM, SIGHUP all caught
- **Warning**: `SIGKILL` will leave terminal in raw mode

### GPU compatibility

**Chip families**:

| Chip | GPUs |
|------|------|
| GA102 | RTX 3090, 3090 Ti, 3080 Ti, 3080, A10, RTX A4500, A5000 |
| AD102 | RTX 4090, RTX A6000, L40S |
| AD103 | RTX 4080 Super, 4080, 4070 Ti Super |
| AD104 | RTX 4070 Ti, 4070 Super, 4070, L4 |
| AD106 | RTX 4060 Ti 16GB |
| AD107 | RTX 4060 Max-Q |
| GA106 | RTX A2000 |
| GA104 | RTX 3070, 3070 LHR **(NOT WORKING)** |

- **Tested**: RTX 3090, 4060 Ti 16GB, 4060 Max-Q
- **Expected**: All listed above except GA104
- **Not working**: RTX 3070 (GA104)

### Thresholds (color coding)

- Core: warn 70°C, danger 85°C
- Junction: warn 80°C, danger 95°C
- VRAM: warn 80°C, danger 95°C

### Buffer safety

- Buffer overflow protection via `vsnprintf` with remaining-byte guard, bounded by `BUFFER_SIZE` (1024 bytes)
- No input sanitization on CLI args; unknown flags error out and exit

## Security Considerations

1. **Root required** — opens `/dev/mem` with `O_RDWR`; can read arbitrary physical memory
2. **No sandboxing** — full host access, not restricted to GPU BARs
3. **`/dev/mem` restricted by modern kernels** — `iomem=relaxed` kernel parameter may be needed
4. **No `madvise` or MADV_DONTNEED** — single-page mmap is minimal exposure

## Build

```bash
# Native
gcc gputemps.c -o gputemps -O3 -lnvidia-ml -lpci -I$CUDA_HOME/targets/x86_64-linux/include

# Docker (auto-downloads CUDA)
./build-docker.sh && sudo ./gputemps
```

### Docker build details

- Base: `ubuntu:20.04` with `DEBIAN_FRONTEND=noninteractive`
- Installs: `gcc`, `curl`, `libpci-dev`, `nvidia-cuda-toolkit`
- `build-docker.sh` creates image `gputemps-builder`, copies out binary, removes container

### CI/CD (GitHub Actions)

- **Trigger**: tags matching `v*` on push, or manual `workflow_dispatch`
- **Runner**: `ubuntu-latest`
- Uses `./build-docker.sh` to build, then `softprops/action-gh-release@v2` to upload
- **Only x86_64 Linux binaries** (no cross-compilation, no arm64)

## Run

```bash
sudo ./gputemps              # continuous table
sudo ./gputemps --once       # single reading
sudo ./gputemps --json       # continuous JSONL
sudo ./gputemps --json --once # single JSON line
```

## Troubleshooting

- `mmap error` → add kernel boot param `iomem=relaxed`, disable Secure Boot
  - Secure Boot disable via `mokutil --disable-validation` or UEFI/BIOS
- Requires root; use `sudo`

## Tests

No automated tests. Manual verification only.

## Credits

- https://github.com/jjziets/gddr6_temps — demonstrated VRAM and Junction temps retrieval
- https://github.com/olealgoritme/gddr6 — pioneered the method to access undocumented GPU registers

## Key files

- `gputemps.c` — entire application
- `Dockerfile` — build environment with CUDA (Ubuntu 20.04 base)
- `build-docker.sh` — build via Docker, extract binary
- `README.md` — end-user documentation with quickstart, compatibility lists, troubleshooting
- `LICENSE` — Apache 2.0 (Copyright 2025 ThomasBaruzier)
