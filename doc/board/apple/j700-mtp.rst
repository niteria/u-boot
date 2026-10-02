.. SPDX-License-Identifier: GPL-2.0+

J700 built-in keyboard: experimental live handoff
================================================

This is the U-Boot half of a matched bootloader/kernel experiment for
MacBook Neo J700 / T8140. It is based on the public J700 branch at
``437a670138dfa351287390da318e2abe6314c634``. ``APPLE_MTP_KEYB`` remains
disabled by default. Enabling it requires an OS implementing the live-MTP
ABI described below; that companion Linux implementation is not part of
this U-Boot repository and is not yet merged upstream.

Build configuration
-------------------

With the usual U-Boot build dependencies and AArch64 cross compiler::

    make CROSS_COMPILE=aarch64-linux-gnu- apple_m1_defconfig
    scripts/kconfig/merge_config.sh -m .config board/apple/j700-mtp.config
    make CROSS_COMPILE=aarch64-linux-gnu- olddefconfig
    make CROSS_COMPILE=aarch64-linux-gnu- -j8

The fragment enables MTP and the optional 64 KiB output-console diagnostic
buffer, disables watchdog autostart, and uses::

    if usb start; then setenv stdin serial,usbkbd; fi; mtpkbd start

USB setup replaces stdin, so ``mtpkbd start`` must reattach the internal
keyboard afterward. The command binds/probes and attaches stdin without
synchronously waiting for READY: normal console polling must drive startup.
An early synchronous READY wait produced zero menu reports on hardware.
The FDT exporter waits up to two seconds only if the keyboard is not ready
when the descriptor is needed. A saved environment overrides build defaults;
check the actual ``preboot`` and ``stdin`` when reproducing the test.

Why retain the session?
-----------------------

The original QUIESCED/RUN-clear handoff leaves an OSLog allocation in DRAM.
Linux then observes a retained request larger than its helper SRAM and rejects
it. Reserving/mapping OSLog alone removed that rejection but firmware still
crashed. SLEEP, alternate OSLog buffers and stopping the endpoint did not
produce a working firmware-only handoff.

The matched solution retains the complete live session, with both AP and IOP
power states ON, and transfers ownership by clearing ASC RUN. Crashlog, syslog,
ioreport and OSLog buffers remain allocated. The receiver resumes the CPU and
checks liveness without restarting RTKit or changing its power states.

ABI 2 descriptor and receiver requirements
------------------------------------------

The helper node compatible with ``apple,t8140-rtk-helper-asc4`` has:

* ``apple,rtkit-handoff = <2>``;
* four ``memory-region`` references in crashlog, syslog, ioreport, OSLog order;
* matching ``memory-region-names``: ``rtkit-crashlog``, ``rtkit-syslog``,
  ``rtkit-ioreport``, ``rtkit-oslog``.

Each referenced ``/reserved-memory`` node has a 64-bit address/size ``reg``,
``no-map``, and ``iommu-addresses`` containing the helper phandle followed by
the same 64-bit address and size. The ranges are identity-mapped, 16 KiB
aligned, nonoverlapping, inside the helper DMA window and at most 1 MiB each.
Original ASC and SRAM register resources are preserved. The exporter rejects
foreign ownership and conflicting repeated descriptors.

The ``apple,dockchannel-hid`` node supplies ``apple,bootloader-comm-seq`` and
``apple,bootloader-ready-mask``. Complete retained INIT packets are replayed
with bounded FIFO draining and capacity/alignment checks after halting MTP.

The matching Linux receiver must:

1. Validate all descriptors, reserve/map every retained buffer and verify the
   actual IOMMU identity translations before resuming MTP. Resource containment
   checks must include ``IORESOURCE_MEM`` as well as range bounds.
2. Initialize an adopted RTKit session, including inherited crashlog/syslog
   handling, and resume ASC. Send management PING (type 3), wait for PONG
   (type 4); do not send INIT, restart endpoints or transition AP/IOP power.
3. Resume the HID communication sequence and run the normal Linux interface
   enable/READY handshake. The bootloader ready mask is diagnostic state,
   not permission to skip keyboard enablement. Skipping it registered a
   keyboard that produced no events during the intermediate experiment.
4. Start multitouch through its normal Linux firmware-loading path.

Older kernels do not implement this contract merely because they can be
selected in the EFI menu. Boot those with an MTP-disabled U-Boot image.

USB and diagnostics
-------------------

J700's board quiesce stops USB before generic OS-prepare removal shuts down
ATC providers. xHCI is stopped before its PHY, clocks and resets are released.
This prerequisite kept USB Ethernet available after the EFI menu.

For the tested diagnostic build, ``/chosen/nixos-neo,uboot-console`` contains
two big-endian 64-bit cells: output-ring address and size. The covering pages
are reserved in the FDT. It is optional bring-up instrumentation, not part of
the MTP ABI. Keyboard diagnostics count polls/reports/accepted reports; they
do not print key codes or values.

Validation
----------

Run the host-only extracted-C contracts from the repository root::

    python3 -B test/host/apple_mtp.py

They compile the actual removal, power, command and descriptor routines with
ASan/UBSan. Tests cover USB ordering/error propagation, bounded RTKit waits,
ON-state retention, FIFO drain/replay limits, stdin attachment and FDT ownership,
alignment, overlap, bounds and repeatability using the tree's real libfdt.
An injected early-poll regression must fail the command contract.

The corresponding downstream code was physically tested on J700 on
2026-10-02, with m1n1 ``0a33275c3c95`` and Linux 7.1.12 plus its matched
ABI-2 receiver:

* Built-in arrows/Enter selected a systemd-boot diagnostic specialisation:
  eight preboot reports, seven accepted.
* The same Linux boot recorded four key events, 451 trackpad ABS events and
  two mouse-button events; Ethernet/SSH remained available.
* A preceding normal boot after power-off/restart recorded four key events,
  1,135 trackpad ABS events and two mouse-button events.
* The integrated default generation subsequently booted with both input
  devices ready, no failed services and working Ethernet/SSH.

The review build with the matched configuration reproduces the tested
``u-boot-nodtb.bin`` SHA-256::

    f7f236b4cb1b9a81c62abc5e85cb7b97669cd9ee790db1f826321ad191eda8b9

Both MTP-enabled and default MTP-disabled AArch64 configurations cross-build.

These are matched-stack observations. Host fixtures and a cross-build do
not qualify another kernel, firmware version or machine. This review branch
retains the tested implementation and its optional diagnostics; it is not
a claim that standalone U-Boot changes fix unmodified Linux handover.
