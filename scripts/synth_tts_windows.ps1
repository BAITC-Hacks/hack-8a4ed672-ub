# Synthetic speech via local Windows SAPI voices (offline). Output is ALWAYS labelled synthetic.
# Usage: powershell -File scripts/synth_tts_windows.ps1 -TextFile in.txt -Out out.wav [-Voice "Microsoft Irina Desktop"] [-Rate 0]
param(
  [Parameter(Mandatory = $true)][string]$TextFile,
  [Parameter(Mandatory = $true)][string]$Out,
  [string]$Voice = "Microsoft Irina Desktop",
  [int]$Rate = 0
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice($Voice)
$synth.Rate = $Rate
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth.SetOutputToWaveFile($Out, $format)
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$synth.Speak($text)
$synth.SetOutputToNull()
$synth.Dispose()
Write-Output "synthetic wav: $Out"
