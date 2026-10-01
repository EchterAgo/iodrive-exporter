#!/usr/bin/env python3
"""Prometheus exporter for Fusion-io / SanDisk ioDrive PCIe flash devices.

Data source: `fio-status -a -f j -u` (JSON, all fields, including the ones the
device reports as "Unavailable."). The command requires root, so either run
this exporter as root (see the systemd unit in README) or grant a NOPASSWD
sudoers rule for fio-status and pass --sudo.

Every scrape runs fio-status (it is fast, ~20 ms once the sudo ticket is warm)
unless --cache-ttl is set, in which case results are reused for that many
seconds.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    Metric,
)

log = logging.getLogger("iodrive-exporter")

NAMESPACE = "iodrive"

# Matches a leading integer/float, tolerating values like
# "1370051460 (2013-06-01)", "0 Center", "97.00", "-0.49".
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_number(value: object) -> Optional[float]:
    """Return a float for a fio-status field, or None if not numeric.

    fio-status encodes everything as strings and uses "Unavailable." (and
    sometimes "") for fields a given device/firmware does not report.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower().startswith("unavail"):
        return None
    m = _NUM_RE.match(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


@dataclass
class Spec:
    """Mapping from a fio-status JSON field to a Prometheus metric."""

    metric: str
    kind: str = "gauge"  # "gauge" or "counter"
    help: str = ""
    scale: float = 1.0  # multiply raw value (e.g. MB -> bytes)


def _specs(entries: Iterable[tuple]) -> dict[str, Spec]:
    out = {}
    for field_name, metric, kind, help_text, *rest in entries:
        scale = rest[0] if rest else 1.0
        out[field_name] = Spec(metric, kind, help_text, scale)
    return out


# --- Adapter-level fields (adapter[]) -------------------------------------
ADAPTER_SPECS = _specs([
    ("bus_amps", f"{NAMESPACE}_bus_amps", "gauge", "PCIe slot bus current (A)."),
    ("bus_volts", f"{NAMESPACE}_bus_volts", "gauge", "PCIe slot bus voltage (V)."),
    ("bus_watts", f"{NAMESPACE}_bus_watts", "gauge", "PCIe slot bus power draw (W)."),
    ("external_power_connected", f"{NAMESPACE}_external_power_connected", "gauge",
     "1 if auxiliary/external power is connected."),
    ("external_power_override", f"{NAMESPACE}_external_power_override", "gauge",
     "1 if external power requirement is overridden."),
    ("has_pcie_errors", f"{NAMESPACE}_adapter_pcie_errors", "gauge",
     "1 if the adapter PCIe link reports any errors."),
    ("has_pcie_correctable_errors", f"{NAMESPACE}_adapter_pcie_correctable_errors", "gauge",
     "1 if the adapter PCIe link reports correctable errors."),
    ("has_pcie_nonfatal_errors", f"{NAMESPACE}_adapter_pcie_nonfatal_errors", "gauge",
     "1 if the adapter PCIe link reports non-fatal errors."),
    ("has_pcie_fatal_errors", f"{NAMESPACE}_adapter_pcie_fatal_errors", "gauge",
     "1 if the adapter PCIe link reports fatal errors."),
    ("has_pcie_unrecognized_reqs", f"{NAMESPACE}_adapter_pcie_unrecognized_reqs", "gauge",
     "1 if the adapter PCIe link reports unrecognized requests."),
    ("pcie_bandwidth_mbps", f"{NAMESPACE}_pcie_bandwidth_mbps", "gauge",
     "Negotiated PCIe bandwidth (Mb/s)."),
    ("pcie_link_lanes", f"{NAMESPACE}_pcie_link_lanes", "gauge", "Negotiated PCIe lane count."),
    ("pcie_link_speed_gtps", f"{NAMESPACE}_pcie_link_speed_gtps", "gauge",
     "Negotiated PCIe link speed (GT/s)."),
    ("pcie_slot_power_watts", f"{NAMESPACE}_pcie_slot_power_watts", "gauge",
     "Power budget of the PCIe slot (W)."),
    ("power_limit_watts", f"{NAMESPACE}_power_limit_watts", "gauge", "Adapter power limit (W)."),
    ("power_monitor_enabled", f"{NAMESPACE}_power_monitor_enabled", "gauge",
     "1 if the adapter power monitor is enabled."),
    ("required_pcie_bandwidth_mbps", f"{NAMESPACE}_required_pcie_bandwidth_mbps", "gauge",
     "PCIe bandwidth the adapter requires (Mb/s)."),
    ("num_expected_iom", f"{NAMESPACE}_expected_iomemory_count", "gauge",
     "Number of iomemory modules expected in the adapter."),
])

# --- iomemory / device-level fields (adapter[].iomemory[]) ----------------
DEVICE_SPECS = _specs([
    ("active_media_pct", f"{NAMESPACE}_active_media_percent", "gauge",
     "Percentage of NAND media still in active use."),
    ("temp_internal_deg_c", f"{NAMESPACE}_temp_internal_celsius", "gauge",
     "Internal (controller/FPGA) temperature (C)."),
    ("temp_internal_peak_deg_c", f"{NAMESPACE}_temp_internal_peak_celsius", "gauge",
     "Peak internal temperature since power-on (C)."),
    ("temp_internal_warn_deg_c", f"{NAMESPACE}_temp_internal_warn_celsius", "gauge",
     "Internal temperature warning threshold (C)."),
    ("nand_thermal_deg_c", f"{NAMESPACE}_nand_thermal_celsius", "gauge",
     "NAND package temperature (C)."),
    ("nand_throttle_start_deg_c", f"{NAMESPACE}_nand_throttle_start_celsius", "gauge",
     "NAND throttling start temperature (C)."),
    ("nand_thermal_threshold_deg_c", f"{NAMESPACE}_nand_thermal_threshold_celsius", "gauge",
     "NAND thermal threshold temperature (C)."),
    ("controller_threshold_max_deg_c", f"{NAMESPACE}_controller_threshold_max_celsius", "gauge",
     "Controller maximum threshold temperature (C)."),
    ("controller_throttle_start_deg_c", f"{NAMESPACE}_controller_throttle_start_celsius", "gauge",
     "Controller throttling start temperature (C)."),
    ("thermal_threshold_deg_c", f"{NAMESPACE}_thermal_threshold_celsius", "gauge",
     "Overall thermal threshold temperature (C)."),
    ("rated_writes_remaining_percent", f"{NAMESPACE}_rated_writes_remaining_percent", "gauge",
     "Remaining rated write endurance (%). Lifetime-left proxy."),
    ("reserve_space_pct", f"{NAMESPACE}_reserve_space_percent", "gauge",
     "Remaining spare/reserve space (%)."),
    ("reserve_warning_threshold_percent", f"{NAMESPACE}_reserve_warning_threshold_percent", "gauge",
     "Reserve space warning threshold (%)."),
    ("factory_size_bytes", f"{NAMESPACE}_factory_size_bytes", "gauge",
     "Factory formatted capacity (bytes)."),
    ("size_bytes", f"{NAMESPACE}_size_bytes", "gauge", "Usable device size (bytes)."),
    ("max_overformat_size_bytes", f"{NAMESPACE}_max_overformat_size_bytes", "gauge",
     "Maximum overformat capacity (bytes)."),
    ("shipped_capacity_bytes", f"{NAMESPACE}_shipped_capacity_bytes", "gauge",
     "Shipped capacity (bytes)."),
    ("sector_count", f"{NAMESPACE}_sector_count", "gauge", "Sector count."),
    ("sector_size_bytes", f"{NAMESPACE}_sector_size_bytes", "gauge", "Sector size (bytes)."),
    ("ram_usage_current_bytes", f"{NAMESPACE}_ram_usage_current_bytes", "gauge",
     "Current host RAM used by the driver (bytes)."),
    ("ram_usage_peak_bytes", f"{NAMESPACE}_ram_usage_peak_bytes", "gauge",
     "Peak host RAM used by the driver (bytes)."),
    ("prealloc_ram_required_mb", f"{NAMESPACE}_prealloc_ram_required_bytes", "gauge",
     "RAM required if preallocation is enabled (bytes).", 1_000_000.0),
    ("session_read_ops", f"{NAMESPACE}_session_read_ops", "gauge",
     "Read operations since driver load (resets on reload)."),
    ("session_write_ops", f"{NAMESPACE}_session_write_ops", "gauge",
     "Write operations since driver load (resets on reload)."),
    ("session_copied_bytes", f"{NAMESPACE}_session_copied_bytes", "gauge",
     "Bytes copied (GC) since driver load (resets on reload)."),
    ("session_erased_bytes", f"{NAMESPACE}_session_erased_bytes", "gauge",
     "Bytes erased since driver load (resets on reload)."),
    ("total_physical_bytes_read", f"{NAMESPACE}_physical_bytes_read_total", "counter",
     "Lifetime physical bytes read."),
    ("total_physical_bytes_written", f"{NAMESPACE}_physical_bytes_written_total", "counter",
     "Lifetime physical bytes written (includes write amplification)."),
    ("write_pwr_throttling_count", f"{NAMESPACE}_write_power_throttle_events", "counter",
     "Cumulative write throttling events due to power."),
    ("write_thermal_throttling_count", f"{NAMESPACE}_write_thermal_throttle_events", "counter",
     "Cumulative write throttling events due to thermals."),
    ("vccaux_avg_volts", f"{NAMESPACE}_vccaux_avg_volts", "gauge", "VCC AUX average voltage (V)."),
    ("vccaux_min_volts", f"{NAMESPACE}_vccaux_min_volts", "gauge", "VCC AUX minimum voltage (V)."),
    ("vccaux_peak_volts", f"{NAMESPACE}_vccaux_peak_volts", "gauge", "VCC AUX peak voltage (V)."),
    ("vccint_avg_volts", f"{NAMESPACE}_vccint_avg_volts", "gauge", "VCC INT average voltage (V)."),
    ("vccint_min_volts", f"{NAMESPACE}_vccint_min_volts", "gauge", "VCC INT minimum voltage (V)."),
    ("vccint_peak_volts", f"{NAMESPACE}_vccint_peak_volts", "gauge", "VCC INT peak voltage (V)."),
    ("powerloss_protect_avail", f"{NAMESPACE}_powerloss_protect_available", "gauge",
     "1 if power-loss protection is available."),
    ("powerloss_protect_enabled", f"{NAMESPACE}_powerloss_protect_enabled", "gauge",
     "1 if power-loss protection is enabled."),
    ("prealloc_enabled", f"{NAMESPACE}_prealloc_enabled", "gauge", "1 if preallocation is enabled."),
    ("beacon_state", f"{NAMESPACE}_beacon_state", "gauge", "Beacon/locate LED state."),
    ("upgrade_in_progress", f"{NAMESPACE}_upgrade_in_progress", "gauge",
     "1 if a firmware upgrade is in progress."),
    ("has_pcie_errors", f"{NAMESPACE}_pcie_errors", "gauge", "1 if the device PCIe link has errors."),
    ("has_pcie_correctable_errors", f"{NAMESPACE}_pcie_correctable_errors", "gauge",
     "1 if the device PCIe link reports correctable errors."),
    ("has_pcie_nonfatal_errors", f"{NAMESPACE}_pcie_nonfatal_errors", "gauge",
     "1 if the device PCIe link reports non-fatal errors."),
    ("has_pcie_fatal_errors", f"{NAMESPACE}_pcie_fatal_errors", "gauge",
     "1 if the device PCIe link reports fatal errors."),
    ("has_pcie_unrecognized_reqs", f"{NAMESPACE}_pcie_unrecognized_reqs", "gauge",
     "1 if the device PCIe link reports unrecognized requests."),
    ("nand_module_count", f"{NAMESPACE}_nand_module_count", "gauge", "Number of populated NAND modules."),
    ("hardware_generation", f"{NAMESPACE}_hardware_generation", "gauge", "Hardware generation."),
    ("ioctl_api_version_major", f"{NAMESPACE}_ioctl_api_version_major", "gauge",
     "Driver ioctl API major version."),
    ("ioctl_api_version_minor", f"{NAMESPACE}_ioctl_api_version_minor", "gauge",
     "Driver ioctl API minor version."),
    ("virtual_controller_active_count", f"{NAMESPACE}_virtual_controller_active_count", "gauge",
     "Number of active virtual controllers."),
])

# --- VSU-level fields (adapter[].iomemory[].vsu[]) ------------------------
VSU_SPECS = _specs([
    ("size_bytes", f"{NAMESPACE}_vsu_size_bytes", "gauge", "Virtual storage unit size (bytes)."),
    ("sector_count", f"{NAMESPACE}_vsu_sector_count", "gauge", "Virtual storage unit sector count."),
    ("sector_size_bytes", f"{NAMESPACE}_vsu_sector_size_bytes", "gauge",
     "Virtual storage unit sector size (bytes)."),
    ("in_use_physical_sectors", f"{NAMESPACE}_vsu_in_use_physical_sectors", "gauge",
     "Physical sectors in use by the VSU."),
    ("max_physical_allowed_sectors", f"{NAMESPACE}_vsu_max_physical_allowed_sectors", "gauge",
     "Maximum physical sectors allowed for the VSU."),
    ("min_physical_reserved_sectors", f"{NAMESPACE}_vsu_min_physical_reserved_sectors", "gauge",
     "Minimum physical sectors reserved for the VSU."),
])

# --- Host-level fields (host[]) -------------------------------------------
HOST_SPECS = _specs([
    ("ram_available_kernel_bytes", f"{NAMESPACE}_host_ram_available_kernel_bytes", "gauge",
     "Kernel-available RAM (bytes)."),
    ("ram_available_physical_bytes", f"{NAMESPACE}_host_ram_available_physical_bytes", "gauge",
     "Available physical RAM (bytes)."),
    ("ram_available_virtual_bytes", f"{NAMESPACE}_host_ram_available_virtual_bytes", "gauge",
     "Available virtual RAM (bytes)."),
    ("ram_total_physical_bytes", f"{NAMESPACE}_host_ram_total_physical_bytes", "gauge",
     "Total physical RAM (bytes)."),
    ("ram_total_virtual_bytes", f"{NAMESPACE}_host_ram_total_virtual_bytes", "gauge",
     "Total virtual RAM (bytes)."),
    ("driver_loaded", f"{NAMESPACE}_host_driver_loaded", "gauge", "1 if the Fusion driver is loaded."),
    ("fusion_trim_running", f"{NAMESPACE}_host_fusion_trim_running", "gauge",
     "1 if Fusion trim is running."),
    ("os_trim_active", f"{NAMESPACE}_host_os_trim_active", "gauge", "1 if OS trim is active."),
    ("os_trim_supported", f"{NAMESPACE}_host_os_trim_supported", "gauge", "1 if OS trim is supported."),
    ("trim_enabled", f"{NAMESPACE}_host_trim_enabled", "gauge", "1 if trim is enabled."),
    ("is_cluster_master", f"{NAMESPACE}_host_cluster_master", "gauge", "1 if host is cluster master."),
])


def _as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _first(item: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = item.get(k)
        if v not in (None, "") and not str(v).lower().startswith("unavail"):
            return str(v)
    return default


@dataclass
class CollectorConfig:
    fio_status_path: str = "fio-status"
    use_sudo: bool = False
    timeout: float = 30.0
    cache_ttl: float = 0.0
    from_file: Optional[str] = None
    include_all: bool = False


class IoDriveCollector:
    """prometheus_client collector that shells out to fio-status."""

    def __init__(self, cfg: CollectorConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None
        self._cache_at = 0.0

    # -- data acquisition --------------------------------------------------
    def _build_command(self) -> list[str]:
        argv = [self.cfg.fio_status_path, "-a", "-f", "j", "-u"]
        if self.cfg.use_sudo:
            argv = ["sudo", "-n", *argv]
        return argv

    def _run_fio_status(self) -> dict:
        if self.cfg.from_file:
            with open(self.cfg.from_file) as fh:
                return json.load(fh)
        cmd = self._build_command()
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.cfg.timeout,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"{cmd[0] if len(cmd) == 1 else ' '.join(cmd[:1])} exited {proc.returncode}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        out = proc.stdout.strip()
        if not out:
            raise RuntimeError("fio-status produced no output")
        return json.loads(out)

    def _get_data(self) -> dict:
        now = time.monotonic()
        with self._lock:
            if (
                self.cfg.cache_ttl > 0
                and self._cache is not None
                and (now - self._cache_at) < self.cfg.cache_ttl
            ):
                return self._cache
            data = self._run_fio_status()
            self._cache = data
            self._cache_at = now
            return data

    # -- metric helpers ----------------------------------------------------
    @staticmethod
    def _emit(specs: dict[str, Spec], item: dict, labelnames: list[str],
              label_values: list[str], families: dict[str, Metric],
              include_all_prefix: Optional[str] = None):
        emitted_names: set[str] = set()
        for field_name, spec in specs.items():
            if field_name not in item:
                continue
            val = parse_number(item.get(field_name))
            if val is None:
                continue
            fam = families.get(spec.metric)
            if fam is None:
                cls = CounterMetricFamily if spec.kind == "counter" else GaugeMetricFamily
                fam = cls(spec.metric, spec.help or spec.metric, labels=labelnames)
                families[spec.metric] = fam
            fam.add_metric(label_values, val * spec.scale)
            emitted_names.add(field_name)

        if include_all_prefix is not None:
            for field_name, raw in item.items():
                if field_name in emitted_names:
                    continue
                if isinstance(raw, (dict, list)):
                    continue
                val = parse_number(raw)
                if val is None:
                    continue
                metric = f"{NAMESPACE}_{include_all_prefix}_{field_name}"
                fam = families.get(metric)
                if fam is None:
                    fam = GaugeMetricFamily(
                        metric, f"Raw fio-status field '{field_name}'.", labels=labelnames)
                    families[metric] = fam
                fam.add_metric(label_values, val)

    @staticmethod
    def _ensure_info(families: dict[str, Metric], metric: str, help_text: str,
                     labelnames: list[str]) -> GaugeMetricFamily:
        """Return the info family, creating it once so multiple devices accumulate."""
        fam = families.get(metric)
        if fam is None:
            fam = GaugeMetricFamily(metric, help_text, labels=labelnames)
            families[metric] = fam
        return fam

    # -- prometheus entrypoint --------------------------------------------
    def collect(self) -> Iterator[Metric]:
        up = GaugeMetricFamily(
            f"{NAMESPACE}_up", "1 if fio-status ran and parsed successfully.")
        collect_duration = GaugeMetricFamily(
            f"{NAMESPACE}_scrape_seconds", "Seconds spent running fio-status.")
        families: dict[str, Metric] = {}

        t0 = time.monotonic()
        try:
            data = self._get_data()
            up.add_metric([], 1)
        except Exception as exc:  # noqa: BLE001 - report any failure as up=0
            log.warning("fio-status failed: %s", exc)
            up.add_metric([], 0)
            collect_duration.add_metric([], time.monotonic() - t0)
            yield up
            yield collect_duration
            return
        collect_duration.add_metric([], time.monotonic() - t0)

        # host[] (machine-global, no per-device labels)
        for host in _as_list(data.get("host")):
            if not isinstance(host, dict):
                continue
            self._emit(HOST_SPECS, host, [], [], families,
                       include_all_prefix="host" if self.cfg.include_all else None)

        # adapter[] -> iomemory[] -> vsu[]
        adapter_labelnames = ["serial", "board_name"]
        device_labelnames = ["serial", "pci_addr"]
        vsu_labelnames = ["serial", "pci_addr", "vsu"]
        for adapter in _as_list(data.get("adapter")):
            if not isinstance(adapter, dict):
                continue
            a_serial = _first(adapter, "serial_number", "fio_serial_number")
            a_board = _first(adapter, "board_name")
            adapter_labels = [a_serial, a_board]
            self._emit(ADAPTER_SPECS, adapter, adapter_labelnames, adapter_labels, families,
                       include_all_prefix="adapter" if self.cfg.include_all else None)

            ainfo = self._ensure_info(
                families, f"{NAMESPACE}_adapter_info",
                "Static adapter attributes; always 1.",
                ["serial", "board_name", "board_kind", "product_name", "oem",
                 "part_number_sku", "external_power_requirement",
                 "powercut_holdup_type", "pcie_bandwidth_compatibility"])
            ainfo.add_metric([
                a_serial, a_board,
                _first(adapter, "board_kind"),
                _first(adapter, "product_name"),
                _first(adapter, "oem"),
                _first(adapter, "part_number_sku"),
                _first(adapter, "external_power_requirement"),
                _first(adapter, "powercut_holdup_type"),
                _first(adapter, "pcie_bandwidth_compatibility"),
            ], 1)

            for dev in _as_list(adapter.get("iomemory")):
                if not isinstance(dev, dict):
                    continue
                serial = _first(dev, "serial_number", "fio_serial_number", default=a_serial)
                pci = _first(dev, "pci_addr")
                device_labels = [serial, pci]
                self._emit(DEVICE_SPECS, dev, device_labelnames, device_labels, families,
                           include_all_prefix="device" if self.cfg.include_all else None)

                devinfo = self._ensure_info(
                    families, f"{NAMESPACE}_device_info",
                    "Static device attributes; always 1.",
                    ["serial", "pci_addr", "device_name", "product_name",
                     "oem", "fw_version", "driver_version", "iom_state",
                     "reserve_status", "write_throttling_state",
                     "thermal_throttle_reason", "powercut_holdup_type"])
                devinfo.add_metric([
                    serial, pci,
                    _first(dev, "device_name"),
                    _first(dev, "product_name"),
                    _first(dev, "oem"),
                    _first(dev, "fw_current_version"),
                    _first(dev, "driver_version"),
                    _first(dev, "iom_state"),
                    _first(dev, "reserve_status"),
                    _first(dev, "write_throttling_state"),
                    _first(dev, "thermal_throttle_reason"),
                    _first(dev, "powercut_holdup_type"),
                ], 1)

                for vsu in _as_list(dev.get("vsu")):
                    if not isinstance(vsu, dict):
                        continue
                    vsu_name = _first(vsu, "device_name")
                    vsu_labels = [serial, pci, vsu_name]
                    self._emit(VSU_SPECS, vsu, vsu_labelnames, vsu_labels, families,
                               include_all_prefix="vsu" if self.cfg.include_all else None)

                    vinfo = self._ensure_info(
                        families, f"{NAMESPACE}_vsu_info",
                        "Static virtual storage unit attributes; always 1.",
                        ["serial", "pci_addr", "vsu", "device_path", "vsu_state", "vsu_type"])
                    vinfo.add_metric([
                        serial, pci, vsu_name,
                        _first(vsu, "device_path"),
                        _first(vsu, "vsu_state"),
                        _first(vsu, "vsu_type"),
                    ], 1)

        yield up
        yield collect_duration
        yield from families.values()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Prometheus exporter for Fusion-io ioDrive.")
    p.add_argument("--web.listen-address", dest="listen", default="0.0.0.0",
                   help="Address to listen on (default: %(default)s).")
    p.add_argument("--web.port", dest="port", type=int, default=9895,
                   help="Port to listen on (default: %(default)s).")
    p.add_argument("--fio-status.path", dest="fio_status_path", default="fio-status",
                   help="Path to the fio-status binary (default: %(default)s).")
    p.add_argument("--sudo", action="store_true",
                   help="Invoke fio-status via 'sudo -n' (needs a NOPASSWD rule).")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="Timeout for each fio-status invocation, seconds (default: %(default)s).")
    p.add_argument("--cache-ttl", dest="cache_ttl", type=float, default=0.0,
                   help="Reuse fio-status results for this many seconds (0 = run every scrape).")
    p.add_argument("--from-file", dest="from_file",
                   help="Read fio-status JSON from a file instead of running it (for testing).")
    p.add_argument("--include-all", action="store_true",
                   help="Also export every numeric fio-status field as iodrive_<section>_<field>.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging verbosity (default: %(default)s).")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = CollectorConfig(
        fio_status_path=args.fio_status_path,
        use_sudo=args.sudo,
        timeout=args.timeout,
        cache_ttl=args.cache_ttl,
        from_file=args.from_file,
        include_all=args.include_all,
    )

    if not cfg.from_file:
        resolved = shutil.which(cfg.fio_status_path) or (
            cfg.fio_status_path if cfg.use_sudo else None)
        if resolved is None:
            log.error("fio-status not found at %r (pass --fio-status.path or --sudo)",
                      cfg.fio_status_path)
            return 2

    REGISTRY.register(IoDriveCollector(cfg))
    start_http_server(args.port, addr=args.listen)
    log.info("iodrive-exporter listening on %s:%d (source=%s)",
             args.listen, args.port,
             cfg.from_file or ("sudo fio-status" if cfg.use_sudo else cfg.fio_status_path))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
