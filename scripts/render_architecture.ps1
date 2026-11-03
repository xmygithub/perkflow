param([Parameter(Mandatory=$true)][string]$DataPath)
# 1. 从同一图数据生成PNG；使用本机字体，不启动应用、不访问业务服务。
Add-Type -AssemblyName System.Drawing
$diagram = Get-Content -LiteralPath $DataPath -Raw -Encoding UTF8 | ConvertFrom-Json
$bitmap = [System.Drawing.Bitmap]::new([int]$diagram.width,[int]$diagram.height)
$bitmap.SetResolution(144,144)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$graphics.Clear([System.Drawing.Color]::White)
$format = [System.Drawing.StringFormat]::new()
$format.Alignment = [System.Drawing.StringAlignment]::Center
$format.LineAlignment = [System.Drawing.StringAlignment]::Center
function Write-DiagramText($text,$x,$y,$w,$h,$size,$color,$bold=$false) {
    $style = if($bold){[System.Drawing.FontStyle]::Bold}else{[System.Drawing.FontStyle]::Regular}
    $font = [System.Drawing.Font]::new('Microsoft YaHei UI',[single]$size,$style,[System.Drawing.GraphicsUnit]::Pixel)
    $brush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml($color))
    $graphics.DrawString([string]$text,$font,$brush,[System.Drawing.RectangleF]::new($x,$y,$w,$h),$format)
    $brush.Dispose();$font.Dispose()
}
try {
    # 2. 先画进程边界，再画连线；节点最后绘制，避免连接线穿过文字。
    foreach($frame in $diagram.frames) {
        $brush=[System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml($frame.fill))
        $pen=[System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml($frame.stroke),2)
        $graphics.FillRectangle($brush,$frame.x,$frame.y,$frame.w,$frame.h)
        $graphics.DrawRectangle($pen,$frame.x,$frame.y,$frame.w,$frame.h)
        $titleWidth = if($frame.titleWidth){$frame.titleWidth}else{$frame.w-30}
        Write-DiagramText $frame.title ($frame.x+15) ($frame.y+8) $titleWidth 38 22 $frame.stroke $true
        $brush.Dispose();$pen.Dispose()
    }
    foreach($edge in $diagram.edges) {
        $pen=[System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml($edge.color),2.5)
        if($edge.dashed){$pen.DashStyle=[System.Drawing.Drawing2D.DashStyle]::Dash}
        $cap=[System.Drawing.Drawing2D.AdjustableArrowCap]::new(5,6,$true)
        $pen.CustomEndCap=$cap
        if($edge.both){$pen.CustomStartCap=$cap}
        [System.Drawing.PointF[]]$points=@($edge.points | ForEach-Object {[System.Drawing.PointF]::new($_[0],$_[1])})
        $graphics.DrawLines($pen,$points)
        $pen.Dispose();$cap.Dispose()
    }
    foreach($node in $diagram.nodes) {
        $brush=[System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml($node.fill))
        $pen=[System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml($node.stroke),2)
        $graphics.FillRectangle($brush,$node.x,$node.y,$node.w,$node.h)
        $graphics.DrawRectangle($pen,$node.x,$node.y,$node.w,$node.h)
        $bodyHeight=$node.lines.Count*27
        $top=$node.y+($node.h-($bodyHeight+36))/2
        Write-DiagramText $node.title ($node.x+10) $top ($node.w-20) 32 23 $node.stroke $true
        for($i=0;$i -lt $node.lines.Count;$i++) {
            Write-DiagramText $node.lines[$i] ($node.x+10) ($top+36+$i*27) ($node.w-20) 27 19 '#263746'
        }
        $brush.Dispose();$pen.Dispose()
    }
    foreach($edge in $diagram.edges) {
        if($edge.label) {
            $brush=[System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
            $graphics.FillRectangle($brush,$edge.lx,$edge.ly,$edge.lw,28)
            Write-DiagramText $edge.label $edge.lx $edge.ly $edge.lw 28 18 $edge.color
            $brush.Dispose()
        }
    }
    Write-DiagramText $diagram.title 30 15 ($diagram.width-60) 49 33 '#17384C' $true
    Write-DiagramText $diagram.subtitle 30 65 ($diagram.width-60) 28 19 '#607583'
    for($i=0;$i -lt $diagram.notes.Count;$i++) {
        Write-DiagramText $diagram.notes[$i] 40 ($diagram.height-72+$i*27) ($diagram.width-80) 27 19 '#526777'
    }
    $output=[System.IO.Path]::ChangeExtension([System.IO.Path]::GetFullPath($DataPath),'.png')
    $bitmap.Save($output,[System.Drawing.Imaging.ImageFormat]::Png)
    Write-Output $output
} finally {
    $format.Dispose();$graphics.Dispose();$bitmap.Dispose()
}
