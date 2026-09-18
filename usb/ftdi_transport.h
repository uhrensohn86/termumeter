#ifndef FTDI_TRANSPORT_H
#define FTDI_TRANSPORT_H

#include <stddef.h>
#include <stdint.h>
#include <libusb-1.0/libusb.h>

#define FTDI_EP_IN  0x81
#define FTDI_EP_OUT 0x02

#define FTDI_BAUD_300   0x2710
#define FTDI_BAUD_600   0x1388
#define FTDI_BAUD_1200  0x09C4
#define FTDI_BAUD_2400  0x04E2
#define FTDI_BAUD_4800  0x0271
#define FTDI_BAUD_9600  0x4138
#define FTDI_BAUD_19200 0x809C

#define FTDI_DATA_7E1 (7 | (2 << 8))
#define FTDI_LSR_TEMT 0x40

typedef struct {
    libusb_context *ctx;
    libusb_device_handle *handle;
    int claimed;
} ftdi_transport;

long ftdi_now_ms(void);

int ftdi_open_fd(ftdi_transport *t, int fd);
void ftdi_close(ftdi_transport *t);

int ftdi_reset(ftdi_transport *t, int index);
int ftdi_purge_rx(ftdi_transport *t, int index);
int ftdi_purge_tx(ftdi_transport *t, int index);
int ftdi_set_baud_raw(ftdi_transport *t, int value, int index);
int ftdi_set_data(ftdi_transport *t, int value, int index);

int ftdi_write(ftdi_transport *t, const unsigned char *data,
               int length, int timeout_ms);

/*
 * Liest genau einen FTDI-USB-IN-Transfer.
 * Die zwei FTDI-Statusbytes werden NICHT als Nutzdaten zurueckgegeben.
 *
 * return >= 0: Anzahl UART-Nutzbytes
 * return < 0 : libusb-Fehlercode
 *
 * line_status darf NULL sein.
 */
int ftdi_read(ftdi_transport *t, unsigned char *data, int capacity,
              int timeout_ms, unsigned char *line_status);

void ftdi_print_usb_error(const char *text, int r);

#endif
