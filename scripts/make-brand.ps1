# The dashboard's logo files, made from the two logo images in dashboard\brand:
#   logo-for-light.webp  the black artwork, for light backgrounds
#   logo-for-dark.webp   the light artwork, for dark backgrounds
# Both have transparent backgrounds.  Writes 256 px PNGs for the page (the sign-in screen and
# the top bar show them at 88 and 28 px) and 64 px ones wrapped in favicon.svg, which shows
# the right one for the browser's light/dark setting.  Uses Windows' own image codecs; nothing
# is installed.  After running it, raise ?v=N in dashboard\index.html.
$ErrorActionPreference = 'Stop'
$brand = Join-Path (Split-Path -Parent $PSScriptRoot) 'dashboard\brand'
Add-Type -AssemblyName PresentationCore, WindowsBase

function Load-Image([string]$path) {
    $fs = [IO.File]::OpenRead($path)
    try {
        $dec = [Windows.Media.Imaging.BitmapDecoder]::Create($fs, [Windows.Media.Imaging.BitmapCreateOptions]::PreservePixelFormat, [Windows.Media.Imaging.BitmapCacheOption]::OnLoad)
        # the WebP decoder's own format loses the alpha channel when drawn: make it explicit
        $bgra = New-Object Windows.Media.Imaging.FormatConvertedBitmap($dec.Frames[0], [Windows.Media.PixelFormats]::Bgra32, $null, 0)
        $bgra.Freeze()
        return $bgra
    } finally { $fs.Close() }
}

function Save-Square($image, [int]$size, [string]$path) {
    $w = $image.PixelWidth; $h = $image.PixelHeight
    $scale = $size / [Math]::Max($w, $h)
    $dw = $w * $scale; $dh = $h * $scale
    $dv = New-Object Windows.Media.DrawingVisual
    [Windows.Media.RenderOptions]::SetBitmapScalingMode($dv, [Windows.Media.BitmapScalingMode]::HighQuality)
    $dc = $dv.RenderOpen()
    $dc.DrawImage($image, (New-Object Windows.Rect((($size - $dw) / 2), (($size - $dh) / 2), $dw, $dh)))
    $dc.Close()
    $rtb = New-Object Windows.Media.Imaging.RenderTargetBitmap($size, $size, 96, 96, [Windows.Media.PixelFormats]::Pbgra32)
    $rtb.Render($dv)
    $enc = New-Object Windows.Media.Imaging.PngBitmapEncoder
    $enc.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($rtb))
    $out = [IO.File]::Create($path)
    try { $enc.Save($out) } finally { $out.Close() }
}

foreach ($kind in 'light', 'dark') {
    $image = Load-Image (Join-Path $brand "logo-for-$kind.webp")
    Save-Square $image 256 (Join-Path $brand "logo-for-$kind-256.png")
    Save-Square $image 64 (Join-Path $brand "icon-for-$kind-64.png")
}
$l = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $brand 'icon-for-light-64.png')))
$d = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $brand 'icon-for-dark-64.png')))
$svg = @"
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<style>.d{display:none}@media (prefers-color-scheme: dark){.l{display:none}.d{display:inline}}</style>
<image class="l" width="64" height="64" href="data:image/png;base64,$l"/>
<image class="d" width="64" height="64" href="data:image/png;base64,$d"/>
</svg>
"@
[IO.File]::WriteAllText((Join-Path $brand 'favicon.svg'), $svg)
Write-Host "logo files written to $brand"
