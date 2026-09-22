# Kubernetes 遗留部署模板

[base.yaml](base.yaml) 与 [aks.yaml](aks.yaml)保留早期 Kubernetes 部署方式。
当前生产与开发服务器的部署说明位于[部署指南](../docs/operations/DEPLOY.md)和[gw2 Runbook](../deploy/gw2/RUNBOOK.md)。

这些模板不能视为已验证的当前生产配置。重新启用前检查镜像、Secret、存储、Ingress、
Sandbox Provider、迁移与独立轨迹 worker 的配置是否与目标环境一致。
根目录 Makefile 仍保留 `k8s-apply` / `k8s-apply-aks`，它们会修改集群资源，
不属于普通本地启动步骤；使用前核对 kube context、namespace 和最终差异。
