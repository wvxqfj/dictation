# Установка и обновление «Диктовки». Запуск (PowerShell):
#   irm https://raw.githubusercontent.com/wvxqfj/dictation/main/install.ps1 | iex
# Права администратора не нужны: всё ставится в %LOCALAPPDATA%\Dictation.
# Повторный запуск обновляет программу, настройки и статистика сохраняются.
& {
    $ProgressPreference = 'SilentlyContinue'   # иначе Invoke-WebRequest в PowerShell 5.1 качает в разы медленнее
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $Repo = 'wvxqfj/dictation'
    $PyVersion = '3.13.16'
    $Root = Join-Path $env:LOCALAPPDATA 'Dictation'
    $App = Join-Path $Root 'app'
    $Venv = Join-Path $Root 'venv'

    function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

    # Python x64 (на ARM-ноутбуках тоже: не для всех библиотек есть сборки под ARM64) с tkinter, 3.11+
    function Find-Python {
        $paths = @()
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $paths += (py -0p 2>$null) | ForEach-Object { if ($_ -match '([A-Za-z]:\\.+?python\.exe)') { $Matches[1] } }
        }
        $paths += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
        foreach ($p in $paths | Select-Object -Unique) {
            if (-not (Test-Path $p)) { continue }
            $out = & $p -c 'import sys, sysconfig, tkinter; print(sysconfig.get_platform(), sys.version_info >= (3, 11))' 2>$null
            if ($LASTEXITCODE -eq 0 -and "$out" -eq 'win-amd64 True') { return $p }
        }
    }

    function Invoke-Checked($exe, [string[]]$arguments, $what) {
        & $exe @arguments
        if ($LASTEXITCODE -ne 0) { throw "$what (код $LASTEXITCODE)" }
    }

    try {
        Step 'Python'
        $py = Find-Python
        if ($py) {
            Write-Host "Найден: $py"
        } else {
            Write-Host "Скачиваю Python $PyVersion (~30 МБ)…"
            $setup = Join-Path $env:TEMP "python-$PyVersion-amd64.exe"
            Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe" `
                -OutFile $setup -UseBasicParsing -ErrorAction Stop
            Write-Host 'Устанавливаю…'
            $proc = Start-Process $setup -Wait -PassThru -ArgumentList '/quiet', 'InstallAllUsers=0', `
                'Include_launcher=0', 'Include_test=0', 'PrependPath=0', 'Shortcuts=0'
            Remove-Item $setup -ErrorAction SilentlyContinue
            if ($proc.ExitCode -ne 0) { throw "Установщик Python завершился с кодом $($proc.ExitCode)" }
            $py = Find-Python
            if (-not $py) { throw 'Python установлен, но не найден' }
        }

        Step 'Останавливаю запущенную Диктовку'
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
            Where-Object { $_.CommandLine -like '*dictation.py*' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

        Step 'Скачиваю программу'
        $zip = Join-Path $env:TEMP 'dictation-main.zip'
        $unpacked = Join-Path $env:TEMP 'dictation-main-src'
        Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing -ErrorAction Stop
        Remove-Item $unpacked -Recurse -Force -ErrorAction SilentlyContinue
        Expand-Archive $zip $unpacked -Force -ErrorAction Stop
        $src = Get-ChildItem $unpacked -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force $App | Out-Null
        Copy-Item (Join-Path $src.FullName '*') $App -Recurse -Force -ErrorAction Stop
        Remove-Item $zip, $unpacked -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Папка программы: $App"

        Step 'Устанавливаю библиотеки (несколько минут)'
        $vpy = Join-Path $Venv 'Scripts\python.exe'
        if (-not (Test-Path $vpy)) { Invoke-Checked $py @('-m', 'venv', $Venv) 'Не удалось создать окружение Python' }
        Invoke-Checked $vpy @('-m', 'pip', 'install', '--upgrade', 'pip', '--disable-pip-version-check', '-q') 'pip'
        Invoke-Checked $vpy @('-m', 'pip', 'install', '-r', (Join-Path $App 'requirements.txt'),
            '--disable-pip-version-check') 'Не удалось установить библиотеки'

        Step 'Ярлык и автозапуск'
        $pyw = Join-Path $Venv 'Scripts\pythonw.exe'
        $main = Join-Path $App 'dictation.py'
        $lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'Диктовка.lnk'
        $sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
        $sc.TargetPath = $pyw
        $sc.Arguments = "`"$main`""
        $sc.WorkingDirectory = $App
        $sc.IconLocation = Join-Path $App 'icon.ico'
        $sc.Description = 'Голосовая диктовка'
        $sc.Save()
        Invoke-Checked $vpy @($main, '--install-autostart') 'Не удалось включить автозапуск'

        Step 'Запускаю'
        Start-Process $pyw -ArgumentList "`"$main`"" -WorkingDirectory $App

        Write-Host ''
        Write-Host 'Готово! Диктовка запущена — значок микрофона в трее (у часов).' -ForegroundColor Green
        Write-Host 'Держите клавишу Copilot и говорите. Нет такой клавиши — щёлкните по значку в трее,'
        Write-Host 'откройте «Настройки» и выберите другую клавишу.'
        Write-Host 'При первом запуске скачаются модели распознавания и перевода (~500 МБ), это пара минут.'
        Write-Host 'Обновить программу — снова выполнить ту же команду установки.'
    } catch {
        Write-Host "`nОшибка установки: $_" -ForegroundColor Red
    }
}
