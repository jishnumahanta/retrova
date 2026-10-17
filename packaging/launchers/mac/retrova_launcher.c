/*
 * Retrova.app main executable (Contents/MacOS/Retrova).
 *
 * A tiny native launcher that embeds the bundled CPython 3.12
 * (Contents/Resources/python/lib/libpython3.12.dylib, found through the
 * @executable_path/../Resources/python/lib rpath) and runs `-m retrova`.
 * Because this Mach-O is the bundle's real main executable, macOS shows
 * "Retrova" with our icon in the Dock, the menu bar and Activity Monitor,
 * and privacy permissions (Full Disk Access etc.) are granted to Retrova.app.
 *
 * Argument handling (sys.executable is this launcher, so it must also work
 * as a Python interpreter for child processes):
 *   Retrova                          -> python -m retrova
 *   Retrova --no-window ...          -> python -m retrova --no-window ...
 *   Retrova -psn_0_12345 ...         -> (-psn_ dropped) python -m retrova ...
 *   Retrova -m mod / -c code / -B ...-> plain Python interpreter (pass-through)
 *   Retrova script.py ...            -> plain Python interpreter (pass-through)
 *
 * The interpreter always runs isolated from the user's Python environment
 * (like -E -s: PYTHONPATH/PYTHONHOME/user site-packages are ignored) and never
 * writes .pyc files into the signed bundle (the bundle ships precompiled ones).
 *
 * When started from Finder/Dock (no arguments, stderr not a terminal) stdout
 * and stderr go to ~/Library/Logs/Retrova/retrova.log so crashes can be reported.
 *
 * The same source builds on Linux for the packaging layout simulation
 * (packaging/rbuild/verify.py); only the executable-path lookup differs.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <spawn.h>
#include <sys/wait.h>

#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif

#ifndef RETROVA_APP_NAME
#define RETROVA_APP_NAME "Retrova"
#endif
#ifndef RETROVA_MODULE
#define RETROVA_MODULE "retrova"
#endif
/* Python home relative to the directory holding this executable. */
#ifndef RETROVA_PYTHON_HOME_REL
#define RETROVA_PYTHON_HOME_REL "../Resources/python"
#endif

extern char **environ;

static int executable_path(char *out, size_t size)
{
    char raw[PATH_MAX * 2];
#ifdef __APPLE__
    uint32_t n = (uint32_t)sizeof(raw);
    if (_NSGetExecutablePath(raw, &n) != 0)
        return -1;
#else
    ssize_t n = readlink("/proc/self/exe", raw, sizeof(raw) - 1);
    if (n <= 0)
        return -1;
    raw[n] = 0;
#endif
    if (!realpath(raw, out))
        return -1;
    (void)size;
    return 0;
}

static void show_alert(const char *message)
{
    /* Best effort: a visible message for people who double-clicked the app. */
#ifdef __APPLE__
    char safe[2048], script[4096];
    size_t k = 0;
    for (const char *p = message; *p && k + 1 < sizeof(safe); p++)
        safe[k++] = (*p == '"' || *p == '\\') ? '\'' : *p;
    safe[k] = 0;
    snprintf(script, sizeof(script),
             "display alert \"%s couldn't start\" message \"%s\" as critical",
             RETROVA_APP_NAME, safe);
    char *argv[] = {"/usr/bin/osascript", "-e", script, NULL};
    pid_t pid;
    if (posix_spawn(&pid, argv[0], NULL, NULL, argv, environ) == 0) {
        int st;
        waitpid(pid, &st, 0);
    }
#endif
    fprintf(stderr, "%s: %s\n", RETROVA_APP_NAME, message);
}

static void redirect_output_to_log(void)
{
    const char *home = getenv("HOME");
    if (!home || !*home)
        return;
    char dir[PATH_MAX], path[PATH_MAX + 64], old[PATH_MAX + 64];
#ifdef __APPLE__
    snprintf(dir, sizeof(dir), "%s/Library/Logs/%s", home, RETROVA_APP_NAME);
#else
    snprintf(dir, sizeof(dir), "%s/.cache/%s", home, RETROVA_APP_NAME);
#endif
    char parent[PATH_MAX + 64];
    snprintf(parent, sizeof(parent), "%s", dir);
    char *slash = strrchr(parent, '/');
    if (slash) {
        *slash = 0;
        mkdir(parent, 0755);
    }
    mkdir(dir, 0755);
    snprintf(path, sizeof(path), "%s/retrova.log", dir);
    struct stat st;
    if (stat(path, &st) == 0 && st.st_size > 2 * 1024 * 1024) {
        snprintf(old, sizeof(old), "%s/retrova.log.1", dir);
        rename(path, old);
    }
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0)
        return;
    dup2(fd, 1);
    dup2(fd, 2);
    if (fd > 2)
        close(fd);
}

static int ends_with(const char *s, const char *suffix)
{
    size_t a = strlen(s), b = strlen(suffix);
    return a >= b && strcmp(s + a - b, suffix) == 0;
}

static int is_python_style_invocation(const char *arg)
{
    if (arg[0] == '-' && arg[1] != '-' && arg[1] != 0 && strncmp(arg, "-psn_", 5) != 0)
        return 1; /* -m, -c, -B, -E, -I, -s, -S, -u, -X, -W, -O ... */
    if (ends_with(arg, ".py") || ends_with(arg, ".pyc") || ends_with(arg, ".pyz"))
        return 1;
    return 0;
}

static int fail_status(PyStatus status, PyConfig *config)
{
    if (config)
        PyConfig_Clear(config);
    if (PyStatus_IsExit(status))
        return status.exitcode;
    char msg[1024];
    snprintf(msg, sizeof(msg), "The bundled Python runtime failed to start (%s%s%s). Please reinstall %s.",
             status.func ? status.func : "", status.func ? ": " : "",
             status.err_msg ? status.err_msg : "unknown error", RETROVA_APP_NAME);
    show_alert(msg);
    return 70;
}

int main(int argc, char **argv)
{
    char exe[PATH_MAX], exe_dir[PATH_MAX], home_raw[PATH_MAX * 2], home[PATH_MAX];

    if (executable_path(exe, sizeof(exe)) != 0) {
        show_alert("Could not find the application's own location.");
        return 70;
    }
    snprintf(exe_dir, sizeof(exe_dir), "%s", exe);
    char *slash = strrchr(exe_dir, '/');
    if (slash)
        *slash = 0;
    snprintf(home_raw, sizeof(home_raw), "%s/%s", exe_dir, RETROVA_PYTHON_HOME_REL);
    if (!realpath(home_raw, home)) {
        show_alert("The bundled Python runtime is missing. Please reinstall the app.");
        return 70;
    }

    /* Build the interpreter argv. */
    char **nargv = calloc((size_t)argc + 8, sizeof(char *));
    if (!nargv)
        return 71;
    int n = 0, i = 1, inject = 1;
    nargv[n++] = exe;
    while (i < argc && strncmp(argv[i], "-psn_", 5) == 0)
        i++; /* Finder process serial number (old macOS) */
    if (i < argc && is_python_style_invocation(argv[i]))
        inject = 0;
    if (inject) {
        nargv[n++] = "-m";
        nargv[n++] = RETROVA_MODULE;
    }
    int user_args = 0;
    for (; i < argc; i++, user_args++)
        nargv[n++] = argv[i];
    nargv[n] = NULL;

    if (inject && user_args == 0 && !isatty(2) && !getenv("RETROVA_NO_LOG"))
        redirect_output_to_log();

    PyStatus status;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.use_environment = 0;     /* like -E: ignore PYTHONPATH, PYTHONHOME, ... */
    config.user_site_directory = 0; /* like -s */
    config.write_bytecode = 0;      /* like -B: never write into the signed bundle */
    config.parse_argv = 1;

    status = PyConfig_SetBytesString(&config, &config.home, home);
    if (PyStatus_Exception(status))
        return fail_status(status, &config);
    status = PyConfig_SetBytesString(&config, &config.program_name, exe);
    if (PyStatus_Exception(status))
        return fail_status(status, &config);
    status = PyConfig_SetBytesString(&config, &config.executable, exe);
    if (PyStatus_Exception(status))
        return fail_status(status, &config);
    status = PyConfig_SetBytesArgv(&config, n, nargv);
    if (PyStatus_Exception(status))
        return fail_status(status, &config);
    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status))
        return fail_status(status, &config);
    PyConfig_Clear(&config);
    return Py_RunMain();
}
