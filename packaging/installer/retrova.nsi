; Retrova Windows installer (built on Linux by packaging/rbuild/win.py with makensis).
; Per-user install, no administrator rights needed to install: the app asks for them
; only when it has to read a whole drive. Always installs into its own folder
; (%LOCALAPPDATA%\Programs\Retrova), and the uninstaller removes only the files the
; installer put there - never anything else.

Target amd64-unicode
SetCompressor /SOLID lzma
SetCompressorDictSize 64
ManifestDPIAware true
RequestExecutionLevel user

!include "MUI2.nsh"

Name "${APP_NAME}"
OutFile "${OUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
BrandingText "${APP_NAME} ${APP_VERSION}"

VIProductVersion "${APP_VERSION4}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "Copyright (C) 2026 Jishnu Mahanta. AGPL-3.0"

!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Install ${APP_NAME} ${APP_VERSION}"
!define MUI_WELCOMEPAGE_TEXT "${APP_NAME} copies files off drives that won't open, checks every copy byte for byte, and helps you take back disk space.$\r$\n$\r$\nIt only ever reads the drives you recover from, and anything it removes goes to the Recycle Bin.$\r$\n$\r$\nNo administrator rights are needed to install. Click Install to continue."
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_NAME}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open ${APP_NAME} now"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

!macro RemoveOurFiles
!include "${UNINSTALL_LIST}"
  Delete "$INSTDIR\Uninstall ${APP_NAME}.exe"
!macroend

Section "Install"
  SetShellVarContext current
  ; Upgrading: clear the previous version's runtime first (only our own files).
  IfFileExists "$INSTDIR\${APP_NAME}.exe" 0 +2
    !insertmacro RemoveOurFiles

  SetOutPath "$INSTDIR"
  File /r "${SRC_DIR}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall ${APP_NAME}.exe"

  CreateShortCut "$SMPROGRAMS\${APP_NAME}.lnk" "$INSTDIR\${APP_NAME}.exe" "" "$INSTDIR\${APP_NAME}.exe" 0
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_NAME}.exe" "" "$INSTDIR\${APP_NAME}.exe" 0

  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${APP_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_NAME}.exe,0"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall ${APP_NAME}.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall ${APP_NAME}.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" ${SIZE_KB}
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  ; Safety: only act on a folder that really holds Retrova.
  IfFileExists "$INSTDIR\${APP_NAME}.exe" +3 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "${APP_NAME} doesn't seem to be installed in $INSTDIR, so nothing was removed."
    Abort
  !insertmacro RemoveOurFiles
  RMDir "$INSTDIR"            ; only if it's now empty
  Delete "$SMPROGRAMS\${APP_NAME}.lnk"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  DeleteRegKey HKCU "${UNINST_KEY}"
  ; Your recovered files, logs and the Find Text index are left untouched.
SectionEnd
