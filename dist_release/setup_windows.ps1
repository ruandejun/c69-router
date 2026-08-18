# ============================================================
# C69-Router - Thiet lap Windows cho may moi
# Chay 1 LAN duy nhat TRUOC KHI chay c69-router.exe lan dau tren
# 1 may tinh moi. Sau khi setup xong, khong can chay lai.
# ============================================================

$ErrorActionPreference = 'Continue'
$script:results = @()
$script:needReboot = $false

function Invoke-Step {
    param([string]$Name, [scriptblock]$Action)
    Write-Host ""
    Write-Host "--- $Name ---" -ForegroundColor Cyan
    try {
        & $Action
        $script:results += [PSCustomObject]@{ Buoc = $Name; KetQua = "OK" }
    } catch {
        Write-Host "LOI: $($_.Exception.Message)" -ForegroundColor Red
        $script:results += [PSCustomObject]@{ Buoc = $Name; KetQua = "LOI: $($_.Exception.Message)" }
    }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "LOI: Script nay PHAI chay voi quyen Administrator." -ForegroundColor Red
    exit 1
}

Write-Host "============================================================" -ForegroundColor Green
Write-Host "  C69-Router - Thiet lap he thong Windows" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green

# 1. Bat IP Forwarding
Invoke-Step "Bat IP Forwarding (IPEnableRouter)" {
    $path = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
    $current = (Get-ItemProperty -Path $path -Name "IPEnableRouter" -ErrorAction SilentlyContinue).IPEnableRouter
    if ($current -ne 1) {
        Set-ItemProperty -Path $path -Name "IPEnableRouter" -Value 1 -Type DWord
        Write-Host "Da bat IPEnableRouter=1. Can KHOI DONG LAI MAY de co hieu luc."
    } else {
        Write-Host "OK: IPEnableRouter da bat san (=1)"
    }
}

# 2. Kich hoat Hyper-V (bat buoc de NetNat hoat dong)
Invoke-Step "Kich hoat Hyper-V (bat buoc de NetNat hoat dong)" {
    $osCaption = (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue).Caption
    $isHome = $osCaption -like "*Home*"
    $requiredFeatures = @("Microsoft-Hyper-V-All", "Microsoft-Hyper-V", "Microsoft-Hyper-V-Tools-All")
    $missingFeatures = @()
    if (-not $isHome) {
        foreach ($f in $requiredFeatures) {
            $feat = Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction SilentlyContinue
            if (-not $feat -or $feat.State -ne "Enabled") { $missingFeatures += $f }
        }
        if ($missingFeatures.Count -eq 0) { Write-Host "OK: Hyper-V da duoc kich hoat san"; return }
    } else {
        $feat = Get-WindowsOptionalFeature -Online -FeatureName "Microsoft-Hyper-V" -ErrorAction SilentlyContinue
        if ($feat -and $feat.State -eq "Enabled") { Write-Host "OK: Hyper-V da duoc kich hoat san tren Windows Home"; return }
    }
    if ($isHome) {
        Write-Host "Windows Home - dang bat Hyper-V qua DISM..." -ForegroundColor Yellow
        $pkgDir = "$env:SystemRoot\servicing\Packages"
        $hvPackages = @(Get-ChildItem -Path $pkgDir -Filter "*Hyper-V*.mum" -ErrorAction SilentlyContinue)
        if ($hvPackages.Count -eq 0) { throw "Khong tim thay Hyper-V packages trong $pkgDir" }
        foreach ($pkg in $hvPackages) { & dism.exe /online /norestart /add-package:"$($pkg.FullName)" | Out-Null }
        & dism.exe /online /enable-feature /featurename:Microsoft-Hyper-V-All /LimitAccess /ALL /NoRestart | Out-Null
        & dism.exe /online /enable-feature /featurename:Microsoft-Hyper-V /LimitAccess /ALL /NoRestart | Out-Null
        $code = $LASTEXITCODE
        if ($code -eq 3010 -or $code -eq 0) {
            Write-Host "Da kich hoat Hyper-V - CAN KHOI DONG LAI MAY." -ForegroundColor Yellow
            $script:needReboot = $true
        } else { throw "DISM that bai exit code $code" }
    } else {
        foreach ($f in $missingFeatures) {
            $result = Enable-WindowsOptionalFeature -Online -FeatureName $f -All -NoRestart -ErrorAction Stop
            if ($result.RestartNeeded) { $script:needReboot = $true }
            Write-Host "Da kich hoat $f"
        }
    }
}

# 3. Dam bao dich vu WinNat chay
Invoke-Step "Dam bao dich vu WinNat dang chay" {
    $svc = Get-Service -Name "WinNat" -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        Set-Service -Name "WinNat" -StartupType Automatic
        Start-Service -Name "WinNat"
        Write-Host "Da bat WinNat"
    } else { Write-Host "OK: WinNat dang chay" }
}

# 4. Tat ICS tranh xung dot
Invoke-Step "Tat Internet Connection Sharing (ICS)" {
    $ics = Get-Service -Name "SharedAccess" -ErrorAction SilentlyContinue
    if ($ics -and $ics.Status -eq 'Running') {
        try {
            Stop-Service -Name "SharedAccess" -Force -ErrorAction Stop
            Set-Service -Name "SharedAccess" -StartupType Disabled -ErrorAction Stop
            Write-Host "Da tat ICS"
        } catch {
            Set-Service -Name "SharedAccess" -StartupType Disabled -ErrorAction SilentlyContinue
            Write-Host "CANH BAO: Khong the dung ICS ngay (da dat Disabled cho lan sau)" -ForegroundColor Yellow
        }
    } else { Write-Host "OK: ICS khong chay" }
}

# 5. Test tao NAT
Invoke-Step "Test tao NAT (xac nhan NetNat hoat dong)" {
    function Test-NatInNewProcess {
        $script = @'
$ErrorActionPreference = "SilentlyContinue"
$all = @(Get-NetNat -ErrorAction SilentlyContinue)
foreach ($n in $all) { if ($n.Name -eq "C69TestNAT") { Remove-NetNat -Name $n.Name -Confirm:$false -ErrorAction SilentlyContinue } }
try {
    New-NetNat -Name "C69TestNAT" -InternalIPInterfaceAddressPrefix "192.168.250.0/24" -ErrorAction Stop | Out-Null
    Remove-NetNat -Name "C69TestNAT" -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Write-Output "OK"
} catch { Write-Output "ERROR: $($_.Exception.Message)" }
'@
        $tmp = [System.IO.Path]::GetTempFileName() + ".ps1"
        Set-Content -Path $tmp -Value $script -Encoding UTF8
        try { return (powershell -NoProfile -ExecutionPolicy Bypass -File $tmp) }
        finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }
    $r = Test-NatInNewProcess
    if ($r -match "^OK") {
        Write-Host "OK: NetNat hoat dong binh thuong"
    } else {
        try { Restart-Service -Name WinNat -Force -ErrorAction Stop } catch {}
        Start-Sleep -Seconds 7
        $r2 = Test-NatInNewProcess
        if ($r2 -match "^OK") { Write-Host "OK: NetNat hoat dong sau khi restart WinNat" }
        else { throw "NAT test that bai: $r2" }
    }
}

# 6. Mo Windows Firewall
Invoke-Step "Mo Windows Firewall cho port API (9000-9099) va DHCP (67/68)" {
    if (-not (Get-NetFirewallRule -DisplayName 'C69Router API 9000' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'C69Router API 9000' -Direction Inbound -Protocol TCP -LocalPort 9000-9099 -Action Allow -Enabled True -Profile Any | Out-Null
    }
    if (-not (Get-NetFirewallRule -DisplayName 'C69Router DHCP 67' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'C69Router DHCP 67' -Direction Inbound -Protocol UDP -LocalPort 67 -Action Allow -Enabled True -Profile Any | Out-Null
    }
    if (-not (Get-NetFirewallRule -DisplayName 'C69Router DHCP 68' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'C69Router DHCP 68' -Direction Inbound -Protocol UDP -LocalPort 68 -Action Allow -Enabled True -Profile Any | Out-Null
    }
    Write-Host "OK: Firewall rules san sang"
}

# 7. Tai sing-box.exe + wintun.dll neu chua co
Invoke-Step "Tai sing-box.exe va wintun.dll (neu chua co)" {
    $dir = $PSScriptRoot
    $singboxExe = Join-Path $dir "sing-box.exe"
    $wintunDll  = Join-Path $dir "wintun.dll"
    if (-not (Test-Path $singboxExe)) {
        Write-Host "Dang tai sing-box.exe..."
        Invoke-WebRequest -Uri "https://github.com/SagerNet/sing-box/releases/download/v1.13.14/sing-box-1.13.14-windows-amd64.zip" -OutFile "$env:TEMP\singbox_dl.zip" -UseBasicParsing
        Expand-Archive -Path "$env:TEMP\singbox_dl.zip" -DestinationPath "$env:TEMP\singbox_extract" -Force
        $found = Get-ChildItem -Path "$env:TEMP\singbox_extract" -Recurse -Filter "sing-box.exe" | Select-Object -First 1
        if ($found) { Copy-Item $found.FullName $singboxExe -Force }
        Remove-Item "$env:TEMP\singbox_dl.zip","$env:TEMP\singbox_extract" -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path $wintunDll)) {
        Write-Host "Dang tai wintun.dll..."
        Invoke-WebRequest -Uri "https://www.wintun.net/builds/wintun-0.14.1.zip" -OutFile "$env:TEMP\wintun_dl.zip" -UseBasicParsing
        Expand-Archive -Path "$env:TEMP\wintun_dl.zip" -DestinationPath "$env:TEMP\wintun_extract" -Force
        $found = Get-ChildItem -Path "$env:TEMP\wintun_extract" -Recurse -Filter "wintun.dll" | Where-Object { $_.FullName -like "*amd64*" } | Select-Object -First 1
        if ($found) { Copy-Item $found.FullName $wintunDll -Force }
        Remove-Item "$env:TEMP\wintun_dl.zip","$env:TEMP\wintun_extract" -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ((Test-Path $singboxExe) -and (Test-Path $wintunDll)) {
        Write-Host "OK: sing-box.exe va wintun.dll san sang tai $dir"
    } else { throw "Khong tai duoc binary (kiem tra ket noi internet)" }
}

# Tom tat
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  TOM TAT KET QUA THIET LAP" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
$script:results | Format-Table -AutoSize -Wrap
if ($script:needReboot) {
    Write-Host ""
    Write-Host "###################################################" -ForegroundColor Yellow
    Write-Host "  PHAI KHOI DONG LAI MAY TRUOC KHI CHAY C69ROUTER" -ForegroundColor Yellow
    Write-Host "###################################################" -ForegroundColor Yellow
} else {
    Write-Host "Tat ca OK. Co the chay c69-router.exe ngay bay gio." -ForegroundColor Green
}
