# LoopGen — Known Limitations

> **Application:** LoopGen — VRF-Aware Loopback Interface Manager  
> **Version:** 2.9.7  
> **Last Updated:** April 2025  
> **Platform:** Ubuntu Linux 20.04 / 22.04 / 24.04 + FRR 8.x / 9.x / 10.x

This document lists all known limitations, constraints, and behavioural
boundaries of LoopGen v2.9.7. Each entry includes the affected area, a
description, the impact on operations, and a workaround where one exists.

---

## Table of Contents

1. [Protocol and Address Family Limitations](#1-protocol-and-address-family-limitations)
2. [Interface and Kernel Limitations](#2-interface-and-kernel-limitations)
3. [FRR Integration Limitations](#3-frr-integration-limitations)
4. [VRF Management Limitations](#4-vrf-management-limitations)
5. [State and Persistence Limitations](#5-state-and-persistence-limitations)
6. [Operational and Scalability Limitations](#6-operational-and-scalability-limitations)
7. [Security Limitations](#7-security-limitations)
8. [Platform Limitations](#8-platform-limitations)
9. [Summary Table](#9-summary-table)

---

## 1. Protocol and Address Family Limitations

| # | Limitation | Area | Description | Impact | Workaround |
|---|---|---|---|---|---|
| L-01 | **IPv4 Only** | Addressing | All IP operations (loopback assignment, BGP/OSPF advertisement, subnet allocation) support IPv4 only. IPv6 is not implemented. | Cannot create IPv6 loopbacks or advertise IPv6 prefixes. | Manually assign IPv6 addresses with `ip -6 addr add` after LoopGen creation. Configure IPv6 routing in `vtysh` manually. |
| L-02 | **No OSPFv3** | Routing — OSPF | Only OSPFv2 (IPv4) is supported. OSPFv3 for IPv6 prefixes is not implemented. | IPv6 OSPF integration unavailable. | Configure OSPFv3 manually in `vtysh`. |
| L-03 | **No BGP IPv6 Address Family** | Routing — BGP | BGP operations use `address-family ipv4 unicast` only. IPv6 unicast, VPNv4, VPNv6, and L2VPN EVPN address families are not supported. | Cannot advertise IPv6 prefixes or EVPN routes via LoopGen. | Configure additional BGP address families manually in `vtysh`. |
| L-04 | **No BGP Community or Policy Attributes** | Routing — BGP | LoopGen issues plain `network X.X.X.X/Y` statements with no route-map, community, local-preference, MED, or AS-path prepend. | All prefixes advertised by LoopGen use BGP defaults. | Apply route-maps and policies manually in `vtysh` after LoopGen advertisement. |
| L-05 | **No OSPF Cost or Timer Control Per Interface** | Routing — OSPF | When using interface-level OSPF (`ip ospf area`), all interfaces are set as `ip ospf passive`. OSPF cost, hello/dead intervals, and authentication are not configurable through LoopGen. | Cannot fine-tune OSPF timers or enable OSPF adjacencies on created loopbacks. | Modify OSPF interface parameters manually in `vtysh` after LoopGen creation. |
| L-06 | **No IS-IS, RIP, or Static Route Integration** | Routing | Only OSPF and BGP are supported. IS-IS, RIP, static route injection, and BFD integration are not implemented. | Cannot advertise loopbacks created by LoopGen into IS-IS or via static routes. | Configure IS-IS or static routes manually in `vtysh`. |
| L-07 | **Fixed /32 Prefix for All Loopbacks** | Addressing | All loopback interfaces created by LoopGen use a `/32` host prefix. Custom prefix lengths are not supported at creation time. | Cannot simulate subnet loopbacks or test route summarisation with non-/32 prefixes. | After creation, manually add a secondary address: `ip addr add 10.0.0.0/24 dev loop001`. |

---

## 2. Interface and Kernel Limitations

| # | Limitation | Area | Description | Impact | Workaround |
|---|---|---|---|---|---|
| L-08 | **Dummy Interfaces Only** | Interface Type | LoopGen creates `dummy` kernel interfaces exclusively. Physical, VLAN, bond, bridge, VXLAN, GRE, macvlan, and all other interface types are not supported. | Cannot use LoopGen to provision production interface types. | Pre-create the required interface type manually, then use LoopGen's Interface Manager (option 7) to assign it to a VRF. |
| L-09 | **No Interface Rename** | Interface Naming | Interface names follow the `<prefix><number>` format (e.g. `loop001`). Renaming an existing interface is not supported by LoopGen. | Cannot change an interface name after creation without deleting and recreating it. | Delete the interface and recreate it with the desired name prefix. |
| L-10 | **Interface Names Limited to 15 Characters** | Interface Naming | Linux `IFNAMSIZ` limits interface names to 15 characters. Long prefixes are silently truncated by LoopGen. | A prefix longer than 12 characters leaves insufficient room for the 3-digit number suffix. | Use short prefixes (≤ 8 characters is recommended). |
| L-11 | **No Persistence Across Reboots** | Kernel State | Dummy interfaces and VRF devices exist only in running kernel state. They are lost on system reboot. The LoopGen state file persists but the kernel objects do not. | After a reboot, the LoopGen state file references interfaces that no longer exist in the kernel. | Integrate with `systemd-networkd`, `/etc/network/interfaces`, or write a startup service that recreates interfaces from the LoopGen state file. |
| L-12 | **pimreg/pim6reg Cleanup Requires FRR PIM Cooperation** | FRR-Internal Interfaces | PIM registration interfaces (`pimreg<N>`, `pim6reg<N>`) reject both `RTM_DELLINK` and `IFLA_MASTER=0` from the kernel. LoopGen clears them by asking FRR to release its PIM sockets via `clear ip pim vrf X interfaces`. | If FRR's PIM daemon is unresponsive, pimreg/pim6reg interfaces may remain orphaned in the GRT after VRF deletion. | Restart FRR (`sudo systemctl restart frr`) to force PIM socket release and clean up orphaned devices. |
| L-13 | **No ECMP or Multiple Addresses Per Interface** | IP Addressing | LoopGen creates each loopback with exactly one IP address. ECMP and multiple IP addresses per loopback are not managed through LoopGen. | Cannot simulate ECMP scenarios directly through LoopGen. | Manually add secondary addresses using `ip addr add` after LoopGen creation. |
| L-14 | **Single Master (VRF) Per Interface** | VRF Placement | Each interface can belong to exactly one VRF at a time. Linux does not support an interface belonging to multiple VRFs simultaneously. | Cannot share a loopback between two VRFs. | Create separate loopback interfaces for each VRF that requires the prefix. |

---

## 3. FRR Integration Limitations

| # | Limitation | Area | Description | Impact | Workaround |
|---|---|---|---|---|---|
| L-15 | **BGP Process Must Pre-exist** | FRR — BGP | LoopGen does not create the BGP router process. A `router bgp <asn>` block must already exist in FRR before LoopGen's BGP advertisement features can be used. | BGP loopback creation will fail with "No BGP process found". | Pre-configure BGP: `sudo vtysh -c "configure terminal" -c "router bgp 65000" -c "end"`. |
| L-16 | **OSPF Process Must Pre-exist** | FRR — OSPF | LoopGen does not create the OSPF router process. `router ospf` (GRT) or `router ospf vrf <name>` must already exist before LoopGen's OSPF features can be used. | OSPF loopback creation will skip FRR config with a warning. | Pre-configure OSPF: `sudo vtysh -c "configure terminal" -c "router ospf" -c "end"`. |
| L-17 | **No BGP Peer or Neighbor Configuration** | FRR — BGP | LoopGen configures BGP network advertisements only. It does not configure BGP neighbor statements, peer-groups, route-reflector settings, or session parameters. | Prefixes advertised by LoopGen will not propagate until BGP peers are manually configured. | Configure BGP peers manually in `vtysh`. |
| L-18 | **write memory Not Called After Interface Operations** | FRR — Persistence | After creating loopbacks and adding BGP/OSPF advertisements, LoopGen does not call `write memory`. If FRR restarts, routing advertisements created by LoopGen are lost. | Routing advertisements disappear after an FRR daemon restart or system reboot. | Manually run `sudo vtysh -c "write memory"` after creation operations. Note: LoopGen's VRF deletion does call `write memory` automatically. |
| L-19 | **OSPF Config Parsing is Text-Based** | FRR — OSPF | OSPF existence checks parse `show running-config` text output. Changes in FRR output formatting across major versions could cause false-negative detection. | OSPF removal may occasionally re-attempt an already-removed statement. | Not operationally harmful — the extra `no network` command is silently ignored by FRR if the statement is already absent. |
| L-20 | **BGP Existence Check Uses the BGP Routing Table** | FRR — BGP | LoopGen verifies BGP network existence via `show bgp [vrf X] ipv4 unicast` rather than running config. Prefixes not yet in the BGP RIB (e.g. suppressed by route-maps) will be missed by the existence check. | A policy-suppressed prefix may not be removed correctly by LoopGen cleanup. | Review FRR route-map and policy configuration if a prefix is not appearing in `show bgp`. |
| L-21 | **Single ASN Assumption Across All VRFs** | FRR — BGP | LoopGen assumes all VRF BGP instances share the same ASN as the GRT BGP process (`router bgp <asn>`). Configurations with different ASNs per VRF are not supported. | Incorrect router context would be selected for VRFs with a different ASN. | Not applicable in standard FRR deployments — FRR itself requires a consistent ASN across VRF BGP instances. |
| L-22 | **No FRR Version Compatibility Checking** | FRR — General | LoopGen does not check the FRR version before issuing commands. Command syntax differences between FRR major versions could cause unexpected failures on unusual builds. | Commands may fail on incompatible FRR builds. | LoopGen is tested on FRR 8.x, 9.x, and 10.x. Check `/var/tmp/loopgen.log` for vtysh output if commands appear to fail. |
| L-23 | **No FRR Startup Config Modification** | FRR — Persistence | LoopGen modifies FRR running config only. It does not write to `/etc/frr/frr.conf` directly. | After `systemctl restart frr`, all LoopGen-applied FRR config is lost unless `write memory` was called first. | Run `sudo vtysh -c "write memory"` after completing LoopGen creation operations. |

---

## 4. VRF Management Limitations

| # | Limitation | Area | Description | Impact | Workaround |
|---|---|---|---|---|---|
| L-24 | **VRF Routing Table IDs Must Be Unique System-Wide** | VRF — Creation | LoopGen validates that the chosen routing table ID is not already used by known VRFs. However, it does not scan `/etc/iproute2/rt_tables` for reserved or externally-used IDs. | A table ID conflict with a non-VRF route table could cause routing anomalies. | Check `cat /etc/iproute2/rt_tables` and avoid IDs 0 (unspec), 253 (default), 254 (main), 255 (local), and any IDs used by external tools. |
| L-25 | **Cannot Rename a VRF** | VRF — Management | VRF renaming is not supported by LoopGen. A VRF must be deleted and recreated with the new name. | Renaming requires deleting all interfaces in the VRF first, then recreating the VRF and its interfaces. | Delete the VRF via LoopGen's VRF Manager, then recreate it with the desired name. |
| L-26 | **VRF Deletion Requires FRR PIM to be Responsive** | VRF — Deletion | Cleaning up `pimreg`/`pim6reg` devices during VRF deletion depends on FRR's PIM daemon responding to `clear ip pim vrf X interfaces`. An unresponsive PIM daemon prevents automatic cleanup. | Orphaned pimreg interfaces may remain visible in GRT. This is cosmetic and does not affect routing. | Restart FRR (`sudo systemctl restart frr`) to force PIM socket cleanup after VRF deletion. |
| L-27 | **Only LoopGen-Tracked Interfaces Receive Full FRR Cleanup** | VRF — Deletion | When deleting a VRF, only interfaces tracked in the LoopGen state file receive full FRR cleanup. Untracked interfaces (e.g. physical interfaces enslaved to the VRF externally) only get kernel-level detachment. | FRR interface stanzas for externally-created interfaces may not be removed by LoopGen. | Manually clean up external interface stanzas: `sudo vtysh -c "configure terminal" -c "no interface ens224" -c "end"`. |
| L-28 | **No VRF Import/Export Route Leaking** | VRF — Routing | LoopGen does not configure inter-VRF route leaking, VRF import/export policies, or VPN route targets in FRR. | Cannot implement hub-and-spoke or shared-services VRF topologies through LoopGen. | Configure route leaking manually in `vtysh` using BGP VPNv4 or `ip route` commands. |

---

## 5. State and Persistence Limitations

| # | Limitation | Area | Description | Impact | Workaround |
|---|---|---|---|---|---|
| L-29 | **Fixed State File Location** | State — Storage | The LoopGen state file is always written to `/var/tmp/loopgen_state.json`. This path is not configurable via the application. | Cannot run multiple independent LoopGen instances with separate state files. | Manually copy the state file between sessions if isolation is needed. |
| L-30 | **No Multi-Instance Concurrency** | State — Locking | The state file has no file locking. Running two simultaneous LoopGen instances on the same system will cause state corruption. | Race conditions on concurrent writes will produce a corrupted or inconsistent state file. | Never run two LoopGen instances simultaneously. Use `pgrep -f loopgen` to verify no instance is running before starting. |
| L-31 | **State File Is Not Encrypted** | State — Security | The LoopGen state file contains IP addresses, VRF names, BGP ASNs, and interface metadata in plain-text JSON. It is readable by any root process. | Sensitive network topology information is exposed at `/var/tmp/loopgen_state.json`. | Restrict file permissions: `sudo chmod 600 /var/tmp/loopgen_state.json`. Consider relocating it to `/root/loopgen_state.json`. |
| L-32 | **Stale State After Manual Interface Deletion** | State — Consistency | If a kernel interface or VRF is deleted outside LoopGen (e.g. `ip link del loop001`), the LoopGen state file retains the stale entry. LoopGen does not auto-reconcile state with the kernel on startup. | The state file becomes inconsistent with the kernel. `Show Interfaces` may reference interfaces that no longer exist. | Use LoopGen's Cleanup menu to remove the stale entry — it will skip the already-absent kernel device gracefully. |
| L-33 | **No State Backup or Rollback** | State — Management | The state file is overwritten atomically on every save. There is no built-in versioning, backup, or rollback capability. | A power failure during a write could theoretically corrupt the state file (mitigated by atomic rename). | Back up manually before large operations: `cp /var/tmp/loopgen_state.json ~/loopgen_backup_$(date +%F).json`. |
| L-34 | **Log File Is Not Rotated** | Logging | `/var/tmp/loopgen.log` grows without bound. LoopGen has no built-in log rotation. | On systems with heavy LoopGen usage the log file may consume significant disk space in `/var/tmp`. | Configure `logrotate`: `echo '/var/tmp/loopgen.log { daily rotate 7 compress missingok }' \| sudo tee /etc/logrotate.d/loopgen`. |

---

## 6. Operational and Scalability Limitations

| # | Limitation | Area | Description | Impact | Workaround |
|---|---|---|---|---|---|
| L-35 | **Interactive CLI Only — No Automation API** | Operations | LoopGen is exclusively an interactive terminal application. It has no REST API, gRPC interface, NETCONF/YANG model, Python importable library, or non-interactive CLI argument mode. | Cannot be called directly from Ansible, Terraform, CI/CD pipelines, or other automation tools. | For automated workflows, use raw `ip` commands + `vtysh` directly, or pipe responses to LoopGen using `expect`. |
| L-36 | **Single-Node Scope** | Operations | LoopGen manages only the local Linux system. It cannot push configuration to remote hosts or synchronise VRF topology across a fleet of nodes. | Not suitable for multi-node lab provisioning without external wrapper scripts. | Use Ansible or similar tools to distribute LoopGen execution across nodes via SSH. |
| L-37 | **No Bulk Import from Configuration File** | Operations | Interfaces and VRFs must be created interactively through the LoopGen menu. There is no facility to read a YAML/JSON/CSV file and provision in bulk. | Large-scale provisioning (100+ interfaces) requires many interactive prompts. | Automate responses using `expect` or by piping input: `printf "2\n1\n5\nbgp-test\nloop\n1\n3\ny\n" \| sudo python3 loopgen.py`. |
| L-38 | **No Undo or Multi-Step Rollback** | Operations | There is no transaction history or multi-step undo capability in LoopGen. Rollback occurs only per-interface at creation time if FRR config fails. | Cannot undo a series of creation steps as a batch after they complete. | Use LoopGen's Cleanup menu immediately after an unwanted creation to remove specific interfaces by tag or name. |
| L-39 | **No Interface Health Monitoring** | Monitoring | LoopGen displays interface state at query time but does not monitor interfaces continuously, alert on state changes, or track interface flaps. | No notification if a loopback created by LoopGen goes down unexpectedly. | Integrate with external monitoring tools (Prometheus `node_exporter`, Nagios, Zabbix) for continuous interface monitoring. |
| L-40 | **Random IP Generator May Exhaust After 1000 Attempts** | IP Allocation | The random IP generator tries up to 1000 candidates before failing. On systems with thousands of existing RFC1918 addresses, it may fail to find a unique IP. | LoopGen creation fails with "No unique random IP after 1000 attempts". | Use subnet mode instead of random mode when a large number of interfaces already exist on the system. |
| L-41 | **No IPAM Integration** | IP Allocation | LoopGen has no integration with external IPAM systems (NetBox, Infoblox, phpIPAM, etc.). IP allocation is local-only based on the LoopGen state file and the kernel address table. | IPs allocated by LoopGen are not registered in the organisation's IPAM, risking conflicts with other provisioning systems. | Export allocated IPs after provisioning: `sudo python3 -c "import json; d=json.load(open('/var/tmp/loopgen_state.json')); [print(v['ip']) for v in d['interfaces'].values()]"`. |

---

## 7. Security Limitations

| # | Limitation | Area | Description | Impact | Workaround |
|---|---|---|---|---|---|
| L-42 | **Requires Root Privileges for Entire Lifetime** | Security | All LoopGen operations require root (`sudo`) because RTNETLINK write operations require `CAP_NET_ADMIN`. There is no privilege separation or capability dropping after startup. | LoopGen runs with full root access throughout its execution. | Review the LoopGen source code before running. Consider running inside a dedicated container or VM with a limited blast radius. |
| L-43 | **Limited Input Sanitisation for vtysh Arguments** | Security | User inputs (VRF names, interface prefixes, tags) are sanitised with regex but are passed to `vtysh` as command arguments. Carefully crafted inputs could theoretically produce unexpected vtysh commands. | Theoretical command injection risk in adversarial environments. | LoopGen is designed for trusted operators in controlled environments and is not suitable as a public-facing service. |
| L-44 | **vtysh Access Not Authenticated by LoopGen** | Security | LoopGen assumes the operator has unrestricted `vtysh` access. FRR RBAC or AAA authentication for vtysh is not handled by LoopGen. | If FRR vtysh has access controls configured, LoopGen operations may fail silently. | Ensure the user running LoopGen has unrestricted vtysh access (requires root or membership in the `frrvty` group). |
| L-45 | **No Audit Trail Integrity Protection** | Security | Operations are logged to `/var/tmp/loopgen.log` as a local flat file with no integrity protection, no remote syslog forwarding, and no tamper detection. | The log file can be modified or deleted by any root-level process on the system. | Forward logs to a remote syslog server via `rsyslog` or `journald` forwarding for tamper-resistant audit trails. |

---

## 8. Platform Limitations

| # | Limitation | Area | Description | Impact | Workaround |
|---|---|---|---|---|---|
| L-46 | **Linux Only** | Platform | LoopGen uses `pyroute2` (Linux RTNETLINK) and `vtysh` (FRR). It does not run on macOS, FreeBSD, Windows, or any non-Linux platform. | Cannot be used on non-Linux operating systems. | Run LoopGen inside a Linux VM or a privileged Docker container with host networking. |
| L-47 | **Ubuntu / Debian Focused** | Platform | LoopGen is tested on Ubuntu 20.04, 22.04, and 24.04. Other distributions (RHEL, Fedora, Alpine) may work but are untested. Package names and system paths may differ. | Unexpected behaviour is possible on non-Debian distributions. | Adapt the installation steps for your distribution's package manager. The LoopGen Python code itself is distribution-agnostic. |
| L-48 | **Python 3.8 Minimum** | Platform | LoopGen uses `importlib.metadata` (stdlib since Python 3.8) and several 3.8+ type hint features. Python 3.6 and 3.7 are not supported. | LoopGen will not start on systems with Python < 3.8. | Upgrade Python: `sudo apt install python3.10` or use `pyenv`. |
| L-49 | **No Unprivileged Container Support** | Platform | Linux VRF devices require `CAP_NET_ADMIN` and access to the host network namespace. LoopGen cannot operate inside standard unprivileged Docker containers. | LoopGen is not usable in standard container environments without elevated privileges. | Run with: `docker run --privileged --network host -it ubuntu:22.04 bash` then install LoopGen inside. |
| L-50 | **Kernel Version Dependency for Stable VRF Support** | Platform | Linux VRF support (`CONFIG_NET_VRF`) was introduced in kernel 4.3. Full feature stability including correct RTNETLINK enslavement ordering requires kernel 5.4+. | On kernels earlier than 5.4, interface VRF placement may not work correctly. | Use Ubuntu 20.04 or later which ships with kernel 5.4 or higher by default. |

---

## 9. Summary Table

| Category | Limitations | IDs | Critical | Minor |
|---|---|---|---|---|
| Protocol and Address Family | 7 | L-01 to L-07 | L-01, L-07 | L-02, L-03, L-04, L-05, L-06 |
| Interface and Kernel | 7 | L-08 to L-14 | L-11, L-12 | L-08, L-09, L-10, L-13, L-14 |
| FRR Integration | 9 | L-15 to L-23 | L-15, L-16, L-18 | L-17, L-19, L-20, L-21, L-22, L-23 |
| VRF Management | 5 | L-24 to L-28 | L-26, L-27 | L-24, L-25, L-28 |
| State and Persistence | 6 | L-29 to L-34 | L-30, L-31 | L-29, L-32, L-33, L-34 |
| Operational and Scalability | 7 | L-35 to L-41 | L-35 | L-36, L-37, L-38, L-39, L-40, L-41 |
| Security | 4 | L-42 to L-45 | L-42, L-43 | L-44, L-45 |
| Platform | 5 | L-46 to L-50 | L-46, L-48 | L-47, L-49, L-50 |
| **Total** | **50** | **L-01 to L-50** | **16** | **34** |

---

> **Legend**
>
> | Severity | Meaning |
> |---|---|
> | **Critical** | May prevent a primary LoopGen use case from working in common deployment scenarios |
> | **Minor** | Edge case, cosmetic issue, or has a straightforward workaround that does not disrupt primary functionality |

---

*For the latest version of this document and the LoopGen source code,
see the project repository.*