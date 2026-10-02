/* SPDX-License-Identifier: GPL-2.0+ OR MIT */
/* Experimental live-session ABI, included by rtkit_helper.c. */
static const char * const rtkit_handoff_names[] = {
	"rtkit-crashlog", "rtkit-syslog", "rtkit-ioreport", "rtkit-oslog",
};

static const char rtkit_handoff_name_list[] =
	"rtkit-crashlog\0rtkit-syslog\0rtkit-ioreport\0rtkit-oslog";

static void rtkit_helper_record_buffer(struct udevice *dev,
				       const struct apple_rtkit_buffer *buf)
{
	struct rtkit_helper_priv *priv = dev_get_priv(dev);
	int index;

	if (!rtkit_helper_uses_dram(dev))
		return;
	switch (buf->endpoint) {
	case 1:
		index = 0;
		break;
	case 2:
		index = 1;
		break;
	case 4:
		index = 2;
		break;
	case 8:
		index = 3;
		break;
	default:
		return;
	}
	priv->handoff[index][0] = buf->dva;
	priv->handoff[index][1] = ALIGN(buf->size, SZ_16K);
}

int apple_rtkit_helper_fixup(struct udevice *dev, void *blob)
{
	struct rtkit_helper_priv *priv = dev_get_priv(dev);
	fdt32_t phandles[4], old_phandles[4];
	const void *prop;
	u32 helper_phandle;
	int helper, reserved, node, len, ret, i, j;
	bool repeat;

	if (!rtkit_helper_uses_dram(dev))
		return 0;
	for (i = 0; i < 4; i++) {
		u64 addr = priv->handoff[i][0], size = priv->handoff[i][1];

		if (!size || size > SZ_1M || !IS_ALIGNED(addr, SZ_16K) ||
		    !IS_ALIGNED(size, SZ_16K) || addr < priv->dma_start ||
		    addr >= priv->dma_end || size > priv->dma_end - addr)
			return -ERANGE;
		for (j = 0; j < i; j++) {
			if (addr < priv->handoff[j][0] + priv->handoff[j][1] &&
			    priv->handoff[j][0] < addr + size)
				return -EINVAL;
		}
	}
	helper = fdt_node_offset_by_compatible(blob, -1,
					       "apple,t8140-rtk-helper-asc4");
	if (helper < 0)
		return helper;
	helper_phandle = fdt_get_phandle(blob, helper);
	if (!helper_phandle)
		return -EINVAL;
	prop = fdt_getprop(blob, helper, "memory-region", &len);
	repeat = !!prop;
	if (repeat) {
		if (len != sizeof(old_phandles))
			return -EEXIST;
		memcpy(old_phandles, prop, len);
		prop = fdt_getprop(blob, helper, "apple,rtkit-handoff", &len);
		if (!prop || len != 4 || fdt32_to_cpu(*(const fdt32_t *)prop) != 2)
			return -EEXIST;
		prop = fdt_getprop(blob, helper, "memory-region-names", &len);
		if (!prop || len != sizeof(rtkit_handoff_name_list) ||
		    memcmp(prop, rtkit_handoff_name_list, len))
			return -EEXIST;
	}
	reserved = fdt_path_offset(blob, "/reserved-memory");
	if (reserved < 0 || fdt_address_cells(blob, reserved) != 2 ||
	    fdt_size_cells(blob, reserved) != 2)
		return -EINVAL;
	for (i = 0; i < 4; i++) {
		u64 addr = priv->handoff[i][0], size = priv->handoff[i][1];
		fdt64_t region[] = { cpu_to_fdt64(addr), cpu_to_fdt64(size) };
		fdt32_t map[5];
		u32 phandle;
		char name[80];

		map[0] = cpu_to_fdt32(helper_phandle);
		memcpy(&map[1], region, sizeof(region));
		snprintf(name, sizeof(name), "%s@%llx", rtkit_handoff_names[i],
			 (unsigned long long)addr);
		reserved = fdt_path_offset(blob, "/reserved-memory");
		node = fdt_subnode_offset(blob, reserved, name);
		if (repeat) {
			if (node < 0 || fdt_get_phandle(blob, node) !=
			    fdt32_to_cpu(old_phandles[i]))
				return -EEXIST;
			prop = fdt_getprop(blob, node, "reg", &len);
			if (!prop || len != sizeof(region) || memcmp(prop, region, len))
				return -EEXIST;
			prop = fdt_getprop(blob, node, "iommu-addresses", &len);
			if (!prop || len != sizeof(map) || memcmp(prop, map, len))
				return -EEXIST;
			if (!fdt_getprop(blob, node, "no-map", &len) || len)
				return -EEXIST;
			continue;
		}
		if (node != -FDT_ERR_NOTFOUND)
			return -EEXIST;
		ret = fdt_generate_phandle(blob, &phandle);
		if (ret)
			return ret;
		node = fdt_add_subnode(blob, reserved, name);
		if (node < 0)
			return node;
		ret = fdt_setprop(blob, node, "reg", region, sizeof(region));
		if (ret)
			return ret;
		ret = fdt_setprop_empty(blob, node, "no-map");
		if (ret)
			return ret;
		ret = fdt_setprop_u32(blob, node, "phandle", phandle);
		if (ret)
			return ret;
		ret = fdt_setprop(blob, node, "iommu-addresses", map, sizeof(map));
		if (ret)
			return ret;
		phandles[i] = cpu_to_fdt32(phandle);
	}
	if (repeat)
		return 0;
	helper = fdt_node_offset_by_phandle(blob, helper_phandle);
	if (helper < 0)
		return helper;
	ret = fdt_setprop(blob, helper, "memory-region", phandles, sizeof(phandles));
	if (ret)
		return ret;
	ret = fdt_setprop(blob, helper, "memory-region-names",
			  rtkit_handoff_name_list, sizeof(rtkit_handoff_name_list));
	if (ret)
		return ret;
	ret = fdt_setprop_u32(blob, helper, "apple,rtkit-handoff", 2);
	if (!ret)
		printf("MTP RTKit: published live-session handoff with four buffers\n");
	return ret;
}
