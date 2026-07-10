$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;

public static class RandomAskInput {
  [StructLayout(LayoutKind.Sequential)]
  public struct POINT { public int X; public int Y; }

  [DllImport("user32.dll")]
  public static extern short GetAsyncKeyState(int vKey);

  [DllImport("user32.dll")]
  public static extern bool GetCursorPos(out POINT point);

  [DllImport("user32.dll")]
  public static extern uint GetClipboardSequenceNumber();
}
'@

$startedAt = [DateTime]::UtcNow
$mouseWasDown = $false
$dragStart = New-Object RandomAskInput+POINT
$dragEnd = New-Object RandomAskInput+POINT

while (([DateTime]::UtcNow - $startedAt).TotalSeconds -lt 15) {
  if (([RandomAskInput]::GetAsyncKeyState(0x1B) -band 0x8000) -ne 0) {
    Write-Output 'CANCEL'
    exit 2
  }

  $isDown = ([RandomAskInput]::GetAsyncKeyState(0x01) -band 0x8000) -ne 0
  if ($isDown -and -not $mouseWasDown) {
    [void][RandomAskInput]::GetCursorPos([ref]$dragStart)
    $mouseWasDown = $true
  }
  elseif (-not $isDown -and $mouseWasDown) {
    [void][RandomAskInput]::GetCursorPos([ref]$dragEnd)
    $distance = [Math]::Abs($dragEnd.X - $dragStart.X) + [Math]::Abs($dragEnd.Y - $dragStart.Y)
    if ($distance -ge 6) {
      $before = [RandomAskInput]::GetClipboardSequenceNumber()
      Start-Sleep -Milliseconds 100
      [System.Windows.Forms.SendKeys]::SendWait('^c')

      $copyStarted = [DateTime]::UtcNow
      while (([DateTime]::UtcNow - $copyStarted).TotalMilliseconds -lt 800) {
        Start-Sleep -Milliseconds 40
        $after = [RandomAskInput]::GetClipboardSequenceNumber()
        if ($after -ne $before) {
          Write-Output "OK:$after"
          exit 0
        }
      }
      Write-Output 'NO_TEXT'
      exit 3
    }
    $mouseWasDown = $false
  }

  Start-Sleep -Milliseconds 14
}

Write-Output 'TIMEOUT'
exit 4
