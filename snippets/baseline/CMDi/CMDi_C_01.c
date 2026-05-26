#include <stdio.h>
#include <stdlib.h>

int ping_host(const char *host) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd), "ping -c 1 %s", host);
    return system(cmd);
}
