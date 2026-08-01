# 应用服务上线发布 SOP

| 文档属性 | 内容 |
| --- | --- |
| 文档编号 | SOP-OPS-RELEASE-003 |
| 分类 | ops_sop |
| 版本 | v3.1 |
| 适用范围 | 公司所有后端应用服务（Java/Go/Python/Node.js）从开发到生产的上线发布流程 |
| 维护人 | DevOps 组 - 周杰 |
| 生效日期 | 2024-04-10 |
| 审核人 | 研发总监 王芳、运维总监 李伟 |

---

## 1. 目的与适用范围

本 SOP 规范公司应用服务从代码合并到生产部署的全流程，覆盖发布前检查、灰度发布、蓝绿部署、回滚、Docker 镜像构建、Kubernetes 部署、发布后验证等关键环节，目标是将线上事故率控制在 0.5% 以下，平均恢复时间 MTTR < 10 分钟。

适用于所有运行在 Kubernetes 集群上的微服务，CI/CD 平台为 GitLab CI + ArgoCD。

## 2. 发布前检查清单

### 2.1 代码与测试检查

| 检查项 | 责任人 | 工具 | 通过标准 |
| --- | --- | --- | --- |
| 代码审查通过 | Tech Lead | GitLab MR | 至少 2 人 Approve |
| 单元测试覆盖率 | 开发 | SonarQube | 行覆盖 ≥ 80%，分支覆盖 ≥ 70% |
| 单元测试通过率 | CI | GitLab CI | 100% 通过 |
| 静态代码扫描 | CI | SonarQube | 0 Critical，0 Blocker |
| 安全漏洞扫描 | CI | Trivy / Snyk | 0 高危 CVE |
| 集成测试 | QA | Jenkins | 全部用例通过 |
| 接口契约测试 | QA | Pact | 与下游契约一致 |
| 性能基准回归 | QA | JMeter | P99 响应时间无劣化 > 10% |

### 2.2 依赖与配置检查

```bash
# 1. 依赖版本检查（pom.xml / package.json / go.mod）
mvn versions:display-dependency-updates
npm outdated
go list -m -u all

# 2. 配置项确认
# 检查 application-prod.yml 中的数据库、缓存、MQ 连接信息
diff config/application-staging.yml config/application-prod.yml

# 3. 数据库变更确认（Flyway / Liquibase）
flyway info -configFiles=flyway-prod.conf
# 必须确认无破坏性 DDL（DROP/RENAME），如有需走 DBA 审批

# 4. 镜像仓库空间检查
harbor-cli project list --name=company-apps

# 5. Secret 确认
kubectl get secret app-secrets -n production -o yaml | grep -c 'data:'
```

### 2.3 发布审批矩阵

| 影响范围 | 审批级别 | 通知范围 |
| --- | --- | --- |
| 单个非核心服务 | Tech Lead | 业务群 |
| 多个服务联动 | 研发总监 + 业务负责人 | 业务群 + 客服 |
| 核心链路（订单/支付） | CTO | 全公司 |
| 数据库 schema 变更 | DBA + 架构组 | 业务群 + DBA |

## 3. Docker 镜像构建与推送

### 3.1 Dockerfile 最佳实践（多阶段构建）

```dockerfile
# Dockerfile - Spring Boot 应用示例
# 阶段一：构建
FROM maven:3.9-eclipse-temurin-17 AS builder
WORKDIR /build
COPY pom.xml .
RUN mvn dependency:go-offline -B
COPY src ./src
RUN mvn clean package -DskipTests -B && \
    mv target/app.jar target/app.jar

# 阶段二：运行（使用 distroless 减小攻击面）
FROM gcr.io/distroless/java17-debian12:nonroot
LABEL maintainer="devops@company.com"
LABEL org.opencontainers.image.source="https://gitlab.company.com/app/order-service"

COPY --from=builder /build/target/app.jar /app/app.jar

USER nonroot:nonroot
EXPOSE 8080 8081

ENTRYPOINT ["java", \
    "-XX:+UseG1GC", \
    "-XX:MaxRAMPercentage=75.0", \
    "-XX:+HeapDumpOnOutOfMemoryError", \
    "-XX:HeapDumpPath=/app/heapdump", \
    "-Djava.security.egd=file:/dev/./urandom", \
    "-jar", "/app/app.jar"]
```

### 3.2 镜像标签规范

| 标签格式 | 用途 | 示例 |
| --- | --- | --- |
| `{service}:{semver}` | 正式发布 | `order-service:2.4.1` |
| `{service}:{semver}-{git_short}` | 可追溯发布 | `order-service:2.4.1-7f3a2b1` |
| `{service}:{semver}-rc.{n}` | 预发候选 | `order-service:2.4.1-rc.3` |
| `{service}:dev-{git_short}` | 测试环境 | `order-service:dev-7f3a2b1` |
| `{service}:latest` | 禁用 | - |

镜像标签遵循语义化版本 SemVer：`MAJOR.MINOR.PATCH`。破坏性变更必须升 MAJOR。

### 3.3 镜像构建与推送脚本

```bash
#!/bin/bash
set -euo pipefail

SERVICE=$1
VERSION=$2
GIT_SHORT=$(git rev-parse --short HEAD)
HARBOR=harbor.company.com/company-apps

# 构建多架构镜像
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    -t ${HARBOR}/${SERVICE}:${VERSION}-${GIT_SHORT} \
    -t ${HARBOR}/${SERVICE}:${VERSION} \
    --push \
    .

# 触发 Trivy 安全扫描
trivy image --exit-code 1 --severity CRITICAL ${HARBOR}/${SERVICE}:${VERSION}-${GIT_SHORT}

# 签名（cosign）
cosign sign --key cosign.key ${HARBOR}/${SERVICE}:${VERSION}-${GIT_SHORT}

echo "镜像推送完成: ${HARBOR}/${SERVICE}:${VERSION}-${GIT_SHORT}"
```

## 4. Kubernetes 部署配置

### 4.1 Deployment YAML 示例

```yaml
# k8s/production/order-service-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: order-service
  namespace: production
  labels:
    app: order-service
    version: "2.4.1"
    tier: backend
    team: trade
spec:
  replicas: 6
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 0
  selector:
    matchLabels:
      app: order-service
  template:
    metadata:
      labels:
        app: order-service
        version: "2.4.1"
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8081"
        prometheus.io/path: "/actuator/prometheus"
    spec:
      serviceAccountName: order-service-sa
      terminationGracePeriodSeconds: 60
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    app: order-service
                topologyKey: kubernetes.io/hostname
      containers:
        - name: order-service
          image: harbor.company.com/company-apps/order-service:2.4.1-7f3a2b1
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8080
            - name: metrics
              containerPort: 8081
          envFrom:
            - configMapRef:
                name: order-service-config
            - secretRef:
                name: order-service-secret
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: POD_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "2000m"
              memory: "3Gi"
          readinessProbe:
            httpGet:
              path: /actuator/health/readiness
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 10
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /actuator/health/liveness
              port: 8080
            initialDelaySeconds: 60
            periodSeconds: 20
            failureThreshold: 3
          startupProbe:
            httpGet:
              path: /actuator/health
              port: 8080
            failureThreshold: 30
            periodSeconds: 10
          lifecycle:
            preStop:
              exec:
                command: ["/bin/sh", "-c", "sleep 15"]
          volumeMounts:
            - name: app-logs
              mountPath: /app/logs
            - name: heapdump
              mountPath: /app/heapdump
      volumes:
        - name: app-logs
          emptyDir: {}
        - name: heapdump
          emptyDir: {}
      imagePullSecrets:
        - name: harbor-pull-secret
```

### 4.2 Service YAML

```yaml
apiVersion: v1
kind: Service
metadata:
  name: order-service
  namespace: production
  labels:
    app: order-service
spec:
  type: ClusterIP
  selector:
    app: order-service
  ports:
    - name: http
      port: 80
      targetPort: 8080
      protocol: TCP
```

### 4.3 ConfigMap 与 Secret

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: order-service-config
  namespace: production
data:
  SPRING_PROFILES_ACTIVE: "prod"
  SERVER_SHUTDOWN: "graceful"
  SPRING_LIFECYCLE_TIMEOUT_PER_SHUTDOWN_PHASE: "30s"
  LOGGING_LEVEL_ROOT: "INFO"
  MANAGEMENT_ENDPOINTS_WEB_EXPOSURE_INCLUDE: "health,info,prometheus,metrics"
  DB_HOST: "mysql.production.svc.cluster.local"
  DB_PORT: "3306"
  DB_NAME: "orders"
  REDIS_HOST: "redis.production.svc.cluster.local"
  KAFKA_BROKERS: "kafka.production.svc.cluster.local:9092"
---
apiVersion: v1
kind: Secret
metadata:
  name: order-service-secret
  namespace: production
type: Opaque
stringData:
  DB_PASSWORD: "REDACTED"
  REDIS_PASSWORD: "REDACTED"
  KAFKA_SASL_PASSWORD: "REDACTED"
  JWT_SIGNING_KEY: "REDACTED"
```

## 5. CI/CD Pipeline 配置

### 5.1 GitLab CI 配置

```yaml
# .gitlab-ci.yml
stages:
  - build
  - test
  - security
  - package
  - deploy-staging
  - integration
  - deploy-production

variables:
  MAVEN_OPTS: "-Dmaven.repo.local=.m2/repository"
  IMAGE: harbor.company.com/company-apps/$CI_PROJECT_NAME

# 缓存加速
cache:
  key: ${CI_COMMIT_REF_SLUG}
  paths:
    - .m2/repository
    - target/

# 编译
build:
  stage: build
  image: maven:3.9-eclipse-temurin-17
  script:
    - mvn clean compile -B
  rules:
    - if: $CI_MERGE_REQUEST_ID

# 单元测试
unit-test:
  stage: test
  image: maven:3.9-eclipse-temurin-17
  script:
    - mvn test -B
    - mvn jacoco:report
  artifacts:
    reports:
      junit: target/surefire-reports/TEST-*.xml
    paths:
      - target/site/jacoco/
  coverage: '/Total.*?([0-9]{1,3})%/'

# 静态扫描
sonarqube:
  stage: test
  image: maven:3.9-eclipse-temurin-17
  script:
    - mvn sonar:sonar -Dsonar.host.url=$SONAR_HOST -Dsonar.login=$SONAR_TOKEN
  allow_failure: false

# 安全扫描
security-scan:
  stage: security
  image: aquasec/trivy:latest
  script:
    - trivy fs --exit-code 1 --severity CRITICAL,HIGH .
  allow_failure: false

# 镜像构建
package:
  stage: package
  image: docker:24
  services:
    - docker:24-dind
  script:
    - docker login -u $HARBOR_USER -p $HARBOR_PASSWORD harbor.company.com
    - docker buildx build
        --platform linux/amd64
        -t $IMAGE:$CI_COMMIT_TAG
        -t $IMAGE:$CI_COMMIT_SHORT_SHA
        --push .
  rules:
    - if: $CI_COMMIT_TAG

# 部署到预发
deploy-staging:
  stage: deploy-staging
  image: argoproj/argocd:v2.7.0
  script:
    - argocd app sync order-service-staging --dest-namespace staging
    - argocd app wait order-service-staging --health --timeout 300s
  rules:
    - if: $CI_COMMIT_TAG
  environment:
    name: staging

# 集成测试
integration-test:
  stage: integration
  image: postman/newman:5
  script:
    - newman run tests/order-service.postman_collection.json
        --env-var "base_url=https://staging.company.com"
  needs: ["deploy-staging"]

# 部署生产（手动触发）
deploy-production:
  stage: deploy-production
  image: argoproj/argocd:v2.7.0
  script:
    - argocd app sync order-service-production --dest-namespace production
    - argocd app wait order-service-production --health --timeout 600s
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+$/
  when: manual
  environment:
    name: production
```

### 5.2 GitHub Actions 配置示例

```yaml
# .github/workflows/release.yml
name: Release Pipeline

on:
  push:
    tags: ['v*.*.*']

jobs:
  build-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          java-version: '17'
          distribution: 'temurin'
          cache: maven
      - run: mvn -B clean verify
      - uses: actions/upload-artifact@v4
        with:
          name: test-results
          path: target/surefire-reports/

  build-image:
    needs: build-test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: harbor.company.com
          username: ${{ secrets.HARBOR_USER }}
          password: ${{ secrets.HARBOR_PASSWORD }}
      - uses: docker/build-push-action@v5
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: |
            harbor.company.com/company-apps/${{ github.event.repository.name }}:${{ github.ref_name }}
            harbor.company.com/company-apps/${{ github.event.repository.name }}:${{ github.sha }}

  deploy-production:
    needs: build-image
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - name: Deploy via ArgoCD
        run: |
          argocd app sync ${{ github.event.repository.name }}-production
          argocd app wait ${{ github.event.repository.name }}-production --health
```

## 6. 灰度发布流程

### 6.1 基于 Nginx Ingress 的灰度（10% → 50% → 100%）

```yaml
# 第一步：10% 灰度（Canary）
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: order-service-canary
  namespace: production
  annotations:
    nginx.ingress.kubernetes.io/canary: "true"
    nginx.ingress.kubernetes.io/canary-weight: "10"
    nginx.ingress.kubernetes.io/canary-by-header: "X-Canary"
    nginx.ingress.kubernetes.io/canary-by-header-value: "true"
spec:
  ingressClassName: nginx
  rules:
    - host: api.company.com
      http:
        paths:
          - path: /api/orders
            pathType: Prefix
            backend:
              service:
                name: order-service-canary
                port:
                  number: 80
```

灰度放量计划：

| 阶段 | 流量比例 | 观察指标 | 持续时间 | 进阶条件 |
| --- | --- | --- | --- | --- |
| Canary | 10% | 错误率、P99 延迟、业务指标 | 30 分钟 | 错误率 < 0.1%，延迟无劣化 |
| Gray-1 | 50% | 同上 + 监控告警 | 1 小时 | 无 P1/P2 告警 |
| Gray-2 | 100% | 全量观察 | 2 小时 | 全部正常 |

### 6.2 基于 Argo Rollouts 的灰度

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: order-service
spec:
  replicas: 6
  strategy:
    canary:
      canaryService: order-service-canary
      stableService: order-service-stable
      trafficRouting:
        nginx:
          stableIngress: order-service-stable
      steps:
        - setWeight: 10
        - pause: { duration: 30m }
        - analysis:
            templates:
              - templateName: success-rate
            args:
              - name: service-name
                value: order-service-canary
        - setWeight: 50
        - pause: { duration: 1h }
        - setWeight: 100
  selector:
    matchLabels:
      app: order-service
  template:
    # ... 同 Deployment spec
```

## 7. 蓝绿部署操作步骤

```bash
# 1. 当前生产为 blue 版本，部署 green 版本
kubectl apply -f k8s/production/order-service-green.yaml

# 2. 等待 green 就绪
kubectl wait --for=condition=ready pod -l app=order-service,version=green -n production --timeout=300s

# 3. 端到端测试 green
curl -H "X-Version: green" https://api.company.com/api/orders/health

# 4. 切换 Service selector 到 green
kubectl patch svc order-service -n production -p '
  {"spec":{"selector":{"version":"green"}}}'

# 5. 观察 5 分钟，确认无误后保留 blue 一段时间
# 6. 清理旧 blue 版本
kubectl delete deployment order-service-blue -n production
```

## 8. 回滚流程

### 8.1 快速回滚（应用层）

```bash
# 方式一：ArgoCD 回滚到上一版本
argocd app rollback order-service-production

# 方式二：kubectl rollout undo
kubectl rollout undo deployment/order-service -n production

# 回滚到指定版本
kubectl rollout history deployment/order-service -n production
kubectl rollout undo deployment/order-service -n production --to-revision=3

# 查看回滚状态
kubectl rollout status deployment/order-service -n production
```

### 8.2 灰度回滚

```bash
# 立即将流量切回 stable
kubectl annotate ingress order-service-canary -n production \
    nginx.ingress.kubernetes.io/canary-weight="0" --overwrite

# 删除 canary 资源
kubectl delete rollout order-service -n production
```

### 8.3 数据库回滚注意事项

⚠️ 数据库变更往往不可逆，必须遵循以下原则：

1. **破坏性 DDL 提前准备 backout 脚本**：每个 DDL 必须配套 `rollback.sql`
2. **扩展而非替换**：新增字段而非删除/重命名，老字段保留 2 个版本再清理
3. **分阶段执行**：先加字段 → 双写 → 切读 → 停老字段 → 删老字段
4. **生产 DDL 必须先在影子库验证**：使用 pt-online-schema-change 或 gh-ost

```bash
# Flyway 回滚（仅对未发布的版本有效）
flyway undo -configFiles=flyway-prod.conf

# 对于已执行的 DDL，使用 backout 脚本手动回滚
mysql -u admin -p < /opt/sql/rollback/v2.4.1_rollback.sql
```

## 9. 发布窗口管理

### 9.1 允许发布窗口

- 工作日：10:00 - 18:00（避开早晚高峰）
- 周二、周四：核心链路发布日
- 紧急修复：7x24，但需 CTO 审批

### 9.2 禁止发布窗口

| 时间段 | 原因 |
| --- | --- |
| 周五 16:00 后 | 周末运维力量薄弱 |
| 节假日前 1 天 | 故障响应困难 |
| 大促日（618/双11）封网期 | 业务高峰 |
| 财务月结日（每月 1-3 日） | 财务系统稳定 |
| 已发布服务未完成观察 | 避免叠加变更 |

### 9.3 紧急发布审批

紧急 hotfix 需走"绿色通道"：电话/钉钉群通知 → CTO 口头审批 → 即时执行 → 24 小时内补齐流程单。

## 10. 发布后验证

### 10.1 健康检查

```bash
# 1. Pod 健康状态
kubectl get pods -n production -l app=order-service
# 期望：6/6 Running，无 Restarts

# 2. 就绪探针
kubectl describe deployment order-service -n production | grep -A5 Conditions

# 3. 端到端健康检查
curl -s https://api.company.com/actuator/health | jq

# 4. 关键接口冒烟测试
curl -s -X POST https://api.company.com/api/orders \
    -H "Content-Type: application/json" \
    -d '{"userId":"smoke_test","productId":"P001","qty":1}' | jq
```

### 10.2 监控确认

发布后必须观察 30 分钟，确认以下指标：

| 指标 | 工具 | 告警阈值 |
| --- | --- | --- |
| 错误率（5xx） | Grafana | > 0.5% |
| P99 响应时间 | Grafana | 较前一版本劣化 > 20% |
| QPS | Grafana | 较前一版本下降 > 15% |
| CPU 使用率 | Grafana | > 80% 持续 5 分钟 |
| 内存使用率 | Grafana | > 85% 持续 5 分钟 |
| GC 时间 | Grafana | Full GC > 0 |
| 业务核心指标 | 业务大盘 | 订单量、支付成功率 |

### 10.3 日志检查

```bash
# 1. 查看新版本 Pod 日志，确认无 ERROR
kubectl logs -n production -l app=order-service,version=2.4.1 --tail=200 | grep -i "error\|exception"

# 2. ELK 检查
# 访问 Kibana，过滤 service=order-service AND level=ERROR，对比发布前后

# 3. 检查是否有 OOM Killed
kubectl get pods -n production -l app=order-service -o jsonpath='{.items[*].status.containerStatuses[*].lastState}' | jq
```

### 10.4 发布观察期

| 阶段 | 时长 | 关注点 | 升级条件 |
| --- | --- | --- | --- |
| T+15min | 15 分钟 | 基础指标 | 任何告警 → 立即回滚 |
| T+1h | 1 小时 | 业务指标 | 核心指标劣化 → 回滚 |
| T+24h | 24 小时 | 长尾问题 | 异常上升 → 复盘 |
| T+7d | 7 天 | 资源使用趋势 | 资源调优 |

## 11. 应急响应与回滚决策

```
发布后异常发现
       ↓
   影响评估
       ↓
  ┌────┴────┐
  ↓         ↓
P1/P2 事故   P3/P4
  ↓         ↓
立即回滚    观察 15min
  ↓         ↓
通知 CTO    恢复 → 复盘
  ↓
执行回滚
  ↓
验证恢复
  ↓
48h 内复盘
```

## 12. 变更记录

| 版本 | 日期 | 修改内容 | 修改人 |
| --- | --- | --- | --- |
| v1.0 | 2022-01-15 | 初版发布 | 周杰 |
| v2.0 | 2023-03-20 | 引入 ArgoCD GitOps 流程 | 周杰 |
| v2.5 | 2023-07-10 | 增加灰度发布章节 | 林涛 |
| v3.0 | 2023-11-25 | 增加 Argo Rollouts 灰度 | 周杰 |
| v3.1 | 2024-04-10 | 增加发布窗口管理与回滚决策树 | 周杰 |

## 13. 附录

### 13.1 常用 kubectl 命令速查

```bash
# 发布相关
kubectl rollout status deployment/order-service -n production
kubectl rollout history deployment/order-service -n production
kubectl rollout undo deployment/order-service -n production
kubectl rollout pause deployment/order-service -n production
kubectl rollout resume deployment/order-service -n production

# 调试
kubectl describe pod <pod-name> -n production
kubectl logs <pod-name> -n production --previous
kubectl exec -it <pod-name> -n production -- /bin/sh

# 资源
kubectl top pods -n production -l app=order-service
kubectl get events -n production --sort-by='.lastTimestamp'
```

### 13.2 参考资料

- Kubernetes 官方部署文档：https://kubernetes.io/docs/concepts/workloads/
- ArgoCD 文档：https://argo-cd.readthedocs.io/
- Argo Rollouts：https://argoproj.github.io/rollouts/
- 公司 GitOps 实践：https://wiki.example.com/devops/gitops
