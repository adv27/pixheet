<#
.SYNOPSIS
Open every .xlsx in a folder with Excel (COM automation) and check it against its source image.

.DESCRIPTION
For each workbook this reports the used range, the row height and the column width, and
compares about -Samples random cell colors with the fitted source image (the image with the
same file name stem in -Source, fitted by scripts/sample_pixels.py with the same options).

Excel runs hidden with alerts off. In that mode Excel refuses to open a file that it would
otherwise offer to repair, so "opened" also means "no repair prompt".

With -Snapshot DIR, Excel also exports each sheet to DIR as a one-page PDF, so you can see the
picture without opening each file. Excel prints very narrow columns wider than they are on
screen, so the PDF is stretched sideways (about 1.2x); on screen every cell is 0.75 x 0.75 pt,
which the report line shows.

.EXAMPLE
uv run pixheet samples\*.jpg
powershell -ExecutionPolicy Bypass -File scripts\excel_check.ps1 -Folder samples
#>
param(
    [Parameter(Mandatory = $true)] [string] $Folder,
    [string] $Source = $Folder,
    [int] $Samples = 80,
    [int] $MaxSide = 1000,
    [string] $Background = "white",
    [string] $Snapshot = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$helper = Join-Path $PSScriptRoot "sample_pixels.py"
$files = @(Get-ChildItem -Path $Folder -Filter *.xlsx -File | Sort-Object Name)
if ($files.Count -eq 0) { Write-Error "No .xlsx files in $Folder" }
if ($Snapshot) { New-Item -ItemType Directory -Force -Path $Snapshot | Out-Null }

function Get-Rgb([double] $color) {
    # Excel colors are 0xBBGGRR.
    $c = [int64] $color
    "{0:X2}{1:X2}{2:X2}" -f ($c -band 0xFF), (($c -shr 8) -band 0xFF), (($c -shr 16) -band 0xFF)
}

$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
$excel.ScreenUpdating = $false
$failed = 0
try {
    foreach ($file in $files) {
        $wb = $null
        $problems = @()
        try {
            try {
                $wb = $excel.Workbooks.Open($file.FullName, 0, $true)
            } catch {
                throw "Excel could not open it (it would need repair): $($_.Exception.Message)"
            }
            $ws = $wb.Worksheets.Item(1)
            $used = $ws.UsedRange
            $rows = $used.Rows.Count
            $cols = $used.Columns.Count
            $line = "{0}: used {1} ({2}x{3}), row height {4} pt, column width {5} ({6} pt)" -f `
                $file.Name, $used.Address($false, $false), $cols, $rows, `
                $ws.Rows.Item(1).RowHeight, $ws.Columns.Item(1).ColumnWidth, $ws.Columns.Item(1).Width

            $src = Get-ChildItem -Path $Source -File |
                Where-Object { $_.BaseName -eq $file.BaseName -and $_.Extension -ne ".xlsx" } |
                Select-Object -First 1
            if ($src) {
                $json = & uv run --project $repo --quiet python $helper $src.FullName `
                    --max-side $MaxSide --background $Background --count $Samples
                if ($LASTEXITCODE -ne 0) { throw "sample_pixels.py failed for $($src.Name)" }
                $expected = $json | ConvertFrom-Json
                if ($expected.width -ne $cols -or $expected.height -ne $rows) {
                    $problems += "size {0}x{1}, expected {2}x{3}" -f $cols, $rows, $expected.width, $expected.height
                }
                $mismatches = 0
                foreach ($s in $expected.samples) {
                    $got = Get-Rgb $ws.Cells.Item($s[0], $s[1]).Interior.Color
                    if ($got -ne $s[2]) {
                        $mismatches++
                        if ($mismatches -le 3) { $problems += "R{0}C{1} is {2}, expected {3}" -f $s[0], $s[1], $got, $s[2] }
                    }
                }
                $line += ", {0}/{1} colors match {2}" -f ($expected.samples.Count - $mismatches), $expected.samples.Count, $src.Name
                if ($mismatches) { $problems += "$mismatches color mismatches" }
            } else {
                $line += ", no source image found"
            }

            if ($Snapshot) {
                # Hidden Excel renders nothing to the screen, but printing (PDF export) works.
                $pdf = Join-Path (Resolve-Path $Snapshot) ($file.BaseName + ".pdf")
                $setup = $ws.PageSetup
                $setup.Zoom = $false
                $setup.FitToPagesWide = 1
                $setup.FitToPagesTall = 1
                $setup.LeftMargin = 0; $setup.RightMargin = 0; $setup.TopMargin = 0; $setup.BottomMargin = 0
                $ws.ExportAsFixedFormat(0, $pdf)  # xlTypePDF
                $line += ", snapshot $pdf"
            }
            Write-Host $line
        } catch {
            $problems += $_.Exception.Message
        } finally {
            if ($wb) {
                $wb.Close($false)
                [void][Runtime.InteropServices.Marshal]::ReleaseComObject($wb)
            }
        }
        if ($problems.Count) {
            $failed++
            Write-Host ("FAIL {0}: {1}" -f $file.Name, ($problems -join "; ")) -ForegroundColor Red
        }
    }
} finally {
    $excel.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($excel)
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
Write-Host ("{0} file(s) checked, {1} failed" -f $files.Count, $failed)
exit [int]($failed -gt 0)
