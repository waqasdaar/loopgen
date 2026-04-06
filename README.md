<div align="center">

```
  ██╗      ██████╗  ██████╗ ██████╗  ██████╗ ███████╗███╗   ██╗
  ██║     ██╔═══██╗██╔═══██╗██╔══██╗██╔════╝ ██╔════╝████╗  ██║
  ██║     ██║   ██║██║   ██║██████╔╝██║  ███╗█████╗  ██╔██╗ ██║
  ██║     ██║   ██║██║   ██║██╔═══╝ ██║   ██║██╔══╝  ██║╚██╗██║
  ███████╗╚██████╔╝╚██████╔╝██║     ╚██████╔╝███████╗██║ ╚████║
  ╚══════╝ ╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚══════╝╚═╝  ╚═══╝
```

# LoopGen — VRF-Aware Loopback Interface Manager

**Production-grade interactive CLI for Linux loopback interface lifecycle management with FRR routing integration**

[![Platform](https://img.shields.io/badge/platform-Ubuntu%20Linux-orange?logo=ubuntu)](https://ubuntu.com)
[![Python](https://img.shields.io/badge/python-3.8%20%7C%203.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue?logo=python)](https://python.org)
[![FRR](https://img.shields.io/badge/FRR-8.x%20%7C%209.x%20%7C%2010.x-green)](https://frrouting.org)
[![Version](https://img.shields.io/badge/version-2.9.7-informational)]()
[![License](https://img.shields.io/badge/license-MIT-lightgrey)]()

</div>

---

## Table of Contents

1. [Introduction](#introduction)
2. [Key Features](#key-features)
3. [Architecture Overview](#architecture-overview)
4. [Dependencies](#dependencies)
   - [System Requirements](#system-requirements)
   - [Python Requirements](#python-requirements)
   - [FRR Requirements](#frr-requirements)
5. [Installation](#installation)
   - [Step 1 — Clone or Download](#step-1--clone-or-download)
   - [Step 2 — Install Python Dependencies](#step-2--install-python-dependencies)
   - [Step 3 — Install and Configure FRR](#step-3--install-and-configure-frr)
   - [Step 4 — Verify Installation](#step-4--verify-installation)
6. [Running the Application](#running-the-application)
   - [Starting the Application](#starting-the-application)
   - [Main Menu Reference](#main-menu-reference)
7. [Use Cases](#use-cases)
   - [Use Case 1 — Create a VRF and Loopbacks with BGP Advertisement](#use-case-1--create-a-vrf-and-loopbacks-with-bgp-advertisement)
   - [Use Case 2 — Create Loopbacks with OSPF in an Existing VRF](#use-case-2--create-loopbacks-with-ospf-in-an-existing-vrf)
   - [Use Case 3 — Bulk Loopback Creation Across Multiple VRFs](#use-case-3--bulk-loopback-creation-across-multiple-vrfs)
   - [Use Case 4 — Create Loopbacks from a Specific Subnet](#use-case-4--create-loopbacks-from-a-specific-subnet)
   - [Use Case 5 — Move an Interface to a Different VRF](#use-case-5--move-an-interface-to-a-different-vrf)
   - [Use Case 6 — Reconfigure an IP Address on an Interface](#use-case-6--reconfigure-an-ip-address-on-an-interface)
   - [Use Case 7 — Clean Up All Loopbacks by Tag](#use-case-7--clean-up-all-loopbacks-by-tag)
   - [Use Case 8 — Delete a VRF and All Its Interfaces](#use-case-8--delete-a-vrf-and-all-its-interfaces)
   - [Use Case 9 — Emergency Cleanup on Abrupt Exit](#use-case-9--emergency-cleanup-on-abrupt-exit)
8. [State and Log Files](#state-and-log-files)
9. [FRR BGP VRF Model Reference](#frr-bgp-vrf-model-reference)
10. [Limitations](#limitations)
11. [Troubleshooting](#troubleshooting)

---

## Introduction

**LoopGen** is a production-grade interactive command-line tool for **Ubuntu Linux** that manages the complete lifecycle of loopback (dummy) interfaces in **VRF-aware** network environments. It integrates directly with **FRR (Free Range Routing)** to automate routing protocol advertisement alongside every interface operation.

The tool is designed for **network automation engineers**, **lab operators**, and **infrastructure teams** who need to rapidly provision, test, and decommission loopback prefixes across multiple VRFs without writing one-off scripts or manually coordinating kernel operations with FRR configuration.

### What problem does it solve?

Managing loopbacks in a multi-VRF environment involves at least three separate systems:

| Layer | Manually requires |
|---|---|
| **Linux kernel** | `ip link`, `ip addr`, `ip route` commands |
| **VRF placement** | Precise netlink ordering (enslave before IP assign) |
| **FRR routing** | `vtysh` config for OSPF/BGP per VRF |
| **State tracking** | Custom scripts to remember what was created |

LoopGen handles all four layers from a single interactive menu — with validation, rollback on failure, and complete cleanup on exit.

### What makes it different?

- **Zero shell parsing** — all kernel operations use `pyroute2` RTNETLINK directly
- **Atomic VRF placement** — single netlink socket session guarantees interfaces land in the correct VRF routing table, not the main table
- **FRR-aware cleanup** — deleting an interface removes BGP/OSPF advertisements, FRR interface stanzas, and the kernel device in the correct order
- **Full VRF lifecycle** — create and delete entire VRFs including their FRR BGP/OSPF instances, interface stanzas, and kernel devices
- **Emergency cleanup** — `Ctrl+C`, `Ctrl+Z`, and `Ctrl+\` trigger an interactive cleanup wizard

---

## Key Features

| Feature | Description |
|---|---|
| VRF Discovery | Auto-detects all kernel VRF devices via pyroute2 RTNETLINK |
| Atomic VRF Placement | Single socket session: create → enslave → IP assign → UP |
| Loopback Creation | Bulk creation of dummy interfaces with sequential naming |
| IP Allocation | Random RFC1918 or from user-defined subnet |
| OSPF Integration | `network` statement or interface-level `ip ospf area` |
| BGP Integration | Correct `router bgp <asn> vrf <name>` per-VRF model |
| VRF Management | Create/delete VRFs in kernel and FRR in one step |
| Interface Manager | Move interfaces between VRFs, reconfigure IP addresses |
| FRR Cleanup | Removes BGP/OSPF advertisements AND FRR interface stanzas |
| State Persistence | JSON state file survives restarts |
| Emergency Cleanup | Signal handlers for `SIGINT`, `SIGTSTP`, `SIGQUIT` |
| Idempotency | State file prevents duplicate creation |
| Audit Logging | Full DEBUG log at `/var/tmp/loopgen.log` |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         LoopGen CLI                                │
├──────────────┬───────────────┬────────────────┬────────────────────┤
│ StateManager │ KernelManager │  FRRManager    │  DisplayManager    │
│              │               │                │                    │
│ JSON file    │  pyroute2     │  vtysh         │  PrettyTable       │
│ /var/tmp/    │  RTNETLINK    │  OSPF / BGP    │  colorama          │
│ loopgen_     │  (no shell    │  VRF-aware     │  grouped tables    │
│ state.json   │   parsing)    │  config        │                    │
├──────────────┴───────────────┴────────────────┴────────────────────┤
│  LoopbackCreator │ CleanupManager │ VRFManager │ InterfaceManager  │
└─────────────────────────────────────────────────────────────────────┘
```

### Component responsibilities

| Component | Responsibility |
|---|---|
| `StateManager` | JSON persistence, idempotency, tag/VRF indexing |
| `KernelManager` | All pyroute2 netlink ops — VRF, dummy interfaces, IP |
| `FRRManager` | vtysh OSPF/BGP config and removal, VRF stanza management |
| `IPUtils` | RFC1918 random IP, subnet allocation, conflict detection |
| `DisplayManager` | VRF-grouped PrettyTable display, FRR config output |
| `LoopbackCreator` | Creation wizard with VRF selection and FRR rollback |
| `CleanupManager` | Deletion by tag/name/all with FRR and kernel cleanup |
| `VRFManager` | Complete VRF lifecycle including BGP/OSPF instance removal |
| `InterfaceManager` | Move interfaces between VRFs, reconfigure IP addresses |

---

## Dependencies

### System Requirements

| Requirement | Minimum Version | Notes |
|---|---|---|
| **Operating System** | Ubuntu 20.04 LTS | Ubuntu 22.04 / 24.04 recommended |
| **Linux Kernel** | 5.4+ | Required for VRF RTNETLINK support |
| **Python** | 3.8 | `importlib.metadata` required (stdlib since 3.8) |
| **Privileges** | root / sudo | RTNETLINK write operations require `CAP_NET_ADMIN` |
| **FRR** | 8.0 | Optional — FRR features gracefully disabled if absent |

### Python Requirements

Install using `pip install -r requirements.txt`:

```
# requirements.txt

# Kernel networking — all interface/VRF operations via RTNETLINK
# No shell command parsing used anywhere
pyroute2>=0.7.0

# Terminal tables — grouped interface display
prettytable>=3.0.0

# Colored terminal output
colorama>=0.4.6
```

> **Note:** LoopGen uses only Python standard library modules beyond these three packages (`json`, `logging`, `signal`, `subprocess`, `re`, `ipaddress`, `pathlib`, etc.).

### FRR Requirements

FRR is **optional**. Without it, LoopGen creates and manages kernel interfaces normally — routing protocol options are gracefully disabled.

To use OSPF or BGP integration:

| FRR Component | Purpose |
|---|---|
| `ospfd` | OSPF routing process for loopback advertisement |
| `bgpd` | BGP routing process for loopback advertisement |
| `pimd` | PIM multicast (if running, creates pimreg devices in VRFs) |
| `vtysh` | CLI interface — LoopGen uses this for all FRR operations |

---

## Installation

### Step 1 — Clone or Download

```bash
# Clone the repository
git clone https://github.com/your-org/LoopGen.git
cd LoopGen

# Or download the single script directly
wget https://raw.githubusercontent.com/your-org/LoopGen/main/LoopGen.py
```

### Step 2 — Install Python Dependencies

```bash
# Option A — using requirements.txt (recommended)
sudo pip3 install -r requirements.txt

# Option B — install packages individually
sudo pip3 install pyroute2 prettytable colorama

# Option C — virtual environment (development)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# Run as: sudo venv/bin/python LoopGen.py
```

### Step 3 — Install and Configure FRR

Skip this step if you only need kernel interface management without routing protocol integration.

```bash
# Add FRR official repository
curl -s https://deb.frrouting.org/frr/keys.gpg \
  | sudo tee /usr/share/keyrings/frrouting.gpg > /dev/null

echo "deb [signed-by=/usr/share/keyrings/frrouting.gpg] \
  https://deb.frrouting.org/frr \
  $(lsb_release -s -c) frr-stable" \
  | sudo tee /etc/apt/sources.list.d/frr.list

sudo apt update && sudo apt install -y frr frr-pythontools

# Enable the daemons you need
sudo sed -i 's/bgpd=no/bgpd=yes/'   /etc/frr/daemons
sudo sed -i 's/ospfd=no/ospfd=yes/' /etc/frr/daemons

# Restart FRR to apply daemon changes
sudo systemctl restart frr
sudo systemctl enable frr

# Verify FRR is running
sudo vtysh -c "show version"
```

#### Minimum BGP configuration (required before using BGP features)

```bash
sudo vtysh << 'EOF'
configure terminal
router bgp 65000
 bgp router-id 10.0.0.1
 no bgp ebgp-requires-policy
 address-family ipv4 unicast
 exit-address-family
end
write memory
EOF
```

#### Minimum OSPF configuration (required before using OSPF features)

```bash
sudo vtysh << 'EOF'
configure terminal
router ospf
 ospf router-id 10.0.0.1
end
write memory
EOF
```

### Step 4 — Verify Installation

```bash
# Verify Python dependencies
python3 -c "import pyroute2, prettytable, colorama; print('OK')"

# Quick pre-flight check (no interfaces created)
sudo python3 LoopGen.py --help 2>/dev/null || echo "Run without args"

# Create a test VRF to verify kernel VRF support
sudo ip link add vrf-test type vrf table 99
sudo ip link set vrf-test up
ip link show type vrf
sudo ip link del vrf-test
```

---

## Running the Application

### Starting the Application

LoopGen requires root privileges for RTNETLINK socket operations:

```bash
# Standard invocation
sudo python3 LoopGen.py

# With virtual environment
sudo /path/to/venv/bin/python LoopGen.py
```

On successful startup you will see the banner followed by the main menu:

```
  ██╗      ██████╗  ██████╗ ...
  ...

  Production Loopback Manager + FRR  |  v2.9.7
  State : /var/tmp/loopgen_state.json
  Log   : /var/tmp/loopgen.log

  FRR: OK  |  Tracked: 0  |  VRFs detected: 2

  ────────────────────────────────────────────────────
  Main Menu
  ────────────────────────────────────────────────────
  [1] Show Interfaces
  [2] Create Loopbacks
  [3] Cleanup Loopbacks
  [4] Show FRR Running Config
  [5] Show Detected VRFs
  [6] VRF Manager          ← create / delete VRFs
  [7] Interface Manager    ← move / reconfigure IPs
  [8] Exit
  ────────────────────────────────────────────────────
  ➤  Select [1]:
```

### Main Menu Reference

| Option | Description |
|---|---|
| `[1] Show Interfaces` | Display all interfaces grouped by VRF. Excludes VRF master devices and FRR-internal pimreg devices. |
| `[2] Create Loopbacks` | Interactive wizard: select VRF(s), count, name prefix, IP mode, routing protocol |
| `[3] Cleanup Loopbacks` | Delete tracked interfaces by tag, name, or all — removes FRR config and kernel devices |
| `[4] Show FRR Config` | Print the complete FRR running configuration |
| `[5] Show Detected VRFs` | List all kernel VRF devices with table IDs |
| `[6] VRF Manager` | Create or delete VRFs (kernel + FRR BGP/OSPF/stanza) |
| `[7] Interface Manager` | Move interfaces to different VRFs or reconfigure IP addresses |
| `[8] Exit` | Save state and exit cleanly |

---

## Use Cases

### Use Case 1 — Create a VRF and Loopbacks with BGP Advertisement

**Scenario:** Provision a new VRF `vrf100` with routing table 100, then create 3 loopback interfaces inside it and advertise them via BGP.

#### Step 1 — Create the VRF

```
➤  Select [1]: 6          # VRF Manager
➤  Choice [4]: 1          # Create new VRF
➤  VRF name: vrf100
➤  Routing table ID: 100
➤  Proceed? [y/N]: y

✔  VRF 'vrf100' created (table=100)
```

**What happened:**
- Kernel VRF device `vrf100` created with routing table 100
- FRR stanza `vrf vrf100 / exit-vrf` added to running config
- Entry added to state file

#### Step 2 — Create loopbacks in the new VRF

```
➤  Select [1]: 2          # Create Loopbacks

Available VRFs:
  [0] GRT
  [1] vrf100  (table 100)

➤  VRF numbers: 1
➤  Number of loopbacks: 3
➤  Tag/label: bgp-test
➤  Interface name prefix: loop
➤  IP Mode [1]: 1         # Random RFC1918
➤  Protocol [1]: 3        # BGP
➤  Proceed? [y/N]: y

✔  loop001  ip=10.45.12.7/32   vrf=vrf100  tag=bgp-test  protocol=BGP
✔  loop002  ip=172.19.88.41/32  vrf=vrf100  tag=bgp-test  protocol=BGP
✔  loop003  ip=192.168.44.5/32  vrf=vrf100  tag=bgp-test  protocol=BGP
```

#### Verify in FRR

```bash
sudo vtysh -c "show bgp vrf vrf100 ipv4 unicast"
```

```
BGP table version is 3, local router ID is 10.0.0.1
   Network          Next Hop    Metric  Path
*> 10.45.12.7/32    0.0.0.0          0 i
*> 172.19.88.41/32  0.0.0.0          0 i
*> 192.168.44.5/32  0.0.0.0          0 i
```

#### Verify in kernel

```bash
ip -4 addr show master vrf100
```

```
8: loop001: <BROADCAST,NOARP,UP,LOWER_UP>
    inet 10.45.12.7/32 scope global loop001
9: loop002: <BROADCAST,NOARP,UP,LOWER_UP>
    inet 172.19.88.41/32 scope global loop002
10: loop003: <BROADCAST,NOARP,UP,LOWER_UP>
    inet 192.168.44.5/32 scope global loop003
```

---

### Use Case 2 — Create Loopbacks with OSPF in an Existing VRF

**Scenario:** Create 2 loopback interfaces in `vrf10` using OSPF network statements in area `0.0.0.0`.

```
➤  Select [1]: 2

Available VRFs:
  [0] GRT
  [1] vrf10  (table 10)
  [2] vrf20  (table 20)

➤  VRF numbers: 1
➤  Number of loopbacks: 2
➤  Tag/label: ospf-loopbacks
➤  Interface name prefix: lo
➤  IP Mode [1]: 2         # From subnet
➤  Subnet: 10.100.0.0/24
➤  Protocol [1]: 2        # OSPF
➤  OSPF Method [1]: 1     # network statement
➤  OSPF area [0.0.0.0]:   # Press Enter for default
➤  Proceed? [y/N]: y

✔  lo001  ip=10.100.0.1/32  vrf=vrf10  protocol=OSPF
✔  lo002  ip=10.100.0.2/32  vrf=vrf10  protocol=OSPF
```

#### Verify in FRR

```bash
sudo vtysh -c "show running-config" | grep -A5 "router ospf vrf vrf10"
```

```
router ospf vrf vrf10
 network 10.100.0.1/32 area 0.0.0.0
 network 10.100.0.2/32 area 0.0.0.0
```

---

### Use Case 3 — Bulk Loopback Creation Across Multiple VRFs

**Scenario:** Create loopbacks in both `vrf10` and `vrf20` simultaneously in one session.

```
➤  Select [1]: 2

➤  VRF numbers: 1,2       # Comma-separated selection

─── VRF: vrf10
➤  Number of loopbacks: 4
➤  Tag/label: infra
➤  Interface name prefix: loop
➤  IP Mode: 1             # Random
➤  Protocol: 1            # None

─── VRF: vrf20
➤  Number of loopbacks: 4
➤  Tag/label: infra
➤  Interface name prefix: loop
➤  IP Mode: 1             # Random
➤  Protocol: 3            # BGP
➤  Proceed? [y/N]: y

✔  loop001  ip=10.x.x.x/32  vrf=vrf10  protocol=None
✔  loop002  ip=10.x.x.x/32  vrf=vrf10  protocol=None
✔  loop003  ip=10.x.x.x/32  vrf=vrf10  protocol=None
✔  loop004  ip=10.x.x.x/32  vrf=vrf10  protocol=None
✔  loop005  ip=10.x.x.x/32  vrf=vrf20  protocol=BGP
✔  loop006  ip=10.x.x.x/32  vrf=vrf20  protocol=BGP
✔  loop007  ip=10.x.x.x/32  vrf=vrf20  protocol=BGP
✔  loop008  ip=10.x.x.x/32  vrf=vrf20  protocol=BGP
```

> Use `all` instead of index numbers to iterate every detected VRF including GRT.

---

### Use Case 4 — Create Loopbacks from a Specific Subnet

**Scenario:** Allocate 5 management loopback addresses from `10.200.0.0/24` in `vrf10` with no routing protocol.

```
➤  Select [1]: 2
➤  VRF numbers: 1         # vrf10
➤  Number of loopbacks: 5
➤  Tag/label: mgmt
➤  Interface name prefix: mgmt
➤  IP Mode [1]: 2         # From subnet
➤  Subnet: 10.200.0.0/24
➤  Protocol: 1            # None
➤  Proceed? [y/N]: y

✔  mgmt001  ip=10.200.0.1/32  vrf=vrf10  tag=mgmt
✔  mgmt002  ip=10.200.0.2/32  vrf=vrf10  tag=mgmt
✔  mgmt003  ip=10.200.0.3/32  vrf=vrf10  tag=mgmt
✔  mgmt004  ip=10.200.0.4/32  vrf=vrf10  tag=mgmt
✔  mgmt005  ip=10.200.0.5/32  vrf=vrf10  tag=mgmt
```

IPs are allocated sequentially from the subnet, skipping any already in use on the system.

---

### Use Case 5 — Move an Interface to a Different VRF

**Scenario:** Move `loop001` from `vrf10` to `vrf20` and advertise it in BGP in the new VRF.

```
➤  Select [1]: 7          # Interface Manager
➤  Choice [3]: 1          # Move interface to a different VRF

Select Interface
────────────────
VRF: vrf10
┌───┬─────────┬───────┬──────────────────┬─────┬──────────┐
│ # │Interface│ State │ IP Address       │ Tag │ Protocol │
├───┼─────────┼───────┼──────────────────┼─────┼──────────┤
│ 0 │ loop001 │ UP    │ 10.200.0.1/32    │ mgmt│ None     │
│ 1 │ loop002 │ UP    │ 10.200.0.2/32    │ mgmt│ None     │
└───┴─────────┴───────┴──────────────────┴─────┴──────────┘

➤  Enter interface # or name: 0

Available VRFs:
  [0] GRT
  [1] vrf10  (table 10)
  [2] vrf20  (table 20)

➤  Target VRF # or name: 2

Plan:  Move loop001 → VRF vrf20
➤  Proceed? [y/N]: y

  Advertise 10.200.0.1/32 in a routing protocol?
  [1] No  [2] OSPF  [3] BGP
➤  Choice: 3

✔  BGP advertisement added for 10.200.0.1/32
✔  loop001 moved to VRF 'vrf20'.
```

---

### Use Case 6 — Reconfigure an IP Address on an Interface

**Scenario:** Change the IP address on `mgmt003` from `10.200.0.3/32` to `172.16.50.10/32`.

```
➤  Select [1]: 7          # Interface Manager
➤  Choice [3]: 2          # Reconfigure IP address

[interface selection table shown — grouped by VRF]

➤  Enter interface # or name: mgmt003

Current IPs on mgmt003:
  10.200.0.3/32

➤  Keep existing IP address(es)? [y/N]: n
➤  New IP address: 172.16.50.10

Plan:  Replace IPs on mgmt003 with 172.16.50.10/32
➤  Proceed? [y/N]: y

  Advertise 172.16.50.10/32 in a routing protocol?
  [1] No  [2] OSPF  [3] BGP
➤  Choice: 1

✔  IP reconfigured on mgmt003: 172.16.50.10/32
```

The tool automatically:
1. Removes any existing FRR routing advertisements for the old IP
2. Removes the old IP from the kernel interface
3. Assigns the new IP
4. Offers to advertise the new IP in OSPF or BGP

---

### Use Case 7 — Clean Up All Loopbacks by Tag

**Scenario:** Remove all interfaces tagged `bgp-test` that were created in Use Case 1.

```
➤  Select [1]: 3          # Cleanup Loopbacks

[current interface table shown]

Cleanup Options:
  [1] Keep all
  [2] Delete ALL
  [3] Delete by tag
  [4] Delete by name

➤  Choice: 3

Available tags:
  bgp-test (3 interface(s))
  infra    (8 interface(s))
  mgmt     (5 interface(s))

➤  Tag to delete: bgp-test

Will delete:
  • loop001
  • loop002
  • loop003

➤  Confirm deletion of 3 item(s)? [yes/N]: yes

ℹ  Removing loop001 (ip=10.45.12.7/32  vrf=vrf100  protocol=BGP)
  Removing BGP network 10.45.12.7/32 (vrf=vrf100  asn=65000) …
✔  BGP network 10.45.12.7/32 removed (vrf=vrf100)
✔  Deleted: loop001

ℹ  Removing loop002 (ip=172.19.88.41/32  vrf=vrf100  protocol=BGP)
✔  BGP network 172.19.88.41/32 removed (vrf=vrf100)
✔  Deleted: loop002

ℹ  Removing loop003 (ip=192.168.44.5/32  vrf=vrf100  protocol=BGP)
✔  BGP network 192.168.44.5/32 removed (vrf=vrf100)
✔  Deleted: loop003
```

Each deleted interface is fully cleaned from:
- FRR BGP/OSPF routing advertisements
- FRR interface stanza cache (`no interface <name>`)
- Linux kernel dummy device
- State file

---

### Use Case 8 — Delete a VRF and All Its Interfaces

**Scenario:** Delete `vrf100` which contains loopback interfaces and FRR PIM internal devices.

```
➤  Select [1]: 6          # VRF Manager
➤  Choice [4]: 2          # Delete existing VRF

┌───┬──────────┬──────────┬────────────────────────────────────┐
│ # │ VRF Name │ Table ID │ Enslaved Interfaces                 │
├───┼──────────┼──────────┼────────────────────────────────────┤
│ 0 │ vrf10    │ 10       │ ens224, loop001, loop002, pimreg10  │
│ 1 │ vrf100   │ 100      │ pimreg100, pim6reg100               │
└───┴──────────┴──────────┴────────────────────────────────────┘

FRR configuration to be removed:
  • router bgp 65000 vrf vrf100
  • vrf vrf100

➤  VRF number or name to delete: 1

[enslaved interface table shown]

➤  Delete VRF 'vrf100' and all its interfaces? [yes/N]: yes

  [Step 1] No LoopGen-tracked interfaces.
  [Step 2] Deleting kernel VRF device 'vrf100' …
✔  Kernel VRF device 'vrf100' deleted.
  [Step 3] Clearing FRR PIM state for 'vrf100' …
✔  FRR-internal interfaces cleaned up.
  [Step 4] Removing complete FRR configuration for VRF 'vrf100' …
    [FRR] Removing BGP instance: router bgp 65000 vrf vrf100
✔      BGP instance removed: router bgp 65000 vrf vrf100
    [FRR] Removing VRF stanza: vrf vrf100
✔      VRF stanza removed: vrf100
✔  FRR configuration for 'vrf100' fully removed.
  [Step 5] Purging FRR interface stanzas …
✔  FRR interface stanzas purged.

✔  VRF 'vrf100' deleted successfully.
```

---

### Use Case 9 — Emergency Cleanup on Abrupt Exit

**Scenario:** A user presses `Ctrl+C` while the application is running. LoopGen intercepts the signal and offers to clean up all configuration it created during the session.

```
^C

════════════════════════════════════════════════════════════════
  Abrupt exit: SIGINT  (Ctrl+C)
════════════════════════════════════════════════════════════════

Emergency Cleanup
┌──────────┬────────────────┬────────┬──────────┬─────────┐
│Interface │ IP             │ VRF    │ Protocol │ Tag     │
├──────────┼────────────────┼────────┼──────────┼─────────┤
│ loop001  │ 10.45.12.7/32  │ vrf100 │ BGP      │ bgp-test│
│ loop002  │ 172.19.88.41/32│ vrf100 │ BGP      │ bgp-test│
└──────────┴────────────────┴────────┴──────────┴─────────┘

➤  Delete ALL configuration made by this script? [yes/No]: yes

  Interface: loop001  ip=10.45.12.7/32  vrf=vrf100  protocol=BGP
➤  Delete loop001? [yes/No]: yes

    [FRR] Removing BGP for 10.45.12.7
    [Kernel] Deleting loop001
    [FRR] Removing interface stanza loop001
    [State] Removing loop001
✔  loop001 deleted

  Interface: loop002  ip=172.19.88.41/32  vrf=vrf100  protocol=BGP
➤  Delete loop002? [yes/No]: yes
    ...
✔  loop002 deleted

Script-managed VRFs:
  vrf100  table=100

➤  Delete script-managed VRFs? [yes/No]: yes
➤    Delete VRF 'vrf100'? [yes/No]: yes
    [Kernel] Deleting VRF device vrf100
    [FRR] Removing complete FRR config for VRF vrf100
✔  VRF 'vrf100' deleted

✔  Emergency cleanup complete.
```

> **Signal support:** `Ctrl+C` (SIGINT), `Ctrl+Z` (SIGTSTP), and `Ctrl+\` (SIGQUIT) all trigger this flow.

---

## State and Log Files

### State file — `/var/tmp/loopgen_state.json`

Persists all created interfaces and VRFs across sessions. Written atomically (write-then-rename) to prevent corruption.

```json
{
  "version": "2.9.7",
  "interfaces": {
    "loop001": {
      "interface":   "loop001",
      "ip":          "10.45.12.7",
      "prefix_len":  32,
      "vrf":         "vrf100",
      "tag":         "bgp-test",
      "protocol":    "BGP",
      "ospf_method": "none",
      "ospf_area":   "0.0.0.0",
      "bgp_asn":     "65000",
      "created_at":  "2025-04-06T10:30:00Z"
    }
  },
  "vrfs": {
    "vrf100": {
      "table":      100,
      "created_at": "2025-04-06T10:25:00Z"
    }
  }
}
```

### Log file — `/var/tmp/loopgen.log`

Full DEBUG-level audit trail of every operation.

```bash
# Follow live activity
sudo tail -f /var/tmp/loopgen.log

# Show only errors
sudo grep -i "error\|ERROR" /var/tmp/loopgen.log

# Show BGP removal steps
sudo grep -i "bgp" /var/tmp/loopgen.log

# Show all vtysh commands issued
sudo grep "vtysh:" /var/tmp/loopgen.log
```

---

## FRR BGP VRF Model Reference

LoopGen uses the correct FRR per-VRF BGP instance model. Understanding this prevents configuration confusion when inspecting FRR manually.

### Correct syntax (what LoopGen uses)

```
# Global Routing Table:
router bgp 65000
  address-family ipv4 unicast
    network 10.1.2.3/32
  exit-address-family

# VRF instance — VRF name is on the ROUTER line:
router bgp 65000 vrf vrf20
  address-family ipv4 unicast
    network 172.16.50.10/32
  exit-address-family
```

### Invalid syntax (causes FRR error)

```
# WRONG — FRR rejects this with "Unknown command":
router bgp 65000
  address-family ipv4 unicast vrf vrf20   ← INVALID
    network 172.16.50.10/32
```

---

## Limitations

The following limitations exist in the current version (`v2.9.7`):

### 1. IPv4 Only

LoopGen supports **IPv4 addresses only**. IPv6 interface assignment, IPv6 BGP (`address-family ipv6 unicast`), and OSPFv3 are not implemented.

### 2. Single-Node Operation

LoopGen manages the local Linux system only. It has no mechanism to push configuration to remote nodes, integrate with Ansible/Netconf/RESTCONF, or synchronise state across multiple hosts.

### 3. FRR BGP Peer Configuration

LoopGen configures BGP **network advertisements** (`network X.X.X.X/Y`) but does **not** configure BGP peer relationships (`neighbor`, `peer-group`, `route-reflector`). A BGP process with at least one peer must be pre-configured before LoopGen's BGP features are useful.

### 4. OSPF Process Must Pre-exist

LoopGen adds OSPF network statements or interface-level `ip ospf area` config, but it does **not** create the OSPF router process itself. You must run `router ospf` (GRT) or `router ospf vrf <name>` (VRF) in vtysh before using OSPF features.

### 5. No Interface Sub-types

Only **dummy (loopback-equivalent)** interfaces are created. LoopGen does not create physical, VLAN, bond, bridge, VXLAN, GRE tunnel, or any other interface type.

### 6. No Prefix-Length Customisation for Loopbacks

All created loopback interfaces use `/32` prefix length. Custom prefix lengths (e.g. `/24` loopbacks for summarisation testing) are not supported.

### 7. Single State File — No Multi-Instance Support

The state file is fixed at `/var/tmp/loopgen_state.json`. Running multiple instances simultaneously is not supported and will cause state corruption. The log file is similarly fixed at `/var/tmp/loopgen.log`.

### 8. No Persistent VRF Configuration Across Reboots

VRFs and dummy interfaces created by LoopGen are **not persistent across reboots**. They exist only in the running kernel state. To make them persistent you must integrate with:
- `/etc/network/interfaces` (ifupdown)
- `systemd-networkd` `.netdev` + `.network` files
- A custom `@reboot` cron job or systemd service

### 9. pimreg / pim6reg Cleanup Dependency on FRR PIM State

When a VRF is deleted, FRR-internal PIM registration interfaces (`pimreg<N>`, `pim6reg<N>`) are cleaned up by asking FRR to release its PIM sockets. If the FRR PIM daemon (`pimd`) is not running or does not respond to `clear ip pim vrf X interfaces`, these interfaces may remain orphaned in the kernel's `default` VRF until FRR is restarted.

### 10. No YANG / RESTCONF / gRPC Interface

LoopGen is exclusively an interactive CLI tool. There is no programmatic API, no REST endpoint, and no structured data export beyond the JSON state file.

### 11. FRR `write memory` Not Called After Every Operation

After interface-level operations (loopback creation, BGP network advertisement), LoopGen does **not** call `write memory` to persist FRR config to disk. If FRR restarts, the routing advertisements will be lost. Call `sudo vtysh -c "write memory"` manually after creating interfaces if persistence across FRR restarts is required.

> **Exception:** `remove_vrf_complete()` (called during VRF deletion) does call `write memory` to ensure VRF removal is persisted.

---

## Troubleshooting

### Application will not start

```bash
# Check Python version
python3 --version    # Must be 3.8+

# Check root privileges
sudo python3 LoopGen.py

# Check dependencies
python3 -c "import pyroute2, prettytable, colorama; print('OK')"
```

### FRR shows as N/A in banner

```bash
# Check FRR is running
sudo systemctl status frr

# Test vtysh manually
sudo vtysh -c "show version"

# Restart FRR if needed
sudo systemctl restart frr
```

### Interfaces appear in wrong VRF

Check the log for VRF membership verification:

```bash
sudo grep "verify_vrf_membership" /var/tmp/loopgen.log
```

If interfaces persistently land in GRT, ensure the kernel supports VRF enslavement:

```bash
# Must return 0 (success)
sudo ip link add vrf-test type vrf table 99 && \
sudo ip link add dummy-test type dummy && \
sudo ip link set dummy-test master vrf-test && \
sudo ip link show master vrf-test && \
sudo ip link del dummy-test && \
sudo ip link del vrf-test
```

### Deleted interfaces still visible in `show interface brief`

This means FRR's interface cache was not cleared. Run manually:

```bash
sudo vtysh << 'EOF'
configure terminal
no interface loop001
no interface loop002
end
write memory
EOF
```

### State file has stale entries

```bash
# View current state
sudo cat /var/tmp/loopgen_state.json | python3 -m json.tool

# Remove a specific stale entry
sudo python3 - << 'EOF'
import json
from pathlib import Path
p = Path("/var/tmp/loopgen_state.json")
d = json.loads(p.read_text())
d["interfaces"].pop("loop001", None)
p.write_text(json.dumps(d, indent=2))
print("Done")
EOF
```

### BGP network not removed after cleanup

```bash
# Check what BGP sees
sudo vtysh -c "show bgp vrf vrf100 ipv4 unicast"

# Check the log for removal steps
sudo grep "remove_bgp_network" /var/tmp/loopgen.log

# Manual removal if needed
sudo vtysh << 'EOF'
configure terminal
router bgp 65000 vrf vrf100
 address-family ipv4 unicast
  no network 10.45.12.7/32
 exit-address-family
end
write memory
EOF
```