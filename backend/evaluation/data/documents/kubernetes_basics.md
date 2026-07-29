# Kubernetes 基础

## 核心概念

Kubernetes（K8s）是容器编排平台，负责自动化部署、扩展和管理容器化应用。

| 概念 | 说明 | 类比 |
|------|------|------|
| Pod | 最小调度单元 | 一组容器 |
| Deployment | 声明式部署 | 应用版本管理 |
| Service | 网络抽象 | 负载均衡器 |
| ConfigMap | 配置管理 | 环境变量文件 |
| Secret | 敏感数据 | 加密配置 |
| Ingress | HTTP 路由 | Nginx 反代 |
| Namespace | 资源隔离 | 项目空间 |

## 常用命令

```bash
# 集群信息
kubectl cluster-info
kubectl get nodes

# Pod 操作
kubectl get pods -n default
kubectl describe pod <pod-name>
kubectl logs -f <pod-name> -c <container-name>

# Deployment 操作
kubectl create deployment nginx --image=nginx:alpine
kubectl scale deployment nginx --replicas=3
kubectl rollout status deployment/nginx
kubectl rollout undo deployment/nginx

# Service 操作
kubectl expose deployment nginx --port=80 --type=LoadBalancer
kubectl get services
```

## Deployment YAML 示例

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-app
  labels:
    app: web
spec:
  replicas: 3
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
      - name: nginx
        image: nginx:1.21-alpine
        ports:
        - containerPort: 80
        resources:
          requests:
            memory: "64Mi"
            cpu: "250m"
          limits:
            memory: "128Mi"
            cpu: "500m"
        livenessProbe:
          httpGet:
            path: /health
            port: 80
          initialDelaySeconds: 30
          periodSeconds: 10
```

## Service 类型对比

| 类型 | 说明 | 访问方式 | 使用场景 |
|------|------|---------|---------|
| ClusterIP | 集群内部 IP | 集群内访问 | 内部服务通信 |
| NodePort | 节点端口 | NodeIP:Port | 开发测试 |
| LoadBalancer | 云负载均衡 | 外部 IP | 生产暴露 |
| Headless | 无 ClusterIP | Pod IP 直连 | StatefulSet |

## 资源管理

| 资源 | 请求（requests） | 限制（limits） | 说明 |
|------|:---:|:---:|------|
| CPU | 250m | 500m | 1 核 = 1000m |
| Memory | 64Mi | 128Mi | 1Mi = 1MB |

## 滚动更新策略

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxSurge: 1        # 更新时最多多出 1 个 Pod
    maxUnavailable: 0  # 更新时不允许减少可用 Pod
```

更新过程：先创建新 Pod → 新 Pod 就绪 → 删除旧 Pod，保证零停机。
