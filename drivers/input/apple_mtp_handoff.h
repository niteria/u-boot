/* SPDX-License-Identifier: GPL-2.0+ OR MIT */
/* Included after the DockChannel parser in apple_mtp_kbd.c. */
int apple_mtp_kbd_fixup(struct udevice *dev, void *blob)
{
	struct apple_mtp_kbd_priv *priv = dev_get_priv(dev);
	struct keyboard_priv *uc_priv = dev_get_uclass_priv(dev);
	ulong start = get_timer(0);
	int node, ret;

	/* Normal console polling starts the keyboard; wait here only if needed. */
	while (!priv->ready && get_timer(start) < 2000) {
		ret = apple_mtp_kbd_check(&uc_priv->input);
		if (ret < 0)
			return ret;
	}
	if (!priv->ready || priv->kbd_iface < 0 || priv->kbd_iface >= 16)
		return -EAGAIN;
	node = fdt_node_offset_by_compatible(blob, -1, "apple,dockchannel-hid");
	if (node < 0)
		return node;
	ret = fdt_setprop_u32(blob, node, "apple,bootloader-comm-seq", priv->tx_seq);
	if (ret)
		return ret;
	return fdt_setprop_u32(blob, node, "apple,bootloader-ready-mask", BIT(priv->kbd_iface));
}

static int apple_mtp_kbd_handoff(struct udevice *dev)
{
	struct apple_mtp_kbd_priv *priv = dev_get_priv(dev);
	struct keyboard_priv *uc_priv = dev_get_uclass_priv(dev);
	u32 pending, budget = priv->fifo_size;
	int i, ret;

	printf("mtpkbd: polls %u, keyboard reports %u, accepted %u\n",
	       priv->poll_count, priv->report_count, priv->accepted_count);
	ret = device_remove(priv->helper, DM_REMOVE_NORMAL);
	if (ret)
		return ret;
	priv->helper = NULL;
	writel(0, priv->irq_base + DCHID_IRQ_MASK);
	writel(priv->rx_irq_mask, priv->irq_base + DCHID_IRQ_FLAG);
	while ((pending = readl(priv->local + DATA_RX_COUNT))) {
		if (pending > priv->fifo_size || !budget--)
			return -EOVERFLOW;
		if (pending < sizeof(struct dchid_hdr)) {
			ret = dockchannel_read(dev, NULL, pending);
			if (ret < 0)
				return ret;
			break;
		}
		ret = apple_mtp_kbd_check(&uc_priv->input);
		if (ret < 0) {
			dev_err(dev, "Discarded partial final DockChannel packet (%d)\n", ret);
			break;
		}
	}
	if (!IS_ALIGNED(priv->init_size, sizeof(u32)) ||
	    priv->init_size > readl(priv->rmt + DATA_TX_FREE))
		return -ENOSPC;
	for (i = 0; i < priv->init_size; i += sizeof(u32))
		writel(get_unaligned_le32(priv->init_data + i), priv->rmt + DATA_TX32);
	printf("mtpkbd: replayed %u INIT bytes for live-session Linux\n", priv->init_size);
	free(priv->pkt);
	free(priv->init_data);
	return 0;
}
