# 建议卡滚动修复发布（2026-09-11）

源码 `617f77ee4ca74ea237f34a25939d8692bedec1b8` 已推送到 `origin/main`。Web 与 Flutter 原生端的建议卡固定在输入框上方，滚动历史时不再反复显隐；卡片区域的滚轮和竖向拖动会转交聊天历史。卡片和加载态由 Claude CLI 按现有主题 tokens 调整。

## 阿里云

- 本机 Docker 从干净 Git 导出构建 `linux/amd64` 镜像 `openbox-frontend-v2:20260911-suggestion-scroll-617f77e`，固定线上 Nginx 1.31.5，构建标识 `20260911-suggestion-scroll-617f77e`。
- 私有 OSS 中转到上海 ECS `i-uf66pcsepxpc23v5qsts`，压缩镜像 SHA-256 `30cbc1aa2e24e7e96aefad96646fff918a7341ca306172b876c66a62faaba82f`。装载前后校验一致，临时对象已删除。
- 备用 loopback 容器的 Nginx 配置、首页、CSS、聊天 JS 和两个 API 检查通过后，仅替换 frontend。后端、Postgres、Redis 容器 ID 保持不变，四服务均 healthy。
- 配置与数据库备份：`/opt/openbox/backups/20260911-suggestion-scroll-617f77e/activation-20260911T082144Z`；数据库备份 6,012,885 字节，`pg_restore --list` 验证通过。没有迁移，解析后的 Compose 只有 frontend image 变化。
- 公网 `https://ai.bossipai.com.cn/` 的构建标识和三个静态资源指纹匹配；`/app`、`/api/environment`、`/api/auth/logto/config` 均返回 200。单实例切换采样约 13.4 秒 502 后恢复。
- 回滚：将 `/opt/openbox/docker-compose.override.yml` 的 frontend image 改回 `openbox-frontend-v2:20260910-fe-928a228`，再执行 `docker compose up -d --no-deps frontend`。

## Android

- 版本 `1.0.18+29`，Release APK：`mobile/build/releases/BossIP-1.0.18-29/BossIP-1.0.18-29.apk`，83,583,619 字节。
- 桌面 7z：`BossIP-Android-1.0.18-29-20260911.7z`，24,971,019 字节，包含 APK、说明与 SHA-256 校验文件。
- 包名 `com.bossip.bipmobile`，`debuggable=false`，支持 arm64-v8a、armeabi-v7a、x86_64；三个 ABI 均验证包含生产地址 `https://ai.bossipai.com.cn`。
- 沿用现有 Android Debug 证书签名，APK v2 签名检查通过，供内部安装测试。没有更换签名密钥或上传应用商店。
- APK SHA-256：`c3f22d764da96e621449526877e55ce3a0c6108fd5e4734abe539c866fd814be`。
- 7z SHA-256：`77b526909fe217abaa63e804d48aa66e672106c3cbb5b21b1a61e9657c29f568`。`7zz t` 通过，解压后的 APK SHA-256 与原包一致。

## 验证

仅运行建议卡相关单测和浏览器/Flutter 回归，覆盖小幅滚动、卡片拖动不误发送、正常点击、键盘、加载态和中英文窄屏布局。没有运行全局测试。本次未做物理设备安装测试。

[结构化发布证据](evidence/suggestion-scroll-release-20260911.json)。
