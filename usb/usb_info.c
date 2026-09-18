#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <unistd.h>
#include <libusb-1.0/libusb.h>

static void print_string(libusb_device_handle *h, uint8_t idx, const char *label) {
    unsigned char buf[256];
    if (!idx) {
        printf("%-14s -\n", label);
        return;
    }
    int r = libusb_get_string_descriptor_ascii(h, idx, buf, sizeof(buf));
    if (r < 0) printf("%-14s [nicht lesbar: %s]\n", label, libusb_error_name(r));
    else printf("%-14s %.*s\n", label, r, buf);
}

static const char *dir(uint8_t ep) {
    return (ep & LIBUSB_ENDPOINT_IN) ? "IN" : "OUT";
}

static const char *xtype(uint8_t a) {
    switch (a & LIBUSB_TRANSFER_TYPE_MASK) {
        case LIBUSB_TRANSFER_TYPE_CONTROL: return "Control";
        case LIBUSB_TRANSFER_TYPE_ISOCHRONOUS: return "Isochron";
        case LIBUSB_TRANSFER_TYPE_BULK: return "Bulk";
        case LIBUSB_TRANSFER_TYPE_INTERRUPT: return "Interrupt";
        default: return "?";
    }
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "Aufruf: termux-usb -r -e %s /dev/bus/usb/BBB/DDD\n", argv[0]);
        return 2;
    }

    char *end = NULL;
    long fd = strtol(argv[1], &end, 10);
    if (!argv[1][0] || (end && *end) || fd < 0) {
        fprintf(stderr, "Fehler: Erwartet wurde der von termux-usb uebergebene Dateideskriptor.\n");
        return 2;
    }

    libusb_context *ctx = NULL;
    libusb_device_handle *h = NULL;

    libusb_set_option(NULL, LIBUSB_OPTION_NO_DEVICE_DISCOVERY);
    int r = libusb_init(&ctx);
    if (r < 0) {
        fprintf(stderr, "libusb_init: %s\n", libusb_error_name(r));
        return 1;
    }

    r = libusb_wrap_sys_device(ctx, (intptr_t)fd, &h);
    if (r < 0 || !h) {
        fprintf(stderr, "libusb_wrap_sys_device: %s\n", libusb_error_name(r));
        libusb_exit(ctx);
        return 1;
    }

    libusb_device *dev = libusb_get_device(h);
    struct libusb_device_descriptor d;
    r = libusb_get_device_descriptor(dev, &d);
    if (r < 0) {
        fprintf(stderr, "Device Descriptor: %s\n", libusb_error_name(r));
        libusb_close(h);
        libusb_exit(ctx);
        return 1;
    }

    printf("USB-INFO\n========\n\n");
    printf("VID:PID:       %04x:%04x\n", d.idVendor, d.idProduct);
    printf("USB-Version:   %x.%02x\n", d.bcdUSB >> 8, d.bcdUSB & 0xff);
    printf("Device-Version:%x.%02x\n", d.bcdDevice >> 8, d.bcdDevice & 0xff);
    printf("Device-Class:  0x%02x\n", d.bDeviceClass);
    printf("Configurations:%u\n", d.bNumConfigurations);
    print_string(h, d.iManufacturer, "Manufacturer:");
    print_string(h, d.iProduct, "Product:");
    print_string(h, d.iSerialNumber, "Serial:");

    for (uint8_t ci = 0; ci < d.bNumConfigurations; ci++) {
        struct libusb_config_descriptor *cfg = NULL;
        r = libusb_get_config_descriptor(dev, ci, &cfg);
        if (r < 0) {
            printf("\nConfig %u: [nicht lesbar: %s]\n", ci, libusb_error_name(r));
            continue;
        }
        printf("\nConfig %u: interfaces=%u value=%u\n",
               ci, cfg->bNumInterfaces, cfg->bConfigurationValue);
        for (int i = 0; i < cfg->bNumInterfaces; i++) {
            const struct libusb_interface *iface = &cfg->interface[i];
            for (int a = 0; a < iface->num_altsetting; a++) {
                const struct libusb_interface_descriptor *alt = &iface->altsetting[a];
                printf("  Interface %u alt %u: class=0x%02x subclass=0x%02x protocol=0x%02x\n",
                       alt->bInterfaceNumber, alt->bAlternateSetting,
                       alt->bInterfaceClass, alt->bInterfaceSubClass,
                       alt->bInterfaceProtocol);
                for (uint8_t e = 0; e < alt->bNumEndpoints; e++) {
                    const struct libusb_endpoint_descriptor *ep = &alt->endpoint[e];
                    printf("    Endpoint 0x%02x %-3s %-9s maxpacket=%u interval=%u\n",
                           ep->bEndpointAddress, dir(ep->bEndpointAddress),
                           xtype(ep->bmAttributes), ep->wMaxPacketSize, ep->bInterval);
                }
            }
        }
        libusb_free_config_descriptor(cfg);
    }

    libusb_close(h);
    libusb_exit(ctx);
    return 0;
}
