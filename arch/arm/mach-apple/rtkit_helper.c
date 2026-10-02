// SPDX-License-Identifier: GPL-2.0+ OR MIT
/*
 * Copyright The Asahi Linux Contributors
 */

#include <dm.h>
#include <dm/device_compat.h>
#include <cpu_func.h>
#include <lmb.h>
#include <fdt_support.h>
#include <mailbox.h>
#include <mapmem.h>
#include <reset.h>

#include <asm/io.h>
#include <asm/arch/rtkit.h>
#include <linux/iopoll.h>
#include <linux/sizes.h>

/* ASC registers */
#define REG_CPU_CTRL		0x0044
#define  REG_CPU_CTRL_RUN	BIT(4)

#define APPLE_RTKIT_EP_OSLOG 8

struct rtkit_helper_priv {
	void *asc;		/* ASC registers */
	struct mbox_chan chan;
	struct apple_rtkit *rtk;
	bool sram_stolen;
	/*
	 * DMA window of the helper's DART ("apple,dma-range").  Buffers are
	 * handed to the firmware by physical address (the DART is in bypass),
	 * so they must lie inside the window the coprocessor can address.
	 */
	u64 dma_start, dma_end;
	u64 handoff[4][2];
	bool retain_buffers;
};

static bool rtkit_helper_uses_dram(struct udevice *dev)
{
	return IS_ENABLED(CONFIG_APPLE_MTP_KEYB) &&
	       device_is_compatible(dev, "apple,t8140-rtk-helper-asc4");
}

#include "rtkit_mtp_adopt.h"

static void rtkit_helper_read_dma_window(struct udevice *dev)
{
	struct rtkit_helper_priv *priv = dev_get_priv(dev);
	struct ofnode_phandle_args args;
	u64 range[2];

	if (dev_read_phandle_with_args(dev, "iommus", "#iommu-cells", 0, 0,
				       &args))
		return;
	if (ofnode_read_u64_array(args.node, "apple,dma-range", range, 2) ||
	    !range[1])
		return;

	priv->dma_start = range[0];
	priv->dma_end = range[0] + range[1];
}

static void *rtkit_helper_alloc(struct rtkit_helper_priv *priv, size_t size)
{
	phys_addr_t addr;

	if (!priv->dma_end)
		return memalign(SZ_16K, ALIGN(size, SZ_16K));

	/* U-Boot's heap sits at the top of RAM, above a 4 GiB window. */
	addr = priv->dma_end - 1;
	if (lmb_alloc_mem(LMB_MEM_ALLOC_MAX, SZ_16K, &addr,
			  ALIGN(size, SZ_16K), LMB_NOOVERWRITE))
		return NULL;
	if (addr < priv->dma_start) {
		lmb_free(addr, ALIGN(size, SZ_16K), LMB_NOOVERWRITE);
		return NULL;
	}

	return map_sysmem(addr, size);
}

static int shmem_setup(void *cookie, struct apple_rtkit_buffer *buf) {
	struct udevice *dev = cookie;
	struct rtkit_helper_priv *priv = dev_get_priv(dev);
	fdt_size_t sram_size;
	fdt_addr_t sram;

	if (rtkit_helper_uses_dram(dev))
		printf("MTP RTKit: endpoint %d buffer %#llx size %#zx mapped %d\n",
		       buf->endpoint, buf->dva, buf->size, buf->is_mapped);

	if (!buf->is_mapped) {
		/* Older MTP firmware retains OSLog in SRAM across OS handoff. */
		if (buf->endpoint == APPLE_RTKIT_EP_OSLOG &&
		    !rtkit_helper_uses_dram(dev)) {
			if (priv->sram_stolen)
				return -EBUSY;

			sram = dev_read_addr_size_name(dev, "sram", &sram_size);
			if (sram == FDT_ADDR_T_NONE || !buf->size ||
			    buf->size > sram_size || sram_size > (u64)-1 - sram)
				return -EFAULT;

			buf->dva = ALIGN_DOWN(sram + sram_size - buf->size, SZ_16K);
			if (buf->dva < sram)
				return -EFAULT;

			priv->sram_stolen = true;
			return 0;
		}

		/* DRAM OSLog is only for the unsafe, opt-in T8140 MTP path. */
		buf->buffer = rtkit_helper_alloc(priv, buf->size);
		if (!buf->buffer)
			return -ENOMEM;

		memset(buf->buffer, 0, buf->size);
		flush_dcache_range((ulong)buf->buffer,
				   (ulong)buf->buffer + ALIGN(buf->size, SZ_16K));
		buf->dva = map_to_sysmem(buf->buffer);
		rtkit_helper_record_buffer(dev, buf);
		return 0;
	}

	/* Linux accepts firmware-owned mappings only inside the helper SRAM. */
	sram = dev_read_addr_size_name(dev, "sram", &sram_size);
	if (sram == FDT_ADDR_T_NONE || buf->dva < sram ||
	    buf->size > sram_size || buf->dva - sram > sram_size - buf->size) {
		dev_err(dev, "RTKit mapped buffer outside SRAM: %#llx+%#zx\n",
			buf->dva, buf->size);
		return -EFAULT;
	}
	return 0;
}

static void shmem_destroy(void *cookie, struct apple_rtkit_buffer *buf) {
	struct udevice *dev = cookie;
	struct rtkit_helper_priv *priv = dev_get_priv(dev);

	if (priv->retain_buffers)
		return;

	/* Firmware keeps OSLog even after QUIESCED and ASC RUN is cleared. */
	if (!buf->buffer || buf->endpoint == APPLE_RTKIT_EP_OSLOG)
		return;

	if (priv->dma_end)
		lmb_free(map_to_sysmem(buf->buffer), ALIGN(buf->size, SZ_16K),
			 LMB_NOOVERWRITE);
	else
		free(buf->buffer);
}

/* Keep T8140 MTP and its DART untouched in the default build. */
bool apple_rtkit_helper_allowed(ofnode node)
{
	return !ofnode_device_is_compatible(node, "apple,t8140-rtk-helper-asc4") ||
	       IS_ENABLED(CONFIG_APPLE_MTP_KEYB);
}

static int rtkit_helper_bind(struct udevice *dev)
{
	return apple_rtkit_helper_allowed(dev_ofnode(dev)) ? 0 : -ENODEV;
}

static int rtkit_helper_probe(struct udevice *dev)
{
	struct rtkit_helper_priv *priv = dev_get_priv(dev);
	u32 ctrl;
	int ret;

	priv->asc = dev_read_addr_ptr(dev);
	if (!priv->asc)
		return -EINVAL;

	ret = mbox_get_by_index(dev, 0, &priv->chan);
	if (ret < 0)
		return ret;

	if (rtkit_helper_uses_dram(dev))
		rtkit_helper_read_dma_window(dev);

	ctrl = readl(priv->asc + REG_CPU_CTRL);
	writel(ctrl | REG_CPU_CTRL_RUN, priv->asc + REG_CPU_CTRL);

	priv->rtk = apple_rtkit_init(&priv->chan, dev, shmem_setup, shmem_destroy);
	if (!priv->rtk)
		return -ENOMEM;

	ret = apple_rtkit_boot(priv->rtk);
	if (ret < 0) {
		printf("%s: Helper apple_rtkit_boot returned: %d\n", __func__, ret);
		goto err_stop;
	}

	ret = apple_rtkit_set_ap_power(priv->rtk, APPLE_RTKIT_PWR_STATE_ON);
	if (ret < 0) {
		printf("%s: Helper apple_rtkit_set_ap_power returned: %d\n", __func__, ret);
		goto err_stop;
	}

	return 0;

err_stop:
	/* A failed probe will not get an OS-prepare remove callback. */
	writel(ctrl & ~REG_CPU_CTRL_RUN, priv->asc + REG_CPU_CTRL);
	readl(priv->asc + REG_CPU_CTRL);
	apple_rtkit_free(priv->rtk);
	priv->rtk = NULL;
	return ret;
}

static int rtkit_helper_remove(struct udevice *dev)
{
	struct rtkit_helper_priv *priv = dev_get_priv(dev);
	u32 ctrl;
	int ret;

	if (!priv->rtk)
		return 0;
	if (rtkit_helper_uses_dram(dev)) {
		priv->retain_buffers = true;
		ret = 0;
		printf("MTP RTKit: preserving AP/IOP ON; halting CPU for transfer\n");
		goto halt;
	}

	ret = apple_rtkit_shutdown(priv->rtk, APPLE_RTKIT_PWR_STATE_QUIESCED);
	printf("MTP RTKit: shutdown state %#x result %d CPU %#x\n",
	       APPLE_RTKIT_PWR_STATE_QUIESCED, ret, readl(priv->asc + REG_CPU_CTRL));

halt:
	ctrl = readl(priv->asc + REG_CPU_CTRL);
	writel(ctrl & ~REG_CPU_CTRL_RUN, priv->asc + REG_CPU_CTRL);
	readl(priv->asc + REG_CPU_CTRL);
	if (ret)
		return ret;

	apple_rtkit_free(priv->rtk);
	priv->rtk = NULL;

	return 0;
}

int apple_rtkit_helper_poll(struct udevice *dev, ulong timeout)
{
	struct rtkit_helper_priv *priv = dev_get_priv(dev);

	return apple_rtkit_poll(priv->rtk, timeout);
}

static const struct udevice_id rtkit_helper_ids[] = {
	{ .compatible = "apple,rtk-helper-asc4" },
	{ /* sentinel */ }
};

U_BOOT_DRIVER(rtkit_helper) = {
	.name = "rtkit_helper",
	.id = UCLASS_MISC,
	.of_match = rtkit_helper_ids,
	.bind = rtkit_helper_bind,
	.priv_auto = sizeof(struct rtkit_helper_priv),
	.probe = rtkit_helper_probe,
	.remove = rtkit_helper_remove,
	.flags = DM_FLAG_OS_PREPARE,
};
