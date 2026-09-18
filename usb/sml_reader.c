#include <stdio.h>
#include <stdlib.h>
#include <libusb-1.0/libusb.h>
#include "ftdi_transport.h"

#define FTDI_INDEX 1
#define BAUD_INDEX 0
#define CAPTURE_MS 2500
#define READ_TIMEOUT_MS 250

/*
 * Passiver SML-Testreader, Version 3.
 *
 * UART: 9600 Baud / 8N1
 * Keine Nutzdaten werden zum Zaehler gesendet.
 *
 * WICHTIG:
 * stdout enthaelt NICHT die binaeren SML-Bytes, sondern zwei
 * ASCII-Hexzeichen pro empfangenem Byte. Dadurch koennen auch
 * 0x00-Bytes sicher durch termux-usb transportiert werden.
 *
 * stderr enthaelt ausschliesslich Diagnosemeldungen.
 */

static int write_hex(const unsigned char *data, int length)
{
    static const char hex[] = "0123456789abcdef";

    for (int i = 0; i < length; i++) {
        unsigned char b = data[i];

        if (putchar(hex[b >> 4]) == EOF)
            return -1;
        if (putchar(hex[b & 0x0f]) == EOF)
            return -1;
    }

    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "[FEHLER] USB-Dateideskriptor fehlt\n");
        return 1;
    }

    char *end = NULL;
    long fd_long = strtol(argv[1], &end, 10);

    if (!argv[1][0] || *end != '\0' || fd_long < 0) {
        fprintf(stderr, "[FEHLER] Ungueltiger USB-Dateideskriptor\n");
        return 1;
    }

    ftdi_transport t;
    int r = ftdi_open_fd(&t, (int)fd_long);

    if (r < 0) {
        fprintf(stderr, "[ERROR] Opening FTDI/USB: %s\n",
                libusb_error_name(r));
        return 1;
    }

    fprintf(stderr, "[1] FTDI connected\n");

    r = ftdi_reset(&t, FTDI_INDEX);
    if (r < 0)
        goto usb_error;

    r = ftdi_set_baud_raw(&t, FTDI_BAUD_9600, BAUD_INDEX);
    if (r < 0)
        goto usb_error;

    /* 8 Datenbits, keine Paritaet, 1 Stopbit */
    r = ftdi_set_data(&t, 8, FTDI_INDEX);
    if (r < 0)
        goto usb_error;

    r = ftdi_purge_rx(&t, FTDI_INDEX);
    if (r < 0)
        goto usb_error;

    fprintf(stderr, "[2] 9600 baud / 8N1\n");
    fprintf(stderr, "[3] Passiver Empfang fuer 2.5 Sekunden\n");
    fprintf(stderr, "[4] stdout: ASCII hex (binary-safe)\n");

    unsigned char payload[62];
    size_t total = 0;
    long start = ftdi_now_ms();

    while (ftdi_now_ms() - start < CAPTURE_MS) {
        unsigned char line_status = 0;

        r = ftdi_read(&t,
                      payload,
                      (int)sizeof(payload),
                      READ_TIMEOUT_MS,
                      &line_status);

        if (r == LIBUSB_ERROR_TIMEOUT)
            continue;

        if (r < 0)
            goto usb_error;

        if (r == 0)
            continue;

        if (write_hex(payload, r) < 0) {
            fprintf(stderr, "[ERROR] Writing stdout\n");
            ftdi_close(&t);
            return 1;
        }

        total += (size_t)r;
    }

    if (putchar('\n') == EOF || fflush(stdout) != 0) {
        fprintf(stderr, "[ERROR] Finalizing stdout\n");
        ftdi_close(&t);
        return 1;
    }

    fprintf(stderr, "[5] Reception ended: %zu bytes\n", total);
    fprintf(stderr, "[6] Hex output: %zu characters\n", total * 2);

    ftdi_close(&t);
    return 0;

usb_error:
    fprintf(stderr, "[ERROR] USB/FTDI: %s\n", libusb_error_name(r));
    ftdi_close(&t);
    return 1;
}
