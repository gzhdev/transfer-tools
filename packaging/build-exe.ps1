#Requires -Version 5.1
<#
.SYNOPSIS
    在 Windows 本机构建单文件 safecopy-gui.exe（PyInstaller onefile + windowed）。

.DESCRIPTION
    流程：建/复用虚拟环境 → 安装 ".[gui]" 与 pyinstaller →（默认）运行测试套件 →
    按 packaging/safecopy-gui.spec 构建 → 校验 dist\safecopy-gui.exe 存在。

    与 CI（.github/workflows/build-exe.yml，windows-latest）产出等价的单文件 exe：
    无控制台窗口、内嵌版本资源（packaging/version_info.txt）与图标
    （packaging/assets/safecopy-gui.ico）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1 -SkipTests -Clean
#>
[CmdletBinding()]
param(
    # 用于创建虚拟环境的 Python 启动器（"py" 或 "python"）。
    [string]$PythonLauncher = "py",
    # 虚拟环境目录（相对仓库根）。
    [string]$VenvDir = ".venv-win",
    # 跳过测试套件（仅构建）。
    [switch]$SkipTests,
    # 构建前清理 build/ 与 dist/。
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    $venvPython = Join-Path $RepoRoot (Join-Path $VenvDir "Scripts\python.exe")

    if ($Clean) {
        foreach ($dir in @("build", "dist")) {
            if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
        }
    }

    if (Test-Path $venvPython) {
        Write-Host "[1/5] 复用已有虚拟环境 $VenvDir"
    } else {
        Write-Host "[1/5] 创建虚拟环境 $VenvDir ..."
        & $PythonLauncher -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败（请确认已安装 Python 3.13）" }
    }

    Write-Host "[2/5] 安装依赖：.[gui] + pyinstaller ..."
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install ".[gui]" "pyinstaller>=6.0"
    if ($LASTEXITCODE -ne 0) { throw "安装依赖失败" }

    if ($SkipTests) {
        Write-Host "[3/5] 已按 -SkipTests 跳过测试套件"
    } else {
        Write-Host "[3/5] 运行测试套件（含 offscreen 界面测试）..."
        & $venvPython -m pip install pytest
        $env:QT_QPA_PLATFORM = "offscreen"
        & $venvPython -m pytest -q
        $testsExit = $LASTEXITCODE
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
        if ($testsExit -ne 0) { throw "测试未通过，已中止打包" }
    }

    Write-Host "[4/5] PyInstaller 构建 safecopy-gui.exe ..."
    & $venvPython -m PyInstaller "packaging\safecopy-gui.spec" --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

    Write-Host "[5/5] 校验产物 ..."
    $exePath = Join-Path $RepoRoot "dist\safecopy-gui.exe"
    if (-not (Test-Path $exePath)) { throw "构建失败：未找到 $exePath" }
    $sizeMb = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
    Write-Host ("构建完成：{0}（{1} MB）" -f $exePath, $sizeMb)
    Write-Host "提示：exe 未做代码签名，首次运行若被 SmartScreen 拦截，请选择「更多信息 → 仍要运行」；"
    Write-Host "      也可用 -SkipTests -Clean 参数做一次干净重建。"
}
finally {
    Pop-Location
}
