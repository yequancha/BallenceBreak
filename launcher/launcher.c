/*
 * BreakReminder launcher
 * 用 pythonw.exe 无黑框启动同目录下的 break_reminder.py。
 * 刻意避免任何 C 运行库（手动实现 wcsrchr + 纯 Win32 API），
 * 使 exe 完全无需 UCRT/MSVCRT，任何 Win10 都可直接运行。
 *
 * 编译（无 CRT 依赖）:
 *   windres icon.rc -O coff -o icon.res
 *   gcc launcher.c icon.res -o ..\\久坐休息提醒.exe -mwindows -O2 -s \
 *       -nostdlib -luser32 -lkernel32
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#ifndef _countof
#define _countof(a) (sizeof(a) / sizeof((a)[0]))
#endif

/* 手写宽字符逆向查找，替代 wcsrchr（避免 UCRT 依赖） */
static const wchar_t *wsrchr(const wchar_t *s, wchar_t c) {
    const wchar_t *hit = NULL;
    for (; *s; s++) if (*s == c) hit = s;
    return hit;
}

static int dir_of_exe(wchar_t *buf, int nbuf) {
    DWORD n = GetModuleFileNameW(NULL, buf, (DWORD)nbuf);
    if (!n) return 0;
    const wchar_t *slash = wsrchr(buf, L'\\');
    if (slash) buf[(int)(slash - buf)] = L'\0';
    return 1;
}

/* 定位 pythonw.exe：先按环境变量，再按已知安装路径 */
static int find_pythonw(wchar_t *buf, int nbuf) {
    /* 1) PATH */
    if (SearchPathW(NULL, L"pythonw.exe", NULL, (DWORD)nbuf, buf, NULL)) return 1;
    /* 2) 常见用户级安装（只展开 %USERNAME%，其余为纯路径） */
    wchar_t user[64]; user[0] = L'\0';
    DWORD ul = GetEnvironmentVariableW(L"USERNAME", user, _countof(user));
    wchar_t p[MAX_PATH];
    static const wchar_t *tmpls[] = {
        L"C:\\Users\\%s\\AppData\\Local\\Programs\\Python\\Python38\\pythonw.exe",
        L"C:\\Users\\%s\\AppData\\Local\\Programs\\Python\\Python39\\pythonw.exe",
        L"C:\\Users\\%s\\AppData\\Local\\Programs\\Python\\Python310\\pythonw.exe",
        L"C:\\Users\\%s\\AppData\\Local\\Programs\\Python\\Python311\\pythonw.exe",
        L"C:\\Users\\%s\\AppData\\Local\\Programs\\Python\\Python312\\pythonw.exe",
        L"C:\\Users\\%s\\AppData\\Local\\Programs\\Python\\Python313\\pythonw.exe",
        L"C:\\Python38\\pythonw.exe", L"C:\\Python39\\pythonw.exe",
        L"C:\\Python310\\pythonw.exe", L"C:\\Python311\\pythonw.exe",
        L"C:\\Python312\\pythonw.exe", L"C:\\Python313\\pythonw.exe",
        L"C:\\Python\\pythonw.exe",
    };
    for (int i = 0; i < (int)_countof(tmpls); i++) {
        if (ul) wsprintfW(p, tmpls[i], user);
        else     lstrcpynW(p, tmpls[i] + 9, MAX_PATH);   /* 剥离 %s 前缀硬编码 */
        if (GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES) {
            lstrcpynW(buf, p, nbuf);
            return 1;
        }
    }
    return 0;
}

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR cmdLine, int nShow) {
    HWND hMsg;
    wchar_t dir[MAX_PATH], script[MAX_PATH], pythonw[MAX_PATH], cmd[MAX_PATH * 2];
    (void)hInst; (void)hPrev; (void)cmdLine; (void)nShow;

    if (!dir_of_exe(dir, _countof(dir))) return 1;

    wsprintfW(script, L"%s\\break_reminder.py", dir);
    if (GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL, L"未找到 break_reminder.py，请将其与本程序放在同一目录。",
                    L"久坐休息提醒", MB_OK | MB_ICONERROR);
        return 1;
    }

    if (!find_pythonw(pythonw, _countof(pythonw))) {
        MessageBoxW(NULL, L"未找到 pythonw.exe，请安装 Python 3.8+ 并勾选 Add to PATH。",
                    L"久坐休息提醒", MB_OK | MB_ICONERROR);
        return 1;
    }

    wsprintfW(cmd, L"\"%s\" \"%s\"", pythonw, script);
    STARTUPINFOW si = { sizeof(si) };
    PROCESS_INFORMATION pi = { 0 };
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE,
                        CREATE_NO_WINDOW, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"启动 Python 失败，请确认 Python 已正确安装。",
                    L"久坐休息提醒", MB_OK | MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    (void)hMsg;
    return 0;
}
