# Fixed script run by companion/excelops.js. Nothing in it comes from the server: the workbook path, the output path and the
# pid file are passed as ARGUMENTS. It opens a temporary copy of a workbook in a private, hidden Excel with macros force-disabled,
# calculates everything, and writes the formula cells' values as JSON. It never saves the workbook.
param(
  [Parameter(Mandatory = $true)][string]$Path,
  [Parameter(Mandatory = $true)][string]$Out,
  [Parameter(Mandatory = $true)][string]$PidFile,
  [int]$MaxCells = 100000
)
$ErrorActionPreference = 'Stop'

function Col([int]$n) {
  $s = ''
  while ($n -gt 0) { $m = ($n - 1) % 26; $s = [char](65 + $m) + $s; $n = [int][math]::Floor(($n - $m) / 26) }
  return $s
}

# Excel stores errors in Value2 as these Int32 codes
$errors = @{
  -2146826281 = '#DIV/0!'; -2146826246 = '#N/A'; -2146826259 = '#NAME?'; -2146826288 = '#NULL!'
  -2146826252 = '#NUM!'; -2146826265 = '#REF!'; -2146826273 = '#VALUE!'; -2146826236 = '#SPILL!'; -2146826235 = '#CALC!'
}

$before = @(Get-Process -Name EXCEL -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$xl = $null
$wb = $null
try {
  $xl = New-Object -ComObject Excel.Application
  $after = @(Get-Process -Name EXCEL -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
  $mine = $after | Where-Object { $before -notcontains $_ } | Select-Object -First 1
  if ($mine) { Set-Content -Path $PidFile -Value $mine -Encoding ascii }   # so the companion can end THIS Excel on a timeout
  $xl.Visible = $false
  $xl.DisplayAlerts = $false
  $xl.EnableEvents = $false
  $xl.AskToUpdateLinks = $false
  $xl.AutomationSecurity = 3          # msoAutomationSecurityForceDisable: no macros run
  $wb = $xl.Workbooks.Open($Path, 0, $true)     # UpdateLinks = 0 (never), ReadOnly = true
  $xl.CalculateFullRebuild()

  $cells = [ordered]@{}
  $count = 0
  $errorCount = 0
  foreach ($ws in $wb.Worksheets) {
    $used = $ws.UsedRange
    $row0 = [int]$used.Row
    $col0 = [int]$used.Column
    $values = $used.Value2
    $formulas = $used.Formula
    if ($values -isnot [array]) {                      # a one-cell sheet comes back as a scalar
      $values = [object[,]]::new(1, 1); $values[0, 0] = $used.Value2
      $formulas = [object[,]]::new(1, 1); $formulas[0, 0] = $used.Formula
      $lo = 0
    } else {
      $lo = $values.GetLowerBound(0)
    }
    $rows = $values.GetLength(0)
    $cols = $values.GetLength(1)
    for ($r = 0; $r -lt $rows; $r++) {
      for ($c = 0; $c -lt $cols; $c++) {
        $f = $formulas[($lo + $r), ($lo + $c)]
        if (-not ($f -is [string]) -or -not $f.StartsWith('=')) { continue }
        $count++
        if ($count -gt $MaxCells) { throw "more than $MaxCells formula cells" }
        $v = $values[($lo + $r), ($lo + $c)]
        $key = $ws.Name + '!' + (Col ($col0 + $c)) + ($row0 + $r)
        if ($v -is [int] -and $errors.ContainsKey($v)) { $cells[$key] = $errors[$v]; $errorCount++ }
        elseif ($null -eq $v) { $cells[$key] = $null }
        else { $cells[$key] = $v }
      }
    }
  }
  $result = [ordered]@{ ok = $true; formulas = $count; errors = $errorCount; version = [string]$xl.Version; cells = $cells }
  [System.IO.File]::WriteAllText($Out, ($result | ConvertTo-Json -Depth 4 -Compress), (New-Object System.Text.UTF8Encoding($false)))
}
catch {
  $msg = ($_.Exception.Message -replace '[\r\n]+', ' ')
  [System.IO.File]::WriteAllText($Out, (@{ ok = $false; error = $msg } | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
  exit 2
}
finally {
  if ($wb) { try { $wb.Close($false) } catch {} ; [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($wb) }
  if ($xl) { try { $xl.Quit() } catch {} ; [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($xl) }
  [GC]::Collect(); [GC]::WaitForPendingFinalizers()
}
