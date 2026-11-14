; The bootstrap is independent of the version being overwritten. Solid LZMA
; shares the identical Python bytes with the final payload.
!define CODEXQQ_HOOK_DIR "${__FILEDIR__}"
!macro NSIS_HOOK_PREINSTALL
  ; A handle remains open for the lifetime of NSIS, excluding concurrent writes.
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\CodexQQInstaller") p.r8 ?e'
  Pop $R0
  ${If} $8 = 0
  ${OrIf} $R0 = 183
    ${IfNot} ${Silent}
      MessageBox MB_OK|MB_ICONSTOP "另一个安装器正在运行，请等待它完成后再试。"
    ${EndIf}
    SetErrorLevel 4
    Abort
  ${EndIf}
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\CodexQQMaintenance\python"
  File /r "${CODEXQQ_HOOK_DIR}\..\resources\python\*"
  SetOutPath "$PLUGINSDIR\CodexQQMaintenance"
  File /oname=maintenance.py "${CODEXQQ_HOOK_DIR}\..\..\..\src\codex_desktop_weixin\package_install.py"
  File /oname=incoming.json "${CODEXQQ_HOOK_DIR}\..\resources\build-manifest.json"
  System::Call 'kernel32::GetCurrentProcessId() i.r9'
  nsExec::ExecToStack '"$PLUGINSDIR\CodexQQMaintenance\python\python.exe" -I -B "$PLUGINSDIR\CodexQQMaintenance\maintenance.py" prepare "$INSTDIR" --manifest "$PLUGINSDIR\CodexQQMaintenance\incoming.json" --bootstrap "$PLUGINSDIR\CodexQQMaintenance\python" --installer-pid $9'
  Pop $R0
  Pop $R1
  ${If} $R0 != 0
    ${IfNot} ${Silent}
      MessageBox MB_OK|MB_ICONSTOP "无法建立程序回退备份，安装尚未开始。请检查磁盘空间或重新运行安装器完成恢复。"
    ${EndIf}
    SetErrorLevel 4
    Abort
  ${EndIf}
  SetOutPath $INSTDIR
!macroend

!macro CODEXQQ_ROLLBACK
  nsExec::ExecToStack '"$PLUGINSDIR\CodexQQMaintenance\python\python.exe" -I -B "$PLUGINSDIR\CodexQQMaintenance\maintenance.py" rollback "$INSTDIR"'
  Pop $R2
  Pop $R3
!macroend

; Health gates precede shortcut/registry publication.
!macro NSIS_HOOK_VERIFYINSTALL
  ExecWait '"$INSTDIR\codex-qq-desktop.exe" --health-check' $R0
  ${If} $R0 != 0
    !insertmacro CODEXQQ_ROLLBACK
    ${IfNot} ${Silent}
      MessageBox MB_OK|MB_ICONSTOP "安装文件校验失败，已尝试恢复原程序。QQ 绑定与运行数据保留；请重新运行安装器查看恢复结果。"
    ${EndIf}
    SetErrorLevel 4
    Abort
  ${EndIf}
  nsExec::ExecToStack '"$INSTDIR\python\python.exe" -I -B -X utf8 -m codex_desktop_weixin.package_health "$INSTDIR"'
  Pop $R0
  Pop $R1
  ${If} $R0 != 0
    !insertmacro CODEXQQ_ROLLBACK
    ${IfNot} ${Silent}
      MessageBox MB_OK|MB_ICONSTOP "内置运行组件检查失败，已尝试恢复原程序。现有 QQ 绑定和运行数据已保留。"
    ${EndIf}
    SetErrorLevel 4
    Abort
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
  nsExec::ExecToStack '"$PLUGINSDIR\CodexQQMaintenance\python\python.exe" -I -B "$PLUGINSDIR\CodexQQMaintenance\maintenance.py" commit "$INSTDIR"'
  Pop $R0
  Pop $R1
  ${If} $R0 != 0
    !insertmacro CODEXQQ_ROLLBACK
    SetErrorLevel 4
    Abort
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  ${If} $UpdateMode <> 1
    nsExec::ExecToStack '"$INSTDIR\python\python.exe" -I -B -X utf8 "$INSTDIR\backend\codex_desktop_weixin\desktop_uninstall.py" "$INSTDIR"'
    Pop $R0
    Pop $R1
    ${If} $R0 != 0
      ${IfNot} ${Silent}
        MessageBox MB_OK|MB_ICONSTOP "无法核实本应用的自启和提醒钩子，程序文件尚未删除。请检查安装完整性后重试；QQ 绑定与运行数据保留。"
      ${EndIf}
      SetErrorLevel 4
      Abort
    ${EndIf}
  ${EndIf}
!macroend
