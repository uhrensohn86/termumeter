#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <unistd.h>
#include <string.h>

#include "ftdi_transport.h"

#define IEC_STX 0x02
#define IEC_ETX 0x03
#define MAX_BLOCK 65536

static int baud_from_iec_char(
    char baud_char, int *baud, int *ftdi_value, int *ftdi_index)
{
    switch (baud_char) {
        case '0': *baud = 300;   *ftdi_value = FTDI_BAUD_300;   *ftdi_index = 1; return 0;
        case '1': *baud = 600;   *ftdi_value = FTDI_BAUD_600;   *ftdi_index = 0; return 0;
        case '2': *baud = 1200;  *ftdi_value = FTDI_BAUD_1200;  *ftdi_index = 0; return 0;
        case '3': *baud = 2400;  *ftdi_value = FTDI_BAUD_2400;  *ftdi_index = 0; return 0;
        case '4': *baud = 4800;  *ftdi_value = FTDI_BAUD_4800;  *ftdi_index = 0; return 0;
        case '5': *baud = 9600;  *ftdi_value = FTDI_BAUD_9600;  *ftdi_index = 0; return 0;
        case '6': *baud = 19200; *ftdi_value = FTDI_BAUD_19200; *ftdi_index = 0; return 0;
        default: return -1;
    }
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "[FEHLER] USB-Dateideskriptor fehlt\n");
        return 1;
    }

    int fd = atoi(argv[1]);
    int r;
    unsigned char rx[62];
    unsigned char block[MAX_BLOCK];
    size_t block_pos = 0;
    ftdi_transport t;

    r = ftdi_open_fd(&t, fd);
    if (r < 0) {
        ftdi_print_usb_error("FTDI/USB oeffnen", r);
        return 1;
    }

    fprintf(stderr, "[1] FTDI verbunden\n");

    /* Bewaehrter IEC-Ausgangszustand: 300 Baud / 7E1. */
    r = ftdi_reset(&t, 1);
    if (r < 0) {
        ftdi_print_usb_error("FTDI Reset", r);
        goto fail;
    }

    r = ftdi_set_baud_raw(&t, FTDI_BAUD_300, 1);
    if (r < 0) {
        ftdi_print_usb_error("300 Baud setzen", r);
        goto fail;
    }

    r = ftdi_set_data(&t, FTDI_DATA_7E1, 1);
    if (r < 0) {
        ftdi_print_usb_error("7E1 setzen", r);
        goto fail;
    }

    /* Nur vor Beginn des IEC-Dialogs leeren. */
    ftdi_purge_rx(&t, 1);
    ftdi_purge_tx(&t, 1);

    fprintf(stderr, "[2] 300 Baud / 7E1\n");
    sleep(1);

    static const unsigned char request[] = {'/', '?', '!', '\r', '\n'};
    r = ftdi_write(&t, request, sizeof(request), 2000);
    if (r < 0) {
        ftdi_print_usb_error("/?! senden", r);
        goto fail;
    }

    fprintf(stderr, "[3] /?! gesendet\n");

    char ident[128];
    int ident_pos = 0;
    int started = 0;
    int complete = 0;
    long start = ftdi_now_ms();

    while (ftdi_now_ms() - start < 20000 && !complete) {
        unsigned char lsr = 0;
        int n = ftdi_read(&t, rx, sizeof(rx), 250, &lsr);

        if (n == LIBUSB_ERROR_TIMEOUT)
            continue;
        if (n < 0) {
            ftdi_print_usb_error("Identifikation empfangen", n);
            goto fail;
        }

        for (int i = 0; i < n; i++) {
            unsigned char ch = rx[i];

            if (!started) {
                if (ch != '/')
                    continue;
                started = 1;
            }

            if (ident_pos < 127)
                ident[ident_pos++] = (char)ch;

            if (ch == '\n') {
                complete = 1;
                break;
            }
        }
    }

    if (!complete) {
        fprintf(stderr, "[FEHLER] Keine vollstaendige Identifikation empfangen\n");
        goto fail;
    }

    ident[ident_pos] = '\0';

    if (ident_pos < 5 ||
        ident[0] != '/' ||
        ident[1] == '\r' || ident[1] == '\n' ||
        ident[2] == '\r' || ident[2] == '\n' ||
        ident[3] == '\r' || ident[3] == '\n') {
        fprintf(stderr, "[FEHLER] Ungueltige IEC-Identifikation\n");
        fprintf(stderr, "[RX] %s\n", ident);
        goto fail;
    }

    char baud_char = ident[4];
    int negotiated_baud = 0;
    int negotiated_ftdi_value = 0;
    int negotiated_ftdi_index = 0;

    if (baud_from_iec_char(
            baud_char, &negotiated_baud,
            &negotiated_ftdi_value, &negotiated_ftdi_index) < 0) {
        fprintf(stderr, "[FEHLER] Nicht unterstuetzte IEC-Baudkennung '%c'\n",
                baud_char);
        fprintf(stderr, "[RX] %s\n", ident);
        goto fail;
    }

    char ident_print[128];
    strncpy(ident_print, ident, sizeof(ident_print) - 1);
    ident_print[sizeof(ident_print) - 1] = '\0';

    for (int i = 0; ident_print[i]; i++) {
        if (ident_print[i] == '\r' || ident_print[i] == '\n') {
            ident_print[i] = '\0';
            break;
        }
    }

    fprintf(stderr, "[4] Zähler: %s\n", ident_print);

    unsigned char ack[] = {
        0x06, '0', (unsigned char)baud_char, '0', '\r', '\n'
    };

    /*
     * IEC Mode C: nach der Identifikationszeile vor der
     * Baudratenquittierung warten. Beim Sagemcom ist diese Pause
     * erforderlich, damit ACK0Z0 die Umschaltung auf die angebotene
     * Baudrate ausloest. Die Pause gilt generisch fuer IEC Mode C.
     */
    fprintf(stderr, "[5] 300 ms vor Baudratenquittierung\n");
    usleep(300000);

    long ack_start = ftdi_now_ms();

    r = ftdi_write(&t, ack, sizeof(ack), 1000);
    if (r < 0) {
        ftdi_print_usb_error("IEC-ACK senden", r);
        goto fail;
    }

    fprintf(stderr, "[6] ACK0%c0 gesendet\n", baud_char);

    /*
     * Bewaehrtes Timing unveraendert:
     * TEMT nicht vor 150 ms akzeptieren.
     */
    int tx_empty = 0;
    long temt_time = 0;

    while (ftdi_now_ms() - ack_start < 1000) {
        unsigned char lsr = 0;
        int n = ftdi_read(&t, rx, sizeof(rx), 50, &lsr);

        if (n == LIBUSB_ERROR_TIMEOUT)
            continue;
        if (n < 0) {
            ftdi_print_usb_error("TX-Status empfangen", n);
            goto fail;
        }

        long elapsed = ftdi_now_ms() - ack_start;
        if (elapsed >= 150 && (lsr & FTDI_LSR_TEMT)) {
            tx_empty = 1;
            temt_time = elapsed;
            break;
        }
    }

    if (!tx_empty) {
        fprintf(stderr, "[FEHLER] TX EMPTY nicht erkannt\n");
        goto fail;
    }

    fprintf(stderr, "[7] TX EMPTY nach %ld ms\n", temt_time);

    /*
     * Nach vollstaendig gesendetem ACK kurze Umschaltpause.
     * 200 ms entsprechen dem nun am Sagemcom verifizierten Ablauf.
     */
    usleep(200000);

    r = ftdi_set_baud_raw(
        &t, negotiated_ftdi_value, negotiated_ftdi_index
    );
    if (r < 0) {
        char error_text[64];
        snprintf(error_text, sizeof(error_text),
                 "%d Baud setzen", negotiated_baud);
        ftdi_print_usb_error(error_text, r);
        goto fail;
    }

    fprintf(stderr, "[8] %d Baud / 7E1\n", negotiated_baud);

    int have_stx = 0;
    int text_mode = 0;
    int have_etx = 0;
    int have_bcc = 0;
    unsigned char prefix[16];
    size_t prefix_pos = 0;

    start = ftdi_now_ms();

    while (ftdi_now_ms() - start < 30000 && !have_bcc) {
        unsigned char lsr = 0;
        int n = ftdi_read(&t, rx, sizeof(rx), 500, &lsr);

        if (n == LIBUSB_ERROR_TIMEOUT)
            continue;
        if (n < 0) {
            ftdi_print_usb_error("Datenblock empfangen", n);
            goto fail;
        }

        for (int i = 0; i < n; i++) {
            unsigned char ch = rx[i];

            if (!have_stx && !text_mode) {
                if (ch == IEC_STX) {
                    have_stx = 1;
                    prefix_pos = 0;
                    fprintf(stderr, "[9] STX erkannt\n");
                } else if (ch == '\r' || ch == '\n') {
                    /*
                     * EFR beginnt den STX-losen Textblock mit CR/LF.
                     * Erst puffern; dadurch wird Rauschen nicht sofort
                     * als gueltiger Textblock akzeptiert.
                     */
                    if (prefix_pos < sizeof(prefix))
                        prefix[prefix_pos++] = ch;
                    continue;
                } else if (ch >= 0x20 && ch <= 0x7e) {
                    text_mode = 1;
                    fprintf(stderr,
                            "[8] IEC-Textblock ohne STX erkannt\n");

                    if (block_pos + prefix_pos + 1 > MAX_BLOCK) {
                        fprintf(stderr, "[FEHLER] Datenblock zu gross\n");
                        goto fail;
                    }

                    if (prefix_pos > 0) {
                        memcpy(block + block_pos, prefix, prefix_pos);
                        block_pos += prefix_pos;
                    }
                    prefix_pos = 0;
                } else {
                    /* Unbekannte Vorlaufbytes verwerfen. */
                    prefix_pos = 0;
                    continue;
                }
            }

            if (block_pos >= MAX_BLOCK) {
                fprintf(stderr, "[FEHLER] Datenblock zu gross\n");
                goto fail;
            }

            block[block_pos++] = ch;

            if (have_etx) {
                have_bcc = 1;
                break;
            }

            if (ch == IEC_ETX)
                have_etx = 1;
        }
    }

    if (!have_stx && !text_mode) {
        fprintf(stderr, "[FEHLER] Kein IEC-Datenblock empfangen\n");
        goto fail;
    }
    if (!have_etx) {
        fprintf(stderr, "[FEHLER] Kein ETX empfangen\n");
        goto fail;
    }
    if (!have_bcc) {
        fprintf(stderr, "[FEHLER] Kein BCC empfangen\n");
        goto fail;
    }

    fprintf(stderr, "[10] Vollstaendiger IEC-Block: %zu Bytes\n", block_pos);

    size_t written = fwrite(block, 1, block_pos, stdout);
    fflush(stdout);

    if (written != block_pos) {
        fprintf(stderr, "[FEHLER] stdout unvollstaendig geschrieben\n");
        goto fail;
    }

    fprintf(stderr, "[11] Rohdaten ausgegeben\n");

    ftdi_close(&t);
    return 0;

fail:
    ftdi_close(&t);
    return 1;
}
