#requires -Version 5.1
<#
倾梦 GPT 工具调用 PowerShell 客户端（修复版）

关键修复：
1. 函数内部进度输出全部用 Write-Host，绝不 Write-Output，避免污染返回值。
2. 返回值只保留一个 [pscustomobject]。
3. 统一 BOM 防御：文件读取、HTTP 文本、content 数组都先 TrimStart([char]0xFEFF)。
4. content 可能是数组，统一用 -join "`n" 转成字符串。

用法：
  pwsh -File scripts/qingmeng-tool-client.ps1 `
    -BaseUrl 'https://your-domain.example' `
    -ApiKey  'YOUR_KEY' `
    -Prompt  'Use the shell tool to run pwd.'
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "https://your-domain.example",
    [Parameter(Mandatory = $true)][string]$ApiKey,
    [string]$Model = "gpt-6",
    [string]$Prompt = "Use the shell tool to run pwd.",
    [int]$TimeoutSec = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Remove-Utf8Bom {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return "" }
    $text = if ($Value -is [array]) { $Value -join "`n" } else { [string]$Value }
    return $text.TrimStart([char]0xFEFF)
}

function Read-Utf8TextFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    return Remove-Utf8Bom $raw
}

function ConvertTo-PlainText {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return "" }
    if ($Value -is [array]) {
        $parts = foreach ($item in $Value) {
            if ($item -is [string]) { $item }
            elseif ($item -is [System.Collections.IDictionary] -and $item.Contains("text")) { [string]$item["text"] }
            else { [string]$item }
        }
        return (Remove-Utf8Bom ($parts -join "`n"))
    }
    return Remove-Utf8Bom ([string]$Value)
}

function Invoke-QingmengToolChat {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$ApiKey,
        [string]$Model = "gpt-6",
        [Parameter(Mandatory = $true)][string]$Prompt,
        [int]$TimeoutSec = 120
    )

    $tools = @(
        @{
            type = "function"
            function = @{
                name = "shell"
                description = "Run a shell command."
                parameters = @{
                    type = "object"
                    properties = @{ command = @{ type = "string" } }
                    required = @("command")
                }
            }
        }
    )

    $payload = @{
        model = $Model
        messages = @(@{ role = "user"; content = $Prompt })
        tools = $tools
        tool_choice = "auto"
    } | ConvertTo-Json -Depth 30 -Compress

    $uri = ($BaseUrl.TrimEnd("/") + "/v1/chat/completions")
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)

    # 进度信息只走 Write-Host；函数返回值只有一个对象。
    Write-Host "[qingmeng] POST $uri model=$Model" -ForegroundColor DarkGray
    $started = Get-Date
    try {
        $response = Invoke-RestMethod `
            -Method Post `
            -Uri $uri `
            -Headers @{ Authorization = "Bearer $ApiKey" } `
            -ContentType "application/json; charset=utf-8" `
            -Body $bytes `
            -TimeoutSec $TimeoutSec
    }
    catch {
        $detail = Remove-Utf8Bom ($_.ErrorDetails.Message)
        if ($detail) { throw "倾梦请求失败：$detail" }
        throw
    }
    $elapsed = ((Get-Date) - $started).TotalSeconds

    $message = $response.choices[0].message
    $content = ConvertTo-PlainText $message.content
    $toolCalls = @()
    if ($null -ne $message.tool_calls) { $toolCalls = @($message.tool_calls) }

    Write-Host ("[qingmeng] status=200 elapsed={0:N2}s tool_calls={1}" -f $elapsed, $toolCalls.Count) -ForegroundColor DarkGray

    return [pscustomobject]@{
        Content   = $content
        ToolCalls = $toolCalls
        Raw       = $response
    }
}

# ---- 示例 ----
$result = Invoke-QingmengToolChat -BaseUrl $BaseUrl -ApiKey $ApiKey -Model $Model -Prompt $Prompt -TimeoutSec $TimeoutSec

if ($result.Content) {
    Write-Host "[assistant] $($result.Content)" -ForegroundColor Cyan
}
foreach ($call in $result.ToolCalls) {
    $name = [string]$call.function.name
    $argsText = Remove-Utf8Bom ([string]$call.function.arguments)
    Write-Host "[tool_call] $name $argsText" -ForegroundColor Yellow
}
if (-not $result.Content -and $result.ToolCalls.Count -eq 0) {
    Write-Host "[warn] 既没有 content 也没有 tool_calls，原始响应：" -ForegroundColor Red
    Write-Host (($result.Raw | ConvertTo-Json -Depth 10))
}
