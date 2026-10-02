#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0+
"""Compile the actual Apple handoff routines against host contract fixtures."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile


def function(path, name):
    text = path.read_text()
    match = re.search(r"(?m)^(?:static )?(?:int|void) " + re.escape(name) + r"\([^)]*\)\s*\{", text)
    if match is None:
        raise ValueError(f"Cannot find {name} in {path}")
    start = text.index("{", match.start())
    depth, end = 1, start + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[match.start():end] + "\n"


COMMON = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#ifdef NDEBUG
#error Assertions must be enabled
#endif
typedef uint32_t u32;
typedef uint8_t u8;
typedef unsigned long long u64;
typedef unsigned long ulong;
#define BIT(b) (1U << (b))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define IS_ALIGNED(n, a) (((n) & ((a) - 1)) == 0)
#define ALIGN(n, a) (((n) + (a) - 1) & ~((a) - 1))
'''


def usb_source(root):
    return COMMON + r'''
struct udevice { int unused; };
struct xhci_dwc3_plat { int phys, clks, resets; };
static struct xhci_dwc3_plat plat;
static struct udevice dev;
static bool phy_on, clocks_on, reset_released, running, fail_stop, j700;
static int stops;
static void *dev_get_plat(struct udevice *d) { (void)d; return &plat; }
static int xhci_deregister(struct udevice *d) {
    (void)d;
    assert(phy_on && clocks_on && reset_released);
    if (fail_stop) return -EIO;
    running = false;
    return 0;
}
static void dwc3_shutdown_phy(struct udevice *d, int *p) { (void)d; (void)p; assert(!running); phy_on = false; }
static void clk_release_bulk(int *p) { (void)p; assert(!running); clocks_on = false; }
static void reset_release_bulk(int *p) { (void)p; assert(!running); reset_released = false; }
''' + function(root / 'drivers/usb/host/xhci-dwc3.c', 'xhci_dwc3_remove') + r'''
#define CONFIG_USB 1
#define IS_ENABLED(x) (x)
static bool of_machine_is_compatible(const char *s) { return j700 && !strcmp(s, "apple,t8140"); }
static int usb_stop(void) { stops++; return running ? xhci_dwc3_remove(&dev) : 0; }
''' + function(root / 'arch/arm/mach-apple/board.c', 'board_quiesce_devices') + r'''
int main(void) {
    phy_on = clocks_on = reset_released = running = true;
    board_quiesce_devices(); assert(!stops && running);
    j700 = true; fail_stop = true;
    assert(xhci_dwc3_remove(&dev) == -EIO && running && phy_on && clocks_on && reset_released);
    fail_stop = false; board_quiesce_devices();
    assert(stops == 1 && !running && !phy_on && !clocks_on && !reset_released);
    board_quiesce_devices(); assert(stops == 2);
    return 0;
}
'''


def mtp_source(root):
    helper = root / 'arch/arm/mach-apple/rtkit_helper.c'
    rtkit = root / 'arch/arm/mach-apple/rtkit.c'
    keyboard = root / 'drivers/input/apple_mtp_kbd.c'
    source = COMMON + r'''
#define APPLE_RTKIT_PWR_STATE_SLEEP 1
#define APPLE_RTKIT_PWR_STATE_QUIESCED 0x10
#define APPLE_RTKIT_PWR_STATE_ON 0x20
#define APPLE_RTKIT_MGMT_TYPE (0xffULL << 52)
#define APPLE_RTKIT_MGMT_PWR_STATE 0xffffULL
#define APPLE_RTKIT_MGMT_SET_IOP_PWR_STATE 6
#define APPLE_RTKIT_MGMT_SET_AP_PWR_STATE 11
#define APPLE_RTKIT_EP_MGMT 0
#define FIELD_PREP(mask, v) (((u64)(v) << __builtin_ctzll(mask)) & (mask))
#define REG_CPU_CTRL 0x44
#define REG_CPU_CTRL_RUN BIT(4)
#define DATA_TX8 4
#define DATA_TX32 0x10
#define DATA_TX_FREE 0x14
#define DATA_RX8 0x1c
#define DATA_RX_COUNT 0x2c
#define DCHID_IRQ_MASK 0
#define DCHID_IRQ_FLAG 4
#define DM_REMOVE_NORMAL 1
#define dev_err(dev, ...) do { (void)(dev); } while (0)
struct apple_mbox_msg { u64 msg0, msg1; };
struct apple_rtkit { void *chan; int ap_pwr, iop_pwr; };
struct udevice { void *priv; bool t8140; };
struct rtkit_helper_priv { void *asc; struct apple_rtkit *rtk; bool retain_buffers; };
struct dchid_hdr { u8 bytes[8]; };
struct input_config { int unused; };
struct keyboard_priv { struct input_config input; };
struct apple_mtp_kbd_priv {
    struct udevice *helper;
    void *local, *rmt, *irq_base;
    u32 rx_irq_mask, fifo_size, init_size, poll_count, report_count, accepted_count;
    u8 *init_data, *pkt;
    bool announce, ready;
};
static struct apple_rtkit rtk;
static struct rtkit_helper_priv hp;
static struct apple_mtp_kbd_priv kp;
static struct keyboard_priv uc;
static struct udevice hd, kd;
static u32 asc[32], local[16], remote[16], irq[2];
static ulong ticks;
static int pending_type, pending_state, suppress_ack, send_error, poll_error;
static int sends, polls, releases, replay_bytes, key_polls, fifo_reads;
static bool stalled;
static void *dev_get_priv(struct udevice *d) { return d->priv; }
static void *dev_get_uclass_priv(struct udevice *d) { (void)d; return &uc; }
static bool device_is_compatible(struct udevice *d, const char *s) {
    assert(!strcmp(s, "apple,t8140-rtk-helper-asc4")); return d->t8140;
}
static bool rtkit_helper_uses_dram(struct udevice *d) { return d->t8140; }
static ulong get_timer(ulong base) { return ++ticks - base; }
static int mbox_send(void *chan, struct apple_mbox_msg *msg) {
    (void)chan; sends++;
    if (send_error) return send_error;
    assert(msg->msg1 == 0);
    pending_type = (msg->msg0 >> 52) & 0xff;
    pending_state = msg->msg0 & 0xffff;
    assert(pending_type == 6 || pending_type == 11);
    if (pending_type == 6) assert(rtk.ap_pwr == 0x10);
    return 0;
}
static int apple_rtkit_poll(struct apple_rtkit *r, ulong timeout) {
    assert(timeout > 0 && timeout <= 1000000); ticks += 50; polls++;
    if (poll_error) return poll_error;
    if (suppress_ack == pending_type) return 0;
    if (pending_type == 6) r->iop_pwr = pending_state;
    else r->ap_pwr = pending_state;
    return 0;
}
static u32 readl(void *p) {
    if (p == (void *)remote + DATA_TX_FREE) return kp.fifo_size - local[DATA_RX_COUNT / 4];
    if (p == (void *)local + DATA_RX8) {
        assert(!(asc[REG_CPU_CTRL / 4] & REG_CPU_CTRL_RUN));
        assert(local[DATA_RX_COUNT / 4]); local[DATA_RX_COUNT / 4]--; fifo_reads++;
    }
    return *(u32 *)p;
}
static void writel(u32 v, void *p) {
    if (p == (void *)remote + DATA_TX32) {
        assert(!(asc[REG_CPU_CTRL / 4] & REG_CPU_CTRL_RUN));
        replay_bytes += 4; local[DATA_RX_COUNT / 4] += 4;
    }
    *(u32 *)p = v;
}
static u32 get_unaligned_le32(const void *p) { u32 v; memcpy(&v, p, 4); return v; }
static void apple_rtkit_free(struct apple_rtkit *r) {
    assert(r == &rtk && !(asc[REG_CPU_CTRL / 4] & REG_CPU_CTRL_RUN));
    if (hp.retain_buffers) assert(r->ap_pwr == 0x20 && r->iop_pwr == 0x20);
    else assert(r->ap_pwr == 0x10 && r->iop_pwr == 0x10);
    releases++;
}
static int apple_mtp_kbd_check(struct input_config *input) {
    (void)input; key_polls++; if (!stalled) local[DATA_RX_COUNT / 4] = 0; return 0;
}
static int dockchannel_read(struct udevice *dev, void *buf, size_t n) {
    (void)dev; assert(!buf); int count = n;
    while (n--) readl((void *)local + DATA_RX8);
    return count;
}
'''
    for name in ('rtkit_wait_power', 'apple_rtkit_set_ap_power', 'apple_rtkit_shutdown'):
        source += function(rtkit, name)
    source += function(helper, 'rtkit_helper_remove')
    source += r'''
static int device_remove(struct udevice *dev, int flags) {
    assert(flags == DM_REMOVE_NORMAL); return rtkit_helper_remove(dev);
}
'''
    source += function(root / 'drivers/input/apple_mtp_handoff.h', 'apple_mtp_kbd_handoff')
    source += function(keyboard, 'apple_mtp_kbd_remove')
    source += r'''
#undef stdin
#define stdin 0
static char inputs[128];
static int env_error, mux_error, probe_error, probe_calls;
static const char *env_get(const char *s) { assert(!strcmp(s, "stdin")); return inputs; }
static int env_set(const char *s, const char *v) {
    assert(!strcmp(s, "stdin")); if (env_error) return env_error;
    assert(strlen(v) < sizeof(inputs)); strcpy(inputs, v); return 0;
}
static int iomux_doenv(int fd, const char *v) { assert(!fd && !strcmp(v, inputs)); return mux_error; }
typedef int ofnode;
struct cmd_tbl { int unused; };
#define CMD_RET_USAGE -1
#define CMD_RET_FAILURE 1
#define CMD_RET_SUCCESS 0
static ofnode ofnode_null(void) { return 0; }
static ofnode ofnode_by_compatible(ofnode n, const char *s) {
    assert(!n && !strcmp(s, "apple,dockchannel-hid")); return 1;
}
static bool ofnode_valid(ofnode n) { return n != 0; }
static int ofnode_read_u32(ofnode n, const char *s, u32 *v) {
    assert(n == 1 && !strcmp(s, "apple,helper-cpu")); *v = 2; return 0;
}
static ofnode ofnode_get_by_phandle(u32 p) { assert(p == 2); return 2; }
static int mtpkbd_bind_node(ofnode n, struct udevice **d) { *d = n == 1 ? &kd : &hd; return 0; }
static int device_probe(struct udevice *d) { assert(d == &kd); probe_calls++; return probe_error; }
static void iomux_printdevs(int fd) { assert(!fd); }
'''
    source += function(keyboard, 'mtpkbd_add_stdin') + function(keyboard, 'do_mtpkbd')
    source += r'''
static void reset(bool t8140) {
    memset(asc, 0, sizeof(asc)); memset(local, 0, sizeof(local));
    memset(remote, 0, sizeof(remote)); memset(irq, 0, sizeof(irq));
    rtk = (struct apple_rtkit){.ap_pwr = 0x20, .iop_pwr = 0x20};
    hp = (struct rtkit_helper_priv){.asc = asc, .rtk = &rtk};
    hd = (struct udevice){.priv = &hp, .t8140 = t8140};
    kp = (struct apple_mtp_kbd_priv){.helper = &hd, .local = local, .rmt = remote,
        .irq_base = irq, .rx_irq_mask = 2, .fifo_size = 64, .announce = true};
    kd = (struct udevice){.priv = &kp}; asc[REG_CPU_CTRL / 4] = REG_CPU_CTRL_RUN | 1;
    ticks = 0; sends = polls = releases = replay_bytes = key_polls = fifo_reads = 0;
    suppress_ack = send_error = poll_error = 0; stalled = false;
}
int main(void) {
    reset(true);
    assert(rtkit_helper_remove(&hd) == 0 && !sends && hp.retain_buffers);
    assert(!hp.rtk && releases == 1 && asc[REG_CPU_CTRL / 4] == 1);
    assert(rtkit_helper_remove(&hd) == 0 && releases == 1);
    reset(false); assert(rtkit_helper_remove(&hd) == 0 && sends == 2);
    for (int type = 6; type <= 11; type += 5) {
        reset(false); suppress_ack = type;
        assert(rtkit_helper_remove(&hd) == -ETIMEDOUT && !releases && hp.rtk);
        assert(ticks < 1100 && polls < 25 && !(asc[REG_CPU_CTRL / 4] & REG_CPU_CTRL_RUN));
    }
    reset(false); send_error = -EIO; assert(rtkit_helper_remove(&hd) == -EIO && !releases);
    reset(false); poll_error = -EPROTO; assert(apple_mtp_kbd_remove(&kd) == -EPROTO && !releases);
    for (unsigned count = 0; count < 8; count++) {
        reset(true); local[DATA_RX_COUNT / 4] = count;
        kp.init_size = 8; kp.init_data = calloc(8, 1); kp.pkt = malloc(32);
        assert(apple_mtp_kbd_remove(&kd) == 0 && replay_bytes == 8 && fifo_reads == (int)count);
        assert(!kp.helper && hp.retain_buffers && !irq[0] && irq[1] == 2);
    }
    reset(true); local[DATA_RX_COUNT / 4] = 65;
    assert(apple_mtp_kbd_remove(&kd) == -EOVERFLOW && !replay_bytes);
    reset(true); local[DATA_RX_COUNT / 4] = 8; stalled = true;
    assert(apple_mtp_kbd_remove(&kd) == -EOVERFLOW && !replay_bytes);
    reset(true); kp.init_size = 65;
    assert(apple_mtp_kbd_remove(&kd) == -ENOSPC && !replay_bytes);
    reset(true); kp.init_size = 68;
    assert(apple_mtp_kbd_remove(&kd) == -ENOSPC && !replay_bytes);
    strcpy(inputs, "serial,usbkbd");
    assert(mtpkbd_add_stdin() == 0 && !strcmp(inputs, "serial,usbkbd,mtpkbd"));
    assert(mtpkbd_add_stdin() == 0 && !strcmp(inputs, "serial,usbkbd,mtpkbd"));
    mux_error = -EIO; assert(mtpkbd_add_stdin() == -EIO); mux_error = 0;
    strcpy(inputs, "serial"); env_error = -ENOMEM;
    assert(mtpkbd_add_stdin() == -ENOMEM && !strcmp(inputs, "serial")); env_error = 0;
    reset(true); char *argv[] = {"mtpkbd", "start"};
    assert(do_mtpkbd(NULL, 0, 2, argv) == 0 && probe_calls == 1 && !key_polls && !kp.ready);
    assert(!strcmp(inputs, "serial,mtpkbd"));
    assert(do_mtpkbd(NULL, 0, 2, argv) == 0 && !key_polls);
    strcpy(inputs, "serial"); probe_error = -EIO;
    assert(do_mtpkbd(NULL, 0, 2, argv) == CMD_RET_FAILURE && !strcmp(inputs, "serial"));
    return 0;
}
'''
    return source


def fdt_source(root):
    return COMMON + r'''
#include <libfdt.h>
#define SZ_16K 0x4000ULL
#define SZ_1M 0x100000ULL
struct apple_rtkit_buffer { int endpoint; u64 dva; size_t size; };
struct rtkit_helper_priv { u64 dma_start, dma_end, handoff[4][2]; };
struct apple_mtp_kbd_priv { bool ready; int kbd_iface; u8 tx_seq; };
struct udevice { void *priv; bool t8140; };
struct input_config { int unused; };
struct keyboard_priv { struct input_config input; };
static struct keyboard_priv uc;
static int polls, poll_error;
static void *dev_get_priv(struct udevice *d) { return d->priv; }
static void *dev_get_uclass_priv(struct udevice *d) { (void)d; return &uc; }
static bool rtkit_helper_uses_dram(struct udevice *d) { return d->t8140; }
static ulong get_timer(ulong base) { static ulong ticks; ticks += 500; return ticks - base; }
static int apple_mtp_kbd_check(struct input_config *p) { (void)p; polls++; return poll_error; }
''' + (root / 'arch/arm/mach-apple/rtkit_mtp_adopt.h').read_text() + function(
        root / 'drivers/input/apple_mtp_handoff.h', 'apple_mtp_kbd_fixup') + r'''
static unsigned char blob[32768], pristine[32768];
static struct rtkit_helper_priv hp;
static struct udevice helper = {.priv = &hp, .t8140 = true};
static struct apple_mtp_kbd_priv kp = {.ready = true, .kbd_iface = 3, .tx_seq = 1};
static struct udevice keyboard = {.priv = &kp, .t8140 = true};
static u64 load64(const void *p) { fdt64_t v; memcpy(&v, p, 8); return fdt64_to_cpu(v); }
static void fixture(void) {
    int node;
    hp = (struct rtkit_helper_priv){.dma_start = 0x10000004000ULL, .dma_end = 0x100ffff4000ULL};
    for (int i = 0; i < 4; i++) {
        struct apple_rtkit_buffer b = {.endpoint = 1 << i, .dva = 0x10000008000ULL + i * 0x8000, .size = i == 3 ? 0x6000 : 0x4000};
        rtkit_helper_record_buffer(&helper, &b);
    }
    assert(!fdt_create_empty_tree(blob, sizeof(blob)));
    assert(!fdt_setprop_u32(blob, 0, "#address-cells", 2));
    assert(!fdt_setprop_u32(blob, 0, "#size-cells", 2));
    node = fdt_add_subnode(blob, 0, "reserved-memory"); assert(node >= 0);
    assert(!fdt_setprop_u32(blob, node, "#address-cells", 2));
    assert(!fdt_setprop_u32(blob, node, "#size-cells", 2));
    assert(!fdt_setprop_empty(blob, node, "ranges"));
    node = fdt_add_subnode(blob, 0, "input"); assert(node >= 0);
    assert(!fdt_setprop_string(blob, node, "compatible", "apple,dockchannel-hid"));
    assert(!fdt_setprop_u32(blob, node, "phandle", 46));
    node = fdt_add_subnode(blob, 0, "mtp"); assert(node >= 0);
    assert(!fdt_setprop_string(blob, node, "compatible", "apple,t8140-rtk-helper-asc4"));
    assert(!fdt_setprop_u32(blob, node, "phandle", 45));
    fdt64_t reg[] = {cpu_to_fdt64(0x314600000ULL), cpu_to_fdt64(0x4000),
                    cpu_to_fdt64(0x314050000ULL), cpu_to_fdt64(0x4000)};
    assert(!fdt_setprop(blob, node, "reg", reg, sizeof(reg)));
    memcpy(pristine, blob, sizeof(blob));
}
int main(void) {
    int node, len;
    fdt32_t phandles[4];
    const fdt32_t *map;
    const fdt64_t *reg;
    fixture(); assert(!apple_rtkit_helper_fixup(&helper, blob));
    node = fdt_node_offset_by_phandle(blob, 45);
    map = fdt_getprop(blob, node, "apple,rtkit-handoff", &len);
    assert(map && len == 4 && fdt32_to_cpu(*map) == 2);
    map = fdt_getprop(blob, node, "memory-region", &len);
    assert(map && len == 16); memcpy(phandles, map, 16);
    reg = fdt_getprop(blob, node, "reg", &len);
    assert(reg && len == 32 && load64(reg) == 0x314600000ULL && load64(reg + 2) == 0x314050000ULL);
    for (int i = 0; i < 4; i++) {
        assert(!strcmp(fdt_stringlist_get(blob, node, "memory-region-names", i, NULL), rtkit_handoff_names[i]));
        int reserved = fdt_node_offset_by_phandle(blob, fdt32_to_cpu(phandles[i])); assert(reserved >= 0);
        assert(fdt_parent_offset(blob, reserved) == fdt_path_offset(blob, "/reserved-memory"));
        reg = fdt_getprop(blob, reserved, "reg", &len);
        assert(reg && len == 16 && load64(reg) == hp.handoff[i][0] && load64(reg + 1) == hp.handoff[i][1]);
        map = fdt_getprop(blob, reserved, "iommu-addresses", &len);
        assert(map && len == 20 && fdt32_to_cpu(*map) == 45 && !memcmp(map + 1, reg, 16));
        assert(fdt_getprop(blob, reserved, "no-map", &len) && len == 0);
    }
    assert(!apple_mtp_kbd_fixup(&keyboard, blob) && !polls);
    node = fdt_node_offset_by_phandle(blob, 46);
    map = fdt_getprop(blob, node, "apple,bootloader-comm-seq", &len); assert(map && fdt32_to_cpu(*map) == 1);
    map = fdt_getprop(blob, node, "apple,bootloader-ready-mask", &len); assert(map && fdt32_to_cpu(*map) == 8);
    memcpy(pristine, blob, sizeof(blob));
    assert(!apple_rtkit_helper_fixup(&helper, blob) && !memcmp(pristine, blob, sizeof(blob)));
    fixture(); hp.handoff[3][1] = 0;
    assert(apple_rtkit_helper_fixup(&helper, blob) == -ERANGE && !memcmp(pristine, blob, sizeof(blob)));
    fixture(); hp.handoff[3][0]++;
    assert(apple_rtkit_helper_fixup(&helper, blob) == -ERANGE);
    fixture(); hp.handoff[3][0] = hp.handoff[0][0];
    assert(apple_rtkit_helper_fixup(&helper, blob) == -EINVAL);
    fixture(); hp.handoff[3][0] = UINT64_MAX - 0x3fff;
    assert(apple_rtkit_helper_fixup(&helper, blob) == -ERANGE);
    fixture(); helper.t8140 = false;
    assert(!apple_rtkit_helper_fixup(&helper, blob) && !memcmp(pristine, blob, sizeof(blob))); helper.t8140 = true;
    fixture(); node = fdt_node_offset_by_phandle(blob, 45);
    assert(!fdt_setprop_u32(blob, node, "memory-region", 123));
    assert(apple_rtkit_helper_fixup(&helper, blob) == -EEXIST);
    fixture(); kp.ready = false;
    assert(apple_mtp_kbd_fixup(&keyboard, blob) == -EAGAIN && polls > 0);
    poll_error = -EIO; assert(apple_mtp_kbd_fixup(&keyboard, blob) == -EIO); poll_error = 0;
    kp.ready = true; kp.kbd_iface = 16;
    assert(apple_mtp_kbd_fixup(&keyboard, blob) == -EAGAIN);
    return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--cc', default='cc')
    args = parser.parse_args()
    root = args.source.resolve()
    cases = {'usb': usb_source(root), 'mtp': mtp_source(root), 'fdt': fdt_source(root)}
    # A regression that synchronously polls during mtpkbd start must fail.
    mutant = cases['mtp'].replace('ret = mtpkbd_add_stdin();', 'apple_mtp_kbd_check(&uc.input); ret = mtpkbd_add_stdin();')
    assert mutant != cases['mtp']
    cases['early-poll-regression'] = mutant
    with tempfile.TemporaryDirectory(prefix='apple-mtp-host-') as directory:
        output = Path(directory)
        for name, source in cases.items():
            path = output / (name + '.c')
            path.write_text(source)
            command = [args.cc, '-std=gnu11', '-D_POSIX_C_SOURCE=200809L', '-Wall', '-Wextra',
                       '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-sign-compare', '-Werror',
                       '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer',
                       '-fno-pie', '-no-pie', str(path), '-o', str(output / name)]
            if name == 'fdt':
                lib = root / 'scripts/dtc/libfdt'
                command += ['-I', str(lib), *[str(lib / (f + '.c')) for f in (
                    'fdt', 'fdt_ro', 'fdt_rw', 'fdt_sw', 'fdt_wip', 'fdt_strerror', 'fdt_empty_tree', 'fdt_addresses')]]
            subprocess.run(command, check=True)
            result = subprocess.run([str(output / name)], capture_output=True, text=True, timeout=10)
            if name == 'early-poll-regression':
                assert result.returncode != 0 and 'Assertion' in result.stderr and 'key_polls' in result.stderr, result
            elif result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            print('PASS:', name)


if __name__ == '__main__':
    main()
