#include <spawn.h>
#include <sys/wait.h>

extern char **environ;

int ping_host(const char *host) {
    char *argv[] = {"ping", "-c", "1", (char *)host, NULL};
    pid_t pid = 0;
    if (posix_spawnp(&pid, "ping", NULL, NULL, argv, environ) != 0) {
        return -1;
    }

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        return -1;
    }
    return status;
}
