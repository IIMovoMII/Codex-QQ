param([switch]$DebugBuild, [switch]$NoBundle)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$app = Join-Path $root 'desktop-app'
$env:PATH = "$env:USERPROFILE\.cargo\bin;$env:PATH"
# Prefer GNU GCC rather than an identically named LLVM shim which lacks libgcc.
$winlibs = Join-Path $env:LOCALAPPDATA 'Microsoft/WinGet/Packages/BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe/mingw64/bin'
if (Test-Path -LiteralPath $winlibs) { $env:PATH = "$winlibs;$env:PATH" }
$compiler = Get-Command x86_64-w64-mingw32-gcc -ErrorAction Stop
$compilerVersion = & $compiler.Source --version
if (($compilerVersion -join ' ') -notmatch 'Brecht Sanders|GCC|gcc.exe') { throw '需要 GNU MinGW GCC；当前同名命令不匹配。' }
if (-not $env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR = Join-Path $env:LOCALAPPDATA 'CodexQQBuild' }
$key = Join-Path $env:LOCALAPPDATA 'CodexQQSigning/desktop.key'
if (-not $DebugBuild -and -not $NoBundle -and -not $env:TAURI_SIGNING_PRIVATE_KEY) {
  if (-not (Test-Path -LiteralPath $key)) { throw '缺少更新签名私钥。请在构建机安全配置 TAURI_SIGNING_PRIVATE_KEY。' }
  $env:TAURI_SIGNING_PRIVATE_KEY = $key
}
$env:PYTHONUTF8 = '1'
Push-Location $root
try {
  py -3 -X utf8 tools/prepare_desktop_package.py
  if ($LASTEXITCODE) { throw '内嵌运行时准备失败。' }
  $resources = Join-Path $app 'src-tauri/resources'
  & (Join-Path $resources 'python/python.exe') -I -B -X utf8 -m codex_desktop_weixin.package_health $resources
  if ($LASTEXITCODE) { throw '独立运行入口健康检查失败。' }
  Push-Location $app
  try {
    $arguments = @('run','tauri','--','build','--target','x86_64-pc-windows-gnu')
    if ($DebugBuild) { $arguments += '--debug' }
    if ($NoBundle) { $arguments += '--no-bundle' }
    $arguments += @('--','--locked')
    & npm @arguments
    if ($LASTEXITCODE) { throw '桌面构建失败。' }
  } finally { Pop-Location }
} finally { Pop-Location }
