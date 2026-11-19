# hy2qianyi

[hy2jieshu](https://github.com/liucong552-art/hy2jieshu) 的双 VPS 轮换迁移工具，沿用 [qianyi](https://github.com/liucong552-art/qianyi) 的临时运行、SSH 传输和目标事务恢复方式。仅支持 HY2，不接收 VLESS 的迁移包。

**只负责迁移，不安装常驻迁移程序。** 目标接管源机的 UDP 443 主节点及全部未到期的普通、WG-NAT 临时节点。同端口以源机为准，目标机独有的节点保留。成功后删除本轮迁移工具、传输材料和临时防护，仅保留迁入的业务数据。源机保持运行，验证成功后由你切 DNS、自行重装旧机。

## 先准备两台 VPS

- Debian 11+ / Ubuntu 20.04+，root、Python 3.10+、systemd、nftables、OpenSSL、curl、ss。两台均已部署标准 hy2jieshu 443 主节点，管理库版本和原生脚本 SHA 必须一致。工具不安装或升级 Hysteria 核心。
- **默认随迁源节点各域名的 TLS 证书链和配对私钥。** 目标主节点允许原来使用不同域名。程序在改写业务前校验证书链、有效期、SAN 和私钥配对，优先使用目标已有的有效同域证书。需要写入的证书随业务事务提交或回滚。目标使用自己的 ACME 账户和续期配置。接管后为迁入域名配置续期，或在迁移前通过 DNS-01 给目标签发独立同域证书。ACME 账户、DNS API 凭据和 WG 私钥分别由各机器管理。
- **WG-NAT 复用目标机已有的同名 WG 接口及独立 Peer**。先按 hy2jieshu 的 vpswg.sh / natjichang.sh 配置目标出口并建立一个目标 NAT 节点，验证握手、fail-closed 定时保护和 oif 策略路由。工具会采用目标 mark、table、优先级，不修改 NAT 出口机，不会把源机 WG 私钥复制过去。源、目标共用同一 Peer 会引起出口切换，应使用不同 Peer。
- 迁入端口如有 TCP 业务、未知 UDP 监听、自定义 unit/drop-in、独立 portbw 或暂停策略，会在替换前拒绝。portbw 需另行按其管理流程配置，不会自动移植 tc/qdisc。
- 有 HY2 手工黑名单时，目标需先安装同版本 hy2-guard。支持 IPv4/IPv6 CIDR，合并并保留目标原有黑名单和其他端口状态；不支持手工修改本项目拥有的 nft 表。
- 原生 hy2_traffic 为可选组件。源已有统计时，导出最新持久采样（由原生分钟定时器更新），不在源机触发采集、不改源配额或业务配置。此时目标须提前通过 hy2jieshu 的原生流程安装匹配版本的统计组件。缺少组件时，预检会在替换业务前停止。

## Windows 一键迁移

两台 VPS 均能用本机同一私钥登录，且已核对 SSH 主机指纹。在 Windows PowerShell 粘贴：

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $client = [Net.WebClient]::new()
    $sha256 = [Security.Cryptography.SHA256]::Create()
    $previousTls = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $body = $client.DownloadData('https://raw.githubusercontent.com/liucong552-art/hy2qianyi/main/hy2qianyi.ps1')
        $hash = ([BitConverter]::ToString($sha256.ComputeHash($body))).Replace('-', '').ToLowerInvariant()
        if ($hash -ne '047d0cb2ffc4f2ff6d4cc42bd9d9a5254e5f5e51d99bf14a86710bbcbb1be0a7') { throw '启动脚本校验失败，请重新打开 README 获取最新命令。' }
    }
    finally {
        [Net.ServicePointManager]::SecurityProtocol = $previousTls
        $client.Dispose()
        $sha256.Dispose()
    }
    $scriptText = [Text.Encoding]::UTF8.GetString($body).TrimStart([char]0xFEFF)
    & ([ScriptBlock]::Create($scriptText))
}
```

按提示填写源机、目标机 IPv4 和 SSH 端口，默认 22。优先使用 `.ssh\vps_root_ed25519`，也识别常见私钥文件名。SSH 私钥留在 Windows。含节点凭据和 TLS 私钥的二进制快照只经过 Windows 内存，通过 SSH 分别读取和提交。导出失败时不会启动目标导入。

## VPS 直接传输

目标需能够以 root SSH 登录源机，且支持本地 TCP 转发。在源机运行：

```bash
(
  set -eu
  umask 077
  d=$(mktemp -d /run/hy2qianyi.XXXXXXXX)
  trap 'rm -rf -- "$d"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  curl --proto '=https' -fsSL --max-time 90 \
    https://raw.githubusercontent.com/liucong552-art/hy2qianyi/main/hy2qianyi.py -o "$d/hy2qianyi.py"
  printf '%s  %s\n' '5e879518039c52bc651d67d4765da137c2b4e57060ae1589eb6fdaee01a20282' "$d/hy2qianyi.py" | sha256sum -c -
  python3 -I -B "$d/hy2qianyi.py" send
)
```

源机读取当前 SSH 地址和端口，必要时按提示改成目标能够连接的外部管理地址。传输服务仅监听源机回环地址，通过 SSH 隧道连接；传输层另校验临时 TLS 证书指纹、一次性令牌和包摘要。默认窗口 20 分钟。

保持源机终端开启，将输出的命令粘贴到目标机，再输入不回显的一次性令牌。看到“迁移完成”后，复制一份客户端配置，将服务器地址换成目标 IP，保留原 SNI、端口、密码、混淆和 TLS 校验。验证主节点、普通和 NAT 节点后再切换 A/AAAA 记录。切换期间让源机继续运行。链接内写死源机 IP 时，需要修改客户端地址。

## 带走什么

保留原链接、密码、Salamander、到期时间、IP 限制、配额总量、快照时计费已用量、剩余额度及重置周期。目标原生库会执行已经到期的配额重置。来源 IP 黏性集合在新机从空集合开始，回滚则恢复旧目标集合的剩余有效期。

每日统计携带最近 30 个北京时间自然日的最新已保存采样。源机导出后产生的用量仍留在源机；DNS 并行期两机分别计数。同端口的每日历史采用源机记录，目标其他节点历史保留。**不要为补历史重复导入旧快照，否则会覆盖新目标已经增长的计费用量。**

主密码文件、配置、元数据、默认值、发布订阅和需要部署的 TLS 证书共同进入目标事务。改写前仅封闭本轮 UDP 端口；失败时恢复旧目标数据与服务。独有端口不停止，SSH 端口不受本轮 UDP 防护规则影响。程序同时拒绝迁入端口上的 TCP 监听，避免其他双协议策略影响已有 TCP 服务。

## 中断恢复

预检拒绝不会替换目标业务配置。启动失败在提交前自动回滚；提交后的清理失败仅完成收尾，避免用旧配额覆盖新用量。恢复文件 `/var/lib/hy2/.hy2qianyi-recovery.json` 含凭据，仅 root 可读。

收到需运行 recover 的提示，或目标导入遭遇断电/强制终止时，重新运行上面的临时下载命令，把最后一行改成：

```bash
python3 -I -B "$d/hy2qianyi.py" recover
```

正常完成后删除本轮脚本、检查配置、隧道 socket、传输 TLS 材料、防护表、服务 drop-in 和恢复文件。迁入的业务证书和统计继续由原管理系统维护。源机不会被自动卸载或重装。

## 验证

Linux：`python3 -B -m unittest discover -s tests -v`

Windows PowerShell：`.\tests\test_windows_relay.ps1`

[本轮真实机器验收](tests/REAL_MACHINE_ACCEPTANCE_2026-11-17.md) 明确记录实际通过、失败及未测边界。测试样例均为虚构地址与凭据，仓库不包含机器快照或真实密钥。
