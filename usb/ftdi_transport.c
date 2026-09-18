#include "ftdi_transport.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#define FTDI_RESET_REQ 0
#define FTDI_BAUD_REQ  3
#define FTDI_DATA_REQ  4

static int ctrl(ftdi_transport *t, int req, int value, int index)
{
    return libusb_control_transfer(
        t->handle, 0x40, req, value, index, NULL, 0, 1000
    );
}

long ftdi_now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

void ftdi_print_usb_error(const char *text, int r)
{
    fprintf(stderr, "[FEHLER] %s: %s\n", text, libusb_error_name(r));
}

int ftdi_open_fd(ftdi_transport *t, int fd)
{
    int r;

    memset(t, 0, sizeof(*t));

    /*
     * termux-usb hat das Geraet bereits geoeffnet.
     * Keine eigene libusb-Device-Discovery starten.
     */
    libusb_set_option(NULL, LIBUSB_OPTION_NO_DEVICE_DISCOVERY);

    r = libusb_init(&t->ctx);
    if (r < 0)
        return r;

    r = libusb_wrap_sys_device(
        t->ctx, (intptr_t)fd, &t->handle
    );
    if (r < 0) {
        libusb_exit(t->ctx);
        t->ctx = NULL;
        return r;
    }

    r = libusb_kernel_driver_active(t->handle, 0);
    if (r == 1) {
        r = libusb_detach_kernel_driver(t->handle, 0);
        if (r < 0) {
            ftdi_close(t);
            return r;
        }
    } else if (r < 0 && r != LIBUSB_ERROR_NOT_SUPPORTED) {
        ftdi_close(t);
        return r;
    }

    r = libusb_claim_interface(t->handle, 0);
    if (r < 0) {
        ftdi_close(t);
        return r;
    }

    t->claimed = 1;
    return 0;
}

void ftdi_close(ftdi_transport *t)
{
    if (!t)
        return;

    if (t->claimed && t->handle) {
        libusb_release_interface(t->handle, 0);
        t->claimed = 0;
    }

    if (t->handle) {
        libusb_close(t->handle);
        t->handle = NULL;
    }

    if (t->ctx) {
        libusb_exit(t->ctx);
        t->ctx = NULL;
    }
}

int ftdi_reset(ftdi_transport *t, int index)
{
    return ctrl(t, FTDI_RESET_REQ, 0, index);
}

int ftdi_purge_rx(ftdi_transport *t, int index)
{
    return ctrl(t, FTDI_RESET_REQ, 1, index);
}

int ftdi_purge_tx(ftdi_transport *t, int index)
{
    return ctrl(t, FTDI_RESET_REQ, 2, index);
}

int ftdi_set_baud_raw(ftdi_transport *t, int value, int index)
{
    return ctrl(t, FTDI_BAUD_REQ, value, index);
}

int ftdi_set_data(ftdi_transport *t, int value, int index)
{
    return ctrl(t, FTDI_DATA_REQ, value, index);
}

int ftdi_write(ftdi_transport *t, const unsigned char *data,
               int length, int timeout_ms)
{
    int transferred = 0;
    int r = libusb_bulk_transfer(
        t->handle, FTDI_EP_OUT, (unsigned char *)data,
        length, &transferred, timeout_ms
    );

    if (r < 0)
        return r;

    if (transferred != length)
        return LIBUSB_ERROR_IO;

    return transferred;
}

int ftdi_read(ftdi_transport *t, unsigned char *data, int capacity,
              int timeout_ms, unsigned char *line_status)
{
    unsigned char packet[64];
    int transferred = 0;

    if (capacity < 0)
        return LIBUSB_ERROR_INVALID_PARAM;

    int r = libusb_bulk_transfer(
        t->handle, FTDI_EP_IN, packet, sizeof(packet),
        &transferred, timeout_ms
    );

    if (r < 0)
        return r;

    if (transferred < 2)
        return 0;

    if (line_status)
        *line_status = packet[1];

    int payload = transferred - 2;
    if (payload > capacity)
        return LIBUSB_ERROR_OVERFLOW;

    if (payload > 0)
        memcpy(data, packet + 2, payload);

    return payload;
}
