# ============================================================
# GenRouter — Thiet lap Windows 10 cho may moi
# Chay 1 LAN duy nhat TRUOC KHI chay c69-router.exe lan dau tren
# 1 may tinh moi. Sau khi setup xong, khong can chay lai (tru khi
# cai lai Windows).
# ============================================================

$ErrorActionPreference = 'Continue'
$script:results = @()
$script:needReboot = $false

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Action
    )
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

# ─── 0. Kiem tra quyen Administrator ───────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "LOI: Script nay PHAI chay voi quyen Administrator. Hay chay lai qua setup_windows.bat (tu dong xin quyen)." -ForegroundColor Red
    exit 1
}

Write-Host "============================================================" -ForegroundColor Green
Write-Host "  GenRouter - Thiet lap he thong Windows cho may moi" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green

# ─── 1. Bat IP Forwarding (chuyen tiep goi tin giua cac card mang) ───
Invoke-Step "Bat IP Forwarding (IPEnableRouter)" {
    $path = "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
    $current = (Get-ItemProperty -Path $path -Name "IPEnableRouter" -ErrorAction SilentlyContinue).IPEnableRouter
    if ($current -ne 1) {
        Set-ItemProperty -Path $path -Name "IPEnableRouter" -Value 1 -Type DWord
        Write-Host "Da bat IPEnableRouter=1 (truoc do: $current). LUU Y: can KHOI DONG LAI MAY de co hieu luc hoan toan."
    } else {
        Write-Host "OK: IPEnableRouter da bat san (=1)"
    }
}

# ─── 1.5. Kich hoat Hyper-V (NGUYEN NHAN GOC THAT SU cua loi "Invalid class") ──
# XAC NHAN TREN MAY THAT: khong phai BFE/mpssvc/WinNat nhu cac chan doan truoc - class
# WMI MSFT_NetNat (nen tang New-NetNat) phu thuoc nen tang Hyper-V. Truoc day phai
# huong dan khach tu vao Settings > Windows Features > tick Hyper-V, rat bat tien voi
# khach khong ranh ky thuat - tu dong hoa bang DISM/PowerShell thay vi bat lam tay.
Invoke-Step "Kich hoat Hyper-V (bat buoc de NetNat hoat dong)" {
    $osCaption = (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue).Caption
    $isHome = $osCaption -like "*Home*"

    $requiredFeatures = @("Microsoft-Hyper-V-All", "Microsoft-Hyper-V", "Microsoft-Hyper-V-Tools-All")
    $missingFeatures = @()

    if (-not $isHome) {
        foreach ($f in $requiredFeatures) {
            $feat = Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction SilentlyContinue
            if (-not $feat -or $feat.State -ne "Enabled") {
                $missingFeatures += $f
            }
        }
        if ($missingFeatures.Count -eq 0) {
            Write-Host "OK: Hyper-V va cac cong cu quan ly da duoc kich hoat san"
            return
        }
    } else {
        # Tren Home chi check Microsoft-Hyper-V
        $feat = Get-WindowsOptionalFeature -Online -FeatureName "Microsoft-Hyper-V" -ErrorAction SilentlyContinue
        if ($feat -and $feat.State -eq "Enabled") {
            Write-Host "OK: Hyper-V da duoc kich hoat san tren Windows Home"
            return
        }
    }

    if ($isHome) {
        # Windows Home khong ho tro Enable-WindowsOptionalFeature cho Hyper-V qua duong
        # thong thuong (nguon cai bi chan). THU THUAT KHONG CHINH THUC: cac file component
        # Hyper-V van co san tren dia (moi edition Windows dung chung 1 bo cai, chi khac
        # tinh nang duoc mo theo giay phep) - cai truc tiep qua DISM /add-package roi bat
        # tinh nang voi /LimitAccess de bo qua yeu cau nguon tu Windows Update. Rui ro thap
        # (chi cai lai dung file Windows co san), dung pho bien de bat Hyper-V/WSL2/Windows
        # Sandbox tren Home - chap nhan theo yeu cau vi da so may khach chay Windows Home.
        Write-Host "Windows HOME phat hien ($osCaption) - dang bat Hyper-V qua DISM package truc tiep (thu thuat khong chinh thuc, co the mat vai phut)..." -ForegroundColor Yellow
        $pkgDir = "$env:SystemRoot\servicing\Packages"
        $hvPackages = @(Get-ChildItem -Path $pkgDir -Filter "*Hyper-V*.mum" -ErrorAction SilentlyContinue)
        if ($hvPackages.Count -eq 0) {
            throw "Khong tim thay Hyper-V component packages trong $pkgDir - khong the tu bat Hyper-V tren may nay."
        }
        foreach ($pkg in $hvPackages) {
            & dism.exe /online /norestart /add-package:"$($pkg.FullName)" | Out-Null
        }
        # Kich hoat ca Hyper-V-All va Microsoft-Hyper-V de dam bao tat ca service va platform duoc bat
        & dism.exe /online /enable-feature /featurename:Microsoft-Hyper-V-All /LimitAccess /ALL /NoRestart | Out-Null
        & dism.exe /online /enable-feature /featurename:Microsoft-Hyper-V /LimitAccess /ALL /NoRestart | Out-Null
        $code = $LASTEXITCODE
        if ($code -eq 3010) {
            Write-Host "DA KICH HOAT Hyper-V THANH CONG tren Windows Home - CAN KHOI DONG LAI MAY truoc khi chay c69-router.exe." -ForegroundColor Yellow
            $script:needReboot = $true
        } elseif ($code -eq 0) {
            Write-Host "OK: Da kich hoat Hyper-V thanh cong tren Windows Home"
        } else {
            throw "DISM enable-feature that bai tren Windows Home, exit code $code"
        }
    } else {
        Write-Host "Dang kich hoat cac tinh nang Hyper-V con thieu ($($missingFeatures -join ', ')). Vui long doi..." -ForegroundColor Yellow
        foreach ($f in $missingFeatures) {
            Write-Host "Dang kich hoat $f..." -ForegroundColor Yellow
            $result = Enable-WindowsOptionalFeature -Online -FeatureName $f -All -NoRestart -ErrorAction Stop
            if ($result.RestartNeeded) {
                Write-Host "✓ Da kich hoat $f (can khoi dong lai may)" -ForegroundColor Yellow
                $script:needReboot = $true
            } else {
                Write-Host "✓ Da kich hoat $f thanh cong"
            }
        }
    }
}

# ─── 2. Dam bao dich vu WinNat dang chay ───────────────────────────
# QUAN TRONG: da xac nhan tren may that - class WMI MSFT_NetNat (nen tang cua
# New-NetNat/Get-NetNat) phu thuoc dich vu "WinNat" (Windows NAT Driver Service),
# KHONG PHAI BFE/mpssvc nhu chan doan ban dau. BFE va Windows Defender Firewall
# (mpssvc) la Protected Service - Windows khoa cung, Admin KHONG the Stop/Restart
# duoc ke ca da elevate ("Cannot open BFE service on computer '.'") - nen huong sua
# truoc day (restart BFE/mpssvc) chua bao gio co tac dung that su. WinNat la service
# binh thuong, Admin dieu khien duoc.
Invoke-Step "Dam bao dich vu WinNat dang chay (nen tang that cua NetNat)" {
    $svc = Get-Service -Name "WinNat" -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        Set-Service -Name "WinNat" -StartupType Automatic
        Start-Service -Name "WinNat"
        Write-Host "Da bat dich vu WinNat (truoc do: $($svc.Status))"
    } else {
        Write-Host "OK: dich vu WinNat dang chay"
    }
}

# ─── 3. Tat xung dot Internet Connection Sharing (ICS) ─────────────
# ICS (SharedAccess) dung co che NAT rieng, tung ghi nhan xung dot voi NetNat gay
# loi "Invalid class" tren mot so may. May nay dung lam GenRouter (tu quan ly NAT
# rieng qua NetNat) nen khong can ICS - tat luon thay vi chi canh bao, vi giu ca 2
# chay song song chinh la nguyen nhan xung dot can tranh.
Invoke-Step "Tat xung dot Internet Connection Sharing (ICS)" {
    $ics = Get-Service -Name "SharedAccess" -ErrorAction SilentlyContinue
    if ($ics -and $ics.Status -eq 'Running') {
        try {
            Stop-Service -Name "SharedAccess" -Force -ErrorAction Stop
            Set-Service -Name "SharedAccess" -StartupType Disabled -ErrorAction Stop
            Write-Host "Da tat va disable dich vu ICS (SharedAccess) - tranh xung dot voi NetNat cua GenRouter."
        } catch {
            # ICS (SharedAccess) noi tieng kho dung "song" tren nhieu may Windows (bao
            # "stop failed" du dang chay quyen Admin) - KHONG coi day la loi chan cung,
            # chi canh bao va van tiep tuc: dat StartupType=Disabled de lan boot sau
            # khong tu chay lai, con viec NetNat co that su hoat dong duoc hay khong da
            # co Buoc 4 (Test tao NAT) kiem tra xac nhan cuoi cung.
            Set-Service -Name "SharedAccess" -StartupType Disabled -ErrorAction SilentlyContinue
            Write-Host "CANH BAO: Khong the dung ICS ngay (loi thuong gap, khong phai do thieu quyen). Da dat Disabled de khong tu chay lan sau - Buoc 4 se xac nhan NAT co hoat dong duoc khong." -ForegroundColor Yellow
        }
    } else {
        Write-Host "OK: ICS khong chay, khong xung dot"
    }
}

# ─── 3.5. Chan doan truc tiep nguyen nhan "Invalid class" (khong doan mo nua) ──
# Loi lap lai giong het nhau du co/khong restart BFE/Firewall (ma 2 service nay thuc
# ra khong the restart duoc qua Stop/Restart-Service ke ca voi quyen Admin - Windows
# khoa cung de chong malware tat firewall) - nghia la BFE/Firewall KHONG PHAI nguyen
# nhan that. Kiem tra truc tiep xem class WMI MSFT_NetNat co ton tai tren may nay
# khong, va co Hyper-V dang chay khong (tung ghi nhan xung dot voi NetNat).
Invoke-Step "Chan doan class WMI NetNat + Hyper-V" {
    $natClass = Get-CimClass -Namespace root/StandardCimv2 -ClassName MSFT_NetNat -ErrorAction SilentlyContinue
    if ($natClass) {
        Write-Host "OK: Class MSFT_NetNat CO ton tai trong WMI tren may nay"
    } else {
        Write-Host "CANH BAO: Class MSFT_NetNat KHONG tim thay trong WMI - day co the la nguyen nhan goc cua loi 'Invalid class' (khong lien quan BFE/Firewall)" -ForegroundColor Red
    }

    $hyperv = Get-Service -Name vmms -ErrorAction SilentlyContinue
    if ($hyperv) {
        Write-Host "CANH BAO: Hyper-V dang duoc cai (dich vu vmms ton tai, Status=$($hyperv.Status)) - Hyper-V tung ghi nhan xung dot voi NetNat WMI provider" -ForegroundColor Yellow
        $switches = Get-VMSwitch -ErrorAction SilentlyContinue
        if ($switches) {
            Write-Host "  Virtual Switch dang co: $($switches.Name -join ', ')" -ForegroundColor Yellow
        }
    } else {
        Write-Host "OK: Hyper-V khong duoc cai tren may nay"
    }

    $wmiHealth = Get-CimInstance -Namespace root/cimv2 -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($wmiHealth) {
        Write-Host "OK: WMI tong the hoat dong binh thuong (Win32_OperatingSystem truy van duoc)"
    } else {
        Write-Host "CANH BAO: WMI co the dang bi loi tong the (khong truy van duoc Win32_OperatingSystem) - can 'winmgmt /salvagerepository'" -ForegroundColor Red
    }
}

# ─── 4. Test thu tao NAT ruleto de xac nhan NetNat hoat dong duoc ──
# QUAN TRONG: moi lan thu New-NetNat chay trong 1 TIEN TRINH powershell.exe HOAN TOAN
# MOI (khong goi lai cmdlet trong cung session vua Restart-Service) - da xac nhan thuc
# te tren may that: goi lai New-NetNat trong CUNG 1 session PowerShell ngay sau khi
# restart WinNat bi loi ".NET Collection was modified; enumeration operation may not
# execute" LAP LAI ON DINH (khong phai race condition tam thoi qua vai giay cho) -
# nguyen nhan la session CIM/WMI cu van con giu handle/cache toi NetNat provider TRUOC
# luc WinNat restart. Tien trinh PowerShell moi tao session CIM moi tinh, tranh loi nay.
Invoke-Step "Test tao NAT (xac nhan tinh nang NetNat hoat dong tren may nay)" {
    function Test-NatInNewProcess {
        $script = @'
$ErrorActionPreference = "SilentlyContinue"
$all = @(Get-NetNat -ErrorAction SilentlyContinue)
foreach ($n in $all) {
    if ($n.Name -eq "GenRouterNAT-Test") {
        Remove-NetNat -Name $n.Name -Confirm:$false -ErrorAction SilentlyContinue
    }
}
try {
    New-NetNat -Name "GenRouterNAT-Test" -InternalIPInterfaceAddressPrefix "192.168.250.0/24" -ErrorAction Stop | Out-Null
    Remove-NetNat -Name "GenRouterNAT-Test" -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Write-Output "OK"
} catch {
    Write-Output "ERROR: $($_.Exception.Message)"
}
'@
        $tmpFile = [System.IO.Path]::GetTempFileName() + ".ps1"
        Set-Content -Path $tmpFile -Value $script -Encoding UTF8
        try {
            return (powershell -NoProfile -ExecutionPolicy Bypass -File $tmpFile)
        } finally {
            Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue
        }
    }

    function Restart-NatServicesSafe {
        # Restart WinNat (service nen tang that cua NetNat) thay vi BFE/mpssvc - 2 service
        # do la Protected Service, Windows khong cho Admin Stop/Restart ke ca da elevate
        # ("Cannot open BFE service on computer '.'" - da xac nhan tren may that), nen
        # restart chung khong bao gio thanh cong. Van giu try/catch phong truong hop
        # WinNat cung nem loi enumerate tam thoi tren mot so may.
        try { Restart-Service -Name WinNat -Force -ErrorAction Stop } catch {
            Write-Host "  (Bo qua loi restart WinNat: $($_.Exception.Message))" -ForegroundColor DarkYellow
        }
    }

    $r = Test-NatInNewProcess
    if ($r -match "^OK") {
        Write-Host "OK: NetNat hoat dong binh thuong tren may nay"
    } else {
        Write-Host "Lan 1 that bai ($r) - dang restart dich vu roi thu lai bang tien trinh PowerShell moi..." -ForegroundColor Yellow
        Restart-NatServicesSafe
        Start-Sleep -Seconds 7
        $r2 = Test-NatInNewProcess
        if ($r2 -match "^OK") {
            Write-Host "OK: NetNat hoat dong binh thuong sau khi restart dich vu (tien trinh moi)"
        } else {
            Write-Host "Lan 2 van loi ($r2) - cho them va thu lan cuoi..." -ForegroundColor Yellow
            Start-Sleep -Seconds 9
            $r3 = Test-NatInNewProcess
            if ($r3 -match "^OK") {
                Write-Host "OK: NetNat hoat dong binh thuong sau khi cho them thoi gian on dinh"
            } else {
                throw "Van loi sau 3 lan thu (moi lan tien trinh PowerShell moi): $r3"
            }
        }
    }
}

# ─── 5. Mo Windows Firewall cho API (8000) + DHCP (67/68) ──────────
Invoke-Step "Mo Windows Firewall cho port API (8000) va DHCP (67/68)" {
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter API 8000' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'GenRouter API 8000' -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Enabled True -Profile Any | Out-Null
    }
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter DHCP 67' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'GenRouter DHCP 67' -Direction Inbound -Protocol UDP -LocalPort 67 -Action Allow -Enabled True -Profile Any | Out-Null
    }
    if (-not (Get-NetFirewallRule -DisplayName 'GenRouter DHCP 68' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'GenRouter DHCP 68' -Direction Inbound -Protocol UDP -LocalPort 68 -Action Allow -Enabled True -Profile Any | Out-Null
    }
    Write-Host "OK: Firewall rules da san sang"
}

# ─── 6. Tai sing-box.exe + wintun.dll neu chua co (cung thu muc voi script) ──
Invoke-Step "Tai sing-box.exe va wintun.dll (neu chua co san)" {
    $dir = $PSScriptRoot
    $singboxExe = Join-Path $dir "sing-box.exe"
    $wintunDll = Join-Path $dir "wintun.dll"

    if (-not (Test-Path $singboxExe)) {
        Write-Host "Dang tai sing-box.exe tu GitHub..."
        Invoke-WebRequest -Uri "https://github.com/SagerNet/sing-box/releases/download/v1.13.14/sing-box-1.13.14-windows-amd64.zip" -OutFile "$env:TEMP\singbox_dl.zip" -UseBasicParsing
        Expand-Archive -Path "$env:TEMP\singbox_dl.zip" -DestinationPath "$env:TEMP\singbox_dl_extract" -Force
        $found = Get-ChildItem -Path "$env:TEMP\singbox_dl_extract" -Recurse -Filter "sing-box.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { Copy-Item $found.FullName $singboxExe -Force }
        Remove-Item "$env:TEMP\singbox_dl.zip", "$env:TEMP\singbox_dl_extract" -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (-not (Test-Path $wintunDll)) {
        Write-Host "Dang tai wintun.dll..."
        Invoke-WebRequest -Uri "https://www.wintun.net/builds/wintun-0.14.1.zip" -OutFile "$env:TEMP\wintun_dl.zip" -UseBasicParsing
        Expand-Archive -Path "$env:TEMP\wintun_dl.zip" -DestinationPath "$env:TEMP\wintun_dl_extract" -Force
        $found = Get-ChildItem -Path "$env:TEMP\wintun_dl_extract" -Recurse -Filter "wintun.dll" -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like "*amd64*" } | Select-Object -First 1
        if ($found) { Copy-Item $found.FullName $wintunDll -Force }
        Remove-Item "$env:TEMP\wintun_dl.zip", "$env:TEMP\wintun_dl_extract" -Recurse -Force -ErrorAction SilentlyContinue
    }

    if ((Test-Path $singboxExe) -and (Test-Path $wintunDll)) {
        Write-Host "OK: sing-box.exe va wintun.dll da san sang tai $dir"
    } else {
        throw "Khong tai duoc day du binary (kiem tra ket noi internet)"
    }
}

# ─── Tom tat ────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  TOM TAT KET QUA THIET LAP" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
$results | Format-Table -AutoSize -Wrap

$hasError = $results | Where-Object { $_.KetQua -like "LOI:*" }
if ($script:needReboot) {
    Write-Host ""
    Write-Host "############################################################" -ForegroundColor Yellow
    Write-Host "  VUA KICH HOAT HYPER-V - BAT BUOC KHOI DONG LAI MAY NGAY" -ForegroundColor Yellow
    Write-Host "  truoc khi chay c69-router.exe, neu khong NAT se KHONG hoat dong." -ForegroundColor Yellow
    Write-Host "############################################################" -ForegroundColor Yellow
} elseif ($hasError) {
    Write-Host "Co buoc bi LOI o tren - can xu ly truoc khi chay c69-router.exe de tranh loi NAT/mang." -ForegroundColor Red
} else {
    Write-Host "Tat ca cac buoc OK. Neu vua bat IPEnableRouter lan dau, KHOI DONG LAI MAY roi moi chay c69-router.exe." -ForegroundColor Green
}
