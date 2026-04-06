#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║           LoopGen — Production-Grade Loopback Interface Manager              ║
║                        with FRR Routing Integration                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Version   : 2.9.7                                                           ║
║  Platform  : Ubuntu Linux 20.04 / 22.04 / 24.04                              ║
║  Python    : 3.8 — 3.12                                                      ║
║  FRR       : 8.x / 9.x / 10.x  (optional — gracefully disabled if absent)    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  CHANGELOG v2.9.7                                                            ║
║  ─────────────────                                                           ║
║  FIX — VRF deletion does not fully clean FRR configuration:                  ║
║                                                                              ║
║  Root cause:                                                                 ║
║    remove_vrf() was set non-fatal (always returns True) in v2.9.4 to         ║
║    handle the "Only inactive VRFs can be deleted" race condition.            ║
║    This meant FRR VRF stanza removal silently did nothing when it failed.    ║
║    Additionally, FRR BGP and OSPF router instances for the VRF were          ║
║    never removed, leaving stale 'router bgp X vrf Y' and                     ║
║    'router ospf vrf Y' stanzas in the running config.                        ║
║                                                                              ║
║  Fix — FRRManager.remove_vrf_complete():                                     ║
║    New comprehensive VRF removal method that:                                ║
║      1. Removes BGP VRF instance ('no router bgp <asn> vrf <name>')          ║
║      2. Removes OSPF VRF instance ('no router ospf vrf <name>')              ║
║      3. Removes the VRF stanza ('no vrf <name>')                             ║
║      4. Verifies each step by re-reading running config                      ║
║      5. Retries with 'write memory' if initial removal is incomplete         ║
║    Called by VRFManager._delete_vrf() Step 4 (after kernel VRF deletion).    ║
║                                                                              ║
║  Also fixes: remove_vrf() now properly reports failures but continues        ║
║  (non-fatal for VRF stanza after kernel device already deleted).             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ─────────────────────────────────────────────────────────────────────────────
#  STANDARD LIBRARY IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
import warnings
from copy import deepcopy
from datetime import datetime
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    ip_network,
)
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
#  THIRD-PARTY IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
try:
    from pyroute2 import IPRoute
    from pyroute2.netlink.exceptions import NetlinkError
except ImportError:
    sys.exit(
        "[FATAL] pyroute2 not installed.\n"
        "        Run: pip install pyroute2"
    )

try:
    from prettytable import PrettyTable
except ImportError:
    sys.exit(
        "[FATAL] prettytable not installed.\n"
        "        Run: pip install prettytable"
    )

_MISSING = object()
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    TABLE_STYLE = None
    try:
        from prettytable.enums import TableStyle as _PtTableStyle
        _m = getattr(_PtTableStyle, "SINGLE_BORDER", _MISSING)
        if _m is not _MISSING:
            TABLE_STYLE = _m
    except (ImportError, Exception):
        pass
    if TABLE_STYLE is None:
        try:
            import importlib as _il
            _pt = _il.import_module("prettytable")
            _c  = getattr(_pt, "SINGLE_BORDER", _MISSING)
            if _c is not _MISSING:
                TABLE_STYLE = _c
        except Exception:
            pass
    if TABLE_STYLE is None:
        TABLE_STYLE = 11

if TABLE_STYLE is None:
    sys.exit(
        "[FATAL] Cannot resolve PrettyTable style.\n"
        "        Run: pip install --upgrade prettytable"
    )

try:
    from colorama import Back, Fore, Style
    from colorama import init as colorama_init
    colorama_init(autoreset=True)
except ImportError:
    sys.exit(
        "[FATAL] colorama not installed.\n"
        "        Run: pip install colorama"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
STATE_FILE             = Path("/var/tmp/loopgen_state.json")
LOG_FILE               = Path("/var/tmp/loopgen.log")
APP_VERSION            = "2.9.7"
MAX_IFNAME_LEN         = 15
DEFAULT_PREFIX         = "loop"
OSPF_AREA_DEFAULT      = "0.0.0.0"
VRF_ENSLAVE_SETTLE_SEC = 0.1

PIMREG_VANISH_TIMEOUT  = 8.0
PIMREG_POLL_INTERVAL   = 0.5

RESERVED_NETWORKS = [
    ip_network("0.0.0.0/8"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),
    ip_network("224.0.0.0/4"),
    ip_network("240.0.0.0/4"),
    ip_network("255.255.255.255/32"),
]

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("loopgen")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] "
        "%(funcName)s:%(lineno)d — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except PermissionError:
        pass
    return logger

log = setup_logging()

# ─────────────────────────────────────────────────────────────────────────────
#  COLOR / OUTPUT HELPERS
# ─────────────────────────────────────────────────────────────────────────────
class C:
    HEADER  = Fore.CYAN   + Style.BRIGHT
    SUCCESS = Fore.GREEN  + Style.BRIGHT
    ERROR   = Fore.RED    + Style.BRIGHT
    WARN    = Fore.YELLOW
    INFO    = Fore.WHITE
    DIM     = Style.DIM
    NEW_IF  = Fore.WHITE  + Back.BLACK + Style.DIM
    PROMPT  = Fore.MAGENTA + Style.BRIGHT
    RESET   = Style.RESET_ALL
    BOLD    = Style.BRIGHT
    CYAN    = Fore.CYAN


def print_header(text: str) -> None:
    width = 64
    print(
        f"\n{C.HEADER}{'═' * width}\n"
        f"  {text}\n"
        f"{'═' * width}{C.RESET}"
    )


def print_success(text: str) -> None:
    print(f"{C.SUCCESS}✔  {text}{C.RESET}")


def print_error(text: str) -> None:
    print(f"{C.ERROR}✘  {text}{C.RESET}")
    log.error(text)


def print_warn(text: str) -> None:
    print(f"{C.WARN}⚠  {text}{C.RESET}")
    log.warning(text)


def print_info(text: str) -> None:
    print(f"{C.INFO}ℹ  {text}{C.RESET}")
    log.info(text)


def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(
            f"{C.PROMPT}  ➤  {text}{suffix}: {C.RESET}"
        ).strip()
        return val if val else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def make_table(*field_names: str) -> PrettyTable:
    tbl = PrettyTable()
    tbl.set_style(TABLE_STYLE)
    tbl.field_names = list(field_names)
    tbl.align = "l"
    return tbl

# ─────────────────────────────────────────────────────────────────────────────
#  INTERFACE NAMING / CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────
def generate_ifname(prefix: str, number: int) -> str:
    safe   = re.sub(r"[^a-zA-Z0-9]", "", prefix) or DEFAULT_PREFIX
    numstr = str(number).zfill(3)
    return f"{safe}{numstr}"[:MAX_IFNAME_LEN]


def next_available_number(
    prefix: str, existing_names: List[str]
) -> int:
    safe    = re.sub(r"[^a-zA-Z0-9]", "", prefix) or DEFAULT_PREFIX
    pattern = re.compile(rf"^{re.escape(safe)}(\d+)$")
    used: set = set()
    for name in existing_names:
        m = pattern.match(name)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return n


def is_frr_internal(ifname: str) -> bool:
    return bool(re.match(r"^pim6?reg\d*$", ifname))


def is_vrf_device(ifname: str, vrfs: Dict) -> bool:
    return ifname in vrfs


def is_selectable_interface(
    ifname: str, vrfs: Dict
) -> bool:
    if ifname == "lo":
        return False
    if is_frr_internal(ifname):
        return False
    if is_vrf_device(ifname, vrfs):
        return False
    return True


def is_display_interface(ifname: str, vrfs: Dict) -> bool:
    if ifname == "lo":
        return False
    if is_frr_internal(ifname):
        return False
    if is_vrf_device(ifname, vrfs):
        return False
    return True

# ─────────────────────────────────────────────────────────────────────────────
#  STATE MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class StateManager:

    def __init__(self, path: Path = STATE_FILE):
        self.path   = path
        self._state: Dict = {
            "version":    APP_VERSION,
            "interfaces": {},
            "vrfs":       {},
        }
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if "interfaces" in data:
                    self._state = data
                    self._state.setdefault("vrfs", {})
                    log.debug(
                        f"State loaded: "
                        f"{len(self._state['interfaces'])} interfaces, "
                        f"{len(self._state['vrfs'])} VRFs"
                    )
                else:
                    print_warn("State schema mismatch — starting fresh.")
            except (json.JSONDecodeError, OSError) as exc:
                print_warn(f"State load error ({exc}) — starting fresh.")

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=2, default=str)
            tmp.rename(self.path)
        except OSError as exc:
            print_error(f"State save failed: {exc}")

    def add(
        self,
        ifname:      str,
        ip:          str,
        prefix_len:  int,
        vrf:         str,
        tag:         str,
        protocol:    str,
        ospf_method: str = "none",
        ospf_area:   str = "0.0.0.0",
        bgp_asn:     str = "",
    ) -> None:
        self._state["interfaces"][ifname] = {
            "interface":   ifname,
            "ip":          ip,
            "prefix_len":  prefix_len,
            "vrf":         vrf,
            "tag":         tag,
            "protocol":    protocol,
            "ospf_method": ospf_method,
            "ospf_area":   ospf_area,
            "bgp_asn":     bgp_asn,
            "created_at":  datetime.utcnow().isoformat() + "Z",
        }
        self.save()

    def update(self, ifname: str, **kwargs) -> None:
        if ifname in self._state["interfaces"]:
            self._state["interfaces"][ifname].update(kwargs)
            self.save()

    def remove(self, ifname: str) -> None:
        self._state["interfaces"].pop(ifname, None)
        self.save()

    def get_all(self) -> Dict:
        return deepcopy(self._state["interfaces"])

    def exists(self, ifname: str) -> bool:
        return ifname in self._state["interfaces"]

    def get_all_ips(self) -> List[str]:
        return [v["ip"] for v in self._state["interfaces"].values()]

    def get_all_names(self) -> List[str]:
        return list(self._state["interfaces"].keys())

    def get_by_tag(self, tag: str) -> List[str]:
        return [
            k for k, v in self._state["interfaces"].items()
            if v.get("tag") == tag
        ]

    def get_by_vrf(self, vrf: str) -> List[Dict]:
        return [
            v for v in self._state["interfaces"].values()
            if v.get("vrf") == vrf
        ]

    def add_vrf(self, vrf_name: str, table_id: int) -> None:
        self._state["vrfs"][vrf_name] = {
            "table":      table_id,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        self.save()

    def remove_vrf(self, vrf_name: str) -> None:
        self._state["vrfs"].pop(vrf_name, None)
        self.save()

    def get_all_vrfs(self) -> Dict:
        return deepcopy(self._state.get("vrfs", {}))

    def vrf_exists(self, vrf_name: str) -> bool:
        return vrf_name in self._state.get("vrfs", {})

    def get_used_table_ids(self) -> List[int]:
        return [
            v["table"]
            for v in self._state.get("vrfs", {}).values()
            if isinstance(v.get("table"), int)
        ]

# ─────────────────────────────────────────────────────────────────────────────
#  KERNEL / VRF MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class KernelManager:

    def __init__(self):
        if os.geteuid() != 0:
            sys.exit(f"{C.ERROR}[FATAL] Must run as root.{C.RESET}")

    def get_vrfs(self) -> Dict[str, Dict]:
        vrfs: Dict[str, Dict] = {}
        try:
            with IPRoute() as ipr:
                for link in ipr.get_links():
                    li = link.get_attr("IFLA_LINKINFO")
                    if not li or li.get_attr("IFLA_INFO_KIND") != "vrf":
                        continue
                    name      = link.get_attr("IFLA_IFNAME")
                    info_data = li.get_attr("IFLA_INFO_DATA")
                    table_id  = (
                        info_data.get_attr("IFLA_VRF_TABLE")
                        if info_data else None
                    )
                    vrfs[name] = {
                        "table":   table_id,
                        "ifindex": link.get("index", 0),
                    }
        except NetlinkError as exc:
            print_error(f"VRF discovery error: {exc}")
        return vrfs

    def get_all_interfaces(self) -> List[Dict]:
        result: List[Dict] = []
        try:
            with IPRoute() as ipr:
                addr_map: Dict[int, List] = {}
                for addr in ipr.get_addr(family=2):
                    idx    = addr.get("index")
                    ip_str = addr.get_attr("IFA_ADDRESS")
                    plen   = addr.get("prefixlen")
                    if ip_str and plen is not None:
                        addr_map.setdefault(idx, []).append(
                            {"ip": ip_str, "prefix_len": plen}
                        )
                for link in ipr.get_links():
                    idx   = link.get("index")
                    name  = link.get_attr("IFLA_IFNAME") or f"if{idx}"
                    flags = link.get("flags", 0)
                    result.append({
                        "name":       name,
                        "ifindex":    idx,
                        "state":      "UP" if (flags & 1) else "DOWN",
                        "master_idx": link.get_attr("IFLA_MASTER"),
                        "addresses":  addr_map.get(idx, []),
                    })
        except NetlinkError as exc:
            print_error(f"Interface enumeration error: {exc}")
        return result

    def get_enslaved_interfaces(
        self, vrf_ifindex: int
    ) -> List[Dict]:
        return [
            i for i in self.get_all_interfaces()
            if i.get("master_idx") == vrf_ifindex
        ]

    def interface_exists(self, ifname: str) -> bool:
        try:
            with IPRoute() as ipr:
                return bool(ipr.link_lookup(ifname=ifname))
        except NetlinkError:
            return False

    def get_all_kernel_ips(self) -> List[str]:
        ips: List[str] = []
        try:
            with IPRoute() as ipr:
                for addr in ipr.get_addr(family=2):
                    ip_str = addr.get_attr("IFA_ADDRESS")
                    if ip_str:
                        ips.append(ip_str)
        except NetlinkError as exc:
            log.error(f"Kernel IP query error: {exc}")
        return ips

    def get_interface_ips(self, ifname: str) -> List[Dict]:
        result: List[Dict] = []
        try:
            with IPRoute() as ipr:
                idxs = ipr.link_lookup(ifname=ifname)
                if not idxs:
                    return result
                for addr in ipr.get_addr(family=2, index=idxs[0]):
                    ip_str = addr.get_attr("IFA_ADDRESS")
                    plen   = addr.get("prefixlen")
                    if ip_str and plen is not None:
                        result.append(
                            {"ip": ip_str, "prefix_len": plen}
                        )
        except NetlinkError as exc:
            log.error(f"get_interface_ips({ifname}): {exc}")
        return result

    def _get_master_name(
        self, ipr: IPRoute, ifname: str
    ) -> Optional[str]:
        idxs = ipr.link_lookup(ifname=ifname)
        if not idxs:
            return None
        link       = ipr.get_links(idxs[0])[0]
        master_idx = link.get_attr("IFLA_MASTER")
        if not master_idx:
            return None
        masters = ipr.get_links(master_idx)
        return masters[0].get_attr("IFLA_IFNAME") if masters else None

    def create_vrf_device(
        self, vrf_name: str, table_id: int
    ) -> bool:
        if self.interface_exists(vrf_name):
            print_warn(f"VRF device '{vrf_name}' already exists.")
            return False
        try:
            with IPRoute() as ipr:
                ipr.link(
                    "add",
                    ifname=vrf_name,
                    kind="vrf",
                    vrf_table=table_id,
                )
                idx_list = ipr.link_lookup(ifname=vrf_name)
                if not idx_list:
                    print_error(
                        f"Cannot resolve {vrf_name} after creation."
                    )
                    return False
                ipr.link("set", index=idx_list[0], state="up")
            log.info(
                f"VRF device created: {vrf_name} table={table_id}"
            )
            return True
        except NetlinkError as exc:
            print_error(
                f"Failed to create VRF device {vrf_name}: {exc}"
            )
            return False

    def delete_vrf_device(self, vrf_name: str) -> bool:
        if not self.interface_exists(vrf_name):
            print_warn(f"VRF device '{vrf_name}' not in kernel.")
            return True
        try:
            with IPRoute() as ipr:
                idx = ipr.link_lookup(ifname=vrf_name)
                ipr.link("del", index=idx[0])
            log.info(f"VRF device deleted: {vrf_name}")
            return True
        except NetlinkError as exc:
            print_error(f"Failed to delete VRF {vrf_name}: {exc}")
            return False

    def poll_until_interfaces_gone(
        self,
        ifnames:  List[str],
        timeout:  float = PIMREG_VANISH_TIMEOUT,
        interval: float = PIMREG_POLL_INTERVAL,
    ) -> List[str]:
        deadline  = time.monotonic() + timeout
        remaining = list(ifnames)
        while remaining and time.monotonic() < deadline:
            time.sleep(interval)
            remaining = [
                name for name in remaining
                if self.interface_exists(name)
            ]
        return remaining

    def create_vrf_interface(
        self,
        ifname:     str,
        ip:         str,
        prefix_len: int,
        vrf_name:   str,
    ) -> bool:
        if self.interface_exists(ifname):
            print_warn(f"Interface {ifname} already exists.")
            return False
        try:
            with IPRoute() as ipr:
                vrf_idx_list = ipr.link_lookup(ifname=vrf_name)
                if not vrf_idx_list:
                    print_error(f"VRF device '{vrf_name}' not found.")
                    return False
                vrf_idx = vrf_idx_list[0]

                ipr.link("add", ifname=ifname, kind="dummy")
                if_idx_list = ipr.link_lookup(ifname=ifname)
                if not if_idx_list:
                    print_error(
                        f"Cannot resolve {ifname} after creation."
                    )
                    return False
                if_idx = if_idx_list[0]

                ipr.link("set", index=if_idx, master=vrf_idx)
                time.sleep(VRF_ENSLAVE_SETTLE_SEC)

                master = self._get_master_name(ipr, ifname)
                if master != vrf_name:
                    print_error(
                        f"VRF check failed: {ifname} "
                        f"master='{master}' expected='{vrf_name}'."
                    )
                    ipr.link("del", index=if_idx)
                    return False

                ipr.addr(
                    "add",
                    index=if_idx,
                    address=ip,
                    prefixlen=prefix_len,
                )
                ipr.link("set", index=if_idx, state="up")

            log.info(
                f"VRF interface ready: {ifname} "
                f"ip={ip}/{prefix_len} vrf={vrf_name}"
            )
            return True
        except NetlinkError as exc:
            print_error(f"Kernel error {ifname}/{vrf_name}: {exc}")
            self.delete_interface(ifname)
            return False

    def create_grt_interface(
        self,
        ifname:     str,
        ip:         str,
        prefix_len: int,
    ) -> bool:
        if self.interface_exists(ifname):
            print_warn(f"Interface {ifname} already exists.")
            return False
        try:
            with IPRoute() as ipr:
                ipr.link("add", ifname=ifname, kind="dummy")
                if_idx_list = ipr.link_lookup(ifname=ifname)
                if not if_idx_list:
                    print_error(
                        f"Cannot resolve {ifname} after creation."
                    )
                    return False
                if_idx = if_idx_list[0]
                ipr.addr(
                    "add",
                    index=if_idx,
                    address=ip,
                    prefixlen=prefix_len,
                )
                ipr.link("set", index=if_idx, state="up")
            log.info(
                f"GRT interface ready: {ifname} ip={ip}/{prefix_len}"
            )
            return True
        except NetlinkError as exc:
            print_error(f"Kernel error {ifname}: {exc}")
            self.delete_interface(ifname)
            return False

    def delete_interface(self, ifname: str) -> bool:
        if not self.interface_exists(ifname):
            print_warn(f"{ifname} not in kernel — skip delete.")
            return True
        try:
            with IPRoute() as ipr:
                idx = ipr.link_lookup(ifname=ifname)
                ipr.link("del", index=idx[0])
            log.debug(f"Deleted: {ifname}")
            return True
        except NetlinkError as exc:
            print_error(f"Delete {ifname} failed: {exc}")
            return False

    def detach_from_vrf(self, ifname: str) -> bool:
        try:
            with IPRoute() as ipr:
                idxs = ipr.link_lookup(ifname=ifname)
                if not idxs:
                    print_error(f"Interface {ifname} not found.")
                    return False
                ipr.link("set", index=idxs[0], master=0)
            log.info(f"Detached {ifname} from VRF")
            return True
        except NetlinkError as exc:
            print_error(f"detach_from_vrf({ifname}): {exc}")
            return False

    def verify_vrf_membership(
        self, ifname: str, expected_vrf: str
    ) -> bool:
        try:
            with IPRoute() as ipr:
                master = self._get_master_name(ipr, ifname)
            ok = master == expected_vrf
            log.debug(
                f"verify_vrf_membership({ifname}): "
                f"master={master} expected={expected_vrf} → {ok}"
            )
            return ok
        except NetlinkError:
            return False

    def remove_ip_from_if(
        self, ifname: str, ip: str, prefix_len: int
    ) -> bool:
        try:
            with IPRoute() as ipr:
                idxs = ipr.link_lookup(ifname=ifname)
                if not idxs:
                    print_error(f"Interface {ifname} not found.")
                    return False
                ipr.addr(
                    "del",
                    index=idxs[0],
                    address=ip,
                    prefixlen=prefix_len,
                )
            log.info(f"Removed {ip}/{prefix_len} from {ifname}")
            return True
        except NetlinkError as exc:
            print_error(
                f"Cannot remove {ip}/{prefix_len} from {ifname}: {exc}"
            )
            return False

    def add_ip_to_if(
        self, ifname: str, ip: str, prefix_len: int
    ) -> bool:
        try:
            with IPRoute() as ipr:
                idxs = ipr.link_lookup(ifname=ifname)
                if not idxs:
                    print_error(f"Interface {ifname} not found.")
                    return False
                ipr.addr(
                    "add",
                    index=idxs[0],
                    address=ip,
                    prefixlen=prefix_len,
                )
            log.info(f"Added {ip}/{prefix_len} to {ifname}")
            return True
        except NetlinkError as exc:
            if exc.code == 17:
                print_warn(
                    f"{ip}/{prefix_len} already assigned to {ifname}."
                )
                return True
            print_error(
                f"Cannot add {ip}/{prefix_len} to {ifname}: {exc}"
            )
            return False

    def move_to_vrf(
        self, ifname: str, vrf_name: str
    ) -> bool:
        try:
            with IPRoute() as ipr:
                if_idx_list = ipr.link_lookup(ifname=ifname)
                if not if_idx_list:
                    print_error(f"Interface {ifname} not found.")
                    return False
                if_idx = if_idx_list[0]

                vrf_idx_list = ipr.link_lookup(ifname=vrf_name)
                if not vrf_idx_list:
                    print_error(f"VRF device '{vrf_name}' not found.")
                    return False
                vrf_idx = vrf_idx_list[0]

                ipr.link("set", index=if_idx, state="down")
                ipr.link("set", index=if_idx, master=vrf_idx)
                time.sleep(VRF_ENSLAVE_SETTLE_SEC)

                master = self._get_master_name(ipr, ifname)
                if master != vrf_name:
                    print_error(
                        f"VRF move failed: {ifname} "
                        f"master='{master}' expected='{vrf_name}'."
                    )
                    return False

                ipr.link("set", index=if_idx, state="up")

            log.info(f"Moved {ifname} → VRF {vrf_name}")
            return True
        except NetlinkError as exc:
            print_error(f"move_to_vrf({ifname}, {vrf_name}): {exc}")
            return False

# ─────────────────────────────────────────────────────────────────────────────
#  FRR MANAGER  — v2.9.7: remove_vrf_complete() added
# ─────────────────────────────────────────────────────────────────────────────
class FRRManager:

    def __init__(self):
        self._available = self._check_vtysh()

    def _check_vtysh(self) -> bool:
        try:
            r = subprocess.run(
                ["which", "vtysh"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                print_warn("vtysh not found — FRR disabled.")
                return False
            t = subprocess.run(
                ["vtysh", "-c", "show version"],
                capture_output=True, text=True, timeout=10,
            )
            if t.returncode != 0:
                print_warn("vtysh not responsive — FRR disabled.")
                return False
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            print_warn(f"vtysh check: {exc} — FRR disabled.")
            return False

    def is_available(self) -> bool:
        return self._available

    def run_vtysh(
        self, commands: List[str], timeout: int = 30
    ) -> Tuple[bool, str]:
        if not self._available:
            return False, "FRR not available"
        cmd = ["vtysh"]
        for c in commands:
            cmd += ["-c", c]
        log.debug(f"vtysh: {commands}")
        try:
            r   = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            out = r.stdout + r.stderr
            log.debug(f"vtysh rc={r.returncode}\n{out}")
            if r.returncode != 0:
                return False, out
            return True, out
        except subprocess.TimeoutExpired:
            return False, "vtysh timed out"
        except Exception as exc:
            return False, str(exc)

    def get_running_config(
        self, grep: Optional[str] = None
    ) -> str:
        if not self._available:
            return "(FRR not available)"
        cmd = "show running-config"
        if grep:
            cmd += f" | include {grep}"
        ok, out = self.run_vtysh([cmd])
        return out if ok else f"(Error: {out})"

    # ── Interface stanza management ───────────────────────────────────────────

    def remove_interface(self, ifname: str) -> bool:
        """Remove a single interface stanza from FRR (non-fatal)."""
        if not self._available:
            return True
        ok, out = self.run_vtysh([
            "configure terminal",
            f"no interface {ifname}",
            "end",
        ])
        if ok:
            log.info(f"FRR: removed interface stanza: {ifname}")
        else:
            log.debug(
                f"FRR: 'no interface {ifname}' — "
                f"may not have been present: {out.strip()}"
            )
        return True

    def purge_frr_interface_stanzas(
        self, ifnames: List[str]
    ) -> None:
        """Remove multiple interface stanzas from FRR in one pass."""
        if not self._available or not ifnames:
            return
        commands = ["configure terminal"]
        for name in ifnames:
            commands.append(f"no interface {name}")
        commands.append("end")
        ok, out = self.run_vtysh(commands)
        if ok:
            log.info(
                f"FRR: purged interface stanzas: {ifnames}"
            )
        else:
            log.debug(
                f"FRR: purge partial result: {out.strip()}"
            )

    # ── PIM cleanup helpers ───────────────────────────────────────────────────

    def clear_pim_vrf(self, vrf_name: str) -> None:
        if not self._available:
            return
        ok, out = self.run_vtysh([
            f"clear ip pim vrf {vrf_name} interfaces"
        ])
        log.debug(
            f"clear_pim_vrf({vrf_name}): ok={ok} out={out.strip()}"
        )

    def clear_pim_all(self) -> None:
        if not self._available:
            return
        self.run_vtysh(["clear ip pim interfaces"])
        self.run_vtysh(["clear ipv6 pim interfaces"])
        log.debug("clear_pim_all: issued global PIM interface clear")

    # ── VRF stanza management ─────────────────────────────────────────────────

    def vrf_exists_in_frr(self, vrf_name: str) -> bool:
        return f"vrf {vrf_name}" in self.get_running_config()

    def configure_vrf(
        self, vrf_name: str, table_id: int
    ) -> bool:
        if not self._available:
            return True
        ok, out = self.run_vtysh([
            "configure terminal",
            f"vrf {vrf_name}",
            "exit-vrf",
            "end",
        ])
        if not ok:
            print_error(f"FRR VRF config failed: {out}")
        else:
            log.info(f"FRR VRF configured: {vrf_name}")
        return ok

    def remove_vrf(self, vrf_name: str) -> bool:
        """
        Remove only the VRF stanza from FRR.
        Non-fatal: if the kernel VRF device is already gone, FRR may
        have already cleaned up the stanza automatically.
        """
        if not self._available:
            return True
        if not self.vrf_exists_in_frr(vrf_name):
            log.info(
                f"VRF '{vrf_name}' stanza not in FRR — skip"
            )
            return True
        ok, out = self.run_vtysh([
            "configure terminal",
            f"no vrf {vrf_name}",
            "end",
        ])
        if not ok:
            log.warning(
                f"FRR 'no vrf {vrf_name}' returned error "
                f"(non-fatal — kernel VRF already gone): {out.strip()}"
            )
        else:
            log.info(f"FRR VRF stanza removed: {vrf_name}")
        return True   # Always non-fatal at this stage

    def remove_vrf_complete(self, vrf_name: str) -> bool:
        """
        Comprehensive FRR VRF cleanup.  Removes ALL FRR configuration
        associated with a VRF in the correct dependency order:

            1. BGP VRF instance     — 'no router bgp <asn> vrf <name>'
            2. OSPF VRF instance    — 'no router ospf vrf <name>'
            3. VRF stanza           — 'no vrf <name>'
            4. write memory         — persist the removal

        Each step is verified by re-reading running config.
        Steps that are not present in the config are silently skipped.
        Returns True if all present stanzas were removed successfully.

        Called by VRFManager._delete_vrf() AFTER the kernel VRF device
        has been deleted so FRR sees no active interfaces.
        """
        if not self._available:
            log.info(
                f"remove_vrf_complete({vrf_name}): FRR not available"
            )
            return True

        log.info(
            f"remove_vrf_complete: starting full FRR cleanup "
            f"for VRF '{vrf_name}'"
        )
        overall_ok = True

        # ── Step 1: Remove BGP VRF instance ───────────────────────────────
        asn = self.get_bgp_asn()
        if asn:
            bgp_vrf_header = f"router bgp {asn} vrf {vrf_name}"
            full = self.get_running_config()
            if bgp_vrf_header in full:
                print_info(
                    f"    [FRR] Removing BGP instance: "
                    f"router bgp {asn} vrf {vrf_name}"
                )
                log.info(
                    f"remove_vrf_complete: removing BGP VRF instance "
                    f"'router bgp {asn} vrf {vrf_name}'"
                )
                ok, out = self.run_vtysh([
                    "configure terminal",
                    f"no router bgp {asn} vrf {vrf_name}",
                    "end",
                ])
                if ok:
                    # Verify removal
                    full_after = self.get_running_config()
                    if bgp_vrf_header in full_after:
                        log.warning(
                            f"remove_vrf_complete: BGP VRF stanza "
                            f"still present after removal attempt"
                        )
                        overall_ok = False
                    else:
                        print_success(
                            f"    BGP instance removed: "
                            f"router bgp {asn} vrf {vrf_name}"
                        )
                        log.info(
                            f"remove_vrf_complete: BGP VRF instance "
                            f"confirmed removed"
                        )
                else:
                    log.warning(
                        f"remove_vrf_complete: 'no router bgp {asn} "
                        f"vrf {vrf_name}' failed: {out.strip()}"
                    )
                    overall_ok = False
            else:
                log.debug(
                    f"remove_vrf_complete: no BGP VRF instance "
                    f"for '{vrf_name}' — skip"
                )

        # ── Step 2: Remove OSPF VRF instance ──────────────────────────────
        ospf_vrf_header = f"router ospf vrf {vrf_name}"
        full = self.get_running_config()
        if ospf_vrf_header in full:
            print_info(
                f"    [FRR] Removing OSPF instance: "
                f"router ospf vrf {vrf_name}"
            )
            log.info(
                f"remove_vrf_complete: removing OSPF VRF instance "
                f"'router ospf vrf {vrf_name}'"
            )
            ok, out = self.run_vtysh([
                "configure terminal",
                f"no router ospf vrf {vrf_name}",
                "end",
            ])
            if ok:
                full_after = self.get_running_config()
                if ospf_vrf_header in full_after:
                    log.warning(
                        f"remove_vrf_complete: OSPF VRF stanza "
                        f"still present after removal attempt"
                    )
                    overall_ok = False
                else:
                    print_success(
                        f"    OSPF instance removed: "
                        f"router ospf vrf {vrf_name}"
                    )
                    log.info(
                        f"remove_vrf_complete: OSPF VRF instance "
                        f"confirmed removed"
                    )
            else:
                log.warning(
                    f"remove_vrf_complete: 'no router ospf "
                    f"vrf {vrf_name}' failed: {out.strip()}"
                )
                overall_ok = False
        else:
            log.debug(
                f"remove_vrf_complete: no OSPF VRF instance "
                f"for '{vrf_name}' — skip"
            )

        # ── Step 3: Remove VRF stanza ──────────────────────────────────────
        full = self.get_running_config()
        if f"vrf {vrf_name}" in full:
            print_info(
                f"    [FRR] Removing VRF stanza: vrf {vrf_name}"
            )
            log.info(
                f"remove_vrf_complete: removing VRF stanza "
                f"'vrf {vrf_name}'"
            )
            ok, out = self.run_vtysh([
                "configure terminal",
                f"no vrf {vrf_name}",
                "end",
            ])
            if ok:
                full_after = self.get_running_config()
                if f"vrf {vrf_name}" in full_after:
                    # May still appear inside router stanzas as a
                    # reference — check for standalone stanza
                    standalone = bool(
                        re.search(
                            rf"^vrf {re.escape(vrf_name)}\s*$",
                            full_after,
                            re.MULTILINE,
                        )
                    )
                    if standalone:
                        log.warning(
                            f"remove_vrf_complete: VRF stanza still "
                            f"present after removal"
                        )
                        overall_ok = False
                    else:
                        print_success(
                            f"    VRF stanza removed: {vrf_name}"
                        )
                        log.info(
                            f"remove_vrf_complete: VRF stanza "
                            f"confirmed removed"
                        )
                else:
                    print_success(
                        f"    VRF stanza removed: {vrf_name}"
                    )
                    log.info(
                        f"remove_vrf_complete: VRF stanza "
                        f"confirmed removed"
                    )
            else:
                # Non-fatal: stanza may have been auto-removed when
                # the kernel VRF device was deleted
                log.warning(
                    f"remove_vrf_complete: 'no vrf {vrf_name}' "
                    f"returned error (may be harmless): {out.strip()}"
                )
                # Check if it's actually still there
                if f"vrf {vrf_name}" not in self.get_running_config():
                    print_success(
                        f"    VRF stanza already removed by FRR: "
                        f"{vrf_name}"
                    )
                    log.info(
                        f"remove_vrf_complete: VRF stanza was already "
                        f"absent despite error response"
                    )
                else:
                    overall_ok = False
        else:
            log.info(
                f"remove_vrf_complete: VRF stanza for '{vrf_name}' "
                f"already absent from FRR — skip"
            )

        # ── Step 4: Persist to disk ────────────────────────────────────────
        ok_write, _ = self.run_vtysh(["write memory"])
        if ok_write:
            log.info(
                f"remove_vrf_complete: write memory OK"
            )
        else:
            log.warning(
                "remove_vrf_complete: write memory failed "
                "(non-fatal)"
            )

        # ── Summary ────────────────────────────────────────────────────────
        if overall_ok:
            log.info(
                f"remove_vrf_complete: all FRR config for "
                f"'{vrf_name}' removed successfully"
            )
        else:
            log.warning(
                f"remove_vrf_complete: some FRR stanzas for "
                f"'{vrf_name}' may not have been fully removed"
            )

        return overall_ok

    # ── ASN helpers ───────────────────────────────────────────────────────────

    def get_bgp_asn(self) -> Optional[str]:
        full  = self.get_running_config()
        match = re.search(r"^router bgp (\d+)$", full, re.MULTILINE)
        return match.group(1) if match else None

    def get_bgp_asn_for_vrf(
        self, vrf: Optional[str] = None
    ) -> Optional[str]:
        if vrf and vrf != "GRT":
            full  = self.get_running_config()
            match = re.search(
                rf"^router bgp (\d+) vrf {re.escape(vrf)}$",
                full, re.MULTILINE,
            )
            if match:
                return match.group(1)
        return self.get_bgp_asn()

    def _resolve_asn(
        self, vrf: Optional[str], explicit_asn: str
    ) -> Optional[str]:
        candidate = explicit_asn.strip()
        if candidate:
            return candidate
        if vrf and vrf != "GRT":
            full  = self.get_running_config()
            match = re.search(
                rf"^router bgp (\d+) vrf {re.escape(vrf)}$",
                full, re.MULTILINE,
            )
            if match:
                return match.group(1)
        return self.get_bgp_asn()

    # ── Process existence ─────────────────────────────────────────────────────

    def ospf_process_exists(
        self, vrf: Optional[str] = None
    ) -> bool:
        full = self.get_running_config()
        if vrf and vrf != "GRT":
            return f"router ospf vrf {vrf}" in full
        return bool(
            re.search(r"^router ospf\s*$", full, re.MULTILINE)
        )

    def bgp_process_exists(
        self, vrf: Optional[str] = None
    ) -> bool:
        full = self.get_running_config()
        if vrf and vrf != "GRT":
            vrf_ok = bool(
                re.search(
                    rf"^router bgp \d+ vrf {re.escape(vrf)}$",
                    full, re.MULTILINE,
                )
            )
            grt_ok = bool(
                re.search(r"^router bgp \d+$", full, re.MULTILINE)
            )
            return vrf_ok or grt_ok
        return bool(
            re.search(r"^router bgp \d+$", full, re.MULTILINE)
        )

    # ── BGP existence ─────────────────────────────────────────────────────────

    def bgp_network_exists_in_frr(
        self,
        ip:         str,
        prefix_len: int,
        vrf:        Optional[str] = None,
    ) -> bool:
        if not self._available:
            return False
        target = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        cmd    = (
            f"show bgp vrf {vrf} ipv4 unicast"
            if (vrf and vrf != "GRT")
            else "show bgp ipv4 unicast"
        )
        ok, output = self.run_vtysh([cmd])
        if not ok:
            return False
        for line in output.splitlines():
            clean = line.strip().lstrip("*>idshr? ")
            if clean.startswith(target):
                return True
        return False

    def _verify_bgp_removal(
        self, ip: str, prefix_len: int, vrf: Optional[str]
    ) -> bool:
        still = self.bgp_network_exists_in_frr(ip, prefix_len, vrf)
        if still:
            log.error(
                f"_verify_bgp_removal: {ip}/{prefix_len} "
                f"STILL present (vrf={vrf})"
            )
        else:
            log.info(
                f"_verify_bgp_removal: {ip}/{prefix_len} "
                f"confirmed removed (vrf={vrf})"
            )
        return not still

    # ── OSPF helpers ──────────────────────────────────────────────────────────

    def _get_ospf_router_block(
        self, vrf: Optional[str] = None
    ) -> str:
        full = self.get_running_config()
        if not full or "(FRR" in full:
            return ""
        if vrf and vrf != "GRT":
            header = f"router ospf vrf {vrf}"
            is_grt = False
        else:
            header = "router ospf"
            is_grt = True
        lines       = full.splitlines()
        inside      = False
        block_lines: List[str] = []
        for line in lines:
            s = line.strip()
            if not inside:
                if (is_grt and s == "router ospf") or \
                        (not is_grt and s == header):
                    inside = True
            else:
                if s.startswith("router ") or s == "!":
                    break
                block_lines.append(line)
        return "\n".join(block_lines)

    def get_ospf_area(self, vrf: Optional[str] = None) -> str:
        block = self._get_ospf_router_block(vrf)
        if block:
            m = re.search(r"network\s+\S+\s+area\s+(\S+)", block)
            if m:
                return m.group(1)
        full = self.get_running_config()
        m    = re.search(r"ip ospf area\s+(\S+)", full)
        return m.group(1) if m else OSPF_AREA_DEFAULT

    def ospf_network_exists(
        self,
        ip:         str,
        prefix_len: int,
        area:       str,
        vrf:        Optional[str] = None,
    ) -> bool:
        net   = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        block = self._get_ospf_router_block(vrf)
        if not block:
            return False
        return bool(
            re.search(
                rf"network\s+{re.escape(net)}"
                rf"\s+area\s+{re.escape(area)}",
                block,
            )
        )

    def ospf_interface_area_exists(
        self, ifname: str, area: str
    ) -> bool:
        full   = self.get_running_config()
        inside = False
        for line in full.splitlines():
            s = line.strip()
            if s == f"interface {ifname}":
                inside = True
                continue
            if inside:
                if s.startswith("interface ") or s == "!":
                    break
                if re.search(
                    rf"ip ospf area\s+{re.escape(area)}", s
                ):
                    return True
        return False

    def configure_ospf_network(
        self,
        ip:         str,
        prefix_len: int,
        area:       str,
        vrf:        Optional[str] = None,
    ) -> bool:
        net = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        ctx = (
            f"router ospf vrf {vrf}"
            if (vrf and vrf != "GRT") else "router ospf"
        )
        ok, out = self.run_vtysh([
            "configure terminal", ctx,
            f"network {net} area {area}", "end",
        ])
        if not ok:
            print_error(f"OSPF network config failed: {out}")
        return ok

    def configure_ospf_interface(
        self,
        ifname: str,
        area:   str,
        vrf:    Optional[str] = None,
    ) -> bool:
        ok, out = self.run_vtysh([
            "configure terminal",
            f"interface {ifname}",
            f"ip ospf area {area}",
            "ip ospf passive",
            "end",
        ])
        if not ok:
            print_error(f"OSPF interface config failed: {out}")
        return ok

    def remove_ospf_network(
        self,
        ip:         str,
        prefix_len: int,
        area:       str,
        vrf:        Optional[str] = None,
    ) -> bool:
        if not self.ospf_network_exists(ip, prefix_len, area, vrf):
            log.info(f"OSPF network absent — skip (vrf={vrf})")
            return True
        net = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        ctx = (
            f"router ospf vrf {vrf}"
            if (vrf and vrf != "GRT") else "router ospf"
        )
        ok, out = self.run_vtysh([
            "configure terminal", ctx,
            f"no network {net} area {area}", "end",
        ])
        if not ok:
            print_error(f"OSPF network removal failed: {out}")
        return ok

    def remove_ospf_interface(
        self,
        ifname: str,
        area:   str,
        vrf:    Optional[str] = None,
    ) -> bool:
        if not self.ospf_interface_area_exists(ifname, area):
            log.info(f"OSPF if area absent on {ifname} — skip")
            return True
        ok, out = self.run_vtysh([
            "configure terminal",
            f"interface {ifname}",
            "no ip ospf area",
            "no ip ospf passive",
            "end",
        ])
        if not ok:
            print_error(f"OSPF interface removal failed: {out}")
        return ok

    def configure_bgp_network(
        self,
        ip:         str,
        prefix_len: int,
        vrf:        Optional[str] = None,
    ) -> bool:
        asn = self.get_bgp_asn_for_vrf(vrf)
        if not asn:
            print_error("No BGP ASN found. Configure BGP first.")
            return False
        network = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        ctx     = (
            f"router bgp {asn} vrf {vrf}"
            if (vrf and vrf != "GRT") else f"router bgp {asn}"
        )
        ok, out = self.run_vtysh([
            "configure terminal",
            ctx,
            "address-family ipv4 unicast",
            f"network {network}",
            "exit-address-family",
            "end",
        ])
        if not ok:
            print_error(f"BGP network config failed: {out}")
        return ok

    def remove_bgp_network(
        self,
        ip:           str,
        prefix_len:   int,
        vrf:          Optional[str] = None,
        explicit_asn: str = "",
    ) -> bool:
        network = str(IPv4Network(f"{ip}/{prefix_len}", strict=False))
        asn = self._resolve_asn(vrf, explicit_asn)
        if not asn:
            print_warn(
                f"Cannot determine BGP ASN for {network} — skip."
            )
            return True

        present = self.bgp_network_exists_in_frr(ip, prefix_len, vrf)
        if not present:
            log.info(
                f"BGP {network} absent in FRR — nothing to remove"
            )
            return True

        ctx = (
            f"router bgp {asn} vrf {vrf}"
            if (vrf and vrf != "GRT") else f"router bgp {asn}"
        )
        ok, out = self.run_vtysh([
            "configure terminal",
            ctx,
            "address-family ipv4 unicast",
            f"no network {network}",
            "exit-address-family",
            "end",
        ])
        if not ok:
            print_error(f"BGP 'no network {network}' failed: {out}")
            return False

        removed = self._verify_bgp_removal(ip, prefix_len, vrf)
        if removed:
            return True

        time.sleep(0.5)
        self.run_vtysh([
            "configure terminal",
            ctx,
            "address-family ipv4 unicast",
            f"no network {network}",
            "exit-address-family",
            "end",
        ])
        removed = self._verify_bgp_removal(ip, prefix_len, vrf)
        if not removed:
            print_warn(
                f"BGP network {network} may still be present.\n"
                f"  Verify: vtysh -c 'show bgp"
                + (f" vrf {vrf}" if vrf else "")
                + " ipv4 unicast'"
            )
        return removed

# ─────────────────────────────────────────────────────────────────────────────
#  IP UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
class IPUtils:

    @staticmethod
    def validate_subnet(subnet_str: str) -> Tuple[bool, str]:
        try:
            net = ip_network(subnet_str, strict=False)
            if net.version != 4:
                return False, "Only IPv4 supported."
            for r in RESERVED_NETWORKS:
                if net.overlaps(r):
                    return False, f"Overlaps reserved range {r}."
            if net.prefixlen > 30:
                return False, "Subnet too small (need at least /30)."
            return True, ""
        except ValueError as exc:
            return False, str(exc)

    @staticmethod
    def validate_host_ip(ip_str: str) -> Tuple[bool, str]:
        try:
            if "/" in ip_str:
                iface = IPv4Interface(ip_str)
                ip    = str(iface.ip)
            else:
                ip = str(IPv4Address(ip_str))
            for r in RESERVED_NETWORKS:
                if IPv4Address(ip) in r:
                    return False, f"Address in reserved range {r}."
            return True, ""
        except ValueError as exc:
            return False, str(exc)

    @staticmethod
    def parse_ip_prefix(ip_str: str) -> Tuple[str, int]:
        if "/" in ip_str:
            iface = IPv4Interface(ip_str)
            return str(iface.ip), iface.network.prefixlen
        return str(IPv4Address(ip_str)), 32

    @staticmethod
    def random_ip(exclude: List[str]) -> str:
        rfc1918 = [
            ip_network("10.0.0.0/8"),
            ip_network("172.16.0.0/12"),
            ip_network("192.168.0.0/16"),
        ]
        exclude_set = set(exclude)
        for _ in range(1000):
            net       = random.choice(rfc1918)
            h_int     = random.randint(
                int(net.network_address) + 1,
                int(net.broadcast_address) - 1,
            )
            candidate = str(IPv4Address(h_int))
            if candidate not in exclude_set:
                return candidate
        raise RuntimeError(
            "No unique random IP found after 1000 attempts."
        )

    @staticmethod
    def get_ips_from_subnet(
        subnet_str: str, count: int, exclude: List[str]
    ) -> List[str]:
        try:
            net         = ip_network(subnet_str, strict=False)
            exclude_set = set(exclude)
            result: List[str] = []
            for host in net.hosts():
                if len(result) >= count:
                    break
                if str(host) not in exclude_set:
                    result.append(str(host))
            return result
        except ValueError as exc:
            print_error(f"Subnet error: {exc}")
            return []

# ─────────────────────────────────────────────────────────────────────────────
#  DISPLAY MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class DisplayManager:

    def __init__(self, state: StateManager, kernel: KernelManager):
        self.state  = state
        self.kernel = kernel

    def show_interfaces(
        self,
        highlight_new: Optional[List[str]] = None,
    ) -> None:
        highlight_new = highlight_new or []
        print_header("Interface Overview")

        kernel_ifaces = {
            i["name"]: i
            for i in self.kernel.get_all_interfaces()
        }
        vrfs       = self.kernel.get_vrfs()
        state_data = self.state.get_all()

        vrf_groups: Dict[str, List] = {"GRT": []}
        for vrf_name in vrfs:
            vrf_groups[vrf_name] = []

        for name, iface in kernel_ifaces.items():
            if not is_display_interface(name, vrfs):
                continue
            vrf_for_if = None
            if iface["master_idx"]:
                for vrf_name, vrf_data in vrfs.items():
                    if vrf_data["ifindex"] == iface["master_idx"]:
                        vrf_for_if = vrf_name
                        break
            vrf_groups.setdefault(
                vrf_for_if or "GRT", []
            ).append(iface)

        total = 0
        for vrf_name in ["GRT"] + sorted(
            k for k in vrf_groups if k != "GRT"
        ):
            ifaces = vrf_groups.get(vrf_name, [])
            in_vrf = [
                v for v in state_data.values()
                if v.get("vrf") == vrf_name
            ]
            if not ifaces and not in_vrf:
                continue

            print(f"\n{C.BOLD}VRF: {C.CYAN}{vrf_name}{C.RESET}")
            tbl = make_table(
                "Interface", "State", "IP Address",
                "Tag", "Protocol", "Created",
            )
            for iface in ifaces:
                ifname    = iface["name"]
                state_col = (
                    f"{C.SUCCESS}UP{C.RESET}"
                    if iface["state"] == "UP"
                    else f"{C.ERROR}DOWN{C.RESET}"
                )
                addrs  = iface["addresses"]
                ip_str = (
                    ", ".join(
                        f"{a['ip']}/{a['prefix_len']}"
                        for a in addrs
                    ) if addrs else "-"
                )
                meta     = state_data.get(ifname, {})
                tag      = meta.get("tag", "-")
                protocol = meta.get("protocol", "-")
                created  = (meta.get("created_at", "-") or "-")[:10]

                row = [ifname, state_col, ip_str, tag, protocol, created]
                if ifname in highlight_new:
                    row = [
                        f"{C.NEW_IF}{cell}{C.RESET}"
                        for cell in row
                    ]
                tbl.add_row(row)
                total += 1

            if ifaces:
                print(tbl)
            else:
                print(f"  {C.DIM}(no interfaces){C.RESET}")

        print(f"\n{C.DIM}Total interfaces: {total}{C.RESET}")

    def show_frr_full(self, frr: FRRManager) -> None:
        print_header("FRR Running Configuration")
        print(f"{C.DIM}{frr.get_running_config()}{C.RESET}")

    def show_interfaces_grouped_table(self) -> List[Dict]:
        print_header("Select Interface")
        print(
            f"  {C.DIM}FRR-internal (pimreg/pim6reg) and VRF devices "
            f"are excluded.{C.RESET}\n"
        )

        kernel_ifaces = {
            i["name"]: i
            for i in self.kernel.get_all_interfaces()
        }
        vrfs       = self.kernel.get_vrfs()
        state_data = self.state.get_all()

        vrf_groups: Dict[str, List] = {"GRT": []}
        for vrf_name in vrfs:
            vrf_groups[vrf_name] = []

        for name, iface in kernel_ifaces.items():
            if not is_selectable_interface(name, vrfs):
                continue
            vrf_for_if = None
            if iface["master_idx"]:
                for vrf_name, vrf_data in vrfs.items():
                    if vrf_data["ifindex"] == iface["master_idx"]:
                        vrf_for_if = vrf_name
                        break
            vrf_groups.setdefault(
                vrf_for_if or "GRT", []
            ).append(iface)

        global_idx = 0
        ordered:   List[Dict] = []

        for vrf_name in ["GRT"] + sorted(
            k for k in vrf_groups if k != "GRT"
        ):
            ifaces = vrf_groups.get(vrf_name, [])
            if not ifaces:
                continue

            print(f"\n{C.BOLD}VRF: {C.CYAN}{vrf_name}{C.RESET}")
            tbl = make_table(
                "#", "Interface", "State", "IP Address",
                "Tag", "Protocol", "Created",
            )
            for iface in ifaces:
                ifname    = iface["name"]
                state_col = (
                    f"{C.SUCCESS}UP{C.RESET}"
                    if iface["state"] == "UP"
                    else f"{C.ERROR}DOWN{C.RESET}"
                )
                addrs  = iface["addresses"]
                ip_str = (
                    ", ".join(
                        f"{a['ip']}/{a['prefix_len']}"
                        for a in addrs
                    ) if addrs else "-"
                )
                meta     = state_data.get(ifname, {})
                tag      = meta.get("tag", "-")
                protocol = meta.get("protocol", "-")
                created  = (meta.get("created_at", "-") or "-")[:10]

                tbl.add_row([
                    global_idx, ifname, state_col, ip_str,
                    tag, protocol, created,
                ])
                enriched        = dict(iface)
                enriched["_vrf"] = vrf_name
                ordered.append(enriched)
                global_idx += 1

            print(tbl)

        if not ordered:
            print(
                f"  {C.DIM}No selectable interfaces found.{C.RESET}"
            )
        return ordered

    def show_interfaces_table(self) -> List[Dict]:
        print_header("Available Interfaces")
        all_ifaces = [
            i for i in self.kernel.get_all_interfaces()
            if i["name"] != "lo"
        ]
        vrfs       = self.kernel.get_vrfs()
        state_data = self.state.get_all()

        tbl = make_table(
            "#", "Interface", "State", "IP Address",
            "VRF", "Tag", "Protocol",
        )
        for idx, iface in enumerate(all_ifaces):
            ifname = iface["name"]
            addrs  = iface["addresses"]
            ip_str = (
                ", ".join(
                    f"{a['ip']}/{a['prefix_len']}" for a in addrs
                ) if addrs else "-"
            )
            state_col = (
                f"{C.SUCCESS}UP{C.RESET}"
                if iface["state"] == "UP"
                else f"{C.ERROR}DOWN{C.RESET}"
            )
            vrf_name = "GRT"
            if iface["master_idx"]:
                for vn, vd in vrfs.items():
                    if vd["ifindex"] == iface["master_idx"]:
                        vrf_name = vn
                        break
            meta     = state_data.get(ifname, {})
            tag      = meta.get("tag", "-")
            protocol = meta.get("protocol", "-")
            tbl.add_row([
                idx, ifname, state_col, ip_str,
                vrf_name, tag, protocol,
            ])
        print(tbl)
        return all_ifaces

# ─────────────────────────────────────────────────────────────────────────────
#  VRF MANAGER  — v2.9.7: Step 4 uses remove_vrf_complete()
# ─────────────────────────────────────────────────────────────────────────────
class VRFManager:
    """
    Interactive VRF lifecycle manager.

    v2.9.7 change in _delete_vrf():
        Step 4 now calls frr.remove_vrf_complete(vrf_name) instead of
        frr.remove_vrf(vrf_name).

        remove_vrf_complete() removes:
            - BGP VRF instance ('no router bgp <asn> vrf <name>')
            - OSPF VRF instance ('no router ospf vrf <name>')
            - VRF stanza ('no vrf <name>')
            - writes to disk ('write memory')

        Each sub-step is verified and logged individually so the user
        can see exactly what was removed from FRR.
    """

    def __init__(
        self,
        state:   StateManager,
        kernel:  KernelManager,
        frr:     FRRManager,
        cleanup: "CleanupManager",
    ):
        self.state   = state
        self.kernel  = kernel
        self.frr     = frr
        self.cleanup = cleanup

    def run(self) -> None:
        print_header("VRF Manager")
        print(
            f"\n  {C.BOLD}Options:{C.RESET}\n"
            f"  {C.CYAN}[1]{C.RESET} Create new VRF\n"
            f"  {C.CYAN}[2]{C.RESET} Delete existing VRF\n"
            f"  {C.CYAN}[3]{C.RESET} Show VRFs\n"
            f"  {C.CYAN}[4]{C.RESET} Back to main menu"
        )
        choice = prompt("Choice", "4")
        if   choice == "1": self._create_vrf()
        elif choice == "2": self._delete_vrf()
        elif choice == "3": self._show_vrfs()

    def _show_vrfs(self) -> None:
        print_header("Detected VRFs")
        vrfs = self.kernel.get_vrfs()
        if not vrfs:
            print_info("No VRF devices — only GRT available.")
            return
        tbl = make_table(
            "VRF Name", "Table ID", "Ifindex",
            "In FRR", "Tracked", "Enslaved Interfaces",
        )
        managed = self.state.get_all_vrfs()
        for name, d in sorted(vrfs.items()):
            in_frr   = (
                "Yes" if self.frr.vrf_exists_in_frr(name) else "No"
            )
            tracked  = "Yes" if name in managed else "No"
            enslaved = self.kernel.get_enslaved_interfaces(
                d["ifindex"]
            )
            enslaved_names = (
                ", ".join(e["name"] for e in enslaved)
                if enslaved else "-"
            )
            tbl.add_row([
                name, d.get("table", "-"), d.get("ifindex", "-"),
                in_frr, tracked, enslaved_names,
            ])
        print(tbl)

    def _create_vrf(self) -> None:
        print_header("Create VRF")

        while True:
            vrf_name = re.sub(
                r"[^a-zA-Z0-9_-]", "",
                prompt("VRF name (e.g. vrf10)"),
            )[:15]
            if not vrf_name:
                print_error("Invalid or empty name.")
                continue
            if self.kernel.interface_exists(vrf_name):
                print_error(f"'{vrf_name}' already exists in kernel.")
                continue
            break

        used_tables = set(
            v["table"]
            for v in self.kernel.get_vrfs().values()
            if v.get("table")
        )
        used_tables.update(self.state.get_used_table_ids())

        while True:
            table_str = prompt("Routing table ID (e.g. 10)")
            try:
                table_id = int(table_str)
                if table_id < 1 or table_id > 65535:
                    print_error("Table ID must be 1–65535.")
                    continue
                if table_id in used_tables:
                    print_error(
                        f"Table ID {table_id} is already in use."
                    )
                    continue
                break
            except ValueError:
                print_error("Enter a valid integer.")

        print(f"\n{C.BOLD}Plan:{C.RESET}")
        print(f"  VRF name   : {C.CYAN}{vrf_name}{C.RESET}")
        print(f"  Table ID   : {table_id}")
        print(
            f"  FRR config : "
            f"{'Yes' if self.frr.is_available() else 'N/A'}"
        )
        if prompt("Proceed? [y/N]", "n").lower() != "y":
            print_info("Aborted.")
            return

        log.info(f"Creating VRF: {vrf_name} table={table_id}")
        if not self.kernel.create_vrf_device(vrf_name, table_id):
            return

        if self.frr.is_available():
            if not self.frr.configure_vrf(vrf_name, table_id):
                print_warn(
                    "FRR VRF config failed — VRF exists in kernel only."
                )

        self.state.add_vrf(vrf_name, table_id)
        print_success(
            f"VRF '{vrf_name}' created (table={table_id})"
        )

    def _delete_vrf(self) -> None:
        """
        Delete a VRF — complete kernel + FRR cleanup.

        Steps:
          1  Delete LoopGen-tracked interfaces
             (routing adv removal + kernel delete + FRR stanza + state)
          2  Delete kernel VRF device
             (auto-releases pimreg/enslaved interfaces)
          3  PIM clear + poll for pimreg to vanish
          4  [v2.9.7] Complete FRR VRF config removal via
             frr.remove_vrf_complete() which removes:
               • BGP VRF instance
               • OSPF VRF instance
               • VRF stanza
               • writes memory
          5  Purge FRR interface stanzas for all enslaved interfaces
          6  State file update
        """
        print_header("Delete VRF")
        vrfs = self.kernel.get_vrfs()
        if not vrfs:
            print_info("No VRFs to delete.")
            return

        tbl = make_table(
            "#", "VRF Name", "Table ID", "Enslaved Interfaces"
        )
        vrf_list = sorted(vrfs.keys())
        for i, name in enumerate(vrf_list):
            enslaved = self.kernel.get_enslaved_interfaces(
                vrfs[name]["ifindex"]
            )
            enslaved_str = (
                ", ".join(e["name"] for e in enslaved)
                if enslaved else "-"
            )
            tbl.add_row([
                i, name,
                vrfs[name].get("table", "-"),
                enslaved_str,
            ])
        print(tbl)

        raw = prompt("VRF number or name to delete")
        if not raw:
            print_error("No input.")
            return

        vrf_name: Optional[str] = None
        try:
            idx = int(raw)
            if 0 <= idx < len(vrf_list):
                vrf_name = vrf_list[idx]
        except ValueError:
            if raw in vrf_list:
                vrf_name = raw

        if not vrf_name:
            print_error(f"VRF '{raw}' not found.")
            return

        vrf_ifindex = vrfs[vrf_name]["ifindex"]
        enslaved    = self.kernel.get_enslaved_interfaces(vrf_ifindex)

        # ── Show what FRR config will be removed ──────────────────────────
        if self.frr.is_available():
            full = self.frr.get_running_config()
            frr_items: List[str] = []
            asn = self.frr.get_bgp_asn()
            if asn and f"router bgp {asn} vrf {vrf_name}" in full:
                frr_items.append(f"router bgp {asn} vrf {vrf_name}")
            if f"router ospf vrf {vrf_name}" in full:
                frr_items.append(f"router ospf vrf {vrf_name}")
            if f"vrf {vrf_name}" in full:
                frr_items.append(f"vrf {vrf_name}")
            if frr_items:
                print(
                    f"\n{C.BOLD}FRR configuration to be removed:{C.RESET}"
                )
                for item in frr_items:
                    print(f"  {C.CYAN}•{C.RESET} {item}")

        if enslaved:
            print(
                f"\n{C.WARN}VRF '{vrf_name}' has "
                f"{len(enslaved)} enslaved interface(s):{C.RESET}"
            )
            state_data = self.state.get_all()
            etbl = make_table(
                "Interface", "Type", "IP Address", "Action"
            )
            for iface in enslaved:
                ifname  = iface["name"]
                addrs   = iface["addresses"]
                ip_str  = (
                    ", ".join(
                        f"{a['ip']}/{a['prefix_len']}"
                        for a in addrs
                    ) if addrs else "-"
                )
                if ifname in state_data:
                    if_type = "LoopGen-tracked"
                    action  = "FRR cleanup + kernel + FRR stanza"
                elif is_frr_internal(ifname):
                    if_type = "FRR-internal (pimreg)"
                    action  = "Released via PIM clear"
                else:
                    if_type = "Untracked"
                    action  = "Kernel delete + FRR stanza"
                etbl.add_row([ifname, if_type, ip_str, action])
            print(etbl)

        if prompt(
            f"Delete VRF '{vrf_name}' and all its "
            f"interfaces? [yes/N]",
            "n",
        ).lower() != "yes":
            print_info("Cancelled.")
            return

        # ── Step 1: Delete LoopGen-tracked interfaces ──────────────────────
        state_data   = self.state.get_all()
        tracked_here = [
            (name, meta)
            for name, meta in state_data.items()
            if meta.get("vrf") == vrf_name
        ]
        if tracked_here:
            print_info(
                f"  [Step 1] Removing "
                f"{len(tracked_here)} LoopGen-tracked interface(s) …"
            )
            for ifname, meta in tracked_here:
                log.info(f"  Removing tracked: {ifname}")
                self.cleanup._delete_one(
                    ifname, meta, verbose=True
                )
        else:
            print_info(
                "  [Step 1] No LoopGen-tracked interfaces."
            )

        # ── Step 2: Delete kernel VRF device ──────────────────────────────
        print_info(
            f"  [Step 2] Deleting kernel VRF device '{vrf_name}' …"
        )
        if not self.kernel.delete_vrf_device(vrf_name):
            print_error(
                f"Failed to delete kernel VRF device '{vrf_name}'."
            )
            return
        print_success(f"  Kernel VRF device '{vrf_name}' deleted.")

        # ── Step 3: PIM clear + poll ───────────────────────────────────────
        frr_internal_names = [
            i["name"] for i in enslaved
            if is_frr_internal(i["name"])
        ]
        if frr_internal_names:
            print_info(
                f"  [Step 3] Clearing FRR PIM state for "
                f"'{vrf_name}' …"
            )
            self.frr.clear_pim_vrf(vrf_name)
            time.sleep(1.0)

            still_there = self.kernel.poll_until_interfaces_gone(
                frr_internal_names,
                timeout=PIMREG_VANISH_TIMEOUT,
                interval=PIMREG_POLL_INTERVAL,
            )
            if still_there:
                print_info(
                    "  Still present — issuing global PIM clear …"
                )
                self.frr.clear_pim_all()
                time.sleep(1.5)
                still_there = self.kernel.poll_until_interfaces_gone(
                    still_there,
                    timeout=PIMREG_VANISH_TIMEOUT,
                    interval=PIMREG_POLL_INTERVAL,
                )

            if still_there:
                print_warn(
                    f"  Interface(s) still visible: "
                    f"{', '.join(still_there)}\n"
                    f"  Will purge FRR stanzas."
                )
                self.frr.purge_frr_interface_stanzas(still_there)
            else:
                print_success("  FRR-internal interfaces cleaned up.")
        else:
            print_info(
                "  [Step 3] No FRR-internal interfaces to wait for."
            )

        # ── Step 4: Complete FRR VRF configuration removal ────────────────
        # [v2.9.7] Uses remove_vrf_complete() instead of remove_vrf()
        # to remove BGP + OSPF + VRF stanza + write memory
        if self.frr.is_available():
            print_info(
                f"  [Step 4] Removing complete FRR configuration "
                f"for VRF '{vrf_name}' …"
            )
            log.info(
                f"Step 4: calling remove_vrf_complete('{vrf_name}')"
            )
            frr_ok = self.frr.remove_vrf_complete(vrf_name)
            if frr_ok:
                print_success(
                    f"  FRR configuration for '{vrf_name}' "
                    f"fully removed."
                )
            else:
                print_warn(
                    f"  Some FRR stanzas for '{vrf_name}' may remain.\n"
                    f"  Check: sudo vtysh -c 'show running-config'"
                )
        else:
            print_info(
                "  [Step 4] FRR not available — skip FRR cleanup."
            )

        # ── Step 5: Purge FRR interface stanzas for all enslaved ──────────
        all_enslaved_names = [i["name"] for i in enslaved]
        if all_enslaved_names and self.frr.is_available():
            print_info(
                f"  [Step 5] Purging FRR interface stanzas for "
                f"{len(all_enslaved_names)} interface(s) …"
            )
            self.frr.purge_frr_interface_stanzas(
                all_enslaved_names
            )
            print_success("  FRR interface stanzas purged.")

        # ── Step 6: State ──────────────────────────────────────────────────
        self.state.remove_vrf(vrf_name)
        log.info(f"VRF '{vrf_name}' removed from state")

        print_success(f"VRF '{vrf_name}' deleted successfully.")
        log.info(f"VRF '{vrf_name}' fully deleted")

# ─────────────────────────────────────────────────────────────────────────────
#  INTERFACE MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class InterfaceManager:

    def __init__(
        self,
        state:   StateManager,
        kernel:  KernelManager,
        frr:     FRRManager,
        display: DisplayManager,
    ):
        self.state   = state
        self.kernel  = kernel
        self.frr     = frr
        self.display = display

    def run(self) -> None:
        print_header("Interface Manager")
        print(
            f"\n  {C.BOLD}Options:{C.RESET}\n"
            f"  {C.CYAN}[1]{C.RESET} Move interface to a different VRF\n"
            f"  {C.CYAN}[2]{C.RESET} Reconfigure IP address on interface\n"
            f"  {C.CYAN}[3]{C.RESET} Back to main menu"
        )
        choice = prompt("Choice", "3")
        if   choice == "1": self._move_to_vrf_wizard()
        elif choice == "2": self._reconfigure_ip_wizard()

    def _select_interface(self) -> Optional[Dict]:
        selectable = self.display.show_interfaces_grouped_table()
        if not selectable:
            print_error("No selectable interfaces available.")
            return None
        raw = prompt(
            "Enter interface # or name (empty to cancel)"
        )
        if not raw:
            return None
        try:
            idx = int(raw)
            if 0 <= idx < len(selectable):
                return selectable[idx]
            print_error(
                f"Index {idx} out of range "
                f"(0–{len(selectable) - 1})."
            )
            return None
        except ValueError:
            pass
        match = next(
            (i for i in selectable if i["name"] == raw), None
        )
        if match:
            return match
        print_error(f"Interface '{raw}' not found.")
        return None

    def _move_to_vrf_wizard(self) -> None:
        print_header("Move Interface to VRF")
        iface = self._select_interface()
        if not iface:
            return
        ifname = iface["name"]

        vrfs        = self.kernel.get_vrfs()
        vrf_choices = ["GRT"] + sorted(vrfs.keys())
        print(f"\n{C.BOLD}Available VRFs:{C.RESET}")
        for i, v in enumerate(vrf_choices):
            suffix = (
                f"  (table {vrfs[v]['table']})"
                if v != "GRT" else ""
            )
            print(
                f"  {C.CYAN}[{i}]{C.RESET} {v}"
                f"{C.DIM}{suffix}{C.RESET}"
            )

        raw = prompt("Target VRF # or name", "0")
        target_vrf: Optional[str] = None
        try:
            idx = int(raw)
            if 0 <= idx < len(vrf_choices):
                target_vrf = vrf_choices[idx]
        except ValueError:
            if raw in vrf_choices:
                target_vrf = raw

        if not target_vrf:
            print_error(f"VRF '{raw}' not found.")
            return

        print(
            f"\n{C.BOLD}Plan:{C.RESET}  Move "
            f"{C.CYAN}{ifname}{C.RESET} → "
            f"VRF {C.CYAN}{target_vrf}{C.RESET}"
        )
        if prompt("Proceed? [y/N]", "n").lower() != "y":
            print_info("Aborted.")
            return

        log.info(f"Moving {ifname} → VRF {target_vrf}")
        if target_vrf == "GRT":
            ok = self.kernel.detach_from_vrf(ifname)
        else:
            ok = self.kernel.move_to_vrf(ifname, target_vrf)

        if not ok:
            return

        if self.state.exists(ifname):
            self.state.update(ifname, vrf=target_vrf)
            print_info("State entry updated.")

        addrs = self.kernel.get_interface_ips(ifname)
        if addrs and self.frr.is_available():
            self._offer_routing(
                ifname,
                addrs[0]["ip"],
                addrs[0]["prefix_len"],
                target_vrf,
            )

        print_success(f"{ifname} moved to VRF '{target_vrf}'.")

    def _reconfigure_ip_wizard(self) -> None:
        print_header("Reconfigure IP Address")
        iface = self._select_interface()
        if not iface:
            return
        ifname = iface["name"]

        current_ips = self.kernel.get_interface_ips(ifname)
        if current_ips:
            print(f"\n{C.BOLD}Current IPs on {ifname}:{C.RESET}")
            for a in current_ips:
                print(
                    f"  {C.CYAN}{a['ip']}/{a['prefix_len']}{C.RESET}"
                )

            keep = prompt(
                "Keep existing IP address(es)? [y/N]", "n"
            )
            if keep.lower() == "y":
                new_ip, new_plen = self._ask_new_ip(ifname)
                if new_ip is None:
                    return
                if not self.kernel.add_ip_to_if(
                    ifname, new_ip, new_plen
                ):
                    return
            else:
                new_ip, new_plen = self._ask_new_ip(ifname)
                if new_ip is None:
                    return

                print(
                    f"\n{C.BOLD}Plan:{C.RESET}  Replace IPs on "
                    f"{C.CYAN}{ifname}{C.RESET} with "
                    f"{C.CYAN}{new_ip}/{new_plen}{C.RESET}"
                )
                if prompt("Proceed? [y/N]", "n").lower() != "y":
                    print_info("Aborted.")
                    return

                if self.frr.is_available() and \
                        self.state.exists(ifname):
                    meta    = self.state.get_all().get(ifname, {})
                    frr_vrf = (
                        meta["vrf"]
                        if meta.get("vrf") != "GRT" else None
                    )
                    if meta.get("protocol") == "BGP":
                        for a in current_ips:
                            self.frr.remove_bgp_network(
                                a["ip"], a["prefix_len"],
                                vrf=frr_vrf,
                                explicit_asn=meta.get("bgp_asn", ""),
                            )
                    elif meta.get("protocol") == "OSPF":
                        for a in current_ips:
                            if meta.get("ospf_method") == "network":
                                self.frr.remove_ospf_network(
                                    a["ip"], a["prefix_len"],
                                    meta.get(
                                        "ospf_area", OSPF_AREA_DEFAULT
                                    ),
                                    frr_vrf,
                                )
                            else:
                                self.frr.remove_ospf_interface(
                                    ifname,
                                    meta.get(
                                        "ospf_area", OSPF_AREA_DEFAULT
                                    ),
                                    frr_vrf,
                                )

                for a in current_ips:
                    self.kernel.remove_ip_from_if(
                        ifname, a["ip"], a["prefix_len"]
                    )

                if not self.kernel.add_ip_to_if(
                    ifname, new_ip, new_plen
                ):
                    return

                if self.state.exists(ifname):
                    self.state.update(
                        ifname, ip=new_ip, prefix_len=new_plen
                    )
                    print_info("State entry updated.")
        else:
            new_ip, new_plen = self._ask_new_ip(ifname)
            if new_ip is None:
                return
            if not self.kernel.add_ip_to_if(
                ifname, new_ip, new_plen
            ):
                return

        print_success(
            f"IP reconfigured on {ifname}: {new_ip}/{new_plen}"
        )

        vrfs        = self.kernel.get_vrfs()
        current_vrf = "GRT"
        iface_fresh = next(
            (
                i for i in self.kernel.get_all_interfaces()
                if i["name"] == ifname
            ),
            None,
        )
        if iface_fresh and iface_fresh.get("master_idx"):
            for vn, vd in vrfs.items():
                if vd["ifindex"] == iface_fresh["master_idx"]:
                    current_vrf = vn
                    break

        if self.frr.is_available():
            self._offer_routing(
                ifname, new_ip, new_plen, current_vrf
            )

    def _offer_routing(
        self,
        ifname:     str,
        ip:         str,
        prefix_len: int,
        vrf:        str,
    ) -> None:
        print(
            f"\n  {C.BOLD}Advertise {ip}/{prefix_len} "
            f"in a routing protocol?{C.RESET}\n"
            f"  {C.CYAN}[1]{C.RESET} No\n"
            f"  {C.CYAN}[2]{C.RESET} OSPF\n"
            f"  {C.CYAN}[3]{C.RESET} BGP"
        )
        frr_vrf = vrf if vrf != "GRT" else None
        choice  = prompt("Choice", "1")

        if choice == "2":
            if not self.frr.ospf_process_exists(frr_vrf):
                print_warn(f"No OSPF process for VRF '{vrf}'.")
                return
            area   = self._normalize_area(
                prompt("OSPF area", self.frr.get_ospf_area(frr_vrf))
            )
            print(
                f"  {C.BOLD}OSPF Method:{C.RESET}  "
                f"{C.CYAN}[1]{C.RESET} network stmt  "
                f"{C.CYAN}[2]{C.RESET} interface-level"
            )
            method = (
                "interface"
                if prompt("Method", "1") == "2"
                else "network"
            )
            if method == "network":
                ok = self.frr.configure_ospf_network(
                    ip, prefix_len, area, frr_vrf
                )
            else:
                ok = self.frr.configure_ospf_interface(
                    ifname, area, frr_vrf
                )
            if ok:
                print_success(
                    f"OSPF advertisement added for {ip}/{prefix_len}"
                )
                if self.state.exists(ifname):
                    self.state.update(
                        ifname,
                        protocol="OSPF",
                        ospf_method=method,
                        ospf_area=area,
                    )

        elif choice == "3":
            if not self.frr.bgp_process_exists(frr_vrf):
                print_warn("No BGP process found.")
                return
            asn = self.frr.get_bgp_asn_for_vrf(frr_vrf) or ""
            if not asn:
                print_error("Could not determine BGP ASN.")
                return
            ok = self.frr.configure_bgp_network(
                ip, prefix_len, frr_vrf
            )
            if ok:
                print_success(
                    f"BGP advertisement added for {ip}/{prefix_len}"
                )
                if self.state.exists(ifname):
                    self.state.update(
                        ifname, protocol="BGP", bgp_asn=asn
                    )

    def _ask_new_ip(
        self, ifname: str
    ) -> Tuple[Optional[str], int]:
        existing_ips   = set(self.kernel.get_all_kernel_ips())
        current_if_ips = {
            a["ip"]
            for a in self.kernel.get_interface_ips(ifname)
        }
        while True:
            raw = prompt(
                "New IP address (e.g. 10.1.2.3 or 10.1.2.3/24, "
                "empty to cancel)"
            )
            if not raw:
                return None, 0
            valid, err = IPUtils.validate_host_ip(raw)
            if not valid:
                print_error(f"Invalid: {err}")
                continue
            ip, plen = IPUtils.parse_ip_prefix(raw)
            if ip in current_if_ips:
                same = prompt(
                    f"{ip}/{plen} already on {ifname}. "
                    "Use anyway? [y/N]",
                    "n",
                )
                if same.lower() == "y":
                    return ip, plen
                continue
            if ip in existing_ips:
                print_warn(
                    f"{ip} already assigned to another interface."
                )
                override = prompt("Use anyway? [y/N]", "n")
                if override.lower() != "y":
                    continue
            return ip, plen

    @staticmethod
    def _normalize_area(area: str) -> str:
        try:
            return str(IPv4Address(area))
        except ValueError:
            try:
                return str(IPv4Address(int(area)))
            except (ValueError, OverflowError):
                return OSPF_AREA_DEFAULT

# ─────────────────────────────────────────────────────────────────────────────
#  LOOPBACK CREATOR
# ─────────────────────────────────────────────────────────────────────────────
class LoopbackCreator:

    def __init__(
        self,
        state:   StateManager,
        kernel:  KernelManager,
        frr:     FRRManager,
        display: DisplayManager,
    ):
        self.state   = state
        self.kernel  = kernel
        self.frr     = frr
        self.display = display

    def run(self) -> None:
        print_header("Create Loopback Interfaces")
        vrfs        = self.kernel.get_vrfs()
        vrf_choices = ["GRT"] + sorted(vrfs.keys())

        print(f"\n{C.BOLD}Available VRFs:{C.RESET}")
        for i, v in enumerate(vrf_choices):
            suffix = (
                f"  (table {vrfs[v]['table']})"
                if v != "GRT" else ""
            )
            print(
                f"  {C.CYAN}[{i}]{C.RESET} {v}"
                f"{C.DIM}{suffix}{C.RESET}"
            )

        selected = self._select_vrfs(vrf_choices)
        if not selected:
            print_error("No VRFs selected.")
            return

        newly_created: List[str] = []
        for vrf in selected:
            print(f"\n{C.BOLD}─── VRF: {C.CYAN}{vrf}{C.RESET}")
            newly_created.extend(self._create_for_vrf(vrf))

        if newly_created:
            print_success(f"Done: {', '.join(newly_created)}")
            self.display.show_interfaces(highlight_new=newly_created)
        else:
            print_warn("No interfaces were created.")

    def _select_vrfs(self, vrf_choices: List[str]) -> List[str]:
        raw = prompt("VRF numbers (comma-sep) or 'all'", "0")
        if raw.lower() == "all":
            return vrf_choices
        selected: List[str] = []
        for part in raw.split(","):
            part = part.strip()
            try:
                idx = int(part)
                if 0 <= idx < len(vrf_choices):
                    selected.append(vrf_choices[idx])
                else:
                    print_warn(f"Index {idx} out of range.")
            except ValueError:
                if part in vrf_choices:
                    selected.append(part)
                else:
                    print_warn(f"Unknown VRF '{part}'.")
        seen: set = set()
        return [
            v for v in selected
            if not (v in seen or seen.add(v))  # type: ignore[func-returns-value]
        ]

    def _create_for_vrf(self, vrf: str) -> List[str]:
        try:
            count = max(1, int(prompt("Number of loopbacks", "1")))
        except ValueError:
            count = 1

        tag    = self._sanitize_tag(prompt("Tag/label", "default"))
        prefix = (
            re.sub(
                r"[^a-zA-Z0-9]", "",
                prompt("Interface name prefix", DEFAULT_PREFIX),
            )[:8] or DEFAULT_PREFIX
        )

        ip_mode, subnet = self._ask_ip_mode(count)
        if ip_mode is None:
            return []

        protocol = self._ask_protocol()

        ospf_method = "none"
        ospf_area   = OSPF_AREA_DEFAULT
        if protocol == "OSPF" and self.frr.is_available():
            ospf_method = self._ask_ospf_method()
            area_input  = prompt(
                "OSPF area",
                self.frr.get_ospf_area(
                    vrf if vrf != "GRT" else None
                ),
            )
            ospf_area = self._normalize_area(area_input)

        bgp_asn = ""
        if protocol == "BGP" and self.frr.is_available():
            frr_vrf = vrf if vrf != "GRT" else None
            bgp_asn = self.frr.get_bgp_asn_for_vrf(frr_vrf) or ""
            if not bgp_asn:
                print_error(
                    "No BGP process found in FRR.\n"
                    "  Configure: sudo vtysh -c 'configure terminal' "
                    "-c 'router bgp 65000' -c 'end'"
                )
                return []

        print(f"\n{C.BOLD}Creation Plan:{C.RESET}")
        print(f"  VRF      : {C.CYAN}{vrf}{C.RESET}")
        print(f"  Count    : {count}")
        print(
            f"  Names    : {prefix}001, {prefix}002 … "
            f"(tag='{tag}' stored as metadata)"
        )
        print(
            f"  IP mode  : {ip_mode}"
            + (f"  subnet={subnet}" if subnet else "")
        )
        print(f"  Protocol : {protocol}")
        if protocol == "OSPF":
            print(
                f"  OSPF     : "
                f"method={ospf_method}  area={ospf_area}"
            )
        if protocol == "BGP":
            bgp_ctx = (
                f"router bgp {bgp_asn} vrf {vrf}"
                if vrf != "GRT" else f"router bgp {bgp_asn}"
            )
            print(f"  BGP ctx  : {bgp_ctx}")

        if prompt("Proceed? [y/N]", "n").lower() != "y":
            print_info("Aborted.")
            return []

        if protocol in ("OSPF", "BGP") and self.frr.is_available():
            print_info("FRR config before changes:")
            self.display.show_frr_full(self.frr)

        existing_ips = (
            self.state.get_all_ips()
            + self.kernel.get_all_kernel_ips()
        )
        if ip_mode == "random":
            ips: List[str] = []
            for _ in range(count):
                try:
                    ip = IPUtils.random_ip(existing_ips)
                    existing_ips.append(ip)
                    ips.append(ip)
                except RuntimeError as exc:
                    print_error(str(exc))
                    break
        else:
            ips = IPUtils.get_ips_from_subnet(
                subnet, count, existing_ips  # type: ignore[arg-type]
            )
            if len(ips) < count:
                print_warn(
                    f"Only {len(ips)} IPs available in {subnet}."
                )
                count = len(ips)

        if not ips:
            print_error("No IPs available.")
            return []

        existing_names = (
            self.state.get_all_names()
            + [i["name"] for i in self.kernel.get_all_interfaces()]
        )
        created_names: List[str] = []
        rolled_back:   List[str] = []
        frr_vrf = vrf if vrf != "GRT" else None

        for ip in ips:
            number = next_available_number(prefix, existing_names)
            ifname = generate_ifname(prefix, number)
            existing_names.append(ifname)

            if self.state.exists(ifname) or \
                    self.kernel.interface_exists(ifname):
                print_warn(f"{ifname} already exists — skip.")
                continue

            print(
                f"\n  {C.INFO}Creating "
                f"{C.BOLD}{ifname}{C.RESET} → {ip}/32  VRF={vrf}"
            )

            if vrf != "GRT":
                kernel_ok = self.kernel.create_vrf_interface(
                    ifname, ip, 32, vrf
                )
            else:
                kernel_ok = self.kernel.create_grt_interface(
                    ifname, ip, 32
                )

            if not kernel_ok:
                print_error(f"Kernel creation failed for {ifname}.")
                continue

            if vrf != "GRT":
                if not self.kernel.verify_vrf_membership(
                    ifname, vrf
                ):
                    print_error(
                        f"VRF membership check failed for {ifname} "
                        f"— rolling back."
                    )
                    self.kernel.delete_interface(ifname)
                    continue

            frr_ok = True
            if protocol == "OSPF" and self.frr.is_available():
                if not self.frr.ospf_process_exists(frr_vrf):
                    print_warn(
                        f"No OSPF process for vrf='{vrf}' — skip."
                    )
                    frr_ok = False
                elif ospf_method == "network":
                    frr_ok = self.frr.configure_ospf_network(
                        ip, 32, ospf_area, frr_vrf
                    )
                else:
                    frr_ok = self.frr.configure_ospf_interface(
                        ifname, ospf_area, frr_vrf
                    )
            elif protocol == "BGP" and self.frr.is_available():
                if not self.frr.bgp_process_exists(frr_vrf):
                    print_warn("No BGP process — skip FRR.")
                    frr_ok = False
                else:
                    frr_ok = self.frr.configure_bgp_network(
                        ip, 32, frr_vrf
                    )

            if not frr_ok and protocol != "None":
                print_error(
                    f"FRR config failed for {ifname} — rolling back."
                )
                self.kernel.delete_interface(ifname)
                rolled_back.append(ifname)
                continue

            self.state.add(
                ifname, ip, 32, vrf, tag, protocol,
                ospf_method=ospf_method,
                ospf_area=ospf_area,
                bgp_asn=bgp_asn,
            )
            created_names.append(ifname)
            print_success(
                f"  {ifname}  ip={ip}/32  vrf={vrf}  "
                f"tag={tag}  protocol={protocol}"
            )

        if rolled_back:
            print_warn(
                f"Rolled back {len(rolled_back)}: "
                f"{', '.join(rolled_back)}"
            )

        if (
            created_names
            and protocol in ("OSPF", "BGP")
            and self.frr.is_available()
        ):
            print_info("FRR config after changes:")
            self.display.show_frr_full(self.frr)

        return created_names

    @staticmethod
    def _sanitize_tag(tag: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "", tag)[:20] or "default"

    @staticmethod
    def _normalize_area(area: str) -> str:
        try:
            return str(IPv4Address(area))
        except ValueError:
            try:
                return str(IPv4Address(int(area)))
            except (ValueError, OverflowError):
                return OSPF_AREA_DEFAULT

    def _ask_ip_mode(
        self, count: int
    ) -> Tuple[Optional[str], Optional[str]]:
        print(
            f"\n  {C.BOLD}IP Mode:{C.RESET}  "
            f"{C.CYAN}[1]{C.RESET} Random (RFC1918)  "
            f"{C.CYAN}[2]{C.RESET} From subnet"
        )
        if prompt("Mode", "1") == "2":
            while True:
                sub = prompt("Subnet (e.g. 10.100.0.0/24)")
                if not sub:
                    print_error("Subnet cannot be empty.")
                    continue
                valid, err = IPUtils.validate_subnet(sub)
                if valid:
                    return "subnet", str(
                        ip_network(sub, strict=False)
                    )
                print_error(f"Invalid subnet: {err}")
        return "random", None

    def _ask_protocol(self) -> str:
        print(
            f"\n  {C.BOLD}Protocol:{C.RESET}  "
            f"{C.CYAN}[1]{C.RESET} None  "
            f"{C.CYAN}[2]{C.RESET} OSPF  "
            f"{C.CYAN}[3]{C.RESET} BGP"
        )
        if not self.frr.is_available():
            print(
                f"    {C.DIM}(FRR not available — "
                f"only 'None' is functional){C.RESET}"
            )
        return {
            "1": "None", "2": "OSPF", "3": "BGP"
        }.get(prompt("Protocol", "1"), "None")

    def _ask_ospf_method(self) -> str:
        print(
            f"\n  {C.BOLD}OSPF Method:{C.RESET}  "
            f"{C.CYAN}[1]{C.RESET} network statement  "
            f"{C.CYAN}[2]{C.RESET} interface-level"
        )
        return (
            "interface"
            if prompt("Method", "1") == "2"
            else "network"
        )

# ─────────────────────────────────────────────────────────────────────────────
#  CLEANUP MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class CleanupManager:

    def __init__(
        self,
        state:   StateManager,
        kernel:  KernelManager,
        frr:     FRRManager,
        display: DisplayManager,
    ):
        self.state   = state
        self.kernel  = kernel
        self.frr     = frr
        self.display = display

    def run(self) -> None:
        print_header("Cleanup Loopback Interfaces")
        state_data = self.state.get_all()
        if not state_data:
            print_info("No tracked interfaces found.")
            return

        self.display.show_interfaces()

        print(f"\n{C.BOLD}Cleanup Options:{C.RESET}")
        print(f"  {C.CYAN}[1]{C.RESET} Keep all (no changes)")
        print(f"  {C.CYAN}[2]{C.RESET} Delete ALL tracked interfaces")
        print(f"  {C.CYAN}[3]{C.RESET} Delete by tag")
        print(f"  {C.CYAN}[4]{C.RESET} Delete by interface name")

        choice = prompt("Choice", "1")
        if   choice == "1": print_info("No changes made.")
        elif choice == "2": self._delete_all(state_data)
        elif choice == "3": self._delete_by_tag(state_data)
        elif choice == "4": self._delete_by_name(state_data)
        else:               print_error(f"Invalid choice: '{choice}'")

        print_info("Updated interface table:")
        self.display.show_interfaces()

    def _delete_one(
        self,
        ifname:  str,
        meta:    Dict,
        verbose: bool = True,
    ) -> bool:
        ip          = meta.get("ip", "")
        prefix_len  = meta.get("prefix_len", 32)
        vrf         = meta.get("vrf", "GRT")
        protocol    = meta.get("protocol", "None")
        ospf_method = meta.get("ospf_method", "none")
        ospf_area   = meta.get("ospf_area", OSPF_AREA_DEFAULT)
        bgp_asn     = meta.get("bgp_asn", "")
        frr_vrf     = vrf if vrf != "GRT" else None

        log.info(
            f"_delete_one: {ifname}  ip={ip}  vrf={vrf}  "
            f"proto={protocol}  bgp_asn='{bgp_asn}'"
        )
        if verbose:
            print_info(
                f"Removing {ifname}  "
                f"(ip={ip}/32  vrf={vrf}  protocol={protocol})"
            )

        if self.frr.is_available() and ip and protocol != "None":
            if protocol == "OSPF":
                if ospf_method == "network":
                    log.info(
                        f"  [FRR] Removing OSPF network "
                        f"{ip}/{prefix_len} area {ospf_area}"
                    )
                    self.frr.remove_ospf_network(
                        ip, prefix_len, ospf_area, frr_vrf
                    )
                elif ospf_method == "interface":
                    log.info(
                        f"  [FRR] Removing OSPF interface "
                        f"{ifname} area {ospf_area}"
                    )
                    self.frr.remove_ospf_interface(
                        ifname, ospf_area, frr_vrf
                    )
                else:
                    self.frr.remove_ospf_network(
                        ip, prefix_len, ospf_area, frr_vrf
                    )
                    self.frr.remove_ospf_interface(
                        ifname, ospf_area, frr_vrf
                    )
            elif protocol == "BGP":
                log.info(
                    f"  [FRR] Removing BGP network "
                    f"{ip}/{prefix_len} (vrf={vrf})"
                )
                if verbose:
                    print_info(
                        f"  Removing BGP network {ip}/32 "
                        f"(vrf={vrf}  asn={bgp_asn or 'auto'}) …"
                    )
                ok = self.frr.remove_bgp_network(
                    ip, prefix_len,
                    vrf=frr_vrf,
                    explicit_asn=bgp_asn,
                )
                if ok and verbose:
                    print_success(
                        f"  BGP network {ip}/32 removed (vrf={vrf})"
                    )

        log.info(f"  [Kernel] Deleting interface {ifname}")
        self.kernel.delete_interface(ifname)

        if self.frr.is_available():
            log.info(
                f"  [FRR] Removing interface stanza for {ifname}"
            )
            self.frr.remove_interface(ifname)

        log.info(f"  [State] Removing {ifname} from state file")
        self.state.remove(ifname)

        if verbose:
            print_success(f"Deleted: {ifname}")
        return True

    def emergency_cleanup(self) -> None:
        state_data = self.state.get_all()
        if not state_data:
            print_info("No tracked interfaces to clean up.")
            return

        print(
            f"\n{C.WARN}{'═' * 64}\n"
            f"  Emergency Cleanup\n"
            f"{'═' * 64}{C.RESET}"
        )
        tbl = make_table(
            "Interface", "IP", "VRF", "Protocol", "Tag"
        )
        for name, meta in state_data.items():
            tbl.add_row([
                name,
                f"{meta.get('ip', '-')}/"
                f"{meta.get('prefix_len', 32)}",
                meta.get("vrf", "-"),
                meta.get("protocol", "-"),
                meta.get("tag", "-"),
            ])
        print(tbl)

        master_ans = prompt(
            "\nDelete ALL configuration made by this script? [yes/No]",
            "no",
        )
        if master_ans.lower() != "yes":
            print_info(
                "Configuration preserved. "
                f"State file: {STATE_FILE}"
            )
            return

        log.info("Emergency cleanup initiated")
        for ifname, meta in list(state_data.items()):
            print(
                f"\n  {C.CYAN}Interface:{C.RESET} {ifname}  "
                f"ip={meta.get('ip', '-')}/"
                f"{meta.get('prefix_len', 32)}  "
                f"vrf={meta.get('vrf', '-')}  "
                f"protocol={meta.get('protocol', '-')}"
            )
            ans = prompt(f"  Delete {ifname}? [yes/No]", "no")
            if ans.lower() != "yes":
                log.info(f"Emergency cleanup: skipped {ifname}")
                print_info(f"  Skipped: {ifname}")
                continue

            log.info(f"Emergency cleanup: deleting {ifname}")
            ip       = meta.get("ip", "")
            vrf      = meta.get("vrf", "GRT")
            protocol = meta.get("protocol", "None")
            bgp_asn  = meta.get("bgp_asn", "")
            frr_vrf  = vrf if vrf != "GRT" else None

            if self.frr.is_available() and ip and protocol != "None":
                print_info(
                    f"    [FRR] Removing {protocol} for {ip}"
                )
            if protocol == "OSPF":
                ospf_method = meta.get("ospf_method", "none")
                ospf_area   = meta.get("ospf_area", OSPF_AREA_DEFAULT)
                if ospf_method == "network":
                    self.frr.remove_ospf_network(
                        ip, meta.get("prefix_len", 32),
                        ospf_area, frr_vrf,
                    )
                else:
                    self.frr.remove_ospf_interface(
                        ifname, ospf_area, frr_vrf
                    )
            elif protocol == "BGP":
                self.frr.remove_bgp_network(
                    ip, meta.get("prefix_len", 32),
                    vrf=frr_vrf, explicit_asn=bgp_asn,
                )

            print_info(f"    [Kernel] Deleting {ifname}")
            self.kernel.delete_interface(ifname)

            if self.frr.is_available():
                print_info(
                    f"    [FRR] Removing interface stanza {ifname}"
                )
                self.frr.remove_interface(ifname)

            print_info(f"    [State] Removing {ifname}")
            self.state.remove(ifname)
            print_success(f"  {ifname} deleted")
            log.info(f"Emergency cleanup: {ifname} deleted")

        managed_vrfs = self.state.get_all_vrfs()
        if managed_vrfs:
            print(f"\n{C.BOLD}Script-managed VRFs:{C.RESET}")
            for vrf_name, vrf_meta in managed_vrfs.items():
                print(
                    f"  {C.CYAN}{vrf_name}{C.RESET}  "
                    f"table={vrf_meta.get('table', '-')}"
                )
            del_vrfs = prompt(
                "Delete script-managed VRFs? [yes/No]", "no"
            )
            if del_vrfs.lower() == "yes":
                for vrf_name in list(managed_vrfs.keys()):
                    ans = prompt(
                        f"  Delete VRF '{vrf_name}'? [yes/No]", "no"
                    )
                    if ans.lower() != "yes":
                        continue
                    log.info(
                        f"Emergency cleanup: deleting VRF {vrf_name}"
                    )
                    print_info(
                        f"    [Kernel] Deleting VRF device {vrf_name}"
                    )
                    self.kernel.delete_vrf_device(vrf_name)
                    print_info(
                        f"    [FRR] Removing complete FRR config "
                        f"for VRF {vrf_name}"
                    )
                    # Use complete removal in emergency path too
                    self.frr.remove_vrf_complete(vrf_name)
                    self.state.remove_vrf(vrf_name)
                    print_success(f"  VRF '{vrf_name}' deleted")

        print_success("Emergency cleanup complete.")
        log.info("Emergency cleanup complete")

    def _confirm(self, targets: List[str]) -> bool:
        print(
            f"\n{C.WARN}The following interfaces will be "
            f"deleted:{C.RESET}"
        )
        for t in targets:
            print(f"  • {t}")
        return (
            prompt(
                f"Confirm deletion of {len(targets)} item(s)? [yes/N]",
                "n",
            ).lower() == "yes"
        )

    def _delete_all(self, state_data: Dict) -> None:
        if self._confirm(list(state_data.keys())):
            for ifname, meta in state_data.items():
                self._delete_one(ifname, meta)
        else:
            print_info("Deletion cancelled.")

    def _delete_by_tag(self, state_data: Dict) -> None:
        tags = sorted(
            {v.get("tag", "-") for v in state_data.values()}
        )
        print(f"\n{C.BOLD}Available tags:{C.RESET}")
        for t in tags:
            n = sum(
                1 for v in state_data.values()
                if v.get("tag") == t
            )
            print(f"  {C.CYAN}{t}{C.RESET} ({n} interface(s))")
        tag = prompt("Tag to delete")
        if not tag:
            print_error("No tag entered.")
            return
        targets = [
            k for k, v in state_data.items()
            if v.get("tag") == tag
        ]
        if not targets:
            print_error(f"No interfaces with tag '{tag}'.")
            return
        if self._confirm(targets):
            for ifname in targets:
                self._delete_one(ifname, state_data[ifname])
        else:
            print_info("Deletion cancelled.")

    def _delete_by_name(self, state_data: Dict) -> None:
        print(f"\n{C.BOLD}Tracked interfaces:{C.RESET}")
        for ifname in sorted(state_data):
            m = state_data[ifname]
            print(
                f"  {C.CYAN}{ifname}{C.RESET}  "
                f"ip={m.get('ip', '-')}  "
                f"vrf={m.get('vrf', '-')}  "
                f"tag={m.get('tag', '-')}  "
                f"protocol={m.get('protocol', '-')}"
            )
        raw = prompt("Interface name(s) to delete (comma-sep)")
        if not raw:
            print_error("No input provided.")
            return
        targets = [n.strip() for n in raw.split(",") if n.strip()]
        valid   = [t for t in targets if t in state_data]
        invalid = [t for t in targets if t not in state_data]
        if invalid:
            print_warn(
                f"Not tracked — will skip: {', '.join(invalid)}"
            )
        if not valid:
            print_error("No valid interfaces selected.")
            return
        if self._confirm(valid):
            for ifname in valid:
                self._delete_one(ifname, state_data[ifname])
        else:
            print_info("Deletion cancelled.")

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN APPLICATION
# ─────────────────────────────────────────────────────────────────────────────
class LoopGenApp:

    def __init__(self):
        self.state   = StateManager()
        self.kernel  = KernelManager()
        self.frr     = FRRManager()
        self.display = DisplayManager(self.state, self.kernel)
        self.cleanup = CleanupManager(
            self.state, self.kernel, self.frr, self.display
        )
        self.creator = LoopbackCreator(
            self.state, self.kernel, self.frr, self.display
        )
        self.vrf_mgr = VRFManager(
            self.state, self.kernel, self.frr, self.cleanup
        )
        self.if_mgr  = InterfaceManager(
            self.state, self.kernel, self.frr, self.display
        )
        self._setup_signal_handlers()

    def _setup_signal_handlers(self) -> None:
        def _handler(signum, frame):
            sig_names = {
                signal.SIGINT:  "SIGINT  (Ctrl+C)",
                signal.SIGTSTP: "SIGTSTP (Ctrl+Z)",
                signal.SIGQUIT: "SIGQUIT (Ctrl+\\)",
            }
            sig_label = sig_names.get(signum, f"signal {signum}")
            print(
                f"\n\n{C.WARN}{'═' * 64}\n"
                f"  Abrupt exit: {sig_label}\n"
                f"{'═' * 64}{C.RESET}"
            )
            log.warning(
                f"Abrupt exit: {sig_label} (PID {os.getpid()})"
            )
            try:
                self.cleanup.emergency_cleanup()
            except Exception as exc:
                log.error(f"Emergency cleanup error: {exc}")
                print_error(f"Emergency cleanup error: {exc}")
            sys.exit(0)

        signal.signal(signal.SIGINT,  _handler)
        signal.signal(signal.SIGQUIT, _handler)
        try:
            signal.signal(signal.SIGTSTP, _handler)
        except (OSError, AttributeError):
            pass

    def _banner(self) -> None:
        print(f"""
{C.HEADER}
  ██╗      ██████╗  ██████╗ ██████╗  ██████╗ ███████╗███╗   ██╗
  ██║     ██╔═══██╗██╔═══██╗██╔══██╗██╔════╝ ██╔════╝████╗  ██║
  ██║     ██║   ██║██║   ██║██████╔╝██║  ███╗█████╗  ██╔██╗ ██║
  ██║     ██║   ██║██║   ██║██╔═══╝ ██║   ██║██╔══╝  ██║╚██╗██║
  ███████╗╚██████╔╝╚██████╔╝██║     ╚██████╔╝███████╗██║ ╚████║
  ╚══════╝ ╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚══════╝╚═╝  ╚═══╝
{C.RESET}
  {C.DIM}Production Loopback Manager + FRR  |  v{APP_VERSION}{C.RESET}
  {C.DIM}State : {STATE_FILE}{C.RESET}
  {C.DIM}Log   : {LOG_FILE}{C.RESET}
""")
        frr_s   = (
            f"{C.SUCCESS}OK{C.RESET}"
            if self.frr.is_available()
            else f"{C.ERROR}N/A{C.RESET}"
        )
        tracked = len(self.state.get_all())
        vrfs    = len(self.kernel.get_vrfs())
        print(
            f"  FRR: {frr_s}  |  "
            f"{C.CYAN}Tracked: {tracked}{C.RESET}  |  "
            f"{C.CYAN}VRFs detected: {vrfs}{C.RESET}\n"
        )

    def _menu(self) -> str:
        print(
            f"\n{C.BOLD}{'─' * 52}\n"
            f"  Main Menu\n"
            f"{'─' * 52}{C.RESET}"
        )
        for key, label in [
            ("1", "Show Interfaces"),
            ("2", "Create Loopbacks"),
            ("3", "Cleanup Loopbacks"),
            ("4", "Show FRR Running Config"),
            ("5", "Show Detected VRFs"),
            ("6", "VRF Manager          ← create / delete VRFs"),
            ("7", "Interface Manager    ← move / reconfigure IPs"),
            ("8", "Exit"),
        ]:
            print(f"  {C.CYAN}[{key}]{C.RESET} {label}")
        print(f"{'─' * 52}")
        return prompt("Select", "1")

    def run(self) -> None:
        self._banner()
        while True:
            choice = self._menu()
            if   choice == "1":
                self.display.show_interfaces()
            elif choice == "2":
                self.creator.run()
            elif choice == "3":
                self.cleanup.run()
            elif choice == "4":
                if self.frr.is_available():
                    self.display.show_frr_full(self.frr)
                else:
                    print_error("FRR is not available.")
            elif choice == "5":
                self._show_vrfs_summary()
            elif choice == "6":
                self.vrf_mgr.run()
            elif choice == "7":
                self.if_mgr.run()
            elif choice == "8":
                print(
                    f"\n{C.SUCCESS}Goodbye! "
                    f"State saved to {STATE_FILE}{C.RESET}\n"
                )
                break
            else:
                print_error(f"Invalid option: '{choice}'")

    def _show_vrfs_summary(self) -> None:
        print_header("Detected VRFs")
        vrfs = self.kernel.get_vrfs()
        if not vrfs:
            print_info(
                "No VRF devices found — only GRT available."
            )
            return
        tbl = make_table("VRF Name", "Table ID", "Ifindex")
        for name, d in sorted(vrfs.items()):
            tbl.add_row(
                [name, d.get("table", "-"), d.get("ifindex", "-")]
            )
        print(tbl)

# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    if sys.platform != "linux":
        sys.exit(
            f"{C.ERROR}[FATAL] LoopGen is Linux-only.{C.RESET}"
        )
    if sys.version_info < (3, 8):
        sys.exit(
            f"{C.ERROR}[FATAL] Python 3.8+ required. "
            f"Found: {sys.version}{C.RESET}"
        )
    if os.geteuid() != 0:
        sys.exit(
            f"{C.ERROR}[FATAL] Root privileges required.\n"
            f"        Run: sudo python3 {sys.argv[0]}{C.RESET}"
        )
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(
            f"{C.ERROR}[FATAL] Cannot access state directory "
            f"{STATE_FILE.parent}: {exc}{C.RESET}"
        )

    log.info(f"LoopGen v{APP_VERSION} starting (PID {os.getpid()})")
    LoopGenApp().run()
    log.info("LoopGen exiting cleanly")


if __name__ == "__main__":
    main()