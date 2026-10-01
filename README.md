# iodrive-exporter

Prometheus exporter for Fusion-io / SanDisk ioDrive and ioScale PCIe flash
devices. It shells out to the vendor `fio-status` utility, parses its JSON
output, and exposes the health, thermal, endurance, PCIe, and throughput
fields as Prometheus metrics.

Multiple devices are supported. Every per-device series is labelled with the
drive `serial` (plus `pci_addr`), so a host with several ioDrives keeps their
metrics separate.

## Requirements

- Linux host with a Fusion-io device and the vendor driver installed.
- `fio-status` (ships with the Fusion-io / SanDisk driver package).
- Python 3.9+ with `prometheus-client` (`pip install -r requirements.txt`).
- Root access: `fio-status` refuses to run for unprivileged users. See
  [Deployment](#deployment) for the two ways to satisfy this.

## How it works

The exporter runs, on each scrape (or once per `--cache-ttl` seconds):

```
fio-status -a -f j -u
```

- `-a` all available information
- `-f j` JSON output
- `-u` include fields the device reports as "Unavailable." (so the schema is
  stable across firmware/driver versions)

`fio-status` is fast (tens of milliseconds once the sudo ticket is warm), so
collecting on every scrape is fine for typical scrape intervals. Use
`--cache-ttl` only if you want to cap how often the command runs.

Numeric fields are parsed defensively: values like `"97.00"`, `"-0.49"`, and
`"1370051460 (2013-06-01)"` all parse, while `"Unavailable."`, `"Healthy"`,
and empty strings are skipped (they surface as info-metric labels instead).

## Running

```bash
# As root (simplest):
sudo python3 iodrive_exporter.py --web.port 9895

# Unprivileged, via a NOPASSWD sudoers rule (see iodrive-exporter.sudoers):
python3 iodrive_exporter.py --sudo --fio-status.path /usr/bin/fio-status

# Test without a device, from a captured JSON file:
fio-status -a -f j -u > /tmp/fio.json      # run once as root
python3 iodrive_exporter.py --from-file /tmp/fio.json
```

Then browse `http://<host>:9895/metrics`.

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `--web.listen-address` | `0.0.0.0` | Address to bind. |
| `--web.port` | `9895` | Port to bind. |
| `--fio-status.path` | `fio-status` | Path to the `fio-status` binary. |
| `--sudo` | off | Invoke `fio-status` via `sudo -n`. |
| `--timeout` | `30` | Per-invocation timeout, seconds. |
| `--cache-ttl` | `0` | Reuse results for N seconds (0 = run every scrape). |
| `--from-file` | unset | Read JSON from a file instead of running `fio-status`. |
| `--include-all` | off | Also export every numeric field as `iodrive_<section>_<field>`. |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

## Metrics

Common labels on per-device series: `serial`, `pci_addr`. VSU series add
`vsu`. Adapter series use `serial`, `board_name`.

### Status / meta

| Metric | Type | Meaning |
| --- | --- | --- |
| `iodrive_up` | gauge | 1 if `fio-status` ran and parsed OK. |
| `iodrive_scrape_seconds` | gauge | Time spent collecting this scrape. |
| `iodrive_adapter_info` | gauge | Static adapter attributes (always 1). |
| `iodrive_device_info` | gauge | Static device attributes incl. `fw_version`, `driver_version`, `iom_state`, `reserve_status`. |
| `iodrive_vsu_info` | gauge | Static VSU attributes incl. `vsu_state`, `vsu_type`. |

### Health and endurance

| Metric | Unit | Meaning |
| --- | --- | --- |
| `iodrive_rated_writes_remaining_percent` | percent | Remaining rated write endurance. Primary lifetime signal. |
| `iodrive_reserve_space_percent` | percent | Remaining spare space. |
| `iodrive_reserve_warning_threshold_percent` | percent | Spare-space warning threshold. |
| `iodrive_active_media_percent` | percent | Share of NAND in active use. |
| `iodrive_powerloss_protect_available` / `_enabled` | bool | Power-loss protection state. |

### Thermal

| Metric | Unit | Meaning |
| --- | --- | --- |
| `iodrive_temp_internal_celsius` | C | Controller/FPGA temperature. |
| `iodrive_temp_internal_peak_celsius` | C | Peak since power-on. |
| `iodrive_temp_internal_warn_celsius` | C | Warning threshold. |
| `iodrive_nand_thermal_celsius` | C | NAND package temperature. |
| `iodrive_nand_throttle_start_celsius` | C | NAND throttle start. |
| `iodrive_controller_throttle_start_celsius` | C | Controller throttle start. |

### Power and PCIe

| Metric | Unit | Meaning |
| --- | --- | --- |
| `iodrive_bus_watts` / `_volts` / `_amps` | W/V/A | Slot bus power draw. |
| `iodrive_power_limit_watts` | W | Adapter power limit. |
| `iodrive_pcie_bandwidth_mbps` | Mb/s | Negotiated bandwidth. |
| `iodrive_pcie_link_lanes` / `_speed_gtps` | count / GT/s | Negotiated link width and speed. |
| `iodrive_pcie_errors` (device) / `iodrive_adapter_pcie_errors` (adapter) | bool | Link error flags (correctable/nonfatal/fatal/unrecognized variants). |

### Throughput and wear counters

| Metric | Type | Meaning |
| --- | --- | --- |
| `iodrive_physical_bytes_read_total` | counter | Lifetime physical bytes read. |
| `iodrive_physical_bytes_written_total` | counter | Lifetime physical bytes written (includes write amplification). |
| `iodrive_write_power_throttle_events_total` | counter | Cumulative power-throttle events. |
| `iodrive_write_thermal_throttle_events_total` | counter | Cumulative thermal-throttle events. |
| `iodrive_session_read_ops` / `_write_ops` / `_copied_bytes` / `_erased_bytes` | gauge | Counters since driver load; reset on driver reload, so use `rate()` with care. |

### Host (machine-global, no device labels)

`iodrive_host_ram_*_bytes`, `iodrive_host_driver_loaded`, and trim-related
flags reported by `fio-status`.

## Example alerts

```yaml
groups:
  - name: iodrive
    rules:
      - alert: IoDriveEnduranceLow
        expr: iodrive_rated_writes_remaining_percent < 10
        for: 1h
        labels: { severity: warning }
        annotations:
          summary: "ioDrive {{ $labels.serial }} endurance below 10%"
      - alert: IoDriveTooHot
        expr: iodrive_temp_internal_celsius > 80
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: "ioDrive {{ $labels.serial }} running hot ({{ $value }}C)"
      - alert: IoDrivePcieErrors
        expr: iodrive_pcie_errors == 1 or iodrive_pcie_fatal_errors == 1
        for: 10m
        labels: { severity: critical }
        annotations:
          summary: "ioDrive {{ $labels.serial }} PCIe link errors"
      - alert: IoDriveExporterDown
        expr: iodrive_up == 0
        for: 5m
        labels: { severity: critical }
        annotations:
          summary: "ioDrive exporter on {{ $labels.instance }} cannot read fio-status"
```

## Deployment

### Option A: run as root (recommended)

Copy `iodrive_exporter.py` to `/opt/iodrive-exporter/`, install the unit, and
start it:

```bash
sudo mkdir -p /opt/iodrive-exporter
sudo cp iodrive_exporter.py /opt/iodrive-exporter/
sudo cp iodrive-exporter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iodrive-exporter
```

The unit runs as `User=root`, so `fio-status` works directly.

### Option B: unprivileged user + sudoers

If you prefer not to run the whole exporter as root:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin iodrive-exporter
sudo install -m 0440 iodrive-exporter.sudoers /etc/sudoers.d/iodrive-exporter
```

Then edit the unit to set `User=iodrive-exporter` and add `--sudo
--fio-status.path /usr/bin/fio-status` to `ExecStart`. The `sudo -n` call
matches the NOPASSWD rule and never blocks on a password prompt.

### Prometheus scrape config

```yaml
scrape_configs:
  - job_name: iodrive
    static_configs:
      - targets: ["host-a:9895", "host-b:9895"]
```

## Grafana dashboard

`grafana/iodrive-dashboard.json` is ready to import (Dashboards -> Import ->
Upload JSON). It has datasource, host, and drive-serial template variables,
and covers:

- Overview stats: exporter up, endurance, reserve space, temperature, power.
- Endurance/reserve trends plus a device inventory table (fw, driver, state).
- Thermal and supply-voltage graphs.
- Byte counters: lifetime physical read/write (cumulative stat + rate graph),
  GC copy/erase rates, and session IOPS.
- PCIe link health (bandwidth, speed/lanes, device and adapter error flags).
- Throttle-event counters (24 h increase) and driver/host RAM usage.

The queries use `instance` and `serial` labels, so multiple hosts and multiple
drives per host filter cleanly.

## Notes and caveats

- `fio-status` field availability varies by device generation and firmware.
  Missing or "Unavailable." fields simply do not appear as series; the exporter
  does not emit zeros for them.
- `session_*` metrics reset when the driver reloads, so they are exported as
  gauges rather than counters.
- The `--include-all` flag exports raw numeric fields under
  `iodrive_<adapter|device|vsu|host>_<field>` for anything not already mapped.
  Useful for discovery; not a stable API.
