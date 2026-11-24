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
function Get-LocalSigningMaterial($expectedKey) {
  if ($PSVersionTable.PSEdition -eq 'Desktop') {
    Add-Type -AssemblyName System.Security
  } else {
    Add-Type -AssemblyName System.Security.Cryptography.ProtectedData
  }
  $paths = @($env:LOCALAPPDATA, $env:APPDATA) | ForEach-Object { Join-Path $_ 'CodexQQSigning/desktop.dpapi' }
  foreach ($path in $paths) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    $clear = $null
    try {
      $clear = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($path), $null, 'CurrentUser')
      $material = [Text.Encoding]::UTF8.GetString($clear) | ConvertFrom-Json
      if ($material.publicKey -ceq $expectedKey -and $material.privateKey -and $material.password) { return $material }
    } catch {
      # A missing or damaged local copy can be recovered from the roaming copy.
      continue
    } finally {
      if ($clear) { [Array]::Clear($clear, 0, $clear.Length) }
    }
  }
}
$signing = $null
$localKey = $null
if (-not $DebugBuild -and -not $NoBundle -and -not $env:TAURI_SIGNING_PRIVATE_KEY) {
  $config = Get-Content -Raw -LiteralPath (Join-Path $app 'src-tauri/tauri.conf.json') | ConvertFrom-Json
  $signing = Get-LocalSigningMaterial $config.plugins.updater.pubkey
  if (-not $signing) {
    $localKey = Join-Path $env:LOCALAPPDATA 'CodexQQSigning/desktop.key'
    if (-not (Test-Path -LiteralPath $localKey -PathType Leaf)) { throw '没有与发布公钥匹配的更新签名材料。请在构建机恢复加密备份或配置 TAURI_SIGNING_PRIVATE_KEY。' }
  }
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
  $previousPassword = $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD
  try {
    $arguments = @('run','tauri','--','build','--target','x86_64-pc-windows-gnu')
    if ($DebugBuild) { $arguments += '--debug' }
    if ($NoBundle) { $arguments += '--no-bundle' }
    $arguments += @('--','--locked')
    if ($signing) {
      $env:TAURI_SIGNING_PRIVATE_KEY = $signing.privateKey
      $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = $signing.password
    } elseif ($localKey) {
      $env:TAURI_SIGNING_PRIVATE_KEY = $localKey
    }
    & npm @arguments
    if ($LASTEXITCODE) { throw '桌面构建失败。' }
  } finally {
    if ($signing -or $localKey) {
      Remove-Item Env:TAURI_SIGNING_PRIVATE_KEY -ErrorAction SilentlyContinue
      [Environment]::SetEnvironmentVariable('TAURI_SIGNING_PRIVATE_KEY_PASSWORD', $previousPassword, 'Process')
    }
    Pop-Location
  }
} finally { Pop-Location }
