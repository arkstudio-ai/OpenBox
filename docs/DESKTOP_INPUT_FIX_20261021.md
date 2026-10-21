# 无影网页桌面勾选操控后无响应（2026-10-21）

## 原因和修改

固定 SDK `2.13.9-asp3.18.11` 暴露的 `enableInput` 与 `setInputEnabled`
控制不同层次，不能作为互斥的新旧 API。厂商实际代码中，前者调用 live engine 的
`enableInput`，后者调用 `getLocalConfig().setInputEnabled`。

原先 `DesktopTab.setSessionControl` 优先调用 `setInputEnabled`，存在该方法时就跳过
`enableInput`。因此页面显示“已接管操控”，配置也是 enabled，但引擎仍可不接收输入。
现在每次只读/操控切换和连接恢复都将两个开关同步到同一个布尔值，继续单独同步
keyboard/IME 与 touch，保持 Client 鼠标模式。

## 线上证据

目标账号的云电脑在线，网页“允许操控”已选中，SDK 可观察到点击，远程输入服务也有收到
数据的日志，但桌面没有响应。虚拟设备禁用后重新启用、网页重连均未解决。

经用户明确批准重启该云电脑后，旧网页仍复现同一问题。随后做单变量验证：
重新加载旧网页并勾选操控，点击远程浏览器第二个标签无效；只调用一次
`session.enableInput(true)` 后，在同一位置点击立即切换成功，未改变鼠标模式。
这证明只调整 SDK local config 不足以启用当前会话的输入。

修复版本部署后，重载正式网页清除所有临时调试调用，并通过真实页面操作验证：

- 初始默认只读；勾选“允许操控”后，点击切换远程浏览器标签成功。
- 真实键盘按键 `1`、`2`、`3` 显示在远程地址栏，未提交搜索。
- 取消“允许操控”后，点击其他标签和按 `4` 均被阻止，原标签和 `123` 保持不变。
- 再次勾选，页面回到“已接管操控”，供用户继续使用。

本次未重建云电脑镜像、清理浏览器资料或删除云电脑文件。重启前已向用户说明应用退出及
未保存内容的影响并取得批准；重启本身不构成该前端问题的修复。

## 验证与发布

6 项 `DesktopTab.test.tsx` 测试通过，涵盖只读、操控、再次连接和关闭操控的两个输入开关；
`tsconfig.app.json`、`tsconfig.node.json` 检查与 Vite 生产构建通过。未运行全量测试。

源码提交 `d0c0e9568321ec6a21826b2e8165955f0533aae1`。
`main@a95a3023` 的前端与生产 `be75d22d` 相同，本次只有桌面组件及其测试改动。
基于 `openbox-frontend-v2:20261021-assistant-be75d22d` 镜像覆盖新静态构建，
保留原 Nginx 配置以及旧的哈希资源，避免已打开页面后续加载旧 chunk 失败。

2026-10-21 12:47:11 UTC 完成 frontend 单服务切换，镜像
`openbox-frontend-v2:20261021-desktop-input-d0c0e956`，healthy、重启 0。
backend 仍是本次问答续接修复版本，其余服务容器不变。公网首页 build-id 匹配，
`/api/environment` 为 200，无数据库迁移。

配置备份在 `/opt/openbox/backups/20261021-desktop-input-d0c0e956/activation-20261021T124649Z`。
回滚只把 compose override 的 frontend 镜像恢复为此前标签，执行
`docker compose up -d --no-deps frontend`，验证 healthy、Nginx 与公网 API；
不要回滚 backend 或数据库。
