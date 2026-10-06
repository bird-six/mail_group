; Keep the electron-builder appId stable: all desktop 3.x releases share the same
; registry identity. App data lives in the Electron profile, outside $INSTDIR.
!include "LogicLib.nsh"
!include "WordFunc.nsh"

!macro mailGroupStop MESSAGE CODE
  MessageBox MB_OK|MB_ICONEXCLAMATION "${MESSAGE}" /SD IDOK
  SetErrorLevel ${CODE}
  Quit
!macroend

!macro customWelcomePage
  !ifdef MAILGROUP_UPDATE_ONLY
    !define MUI_WELCOMEPAGE_TITLE "升级邮件群发助手至 ${VERSION}"
    !define MUI_WELCOMEPAGE_TEXT "此离线升级包沿用已安装版本的位置，保留邮箱配置、授权码、收件人、模板、附件、任务和邮件缓存。$\r$\n$\r$\n请先在旧程序中打开数据文件夹，正常退出程序后备份整个 data 文件夹。无需卸载旧版。$\r$\n$\r$\n有发送任务时请选择“暂停并退出”，等待窗口关闭后继续。升级完成后从原快捷方式启动。"
  !else
    !define MUI_WELCOMEPAGE_TITLE "安装邮件群发助手 ${VERSION}"
    !define MUI_WELCOMEPAGE_TEXT "安装后可直接使用，无需 Python 或 Node.js。$\r$\n$\r$\n已有桌面版时可覆盖安装，程序数据保存在 Windows 用户的数据目录中，安装器不会重置。$\r$\n$\r$\n请先正常退出正在运行的程序；升级前建议备份整个 data 文件夹。"
  !endif
  !insertmacro MUI_PAGE_WELCOME
!macroend

!macro customInit
  !ifdef MAILGROUP_UPDATE_ONLY
    ; Reject absent or ambiguous installations before making any changes.
    ReadRegStr $R0 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
    ReadRegStr $R1 HKLM "${INSTALL_REGISTRY_KEY}" InstallLocation
    ${If} $R0 != ""
    ${AndIf} $R1 != ""
      !insertmacro mailGroupStop "检测到当前用户和所有用户的两份安装。请使用完整安装包选择要更新的安装。数据不会被清空。" 2
    ${EndIf}
    ${If} $R0 == ""
    ${AndIf} $R1 == ""
      !insertmacro mailGroupStop "未找到已安装的桌面版。首次安装请运行 nsis 安装包；免安装版用户请使用新版 portable 程序。" 2
    ${EndIf}
  !endif
  ; Assisted installs skip the section check in the elevated inner process.
  ; Check here too so per-machine upgrades receive the same protections.
  !insertmacro customCheckAppRunning
!macroend

!ifdef MAILGROUP_UPDATE_ONLY
  !macro customInstallMode
    !ifndef BUILD_UNINSTALLER
      ${If} $hasPerMachineInstallation == "1"
        StrCpy $isForceMachineInstall "1"
      ${Else}
        StrCpy $isForceCurrentInstall "1"
      ${EndIf}
    !endif
  !macroend
!endif

!macro mailGroupRequireClosed EXE_NAME
  nsProcess::_FindProcess "${EXE_NAME}"
  Pop $R0
  ${If} $R0 == 0
    !insertmacro mailGroupStop "邮件群发助手或本地服务仍在运行。请在程序中正常退出；有任务时选择“暂停并退出”，等待窗口关闭后重新运行安装程序。安装器不会强制结束发送任务。" 3
  ${ElseIf} $R0 != 603
    !insertmacro mailGroupStop "无法确认程序是否已退出，安装已停止。请关闭邮件群发助手后重试。" 4
  ${EndIf}
!macroend

!macro customCheckAppRunning
  !ifndef BUILD_UNINSTALLER
    !ifdef MAILGROUP_UPDATE_ONLY
      ; Ignore /D overrides: updates must replace the registered installation.
      ReadRegStr $R0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" InstallLocation
      ${If} $R0 == ""
        !insertmacro mailGroupStop "未找到此安装范围下的旧版，请使用完整安装包。" 2
      ${EndIf}
      ${IfNot} ${FileExists} "$R0\${APP_EXECUTABLE_FILENAME}"
        !insertmacro mailGroupStop "旧版程序文件不完整，请使用完整安装包修复安装。原数据保留。" 2
      ${EndIf}
      StrCpy $INSTDIR $R0
      StrCpy $appExe "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
    !endif
    ReadRegStr $R0 SHELL_CONTEXT "${UNINSTALL_REGISTRY_KEY}" DisplayVersion
    ${If} $R0 != ""
      ${VersionCompare} $R0 "${VERSION}" $R1
      ${If} $R1 == 1
        !insertmacro mailGroupStop "已安装版本比此安装包更新。为保护数据，请使用相同或更高版本的安装包。" 5
      ${EndIf}
    ${EndIf}
  !endif
  ; Unlike the default installer, never kill an app that may be sending mail.
  ; Check portable instances and the service as well as the installed GUI.
  !insertmacro mailGroupRequireClosed "${APP_EXECUTABLE_FILENAME}"
  !insertmacro mailGroupRequireClosed "mail-group-service.exe"
!macroend
