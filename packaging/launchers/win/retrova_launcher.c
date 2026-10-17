/*
 * Retrova.exe - Windows GUI-subsystem launcher for the bundled CPython 3.12.
 *
 * Retrova.exe sits next to python312.dll, DLLs\ and Lib\ (the python-build-
 * standalone layout), loads python312.dll and calls Py_Main() in-process.
 * So the running app *is* Retrova.exe: our icon and name in the taskbar,
 * Task Manager and the UAC prompt, and pywebview picks the window icon from
 * sys.executable (= Retrova.exe).
 *
 * Argument handling (sys.executable is Retrova.exe, so it must also behave as
 * a Python interpreter for child processes, e.g. the elevated helper started
 * with ShellExecute "runas" or multiprocessing workers):
 *   Retrova.exe                       -> python -E -s -m retrova
 *   Retrova.exe --serve-only ...      -> python -E -s -m retrova --serve-only ...
 *   Retrova.exe -m mod / -c code ...  -> plain interpreter (pass-through, still -E -s)
 *   Retrova.exe script.py ...         -> plain interpreter (pass-through)
 *
 * -E/-s keep a user's global PYTHONPATH/PYTHONHOME or user site-packages
 * from breaking the bundled runtime. The app package lives in .\app and is
 * put on sys.path by Lib\site-packages\retrova-app.pth (no env vars needed,
 * which matters because elevated processes don't inherit the environment).
 *
 * Started without arguments (double-click), stderr goes to
 * %LOCALAPPDATA%\Retrova\logs\retrova.log so crashes can be reported.
 */
#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shellapi.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define APP_NAME L"Retrova"
#define MODULE_NAME L"retrova"

typedef int(__cdecl *Py_Main_t)(int, wchar_t **);

static void fail(const wchar_t *msg)
{
    MessageBoxW(NULL, msg, APP_NAME L" couldn't start", MB_OK | MB_ICONERROR);
}

static int ends_with(const wchar_t *s, const wchar_t *suffix)
{
    size_t a = wcslen(s), b = wcslen(suffix);
    return a >= b && _wcsicmp(s + a - b, suffix) == 0;
}

static int is_python_style_invocation(const wchar_t *arg)
{
    if (arg[0] == L'-' && arg[1] != L'-' && arg[1] != 0)
        return 1; /* -m, -c, -B, -E, -I, -s, -S, -u, -X, -W, -O ... */
    if (ends_with(arg, L".py") || ends_with(arg, L".pyw") || ends_with(arg, L".pyc") ||
        ends_with(arg, L".pyz"))
        return 1;
    return 0;
}

static void redirect_output_to_log(void)
{
    wchar_t dir[MAX_PATH * 2], path[MAX_PATH * 2], old[MAX_PATH * 2];
    DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH)
        return;
    wcscat(dir, L"\\" APP_NAME);
    CreateDirectoryW(dir, NULL);
    wcscat(dir, L"\\logs");
    CreateDirectoryW(dir, NULL);
    wcscpy(path, dir);
    wcscat(path, L"\\retrova.log");
    WIN32_FILE_ATTRIBUTE_DATA fa;
    if (GetFileAttributesExW(path, GetFileExInfoStandard, &fa) &&
        (fa.nFileSizeHigh > 0 || fa.nFileSizeLow > 2u * 1024u * 1024u)) {
        wcscpy(old, dir);
        wcscat(old, L"\\retrova.log.1");
        MoveFileExW(path, old, MOVEFILE_REPLACE_EXISTING);
    }
    SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
    HANDLE h = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           &sa, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        return;
    /* python312.dll's CRT is loaded after this, so it picks the handle up as fd 2.
     * Only stderr: Python writes stderr with errors="backslashreplace", so a
     * print() of an unencodable file name can never crash the app (stdout stays
     * None, exactly like pythonw.exe). Tracebacks and warnings land in the log. */
    SetStdHandle(STD_ERROR_HANDLE, h);
}

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, PWSTR cmdLine, int nShow)
{
    (void)hInst; (void)hPrev; (void)cmdLine; (void)nShow;

    wchar_t exe[32768];
    DWORD len = GetModuleFileNameW(NULL, exe, (DWORD)(sizeof(exe) / sizeof(exe[0])));
    if (len == 0 || len >= sizeof(exe) / sizeof(exe[0])) {
        fail(L"Could not find the application's own location.");
        return 70;
    }
    wchar_t dir[32768];
    wcscpy(dir, exe);
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash)
        *slash = 0;

    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) {
        fail(L"Could not read the command line.");
        return 70;
    }

    int inject = !(argc > 1 && is_python_style_invocation(argv[1]));
    wchar_t **nargv = (wchar_t **)calloc((size_t)argc + 8, sizeof(wchar_t *));
    if (!nargv)
        return 71;
    int n = 0;
    nargv[n++] = exe;
    nargv[n++] = L"-E";
    nargv[n++] = L"-s";
    if (inject) {
        nargv[n++] = L"-m";
        nargv[n++] = MODULE_NAME;
    }
    for (int i = 1; i < argc; i++)
        nargv[n++] = argv[i];
    nargv[n] = NULL;

    if (inject && argc == 1 && GetStdHandle(STD_ERROR_HANDLE) == NULL &&
        GetEnvironmentVariableW(L"RETROVA_NO_LOG", NULL, 0) == 0)
        redirect_output_to_log();

    /* Load python312.dll from our own folder only (plus its folder for vcruntime). */
    wchar_t dll[32768];
    wcscpy(dll, dir);
    wcscat(dll, L"\\python312.dll");
    HMODULE py = LoadLibraryExW(dll, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!py) {
        fail(L"The bundled Python runtime (python312.dll) is missing or damaged.\n\n"
             L"Please reinstall Retrova.");
        return 70;
    }
    Py_Main_t py_main = (Py_Main_t)(void *)GetProcAddress(py, "Py_Main");
    if (!py_main) {
        fail(L"The bundled Python runtime is damaged (Py_Main not found).\n\nPlease reinstall Retrova.");
        return 70;
    }
    int rc = py_main(n, nargv);
    LocalFree(argv);
    return rc;
}
