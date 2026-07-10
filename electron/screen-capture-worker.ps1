$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;

public static class RandomAskDpi {
  [DllImport("user32.dll")]
  public static extern bool SetProcessDpiAwarenessContext(IntPtr value);

  [DllImport("user32.dll")]
  public static extern bool SetProcessDPIAware();

  [DllImport("user32.dll")]
  public static extern IntPtr GetForegroundWindow();

  [DllImport("user32.dll")]
  public static extern IntPtr GetAncestor(IntPtr window, uint flags);

  [DllImport("user32.dll", SetLastError = true)]
  public static extern bool GetWindowDisplayAffinity(IntPtr window, out uint affinity);

  public static IntPtr GetForegroundRootOwner() {
    IntPtr foreground = GetForegroundWindow();
    if (foreground == IntPtr.Zero) return foreground;
    IntPtr rootOwner = GetAncestor(foreground, 3); // GA_ROOTOWNER
    return rootOwner == IntPtr.Zero ? foreground : rootOwner;
  }
}
'@
$perMonitorAware = $false
try { $perMonitorAware = [RandomAskDpi]::SetProcessDpiAwarenessContext([IntPtr](-4)) } catch { $perMonitorAware = $false }
if (-not $perMonitorAware) { [void][RandomAskDpi]::SetProcessDPIAware() }

[Console]::Out.WriteLine('{"type":"ready"}')
[Console]::Out.Flush()

while (($line = [Console]::In.ReadLine()) -ne $null) {
  if ([string]::IsNullOrWhiteSpace($line)) { continue }
  $bitmap = $null
  $graphics = $null
  $stream = $null
  $request = $null
  try {
    $request = $line | ConvertFrom-Json
    if ($request.type -eq 'quit') { break }
    if ($request.type -eq 'affinity') {
      [uint32]$affinity = 0
      $window = [IntPtr]([Int64]([string]$request.windowHandle))
      $affinityOk = [RandomAskDpi]::GetWindowDisplayAffinity($window, [ref]$affinity)
      $response = [ordered]@{
        type = 'affinity'
        id = [string]$request.id
        ok = $affinityOk
        affinity = [uint32]$affinity
        error = if ($affinityOk) { '' } else { "GetWindowDisplayAffinity failed: $([Runtime.InteropServices.Marshal]::GetLastWin32Error())" }
      }
      [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress))
      [Console]::Out.Flush()
      continue
    }
    $width = [Math]::Max(1, [int]$request.width)
    $height = [Math]::Max(1, [int]$request.height)
    if ($width -gt 10000 -or $height -gt 10000) { throw 'Capture bounds are too large' }

    $isProbe = $request.type -eq 'probe'
    $margin = if ($isProbe) { [Math]::Max(0, [int]$request.margin) } else { 0 }
    $captureX = [int]$request.x - $margin
    $captureY = [int]$request.y - $margin
    $captureWidth = $width + ($margin * 2)
    $captureHeight = $height + ($margin * 2)

    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $bitmap = New-Object System.Drawing.Bitmap($captureWidth, $captureHeight, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $size = New-Object System.Drawing.Size($captureWidth, $captureHeight)
    $operation = [System.Drawing.CopyPixelOperation]::SourceCopy
    $graphics.CopyFromScreen($captureX, $captureY, 0, 0, $size, $operation)

    $lumaTotal = 0.0
    $lumaSamples = 0
    if ($isProbe) {
      $topY = [Math]::Max(1, [Math]::Floor($margin / 2))
      $bottomY = [Math]::Min($captureHeight - 2, $captureHeight - $topY - 1)
      for ($index = 0; $index -lt 12; $index += 1) {
        $sampleX = [Math]::Min($captureWidth - 2, [Math]::Max(1, [Math]::Round($margin + (($index + 0.5) * $width / 12.0))))
        foreach ($sampleY in @($topY, $bottomY)) {
          $pixel = $bitmap.GetPixel([int]$sampleX, [int]$sampleY)
          $lumaTotal += (0.2126 * $pixel.R) + (0.7152 * $pixel.G) + (0.0722 * $pixel.B)
          $lumaSamples += 1
        }
      }
      $leftX = $topY
      $rightX = [Math]::Min($captureWidth - 2, $captureWidth - $leftX - 1)
      for ($index = 0; $index -lt 5; $index += 1) {
        $sampleY = [Math]::Min($captureHeight - 2, [Math]::Max(1, [Math]::Round($margin + (($index + 0.5) * $height / 5.0))))
        foreach ($sampleX in @($leftX, $rightX)) {
          $pixel = $bitmap.GetPixel([int]$sampleX, [int]$sampleY)
          $lumaTotal += (0.2126 * $pixel.R) + (0.7152 * $pixel.G) + (0.0722 * $pixel.B)
          $lumaSamples += 1
        }
      }
    }
    else {
      $stepX = [Math]::Max(1, [Math]::Floor($width / 8))
      $stepY = [Math]::Max(1, [Math]::Floor($height / 5))
      for ($sampleY = [Math]::Floor($stepY / 2); $sampleY -lt $height; $sampleY += $stepY) {
        for ($sampleX = [Math]::Floor($stepX / 2); $sampleX -lt $width; $sampleX += $stepX) {
          $pixel = $bitmap.GetPixel($sampleX, $sampleY)
          $lumaTotal += (0.2126 * $pixel.R) + (0.7152 * $pixel.G) + (0.0722 * $pixel.B)
          $lumaSamples += 1
        }
      }
    }

    $luma = if ($lumaSamples -gt 0) { [Math]::Round($lumaTotal / ($lumaSamples * 255.0), 3) } else { 0.5 }
    if ($isProbe) {
      $watch.Stop()
      $response = [ordered]@{
        type = 'probe'
        id = [string]$request.id
        ok = $true
        luma = $luma
        foreground = [string]([RandomAskDpi]::GetForegroundRootOwner().ToInt64())
        captureMs = $watch.ElapsedMilliseconds
      }
      [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress))
      [Console]::Out.Flush()
      continue
    }

    $stream = New-Object System.IO.MemoryStream
    $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $encoded = [Convert]::ToBase64String($stream.ToArray())
    $watch.Stop()
    $response = [ordered]@{
      type = 'capture'
      id = [string]$request.id
      ok = $true
      data = $encoded
      luma = $luma
      captureMs = $watch.ElapsedMilliseconds
    }
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
  }
  catch {
    $response = [ordered]@{
      type = if ($request -and $request.type) { [string]$request.type } else { 'capture' }
      id = if ($request) { [string]$request.id } else { '' }
      ok = $false
      error = $_.Exception.Message
    }
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
  }
  finally {
    if ($graphics) { $graphics.Dispose() }
    if ($bitmap) { $bitmap.Dispose() }
    if ($stream) { $stream.Dispose() }
  }
}
